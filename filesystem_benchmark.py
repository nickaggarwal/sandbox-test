"""
Filesystem Benchmark for Sandbox Profiling.

Benchmarks filesystem operations that simulate an agent writing code,
building artifacts, and transferring files via native FS APIs.
Returns a list of StepProfile objects compatible with the parallel profiler.
"""
import base64
import json
import time

from run_parallel_profiled import StepProfile


# ── Test Data Generators ────────────────────────────────────────────

def generate_python_source(name, size_hint='small'):
    """Generate a synthetic Python source file as bytes.

    size_hint: 'small' (~500B), 'medium' (~5KB), 'large' (~20KB)
    """
    sizes = {'small': 500, 'medium': 5000, 'large': 20000}
    target = sizes.get(size_hint, 500)

    lines = [
        '"""Auto-generated module: {}."""'.format(name),
        'import os',
        'import json',
        'import datetime',
        '',
    ]

    class_num = 0
    while sum(len(l) + 1 for l in lines) < target:
        class_num += 1
        lines.extend([
            '',
            'class Handler{}:'.format(class_num),
            '    """Handler class number {}.'.format(class_num),
            '    Processes data and returns results.',
            '    """',
            '',
            '    def __init__(self, config=None):',
            '        self.config = config or {}',
            '        self.data = []',
            '        self.counter = 0',
            '',
            '    def process(self, input_data):',
            '        result = {}',
            '        for key, value in input_data.items():',
            '            result[key] = str(value).upper()',
            '        self.data.append(result)',
            '        self.counter += 1',
            '        return result',
            '',
            '    def validate(self):',
            '        return len(self.data) > 0 and self.counter > 0',
            '',
            '    def reset(self):',
            '        self.data = []',
            '        self.counter = 0',
            '',
        ])

    return '\n'.join(lines).encode('utf-8')


def generate_csv_data(rows=500):
    """Generate CSV data as bytes."""
    lines = ['id,name,value,category,timestamp']
    for i in range(rows):
        lines.append('{},item_{},{:.4f},cat_{},{}'.format(
            i, i, i * 3.14159, i % 5,
            '2024-01-{:02d}T{:02d}:00:00'.format(i % 28 + 1, i % 24),
        ))
    return '\n'.join(lines).encode('utf-8')


def generate_large_json(size_bytes=1_000_000):
    """Generate a ~1MB JSON document as bytes."""
    records = []
    record_size_approx = 200
    num_records = size_bytes // record_size_approx

    for i in range(num_records):
        records.append({
            'id': i,
            'name': 'record_{}'.format(i),
            'value': round(i * 3.14159, 5),
            'tags': ['alpha', 'beta', 'gamma'],
            'metadata': {'source': 'benchmark', 'index': i},
        })

    data = {
        'version': '1.0',
        'benchmark': 'filesystem_io',
        'records': records,
    }
    return json.dumps(data).encode('utf-8')


# ── Benchmark Steps ─────────────────────────────────────────────────

