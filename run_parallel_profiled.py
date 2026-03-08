#!/usr/bin/env python3
"""
Parallel + Profiled Sandbox Runner.

Launches multiple sandboxes concurrently (Daytona SDK or E2B),
profiles every step (create, upload, install, test, train, cleanup),
and produces a comparative report across providers.
"""
import argparse
import json
import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime

# Fix SSL cert verification on macOS
try:
    import certifi
    os.environ.setdefault('SSL_CERT_FILE', certifi.where())
except ImportError:
    pass

# Load API keys from .env file or environment
def _load_env():
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, val = line.split('=', 1)
                    os.environ.setdefault(key.strip(), val.strip())

_load_env()

DAYTONA_API_KEY = os.environ.get('DAYTONA_API_KEY', '')
E2B_API_KEY = os.environ.get('E2B_API_KEY', '')
BLAXEL_API_KEY = os.environ.get('BLAXEL_API_KEY', '')


# ── Profiling Timer ─────────────────────────────────────────────────

@dataclass
class StepProfile:
    name: str
    started_at: float = 0.0
    ended_at: float = 0.0
    duration_s: float = 0.0
    success: bool = False
    detail: str = ''

    def to_dict(self):
        return {
            'name': self.name,
            'duration_s': round(self.duration_s, 2),
            'success': self.success,
            'detail': self.detail[:300],
        }


@dataclass
class SandboxProfile:
    sandbox_label: str
    provider: str = 'daytona'
    sandbox_id: str = ''
    config: dict = field(default_factory=dict)
    steps: list = field(default_factory=list)
    total_duration_s: float = 0.0
    tests_passed: int = 0
    tests_total: int = 0
    rl_best_reward: float = 0.0
    rl_final_avg: float = 0.0
    rl_best_actions: list = field(default_factory=list)
    error: str = ''

    def add_step(self, name):
        step = StepProfile(name=name, started_at=time.time())
        self.steps.append(step)
        return step

    def finish_step(self, step, success=True, detail=''):
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = success
        step.detail = detail

    @property
    def step_summary(self):
        lines = []
        for s in self.steps:
            status = 'OK' if s.success else 'FAIL'
            lines.append(f'  {s.name:<25} {s.duration_s:>7.1f}s  [{status}]')
        return '\n'.join(lines)

    def to_dict(self):
        return {
            'sandbox_label': self.sandbox_label,
            'provider': self.provider,
            'sandbox_id': self.sandbox_id,
            'config': self.config,
            'total_duration_s': round(self.total_duration_s, 2),
            'tests_passed': self.tests_passed,
            'tests_total': self.tests_total,
            'rl_best_reward': self.rl_best_reward,
            'rl_final_avg': self.rl_final_avg,
            'rl_best_actions': self.rl_best_actions,
            'steps': [s.to_dict() for s in self.steps],
            'error': self.error,
        }


# ── Provider Factory ───────────────────────────────────────────────

def create_runner(provider):
    """Create a sandbox runner for the given provider."""
    if provider == 'daytona':
        from daytona_sandbox import DaytonaSandboxRunner
        return DaytonaSandboxRunner(api_key=DAYTONA_API_KEY)
    elif provider == 'e2b':
        from e2b_sandbox import E2BSandboxRunner
        return E2BSandboxRunner(api_key=E2B_API_KEY)
    elif provider == 'blaxel':
        from blaxel_sandbox import BlaxelSandboxRunner
        return BlaxelSandboxRunner(api_key=BLAXEL_API_KEY)
    elif provider == 'modal':
        from modal_sandbox import ModalSandboxRunner
        return ModalSandboxRunner()
    else:
        raise ValueError(f'Unknown provider: {provider}')


def get_results_path(provider):
    """Get the remote results file path for a provider."""
    if provider == 'daytona':
        return '/home/daytona/app/rl_output/training_results.json'
    elif provider == 'blaxel':
        return '/blaxel/app/rl_output/training_results.json'
    elif provider == 'modal':
        return '/root/app/rl_output/training_results.json'
    return '/home/user/app/rl_output/training_results.json'


# ── Filesystem Benchmark Pipeline ─────────────────────────────────

def run_profiled_fs_benchmark(
    label,
    provider='daytona',
    log_prefix='',
    stagger_delay=0,
):
    """Run the filesystem benchmark pipeline with full profiling."""
    if stagger_delay > 0:
        print(f'{log_prefix}[{label}] Staggering start by {stagger_delay}s...')
        time.sleep(stagger_delay)

    profile = SandboxProfile(
        sandbox_label=label,
        provider=provider,
        config={'benchmark': 'filesystem'},
    )
    pipeline_start = time.time()

    def log(msg):
        print(f'{log_prefix}[{label}] {msg}')

    runner = create_runner(provider)

    try:
        # Create sandbox
        step = profile.add_step('create_sandbox')
        log(f'Creating {provider} sandbox...')
        try:
            runner.create_sandbox()
            profile.sandbox_id = runner.sandbox_id or ''
            profile.finish_step(step, success=True, detail=profile.sandbox_id)
            log(f'  Sandbox ready: {profile.sandbox_id[:20]}...')
        except Exception as e:
            profile.finish_step(step, success=False, detail=str(e))
            profile.error = f'Create failed: {e}'
            log(f'  CREATE FAILED: {e}')
            return profile

        # Run filesystem benchmark steps
        from filesystem_benchmark import run_filesystem_benchmark
        log('Running filesystem benchmark...')
        fs_steps = run_filesystem_benchmark(runner, provider)
        profile.steps.extend(fs_steps)

    finally:
        step = profile.add_step('destroy_sandbox')
        log('Destroying sandbox...')
        try:
            runner.destroy()
            profile.finish_step(step, success=True)
        except Exception as e:
            profile.finish_step(step, success=False, detail=str(e))

    profile.total_duration_s = time.time() - pipeline_start
    log(f'DONE in {profile.total_duration_s:.1f}s')
    return profile


# ── Async Task + Pause/Resume Benchmark Pipeline ──────────────────

