"""
Iterative Code-Edit-Test Loop Benchmark for Sandbox Profiling.

Simulates the core coding agent cycle:
  write code -> run tests -> read errors -> fix code -> run tests again

This measures the round-trip latency that directly impacts how fast
a coding agent can iterate on solutions.

Steps:
1. Upload deliberately broken code + test file
2. Run tests (expect failure)
3. Upload fixed code
4. Run tests (expect pass)
5. Add new feature + new tests
6. Final validation (all tests pass)
"""
import re
import time

from run_parallel_profiled import StepProfile


# ── Test Project Content ───────────────────────────────────────────

BROKEN_CALCULATOR = b'''"""Calculator module with bugs."""


class Calculator:
    def __init__(self):
        self.history = []

    def add(self, a, b):
        result = a - b  # BUG: subtracts instead of adds
        self.history.append(('add', a, b, result))
        return result

    def subtract(self, a, b):
        result = a - b
        self.history.append(('subtract', a, b, result))
        return result

    def multiply(self, a, b):
        result = a * b
        self.history.append(('multiply', a, b, result))
        return result

    def divide(self, a, b):
        result = a / b  # BUG: no zero check
        self.history.append(('divide', a, b, result))
        return result

    def last_result(self):
        if self.history:
            return self.history[-1][3]
        return None
'''

FIXED_CALCULATOR = b'''"""Calculator module - fixed."""


class Calculator:
    def __init__(self):
        self.history = []

    def add(self, a, b):
        result = a + b
        self.history.append(('add', a, b, result))
        return result

    def subtract(self, a, b):
        result = a - b
        self.history.append(('subtract', a, b, result))
        return result

    def multiply(self, a, b):
        result = a * b
        self.history.append(('multiply', a, b, result))
        return result

    def divide(self, a, b):
        if b == 0:
            raise ValueError("Cannot divide by zero")
        result = a / b
        self.history.append(('divide', a, b, result))
        return result

    def last_result(self):
        if self.history:
            return self.history[-1][3]
        return None
'''

V2_CALCULATOR = b'''"""Calculator module - v2 with power method."""


class Calculator:
    def __init__(self):
        self.history = []

    def add(self, a, b):
        result = a + b
        self.history.append(('add', a, b, result))
        return result

    def subtract(self, a, b):
        result = a - b
        self.history.append(('subtract', a, b, result))
        return result

    def multiply(self, a, b):
        result = a * b
        self.history.append(('multiply', a, b, result))
        return result

    def divide(self, a, b):
        if b == 0:
            raise ValueError("Cannot divide by zero")
        result = a / b
        self.history.append(('divide', a, b, result))
        return result

    def power(self, base, exp):
        result = base ** exp
        self.history.append(('power', base, exp, result))
        return result

    def last_result(self):
        if self.history:
            return self.history[-1][3]
        return None

    def history_count(self):
        return len(self.history)
'''

TEST_CALCULATOR = b'''"""Tests for Calculator."""
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from calculator import Calculator


def test_add():
    c = Calculator()
    assert c.add(2, 3) == 5


def test_subtract():
    c = Calculator()
    assert c.subtract(10, 4) == 6


def test_multiply():
    c = Calculator()
    assert c.multiply(3, 4) == 12


def test_divide():
    c = Calculator()
    assert c.divide(10, 2) == 5.0


def test_divide_by_zero():
    c = Calculator()
    with pytest.raises(ValueError, match="Cannot divide by zero"):
        c.divide(10, 0)


def test_last_result():
    c = Calculator()
    c.add(1, 2)
    assert c.last_result() == 3
'''

