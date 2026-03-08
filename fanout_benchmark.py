"""
Multi-Sandbox Fan-Out Benchmark for Sandbox Profiling.

Tests how fast a single agent process can spin up N sandboxes,
distribute different tasks across them, and collect results.
This simulates an agent parallelizing work (e.g., testing on
different configs, or splitting work across workers).

Steps:
1. Create N sandboxes concurrently
2. Upload code to all sandboxes concurrently
3. Run different compute tasks on each sandbox concurrently
4. Collect results from all sandboxes concurrently
5. Destroy all sandboxes concurrently

NOTE: Unlike other benchmarks, this one manages its own sandbox
lifecycle internally (creates and destroys its own runners).
"""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# Fix SSL cert verification on macOS
try:
    import certifi
    os.environ.setdefault('SSL_CERT_FILE', certifi.where())
except ImportError:
    pass

from run_parallel_profiled import StepProfile


# ── Compute Task Content ──────────────────────────────────────────

COMPUTE_SCRIPT = b'''"""Compute tasks for fan-out benchmark."""
import json
import sys
import time


def factorial(n):
    result = 1
    for i in range(2, n + 1):
        result *= i
    return result


def fibonacci(n):
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a


def primes(limit):
    sieve = [True] * (limit + 1)
    sieve[0] = sieve[1] = False
    for i in range(2, int(limit**0.5) + 1):
        if sieve[i]:
            for j in range(i * i, limit + 1, i):
                sieve[j] = False
    return [i for i in range(limit + 1) if sieve[i]]


def sort_random(n):
    import random
    random.seed(42)
    data = [random.randint(0, 1000000) for _ in range(n)]
    return sorted(data)


if __name__ == "__main__":
    task = sys.argv[1]
    arg = int(sys.argv[2])
    start = time.time()

    if task == "factorial":
        result = str(factorial(arg))[-20:]
    elif task == "fibonacci":
        result = str(fibonacci(arg))[-20:]
    elif task == "primes":
        p = primes(arg)
        result = str(len(p))
    elif task == "sort":
        s = sort_random(arg)
        result = str(len(s))
    else:
        result = "unknown task"

    elapsed = time.time() - start
    output = {"task": task, "arg": arg, "result": result, "elapsed": round(elapsed, 4)}
    json.dump(output, open("task_result.json", "w"))
    print(json.dumps(output))
'''

# Tasks to distribute across sandboxes
TASKS = [
    ('factorial', 500),
    ('fibonacci', 1000),
    ('primes', 10000),
    ('sort', 50000),
]


# ── Helper ─────────────────────────────────────────────────────────

def _create_runner_with_key(provider, api_key):
    """Create a sandbox runner with explicit API key."""
    if provider == 'daytona':
        from daytona_sandbox import DaytonaSandboxRunner
        return DaytonaSandboxRunner(api_key=api_key)
    elif provider == 'e2b':
        from e2b_sandbox import E2BSandboxRunner
        return E2BSandboxRunner(api_key=api_key)
    elif provider == 'blaxel':
        from blaxel_sandbox import BlaxelSandboxRunner
        return BlaxelSandboxRunner(api_key=api_key)
    elif provider == 'modal':
        from modal_sandbox import ModalSandboxRunner
        return ModalSandboxRunner()
    else:
        raise ValueError('Unknown provider: {}'.format(provider))


def _get_base_dir(provider):
    """Get the base directory for this benchmark in a given provider."""
    if provider == 'daytona':
        return '/root/fanout_bench'
    elif provider == 'blaxel':
        return '/blaxel/fanout_bench'
    elif provider == 'modal':
        return '/root/fanout_bench'
    else:
        return '/home/user/fanout_bench'


# ── Custom Image Helpers ───────────────────────────────────────────

CUSTOM_DEPS = ['django', 'djangorestframework', 'pytest', 'flake8', 'numpy']