def run_profiled_pause_benchmark(
    label,
    provider='daytona',
    log_prefix='',
    stagger_delay=0,
):
    """Run the async task + pause/resume benchmark with full profiling."""
    if stagger_delay > 0:
        print(f'{log_prefix}[{label}] Staggering start by {stagger_delay}s...')
        time.sleep(stagger_delay)

    profile = SandboxProfile(
        sandbox_label=label,
        provider=provider,
        config={'benchmark': 'pause_resume'},
    )
    pipeline_start = time.time()

    def log(msg):
        print(f'{log_prefix}[{label}] {msg}')

    runner = create_runner(provider)

    try:
        # Create sandbox
        step = profile.add_step('create_sandbox')
        log(f'Creating {provider} sandbox...')
        try:
            runner.create_sandbox()
            profile.sandbox_id = runner.sandbox_id or ''
            profile.finish_step(step, success=True, detail=profile.sandbox_id)
            log(f'  Sandbox ready: {profile.sandbox_id[:20]}...')
        except Exception as e:
            profile.finish_step(step, success=False, detail=str(e))
            profile.error = f'Create failed: {e}'
            log(f'  CREATE FAILED: {e}')
            return profile

        # Run async task + pause/resume benchmark
        from async_task_benchmark import run_async_task_benchmark
        log('Running async task + pause/resume benchmark...')
        async_steps = run_async_task_benchmark(runner, provider)
        profile.steps.extend(async_steps)

    finally:
        step = profile.add_step('destroy_sandbox')
        log('Destroying sandbox...')
        try:
            runner.destroy()
            profile.finish_step(step, success=True)
        except Exception as e:
            profile.finish_step(step, success=False, detail=str(e))

    profile.total_duration_s = time.time() - pipeline_start
    log(f'DONE in {profile.total_duration_s:.1f}s')
    return profile


# ── Concurrent Exec Benchmark Pipeline ────────────────────────────

def run_profiled_concurrent_benchmark(
    label,
    provider='daytona',
    log_prefix='',
    stagger_delay=0,
):
    """Run the concurrent exec benchmark pipeline with full profiling."""
    if stagger_delay > 0:
        print(f'{log_prefix}[{label}] Staggering start by {stagger_delay}s...')
        time.sleep(stagger_delay)

    profile = SandboxProfile(
        sandbox_label=label,
        provider=provider,
        config={'benchmark': 'concurrent_exec'},
    )
    pipeline_start = time.time()

    def log(msg):
        print(f'{log_prefix}[{label}] {msg}')

    runner = create_runner(provider)

    try:
        # Create sandbox
        step = profile.add_step('create_sandbox')
        log(f'Creating {provider} sandbox...')
        try:
            runner.create_sandbox()
            profile.sandbox_id = runner.sandbox_id or ''
            profile.finish_step(step, success=True, detail=profile.sandbox_id)
            log(f'  Sandbox ready: {profile.sandbox_id[:20]}...')
        except Exception as e:
            profile.finish_step(step, success=False, detail=str(e))
            profile.error = f'Create failed: {e}'
            log(f'  CREATE FAILED: {e}')
            return profile

        # Run concurrent exec benchmark
        from concurrent_exec_benchmark import run_concurrent_exec_benchmark
        log('Running concurrent exec benchmark...')
        ce_steps = run_concurrent_exec_benchmark(runner, provider)
        profile.steps.extend(ce_steps)

    finally:
        step = profile.add_step('destroy_sandbox')
        log('Destroying sandbox...')
        try:
            runner.destroy()
            profile.finish_step(step, success=True)
        except Exception as e:
            profile.finish_step(step, success=False, detail=str(e))

    profile.total_duration_s = time.time() - pipeline_start
    log(f'DONE in {profile.total_duration_s:.1f}s')
    return profile


# ── Iteration Loop Benchmark Pipeline ─────────────────────────────

def run_profiled_iteration_benchmark(
    label,
    provider='daytona',
    log_prefix='',
    stagger_delay=0,
):
    """Run the iteration loop benchmark pipeline with full profiling."""
    if stagger_delay > 0:
        print(f'{log_prefix}[{label}] Staggering start by {stagger_delay}s...')
        time.sleep(stagger_delay)

    profile = SandboxProfile(
        sandbox_label=label,
        provider=provider,
        config={'benchmark': 'iteration_loop'},
    )
    pipeline_start = time.time()

    def log(msg):
        print(f'{log_prefix}[{label}] {msg}')

    runner = create_runner(provider)

    try:
        # Create sandbox
        step = profile.add_step('create_sandbox')
        log(f'Creating {provider} sandbox...')
        try:
            runner.create_sandbox()
            profile.sandbox_id = runner.sandbox_id or ''
            profile.finish_step(step, success=True, detail=profile.sandbox_id)
            log(f'  Sandbox ready: {profile.sandbox_id[:20]}...')
        except Exception as e:
            profile.finish_step(step, success=False, detail=str(e))
            profile.error = f'Create failed: {e}'
            log(f'  CREATE FAILED: {e}')
            return profile

        # Run iteration loop benchmark
        from iteration_loop_benchmark import run_iteration_loop_benchmark
        log('Running iteration loop benchmark...')
        iter_steps = run_iteration_loop_benchmark(runner, provider)
        profile.steps.extend(iter_steps)

    finally:
        step = profile.add_step('destroy_sandbox')
        log('Destroying sandbox...')
        try:
            runner.destroy()
            profile.finish_step(step, success=True)
        except Exception as e:
            profile.finish_step(step, success=False, detail=str(e))

    profile.total_duration_s = time.time() - pipeline_start
    log(f'DONE in {profile.total_duration_s:.1f}s')
    return profile


# ── Fan-Out Benchmark Pipeline ────────────────────────────────────

def run_profiled_fanout_benchmark(
    label,
    provider='daytona',
    log_prefix='',
    stagger_delay=0,
):
    """Run the multi-sandbox fan-out benchmark with full profiling.

    Unlike other benchmarks, fan-out manages its own sandbox lifecycle
    internally (creates and destroys N sandboxes itself).
    """
    if stagger_delay > 0:
        print(f'{log_prefix}[{label}] Staggering start by {stagger_delay}s...')
        time.sleep(stagger_delay)

    profile = SandboxProfile(
        sandbox_label=label,
        provider=provider,
        config={'benchmark': 'fanout'},
    )
    pipeline_start = time.time()

    def log(msg):
        print(f'{log_prefix}[{label}] {msg}')

    log(f'Running fan-out benchmark ({provider})...')

    api_key = {
        'daytona': DAYTONA_API_KEY,
        'e2b': E2B_API_KEY,
        'blaxel': BLAXEL_API_KEY,
    }.get(provider)

    from fanout_benchmark import run_fanout_benchmark
    fo_steps = run_fanout_benchmark(
        None, provider, api_key=api_key, num_sandboxes=10)
    profile.steps.extend(fo_steps)

    profile.total_duration_s = time.time() - pipeline_start
    log(f'DONE in {profile.total_duration_s:.1f}s')
    return profile


# ── Coding Agent Benchmark Pipeline ──────────────────────────────

