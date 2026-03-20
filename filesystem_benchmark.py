"""
Filesystem Benchmark for Sandbox Profiling.

Benchmarks filesystem operations that simulate an agent writing code,
building artifacts, and transferring files via native FS APIs.
Returns a list of StepProfile objects compatible with the parallel profiler.
"""
import base64
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

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

    packages = ['numpy', 'pandas', 'scipy']
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


def _step_overwrite_speed(runner, base_dir):
    """Measure rapid consecutive overwrites of the same file."""
    step = StepProfile(name='fs_overwrite_speed', started_at=time.time())

    target_path = '{}/overwrite_test.py'.format(base_dir)
    num_overwrites = 20
    latencies = []

    try:
        for i in range(num_overwrites):
            content = '# Overwrite iteration {}\n'.format(i)
            content += 'def process(data):\n'
            content += '    """Iteration {} of processing."""\n'.format(i)
            content += '    result = []\n'
            content += '    for item in data:\n'
            content += '        result.append(item * {})\n'.format(i + 1)
            content += '    return result\n'
            content += '\n' * (i % 5)  # slight size variation
            content_bytes = content.encode('utf-8')

            t0 = time.time()
            runner.upload_file_native(content_bytes, target_path)
            latency = time.time() - t0
            latencies.append(latency)

        min_lat = min(latencies)
        avg_lat = sum(latencies) / len(latencies)
        max_lat = max(latencies)

        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = len(latencies) == num_overwrites
        step.detail = '{} overwrites: min={:.3f}s avg={:.3f}s max={:.3f}s'.format(
            num_overwrites, min_lat, avg_lat, max_lat)
    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = str(e)[:200]

    return step


def _step_large_file_scaling(runner, base_dir):
    """Upload/download at 1MB, 5MB, 10MB, 25MB and report throughput."""
    step = StepProfile(name='fs_large_file_scaling', started_at=time.time())

    sizes_mb = [1, 5, 10, 25]
    results = []

    try:
        runner.exec('mkdir -p {}/large_files'.format(base_dir), cwd='/tmp')

        for size_mb in sizes_mb:
            size_bytes = size_mb * 1024 * 1024
            # Generate deterministic data (repeating pattern)
            pattern = 'X' * 1024  # 1KB pattern
            data = (pattern * (size_bytes // 1024)).encode('utf-8')[:size_bytes]

            remote_path = '{}/large_files/test_{}mb.bin'.format(base_dir, size_mb)

            # Upload
            t0 = time.time()
            runner.upload_file_native(data, remote_path)
            upload_s = time.time() - t0
            upload_mbps = size_mb / upload_s if upload_s > 0 else 0

            # Download
            t0 = time.time()
            downloaded = runner.download_file_native(remote_path)
            download_s = time.time() - t0
            download_mbps = size_mb / download_s if download_s > 0 else 0

            ok = downloaded is not None and len(downloaded) == size_bytes
            results.append({
                'size_mb': size_mb,
                'upload_mbps': round(upload_mbps, 1),
                'download_mbps': round(download_mbps, 1),
                'ok': ok,
            })

        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = all(r['ok'] for r in results)
        detail_parts = []
        for r in results:
            detail_parts.append('{}MB: up={:.1f}MB/s down={:.1f}MB/s'.format(
                r['size_mb'], r['upload_mbps'], r['download_mbps']))
        step.detail = '; '.join(detail_parts)
    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = str(e)[:200]

    return step


def _step_concurrent_io(runner, base_dir):
    """Upload/download 10 files concurrently and compare to sequential estimate."""
    step = StepProfile(name='fs_concurrent_io', started_at=time.time())

    num_files = 10
    file_size = 1024  # 1KB each

    try:
        runner.exec('mkdir -p {}/concurrent_test'.format(base_dir), cwd='/tmp')

        # Generate test files
        files = {}
        for i in range(num_files):
            name = 'concurrent_{}.txt'.format(i)
            content = 'File {} content: {}\n'.format(i, 'A' * (file_size - 30))
            files[name] = content.encode('utf-8')

        # Concurrent upload
        upload_times = []

        def upload_one(name_content):
            name, content = name_content
            remote = '{}/concurrent_test/{}'.format(base_dir, name)
            t0 = time.time()
            runner.upload_file_native(content, remote)
            return time.time() - t0

        t_upload_start = time.time()
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(upload_one, item) for item in files.items()]
            for f in as_completed(futures):
                upload_times.append(f.result())
        concurrent_upload_s = time.time() - t_upload_start
        sequential_upload_est = sum(upload_times)

        # Concurrent download
        download_times = []
        downloaded_sizes = []

        def download_one(name):
            remote = '{}/concurrent_test/{}'.format(base_dir, name)
            t0 = time.time()
            content = runner.download_file_native(remote)
            elapsed = time.time() - t0
            return elapsed, len(content) if content else 0

        t_download_start = time.time()
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(download_one, name) for name in files.keys()]
            for f in as_completed(futures):
                elapsed, size = f.result()
                download_times.append(elapsed)
                downloaded_sizes.append(size)
        concurrent_download_s = time.time() - t_download_start
        sequential_download_est = sum(download_times)

        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = len(downloaded_sizes) == num_files and all(s > 0 for s in downloaded_sizes)
        step.detail = (
            '{} files: upload wall={:.2f}s (seq_est={:.2f}s), '
            'download wall={:.2f}s (seq_est={:.2f}s)'.format(
                num_files, concurrent_upload_s, sequential_upload_est,
                concurrent_download_s, sequential_download_est))
    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = str(e)[:200]

    return step


