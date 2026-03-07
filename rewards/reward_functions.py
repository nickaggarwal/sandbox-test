"""
Multi-objective reward functions for evaluating Calendly-like scheduling code.

Reward Categories:
1. Functional Correctness (syntax, tests, conflict detection)
2. Code Quality (readability, modularity, efficiency)
3. Domain-Specific Scheduling Logic (buffers, timezones, masking)
"""
import ast
import hashlib
import os
import re
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class RewardBreakdown:
    """Detailed breakdown of all reward components."""
    # Functional Correctness
    syntax_success: float = 0.0           # +1
    test_pass_rate: float = 0.0           # 0 to +10
    zero_conflict: float = 0.0            # +5

    # Code Quality
    readability: float = 0.0              # +2
    modularity: float = 0.0              # +3
    efficiency_penalty: float = 0.0       # -0.1 per issue

    # Domain-Specific
    buffer_enforcement: float = 0.0       # +4
    timezone_consistency: float = 0.0     # +5
    availability_masking: float = 0.0     # +3

    # Weights (adjustable during training)
    w_correctness: float = 1.0
    w_quality: float = 0.5
    w_domain: float = 0.8

    @property
    def correctness_total(self) -> float:
        return self.syntax_success + self.test_pass_rate + self.zero_conflict

    @property
    def quality_total(self) -> float:
        return self.readability + self.modularity + self.efficiency_penalty

    @property
    def domain_total(self) -> float:
        return (
            self.buffer_enforcement
            + self.timezone_consistency
            + self.availability_masking
        )

    @property
    def total_reward(self) -> float:
        return (
            self.w_correctness * self.correctness_total
            + self.w_quality * self.quality_total
            + self.w_domain * self.domain_total
        )

    def to_dict(self) -> dict:
        return {
            'syntax_success': self.syntax_success,
            'test_pass_rate': self.test_pass_rate,
            'zero_conflict': self.zero_conflict,
            'readability': self.readability,
            'modularity': self.modularity,
            'efficiency_penalty': self.efficiency_penalty,
            'buffer_enforcement': self.buffer_enforcement,
            'timezone_consistency': self.timezone_consistency,
            'availability_masking': self.availability_masking,
            'correctness_total': self.correctness_total,
            'quality_total': self.quality_total,
            'domain_total': self.domain_total,
            'total_reward': self.total_reward,
            'weights': {
                'correctness': self.w_correctness,
                'quality': self.w_quality,
                'domain': self.w_domain,
            },
        }


class SyntaxReward:
    """Reward +1 for syntactically correct Python code."""

    @staticmethod
    def evaluate(code):
        try:
            ast.parse(code)
            return 1.0
        except SyntaxError:
            return 0.0


class TestPassRateReward:
    """Reward 0-10 based on percentage of unit tests passed.

    Uses inline Django test runner instead of subprocess to avoid
    import errors and silent failures. Results are cached by code hash.
    """

    _cache = {}

    @classmethod
    def clear_cache(cls):
        cls._cache.clear()

    @classmethod
    def evaluate(cls, project_dir, code_hash=''):
        """Run Django tests inline and return (score, details)."""
        if code_hash and code_hash in cls._cache:
            return cls._cache[code_hash]

        score = 0.0
        details = {'passed': 0, 'failed': 0, 'total': 0, 'errors': []}

        try:
            import django
            from django.test.runner import DiscoverRunner
            from django.test.utils import (
                setup_test_environment,
                teardown_test_environment,
            )

            runner = DiscoverRunner(
                verbosity=0, interactive=False, failfast=False,
            )
            suite = runner.build_suite(['scheduling'])

            old_config = runner.setup_databases()
            try:
                setup_test_environment()
                result = runner.run_suite(suite)
                teardown_test_environment()

                total = result.testsRun
                failed = len(result.failures) + len(result.errors)
                passed = total - failed

                if total > 0:
                    score = (passed / total) * 10.0

                details = {
                    'passed': passed,
                    'failed': failed,
                    'total': total,
                    'rate': passed / max(total, 1),
                    'errors': [str(e[1])[:200] for e in result.errors[:3]],
                    'failures': [str(f[1])[:200]
                                 for f in result.failures[:3]],
                }
            finally:
                runner.teardown_databases(old_config)

        except Exception as e:
            details = {
                'passed': 0, 'failed': 0, 'total': 0,
                'error': str(e)[:300],
            }

        if code_hash:
            cls._cache[code_hash] = (score, details)

        return score, details


class ZeroConflictReward:
    """Reward +5 for correct conflict detection logic."""

    @staticmethod
    def evaluate(code):
        indicators = [
            r'conflict',
            r'overlap',
            r'buffer_start.*buffer_end',
            r'__lt.*__gt|__gt.*__lt',
            r'filter.*status.*confirmed',
        ]
        found = sum(
            1 for pattern in indicators
            if re.search(pattern, code, re.IGNORECASE)
        )
        return min(5.0, (found / len(indicators)) * 5.0)


