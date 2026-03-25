"""
Filesystem Benchmark for Sandbox Profiling.

Benchmarks filesystem operations that simulate an agent writing code,
building artifacts, and transferring files via native FS APIs.
Includes large file I/O (up to 100MB), SQLite database operations,
and pip package install/import tests.
Returns a list of StepProfile objects compatible with the parallel profiler.
"""
import base64
import json
import os
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


def _step_large_file_io(runner, base_dir):
    """Test large file I/O: in-sandbox generation + native API download/upload.

    Tests 10MB, 50MB, 100MB files. For each size:
    - Generate random data in-sandbox and measure write speed
    - Read back and checksum in-sandbox to measure read speed
    - Download via native FS API (measures API download throughput)
    - Upload 10MB via native FS API (larger sizes skip upload to avoid timeouts)
    """
    step = StepProfile(name='fs_large_file_io', started_at=time.time())

    runner.exec('mkdir -p {}/large_files'.format(base_dir), cwd='/tmp')

    sizes_mb = [10, 50, 100]
    results = []

    for size_mb in sizes_mb:
        size_bytes = size_mb * 1024 * 1024
        r = {'size_mb': size_mb, 'ok': True}

        # Generate file in-sandbox and measure write speed
        gen_cmd = (
            "python3 -c \""
            "import os, hashlib, time; "
            "data = os.urandom({size}); "
            "t0 = time.time(); "
            "f = open('{base}/large_files/test_{mb}mb.bin', 'wb'); "
            "f.write(data); f.flush(); os.fsync(f.fileno()); f.close(); "
            "write_s = time.time() - t0; "
            "t0 = time.time(); "
            "h = hashlib.md5(open('{base}/large_files/test_{mb}mb.bin', 'rb').read()).hexdigest(); "
            "read_s = time.time() - t0; "
            "print(h + ' ' + str(round(write_s, 3)) + ' ' + str(round(read_s, 3)))\""
        ).format(size=size_bytes, base=base_dir, mb=size_mb)

        gen_result = runner.exec(gen_cmd, cwd=base_dir, timeout=120)
        parts = gen_result.get('result', '').strip().split()
        if len(parts) == 3:
            original_hash, write_s, read_s = parts[0], float(parts[1]), float(parts[2])
            r['write_s'] = write_s
            r['read_s'] = read_s
            r['write_mbps'] = round(size_mb / write_s, 1) if write_s > 0 else 0
            r['read_mbps'] = round(size_mb / read_s, 1) if read_s > 0 else 0
        else:
            r['ok'] = False
            r['error'] = 'gen failed: {}'.format(gen_result.get('result', '')[:80])
            results.append(r)
            continue

        # Native API download
        remote_path = '{}/large_files/test_{}mb.bin'.format(base_dir, size_mb)
        t0 = time.time()
        try:
            content = runner.download_file_native(remote_path)
            r['download_s'] = round(time.time() - t0, 3)
            r['download_size'] = len(content) if content else 0
            r['api_down_mbps'] = round(size_mb / r['download_s'], 1) if r['download_s'] > 0 else 0
        except Exception as e:
            r['download_s'] = round(time.time() - t0, 3)
            r['api_down_mbps'] = 0
            r['download_error'] = str(e)[:80]
            content = None

        # Native API upload (only for 10MB to avoid timeouts/413s)
        if size_mb <= 10 and content:
            restore_path = '{}/large_files/restored_{}mb.bin'.format(base_dir, size_mb)
            t0 = time.time()
            try:
                runner.upload_file_native(content, restore_path)
                r['upload_s'] = round(time.time() - t0, 3)
                r['api_up_mbps'] = round(size_mb / r['upload_s'], 1) if r['upload_s'] > 0 else 0

                # Verify round-trip integrity
                verify_cmd = (
                    "python3 -c \""
                    "import hashlib; "
                    "h = hashlib.md5(open('{}', 'rb').read()).hexdigest(); "
                    "print(h)\""
                ).format(restore_path)
                verify_result = runner.exec(verify_cmd, cwd=base_dir, timeout=60)
                restored_hash = verify_result.get('result', '').strip()
                r['integrity'] = (original_hash == restored_hash)
                if not r['integrity']:
                    r['ok'] = False
            except Exception as e:
                r['upload_s'] = round(time.time() - t0, 3)
                r['upload_error'] = str(e)[:80]

        results.append(r)

    step.ended_at = time.time()
    step.duration_s = step.ended_at - step.started_at
    step.success = all(r.get('ok', False) for r in results)
    details = []
    for r in results:
        parts = ['{}MB'.format(r['size_mb'])]
        if 'write_s' in r:
            parts.append('write {:.1f}s ({:.0f}MB/s)'.format(r['write_s'], r.get('write_mbps', 0)))
            parts.append('read {:.1f}s ({:.0f}MB/s)'.format(r['read_s'], r.get('read_mbps', 0)))
        if 'download_s' in r:
            parts.append('api_down {:.1f}s ({:.0f}MB/s)'.format(r['download_s'], r.get('api_down_mbps', 0)))
        if 'download_error' in r:
            parts.append('api_down FAIL')
        if 'upload_s' in r:
            parts.append('api_up {:.1f}s ({:.0f}MB/s)'.format(r['upload_s'], r.get('api_up_mbps', 0)))
        if 'integrity' in r:
            parts.append('integrity={}'.format('OK' if r['integrity'] else 'FAIL'))
        if 'error' in r:
            parts.append('ERR: {}'.format(r['error']))
        details.append(': '.join([parts[0], ', '.join(parts[1:])]))
    step.detail = '; '.join(details)
    return step


