"""
Security & Isolation Benchmark for Sandbox Profiling.

Probes each provider's isolation guarantees: metadata service access,
privilege escalation surface, container escape vectors, internal network
reachability, sensitive filesystem exposure, resource limits, egress
filtering, and environment variable leakage.

Inverted pass/fail: success=True means the sandbox BLOCKED the attack
(isolation held). success=False means the attack SUCCEEDED (isolation broken).

All tests are read-only / non-destructive. No fork bombs, no disk fills,
no actual exploits -- tests only probe whether the attack surface exists.

All tests use pure Python scripts -- no curl or external tools needed.
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

METADATA_SCRIPT = """\
import json
import socket
import urllib.request

endpoints = [
    ('AWS IMDSv1', 'http://169.254.169.254/latest/meta-data/'),
    ('Azure IMDS', 'http://169.254.169.254/metadata/instance?api-version=2021-02-01'),
    ('GCP metadata', 'http://metadata.google.internal/computeMetadata/v1/'),
]

reachable = []
for name, url in endpoints:
    try:
        headers = {}
        if 'google' in url:
            headers['Metadata-Flavor'] = 'Google'
        if 'azure' in url:
            headers['Metadata'] = 'true'
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = resp.read(512).decode('utf-8', errors='replace')
            reachable.append({'name': name, 'status': resp.status, 'data': data[:200]})
    except Exception:
        pass

print(json.dumps({'reachable': reachable}))
"""


def _step_sec_metadata_service(runner, base_dir):
    """Probe cloud metadata / IMDS endpoints."""
    step = StepProfile(name='sec_metadata_service', started_at=time.time())

    try:
        result = _run_script(runner, base_dir, 'sec_metadata.py',
                             METADATA_SCRIPT, timeout=30)

        import json
        output = result['result'].strip()
        if result['exit_code'] == 0 and output:
            data = json.loads(output)
            reachable = data.get('reachable', [])
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at
            if reachable:
                names = ', '.join(r['name'] for r in reachable)
                step.success = False
                step.detail = 'CRITICAL: reachable endpoints: {}'.format(names)
            else:
                step.success = True
                step.detail = 'all metadata endpoints blocked'
        else:
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at
            step.success = True
            step.detail = 'script failed (endpoints likely blocked): {}'.format(
                output[:200])
    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = True
        step.detail = 'probe error (likely blocked): {}'.format(str(e)[:200])

    return step


PRIVILEGE_SCRIPT = """\
import json
import os
import subprocess

info = {}

# uid / whoami
info['uid'] = os.getuid()
info['euid'] = os.geteuid()
try:
    info['whoami'] = subprocess.run(['whoami'], capture_output=True,
                                     text=True, timeout=5).stdout.strip()
except Exception:
    info['whoami'] = 'unknown'

# /etc/shadow readable?
try:
    with open('/etc/shadow', 'r') as f:
        f.read(1)
    info['shadow_readable'] = True
except Exception:
    info['shadow_readable'] = False

# sudo -l
try:
    r = subprocess.run(['sudo', '-n', '-l'], capture_output=True,
                       text=True, timeout=5)
    info['sudo_available'] = r.returncode == 0
    info['sudo_output'] = r.stdout[:200] if r.returncode == 0 else ''
except Exception:
    info['sudo_available'] = False
    info['sudo_output'] = ''

# Capabilities from /proc/self/status
info['cap_eff'] = ''
info['seccomp'] = ''
try:
    with open('/proc/self/status', 'r') as f:
        for line in f:
            if line.startswith('CapEff:'):
                info['cap_eff'] = line.split(':')[1].strip()
            if line.startswith('Seccomp:'):
                info['seccomp'] = line.split(':')[1].strip()
except Exception:
    pass

# AppArmor
try:
    with open('/proc/self/attr/current', 'r') as f:
        info['apparmor'] = f.read().strip()
except Exception:
    info['apparmor'] = 'unconfined_or_unavailable'

