"""
Runloop Sandbox Integration.

Same interface as DaytonaSandboxRunner, E2BSandboxRunner, BlaxelSandboxRunner,
and ModalSandboxRunner so the parallel profiler can swap providers transparently.

Uses the `runloop_api_client` package with the RunloopSDK object-oriented API.
Auth via RUNLOOP_API_KEY env var or explicit api_key parameter.
"""
import io
import os
import tarfile
import time

# Fix SSL certificate verification for Python 3.12 + macOS
try:
    import certifi
    os.environ.setdefault('SSL_CERT_FILE', certifi.where())
except ImportError:
    pass

from runloop_api_client import RunloopSDK


class RunloopSandboxRunner:
    """Manages Runloop devbox lifecycle for RL agent execution."""

    def __init__(self, api_key=None):
        self.api_key = api_key or os.environ.get('RUNLOOP_API_KEY', '')
        self.sdk = RunloopSDK(bearer_token=self.api_key)
        self.devbox = None
        self.sandbox_id = None

    def create_sandbox(self):
        """Create a new Runloop devbox."""
        self.devbox = self.sdk.devbox.create(
            name='bench-{}'.format(int(time.time() * 1000) % 1000000),
        )
        info = self.devbox.get_info()
        self.sandbox_id = info.id or 'runloop-devbox'
        print('[Runloop] Created devbox: {}'.format(self.sandbox_id[:20]))
        return {'id': self.sandbox_id}

    def exec(self, command, cwd='/home/user/app', timeout=300):
        """Execute a shell command in the Runloop devbox."""
        try:
            full_cmd = (
                'export LD_LIBRARY_PATH=/home/user/.local/lib:'
                '$LD_LIBRARY_PATH && cd {} && {}'
            ).format(cwd, command)
            result = self.devbox.cmd.exec(full_cmd)
            exit_code = result.exit_code
            stdout = result.stdout() or ''
            stderr = result.stderr() or ''
            output = stdout
            if stderr:
                output = output + '\n' + stderr if output else stderr
            return {'exit_code': exit_code, 'result': output}
        except Exception as e:
            return {'exit_code': -1, 'result': str(e)}

    def upload_project(self, project_dir, remote_dir='app'):
        """Upload project files to the Runloop devbox."""
        skip_dirs = {
            '__pycache__', '.git', 'venv', 'node_modules',
            'rl_output', '.claude', 'memory',
        }
        extensions = ('.py', '.txt', '.json', '.cfg', '.toml')

        files_to_upload = []
        for root, dirs, files in os.walk(project_dir):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for fname in files:
                if not fname.endswith(extensions):
                    continue
                if fname == 'db.sqlite3':
                    continue
                local_path = os.path.join(root, fname)
                rel_path = os.path.relpath(local_path, project_dir)
                files_to_upload.append((local_path, rel_path))

        print('  Uploading {} files via Runloop filesystem...'.format(
            len(files_to_upload)))

        # Create tar.gz in memory
        buf = io.BytesIO()
        with tarfile.open(mode='w:gz', fileobj=buf) as tar:
            for local_path, rel_path in files_to_upload:
                tar.add(local_path, arcname=rel_path)
        tar_bytes = buf.getvalue()

        print('  Archive: {:.1f}KB compressed'.format(len(tar_bytes) / 1024))

        remote_base = '/home/user/{}'.format(remote_dir)

        # Create dir
        self.exec('mkdir -p {}'.format(remote_base), cwd='/home/user')

        # Write tar via devbox file API (binary upload)
        tar_path = '/tmp/project.tar.gz'
        self.devbox.file.upload(path=tar_path, file=tar_bytes)

        # Extract
        result = self.exec(
            'tar xzf {} -C {}'.format(tar_path, remote_base),
            cwd='/home/user',
        )
        if result['exit_code'] != 0:
            raise RuntimeError('Runloop tar extract failed: {}'.format(
                result['result']))

        # Clean up tar
        self.exec('rm -f {}'.format(tar_path), cwd='/home/user')
        print('  Upload complete')

    def setup_environment(self):
        """Install dependencies and run migrations."""
        commands = [
            # Runloop devboxes lack libsqlite3.so.0 and user has no root access;
            # download the .deb from Debian and extract to a user-writable path,
            # then use LD_LIBRARY_PATH for all subsequent commands.
            'curl -sL http://ftp.debian.org/debian/pool/main/s/sqlite3/'
            'libsqlite3-0_3.40.1-2+deb12u2_amd64.deb -o /tmp/sqlite3.deb '
            '&& dpkg-deb -x /tmp/sqlite3.deb /tmp/sqlite3_extract '
            '&& mkdir -p /home/user/.local/lib '
            '&& cp /tmp/sqlite3_extract/usr/lib/x86_64-linux-gnu/libsqlite3.so.0* '
            '/home/user/.local/lib/',
            'pip install django djangorestframework pytz gymnasium numpy',
            'python manage.py migrate --run-syncdb',
        ]
        results = []
        for cmd in commands:
            print('[Runloop] Running: {}'.format(cmd[:80]))
            result = self.exec(cmd, cwd='/home/user/app', timeout=180)
            results.append(result)
            print('  Exit code: {}'.format(result['exit_code']))
            if result['exit_code'] != 0:
                lines = result['result'].strip().split('\n')
                for line in lines[-8:]:
                    print('  {}'.format(line))
        return results

    def run_tests(self):
        return self.exec(
            'python manage.py test scheduling --verbosity=2',
            cwd='/home/user/app',
            timeout=120,
        )

    def run_rl_training(self, episodes=50, max_steps=20):
        return self.exec(
            'python run_rl_agent.py --episodes {} --max-steps {}'.format(
                episodes, max_steps
            ),
            cwd='/home/user/app',
            timeout=600,
        )

    def download_file(self, remote_path):
        """Download a file from the devbox."""
        try:
            content = self.devbox.file.read(file_path=remote_path)
            return content
        except Exception:
            result = self.exec(
                'cat {}'.format(remote_path), cwd='/home/user'
            )
            return result['result']

    def upload_file_native(self, content, remote_path):
        """Upload a single file using Runloop native file API."""
        if isinstance(content, bytes):
            # Use binary upload for bytes content
            self.devbox.file.upload(path=remote_path, file=content)
        else:
            self.devbox.file.write(file_path=remote_path, contents=content)

    def download_file_native(self, remote_path):
        """Download a single file using Runloop native file API. Returns bytes."""
        try:
            data = self.devbox.file.download(path=remote_path)
            if isinstance(data, str):
                return data.encode('utf-8')
            return data
        except Exception:
            # Fallback: try file.read for text, then encode
            try:
                content = self.devbox.file.read(file_path=remote_path)
                if isinstance(content, str):
                    return content.encode('utf-8')
                return content
            except Exception:
                # Last resort: exec cat + base64
                import base64
                result = self.exec(
                    'base64 < {}'.format(remote_path),
                    cwd='/tmp', timeout=30,
                )
                if result['exit_code'] == 0 and result['result'].strip():
                    return base64.b64decode(result['result'].strip())
                return b''

    def list_files_native(self, remote_path):
        """List files in a directory. Falls back to ls via exec."""
        result = self.exec(
            'ls -1 {}'.format(remote_path), cwd='/tmp', timeout=15,
        )
        if result['exit_code'] == 0:
            return [f for f in result['result'].strip().split('\n') if f]
        return []

    def pause_sandbox(self):
        """Suspend the devbox (preserves state)."""
        self.devbox.suspend()

    def resume_sandbox(self):
        """Resume a suspended devbox."""
        self.devbox.resume()

    def destroy(self):
        if self.devbox:
            try:
                self.devbox.shutdown()
                print('[Runloop] Shutdown devbox: {}'.format(
                    self.sandbox_id[:20] if self.sandbox_id else 'N/A'))
            except Exception as e:
                print('[Runloop] Cleanup error: {}'.format(e))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.destroy()
        return False
