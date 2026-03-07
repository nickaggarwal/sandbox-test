"""
Gymnasium environment for RL-based code generation/improvement
of a Calendly-like scheduling app.

The agent starts with a *skeleton* of the scheduling engine (method
bodies stripped) and must learn which code actions restore/improve
the implementation to maximise the multi-objective reward.

Key improvements over v0:
- Skeleton-based reset: agent starts with incomplete code
- All 16 actions have templates and pattern-exists checks
- complete_implementation is no longer an instant-terminate
- Exploration bonus and novelty reward
- close() restores original engine.py
"""
import os
import re
import hashlib
import shutil
from typing import Any, Dict, List, Optional, Tuple

import gymnasium as gym
import numpy as np
from gymnasium import spaces

from .reward_functions import RewardCalculator, RewardBreakdown, TestPassRateReward
from .skeleton import generate_skeleton, get_stripped_methods, STRIPPABLE_METHODS


# ── Code modification actions the RL agent can take ──────────────
CODE_ACTIONS = [
    'noop',                     # 0
    'add_conflict_detection',   # 1
    'add_buffer_enforcement',   # 2
    'add_timezone_handling',    # 3
    'add_availability_masking', # 4
    'add_booking_creation',     # 5
    'add_availability_override',# 6
    'refactor_modularity',      # 7
    'improve_readability',      # 8
    'optimize_queries',         # 9
    'add_daily_limit',          # 10
    'add_dst_handling',         # 11
    'add_slot_interval',        # 12
    'fix_syntax',               # 13
    'add_cancellation',         # 14
    'complete_implementation',  # 15
]