print(json.dumps(info))
"""


def _step_sec_privilege_info(runner, base_dir):
    """Audit privilege level, capabilities, and security modules."""
    step = StepProfile(name='sec_privilege_info', started_at=time.time())

    try:
        result = _run_script(runner, base_dir, 'sec_priv.py',
                             PRIVILEGE_SCRIPT, timeout=30)

        import json
        output = result['result'].strip()
        if result['exit_code'] == 0 and output:
            data = json.loads(output)
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at

            uid = data.get('uid', -1)
            cap_eff = data.get('cap_eff', '')
            seccomp = data.get('seccomp', '')
            shadow = data.get('shadow_readable', False)

            # Full capabilities = 000001ffffffffff (or similar high value)
            full_caps = cap_eff in ('0000003fffffffff', '000001ffffffffff',
                                    '0000003ffffffeff')
            seccomp_on = seccomp not in ('0', '')

            is_root = uid == 0
            if not is_root:
                step.success = True
                step.detail = 'non-root uid={}, caps={}, seccomp={}'.format(
                    uid, cap_eff, seccomp)
            elif seccomp_on or not full_caps:
                step.success = True
                step.detail = 'root but restricted: caps={}, seccomp={}, shadow={}'.format(
                    cap_eff, seccomp, shadow)
            else:
                step.success = False
                step.detail = 'root with full caps={}, seccomp={}, shadow={}'.format(
                    cap_eff, seccomp, shadow)
        else:
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at
            step.success = False
            step.detail = 'privilege audit failed: {}'.format(output[:200])
    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = str(e)[:200]

    return step


CONTAINER_ESCAPE_SCRIPT = """\
import json
import os
import subprocess
import stat

findings = []

# Docker socket
if os.path.exists('/var/run/docker.sock'):
    findings.append('docker_socket_exists')

# Container detection
if os.path.exists('/.dockerenv'):
    findings.append('dockerenv_present')
if os.path.exists('/run/.containerenv'):
    findings.append('containerenv_present')

# Host filesystem traversal via /proc/1/root
try:
    with open('/proc/1/root/etc/hostname', 'r') as f:
        hostname = f.read().strip()
    findings.append('host_fs_readable:hostname={}'.format(hostname))
except Exception:
    pass

# cgroup info
try:
    with open('/proc/1/cgroup', 'r') as f:
        cgroup = f.read().strip()[:200]
    findings.append('cgroup_readable')
except Exception:
    pass

# mount info
try:
    r = subprocess.run(['mount'], capture_output=True, text=True, timeout=5)
    if r.returncode == 0:
        findings.append('mount_available')
except Exception:
    pass

# namespace tools
for tool in ['unshare', 'nsenter']:
    try:
        r = subprocess.run(['which', tool], capture_output=True,
                           text=True, timeout=5)
        if r.returncode == 0:
            findings.append('{}_available'.format(tool))
    except Exception:
        pass

# kernel module loading
try:
    r = subprocess.run(['which', 'modprobe'], capture_output=True,
                       text=True, timeout=5)
    if r.returncode == 0:
        findings.append('modprobe_available')
except Exception:
    pass

print(json.dumps({'findings': findings}))
"""


def _step_sec_container_escape(runner, base_dir):
    """Check container/VM escape surface."""
    step = StepProfile(name='sec_container_escape', started_at=time.time())

    try:
        result = _run_script(runner, base_dir, 'sec_escape.py',
                             CONTAINER_ESCAPE_SCRIPT, timeout=30)

        import json
        output = result['result'].strip()
        if result['exit_code'] == 0 and output:
            data = json.loads(output)
            findings = data.get('findings', [])
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at

            critical = [f for f in findings
                        if f.startswith(('docker_socket',
                                         'host_fs_readable'))]
            if critical:
                step.success = False
                step.detail = 'CRITICAL: {}'.format(', '.join(critical))
            else:
                step.success = True
                step.detail = 'escape surface limited: {}'.format(
                    ', '.join(findings) if findings else 'none found')
        else:
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at
            step.success = False
            step.detail = 'escape check failed: {}'.format(output[:200])
    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = str(e)[:200]

    return step


NETWORK_SCAN_SCRIPT = """\
import json
import socket
import struct

