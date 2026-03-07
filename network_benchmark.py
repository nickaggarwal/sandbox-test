"""
Network Speed Benchmark for Sandbox Profiling.

Benchmarks network latency, throughput (download/upload), DNS resolution,
and real-world package install speed from inside each sandbox.
Returns a list of StepProfile objects compatible with the parallel profiler.
"""
import time

from run_parallel_profiled import StepProfile


# ── Benchmark Steps ─────────────────────────────────────────────────

def _step_net_latency(runner, base_dir):
    """Measure HTTP round-trip latency with 5 small GET requests."""
    step = StepProfile(name='net_latency', started_at=time.time())

    try:
        # Use curl with timing output for 5 requests to a fast endpoint
        cmd = (
            "python3 -c \""
            "import subprocess, json; "
            "times = []; "
            "[times.append(float(subprocess.run("
            "['curl', '-s', '-o', '/dev/null', '-w', '%{time_total}', "
            "'https://www.google.com/robots.txt'], "
            "capture_output=True, text=True).stdout)) "
            "for _ in range(5)]; "
            "print(json.dumps({"
            "'min': round(min(times)*1000, 1), "
            "'avg': round(sum(times)/len(times)*1000, 1), "
            "'max': round(max(times)*1000, 1), "
            "'samples': len(times)"
            "}))\""
        )
        result = runner.exec(cmd, cwd=base_dir)

        import json
        if result['exit_code'] == 0 and result['result'].strip():
            data = json.loads(result['result'].strip())
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at
            step.success = True
            step.detail = 'min={min}ms avg={avg}ms max={max}ms ({samples} samples)'.format(**data)
        else:
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at
            step.success = False
            step.detail = 'curl failed: {}'.format(result['result'][:200])
    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = str(e)[:200]

    return step


def _step_net_download(runner, base_dir):
    """Measure download throughput with a ~10MB file."""
    step = StepProfile(name='net_download', started_at=time.time())

    try:
        # Download ~10MB from Cloudflare speed test and measure throughput
        cmd = (
            "curl -s -o /dev/null "
            "-w '%{speed_download} %{size_download} %{time_total}' "
            "'https://speed.cloudflare.com/__down?bytes=10000000'"
        )
        result = runner.exec(cmd, cwd=base_dir)

        if result['exit_code'] == 0 and result['result'].strip():
            parts = result['result'].strip().split()
            if len(parts) >= 3:
                speed_bps = float(parts[0])
                size_bytes = int(float(parts[1]))
                total_time = float(parts[2])
                speed_mbps = speed_bps / (1024 * 1024)

                step.ended_at = time.time()
                step.duration_s = step.ended_at - step.started_at
                step.success = size_bytes > 0
                step.detail = '{:.1f}MB in {:.1f}s ({:.2f} MB/s)'.format(
                    size_bytes / (1024 * 1024), total_time, speed_mbps)
            else:
                step.ended_at = time.time()
                step.duration_s = step.ended_at - step.started_at
                step.success = False
                step.detail = 'unexpected curl output: {}'.format(result['result'][:200])
        else:
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at
            step.success = False
            step.detail = 'download failed: {}'.format(result['result'][:200])
    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = str(e)[:200]

    return step


def _step_net_upload(runner, base_dir):
    """Measure upload throughput by POSTing a ~5MB file."""
    step = StepProfile(name='net_upload', started_at=time.time())

    try:
        # Generate 5MB of random data
        gen_cmd = (
            "python3 -c \""
            "import os; "
            "f = open('{}/upload_test.bin', 'wb'); "
            "f.write(os.urandom(5 * 1024 * 1024)); "
            "f.close(); "
            "print('generated')\""
        ).format(base_dir)
        gen_result = runner.exec(gen_cmd, cwd=base_dir)

        if gen_result['exit_code'] != 0:
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at
            step.success = False
            step.detail = 'file generation failed: {}'.format(gen_result['result'][:200])
            return step

        # Upload to httpbin.org/post and measure speed
        upload_cmd = (
            "curl -s -o /dev/null "
            "-w '%{speed_upload} %{size_upload} %{time_total}' "
            "-X POST -F 'file=@{}/upload_test.bin' "
            "https://httpbin.org/post"
        ).format(base_dir)
        result = runner.exec(upload_cmd, cwd=base_dir)

        if result['exit_code'] == 0 and result['result'].strip():
            parts = result['result'].strip().split()
            if len(parts) >= 3:
                speed_bps = float(parts[0])
                size_bytes = int(float(parts[1]))
                total_time = float(parts[2])
                speed_mbps = speed_bps / (1024 * 1024)

                step.ended_at = time.time()
                step.duration_s = step.ended_at - step.started_at
                step.success = size_bytes > 0
                step.detail = '{:.1f}MB in {:.1f}s ({:.2f} MB/s)'.format(
                    size_bytes / (1024 * 1024), total_time, speed_mbps)
            else:
                step.ended_at = time.time()
                step.duration_s = step.ended_at - step.started_at
                step.success = False
                step.detail = 'unexpected curl output: {}'.format(result['result'][:200])
        else:
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at
            step.success = False
            step.detail = 'upload failed: {}'.format(result['result'][:200])
    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = str(e)[:200]

    return step


