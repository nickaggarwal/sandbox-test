"""
Network Speed Benchmark for Sandbox Profiling.

Benchmarks network latency, throughput (download/upload), DNS resolution,
and real-world package install speed from inside each sandbox.
Returns a list of StepProfile objects compatible with the parallel profiler.

All tests use pure Python (urllib/socket) -- no curl dependency.
"""
import time

from run_parallel_profiled import StepProfile


# ── Helper: write a Python script and run it ───────────────────────

def _run_script(runner, base_dir, filename, script, timeout=120):
    """Write a Python script to the sandbox and execute it."""
    import base64
    encoded = base64.b64encode(script.encode('utf-8')).decode()
    write_cmd = (
        "python3 -c \""
        "import base64; "
        "data = base64.b64decode('{}'); "
        "f = open('{}/{}', 'wb'); "
        "f.write(data); f.close()\""
    ).format(encoded, base_dir, filename)
    runner.exec(write_cmd, cwd=base_dir)
    return runner.exec('python3 {}/{}'.format(base_dir, filename),
                       cwd=base_dir, timeout=timeout)


# ── Benchmark Steps ─────────────────────────────────────────────────

LATENCY_SCRIPT = """\
import urllib.request
import json
import time

times = []
for _ in range(5):
    t0 = time.time()
    try:
        req = urllib.request.Request('https://www.google.com/robots.txt')
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        times.append(time.time() - t0)
    except Exception:
        pass

if times:
    print(json.dumps({
        'min': round(min(times) * 1000, 1),
        'avg': round(sum(times) / len(times) * 1000, 1),
        'max': round(max(times) * 1000, 1),
        'samples': len(times)
    }))
else:
    print('ERROR: no successful requests')
"""


def _step_net_latency(runner, base_dir):
    """Measure HTTP round-trip latency with 5 small GET requests."""
    step = StepProfile(name='net_latency', started_at=time.time())

    try:
        result = _run_script(runner, base_dir, 'net_latency.py', LATENCY_SCRIPT)

        import json
        output = result['result'].strip()
        if result['exit_code'] == 0 and output and not output.startswith('ERROR'):
            data = json.loads(output)
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at
            step.success = True
            step.detail = 'min={min}ms avg={avg}ms max={max}ms ({samples} samples)'.format(**data)
        else:
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at
            step.success = False
            step.detail = 'latency test failed: {}'.format(output[:200])
    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = str(e)[:200]

    return step


DOWNLOAD_SCRIPT = """\
import urllib.request
import json
import time

url = 'https://speed.cloudflare.com/__down?bytes=10000000'
t0 = time.time()
try:
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (compatible; SandboxBenchmark/1.0)'
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    elapsed = time.time() - t0
    size = len(data)
    speed_mbps = (size / (1024 * 1024)) / elapsed if elapsed > 0 else 0
    print(json.dumps({
        'size_mb': round(size / (1024 * 1024), 1),
        'time_s': round(elapsed, 2),
        'speed_mbps': round(speed_mbps, 2)
    }))
except Exception as e:
    print(json.dumps({'error': str(e)[:200]}))
"""


def _step_net_download(runner, base_dir):
    """Measure download throughput with a ~10MB file."""
    step = StepProfile(name='net_download', started_at=time.time())

    try:
        result = _run_script(runner, base_dir, 'net_download.py', DOWNLOAD_SCRIPT)

        import json
        output = result['result'].strip()
        if result['exit_code'] == 0 and output:
            data = json.loads(output)
            if 'error' in data:
                step.ended_at = time.time()
                step.duration_s = step.ended_at - step.started_at
                step.success = False
                step.detail = 'download error: {}'.format(data['error'])
            else:
                step.ended_at = time.time()
                step.duration_s = step.ended_at - step.started_at
                step.success = data['size_mb'] > 0
                step.detail = '{size_mb}MB in {time_s}s ({speed_mbps} MB/s)'.format(**data)
        else:
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at
            step.success = False
            step.detail = 'download failed: {}'.format(output[:200])
    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = str(e)[:200]

    return step