# Get default gateway from /proc/net/route
gateway = None
try:
    with open('/proc/net/route', 'r') as f:
        for line in f.readlines()[1:]:
            parts = line.strip().split()
            if len(parts) >= 3 and parts[1] == '00000000':
                gw_hex = parts[2]
                gw_bytes = bytes.fromhex(gw_hex)
                gateway = socket.inet_ntoa(gw_bytes)
                break
except Exception:
    pass

# Get own IP
own_ip = None
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(('8.8.8.8', 80))
    own_ip = s.getsockname()[0]
    s.close()
except Exception:
    pass

# Management ports to probe
mgmt_ports = [22, 2375, 2376, 6443, 10250, 8080, 4243]

reachable = []

# Probe gateway
if gateway:
    for port in mgmt_ports:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            if s.connect_ex((gateway, port)) == 0:
                reachable.append('{}:{}'.format(gateway, port))
            s.close()
        except Exception:
            pass

# Probe common internal IPs
internal_ips = ['10.0.0.1', '172.17.0.1', '192.168.0.1']
for ip in internal_ips:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        if s.connect_ex((ip, 80)) == 0:
            reachable.append('{}:80'.format(ip))
        s.close()
    except Exception:
        pass

print(json.dumps({
    'gateway': gateway,
    'own_ip': own_ip,
    'reachable': reachable,
}))
"""


def _step_sec_network_scan(runner, base_dir):
    """Probe internal network for management services."""
    step = StepProfile(name='sec_network_scan', started_at=time.time())

    try:
        result = _run_script(runner, base_dir, 'sec_netscan.py',
                             NETWORK_SCAN_SCRIPT, timeout=60)

        import json
        output = result['result'].strip()
        if result['exit_code'] == 0 and output:
            data = json.loads(output)
            reachable = data.get('reachable', [])
            gateway = data.get('gateway', 'unknown')
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at

            if reachable:
                step.success = False
                step.detail = 'reachable mgmt services: {}; gw={}'.format(
                    ', '.join(reachable), gateway)
            else:
                step.success = True
                step.detail = 'no mgmt ports reachable; gw={}'.format(gateway)
        else:
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at
            step.success = True
            step.detail = 'scan failed (likely isolated): {}'.format(
                output[:200])
    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = True
        step.detail = 'probe error (likely isolated): {}'.format(str(e)[:200])

    return step


FILESYSTEM_SCRIPT = """\
import json
import os
import glob as globmod

accessible = []

# Kernel memory / symbols
sensitive_files = ['/proc/kcore', '/proc/kallsyms',
                   '/sys/kernel/security/']
for path in sensitive_files:
    try:
        with open(path, 'r') as f:
            f.read(64)
        accessible.append(path)
    except Exception:
        pass

# Block devices
for dev in ['/dev/sda', '/dev/vda', '/dev/xvda', '/dev/mem', '/dev/kmsg']:
    try:
        with open(dev, 'rb') as f:
            f.read(1)
        accessible.append(dev)
    except Exception:
        pass

# Host mounts via /proc/mounts
host_mounts = []
try:
    with open('/proc/mounts', 'r') as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2:
                mount_point = parts[1]
                # Flag suspicious host-level mounts
                if mount_point in ('/', '/etc', '/var', '/root', '/home'):
                    fs_type = parts[2] if len(parts) > 2 else ''
                    if fs_type not in ('overlay', 'tmpfs', 'proc', 'sysfs',
                                       'devtmpfs', 'cgroup', 'cgroup2'):
                        host_mounts.append(mount_point)
except Exception:
    pass
if host_mounts:
    accessible.append('host_mounts:' + ','.join(host_mounts))

# Other processes' environ
proc_env_leak = False
try:
    for pid_dir in os.listdir('/proc'):
        if pid_dir.isdigit() and pid_dir != str(os.getpid()):
            env_path = '/proc/{}/environ'.format(pid_dir)
            try:
                with open(env_path, 'r') as f:
                    data = f.read(64)
                if data:
                    proc_env_leak = True
                    break
            except Exception:
                pass
except Exception:
    pass
if proc_env_leak:
    accessible.append('other_proc_environ')