def _step_net_dns(runner, base_dir):
    """Measure DNS resolution time for multiple hostnames."""
    step = StepProfile(name='net_dns', started_at=time.time())

    try:
        cmd = (
            "python3 -c \""
            "import socket, time, json; "
            "hosts = ['google.com', 'github.com', 'pypi.org', 'cloudflare.com', 'amazonaws.com']; "
            "results = []; "
            "for h in hosts: "
            "    t0 = time.time(); "
            "    try: "
            "        socket.getaddrinfo(h, 443); "
            "        results.append({'host': h, 'ms': round((time.time()-t0)*1000, 2), 'ok': True}); "
            "    except Exception as e: "
            "        results.append({'host': h, 'ms': round((time.time()-t0)*1000, 2), 'ok': False}); "
            "times = [r['ms'] for r in results if r['ok']]; "
            "print(json.dumps({"
            "'resolved': sum(1 for r in results if r['ok']), "
            "'total': len(hosts), "
            "'min': round(min(times), 2) if times else 0, "
            "'avg': round(sum(times)/len(times), 2) if times else 0, "
            "'max': round(max(times), 2) if times else 0"
            "}))\""
        )
        result = runner.exec(cmd, cwd=base_dir)

        import json
        if result['exit_code'] == 0 and result['result'].strip():
            data = json.loads(result['result'].strip())
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at
            step.success = data['resolved'] == data['total']
            step.detail = '{resolved}/{total} resolved, min={min}ms avg={avg}ms max={max}ms'.format(**data)
        else:
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at
            step.success = False
            step.detail = 'DNS test failed: {}'.format(result['result'][:200])
    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = str(e)[:200]

    return step


def _step_net_pip_install(runner, base_dir):
    """Measure real-world network impact by timing pip install requests."""
    step = StepProfile(name='net_pip_install', started_at=time.time())

    try:
        # Uninstall first to ensure a clean install
        runner.exec('pip uninstall -y requests 2>/dev/null', cwd=base_dir)

        # Clear pip cache to force network fetch
        runner.exec('pip cache purge 2>/dev/null', cwd=base_dir)

        # Time the install
        cmd = (
            "python3 -c \""
            "import subprocess, time; "
            "t0 = time.time(); "
            "r = subprocess.run(['pip', 'install', 'requests'], "
            "capture_output=True, text=True); "
            "elapsed = time.time() - t0; "
            "print('{:.2f} {}'.format(elapsed, r.returncode))\""
        )
        result = runner.exec(cmd, cwd=base_dir)

        if result['exit_code'] == 0 and result['result'].strip():
            parts = result['result'].strip().split()
            if len(parts) >= 2:
                elapsed = float(parts[0])
                exit_code = int(parts[1])

                step.ended_at = time.time()
                step.duration_s = step.ended_at - step.started_at
                step.success = exit_code == 0
                step.detail = 'pip install requests: {:.2f}s (exit={})'.format(
                    elapsed, exit_code)
            else:
                step.ended_at = time.time()
                step.duration_s = step.ended_at - step.started_at
                step.success = False
                step.detail = 'unexpected output: {}'.format(result['result'][:200])
        else:
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at
            step.success = False
            step.detail = 'pip install failed: {}'.format(result['result'][:200])
    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = str(e)[:200]

    return step


# ── Main Benchmark Function ────────────────────────────────────────

def run_network_benchmark(runner, provider):
    """Execute the full network speed benchmark suite.

    Args:
        runner: A sandbox runner instance (must already have a sandbox created).
        provider: 'daytona', 'e2b', 'blaxel', or 'modal' (for path resolution).

    Returns:
        list[StepProfile]: Profiling data for each benchmark step.
    """
    if provider == 'daytona':
        base_dir = '/root/net_bench'
    elif provider == 'blaxel':
        base_dir = '/blaxel/net_bench'
    elif provider == 'modal':
        base_dir = '/root/net_bench'
    else:
        base_dir = '/home/user/net_bench'
    runner.exec('mkdir -p {}'.format(base_dir), cwd='/tmp')

    steps = []

    print('    [NET] Step 1/5: HTTP latency...')
    steps.append(_step_net_latency(runner, base_dir))
    print('    [NET]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [NET] Step 2/5: Download throughput...')
    steps.append(_step_net_download(runner, base_dir))
    print('    [NET]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [NET] Step 3/5: Upload throughput...')
    steps.append(_step_net_upload(runner, base_dir))
    print('    [NET]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [NET] Step 4/5: DNS resolution...')
    steps.append(_step_net_dns(runner, base_dir))
    print('    [NET]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [NET] Step 5/5: pip install (real-world)...')
    steps.append(_step_net_pip_install(runner, base_dir))
    print('    [NET]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    return steps