def run_profiled_agent_benchmark(
    label,
    provider='e2b',
    log_prefix='',
    stagger_delay=0,
    project_dir='.',
    llm_backend='gemini',
    llm_model=None,
    llm_api_key=None,
    agent_iterations=3,
    reward_threshold=25.0,
    bootstrap_app=False,
):
    """Run the coding agent benchmark with full profiling.

    The coding agent manages its own sandbox lifecycle internally,
    so this wrapper does not create/destroy a sandbox separately.
    """
    if stagger_delay > 0:
        print(f'{log_prefix}[{label}] Staggering start by {stagger_delay}s...')
        time.sleep(stagger_delay)

    profile = SandboxProfile(
        sandbox_label=label,
        provider=provider,
        config={
            'benchmark': 'coding_agent',
            'llm_backend': llm_backend,
            'llm_model': llm_model or 'default',
            'max_iterations': agent_iterations,
            'reward_threshold': reward_threshold,
            'bootstrap_app': bootstrap_app,
        },
    )
    pipeline_start = time.time()

    def log(msg):
        print(f'{log_prefix}[{label}] {msg}')

    log(f'Running coding agent benchmark ({provider}, llm={llm_backend})...')

    from coding_agent_benchmark import run_coding_agent_benchmark
    agent_steps = run_coding_agent_benchmark(
        runner=None,
        provider=provider,
        project_dir=project_dir,
        llm_backend=llm_backend,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
        max_iterations=agent_iterations,
        reward_threshold=reward_threshold,
        bootstrap_app=bootstrap_app,
    )
    profile.steps.extend(agent_steps)

    profile.total_duration_s = time.time() - pipeline_start
    log(f'DONE in {profile.total_duration_s:.1f}s')
    return profile


# ── Network Speed Benchmark Pipeline ─────────────────────────────

def run_profiled_network_benchmark(
    label,
    provider='daytona',
    log_prefix='',
    stagger_delay=0,
):
    """Run the network speed benchmark pipeline with full profiling."""
    if stagger_delay > 0:
        print(f'{log_prefix}[{label}] Staggering start by {stagger_delay}s...')
        time.sleep(stagger_delay)

    profile = SandboxProfile(
        sandbox_label=label,
        provider=provider,
        config={'benchmark': 'network'},
    )
    pipeline_start = time.time()

    def log(msg):
        print(f'{log_prefix}[{label}] {msg}')

    runner = create_runner(provider)

    try:
        # Create sandbox
        step = profile.add_step('create_sandbox')
        log(f'Creating {provider} sandbox...')
        try:
            runner.create_sandbox()
            profile.sandbox_id = runner.sandbox_id or ''
            profile.finish_step(step, success=True, detail=profile.sandbox_id)
            log(f'  Sandbox ready: {profile.sandbox_id[:20]}...')
        except Exception as e:
            profile.finish_step(step, success=False, detail=str(e))
            profile.error = f'Create failed: {e}'
            log(f'  CREATE FAILED: {e}')
            return profile

        # Run network benchmark steps
        from network_benchmark import run_network_benchmark
        log('Running network speed benchmark...')
        net_steps = run_network_benchmark(runner, provider)
        profile.steps.extend(net_steps)

    finally:
        step = profile.add_step('destroy_sandbox')
        log('Destroying sandbox...')
        try:
            runner.destroy()
            profile.finish_step(step, success=True)
        except Exception as e:
            profile.finish_step(step, success=False, detail=str(e))

    profile.total_duration_s = time.time() - pipeline_start
    log(f'DONE in {profile.total_duration_s:.1f}s')
    return profile


# ── Security & Isolation Benchmark Pipeline ──────────────────────

def run_profiled_security_benchmark(
    label,
    provider='daytona',
    log_prefix='',
    stagger_delay=0,
):
    """Run the security & isolation benchmark pipeline with full profiling."""
    if stagger_delay > 0:
        print(f'{log_prefix}[{label}] Staggering start by {stagger_delay}s...')
        time.sleep(stagger_delay)

    profile = SandboxProfile(
        sandbox_label=label,
        provider=provider,
        config={'benchmark': 'security'},
    )
    pipeline_start = time.time()

    def log(msg):
        print(f'{log_prefix}[{label}] {msg}')

    runner = create_runner(provider)

    try:
        # Create sandbox
        step = profile.add_step('create_sandbox')
        log(f'Creating {provider} sandbox...')
        try:
            runner.create_sandbox()
            profile.sandbox_id = runner.sandbox_id or ''
            profile.finish_step(step, success=True, detail=profile.sandbox_id)
            log(f'  Sandbox ready: {profile.sandbox_id[:20]}...')
        except Exception as e:
            profile.finish_step(step, success=False, detail=str(e))
            profile.error = f'Create failed: {e}'
            log(f'  CREATE FAILED: {e}')
            return profile

        # Run security benchmark steps
        from security_benchmark import run_security_benchmark
        log('Running security & isolation benchmark...')
        sec_steps = run_security_benchmark(runner, provider)
        profile.steps.extend(sec_steps)

    finally:
        step = profile.add_step('destroy_sandbox')
        log('Destroying sandbox...')
        try:
            runner.destroy()
            profile.finish_step(step, success=True)
        except Exception as e:
            profile.finish_step(step, success=False, detail=str(e))

    profile.total_duration_s = time.time() - pipeline_start
    log(f'DONE in {profile.total_duration_s:.1f}s')
    return profile


# ── Security Exploits Benchmark Pipeline ──────────────────────────

def run_profiled_security_exploits_benchmark(
    label,
    provider='daytona',
    log_prefix='',
    stagger_delay=0,
):
    """Run the security exploit validation benchmark with full profiling."""
    if stagger_delay > 0:
        print(f'{log_prefix}[{label}] Staggering start by {stagger_delay}s...')
        time.sleep(stagger_delay)

    profile = SandboxProfile(
        sandbox_label=label,
        provider=provider,
        config={'benchmark': 'security-exploits'},
    )
    pipeline_start = time.time()

    def log(msg):
        print(f'{log_prefix}[{label}] {msg}')

    runner = create_runner(provider)

    try:
        # Create sandbox
        step = profile.add_step('create_sandbox')
        log(f'Creating {provider} sandbox...')
        try:
            runner.create_sandbox()
            profile.sandbox_id = runner.sandbox_id or ''
            profile.finish_step(step, success=True, detail=profile.sandbox_id)
            log(f'  Sandbox ready: {profile.sandbox_id[:20]}...')
        except Exception as e:
            profile.finish_step(step, success=False, detail=str(e))
            profile.error = f'Create failed: {e}'
            log(f'  CREATE FAILED: {e}')
            return profile

        # Run security exploit validation steps
        from security_exploits import run_security_exploits_benchmark
        log('Running security exploit validation...')
        exploit_steps = run_security_exploits_benchmark(runner, provider)
        profile.steps.extend(exploit_steps)

    finally:
        step = profile.add_step('destroy_sandbox')
        log('Destroying sandbox...')
        try:
            runner.destroy()
            profile.finish_step(step, success=True)
        except Exception as e:
            profile.finish_step(step, success=False, detail=str(e))

    profile.total_duration_s = time.time() - pipeline_start
    log(f'DONE in {profile.total_duration_s:.1f}s')
    return profile


