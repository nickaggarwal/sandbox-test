"""
Concurrent Exec Benchmark for Sandbox Profiling.

Tests whether a sandbox can handle multiple parallel exec() calls,
which is critical for agents that fire lint/test/typecheck/format
simultaneously within a single sandbox.

Steps:
1. Setup workspace with a Python project + install tooling
2. Run 4 commands sequentially (baseline)
3. Run same 4 commands concurrently via threads
4. Compare sequential vs concurrent timing
"""
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from run_parallel_profiled import StepProfile


# ── Test Project Content ───────────────────────────────────────────

APP_SOURCE = b'''"""Sample application module."""
import os
import json
from typing import Dict, List, Optional


class DataProcessor:
    """Processes data records."""

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self.records: List[Dict] = []

    def add_record(self, record: Dict) -> None:
        """Add a record to the processor."""
        self.records.append(record)

    def process_all(self) -> List[Dict]:
        """Process all records and return results."""
        results = []
        for r in self.records:
            processed = {k: str(v).upper() for k, v in r.items()}
            results.append(processed)
        return results

    def summary(self) -> Dict:
        """Return a summary of processed records."""
        return {"count": len(self.records), "processed": len(self.process_all())}


class Validator:
    """Validates input data."""

    def validate(self, data: Dict) -> bool:
        """Validate a single data record."""
        return isinstance(data, dict) and len(data) > 0

    def validate_batch(self, items: List[Dict]) -> List[bool]:
        """Validate a batch of data records."""
        return [self.validate(item) for item in items]
'''

UTILS_SOURCE = b'''"""Utility functions."""
import json
import os
from typing import Any


def load_config(path: str) -> dict:
    """Load configuration from a JSON file."""
    with open(path, "r") as f:
        return json.load(f)


def format_output(data: dict) -> str:
    """Format data as a JSON string."""
    return json.dumps(data, indent=2)


def ensure_dir(path: str) -> None:
    """Ensure a directory exists."""
    os.makedirs(path, exist_ok=True)


def flatten_dict(d: dict, prefix: str = "") -> dict:
    """Flatten a nested dictionary."""
    items: list = []
    for k, v in d.items():
        new_key = "{}_{}".format(prefix, k) if prefix else k
        if isinstance(v, dict):
            items.extend(flatten_dict(v, new_key).items())
        else:
            items.append((new_key, v))
    return dict(items)
'''

TEST_SOURCE = b'''"""Tests for app module."""
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from app import DataProcessor, Validator


def test_add_record():
    dp = DataProcessor()
    dp.add_record({"name": "test"})
    assert len(dp.records) == 1


def test_process_all():
    dp = DataProcessor()
    dp.add_record({"key": "value"})
    results = dp.process_all()
    assert results[0]["key"] == "VALUE"


def test_summary():
    dp = DataProcessor()
    dp.add_record({"a": 1})
    s = dp.summary()
    assert s["count"] == 1


def test_validator():
    v = Validator()
    assert v.validate({"key": "val"}) is True
    assert v.validate({}) is False


def test_validate_batch():
    v = Validator()
    results = v.validate_batch([{"a": 1}, {}, {"b": 2}])
    assert results == [True, False, True]


def test_processor_empty():
    dp = DataProcessor()
    assert dp.process_all() == []
    assert dp.summary() == {"count": 0, "processed": 0}
'''

SETUP_CFG = b'''[flake8]
max-line-length = 120
exclude = __pycache__

[mypy]
ignore_missing_imports = True
'''


# ── Benchmark Steps ────────────────────────────────────────────────

def _step_setup_workspace(runner, base_dir):
    """Upload test project and install tooling."""
    step = StepProfile(name='ce_setup_workspace', started_at=time.time())

    runner.exec('mkdir -p {}/src'.format(base_dir), cwd='/tmp')

    files = {
        '{}/src/app.py'.format(base_dir): APP_SOURCE,
        '{}/src/utils.py'.format(base_dir): UTILS_SOURCE,
        '{}/src/__init__.py'.format(base_dir): b'',
        '{}/test_app.py'.format(base_dir): TEST_SOURCE,
        '{}/setup.cfg'.format(base_dir): SETUP_CFG,
    }

    uploaded = 0
    for path, content in files.items():
        try:
            runner.upload_file_native(content, path)
            uploaded += 1
        except Exception as e:
            print('      Upload failed {}: {}'.format(path, e))

    # Install tooling
    install_result = runner.exec(
        'pip install pytest flake8 mypy black 2>&1 | tail -3',
        cwd=base_dir,
        timeout=120,
    )

    step.ended_at = time.time()
    step.duration_s = step.ended_at - step.started_at
    step.success = uploaded == len(files) and install_result['exit_code'] == 0
    step.detail = '{}/{} files, pip {}'.format(
        uploaded, len(files),
        'OK' if install_result['exit_code'] == 0 else 'FAIL')
    return step


def _run_single_command(runner, base_dir, cmd):
    """Run a single command and return (duration, exit_code, name)."""
    t0 = time.time()
    result = runner.exec(cmd, cwd=base_dir, timeout=60)
    dur = time.time() - t0
    return dur, result['exit_code']