def _step_sqlite_operations(runner, base_dir):
    """Create SQLite DB, insert rows, query, copy DB file, and verify."""
    step = StepProfile(name='fs_sqlite_ops', started_at=time.time())

    db_path = '{}/bench.db'.format(base_dir)
    copy_path = '{}/bench_copy.db'.format(base_dir)

    # Build the script without .format() to avoid escaping issues
    script_lines = [
        'import sqlite3, time, json, os, shutil, sys',
        '',
        'db_path = sys.argv[1]',
        'copy_path = sys.argv[2]',
        'results = {}',
        '',
        '# Create and populate',
        't0 = time.time()',
        'conn = sqlite3.connect(db_path)',
        'c = conn.cursor()',
        'c.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, name TEXT, ts REAL, payload TEXT)")',
        'c.execute("CREATE TABLE IF NOT EXISTS metrics (id INTEGER PRIMARY KEY, event_id INTEGER, value REAL, label TEXT)")',
        '',
        'batch = []',
        'for i in range(10000):',
        '    batch.append((i, "event_" + str(i), time.time() + i * 0.001, json.dumps({"idx": i, "data": "x" * 100})))',
        'c.executemany("INSERT INTO events VALUES (?, ?, ?, ?)", batch)',
        '',
        'mbatch = []',
        'for i in range(50000):',
        '    mbatch.append((i, i % 10000, i * 3.14159, "label_" + str(i % 100)))',
        'c.executemany("INSERT INTO metrics VALUES (?, ?, ?, ?)", mbatch)',
        '',
        'c.execute("CREATE INDEX IF NOT EXISTS idx_events_name ON events(name)")',
        'c.execute("CREATE INDEX IF NOT EXISTS idx_metrics_event ON metrics(event_id)")',
        'c.execute("CREATE INDEX IF NOT EXISTS idx_metrics_label ON metrics(label)")',
        'conn.commit()',
        'results["insert_s"] = round(time.time() - t0, 3)',
        '',
        '# Queries',
        't0 = time.time()',
        'c.execute("SELECT COUNT(*) FROM events")',
        'event_count = c.fetchone()[0]',
        'c.execute("SELECT COUNT(*) FROM metrics")',
        'metric_count = c.fetchone()[0]',
        'c.execute("SELECT e.name, SUM(m.value), COUNT(*) FROM events e JOIN metrics m ON e.id = m.event_id GROUP BY e.name ORDER BY SUM(m.value) DESC LIMIT 10")',
        'top10 = c.fetchall()',
        'c.execute("SELECT label, AVG(value), MIN(value), MAX(value) FROM metrics GROUP BY label")',
        'agg = c.fetchall()',
        'c.execute("SELECT * FROM events WHERE name LIKE \'event_99%\'")',
        'filtered = c.fetchall()',
        'results["query_s"] = round(time.time() - t0, 3)',
        'results["event_count"] = event_count',
        'results["metric_count"] = metric_count',
        'results["top10_count"] = len(top10)',
        'results["agg_count"] = len(agg)',
        'results["filtered_count"] = len(filtered)',
        '',
        '# Copy DB file and verify (more reliable than iterdump)',
        'conn.close()',
        'results["db_size_kb"] = os.path.getsize(db_path) // 1024',
        '',
        't0 = time.time()',
        'shutil.copy2(db_path, copy_path)',
        'results["copy_s"] = round(time.time() - t0, 3)',
        '',
        '# Verify copied DB',
        't0 = time.time()',
        'conn2 = sqlite3.connect(copy_path)',
        'c2 = conn2.cursor()',
        'c2.execute("SELECT COUNT(*) FROM events")',
        'results["copy_events"] = c2.fetchone()[0]',
        'c2.execute("SELECT COUNT(*) FROM metrics")',
        'results["copy_metrics"] = c2.fetchone()[0]',
        'c2.execute("SELECT e.name, SUM(m.value) FROM events e JOIN metrics m ON e.id = m.event_id GROUP BY e.name LIMIT 5")',
        'c2.fetchall()',
        'conn2.close()',
        'results["verify_s"] = round(time.time() - t0, 3)',
        '',
        'print(json.dumps(results))',
    ]
    script_content = '\n'.join(script_lines)

    # Write the script to sandbox and execute
    script_path = '{}/sqlite_bench.py'.format(base_dir)
    try:
        runner.upload_file_native(script_content.encode('utf-8'), script_path)
    except Exception:
        encoded = base64.b64encode(script_content.encode('utf-8')).decode()
        runner.exec(
            "python3 -c \"import base64; open('{}', 'wb').write(base64.b64decode('{}'))\"".format(
                script_path, encoded),
            cwd=base_dir,
        )

    result = runner.exec(
        'python3 {} {} {}'.format(script_path, db_path, copy_path),
        cwd=base_dir,
        timeout=120,
    )

    try:
        output = result.get('result', '').strip()
        # Extract the last line (JSON) in case there's other output
        json_line = [l for l in output.split('\n') if l.strip().startswith('{')][-1]
        data = json.loads(json_line)
        integrity_ok = (
            data.get('copy_events') == data.get('event_count', 0)
            and data.get('copy_metrics') == data.get('metric_count', 0)
        )
        step.success = integrity_ok and data.get('event_count', 0) == 10000
        step.detail = (
            '10K events + 50K metrics: insert {insert_s}s, query {query_s}s, '
            'copy {copy_s}s, verify {verify_s}s, '
            'db={db_size_kb}KB, integrity={integrity}'.format(
                integrity='OK' if integrity_ok else 'FAIL',
                **data,
            )
        )
    except Exception as e:
        step.success = False
        output = result.get('result', '')
        step.detail = 'Parse error: {} | output: {}'.format(str(e)[:100], output[:200])

    step.ended_at = time.time()
    step.duration_s = step.ended_at - step.started_at
    return step