class ReadabilityReward:
    """Reward +2 for code following PEP8 and readability conventions."""

    @staticmethod
    def evaluate(code):
        score = 2.0
        lines = code.split('\n')

        long_lines = sum(1 for l in lines if len(l) > 120)
        if long_lines > 5:
            score -= 0.5

        if not re.search(r'""".*"""|\'\'\'.*\'\'\'', code, re.DOTALL):
            score -= 0.3

        if re.search(r'[a-z][A-Z]', code) and not re.search(r'_', code):
            score -= 0.3

        func_count = len(re.findall(r'def \w+', code))
        doc_count = len(re.findall(r'"""', code)) // 2
        if func_count > 0 and doc_count / func_count < 0.3:
            score -= 0.4

        return max(0.0, score)


class ModularityReward:
    """Reward +3 for separation of concerns."""

    @staticmethod
    def evaluate(file_contents):
        score = 0.0
        expected_modules = {
            'models': ['class.*Model', 'models\\.'],
            'engine': ['def.*conflict|def.*buffer|def.*timezone'],
            'views': ['APIView|ViewSet|generics'],
            'serializers': ['Serializer'],
        }
        for module, patterns in expected_modules.items():
            for filename, content in file_contents.items():
                if module in filename.lower():
                    if any(re.search(p, content) for p in patterns):
                        score += 0.75
                        break
        return min(3.0, score)


class EfficiencyPenalty:
    """Penalty -0.1 per inefficiency found."""

    @staticmethod
    def evaluate(code):
        penalty = 0.0
        nested_loops = len(re.findall(r'for .+:\s*\n\s+for .+:', code))
        penalty -= nested_loops * 0.1

        lines = code.split('\n')
        if len(lines) > 500:
            excess = (len(lines) - 500) // 50
            penalty -= excess * 0.1

        n_plus_one = len(re.findall(r'for.*in.*\.objects\.all\(\)', code))
        penalty -= n_plus_one * 0.2

        return max(-3.0, penalty)


class BufferEnforcementReward:
    """Reward +4 for correct buffer time implementation."""

    @staticmethod
    def evaluate(code):
        score = 0.0
        if re.search(r'buffer_before|buffer_after|buffer_start|buffer_end', code):
            score += 1.0
        if re.search(r'timedelta.*buffer', code, re.IGNORECASE):
            score += 1.0
        if re.search(r'buffer_start.*=.*start.*-.*timedelta', code):
            score += 1.0
        if re.search(r'buffer_end.*=.*end.*\+.*timedelta', code):
            score += 1.0
        return min(4.0, score)


class TimezoneConsistencyReward:
    """Reward +5 for correct timezone handling."""

    @staticmethod
    def evaluate(code):
        score = 0.0
        if re.search(r'pytz|zoneinfo', code):
            score += 1.0
        if re.search(r'localize|astimezone', code):
            score += 1.0
        if re.search(r'normalize_to_utc|\.astimezone\(.*UTC\)', code):
            score += 1.0
        if re.search(r'guest_timezone|target_tz|source_tz', code):
            score += 1.0
        if re.search(r'tzinfo.*is.*None|\.tzinfo', code):
            score += 1.0
        return min(5.0, score)


class AvailabilityMaskingReward:
    """Reward +3 for correct availability masking."""

    @staticmethod
    def evaluate(code):
        score = 0.0
        if re.search(r'available.*slot|slot.*available|masked',
                      code, re.IGNORECASE):
            score += 1.0
        if re.search(r'assertNotIn.*reason|assertNotIn.*host_calendar', code):
            score += 1.0
        if re.search(r'override.*available|AvailabilityOverride', code):
            score += 1.0
        return min(3.0, score)


class RewardCalculator:
    """Aggregates all reward components with configurable weights."""

    def __init__(self, w_correctness=1.0, w_quality=0.5, w_domain=0.8):
        self.w_correctness = w_correctness
        self.w_quality = w_quality
        self.w_domain = w_domain

    def evaluate(self, code, project_dir, file_contents=None,
                 run_tests=True):
        """Evaluate code and return full reward breakdown."""
        breakdown = RewardBreakdown(
            w_correctness=self.w_correctness,
            w_quality=self.w_quality,
            w_domain=self.w_domain,
        )

        # 1. Functional Correctness
        breakdown.syntax_success = SyntaxReward.evaluate(code)

        if run_tests and breakdown.syntax_success > 0:
            code_hash = hashlib.md5(code.encode()).hexdigest()
            breakdown.test_pass_rate, _ = TestPassRateReward.evaluate(
                project_dir, code_hash=code_hash,
            )

        breakdown.zero_conflict = ZeroConflictReward.evaluate(code)

        # 2. Code Quality
        breakdown.readability = ReadabilityReward.evaluate(code)

        if file_contents:
            breakdown.modularity = ModularityReward.evaluate(file_contents)
        else:
            breakdown.modularity = 1.5

        breakdown.efficiency_penalty = EfficiencyPenalty.evaluate(code)

        # 3. Domain-Specific
        breakdown.buffer_enforcement = BufferEnforcementReward.evaluate(code)
        breakdown.timezone_consistency = TimezoneConsistencyReward.evaluate(
            code
        )
        breakdown.availability_masking = AvailabilityMaskingReward.evaluate(
            code
        )

        return breakdown

    def update_weights(self, epoch, total_epochs):
        """Curriculum learning: shift from correctness to quality."""
        progress = epoch / max(total_epochs, 1)
        self.w_correctness = max(0.5, 1.0 - 0.3 * progress)
        self.w_quality = min(1.0, 0.3 + 0.7 * progress)
        self.w_domain = 0.8