# ── Code templates for each action ──────────────────────────────
CODE_TEMPLATES = {
    'add_conflict_detection': '''
    @staticmethod
    def has_conflict(host_id, start, end, buffer_before=0, buffer_after=0, exclude_booking_id=None):
        """Check if proposed time conflicts with existing bookings."""
        proposed_buffer_start = start - timedelta(minutes=buffer_before)
        proposed_buffer_end = end + timedelta(minutes=buffer_after)
        conflicts = Booking.objects.filter(
            host_id=host_id, status='confirmed',
        ).exclude(id=exclude_booking_id).filter(
            buffer_start__lt=proposed_buffer_end,
            buffer_end__gt=proposed_buffer_start,
        )
        return conflicts.exists()

    @staticmethod
    def get_conflicts(host_id, start, end):
        """Return all bookings that conflict with the proposed window."""
        return list(
            Booking.objects.filter(
                host_id=host_id, status='confirmed',
                buffer_start__lt=end, buffer_end__gt=start,
            ).order_by('start_time')
        )
''',
    'add_buffer_enforcement': '''
    @staticmethod
    def enforce_buffer(event_type, start):
        """Calculate meeting and buffer boundaries."""
        buffer_start = start - timedelta(minutes=event_type.buffer_before_minutes)
        meeting_start = start
        meeting_end = start + timedelta(minutes=event_type.duration_minutes)
        buffer_end = meeting_end + timedelta(minutes=event_type.buffer_after_minutes)
        return buffer_start, meeting_start, meeting_end, buffer_end
''',
    'add_timezone_handling': '''
    @staticmethod
    def convert_timezone(dt, source_tz, target_tz):
        """Convert datetime between timezones with DST handling."""
        src = pytz.timezone(source_tz)
        tgt = pytz.timezone(target_tz)
        if dt.tzinfo is None:
            dt = src.localize(dt)
        else:
            dt = dt.astimezone(src)
        return dt.astimezone(tgt)

    @staticmethod
    def normalize_to_utc(dt, source_tz):
        """Convert any datetime to UTC for storage."""
        src = pytz.timezone(source_tz)
        if dt.tzinfo is None:
            dt = src.localize(dt)
        return dt.astimezone(pytz.UTC)

    @staticmethod
    def display_in_timezone(dt, target_tz):
        """Convert a UTC datetime to a display timezone."""
        tgt = pytz.timezone(target_tz)
        if dt.tzinfo is None:
            dt = pytz.UTC.localize(dt)
        return dt.astimezone(tgt)
''',
    'add_availability_masking': '''
    @classmethod
    def get_available_slots(cls, host_id, event_type, target_date, guest_timezone='UTC', slot_interval_minutes=15):
        """Get available slots with private details masked."""
        host_tz_name = cls._get_host_timezone(host_id, target_date)
        host_tz = pytz.timezone(host_tz_name)
        guest_tz = pytz.timezone(guest_timezone)
        override = AvailabilityOverride.objects.filter(host_id=host_id, date=target_date).first()
        if override and not override.is_available:
            return []
        windows = cls._get_day_windows(host_id, target_date, override, host_tz)
        if not windows:
            return []
        slots = []
        duration = timedelta(minutes=event_type.duration_minutes)
        interval = timedelta(minutes=slot_interval_minutes)
        for window_start, window_end in windows:
            current = window_start
            while current + duration <= window_end:
                buf_start, meet_start, meet_end, buf_end = cls.enforce_buffer(event_type, current)
                if buf_start >= window_start and buf_end <= window_end:
                    if not cls.has_conflict(host_id, meet_start, meet_end, event_type.buffer_before_minutes, event_type.buffer_after_minutes):
                        if cls._check_daily_limit(host_id, event_type, target_date, host_tz):
                            guest_start = meet_start.astimezone(guest_tz)
                            guest_end = meet_end.astimezone(guest_tz)
                            slots.append({
                                'start_time': guest_start.isoformat(),
                                'end_time': guest_end.isoformat(),
                                'available': True,
                            })
                current += interval
        return slots

    @classmethod
    def get_masked_availability(cls, host_id, event_type, start_date, end_date, guest_timezone='UTC'):
        """Get availability for a date range with private details masked."""
        result = {}
        current_date = start_date
        while current_date <= end_date:
            day_slots = cls.get_available_slots(host_id, event_type, current_date, guest_timezone)
            result[current_date.isoformat()] = {
                'date': current_date.isoformat(),
                'slots': day_slots,
                'has_availability': len(day_slots) > 0,
            }
            current_date += timedelta(days=1)
        return result
''',
    'add_booking_creation': '''
    @classmethod
    def create_booking(cls, event_type, start_time_utc, guest_name, guest_email, guest_timezone='UTC', notes=''):
        """Create booking with full validation."""
        if start_time_utc.tzinfo is None:
            start_time_utc = pytz.UTC.localize(start_time_utc)
        else:
            start_time_utc = start_time_utc.astimezone(pytz.UTC)
        buf_start, meet_start, meet_end, buf_end = cls.enforce_buffer(event_type, start_time_utc)
        if cls.has_conflict(event_type.host_id, meet_start, meet_end, event_type.buffer_before_minutes, event_type.buffer_after_minutes):
            raise ValueError('Time slot conflicts with existing booking.')
        booking = Booking(
            event_type=event_type, host=event_type.host,
            guest_name=guest_name, guest_email=guest_email,
            guest_timezone=guest_timezone,
            start_time=meet_start, end_time=meet_end,
            buffer_start=buf_start, buffer_end=buf_end,
            notes=notes,
        )
        booking.save()
        return booking
''',
    'add_availability_override': '''
class AvailabilityOverride(models.Model):
    host = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    date = models.DateField()
    is_available = models.BooleanField(default=False)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    timezone = models.CharField(max_length=50, default='UTC')
''',
    'add_cancellation': '''
    def cancel(self):
        """Cancel this booking."""
        self.status = 'cancelled'
        self.cancelled_at = timezone.now()
        self.save()
''',
    'add_daily_limit': '''
    @staticmethod
    def _check_daily_limit(host_id, event_type, target_date, host_tz):
        """Check if daily booking limit has been reached."""
        if event_type.max_bookings_per_day == 0:
            return True
        from datetime import time as time_cls
        day_start = host_tz.localize(datetime.combine(target_date, time_cls.min))
        day_end = host_tz.localize(datetime.combine(target_date, time_cls.max))
        count = Booking.objects.filter(
            host_id=host_id, event_type=event_type,
            status='confirmed',
            start_time__gte=day_start, start_time__lt=day_end,
        ).count()
        return count < event_type.max_bookings_per_day
''',
    'add_dst_handling': '''
    @staticmethod
    def convert_timezone(dt, source_tz, target_tz):
        """Convert datetime with full DST awareness."""
        src = pytz.timezone(source_tz)
        tgt = pytz.timezone(target_tz)
        if dt.tzinfo is None:
            dt = src.localize(dt)
        return dt.astimezone(tgt)
''',
    # ── Five previously-missing templates ────────────────────────
    'fix_syntax': '''
    # Syntax-fix pass: ensure all imports are present
    @staticmethod
    def _validate_imports():
        """Verify scheduling engine imports are intact."""
        required = ['datetime', 'timedelta', 'pytz', 'Booking', 'EventType']
        import sys
        mod = sys.modules.get('scheduling.engine')
        missing = [r for r in required if not hasattr(mod, r)]
        return len(missing) == 0
''',
    'refactor_modularity': '''
    # Refactored helper: extracted from get_available_slots
    @staticmethod
    def _is_within_availability(host_id, local_start, local_end):
        """Check if a time falls within the host availability windows."""
        target_date = local_start.date()
        override = AvailabilityOverride.objects.filter(
            host_id=host_id, date=target_date
        ).first()
        if override:
            if not override.is_available:
                return False
            if override.start_time and override.end_time:
                return (local_start.time() >= override.start_time
                        and local_end.time() <= override.end_time)
        day_of_week = target_date.weekday()
        availabilities = Availability.objects.filter(
            host_id=host_id, day_of_week=day_of_week
        )
        for avail in availabilities:
            if (local_start.time() >= avail.start_time
                    and local_end.time() <= avail.end_time):
                return True
        return False
''',
    'improve_readability': '''
    # Enhanced docstrings and type annotations
    @staticmethod
    def _get_host_timezone(host_id: int, for_date) -> str:
        """Determine the host timezone for a given date.

        Priority order:
        1. Date-specific AvailabilityOverride timezone
        2. Default Availability timezone
        3. Fallback to UTC
        """
        override = AvailabilityOverride.objects.filter(
            host_id=host_id, date=for_date
        ).first()
        if override and override.timezone:
            return override.timezone
        avail = Availability.objects.filter(host_id=host_id).first()
        if avail:
            return avail.timezone
        return 'UTC'
''',
    'optimize_queries': '''
    # Optimised: use select_related to avoid N+1 queries
    @staticmethod
    def get_host_bookings(host_id, start_date, end_date):
        """Retrieve host bookings with optimised queries (select_related)."""
        return list(
            Booking.objects.filter(
                host_id=host_id,
                status='confirmed',
                start_time__date__gte=start_date,
                start_time__date__lte=end_date,
            ).select_related('event_type', 'host')
            .order_by('start_time')
        )
''',
    'add_slot_interval': '''
    @classmethod
    def get_slots_with_interval(cls, host_id, event_type, target_date,
                                slot_interval_minutes=15, guest_timezone='UTC'):
        """Generate candidate slots at configurable intervals."""
        return cls.get_available_slots(
            host_id, event_type, target_date,
            guest_timezone=guest_timezone,
            slot_interval_minutes=slot_interval_minutes,
        )
''',
    'complete_implementation': '''
    # Signal: all methods implemented — run full validation
    pass
''',
}