def _step_pip_install_import(runner, base_dir):
    """Install pip packages and verify they import and work correctly."""
    step = StepProfile(name='fs_pip_install_import', started_at=time.time())

    packages = [
        {'name': 'requests', 'verify': "import requests; print(requests.__version__)"},
        {'name': 'pyyaml', 'verify': "import yaml; print(yaml.dump({'test': [1,2,3]}))"},
        {'name': 'numpy', 'verify': "import numpy as np; a = np.random.rand(1000,1000); print(f'det={np.linalg.det(a[:3,:3]):.4f}, shape={a.shape}')"},
    ]

    results = []
    for pkg in packages:
        t0 = time.time()
        install_result = runner.exec(
            'pip install --quiet {} 2>&1 | tail -3'.format(pkg['name']),
            cwd=base_dir,
            timeout=120,
        )
        install_s = round(time.time() - t0, 3)

        t0 = time.time()
        verify_result = runner.exec(
            'python3 -c "{}"'.format(pkg['verify']),
            cwd=base_dir,
            timeout=30,
        )
        verify_s = round(time.time() - t0, 3)
        verify_ok = verify_result.get('exit_code', 1) == 0
        verify_output = verify_result.get('result', '').strip()[:80]

        results.append({
            'name': pkg['name'],
            'install_s': install_s,
            'verify_s': verify_s,
            'ok': verify_ok,
            'output': verify_output,
        })

    step.ended_at = time.time()
    step.duration_s = step.ended_at - step.started_at
    step.success = all(r['ok'] for r in results)
    details = []
    for r in results:
        status = 'OK' if r['ok'] else 'FAIL'
        details.append('{}: install {:.1f}s, verify {:.2f}s [{}]'.format(
            r['name'], r['install_s'], r['verify_s'], status))
    step.detail = '; '.join(details)
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
    if provider in ('daytona', 'modal', 'tensorlake'):
        base_dir = '/root/fs_bench'
    elif provider == 'blaxel':
        base_dir = '/blaxel/fs_bench'
    else:
        base_dir = '/home/user/fs_bench'
    runner.exec('mkdir -p {}'.format(base_dir), cwd='/tmp')

    steps = []
    total_steps = 9

    print('    [FS] Step 1/{}: Code generation...'.format(total_steps))
    steps.append(_step_code_generation(runner, base_dir))
    print('    [FS]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [FS] Step 2/{}: Build/compile...'.format(total_steps))
    steps.append(_step_build_compile(runner, base_dir))
    print('    [FS]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [FS] Step 3/{}: Native file upload...'.format(total_steps))
    steps.append(_step_upload_files(runner, base_dir))
    print('    [FS]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [FS] Step 4/{}: Native file download...'.format(total_steps))
    steps.append(_step_download_files(runner, base_dir))
    print('    [FS]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [FS] Step 5/{}: Pip package store & retrieve...'.format(total_steps))
    steps.append(_step_pip_package_io(runner, base_dir))
    print('    [FS]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [FS] Step 6/{}: Large file I/O (10/50/100MB)...'.format(total_steps))
    steps.append(_step_large_file_io(runner, base_dir))
    print('    [FS]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [FS] Step 7/{}: SQLite operations...'.format(total_steps))
    steps.append(_step_sqlite_operations(runner, base_dir))
    print('    [FS]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [FS] Step 8/{}: Pip install & import...'.format(total_steps))
    steps.append(_step_pip_install_import(runner, base_dir))
    print('    [FS]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [FS] Step 9/{}: List & verify...'.format(total_steps))
    steps.append(_step_list_verify(runner, base_dir))
    print('    [FS]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    return steps