def _step_code_generation(runner, base_dir):
    """Use exec() to generate Python files in sandbox (simulates agent coding)."""
    step = StepProfile(name='fs_code_generation', started_at=time.time())

    runner.exec('mkdir -p {}/src'.format(base_dir), cwd='/tmp')

    files_created = 0
    total_bytes = 0

    for i in range(10):
        if i < 4:
            size = 'small'
        elif i < 7:
            size = 'medium'
        else:
            size = 'large'

        filename = 'module_{}.py'.format(i)
        source = generate_python_source(filename, size)
        encoded = base64.b64encode(source).decode()

        write_cmd = (
            "python3 -c \""
            "import base64; "
            "data = base64.b64decode('{}'); "
            "f = open('{}/src/{}', 'wb'); "
            "f.write(data); f.close()\""
        ).format(encoded, base_dir, filename)

        result = runner.exec(write_cmd, cwd=base_dir)
        if result['exit_code'] == 0:
            files_created += 1
            total_bytes += len(source)

    step.ended_at = time.time()
    step.duration_s = step.ended_at - step.started_at
    step.success = files_created == 10
    step.detail = '{} files, {}KB total'.format(
        files_created, total_bytes // 1024)
    return step


def _step_build_compile(runner, base_dir):
    """Compile Python files to .pyc build artifacts."""
    step = StepProfile(name='fs_build_compile', started_at=time.time())

    result = runner.exec(
        'python3 -m compileall -b {}/src 2>&1'.format(base_dir),
        cwd=base_dir,
    )

    count_result = runner.exec(
        'find {}/src -name "*.pyc" | wc -l'.format(base_dir),
        cwd=base_dir,
    )
    pyc_count = count_result['result'].strip()

    step.ended_at = time.time()
    step.duration_s = step.ended_at - step.started_at
    step.success = result['exit_code'] == 0
    step.detail = '{} .pyc files generated'.format(pyc_count)
    return step


def _step_upload_files(runner, base_dir):
    """Upload test files individually via native FS API."""
    step = StepProfile(name='fs_upload_files', started_at=time.time())

    runner.exec('mkdir -p {}/uploaded'.format(base_dir), cwd='/tmp')

    test_files = {
        'config.json': json.dumps(
            {'app': 'benchmark', 'version': '1.0', 'debug': False},
            indent=2,
        ).encode(),
        'small.py': generate_python_source('small', 'small'),
        'medium.py': generate_python_source('medium', 'medium'),
        'data.csv': generate_csv_data(rows=500),
        'readme.md': ('# Benchmark\n\n'
                       'This is a test file for filesystem benchmarking.\n'
                       * 50).encode(),
    }

    uploaded = 0
    total_bytes = 0
    for name, content in test_files.items():
        remote_path = '{}/uploaded/{}'.format(base_dir, name)
        try:
            runner.upload_file_native(content, remote_path)
            uploaded += 1
            total_bytes += len(content)
        except Exception as e:
            print('    Upload failed for {}: {}'.format(name, e))

    step.ended_at = time.time()
    step.duration_s = step.ended_at - step.started_at
    step.success = uploaded == len(test_files)
    step.detail = '{}/{} files, {}KB'.format(
        uploaded, len(test_files), total_bytes // 1024)
    return step


def _step_download_files(runner, base_dir):
    """Download build artifacts and uploaded files via native FS API."""
    step = StepProfile(name='fs_download_files', started_at=time.time())

    # Get list of .pyc files
    list_result = runner.exec(
        'find {}/src -name "*.pyc" -type f'.format(base_dir),
        cwd=base_dir,
    )
    pyc_files = [
        f.strip() for f in list_result['result'].strip().split('\n')
        if f.strip()
    ]

    downloaded = 0
    total_bytes = 0

    # Download .pyc files (cap at 5)
    for pyc_path in pyc_files[:5]:
        try:
            content = runner.download_file_native(pyc_path)
            downloaded += 1
            total_bytes += len(content) if content else 0
        except Exception as e:
            print('    Download failed for {}: {}'.format(pyc_path, e))

    # Download uploaded files back
    for name in ['config.json', 'small.py', 'medium.py', 'data.csv']:
        remote_path = '{}/uploaded/{}'.format(base_dir, name)
        try:
            content = runner.download_file_native(remote_path)
            downloaded += 1
            total_bytes += len(content) if content else 0
        except Exception as e:
            print('    Download failed for {}: {}'.format(name, e))

    step.ended_at = time.time()
    step.duration_s = step.ended_at - step.started_at
    step.success = downloaded > 0
    step.detail = '{} files, {}KB'.format(downloaded, total_bytes // 1024)
    return step


def _step_pip_package_io(runner, base_dir):
    """Download pip wheels in sandbox, retrieve via FS API, upload back, verify."""
    step = StepProfile(name='fs_pip_package_io', started_at=time.time())

    pkg_dir = '{}/pip_wheels'.format(base_dir)
    restore_dir = '{}/pip_restored'.format(base_dir)
    runner.exec('mkdir -p {} {}'.format(pkg_dir, restore_dir), cwd='/tmp')

    packages = ['requests', 'pydantic', 'jinja2']
    pkg_details = []

    try:
        # Step A: download wheel files inside sandbox
        for pkg in packages:
            t0 = time.time()
            result = runner.exec(
                'pip download --no-deps -d {} {} 2>&1 | tail -2'.format(
                    pkg_dir, pkg),
                cwd=base_dir,
            )
            pkg_details.append({
                'package': pkg,
                'download_ok': result['exit_code'] == 0,
                'download_s': round(time.time() - t0, 2),
            })

        # Step B: list the wheel files
        ls_result = runner.exec(
            'ls -1 {}/*.whl 2>/dev/null || ls -1 {}/*.tar.gz 2>/dev/null || echo ""'.format(
                pkg_dir, pkg_dir),
            cwd=base_dir,
        )
        wheel_files = [f.strip() for f in ls_result['result'].strip().split('\n') if f.strip()]

        # Step C: retrieve each wheel via native FS download
        retrieved = 0
        total_down_bytes = 0
        local_wheels = {}
        for whl_path in wheel_files:
            t0 = time.time()
            try:
                content = runner.download_file_native(whl_path)
                if content and len(content) > 0:
                    retrieved += 1
                    total_down_bytes += len(content)
                    local_wheels[whl_path] = content
                    # find matching detail
                    for d in pkg_details:
                        if d['package'] in whl_path.lower():
                            d['retrieve_ok'] = True
                            d['retrieve_s'] = round(time.time() - t0, 2)
                            d['size_kb'] = len(content) // 1024
                            break
            except Exception as e:
                for d in pkg_details:
                    if d['package'] in whl_path.lower():
                        d['retrieve_ok'] = False
                        d['retrieve_error'] = str(e)[:100]
                        break

        # Step D: upload each wheel back to a different dir
        uploaded = 0
        total_up_bytes = 0
        for whl_path, content in local_wheels.items():
            filename = whl_path.split('/')[-1]
            dest = '{}/{}'.format(restore_dir, filename)
            t0 = time.time()
            try:
                runner.upload_file_native(content, dest)
                uploaded += 1
                total_up_bytes += len(content)
                for d in pkg_details:
                    if d['package'] in filename.lower():
                        d['upload_ok'] = True
                        d['upload_s'] = round(time.time() - t0, 2)
                        break
            except Exception as e:
                for d in pkg_details:
                    if d['package'] in filename.lower():
                        d['upload_ok'] = False
                        d['upload_error'] = str(e)[:100]
                        break

        # Step E: verify round-trip with Python hashlib (portable)
        verify_cmd = (
            "python3 -c \""
            "import hashlib, glob, os; "
            "dirs = ['{}', '{}']; "
            "results = []; "
            "[results.append(hashlib.md5(open(f,'rb').read()).hexdigest() + '  ' + f) "
            "for d in dirs for f in sorted(glob.glob(d + '/*'))]; "
            "print('\\n'.join(results))\""
        ).format(pkg_dir, restore_dir)
        verify_result = runner.exec(verify_cmd, cwd=base_dir)
        checksums = verify_result.get('result', '').strip()
        # parse checksum pairs: originals and restored should match
        hash_map = {}
        for line in checksums.split('\n'):
            parts = line.strip().split('  ', 1)
            if len(parts) == 2:
                h, path = parts
                fname = path.split('/')[-1]
                hash_map.setdefault(fname, []).append(h)
        integrity_ok = all(
            len(set(hashes)) == 1 for hashes in hash_map.values()
        ) if hash_map and len(hash_map) > 0 else False

        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = (
            retrieved == len(wheel_files) > 0
            and uploaded == retrieved
            and integrity_ok
        )
        step.detail = (
            '{} wheels: down {}KB, up {}KB, integrity={}'.format(
                len(wheel_files),
                total_down_bytes // 1024,
                total_up_bytes // 1024,
                'OK' if integrity_ok else 'FAIL')
        )

    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = str(e)[:200]

    return step


def _step_list_verify(runner, base_dir):
    """List directory contents and verify file counts."""
    step = StepProfile(name='fs_list_verify', started_at=time.time())

    # List via exec
    result = runner.exec(
        'find {} -type f | wc -l'.format(base_dir),
        cwd=base_dir,
    )
    exec_count = result['result'].strip()

    # Try native FS list
    native_detail = ''
    try:
        entries = runner.list_files_native('{}/src'.format(base_dir))
        native_count = len(entries) if entries else 0
        native_detail = ', native: {} entries'.format(native_count)
    except Exception:
        native_detail = ', native list N/A'

    step.ended_at = time.time()
    step.duration_s = step.ended_at - step.started_at
    step.success = int(exec_count) > 0 if exec_count.isdigit() else False
    step.detail = '{} files total{}'.format(exec_count, native_detail)
    return step


# ── Main Benchmark Function ────────────────────────────────────────

def run_filesystem_benchmark(runner, provider):
    """Execute the full filesystem benchmark suite.

    Args:
        runner: A DaytonaSandboxRunner or E2BSandboxRunner instance
                (must already have a sandbox created).
        provider: 'daytona' or 'e2b' (for path resolution).

    Returns:
        list[StepProfile]: Profiling data for each benchmark step.
    """
    if provider == 'daytona':
        base_dir = '/home/daytona/fs_bench'
    elif provider == 'blaxel':
        base_dir = '/blaxel/fs_bench'
    elif provider == 'modal':
        base_dir = '/root/fs_bench'
    else:
        base_dir = '/home/user/fs_bench'
    runner.exec('mkdir -p {}'.format(base_dir), cwd='/tmp')

    steps = []

    print('    [FS] Step 1/6: Code generation...')
    steps.append(_step_code_generation(runner, base_dir))
    print('    [FS]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [FS] Step 2/6: Build/compile...')
    steps.append(_step_build_compile(runner, base_dir))
    print('    [FS]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [FS] Step 3/6: Native file upload...')
    steps.append(_step_upload_files(runner, base_dir))
    print('    [FS]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [FS] Step 4/6: Native file download...')
    steps.append(_step_download_files(runner, base_dir))
    print('    [FS]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [FS] Step 5/6: Pip package store & retrieve...')
    steps.append(_step_pip_package_io(runner, base_dir))
    print('    [FS]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [FS] Step 6/6: List & verify...')
    steps.append(_step_list_verify(runner, base_dir))
    print('    [FS]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    return steps