# Patterns to detect if an action's code already exists
_PATTERN_EXISTS = {
    'add_conflict_detection': r'def has_conflict',
    'add_buffer_enforcement': r'def enforce_buffer',
    'add_timezone_handling': r'def convert_timezone',
    'add_availability_masking': r'def get_available_slots',
    'add_booking_creation': r'def create_booking',
    'add_availability_override': r'class AvailabilityOverride',
    'add_cancellation': r'def cancel',
    'add_daily_limit': r'def _check_daily_limit',
    'add_dst_handling': r'def convert_timezone',
    'add_slot_interval': r'def get_slots_with_interval|slot_interval',
    'fix_syntax': r'def _validate_imports',
    'refactor_modularity': r'def _is_within_availability',
    'improve_readability': r'Priority order',
    'optimize_queries': r'select_related',
    'complete_implementation': r'__complete_marker__',
}

# Map action -> target file
_TARGET_FILE = {
    'add_conflict_detection': 'engine.py',
    'add_buffer_enforcement': 'engine.py',
    'add_timezone_handling': 'engine.py',
    'add_availability_masking': 'engine.py',
    'add_booking_creation': 'engine.py',
    'add_availability_override': 'models.py',
    'refactor_modularity': 'engine.py',
    'improve_readability': 'engine.py',
    'optimize_queries': 'engine.py',
    'add_daily_limit': 'engine.py',
    'add_dst_handling': 'engine.py',
    'add_slot_interval': 'engine.py',
    'fix_syntax': 'engine.py',
    'add_cancellation': 'models.py',
    'complete_implementation': 'engine.py',
}