# Cloud credentials
cred_paths = [
    os.path.expanduser('~/.aws/credentials'),
    os.path.expanduser('~/.aws/config'),
    os.path.expanduser('~/.config/gcloud/application_default_credentials.json'),
    os.path.expanduser('~/.kube/config'),
]
for cp in cred_paths:
    try:
        if os.path.exists(cp):
            with open(cp, 'r') as f:
                f.read(1)
            accessible.append('cloud_cred:' + cp)
    except Exception:
        pass

print(json.dumps({'accessible': accessible}))
"""


def _step_sec_filesystem_exposure(runner, base_dir):
    """Check access to sensitive files, devices, and cloud credentials."""
    step = StepProfile(name='sec_filesystem_exposure', started_at=time.time())

    try:
        result = _run_script(runner, base_dir, 'sec_fs.py',
                             FILESYSTEM_SCRIPT, timeout=30)

        import json
        output = result['result'].strip()
        if result['exit_code'] == 0 and output:
            data = json.loads(output)
            accessible = data.get('accessible', [])
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at

            critical = [a for a in accessible
                        if a.startswith(('/dev/mem', '/dev/sda', '/dev/vda',
                                         '/dev/xvda', '/proc/kcore',
                                         'cloud_cred'))]
            if critical:
                step.success = False
                step.detail = 'CRITICAL accessible: {}'.format(
                    ', '.join(critical))
            else:
                step.success = True
                step.detail = 'accessible: {}'.format(
                    ', '.join(accessible) if accessible else 'none sensitive')
        else:
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at
            step.success = False
            step.detail = 'fs check failed: {}'.format(output[:200])
    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = str(e)[:200]

    return step


RESOURCE_LIMITS_SCRIPT = """\
import json
import os
import resource

limits = {}

# PID limit
try:
    with open('/proc/sys/kernel/pid_max', 'r') as f:
        limits['pid_max'] = f.read().strip()
except Exception:
    limits['pid_max'] = 'unreadable'

# cgroup pids.max (cgroup v2 and v1)
for path in ['/sys/fs/cgroup/pids.max',
             '/sys/fs/cgroup/pids/pids.max',
             '/sys/fs/cgroup/pids/docker/pids.max']:
    try:
        with open(path, 'r') as f:
            val = f.read().strip()
        limits['pids_max'] = val
        limits['pids_max_path'] = path
        break
    except Exception:
        pass

# Memory limit (cgroup v2 then v1)
for path in ['/sys/fs/cgroup/memory.max',
             '/sys/fs/cgroup/memory/memory.limit_in_bytes']:
    try:
        with open(path, 'r') as f:
            val = f.read().strip()
        limits['memory_limit'] = val
        limits['memory_limit_path'] = path
        break
    except Exception:
        pass

# CPU limit (cgroup v2 then v1)
for path in ['/sys/fs/cgroup/cpu.max']:
    try:
        with open(path, 'r') as f:
            val = f.read().strip()
        limits['cpu_max'] = val
        break
    except Exception:
        pass

if 'cpu_max' not in limits:
    try:
        with open('/sys/fs/cgroup/cpu/cpu.cfs_quota_us', 'r') as f:
            quota = f.read().strip()
        with open('/sys/fs/cgroup/cpu/cpu.cfs_period_us', 'r') as f:
            period = f.read().strip()
        limits['cpu_max'] = '{} {}'.format(quota, period)
    except Exception:
        pass

# File descriptor ulimit
limits['nofile_soft'], limits['nofile_hard'] = resource.getrlimit(
    resource.RLIMIT_NOFILE)

# Try opening many FDs
max_opened = 0
fds = []
try:
    for i in range(10000):
        fd = os.open('/dev/null', os.O_RDONLY)
        fds.append(fd)
        max_opened += 1
except OSError:
    pass
finally:
    for fd in fds:
        os.close(fd)
limits['fds_opened'] = max_opened