def _build_custom_image(provider):
    """Build a custom image with pre-installed deps for fan-out."""
    if provider == 'daytona':
        from daytona import Image
        return Image.debian_slim('3.12').pip_install(*CUSTOM_DEPS)
    elif provider == 'modal':
        import modal
        return modal.Image.debian_slim(python_version='3.12').pip_install(*CUSTOM_DEPS)
    return None


def _create_runner_with_custom_image(provider, api_key, image):
    """Create a sandbox runner using a custom image."""
    if provider == 'daytona':
        from daytona import Daytona, DaytonaConfig, CreateSandboxFromImageParams, Resources
        from daytona_sandbox import DaytonaSandboxRunner
        runner = DaytonaSandboxRunner(api_key=api_key)
        daytona = Daytona(DaytonaConfig(api_key=api_key, target='us'))
        sandbox = daytona.create(
            CreateSandboxFromImageParams(
                image=image,
                resources=Resources(cpu=4, memory=8, disk=10),
                auto_stop_interval=30,
            ),
            timeout=300,
        )
        runner._daytona = daytona
        runner._sandbox = sandbox
        runner.sandbox_id = sandbox.id
        return runner
    elif provider == 'modal':
        import modal
        from modal_sandbox import ModalSandboxRunner
        runner = ModalSandboxRunner()
        runner._app = modal.App.lookup("sandbox-rl-test", create_if_missing=True)
        runner._sandbox = modal.Sandbox.create(
            image=image,
            app=runner._app,
            timeout=600,
            cpu=4.0,
            memory=8192,
        )
        runner.sandbox_id = runner._sandbox.object_id
        return runner
    else:
        # E2B and Blaxel don't support custom images -- fall back to default
        return _create_runner_with_key(provider, api_key)


# ── Benchmark Steps ────────────────────────────────────────────────