class SchedulingCodeEnv(gym.Env):
    """
    RL environment where an agent restores a skeleton scheduling engine.

    Observation: vector encoding of current reward signals + actions bitmap
    Action: discrete selection of code modifications to apply
    Reward: multi-objective score from RewardCalculator (improvement-based)
    """

    metadata = {'render_modes': ['human', 'ansi']}

    def __init__(
        self,
        project_dir: str = '.',
        max_steps: int = 20,
        render_mode: Optional[str] = None,
        w_correctness: float = 1.0,
        w_quality: float = 0.5,
        w_domain: float = 0.8,
        difficulty: float = 1.0,
    ):
        super().__init__()

        self.project_dir = os.path.abspath(project_dir)
        self.max_steps = max_steps
        self.render_mode = render_mode
        self.difficulty = difficulty

        self.reward_calculator = RewardCalculator(
            w_correctness=w_correctness,
            w_quality=w_quality,
            w_domain=w_domain,
        )

        # Action space: choose one of the code actions
        self.action_space = spaces.Discrete(len(CODE_ACTIONS))

        # Observation space: 11 reward features + 16 action bitmap = 27
        self.observation_space = spaces.Box(
            low=-10.0,
            high=30.0,
            shape=(27,),
            dtype=np.float32,
        )

        # Keep a backup of the original engine.py for restoration
        self._engine_path = os.path.join(
            self.project_dir, 'scheduling', 'engine.py'
        )
        self._engine_backup = None
        if os.path.exists(self._engine_path):
            with open(self._engine_path, 'r') as f:
                self._engine_backup = f.read()

        self._reset_state()

    def _reset_state(self):
        self.current_step = 0
        self.actions_taken = set()
        self.action_history = []
        self.current_code = ''
        self.code_sections = {}
        self.last_breakdown = None
        self.cumulative_reward = 0.0
        self.history = []
        self._unique_actions_applied = set()
        # Clear test cache between episodes so code changes are re-tested
        TestPassRateReward.clear_cache()

    def reset(
        self,
        seed: Optional[int] = None,
        options: Optional[dict] = None,
    ) -> Tuple[np.ndarray, dict]:
        super().reset(seed=seed)
        self._reset_state()

        # Restore original engine.py then create skeleton
        self._restore_engine()
        self._prepare_skeleton_codebase()

        # Load skeleton code
        self._load_project_code()

        # Initial evaluation (no tests — skeleton will mostly fail)
        self.last_breakdown = self._evaluate_current_code(run_tests=False)

        obs = self._get_observation()
        info = {
            'reward_breakdown': self.last_breakdown.to_dict(),
            'step': 0,
        }

        return obs, info

    def step(
        self, action: int,
    ) -> Tuple[np.ndarray, float, bool, bool, dict]:
        self.current_step += 1
        action_name = CODE_ACTIONS[action]

        # Apply the action
        applied = self._apply_action(action_name)
        self.actions_taken.add(action)
        self.action_history.append(action_name)

        # Run tests every 3 steps, on the last step, or when code changed
        run_tests = (
            applied
            or self.current_step % 3 == 0
            or self.current_step >= self.max_steps
        )
        new_breakdown = self._evaluate_current_code(run_tests=run_tests)

        # --- Reward shaping ---
        old_total = self.last_breakdown.total_reward if self.last_breakdown else 0
        new_total = new_breakdown.total_reward

        # Base reward: improvement in total score
        reward = new_total - old_total

        # Bonus for successfully applying a new action
        if applied and action_name != 'noop':
            reward += 0.5
            # Extra novelty bonus for first time applying this action
            if action_name not in self._unique_actions_applied:
                self._unique_actions_applied.add(action_name)
                reward += 0.3

        # Penalty for failed/redundant actions
        if not applied and action_name != 'noop':
            reward -= 0.2

        # Penalty for noop (encourage exploration)
        if action_name == 'noop':
            reward -= 0.1

        # Bonus for high test pass rate
        if new_breakdown.test_pass_rate > 8.0:
            reward += 0.5

        self.last_breakdown = new_breakdown
        self.cumulative_reward += reward

        # --- Termination ---
        # Terminate when score is high enough (agent solved it)
        terminated = new_total >= 25.0
        truncated = self.current_step >= self.max_steps

        # complete_implementation now triggers a full test run instead of
        # instant termination — only terminates if tests are also good
        if action_name == 'complete_implementation' and applied:
            full_bd = self._evaluate_current_code(run_tests=True)
            if full_bd.total_reward >= 20.0:
                reward += 2.0  # large bonus for declaring done at right time
                terminated = True
            else:
                reward -= 0.5  # penalty for premature completion

        self.history.append({
            'step': self.current_step,
            'action': action_name,
            'applied': applied,
            'reward': reward,
            'total_reward': new_breakdown.total_reward,
        })

        obs = self._get_observation()
        info = {
            'reward_breakdown': new_breakdown.to_dict(),
            'action_name': action_name,
            'applied': applied,
            'step': self.current_step,
            'cumulative_reward': self.cumulative_reward,
        }

        return obs, reward, terminated, truncated, info

    # ── Observation ──────────────────────────────────────────────

    def _get_observation(self) -> np.ndarray:
        bd = self.last_breakdown or RewardBreakdown()

        features = [
            bd.syntax_success,
            bd.test_pass_rate,
            bd.zero_conflict,
            bd.readability,
            bd.modularity,
            bd.efficiency_penalty,
            bd.buffer_enforcement,
            bd.timezone_consistency,
            bd.availability_masking,
            float(self.current_step),
            bd.total_reward,
        ]

        # Actions-taken bitmap (16 bits)
        bitmap = [
            1.0 if i in self.actions_taken else 0.0
            for i in range(len(CODE_ACTIONS))
        ]

        obs = np.array(features + bitmap, dtype=np.float32)
        return obs

    # ── Code Loading ─────────────────────────────────────────────

    def _load_project_code(self):
        """Load all Python files from the scheduling app."""
        scheduling_dir = os.path.join(self.project_dir, 'scheduling')
        self.code_sections = {}

        if os.path.isdir(scheduling_dir):
            for fname in os.listdir(scheduling_dir):
                if fname.endswith('.py'):
                    fpath = os.path.join(scheduling_dir, fname)
                    with open(fpath, 'r') as f:
                        self.code_sections[fname] = f.read()

        self.current_code = '\n\n'.join(self.code_sections.values())

    # ── Skeleton Management ──────────────────────────────────────

    def _prepare_skeleton_codebase(self):
        """Replace engine.py with a skeleton version.

        The skeleton has method bodies stripped so the RL agent
        must restore them through actions.
        """
        if not os.path.exists(self._engine_path):
            return

        with open(self._engine_path, 'r') as f:
            full_code = f.read()

        skeleton = generate_skeleton(full_code, difficulty=self.difficulty)

        with open(self._engine_path, 'w') as f:
            f.write(skeleton)

    def _restore_engine(self):
        """Restore the original engine.py from backup."""
        if self._engine_backup and os.path.exists(self._engine_path):
            with open(self._engine_path, 'w') as f:
                f.write(self._engine_backup)

    # ── Action Application ───────────────────────────────────────

    def _apply_action(self, action_name: str) -> bool:
        """Apply a code action. Returns True if successfully applied."""
        if action_name == 'noop':
            return False

        if action_name == 'complete_implementation':
            # Mark completion — only meaningful once
            if '__complete_marker__' not in self.current_code:
                target = os.path.join(
                    self.project_dir, 'scheduling', 'engine.py',
                )
                if os.path.exists(target):
                    with open(target, 'a') as f:
                        f.write('\n# __complete_marker__\n')
                    self._load_project_code()
                    return True
            return False

        template = CODE_TEMPLATES.get(action_name)
        if not template:
            return False

        # Check if this code pattern already exists
        if self._pattern_exists(action_name):
            return False

        # Determine target file
        target_file = _TARGET_FILE.get(action_name)
        if not target_file:
            return False

        target_path = os.path.join(
            self.project_dir, 'scheduling', target_file,
        )

        if not os.path.exists(target_path):
            return False

        with open(target_path, 'r') as f:
            content = f.read()

        # Check for NotImplementedError stubs and replace them
        replaced = self._replace_stub(content, action_name, template)
        if replaced is not None:
            content = replaced
        elif 'class ' in template and 'Model' in template:
            # Model class — append to module
            content += '\n' + template
        elif 'def ' in template:
            # Method — append inside SchedulingEngine class
            if 'class SchedulingEngine' in content:
                content = content.rstrip() + '\n' + template + '\n'
            else:
                content += '\n' + template
        else:
            # Misc code — append
            content += '\n' + template

        with open(target_path, 'w') as f:
            f.write(content)

        # Reload and verify syntax
        self._load_project_code()

        import ast
        try:
            ast.parse(self.code_sections.get(target_file, ''))
        except SyntaxError:
            # Revert on syntax error
            with open(target_path, 'w') as f:
                old = content  # already written; restore from before
                f.write(content)
            return False

        return True

    def _replace_stub(self, content, action_name, template):
        """Try to replace a NotImplementedError stub for this action.

        The skeleton generator replaces method bodies with
        ``raise NotImplementedError("method_name not implemented yet")``.
        If we find such a stub for any method associated with this action,
        replace the entire class body section.

        Returns modified content string or None if no stub found.
        """
        # Map action -> method names it restores
        action_to_methods = {}
        for method, action in STRIPPABLE_METHODS.items():
            action_to_methods.setdefault(action, []).append(method)

        methods = action_to_methods.get(action_name, [])
        if not methods:
            return None

        modified = False
        for method_name in methods:
            stub_pattern = (
                r'(def {}\([^)]*\).*?)'
                r'raise NotImplementedError\('
                r'"{} not implemented yet"\)'.format(
                    re.escape(method_name), re.escape(method_name),
                )
            )
            match = re.search(stub_pattern, content, re.DOTALL)
            if match:
                # Find the indentation of the raise statement
                raise_pos = content.find(
                    'raise NotImplementedError("{} not implemented yet")'.format(
                        method_name
                    )
                )
                if raise_pos >= 0:
                    # Replace the raise with the template body
                    line_start = content.rfind('\n', 0, raise_pos) + 1
                    line_end = content.find('\n', raise_pos)
                    if line_end < 0:
                        line_end = len(content)

                    # Extract method body from template
                    body_lines = self._extract_method_body(
                        template, method_name
                    )
                    if body_lines is not None:
                        content = (
                            content[:line_start]
                            + body_lines
                            + content[line_end:]
                        )
                        modified = True

        return content if modified else None

    @staticmethod
    def _extract_method_body(template, method_name):
        """Extract the body of a specific method from a template string.

        Returns the body lines (including indentation) or None.
        """
        lines = template.split('\n')
        inside = False
        body_lines = []
        def_indent = 0

        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith('def {}'.format(method_name)):
                inside = True
                def_indent = len(line) - len(stripped)
                continue
            if inside:
                if stripped.startswith('def ') and not stripped.startswith(
                    'def {}'.format(method_name)
                ):
                    break
                # Skip docstrings (we keep the original docstring from skeleton)
                if stripped.startswith('"""') or stripped.startswith("'''"):
                    # Skip single-line docstring
                    quote = stripped[:3]
                    if stripped.count(quote) >= 2:
                        continue
                    # Multi-line: skip until closing
                    continue
                body_lines.append(line)

        if body_lines:
            return '\n'.join(body_lines)
        return None

    def _pattern_exists(self, action_name: str) -> bool:
        """Check if the action's code pattern already exists."""
        pattern = _PATTERN_EXISTS.get(action_name)
        if pattern and re.search(pattern, self.current_code):
            return True
        return False

    # ── Evaluation ───────────────────────────────────────────────

    def _evaluate_current_code(
        self, run_tests: bool = False,
    ) -> RewardBreakdown:
        """Evaluate current code state."""
        return self.reward_calculator.evaluate(
            code=self.current_code,
            project_dir=self.project_dir,
            file_contents=self.code_sections,
            run_tests=run_tests,
        )

    # ── Render ───────────────────────────────────────────────────

    def render(self):
        if self.render_mode in ('human', 'ansi'):
            bd = self.last_breakdown or RewardBreakdown()
            output = (
                f"\n{'='*60}\n"
                f"Step: {self.current_step}/{self.max_steps}\n"
                f"Actions so far: {self.action_history}\n"
                f"{'='*60}\n"
                f"CORRECTNESS  (w={bd.w_correctness:.1f}):\n"
                f"  Syntax:     {bd.syntax_success:.1f}/1\n"
                f"  Tests:      {bd.test_pass_rate:.1f}/10\n"
                f"  Conflicts:  {bd.zero_conflict:.1f}/5\n"
                f"QUALITY      (w={bd.w_quality:.1f}):\n"
                f"  Readability:{bd.readability:.1f}/2\n"
                f"  Modularity: {bd.modularity:.1f}/3\n"
                f"  Efficiency: {bd.efficiency_penalty:.1f}\n"
                f"DOMAIN       (w={bd.w_domain:.1f}):\n"
                f"  Buffers:    {bd.buffer_enforcement:.1f}/4\n"
                f"  Timezones:  {bd.timezone_consistency:.1f}/5\n"
                f"  Masking:    {bd.availability_masking:.1f}/3\n"
                f"{'='*60}\n"
                f"TOTAL REWARD: {bd.total_reward:.2f}\n"
                f"{'='*60}\n"
            )
            if self.render_mode == 'ansi':
                return output
            print(output)

    # ── Cleanup ──────────────────────────────────────────────────

    def close(self):
        """Restore the original engine.py on environment close."""
        self._restore_engine()
        super().close()


# Register the environment
gym.register(
    id='SchedulingCode-v0',
    entry_point='rewards.gym_env:SchedulingCodeEnv',
)