TEST_CALCULATOR_V2 = b'''"""Tests for Calculator v2."""
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from calculator import Calculator


def test_add():
    c = Calculator()
    assert c.add(2, 3) == 5


def test_subtract():
    c = Calculator()
    assert c.subtract(10, 4) == 6


def test_multiply():
    c = Calculator()
    assert c.multiply(3, 4) == 12


def test_divide():
    c = Calculator()
    assert c.divide(10, 2) == 5.0


def test_divide_by_zero():
    c = Calculator()
    with pytest.raises(ValueError, match="Cannot divide by zero"):
        c.divide(10, 0)


def test_last_result():
    c = Calculator()
    c.add(1, 2)
    assert c.last_result() == 3


def test_power():
    c = Calculator()
    assert c.power(2, 10) == 1024


def test_power_zero():
    c = Calculator()
    assert c.power(5, 0) == 1


def test_history_count():
    c = Calculator()
    assert c.history_count() == 0
    c.add(1, 2)
    c.multiply(3, 4)
    assert c.history_count() == 2
'''


# ── Benchmark Steps ────────────────────────────────────────────────

def _step_upload_broken_code(runner, base_dir):
    """Upload deliberately buggy code and test file."""
    step = StepProfile(name='iter_upload_broken', started_at=time.time())

    runner.exec('mkdir -p {}'.format(base_dir), cwd='/tmp')

    total_bytes = 0
    uploaded = 0

    for path, content in [
        ('{}/calculator.py'.format(base_dir), BROKEN_CALCULATOR),
        ('{}/test_calculator.py'.format(base_dir), TEST_CALCULATOR),
    ]:
        try:
            runner.upload_file_native(content, path)
            uploaded += 1
            total_bytes += len(content)
        except Exception as e:
            print('      Upload failed {}: {}'.format(path, e))

    # Install pytest
    runner.exec('pip install pytest 2>&1 | tail -2', cwd=base_dir, timeout=60)

    step.ended_at = time.time()
    step.duration_s = step.ended_at - step.started_at
    step.success = uploaded == 2
    step.detail = '{} files uploaded, {}B'.format(uploaded, total_bytes)
    return step


def _step_run_tests_expect_fail(runner, base_dir):
    """Run tests, expecting failures."""
    step = StepProfile(name='iter_test_fail', started_at=time.time())

    # Wrap command so non-zero exit doesn't cause SDK exceptions
    result = runner.exec(
        'python3 -m pytest test_calculator.py -v --tb=short; echo "PYTEST_EXIT=$?"',
        cwd=base_dir,
        timeout=60,
    )

    output = result['result']
    failures = 0
    errors = 0
    fail_match = re.search(r'(\d+) failed', output)
    if fail_match:
        failures = int(fail_match.group(1))
    err_match = re.search(r'(\d+) error', output)
    if err_match:
        errors = int(err_match.group(1))
    # Parse the real exit code from our echo wrapper
    exit_match = re.search(r'PYTEST_EXIT=(\d+)', output)
    real_exit = int(exit_match.group(1)) if exit_match else result['exit_code']

    step.ended_at = time.time()
    step.duration_s = step.ended_at - step.started_at
    # Success means: tests ran AND at least one failed/errored (confirming bugs exist)
    tests_failed = (failures + errors) > 0 or real_exit > 0
    step.success = tests_failed
    step.detail = '{} failures, {} errors, exit={}, exec={:.2f}s'.format(
        failures, errors, real_exit, step.duration_s)
    return step


def _step_upload_fix(runner, base_dir):
    """Upload the fixed version of calculator.py."""
    step = StepProfile(name='iter_upload_fix', started_at=time.time())

    try:
        runner.upload_file_native(
            FIXED_CALCULATOR,
            '{}/calculator.py'.format(base_dir),
        )
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = True
        step.detail = '{}B written, overwrite in {:.3f}s'.format(
            len(FIXED_CALCULATOR), step.duration_s)
    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = 'upload failed: {}'.format(str(e)[:100])

    return step