def _step_sequential_exec(runner, base_dir):
    """Run 4 commands sequentially as a baseline."""
    step = StepProfile(name='ce_sequential_exec', started_at=time.time())

    commands = [
        ('flake8', 'python3 -m flake8 src/ --max-line-length=120 || true'),
        ('pytest', 'python3 -m pytest test_app.py -v 2>&1 || true'),
        ('mypy', 'python3 -m mypy src/ --ignore-missing-imports || true'),
        ('black', 'python3 -m black --check src/ 2>&1 || true'),
    ]

    timings = {}
    all_ok = True
    for name, cmd in commands:
        dur, exit_code = _run_single_command(runner, base_dir, cmd)
        timings[name] = dur
        if exit_code == -1:
            all_ok = False

    total = sum(timings.values())

    step.ended_at = time.time()
    step.duration_s = step.ended_at - step.started_at
    step.success = all_ok
    step.detail = '4 sequential: {:.2f}s ({})'.format(
        total,
        ', '.join('{}={:.2f}s'.format(k, v) for k, v in timings.items()),
    )
    return step, total


def _step_concurrent_exec(runner, base_dir):
    """Run 4 commands concurrently via threads."""
    step = StepProfile(name='ce_concurrent_exec', started_at=time.time())

    commands = [
        ('flake8', 'python3 -m flake8 src/ --max-line-length=120 || true'),
        ('pytest', 'python3 -m pytest test_app.py -v 2>&1 || true'),
        ('mypy', 'python3 -m mypy src/ --ignore-missing-imports || true'),
        ('black', 'python3 -m black --check src/ 2>&1 || true'),
    ]

    timings = {}
    all_ok = True

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {}
        for name, cmd in commands:
            future = pool.submit(_run_single_command, runner, base_dir, cmd)
            futures[future] = name

        for future in as_completed(futures):
            name = futures[future]
            try:
                dur, exit_code = future.result()
                timings[name] = dur
                if exit_code == -1:
                    all_ok = False
            except Exception as e:
                timings[name] = 0
                all_ok = False

    total = step.ended_at = time.time()
    step.duration_s = step.ended_at - step.started_at
    total_wall = step.duration_s

    step.success = all_ok
    step.detail = '4 concurrent: {:.2f}s ({})'.format(
        total_wall,
        ', '.join('{}={:.2f}s'.format(k, v) for k, v in timings.items()),
    )
    return step, total_wall


def _step_comparison(sequential_time, concurrent_time):
    """Compare sequential vs concurrent timing."""
    step = StepProfile(name='ce_comparison', started_at=time.time())

    if concurrent_time > 0:
        speedup = sequential_time / concurrent_time
    else:
        speedup = 1.0

    parallel_support = 'yes' if speedup > 1.3 else 'limited'

    step.ended_at = time.time()
    step.duration_s = step.ended_at - step.started_at
    step.success = True
    step.detail = 'speedup={:.2f}x, seq={:.2f}s, conc={:.2f}s, parallel={}'.format(
        speedup, sequential_time, concurrent_time, parallel_support)
    return step


# ── Main Benchmark Function ───────────────────────────────────────

def run_concurrent_exec_benchmark(runner, provider):
    """Execute the concurrent exec benchmark suite.

    Args:
        runner: A sandbox runner instance (must already have a sandbox created).
        provider: 'daytona', 'e2b', 'blaxel', or 'modal'.

    Returns:
        list[StepProfile]: Profiling data for each benchmark step.
    """
    if provider == 'daytona':
        base_dir = '/root/concurrent_bench'
    elif provider == 'blaxel':
        base_dir = '/blaxel/concurrent_bench'
    elif provider == 'modal':
        base_dir = '/root/concurrent_bench'
    else:
        base_dir = '/home/user/concurrent_bench'

    runner.exec('mkdir -p {}'.format(base_dir), cwd='/tmp')

    steps = []

    print('    [CE] Step 1/4: Setup workspace...')
    setup_step = _step_setup_workspace(runner, base_dir)
    steps.append(setup_step)
    print('    [CE]   {:.1f}s - {}'.format(setup_step.duration_s, setup_step.detail))

    print('    [CE] Step 2/4: Sequential exec (baseline)...')
    seq_step, seq_time = _step_sequential_exec(runner, base_dir)
    steps.append(seq_step)
    print('    [CE]   {:.1f}s - {}'.format(seq_step.duration_s, seq_step.detail))

    print('    [CE] Step 3/4: Concurrent exec...')
    conc_step, conc_time = _step_concurrent_exec(runner, base_dir)
    steps.append(conc_step)
    print('    [CE]   {:.1f}s - {}'.format(conc_step.duration_s, conc_step.detail))

    print('    [CE] Step 4/4: Comparison...')
    comp_step = _step_comparison(seq_time, conc_time)
    steps.append(comp_step)
    print('    [CE]   {}'.format(comp_step.detail))

    return steps