def _step_binary_integrity(runner, base_dir):
    """Upload 5MB of random bytes, download back, verify byte-for-byte equality."""
    step = StepProfile(name='fs_binary_integrity', started_at=time.time())

    size_bytes = 5 * 1024 * 1024  # 5MB

    try:
        # Generate random bytes
        original_data = os.urandom(size_bytes)
        remote_path = '{}/binary_test.bin'.format(base_dir)

        # Upload
        t_up = time.time()
        runner.upload_file_native(original_data, remote_path)
        upload_s = time.time() - t_up

        # Download
        t_down = time.time()
        downloaded_data = runner.download_file_native(remote_path)
        download_s = time.time() - t_down

        # Verify
        if downloaded_data is None:
            integrity_ok = False
            mismatch_detail = 'download returned None'
        elif len(downloaded_data) != len(original_data):
            integrity_ok = False
            mismatch_detail = 'size mismatch: sent {} got {}'.format(
                len(original_data), len(downloaded_data))
        elif downloaded_data == original_data:
            integrity_ok = True
            mismatch_detail = 'OK'
        else:
            integrity_ok = False
            # Find first mismatch position
            for pos in range(min(len(original_data), len(downloaded_data))):
                if original_data[pos] != downloaded_data[pos]:
                    mismatch_detail = 'first mismatch at byte {}'.format(pos)
                    break

        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = integrity_ok
        step.detail = '5MB binary: upload={:.2f}s download={:.2f}s integrity={}'.format(
            upload_s, download_s, mismatch_detail)
    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = str(e)[:200]

    return step