def _step_create_sandboxes(provider, api_key, num_sandboxes,
                           custom_image=None):
    """Create N sandboxes concurrently."""
    step_name = 'fo_create_custom' if custom_image else 'fo_create_sandboxes'
    step = StepProfile(name=step_name, started_at=time.time())

    runners = []
    errors = []

    def _create_one(idx):
        if custom_image:
            runner = _create_runner_with_custom_image(
                provider, api_key, custom_image)
        else:
            runner = _create_runner_with_key(provider, api_key)
            runner.create_sandbox()
        return runner

    with ThreadPoolExecutor(max_workers=num_sandboxes) as pool:
        futures = {
            pool.submit(_create_one, i): i
            for i in range(num_sandboxes)
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                runner = future.result()
                runners.append((idx, runner))
            except Exception as e:
                errors.append('sandbox-{}: {}'.format(idx, str(e)[:80]))

    # Sort by index to maintain order
    runners.sort(key=lambda x: x[0])
    runner_list = [r for _, r in runners]

    step.ended_at = time.time()
    step.duration_s = step.ended_at - step.started_at
    step.success = len(runner_list) == num_sandboxes
    avg = step.duration_s / max(len(runner_list), 1)
    step.detail = '{} sandboxes in {:.2f}s (avg {:.2f}s each)'.format(
        len(runner_list), step.duration_s, avg)
    if errors:
        step.detail += ', errors: ' + '; '.join(errors[:2])

    return step, runner_list


def _step_upload_code(runners, provider):
    """Upload compute script to all sandboxes concurrently."""
    step = StepProfile(name='fo_upload_code', started_at=time.time())
    base_dir = _get_base_dir(provider)

    uploaded = 0
    errors = []

    def _upload_one(runner):
        runner.exec('mkdir -p {}'.format(base_dir), cwd='/tmp')
        runner.upload_file_native(
            COMPUTE_SCRIPT,
            '{}/compute.py'.format(base_dir),
        )
        return True

    with ThreadPoolExecutor(max_workers=len(runners)) as pool:
        futures = {
            pool.submit(_upload_one, r): i
            for i, r in enumerate(runners)
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                future.result()
                uploaded += 1
            except Exception as e:
                errors.append('sandbox-{}: {}'.format(idx, str(e)[:80]))

    step.ended_at = time.time()
    step.duration_s = step.ended_at - step.started_at
    step.success = uploaded == len(runners)
    step.detail = '{} sandboxes loaded, {}B each, wall={:.2f}s'.format(
        uploaded, len(COMPUTE_SCRIPT), step.duration_s)
    return step


def _step_run_different_tasks(runners, provider):
    """Run a different compute task on each sandbox concurrently."""
    step = StepProfile(name='fo_run_tasks', started_at=time.time())
    base_dir = _get_base_dir(provider)

    task_timings = []
    completed = 0
    errors = []

    def _run_one(runner, task_name, task_arg):
        t0 = time.time()
        result = runner.exec(
            'python3 compute.py {} {}'.format(task_name, task_arg),
            cwd=base_dir,
            timeout=120,
        )
        dur = time.time() - t0
        return result, dur

    with ThreadPoolExecutor(max_workers=len(runners)) as pool:
        futures = {}
        for i, runner in enumerate(runners):
            task_name, task_arg = TASKS[i % len(TASKS)]
            future = pool.submit(_run_one, runner, task_name, task_arg)
            futures[future] = (i, task_name)

        for future in as_completed(futures):
            idx, task_name = futures[future]
            try:
                result, dur = future.result()
                task_timings.append(dur)
                if result['exit_code'] == 0:
                    completed += 1
                else:
                    errors.append('{}: exit={}'.format(
                        task_name, result['exit_code']))
            except Exception as e:
                errors.append('{}: {}'.format(task_name, str(e)[:60]))

    step.ended_at = time.time()
    step.duration_s = step.ended_at - step.started_at
    step.success = completed == len(runners)
    times_str = ', '.join('{:.2f}s'.format(t) for t in task_timings)
    step.detail = '{} tasks done, wall={:.2f}s, times=[{}]'.format(
        completed, step.duration_s, times_str)
    return step


def _step_collect_results(runners, provider):
    """Download result JSON from each sandbox concurrently."""
    step = StepProfile(name='fo_collect_results', started_at=time.time())
    base_dir = _get_base_dir(provider)

    collected = 0
    all_valid = True
    results = []

    def _collect_one(runner):
        content = runner.download_file_native(
            '{}/task_result.json'.format(base_dir))
        data = json.loads(content)
        return data

    with ThreadPoolExecutor(max_workers=len(runners)) as pool:
        futures = {
            pool.submit(_collect_one, r): i
            for i, r in enumerate(runners)
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                data = future.result()
                results.append(data)
                if 'task' in data and 'result' in data:
                    collected += 1
                else:
                    all_valid = False
            except Exception as e:
                all_valid = False

    step.ended_at = time.time()
    step.duration_s = step.ended_at - step.started_at
    step.success = collected == len(runners) and all_valid
    step.detail = '{} results collected, wall={:.2f}s, valid={}'.format(
        collected, step.duration_s, all_valid)
    return step


def _step_destroy_sandboxes(runners):
    """Destroy all sandboxes concurrently."""
    step = StepProfile(name='fo_destroy_sandboxes', started_at=time.time())

    destroyed = 0

    def _destroy_one(runner):
        runner.destroy()
        return True

    with ThreadPoolExecutor(max_workers=len(runners)) as pool:
        futures = {
            pool.submit(_destroy_one, r): i
            for i, r in enumerate(runners)
        }
        for future in as_completed(futures):
            try:
                future.result()
                destroyed += 1
            except Exception:
                pass

    step.ended_at = time.time()
    step.duration_s = step.ended_at - step.started_at
    step.success = destroyed == len(runners)
    step.detail = '{} sandboxes destroyed in {:.2f}s'.format(
        destroyed, step.duration_s)
    return step


# ── Main Benchmark Function ───────────────────────────────────────

def run_fanout_benchmark(runner, provider, api_key=None, num_sandboxes=3):
    """Execute the multi-sandbox fan-out benchmark.

    NOTE: The `runner` parameter is not used for execution (this benchmark
    creates its own sandboxes). It is accepted for interface consistency.

    Args:
        runner: Unused (for interface consistency). Pass None.
        provider: 'daytona', 'e2b', 'blaxel', or 'modal'.
        api_key: API key for the provider (not needed for Modal).
        num_sandboxes: Number of sandboxes to fan out (default 3).

    Returns:
        list[StepProfile]: Profiling data for each benchmark step.
    """
    supports_custom = provider in ('daytona', 'modal')
    total_steps = 6 if supports_custom else 5
    step_num = 0
    steps = []
    runners = []

    # Step 1: Create N sandboxes with stock images
    step_num += 1
    print('    [FO] Step {}/{}: Create {} sandboxes (stock image)...'.format(
        step_num, total_steps, num_sandboxes))
    create_step, runners = _step_create_sandboxes(
        provider, api_key, num_sandboxes)
    steps.append(create_step)
    print('    [FO]   {:.1f}s - {}'.format(create_step.duration_s, create_step.detail))

    if not runners:
        print('    [FO] No sandboxes created, aborting fan-out benchmark')
        return steps

    try:
        step_num += 1
        print('    [FO] Step {}/{}: Upload code to {} sandboxes...'.format(
            step_num, total_steps, len(runners)))
        upload_step = _step_upload_code(runners, provider)
        steps.append(upload_step)
        print('    [FO]   {:.1f}s - {}'.format(
            upload_step.duration_s, upload_step.detail))

        step_num += 1
        print('    [FO] Step {}/{}: Run tasks on {} sandboxes...'.format(
            step_num, total_steps, len(runners)))
        tasks_step = _step_run_different_tasks(runners, provider)
        steps.append(tasks_step)
        print('    [FO]   {:.1f}s - {}'.format(
            tasks_step.duration_s, tasks_step.detail))

        step_num += 1
        print('    [FO] Step {}/{}: Collect results...'.format(
            step_num, total_steps))
        collect_step = _step_collect_results(runners, provider)
        steps.append(collect_step)
        print('    [FO]   {:.1f}s - {}'.format(
            collect_step.duration_s, collect_step.detail))

    finally:
        step_num += 1
        print('    [FO] Step {}/{}: Destroy {} sandboxes...'.format(
            step_num, total_steps, len(runners)))
        destroy_step = _step_destroy_sandboxes(runners)
        steps.append(destroy_step)
        print('    [FO]   {:.1f}s - {}'.format(
            destroy_step.duration_s, destroy_step.detail))

    # Step 6 (Daytona/Modal only): Fan-out with custom images
    if supports_custom:
        step_num += 1
        print('    [FO] Step {}/{}: Create {} sandboxes (custom image)...'.format(
            step_num, total_steps, num_sandboxes))
        try:
            custom_image = _build_custom_image(provider)
            custom_step, custom_runners = _step_create_sandboxes(
                provider, api_key, num_sandboxes, custom_image=custom_image)
            steps.append(custom_step)
            print('    [FO]   {:.1f}s - {}'.format(
                custom_step.duration_s, custom_step.detail))

            # Destroy the custom image sandboxes
            if custom_runners:
                _step_destroy_sandboxes(custom_runners)
        except Exception as e:
            err_step = StepProfile(name='fo_create_custom',
                                  started_at=time.time())
            err_step.ended_at = time.time()
            err_step.duration_s = 0.0
            err_step.success = False
            err_step.detail = 'custom image fan-out failed: {}'.format(
                str(e)[:200])
            steps.append(err_step)
            print('    [FO]   FAILED - {}'.format(err_step.detail))

    return steps