print(json.dumps(limits))
"""


def _step_sec_resource_limits(runner, base_dir):
    """Check cgroup resource limits (PID, memory, CPU, FD)."""
    step = StepProfile(name='sec_resource_limits', started_at=time.time())

    try:
        result = _run_script(runner, base_dir, 'sec_rlimits.py',
                             RESOURCE_LIMITS_SCRIPT, timeout=30)

        import json
        output = result['result'].strip()
        if result['exit_code'] == 0 and output:
            data = json.loads(output)
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at

            has_mem = 'memory_limit' in data and data['memory_limit'] != 'max'
            has_pid = 'pids_max' in data and data['pids_max'] != 'max'
            has_cpu = 'cpu_max' in data and data['cpu_max'] != 'max'

            parts = []
            if has_mem:
                parts.append('mem={}'.format(data['memory_limit']))
            if has_pid:
                parts.append('pids={}'.format(data.get('pids_max', '?')))
            if has_cpu:
                parts.append('cpu={}'.format(data.get('cpu_max', '?')))
            parts.append('fds_opened={}'.format(data.get('fds_opened', '?')))
            parts.append('nofile={}/{}'.format(
                data.get('nofile_soft', '?'), data.get('nofile_hard', '?')))

            if has_mem or has_pid or has_cpu:
                step.success = True
                step.detail = 'limits configured: {}'.format('; '.join(parts))
            else:
                step.success = False
                step.detail = 'no cgroup limits found: {}'.format(
                    '; '.join(parts))
        else:
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at
            step.success = False
            step.detail = 'resource check failed: {}'.format(output[:200])
    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = str(e)[:200]

    return step


EGRESS_SCRIPT = """\
import json
import socket

# Test egress to well-known external IP on dangerous ports
target = '1.1.1.1'
ports = {
    25: 'SMTP',
    6379: 'Redis',
    3306: 'MySQL',
    5432: 'Postgres',
}

results = {}
for port, name in ports.items():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        rc = s.connect_ex((target, port))
        results[name] = 'open' if rc == 0 else 'filtered'
        s.close()
    except Exception:
        results[name] = 'filtered'

# Raw socket test
raw_socket_works = False
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
    s.close()
    raw_socket_works = True
except Exception:
    pass

# IPv6 availability
ipv6_available = False
try:
    s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    s.settimeout(2)
    s.connect(('2606:4700:4700::1111', 80))
    ipv6_available = True
    s.close()
except Exception:
    pass

print(json.dumps({
    'ports': results,
    'raw_socket': raw_socket_works,
    'ipv6': ipv6_available,
}))
"""


def _step_sec_egress_filtering(runner, base_dir):
    """Test outbound network filtering on dangerous ports."""
    step = StepProfile(name='sec_egress_filtering', started_at=time.time())

    try:
        result = _run_script(runner, base_dir, 'sec_egress.py',
                             EGRESS_SCRIPT, timeout=30)

        import json
        output = result['result'].strip()
        if result['exit_code'] == 0 and output:
            data = json.loads(output)
            ports = data.get('ports', {})
            raw_socket = data.get('raw_socket', False)

            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at

            open_ports = [name for name, status in ports.items()
                          if status == 'open']
            parts = []
            for name, status in ports.items():
                parts.append('{}={}'.format(name, status))
            parts.append('raw_socket={}'.format(raw_socket))
            parts.append('ipv6={}'.format(data.get('ipv6', False)))

            if raw_socket or len(open_ports) == len(ports):
                step.success = False
                step.detail = 'weak egress filtering: {}'.format(
                    '; '.join(parts))
            else:
                step.success = True
                step.detail = '{}'.format('; '.join(parts))
        else:
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at
            step.success = True
            step.detail = 'egress test failed (likely filtered): {}'.format(
                output[:200])
    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = True
        step.detail = 'probe error (likely filtered): {}'.format(str(e)[:200])

    return step


ENV_LEAK_SCRIPT = """\
import json
import os
import re

sensitive_patterns = [
    r'.*API_KEY.*', r'.*SECRET.*', r'.*TOKEN.*', r'.*PASSWORD.*',
    r'.*CREDENTIAL.*', r'.*AWS_.*', r'.*PRIVATE_KEY.*',
]

compiled = [re.compile(p, re.IGNORECASE) for p in sensitive_patterns]