def _step_deep_tree(runner, base_dir):
    """Create a 3-level directory tree with ~30 files, download from various depths."""
    step = StepProfile(name='fs_deep_tree', started_at=time.time())

    tree_base = '{}/deep_tree'.format(base_dir)

    try:
        # Create directory tree and files via exec
        dirs = [
            '{}/level1_a'.format(tree_base),
            '{}/level1_a/level2_a'.format(tree_base),
            '{}/level1_a/level2_a/level3_a'.format(tree_base),
            '{}/level1_a/level2_b'.format(tree_base),
            '{}/level1_a/level2_b/level3_b'.format(tree_base),
            '{}/level1_b'.format(tree_base),
            '{}/level1_b/level2_c'.format(tree_base),
            '{}/level1_b/level2_c/level3_c'.format(tree_base),
            '{}/level1_b/level2_d'.format(tree_base),
            '{}/level1_c'.format(tree_base),
        ]
        runner.exec('mkdir -p {}'.format(' '.join(dirs)), cwd='/tmp')

        # Create files at various depths
        file_paths = []
        create_cmds = []
        file_idx = 0
        for d in dirs:
            for j in range(3):
                fname = 'file_{}_{}.txt'.format(file_idx, j)
                fpath = '{}/{}'.format(d, fname)
                file_paths.append(fpath)
                create_cmds.append(
                    "echo 'Content of {} at depth in {}' > {}".format(
                        fname, d.split('deep_tree/')[-1] if 'deep_tree/' in d else d,
                        fpath))
            file_idx += 1

        # Execute file creation in batches
        batch_size = 10
        for i in range(0, len(create_cmds), batch_size):
            batch = ' && '.join(create_cmds[i:i + batch_size])
            runner.exec(batch, cwd='/tmp')

        # Verify file count
        count_result = runner.exec(
            'find {} -type f | wc -l'.format(tree_base), cwd='/tmp')
        total_files = count_result['result'].strip()

        # Download files from various depths via native API
        downloaded = 0
        total_bytes = 0
        # Sample files from different depths
        sample_indices = [0, 3, 6, 9, 12, 15, 18, 21, 24, 27]
        for idx in sample_indices:
            if idx < len(file_paths):
                try:
                    content = runner.download_file_native(file_paths[idx])
                    if content and len(content) > 0:
                        downloaded += 1
                        total_bytes += len(content)
                except Exception:
                    pass

        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = downloaded >= 8  # allow some tolerance
        step.detail = '{} files created, {}/{} downloaded ({}B), 3-level tree'.format(
            total_files, downloaded, len(sample_indices), total_bytes)
    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = str(e)[:200]

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
        base_dir = '/root/fs_bench'
    elif provider == 'blaxel':
        base_dir = '/blaxel/fs_bench'
    elif provider == 'modal':
        base_dir = '/root/fs_bench'
    else:
        base_dir = '/home/user/fs_bench'
    runner.exec('mkdir -p {}'.format(base_dir), cwd='/tmp')

    steps = []

    print('    [FS] Step 1/11: Code generation...')
    steps.append(_step_code_generation(runner, base_dir))
    print('    [FS]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [FS] Step 2/11: Build/compile...')
    steps.append(_step_build_compile(runner, base_dir))
    print('    [FS]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [FS] Step 3/11: Native file upload...')
    steps.append(_step_upload_files(runner, base_dir))
    print('    [FS]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [FS] Step 4/11: Native file download...')
    steps.append(_step_download_files(runner, base_dir))
    print('    [FS]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [FS] Step 5/11: Pip package store & retrieve...')
    steps.append(_step_pip_package_io(runner, base_dir))
    print('    [FS]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [FS] Step 6/11: List & verify...')
    steps.append(_step_list_verify(runner, base_dir))
    print('    [FS]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [FS] Step 7/11: Rapid file overwrites...')
    steps.append(_step_overwrite_speed(runner, base_dir))
    print('    [FS]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [FS] Step 8/11: Large file scaling...')
    steps.append(_step_large_file_scaling(runner, base_dir))
    print('    [FS]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [FS] Step 9/11: Concurrent I/O...')
    steps.append(_step_concurrent_io(runner, base_dir))
    print('    [FS]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [FS] Step 10/11: Binary integrity...')
    steps.append(_step_binary_integrity(runner, base_dir))
    print('    [FS]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [FS] Step 11/11: Deep directory tree...')
    steps.append(_step_deep_tree(runner, base_dir))
    print('    [FS]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    return steps