DOWNLOAD_LARGE_SCRIPT = """\
import urllib.request
import json
import time

# Sustained ~100MB download: 10x 10MB fetches from Cloudflare
url = 'https://speed.cloudflare.com/__down?bytes=10000000'
total_bytes = 0
t0 = time.time()
try:
    for i in range(10):
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; SandboxBenchmark/1.0)'
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
    elapsed = time.time() - t0
    speed_mbps = (total_bytes / (1024 * 1024)) / elapsed if elapsed > 0 else 0
    print(json.dumps({
        'size_mb': round(total_bytes / (1024 * 1024), 1),
        'time_s': round(elapsed, 2),
        'speed_mbps': round(speed_mbps, 2)
    }))
except Exception as e:
    elapsed = time.time() - t0
    print(json.dumps({'error': str(e)[:200], 'partial_mb': round(total_bytes / (1024*1024), 1)}))
"""


def _step_net_download_large(runner, base_dir):
    """Measure download throughput with a ~100MB file."""
    step = StepProfile(name='net_download_large', started_at=time.time())

    try:
        result = _run_script(runner, base_dir, 'net_download_lg.py',
                             DOWNLOAD_LARGE_SCRIPT, timeout=120)

        import json
        output = result['result'].strip()
        if result['exit_code'] == 0 and output:
            data = json.loads(output)
            if 'error' in data:
                step.ended_at = time.time()
                step.duration_s = step.ended_at - step.started_at
                step.success = False
                step.detail = 'download error: {}'.format(data['error'])
            else:
                step.ended_at = time.time()
                step.duration_s = step.ended_at - step.started_at
                step.success = data['size_mb'] > 90
                step.detail = '{size_mb}MB in {time_s}s ({speed_mbps} MB/s)'.format(**data)
        else:
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at
            step.success = False
            step.detail = 'download failed: {}'.format(output[:200])
    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = str(e)[:200]

    return step


UPLOAD_SCRIPT = """\
import urllib.request
import json
import time
import os

# Generate 5MB of random data
payload = os.urandom(5 * 1024 * 1024)

# POST to httpbin.org/post
boundary = '----PythonBenchmark'
header_part = ('--' + boundary + '\\r\\n'
    'Content-Disposition: form-data; name="file"; filename="test.bin"\\r\\n'
    'Content-Type: application/octet-stream\\r\\n\\r\\n').encode()
footer_part = ('\\r\\n--' + boundary + '--\\r\\n').encode()
body = header_part + payload + footer_part

req = urllib.request.Request(
    'https://httpbin.org/post',
    data=body,
    headers={
        'Content-Type': 'multipart/form-data; boundary=' + boundary,
        'Content-Length': str(len(body))
    },
    method='POST'
)

t0 = time.time()
try:
    with urllib.request.urlopen(req, timeout=60) as resp:
        resp.read()
    elapsed = time.time() - t0
    size_mb = len(payload) / (1024 * 1024)
    speed = size_mb / elapsed if elapsed > 0 else 0
    print(json.dumps({
        'size_mb': round(size_mb, 1),
        'time_s': round(elapsed, 2),
        'speed_mbps': round(speed, 2)
    }))
except Exception as e:
    print(json.dumps({'error': str(e)[:200]}))
"""


def _step_net_upload(runner, base_dir):
    """Measure upload throughput by POSTing a ~5MB file."""
    step = StepProfile(name='net_upload', started_at=time.time())

    try:
        result = _run_script(runner, base_dir, 'net_upload.py', UPLOAD_SCRIPT)

        import json
        output = result['result'].strip()
        if result['exit_code'] == 0 and output:
            data = json.loads(output)
            if 'error' in data:
                step.ended_at = time.time()
                step.duration_s = step.ended_at - step.started_at
                step.success = False
                step.detail = 'upload error: {}'.format(data['error'])
            else:
                step.ended_at = time.time()
                step.duration_s = step.ended_at - step.started_at
                step.success = data['size_mb'] > 0
                step.detail = '{size_mb}MB in {time_s}s ({speed_mbps} MB/s)'.format(**data)
        else:
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at
            step.success = False
            step.detail = 'upload failed: {}'.format(output[:200])
    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = str(e)[:200]

    return step