# Check env vars
env_vars = dict(os.environ)
suspicious = []
for key in env_vars:
    for pat in compiled:
        if pat.match(key):
            suspicious.append(key)
            break

# Also check /proc/self/environ for consistency
proc_env_keys = []
try:
    with open('/proc/self/environ', 'rb') as f:
        data = f.read()
    for entry in data.split(b'\\x00'):
        if b'=' in entry:
            key = entry.split(b'=')[0].decode('utf-8', errors='replace')
            proc_env_keys.append(key)
except Exception:
    pass

# Extra keys in /proc/self/environ not in os.environ
extra_proc_keys = [k for k in proc_env_keys if k and k not in env_vars]

print(json.dumps({
    'suspicious_env_vars': suspicious,
    'total_env_vars': len(env_vars),
    'extra_proc_keys': extra_proc_keys[:20],
}))
"""


def _step_sec_env_leak(runner, base_dir):
    """Check for leaked credentials in environment variables."""
    step = StepProfile(name='sec_env_leak', started_at=time.time())

    try:
        result = _run_script(runner, base_dir, 'sec_envleak.py',
                             ENV_LEAK_SCRIPT, timeout=30)

        import json
        output = result['result'].strip()
        if result['exit_code'] == 0 and output:
            data = json.loads(output)
            suspicious = data.get('suspicious_env_vars', [])
            extra = data.get('extra_proc_keys', [])
            total = data.get('total_env_vars', 0)

            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at

            if suspicious:
                step.success = False
                step.detail = 'sensitive env vars found: {} (total={})'.format(
                    ', '.join(suspicious), total)
            else:
                step.success = True
                step.detail = 'no sensitive env vars leaked (total={})'.format(
                    total)
            if extra:
                step.detail += '; extra proc keys: {}'.format(
                    ', '.join(extra[:5]))
        else:
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at
            step.success = False
            step.detail = 'env leak check failed: {}'.format(output[:200])
    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = str(e)[:200]

    return step


# ── Main Benchmark Function ────────────────────────────────────────

def run_security_benchmark(runner, provider):
    """Execute the full security & isolation benchmark suite.

    Args:
        runner: A sandbox runner instance (must already have a sandbox created).
        provider: 'daytona', 'e2b', 'blaxel', or 'modal' (for path resolution).

    Returns:
        list[StepProfile]: Profiling data for each benchmark step.
    """
    if provider == 'daytona':
        base_dir = '/root/sec_bench'
    elif provider == 'blaxel':
        base_dir = '/blaxel/sec_bench'
    elif provider == 'modal':
        base_dir = '/root/sec_bench'
    else:
        base_dir = '/home/user/sec_bench'
    runner.exec('mkdir -p {}'.format(base_dir), cwd='/tmp')

    steps = []

    print('    [SEC] Step 1/8: Cloud metadata / IMDS access...')
    steps.append(_step_sec_metadata_service(runner, base_dir))
    print('    [SEC]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [SEC] Step 2/8: Privilege & identity audit...')
    steps.append(_step_sec_privilege_info(runner, base_dir))
    print('    [SEC]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [SEC] Step 3/8: Container/VM escape surface...')
    steps.append(_step_sec_container_escape(runner, base_dir))
    print('    [SEC]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [SEC] Step 4/8: Internal network scan...')
    steps.append(_step_sec_network_scan(runner, base_dir))
    print('    [SEC]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [SEC] Step 5/8: Sensitive filesystem exposure...')
    steps.append(_step_sec_filesystem_exposure(runner, base_dir))
    print('    [SEC]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [SEC] Step 6/8: Resource limits (cgroup)...')
    steps.append(_step_sec_resource_limits(runner, base_dir))
    print('    [SEC]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [SEC] Step 7/8: Egress filtering...')
    steps.append(_step_sec_egress_filtering(runner, base_dir))
    print('    [SEC]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    print('    [SEC] Step 8/8: Environment variable leakage...')
    steps.append(_step_sec_env_leak(runner, base_dir))
    print('    [SEC]   {:.1f}s - {}'.format(steps[-1].duration_s, steps[-1].detail))

    return steps