# ── Docker Image Benchmark Pipeline ──────────────────────────────

def run_profiled_docker_benchmark(
    label,
    provider='daytona',
    log_prefix='',
    stagger_delay=0,
):
    """Run the custom Docker image benchmark with full profiling.

    Like fanout, this benchmark manages its own sandbox lifecycle
    internally (creates sandboxes with custom images).
    """
    if stagger_delay > 0:
        print(f'{log_prefix}[{label}] Staggering start by {stagger_delay}s...')
        time.sleep(stagger_delay)

    profile = SandboxProfile(
        sandbox_label=label,
        provider=provider,
        config={'benchmark': 'docker'},
    )
    pipeline_start = time.time()

    def log(msg):
        print(f'{log_prefix}[{label}] {msg}')

    log(f'Running Docker image benchmark ({provider})...')

    api_key = {
        'daytona': DAYTONA_API_KEY,
        'e2b': E2B_API_KEY,
        'blaxel': BLAXEL_API_KEY,
    }.get(provider)

    from docker_benchmark import run_docker_benchmark
    docker_steps = run_docker_benchmark(
        None, provider, api_key=api_key)
    profile.steps.extend(docker_steps)

    profile.total_duration_s = time.time() - pipeline_start
    log(f'DONE in {profile.total_duration_s:.1f}s')
    return profile


# ── Single Sandbox Pipeline (profiled) ─────────────────────────────

def run_profiled_sandbox(
    label,
    episodes,
    max_steps,
    project_dir,
    provider='daytona',
    log_prefix='',
    stagger_delay=0,
):
    """Run a single sandbox pipeline with full profiling."""

    if stagger_delay > 0:
        print(f'{log_prefix}[{label}] Staggering start by {stagger_delay}s...')
        time.sleep(stagger_delay)

    profile = SandboxProfile(
        sandbox_label=label,
        provider=provider,
        config={'episodes': episodes, 'max_steps': max_steps},
    )
    pipeline_start = time.time()

    def log(msg):
        print(f'{log_prefix}[{label}] {msg}')

    runner = create_runner(provider)

    try:
        # ── Step 1: Create sandbox ──
        step = profile.add_step('create_sandbox')
        log(f'Creating {provider} sandbox...')
        try:
            runner.create_sandbox()
            profile.sandbox_id = runner.sandbox_id or ''
            profile.finish_step(step, success=True,
                                detail=profile.sandbox_id)
            log(f'  Sandbox ready: {profile.sandbox_id[:20]}...')
        except Exception as e:
            profile.finish_step(step, success=False, detail=str(e))
            profile.error = f'Create failed: {e}'
            log(f'  CREATE FAILED: {e}')
            return profile

        # ── Step 2: Upload project ──
        step = profile.add_step('upload_project')
        log('Uploading project...')
        try:
            runner.upload_project(project_dir)
            profile.finish_step(step, success=True)
            log(f'  Upload complete')
        except Exception as e:
            profile.finish_step(step, success=False, detail=str(e))
            log(f'  Upload error: {e}')

        # ── Step 3: Install deps ──
        step = profile.add_step('install_deps')
        log('Installing dependencies...')
        try:
            results = runner.setup_environment()
            all_ok = all(r['exit_code'] == 0 for r in results)
            profile.finish_step(step, success=all_ok)
            log(f'  Install {"OK" if all_ok else "FAILED"}')
        except Exception as e:
            profile.finish_step(step, success=False, detail=str(e))
            log(f'  Install error: {e}')

        # ── Step 4: Run tests ──
        step = profile.add_step('run_tests')
        log('Running Django tests...')
        try:
            test_result = runner.run_tests()
            test_ok = test_result['exit_code'] == 0
            output = test_result['result']

            # Parse test count
            import re
            match = re.search(r'Ran (\d+) test', output)
            if match:
                profile.tests_total = int(match.group(1))
                if test_ok:
                    profile.tests_passed = profile.tests_total

            profile.finish_step(step, success=test_ok, detail=output[-200:])
            log(f'  Tests: {profile.tests_passed}/{profile.tests_total} passed')
        except Exception as e:
            profile.finish_step(step, success=False, detail=str(e))
            log(f'  Test error: {e}')

        # ── Step 5: RL Training ──
        step = profile.add_step('rl_training')
        log(f'RL training ({episodes} ep, {max_steps} steps)...')
        try:
            rl_result = runner.run_rl_training(
                episodes=episodes, max_steps=max_steps
            )
            rl_ok = rl_result['exit_code'] == 0
            output = rl_result['result']
            profile.finish_step(step, success=rl_ok, detail=output[-300:])

            if rl_ok:
                log('  Training complete, fetching results...')
        except Exception as e:
            profile.finish_step(step, success=False, detail=str(e))
            log(f'  RL error: {e}')

        # ── Step 6: Retrieve results ──
        step = profile.add_step('retrieve_results')
        log('Retrieving results...')
        try:
            results_path = get_results_path(provider)
            results_json = runner.download_file(results_path)
            results = json.loads(results_json)

            profile.rl_best_reward = results.get('best_reward', 0)
            profile.rl_final_avg = results.get('final_avg_reward', 0)
            profile.rl_best_actions = results.get('best_actions', [])

            profile.finish_step(step, success=True)
            log(f'  Best reward: {profile.rl_best_reward}')
        except Exception as e:
            profile.finish_step(step, success=False, detail=str(e))
            log(f'  Results error: {e}')

    finally:
        # ── Step 7: Cleanup ──
        step = profile.add_step('destroy_sandbox')
        log('Destroying sandbox...')
        try:
            runner.destroy()
            profile.finish_step(step, success=True)
        except Exception as e:
            profile.finish_step(step, success=False, detail=str(e))

    profile.total_duration_s = time.time() - pipeline_start
    log(f'DONE in {profile.total_duration_s:.1f}s')
    return profile