def _step_run_tests_expect_pass(runner, base_dir):
    """Run tests, expecting all to pass."""
    step = StepProfile(name='iter_test_pass', started_at=time.time())

    result = runner.exec(
        'python3 -m pytest test_calculator.py -v --tb=short',
        cwd=base_dir,
        timeout=60,
    )

    output = result['result']
    passed = 0
    pass_match = re.search(r'(\d+) passed', output)
    if pass_match:
        passed = int(pass_match.group(1))

    step.ended_at = time.time()
    step.duration_s = step.ended_at - step.started_at
    step.success = result['exit_code'] == 0
    step.detail = '{} tests passed, exec={:.2f}s'.format(
        passed, step.duration_s)
    return step


def _step_add_feature(runner, base_dir):
    """Upload v2 calculator with power() and updated tests."""
    step = StepProfile(name='iter_add_feature', started_at=time.time())

    total_bytes = 0
    uploaded = 0

    for path, content in [
        ('{}/calculator.py'.format(base_dir), V2_CALCULATOR),
        ('{}/test_calculator.py'.format(base_dir), TEST_CALCULATOR_V2),
    ]:
        try:
            runner.upload_file_native(content, path)
            uploaded += 1
            total_bytes += len(content)
        except Exception as e:
            print('      Upload failed {}: {}'.format(path, e))

    step.ended_at = time.time()
    step.duration_s = step.ended_at - step.started_at
    step.success = uploaded == 2
    step.detail = '{} files updated, {}B, iteration 2'.format(
        uploaded, total_bytes)
    return step


def _step_final_validation(runner, base_dir):
    """Run all tests one final time to validate everything works."""
    step = StepProfile(name='iter_final_validation', started_at=time.time())

    result = runner.exec(
        'python3 -m pytest test_calculator.py -v --tb=short',
        cwd=base_dir,
        timeout=60,
    )

    output = result['result']
    passed = 0
    total = 0
    pass_match = re.search(r'(\d+) passed', output)
    if pass_match:
        passed = int(pass_match.group(1))
        total = passed
    fail_match = re.search(r'(\d+) failed', output)
    if fail_match:
        total += int(fail_match.group(1))

    step.ended_at = time.time()
    step.duration_s = step.ended_at - step.started_at
    step.success = result['exit_code'] == 0 and passed >= 9
    step.detail = '{}/{} tests passed, final validation {}'.format(
        passed, total if total else passed,
        'OK' if step.success else 'FAIL')
    return step


# ── Main Benchmark Function ───────────────────────────────────────

def run_iteration_loop_benchmark(runner, provider):
    """Execute the iterative code-edit-test loop benchmark.

    Args:
        runner: A sandbox runner instance (must already have a sandbox created).
        provider: 'daytona', 'e2b', 'blaxel', or 'modal'.

    Returns:
        list[StepProfile]: Profiling data for each benchmark step.
    """
    if provider == 'daytona':
        base_dir = '/root/iter_bench'
    elif provider == 'blaxel':
        base_dir = '/blaxel/iter_bench'
    elif provider == 'modal':
        base_dir = '/root/iter_bench'
    else:
        base_dir = '/home/user/iter_bench'

    runner.exec('mkdir -p {}'.format(base_dir), cwd='/tmp')

    steps = []

    print('    [ITER] Step 1/6: Upload broken code...')
    steps.append(_step_upload_broken_code(runner, base_dir))
    print('    [ITER]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [ITER] Step 2/6: Run tests (expect fail)...')
    steps.append(_step_run_tests_expect_fail(runner, base_dir))
    print('    [ITER]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [ITER] Step 3/6: Upload fix...')
    steps.append(_step_upload_fix(runner, base_dir))
    print('    [ITER]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [ITER] Step 4/6: Run tests (expect pass)...')
    steps.append(_step_run_tests_expect_pass(runner, base_dir))
    print('    [ITER]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [ITER] Step 5/6: Add feature (iteration 2)...')
    steps.append(_step_add_feature(runner, base_dir))
    print('    [ITER]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [ITER] Step 6/6: Final validation...')
    steps.append(_step_final_validation(runner, base_dir))
    print('    [ITER]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    return steps