DNS_SCRIPT = """\
import socket
import time
import json

hosts = ['google.com', 'github.com', 'pypi.org', 'cloudflare.com', 'amazonaws.com']
results = []
for h in hosts:
    t0 = time.time()
    try:
        socket.getaddrinfo(h, 443)
        results.append({'host': h, 'ms': round((time.time() - t0) * 1000, 2), 'ok': True})
    except Exception:
        results.append({'host': h, 'ms': round((time.time() - t0) * 1000, 2), 'ok': False})

times = [r['ms'] for r in results if r['ok']]
print(json.dumps({
    'resolved': sum(1 for r in results if r['ok']),
    'total': len(hosts),
    'min': round(min(times), 2) if times else 0,
    'avg': round(sum(times) / len(times), 2) if times else 0,
    'max': round(max(times), 2) if times else 0
}))
"""


def _step_net_dns(runner, base_dir):
    """Measure DNS resolution time for multiple hostnames."""
    step = StepProfile(name='net_dns', started_at=time.time())

    try:
        result = _run_script(runner, base_dir, 'net_dns.py', DNS_SCRIPT)

        import json
        output = result['result'].strip()
        if result['exit_code'] == 0 and output:
            data = json.loads(output)
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at
            step.success = data['resolved'] == data['total']
            step.detail = '{resolved}/{total} resolved, min={min}ms avg={avg}ms max={max}ms'.format(**data)
        else:
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at
            step.success = False
            step.detail = 'DNS test failed: {}'.format(output[:200])
    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = str(e)[:200]

    return step


PIP_INSTALL_SCRIPT = """\
import subprocess
import time

t0 = time.time()
r = subprocess.run(['pip', 'install', 'requests'], capture_output=True, text=True)
elapsed = time.time() - t0
print('{:.2f} {}'.format(elapsed, r.returncode))
"""


def _step_net_pip_install(runner, base_dir):
    """Measure real-world network impact by timing pip install requests."""
    step = StepProfile(name='net_pip_install', started_at=time.time())

    try:
        # Uninstall first to ensure a clean install
        runner.exec('pip uninstall -y requests 2>/dev/null', cwd=base_dir)

        # Clear pip cache to force network fetch
        runner.exec('pip cache purge 2>/dev/null', cwd=base_dir)

        # Time the install
        result = _run_script(runner, base_dir, 'net_pip.py', PIP_INSTALL_SCRIPT)

        output = result['result'].strip()
        if result['exit_code'] == 0 and output:
            parts = output.split()
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
                step.detail = 'unexpected output: {}'.format(output[:200])
        else:
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at
            step.success = False
            step.detail = 'pip install failed: {}'.format(output[:200])
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

    print('    [NET] Step 1/6: HTTP latency...')
    steps.append(_step_net_latency(runner, base_dir))
    print('    [NET]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [NET] Step 2/6: Download throughput (10MB)...')
    steps.append(_step_net_download(runner, base_dir))
    print('    [NET]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [NET] Step 3/6: Download throughput (100MB)...')
    steps.append(_step_net_download_large(runner, base_dir))
    print('    [NET]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [NET] Step 4/6: Upload throughput (5MB)...')
    steps.append(_step_net_upload(runner, base_dir))
    print('    [NET]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [NET] Step 5/6: DNS resolution...')
    steps.append(_step_net_dns(runner, base_dir))
    print('    [NET]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [NET] Step 6/6: pip install (real-world)...')
    steps.append(_step_net_pip_install(runner, base_dir))
    print('    [NET]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    return steps