# ── Parallel Orchestrator ───────────────────────────────────────────

def run_parallel(
    configs,
    project_dir='.',
    max_workers=3,
    stagger_s=2.0,
):
    """Run multiple sandbox pipelines in parallel.

    configs: list of dicts with keys 'label', 'episodes', 'max_steps', 'provider'
    """
    providers_used = set(c.get('provider', 'daytona') for c in configs)
    print('=' * 74)
    print(f'  PARALLEL SANDBOX PROFILING - {len(configs)} sandboxes, '
          f'{max_workers} concurrent')
    print(f'  Providers: {", ".join(providers_used)}')
    print(f'  Stagger: {stagger_s}s between launches')
    print('=' * 74)
    print(f'  Started: {datetime.now().isoformat()}')
    print()

    all_start = time.time()
    profiles = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for i, cfg in enumerate(configs):
            label = cfg.get('label', f'sandbox-{i+1}')
            provider = cfg.get('provider', 'daytona')
            benchmark_type = cfg.get('benchmark', 'rl')

            if benchmark_type == 'fs':
                future = pool.submit(
                    run_profiled_fs_benchmark,
                    label=label,
                    provider=provider,
                    log_prefix=f'[{i+1}/{len(configs)}] ',
                    stagger_delay=i * stagger_s,
                )
            elif benchmark_type == 'pause':
                future = pool.submit(
                    run_profiled_pause_benchmark,
                    label=label,
                    provider=provider,
                    log_prefix=f'[{i+1}/{len(configs)}] ',
                    stagger_delay=i * stagger_s,
                )
            elif benchmark_type == 'concurrent':
                future = pool.submit(
                    run_profiled_concurrent_benchmark,
                    label=label,
                    provider=provider,
                    log_prefix=f'[{i+1}/{len(configs)}] ',
                    stagger_delay=i * stagger_s,
                )
            elif benchmark_type == 'iteration':
                future = pool.submit(
                    run_profiled_iteration_benchmark,
                    label=label,
                    provider=provider,
                    log_prefix=f'[{i+1}/{len(configs)}] ',
                    stagger_delay=i * stagger_s,
                )
            elif benchmark_type == 'fanout':
                future = pool.submit(
                    run_profiled_fanout_benchmark,
                    label=label,
                    provider=provider,
                    log_prefix=f'[{i+1}/{len(configs)}] ',
                    stagger_delay=i * stagger_s,
                )
            elif benchmark_type == 'docker':
                future = pool.submit(
                    run_profiled_docker_benchmark,
                    label=label,
                    provider=provider,
                    log_prefix=f'[{i+1}/{len(configs)}] ',
                    stagger_delay=i * stagger_s,
                )
            elif benchmark_type == 'network':
                future = pool.submit(
                    run_profiled_network_benchmark,
                    label=label,
                    provider=provider,
                    log_prefix=f'[{i+1}/{len(configs)}] ',
                    stagger_delay=i * stagger_s,
                )
            elif benchmark_type == 'security':
                future = pool.submit(
                    run_profiled_security_benchmark,
                    label=label,
                    provider=provider,
                    log_prefix=f'[{i+1}/{len(configs)}] ',
                    stagger_delay=i * stagger_s,
                )
            elif benchmark_type == 'security-exploits':
                future = pool.submit(
                    run_profiled_security_exploits_benchmark,
                    label=label,
                    provider=provider,
                    log_prefix=f'[{i+1}/{len(configs)}] ',
                    stagger_delay=i * stagger_s,
                )
            elif benchmark_type == 'agent':
                future = pool.submit(
                    run_profiled_agent_benchmark,
                    label=label,
                    provider=provider,
                    log_prefix=f'[{i+1}/{len(configs)}] ',
                    stagger_delay=i * stagger_s,
                    project_dir=project_dir,
                    llm_backend=cfg.get('llm_backend', 'gemini'),
                    llm_model=cfg.get('llm_model'),
                    llm_api_key=cfg.get('llm_api_key'),
                    agent_iterations=cfg.get('agent_iterations', 3),
                    reward_threshold=cfg.get('reward_threshold', 25.0),
                    bootstrap_app=cfg.get('bootstrap_app', False),
                )
            else:
                future = pool.submit(
                    run_profiled_sandbox,
                    label=label,
                    episodes=cfg.get('episodes', 30),
                    max_steps=cfg.get('max_steps', 15),
                    project_dir=project_dir,
                    provider=provider,
                    log_prefix=f'[{i+1}/{len(configs)}] ',
                    stagger_delay=i * stagger_s,
                )
            futures[future] = {
                'label': label,
                'provider': provider,
            }

        for future in as_completed(futures):
            meta = futures[future]
            label = meta['label']
            provider = meta['provider']
            try:
                profile = future.result()
                profiles.append(profile)
            except Exception as e:
                print(f'[ERROR] {label} crashed: {e}')
                profiles.append(SandboxProfile(
                    sandbox_label=label,
                    provider=provider,
                    error=str(e),
                ))

    total_wall = time.time() - all_start

    # ── Generate Report ─────────────────────────────────────────────
    _print_report(profiles, total_wall, providers_used)

    # Save full report
    report = _build_report(profiles, total_wall)

    os.makedirs('rl_output', exist_ok=True)
    report_path = 'rl_output/parallel_profile_report.json'
    with open(report_path, 'w') as f:
        json.dump(report, f, indent=2, default=str)

    print(f'  Full report saved to: {report_path}')
    print('=' * 74)

    return report


