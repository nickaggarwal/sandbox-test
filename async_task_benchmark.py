"""
Async Task + Pause/Resume Benchmark for Sandbox Profiling.

Tests:
1. Start a long-running background task (async)
2. Write state files while task runs
3. Pause the sandbox mid-task
4. Resume the sandbox
5. Verify filesystem state survived pause/resume
6. Verify background task can be restarted after resume
7. Download results and verify integrity

Measures pause latency, resume latency, and state preservation reliability.
"""
import json
import time

from run_parallel_profiled import StepProfile


def run_async_task_benchmark(runner, provider):
    """Execute the async task + pause/resume benchmark.

    Args:
        runner: A sandbox runner instance (already created).
        provider: 'daytona', 'e2b', or 'blaxel'.

    Returns:
        list[StepProfile]: Profiling data for each benchmark step.
    """
    if provider == 'daytona':
        base_dir = '/home/daytona/async_bench'
    elif provider == 'blaxel':
        base_dir = '/blaxel/async_bench'
    elif provider == 'modal':
        base_dir = '/root/async_bench'
    else:
        base_dir = '/home/user/async_bench'

    runner.exec('mkdir -p {}'.format(base_dir), cwd='/tmp')

    steps = []

    print('    [ASYNC] Step 1/7: Start background task...')
    steps.append(_step_start_bg_task(runner, base_dir))
    print('    [ASYNC]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [ASYNC] Step 2/7: Write state files...')
    steps.append(_step_write_state(runner, base_dir))
    print('    [ASYNC]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [ASYNC] Step 3/7: Pause sandbox...')
    steps.append(_step_pause(runner, provider))
    print('    [ASYNC]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [ASYNC] Step 4/7: Resume sandbox...')
    steps.append(_step_resume(runner, provider))
    print('    [ASYNC]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [ASYNC] Step 5/7: Verify state after resume...')
    steps.append(_step_verify_state(runner, base_dir))
    print('    [ASYNC]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [ASYNC] Step 6/7: Restart task after resume...')
    steps.append(_step_restart_task(runner, base_dir))
    print('    [ASYNC]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [ASYNC] Step 7/7: Download & verify results...')
    steps.append(_step_download_verify(runner, base_dir))
    print('    [ASYNC]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    return steps


def _step_start_bg_task(runner, base_dir):
    """Start a background Python task that writes progress to a file."""
    step = StepProfile(name='async_start_task', started_at=time.time())

    # Create a Python script that runs as a background task
    script = '''
import json, time, os
output_dir = "{base_dir}"
os.makedirs(output_dir, exist_ok=True)
progress_file = os.path.join(output_dir, "progress.json")
result_file = os.path.join(output_dir, "task_result.json")
total_steps = 20
for i in range(total_steps):
    progress = {{"step": i+1, "total": total_steps, "pct": round((i+1)/total_steps*100, 1), "ts": time.time()}}
    with open(progress_file, "w") as f:
        json.dump(progress, f)
    time.sleep(0.5)
result = {{"status": "complete", "steps_done": total_steps, "finished_at": time.time()}}
with open(result_file, "w") as f:
    json.dump(result, f)
'''.format(base_dir=base_dir)

    # Write the script to sandbox
    script_path = '{}/bg_task.py'.format(base_dir)
    try:
        runner.upload_file_native(script.encode(), script_path)
    except Exception:
        # Fallback: write via exec
        runner.exec(
            "python3 -c \"f=open('{}','w'); f.write('''{}'''); f.close()\"".format(
                script_path, script.replace("'", "\\'")),
            cwd=base_dir,
        )

    # Launch in background
    result = runner.exec(
        'nohup python3 {} > {}/bg_task.log 2>&1 &'.format(
            script_path, base_dir),
        cwd=base_dir,
    )

    # Wait a moment for task to start writing progress
    time.sleep(2)

    # Check that it's running
    check = runner.exec(
        'cat {}/progress.json 2>/dev/null || echo not_started'.format(base_dir),
        cwd=base_dir,
    )

    step.ended_at = time.time()
    step.duration_s = step.ended_at - step.started_at
    step.success = 'step' in check['result']
    step.detail = 'bg task started, progress: {}'.format(
        check['result'].strip()[:100])
    return step


def _step_write_state(runner, base_dir):
    """Write multiple state files to test persistence across pause/resume."""
    step = StepProfile(name='async_write_state', started_at=time.time())

    state_data = {
        'checkpoint.json': json.dumps({
            'model_weights': [0.1, 0.2, 0.3, 0.4, 0.5],
            'epoch': 42,
            'loss': 0.0312,
            'optimizer_state': {'lr': 0.001, 'beta1': 0.9},
        }).encode(),
        'config.yaml': b'model:\n  layers: 4\n  hidden: 256\n  dropout: 0.1\ntraining:\n  epochs: 100\n  batch_size: 32\n',
        'data_cache.bin': bytes(range(256)) * 40,  # 10KB binary
    }

    written = 0
    total_bytes = 0
    for name, content in state_data.items():
        path = '{}/{}'.format(base_dir, name)
        try:
            runner.upload_file_native(content, path)
            written += 1
            total_bytes += len(content)
        except Exception as e:
            print('      Write failed {}: {}'.format(name, e))

    step.ended_at = time.time()
    step.duration_s = step.ended_at - step.started_at
    step.success = written == len(state_data)
    step.detail = '{}/{} files, {}KB'.format(
        written, len(state_data), total_bytes // 1024)
    return step


def _step_pause(runner, provider):
    """Pause the sandbox."""
    step = StepProfile(name='async_pause', started_at=time.time())
    try:
        runner.pause_sandbox()
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = True
        step.detail = 'paused in {:.3f}s'.format(step.duration_s)
    except NotImplementedError as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = 'pause unsupported: {}'.format(str(e)[:120])
    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = 'pause failed: {}'.format(str(e)[:150])
    return step


def _step_resume(runner, provider):
    """Resume the sandbox."""
    step = StepProfile(name='async_resume', started_at=time.time())
    try:
        runner.resume_sandbox()
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = True
        step.detail = 'resumed in {:.3f}s'.format(step.duration_s)
    except NotImplementedError as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = 'resume unsupported: {}'.format(str(e)[:120])
    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = 'resume failed: {}'.format(str(e)[:150])
    return step


def _step_verify_state(runner, base_dir):
    """Verify that files written before pause still exist after resume."""
    step = StepProfile(name='async_verify_state', started_at=time.time())

    expected_files = ['checkpoint.json', 'config.yaml', 'data_cache.bin',
                      'bg_task.py', 'progress.json']
    verified = 0
    details = []

    for fname in expected_files:
        path = '{}/{}'.format(base_dir, fname)
        try:
            content = runner.download_file_native(path)
            if content and len(content) > 0:
                verified += 1
                details.append('{}: {}B'.format(fname, len(content)))
            else:
                details.append('{}: EMPTY'.format(fname))
        except Exception as e:
            details.append('{}: MISSING'.format(fname))

    # Verify checkpoint JSON integrity
    integrity_ok = False
    try:
        ckpt = runner.download_file_native(
            '{}/checkpoint.json'.format(base_dir))
        data = json.loads(ckpt)
        integrity_ok = (
            data.get('epoch') == 42
            and data.get('loss') == 0.0312
            and len(data.get('model_weights', [])) == 5
        )
    except Exception:
        pass

    # Verify binary file integrity
    binary_ok = False
    try:
        bindata = runner.download_file_native(
            '{}/data_cache.bin'.format(base_dir))
        binary_ok = (len(bindata) == 10240
                     and bindata[:256] == bytes(range(256)))
    except Exception:
        pass

    step.ended_at = time.time()
    step.duration_s = step.ended_at - step.started_at
    step.success = verified >= 4 and integrity_ok
    step.detail = '{}/{} files, json={}, bin={}'.format(
        verified, len(expected_files),
        'OK' if integrity_ok else 'FAIL',
        'OK' if binary_ok else 'FAIL',
    )
    return step


def _step_restart_task(runner, base_dir):
    """Restart a compute task after resume and verify it completes."""
    step = StepProfile(name='async_restart_task', started_at=time.time())

    # Write a compute script then run it
    script = (
        'import json, time\n'
        'def fib(n):\n'
        '    a, b = 0, 1\n'
        '    for _ in range(n): a, b = b, a+b\n'
        '    return a\n'
        'start = time.time()\n'
        'results = [fib(i*100) for i in range(1, 51)]\n'
        'elapsed = time.time() - start\n'
        'data = dict(fib_count=len(results), elapsed=round(elapsed,3),\n'
        '    last_digits=[r % 10000 for r in results[-5:]])\n'
        'json.dump(data, open("{base_dir}/restart_result.json", "w"))\n'
    ).format(base_dir=base_dir)

    script_path = '{}/restart_compute.py'.format(base_dir)
    try:
        runner.upload_file_native(script.encode(), script_path)
    except Exception:
        runner.exec(
            "cat > {} << 'PYEOF'\n{}\nPYEOF".format(script_path, script),
            cwd=base_dir,
        )

    compute_cmd = 'python3 {}'.format(script_path)

    result = runner.exec(compute_cmd, cwd=base_dir, timeout=60)

    # Verify result file
    task_ok = False
    elapsed = 0
    try:
        content = runner.download_file_native(
            '{}/restart_result.json'.format(base_dir))
        data = json.loads(content)
        task_ok = data.get('fib_count') == 50
        elapsed = data.get('elapsed', 0)
    except Exception:
        pass

    step.ended_at = time.time()
    step.duration_s = step.ended_at - step.started_at
    step.success = result['exit_code'] == 0 and task_ok
    step.detail = 'compute {}, fib50 in {:.3f}s'.format(
        'OK' if task_ok else 'FAIL', elapsed)
    return step


def _step_download_verify(runner, base_dir):
    """Download all result files and verify total integrity."""
    step = StepProfile(name='async_download_verify', started_at=time.time())

    files_to_check = [
        'checkpoint.json', 'config.yaml', 'data_cache.bin',
        'progress.json', 'restart_result.json', 'bg_task.py',
    ]

    downloaded = 0
    total_bytes = 0

    for fname in files_to_check:
        path = '{}/{}'.format(base_dir, fname)
        try:
            content = runner.download_file_native(path)
            if content and len(content) > 0:
                downloaded += 1
                total_bytes += len(content)
        except Exception:
            pass

    step.ended_at = time.time()
    step.duration_s = step.ended_at - step.started_at
    step.success = downloaded >= 5
    step.detail = '{}/{} files, {}KB total'.format(
        downloaded, len(files_to_check), total_bytes // 1024)
    return step