def _print_report(profiles, total_wall, providers_used):
    print()
    print('=' * 74)
    print('  PROFILING REPORT')
    print('=' * 74)
    print()

    # Sort by label
    profiles.sort(key=lambda p: p.sandbox_label)

    # Per-sandbox breakdown
    for p in profiles:
        sid = p.sandbox_id[:16] + '...' if p.sandbox_id else 'N/A'
        print(f'  {p.sandbox_label} [{p.provider}] (sandbox: {sid})')
        print(f'  Config: episodes={p.config.get("episodes")}, '
              f'max_steps={p.config.get("max_steps")}')
        print(p.step_summary)
        print(f'  {"─"*50}')
        print(f'  Total:    {p.total_duration_s:>7.1f}s')
        print(f'  Tests:    {p.tests_passed}/{p.tests_total}')
        print(f'  RL Best:  {p.rl_best_reward}')
        print(f'  RL Avg:   {p.rl_final_avg}')
        if p.error:
            print(f'  ERROR:    {p.error}')
        print()

    # Timing comparison table — dynamic step names
    known_order = [
        'create_sandbox', 'upload_project', 'install_deps',
        'run_tests', 'rl_training', 'retrieve_results',
        'fs_code_generation', 'fs_build_compile', 'fs_upload_files',
        'fs_download_files', 'fs_pip_package_io', 'fs_list_verify',
        'async_start_task', 'async_write_state', 'async_pause',
        'async_resume', 'async_verify_state', 'async_restart_task',
        'async_download_verify',
        'ce_setup_workspace', 'ce_sequential_exec',
        'ce_concurrent_exec', 'ce_comparison',
        'iter_upload_broken', 'iter_test_fail', 'iter_upload_fix',
        'iter_test_pass', 'iter_add_feature', 'iter_final_validation',
        'fo_create_sandboxes', 'fo_upload_code', 'fo_run_tasks',
        'fo_collect_results', 'fo_destroy_sandboxes', 'fo_create_custom',
        'agent_setup', 'agent_upload_project',
        'agent_iteration_1', 'agent_iteration_2', 'agent_iteration_3',
        'agent_iteration_4', 'agent_iteration_5',
        'agent_final_result',
        'docker_build_image', 'docker_create_sandbox',
        'docker_verify_deps', 'docker_run_workload',
        'docker_stock_boot', 'docker_comparison',
        'net_latency', 'net_download', 'net_download_large',
        'net_upload', 'net_dns', 'net_pip_install',
        'sec_metadata_service', 'sec_privilege_info', 'sec_container_escape',
        'sec_network_scan', 'sec_filesystem_exposure', 'sec_resource_limits',
        'sec_egress_filtering', 'sec_env_leak',
        'exploit_devmem_read', 'exploit_host_fs_traversal',
        'exploit_raw_socket_ping', 'exploit_capability_abuse',
        'exploit_resource_exhaustion', 'exploit_vm_guest_escape',
        'destroy_sandbox',
    ]
    all_step_names = set()
    for p in profiles:
        for s in p.steps:
            all_step_names.add(s.name)
    step_names = [s for s in known_order if s in all_step_names]
    step_names += sorted(all_step_names - set(known_order))

    header = f'  {"Step":<25}'
    for p in profiles:
        header += f'  {p.sandbox_label:>14}'
    print(header)
    print('  ' + '─' * (25 + 16 * len(profiles)))

    for sn in step_names:
        row = f'  {sn:<25}'
        for p in profiles:
            found = [s for s in p.steps if s.name == sn]
            if found:
                s = found[0]
                marker = '' if s.success else '!'
                row += f'  {s.duration_s:>12.1f}s{marker}'
            else:
                row += f'  {"—":>14}'
        print(row)

    total_row = f'  {"TOTAL":<25}'
    for p in profiles:
        total_row += f'  {p.total_duration_s:>12.1f}s'
    print('  ' + '─' * (25 + 16 * len(profiles)))
    print(total_row)
    print()
    print(f'  Wall-clock time (parallel): {total_wall:.1f}s')
    sum_serial = sum(p.total_duration_s for p in profiles)
    print(f'  Sum of all sandbox times:   {sum_serial:.1f}s')
    if sum_serial > 0:
        speedup = sum_serial / total_wall
        print(f'  Parallel speedup:           {speedup:.2f}x')
    print()

    # ── Provider comparison (if multiple providers) ──────────────
    if len(providers_used) > 1:
        print('  PROVIDER COMPARISON:')
        print('  ' + '─' * 60)
        for provider in sorted(providers_used):
            pp = [p for p in profiles if p.provider == provider and not p.error]
            if not pp:
                print(f'    {provider}: no successful runs')
                continue
            avg_total = sum(p.total_duration_s for p in pp) / len(pp)
            avg_tests = sum(p.tests_passed for p in pp) / len(pp)

            step_avgs = {}
            for sn in step_names:
                durs = []
                for p in pp:
                    found = [s for s in p.steps if s.name == sn and s.success]
                    if found:
                        durs.append(found[0].duration_s)
                if durs:
                    step_avgs[sn] = sum(durs) / len(durs)

            print(f'    {provider} ({len(pp)} runs):')
            print(f'      Avg total:     {avg_total:>7.1f}s')
            print(f'      Avg tests:     {avg_tests:>7.1f}')
            for sn, avg in step_avgs.items():
                print(f'      {sn:<23} {avg:>7.1f}s')
            print()

        # Head-to-head fastest
        print('  HEAD-TO-HEAD (fastest per step):')
        for sn in step_names:
            best_provider = None
            best_time = float('inf')
            for provider in providers_used:
                pp = [p for p in profiles if p.provider == provider]
                for p in pp:
                    found = [s for s in p.steps if s.name == sn and s.success]
                    if found and found[0].duration_s < best_time:
                        best_time = found[0].duration_s
                        best_provider = provider
            if best_provider:
                print(f'    {sn:<25} {best_provider:>10} ({best_time:.1f}s)')
        print()

    # Average per step
    print(f'  {"AVERAGE PER STEP":<25}')
    print('  ' + '─' * 40)
    for sn in step_names:
        durations = []
        for p in profiles:
            found = [s for s in p.steps if s.name == sn]
            if found and found[0].success:
                durations.append(found[0].duration_s)
        if durations:
            avg = sum(durations) / len(durations)
            mn = min(durations)
            mx = max(durations)
            print(f'  {sn:<25} avg={avg:>6.1f}s  '
                  f'min={mn:>6.1f}s  max={mx:>6.1f}s')
    print()

    # Hotspot analysis
    print('  BOTTLENECK ANALYSIS:')
    all_steps = []
    for p in profiles:
        for s in p.steps:
            if s.success:
                all_steps.append((s.name, s.duration_s,
                                  p.sandbox_label, p.provider))
    all_steps.sort(key=lambda x: -x[1])
    for name, dur, label, prov in all_steps[:5]:
        print(f'    {dur:>7.1f}s  {name:<25} ({label} [{prov}])')
    print()


def _build_report(profiles, total_wall):
    sum_serial = sum(p.total_duration_s for p in profiles)
    return {
        'timestamp': datetime.now().isoformat(),
        'wall_clock_s': round(total_wall, 2),
        'serial_sum_s': round(sum_serial, 2),
        'speedup': round(sum_serial / max(total_wall, 0.1), 2),
        'sandbox_count': len(profiles),
        'providers': list(set(p.provider for p in profiles)),
        'profiles': [p.to_dict() for p in profiles],
    }


# ── CLI Entry Point ─────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Run parallel profiled sandboxes (Daytona, E2B, Blaxel, or Modal)'
    )
    parser.add_argument(
        '--sandboxes', type=int, default=3,
        help='Number of parallel sandboxes',
    )
    parser.add_argument(
        '--max-workers', type=int, default=3,
        help='Max concurrent sandbox threads',
    )
    parser.add_argument('--episodes', type=int, default=30)
    parser.add_argument('--max-steps', type=int, default=15)
    parser.add_argument('--project-dir', default='.')
    parser.add_argument(
        '--provider',
        choices=['daytona', 'e2b', 'blaxel', 'modal', 'both', 'all'],
        default='daytona',
        help='Sandbox provider ("both" for Daytona+E2B, "all" for all four)',
    )
    parser.add_argument(
        '--stagger', type=float, default=2.0,
        help='Seconds between sandbox launches (reduces thundering herd)',
    )
    parser.add_argument(
        '--vary-config', action='store_true',
        help='Use different RL configs per sandbox for comparison',
    )
    parser.add_argument(
        '--benchmark',
        choices=['rl', 'fs', 'pause', 'concurrent', 'iteration', 'fanout', 'agent', 'docker', 'network', 'security', 'security-exploits', 'all'],
        default='rl',
        help='Benchmark type: "rl" (default), "fs" (filesystem), '
             '"pause" (async task pause/resume), "concurrent" (concurrent exec), '
             '"iteration" (edit-test loop), "fanout" (multi-sandbox fan-out), '
             '"agent" (LLM coding agent loop), '
             '"docker" (custom Docker image), '
             '"network" (network speed), '
             '"security" (isolation & security), '
             '"security-exploits" (exploit validation PoCs), '
             '"all" (all benchmarks)',
    )
    parser.add_argument(
        '--llm',
        choices=['gemini', 'vertex-claude'],
        default='gemini',
        help='LLM backend for agent benchmark (default: gemini)',
    )
    parser.add_argument(
        '--llm-model',
        default=None,
        help='Model name override for agent benchmark',
    )
    parser.add_argument(
        '--llm-api-key',
        default=None,
        help='API key for the LLM (or set GEMINI_API_KEY in .env)',
    )
    parser.add_argument(
        '--agent-iterations', type=int, default=3,
        help='Max generate-test-fix cycles for agent benchmark (default: 3)',
    )
    parser.add_argument(
        '--reward-threshold', type=float, default=25.0,
        help='Stop agent when total reward reaches this value (default: 25.0)',
    )
    parser.add_argument(
        '--bootstrap-app', action='store_true',
        help='Agent generates the Django app from scratch (no local project upload)',
    )

    args = parser.parse_args()

    if args.provider in ('both', 'all'):
        if args.provider == 'all':
            providers = ['daytona', 'e2b', 'blaxel', 'modal']
        else:
            providers = ['daytona', 'e2b']
        n_each = max(1, args.sandboxes // len(providers))
        configs = []
        if args.vary_config:
            rl_configs = [
                {'episodes': 10, 'max_steps': 10},
                {'episodes': 30, 'max_steps': 15},
                {'episodes': 50, 'max_steps': 20},
            ]
        else:
            rl_configs = [
                {'episodes': args.episodes, 'max_steps': args.max_steps},
            ] * n_each

        for provider in providers:
            for i in range(n_each):
                cfg = rl_configs[i % len(rl_configs)]
                configs.append({
                    'label': f'{provider}-{i+1}',
                    'provider': provider,
                    **cfg,
                })

    elif args.vary_config:
        configs = [
            {
                'label': f'fast-10ep',
                'episodes': 10,
                'max_steps': 10,
                'provider': args.provider,
            },
            {
                'label': f'medium-30ep',
                'episodes': 30,
                'max_steps': 15,
                'provider': args.provider,
            },
            {
                'label': f'long-50ep',
                'episodes': 50,
                'max_steps': 20,
                'provider': args.provider,
            },
        ][:args.sandboxes]
    else:
        configs = [
            {
                'label': f'{args.provider}-{i+1}',
                'episodes': args.episodes,
                'max_steps': args.max_steps,
                'provider': args.provider,
            }
            for i in range(args.sandboxes)
        ]

    # Add filesystem benchmark configs if requested
    if args.benchmark in ('fs', 'all'):
        fs_configs = []
        if args.provider in ('both', 'all'):
            if args.provider == 'all':
                fs_providers = ['daytona', 'e2b', 'blaxel', 'modal']
            else:
                fs_providers = ['daytona', 'e2b']
            for prov in fs_providers:
                fs_configs.append({
                    'label': f'{prov}-fs',
                    'provider': prov,
                    'benchmark': 'fs',
                })
        else:
            for i in range(args.sandboxes):
                fs_configs.append({
                    'label': f'{args.provider}-fs-{i+1}',
                    'provider': args.provider,
                    'benchmark': 'fs',
                })

        if args.benchmark == 'fs':
            configs = fs_configs
        else:  # 'all'
            configs = configs + fs_configs

    # Add pause/resume benchmark configs if requested
    if args.benchmark in ('pause', 'all'):
        pause_configs = []
        if args.provider in ('both', 'all'):
            if args.provider == 'all':
                pause_providers = ['daytona', 'e2b', 'blaxel', 'modal']
            else:
                pause_providers = ['daytona', 'e2b']
            for prov in pause_providers:
                pause_configs.append({
                    'label': f'{prov}-pause',
                    'provider': prov,
                    'benchmark': 'pause',
                })
        else:
            for i in range(args.sandboxes):
                pause_configs.append({
                    'label': f'{args.provider}-pause-{i+1}',
                    'provider': args.provider,
                    'benchmark': 'pause',
                })

        if args.benchmark == 'pause':
            configs = pause_configs
        else:  # 'all'
            configs = configs + pause_configs

    # Add concurrent exec benchmark configs if requested
    if args.benchmark in ('concurrent', 'all'):
        concurrent_configs = []
        if args.provider in ('both', 'all'):
            if args.provider == 'all':
                cc_providers = ['daytona', 'e2b', 'blaxel', 'modal']
            else:
                cc_providers = ['daytona', 'e2b']
            for prov in cc_providers:
                concurrent_configs.append({
                    'label': f'{prov}-concurrent',
                    'provider': prov,
                    'benchmark': 'concurrent',
                })
        else:
            for i in range(args.sandboxes):
                concurrent_configs.append({
                    'label': f'{args.provider}-concurrent-{i+1}',
                    'provider': args.provider,
                    'benchmark': 'concurrent',
                })

        if args.benchmark == 'concurrent':
            configs = concurrent_configs
        else:  # 'all'
            configs = configs + concurrent_configs

    # Add iteration loop benchmark configs if requested
    if args.benchmark in ('iteration', 'all'):
        iteration_configs = []
        if args.provider in ('both', 'all'):
            if args.provider == 'all':
                iter_providers = ['daytona', 'e2b', 'blaxel', 'modal']
            else:
                iter_providers = ['daytona', 'e2b']
            for prov in iter_providers:
                iteration_configs.append({
                    'label': f'{prov}-iteration',
                    'provider': prov,
                    'benchmark': 'iteration',
                })
        else:
            for i in range(args.sandboxes):
                iteration_configs.append({
                    'label': f'{args.provider}-iteration-{i+1}',
                    'provider': args.provider,
                    'benchmark': 'iteration',
                })

        if args.benchmark == 'iteration':
            configs = iteration_configs
        else:  # 'all'
            configs = configs + iteration_configs

    # Add fan-out benchmark configs if requested
    if args.benchmark in ('fanout', 'all'):
        fanout_configs = []
        if args.provider in ('both', 'all'):
            if args.provider == 'all':
                fo_providers = ['daytona', 'e2b', 'blaxel', 'modal']
            else:
                fo_providers = ['daytona', 'e2b']
            for prov in fo_providers:
                fanout_configs.append({
                    'label': f'{prov}-fanout',
                    'provider': prov,
                    'benchmark': 'fanout',
                })
        else:
            for i in range(args.sandboxes):
                fanout_configs.append({
                    'label': f'{args.provider}-fanout-{i+1}',
                    'provider': args.provider,
                    'benchmark': 'fanout',
                })

        if args.benchmark == 'fanout':
            configs = fanout_configs
        else:  # 'all'
            configs = configs + fanout_configs

    # Add coding agent benchmark configs if requested
    if args.benchmark in ('agent', 'all'):
        agent_configs = []
        if args.provider in ('both', 'all'):
            if args.provider == 'all':
                agent_providers = ['daytona', 'e2b', 'blaxel', 'modal']
            else:
                agent_providers = ['daytona', 'e2b']
            for prov in agent_providers:
                agent_configs.append({
                    'label': f'{prov}-agent',
                    'provider': prov,
                    'benchmark': 'agent',
                    'llm_backend': args.llm,
                    'llm_model': args.llm_model,
                    'llm_api_key': args.llm_api_key,
                    'agent_iterations': args.agent_iterations,
                    'reward_threshold': args.reward_threshold,
                    'bootstrap_app': args.bootstrap_app,
                })
        else:
            for i in range(args.sandboxes):
                agent_configs.append({
                    'label': f'{args.provider}-agent-{i+1}',
                    'provider': args.provider,
                    'benchmark': 'agent',
                    'llm_backend': args.llm,
                    'llm_model': args.llm_model,
                    'llm_api_key': args.llm_api_key,
                    'agent_iterations': args.agent_iterations,
                    'reward_threshold': args.reward_threshold,
                    'bootstrap_app': args.bootstrap_app,
                })

        if args.benchmark == 'agent':
            configs = agent_configs
        else:  # 'all'
            configs = configs + agent_configs

    # Add custom Docker image benchmark configs if requested
    if args.benchmark in ('docker', 'all'):
        docker_configs = []
        if args.provider in ('both', 'all'):
            if args.provider == 'all':
                docker_providers = ['daytona', 'e2b', 'blaxel', 'modal']
            else:
                docker_providers = ['daytona', 'e2b']
            for prov in docker_providers:
                docker_configs.append({
                    'label': f'{prov}-docker',
                    'provider': prov,
                    'benchmark': 'docker',
                })
        else:
            for i in range(args.sandboxes):
                docker_configs.append({
                    'label': f'{args.provider}-docker-{i+1}',
                    'provider': args.provider,
                    'benchmark': 'docker',
                })

        if args.benchmark == 'docker':
            configs = docker_configs
        else:  # 'all'
            configs = configs + docker_configs

    # Add security benchmark configs if requested
    if args.benchmark in ('security', 'all'):
        security_configs = []
        if args.provider in ('both', 'all'):
            if args.provider == 'all':
                sec_providers = ['daytona', 'e2b', 'blaxel', 'modal']
            else:
                sec_providers = ['daytona', 'e2b']
            for prov in sec_providers:
                security_configs.append({
                    'label': f'{prov}-security',
                    'provider': prov,
                    'benchmark': 'security',
                })
        else:
            for i in range(args.sandboxes):
                security_configs.append({
                    'label': f'{args.provider}-security-{i+1}',
                    'provider': args.provider,
                    'benchmark': 'security',
                })

        if args.benchmark == 'security':
            configs = security_configs
        else:  # 'all'
            configs = configs + security_configs

    # Add security exploits benchmark configs if requested
    if args.benchmark in ('security-exploits', 'all'):
        sec_exploit_configs = []
        if args.provider in ('both', 'all'):
            if args.provider == 'all':
                se_providers = ['daytona', 'e2b', 'blaxel', 'modal']
            else:
                se_providers = ['daytona', 'e2b']
            for prov in se_providers:
                sec_exploit_configs.append({
                    'label': f'{prov}-sec-exploits',
                    'provider': prov,
                    'benchmark': 'security-exploits',
                })
        else:
            for i in range(args.sandboxes):
                sec_exploit_configs.append({
                    'label': f'{args.provider}-sec-exploits-{i+1}',
                    'provider': args.provider,
                    'benchmark': 'security-exploits',
                })

        if args.benchmark == 'security-exploits':
            configs = sec_exploit_configs
        else:  # 'all'
            configs = configs + sec_exploit_configs

    # Add network speed benchmark configs if requested
    if args.benchmark in ('network', 'all'):
        network_configs = []
        if args.provider in ('both', 'all'):
            if args.provider == 'all':
                net_providers = ['daytona', 'e2b', 'blaxel', 'modal']
            else:
                net_providers = ['daytona', 'e2b']
            for prov in net_providers:
                network_configs.append({
                    'label': f'{prov}-network',
                    'provider': prov,
                    'benchmark': 'network',
                })
        else:
            for i in range(args.sandboxes):
                network_configs.append({
                    'label': f'{args.provider}-network-{i+1}',
                    'provider': args.provider,
                    'benchmark': 'network',
                })

        if args.benchmark == 'network':
            configs = network_configs
        else:  # 'all'
            configs = configs + network_configs

    run_parallel(
        configs=configs,
        project_dir=args.project_dir,
        max_workers=args.max_workers,
        stagger_s=args.stagger,
    )
