"""
Daytona Sandbox Integration using the official Python SDK.

Uses the `daytona` package (>=0.148) for sandbox lifecycle,
file uploads (native FS API), and command execution.
Falls back to tar.gz bundled upload for efficiency.
"""
import base64
import io
import os
import tarfile
import time
from typing import Optional

# Fix SSL certificate verification for Python 3.12 + Homebrew
try:
    import certifi
    os.environ.setdefault('SSL_CERT_FILE', certifi.where())
except ImportError:
    pass

from daytona import Daytona, DaytonaConfig, CreateSandboxFromImageParams, Resources


class DaytonaSandboxRunner:
    """Manages Daytona sandbox lifecycle for RL agent execution."""

    def __init__(self, api_key, target='us'):
        self.api_key = api_key
        self.target = target
        self.daytona = Daytona(DaytonaConfig(
            api_key=api_key,
            target=target,
        ))
        self.sandbox = None
        self.sandbox_id = None

    def create_sandbox(self):
        """Create a Python sandbox via Daytona SDK (python:3.12-slim + resources)."""
        self.sandbox = self.daytona.create(
            CreateSandboxFromImageParams(
                image='python:3.12-slim',
                language='python',
                resources=Resources(cpu=4, memory=8, disk=10),
                env_vars={
                    'DJANGO_SETTINGS_MODULE': 'calendly_project.settings',
                    'PYTHONDONTWRITEBYTECODE': '1',
                },
                auto_stop_interval=30,
            ),
            timeout=300,
        )
        self.sandbox_id = self.sandbox.id
        print('[Daytona SDK] Created sandbox: {}'.format(self.sandbox_id))
        return {'id': self.sandbox_id}

    def exec(self, command, cwd='/root/app', timeout=300):
        """Execute a shell command in the sandbox.

        Note: Daytona has a hard ~60s server-side timeout on process.exec().
        For long-running commands (>50s), use exec_long() instead.
        """
        try:
            response = self.sandbox.process.exec(
                command, cwd=cwd, timeout=timeout,
            )
            return {
                'exit_code': response.exit_code,
                'result': response.result or '',
            }
        except Exception as e:
            return {'exit_code': -1, 'result': str(e)}

    def exec_long(self, command, cwd='/root/app', timeout=600,
                  poll_interval=5):
        """Execute a long-running command using nohup + polling.

        Daytona's process.exec() has a ~60s server-side timeout, so
        long-running commands must be run in the background and polled.
        """
        import time as _time

        log_file = '/tmp/_long_cmd_{}.log'.format(
            int(_time.time() * 1000) % 100000
        )
        done_file = '{}.done'.format(log_file)

        # Launch in background with nohup, write exit code to done_file
        bg_cmd = (
            'nohup bash -c \'{cmd}; echo $? > {done}\' '
            '> {log} 2>&1 &'
        ).format(cmd=command.replace("'", "'\\''"), log=log_file,
                 done=done_file)

        self.exec(bg_cmd, cwd=cwd, timeout=30)

        # Poll for completion
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            _time.sleep(poll_interval)
            check = self.exec(
                'cat {} 2>/dev/null || echo waiting'.format(done_file),
                cwd='/tmp', timeout=15,
            )
            result_text = check['result'].strip()
            if result_text != 'waiting' and result_text != '':
                # Command finished — read the full output
                try:
                    exit_code = int(result_text)
                except ValueError:
                    exit_code = 1
                output = self.exec(
                    'cat {}'.format(log_file), cwd='/tmp', timeout=15,
                )
                # Cleanup temp files
                self.exec(
                    'rm -f {} {}'.format(log_file, done_file),
                    cwd='/tmp', timeout=10,
                )
                return {
                    'exit_code': exit_code,
                    'result': output['result'],
                }

        # Timeout — try to get partial output
        output = self.exec(
            'cat {}'.format(log_file), cwd='/tmp', timeout=15,
        )
        return {
            'exit_code': -1,
            'result': 'TIMEOUT after {}s. Partial output:\n{}'.format(
                timeout, output['result']
            ),
        }

    # ── File Upload ─────────────────────────────────────────────────

    def upload_project(self, project_dir, remote_dir='app'):
        """Upload project as a tar.gz bundle via native FS API."""
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

        print('  Bundling {} files into tar.gz...'.format(len(files_to_upload)))

        # Create in-memory tar.gz
        buf = io.BytesIO()
        with tarfile.open(mode='w:gz', fileobj=buf) as tar:
            for local_path, rel_path in files_to_upload:
                tar.add(local_path, arcname=rel_path)
        tar_bytes = buf.getvalue()

        print('  Archive: {:.1f}KB compressed ({} files)'.format(
            len(tar_bytes) / 1024, len(files_to_upload)))

        remote_base = '/root/{}'.format(remote_dir)

        # Create target directory
        self.exec('mkdir -p {}'.format(remote_base), cwd='/tmp')

        # Upload tar.gz via native FS API
        tar_remote = '/tmp/project_upload.tar.gz'
        self.sandbox.fs.upload_file(tar_bytes, tar_remote)

        # Extract
        result = self.exec(
            'tar xzf {} -C {}'.format(tar_remote, remote_base),
            cwd='/tmp',
        )
        if result['exit_code'] == 0:
            self.exec('rm -f {}'.format(tar_remote), cwd='/tmp')

        if result['exit_code'] != 0:
            raise RuntimeError(
                'Tar extract failed: {}'.format(result['result'])
            )

        print('  Upload complete')

    # ── Environment Setup ───────────────────────────────────────────

    def setup_environment(self):
        """Install dependencies and run migrations."""
        commands = [
            'pip install django djangorestframework pytz gymnasium numpy',
            'python manage.py migrate --run-syncdb',
        ]
        results = []
        for cmd in commands:
            print('[Daytona SDK] Running: {}'.format(cmd))
            result = self.exec(cmd, timeout=180)
            results.append(result)
            print('  Exit code: {}'.format(result['exit_code']))
            if result['exit_code'] != 0:
                lines = result['result'].strip().split('\n')
                for line in lines[-8:]:
                    print('  {}'.format(line))
        return results

    def run_tests(self):
        return self.exec_long(
            'python manage.py test scheduling --verbosity=2',
            timeout=120,
        )

    def run_rl_training(self, episodes=50, max_steps=20):
        return self.exec_long(
            'python run_rl_agent.py --episodes {} --max-steps {}'.format(
                episodes, max_steps
            ),
            timeout=600,
        )

    def download_file(self, remote_path):
        """Download a file from the sandbox via FS API."""
        try:
            content = self.sandbox.fs.download_file(remote_path)
            if isinstance(content, bytes):
                return content.decode('utf-8')
            return content
        except Exception:
            # Fallback to cat
            result = self.exec(
                'cat {}'.format(remote_path), cwd='/tmp'
            )
            return result['result']

    def upload_file_native(self, content, remote_path):
        """Upload a single file using Daytona native FS API."""
        if isinstance(content, str):
            content = content.encode('utf-8')
        self.sandbox.fs.upload_file(content, remote_path)

    def download_file_native(self, remote_path):
        """Download a single file using Daytona native FS API. Returns bytes."""
        try:
            content = self.sandbox.fs.download_file(remote_path)
            if isinstance(content, str):
                content = content.encode('utf-8')
            return content
        except Exception:
            # Fallback for Python 3.9 compatibility (SDK uses str | None)
            import base64 as b64
            result = self.exec(
                'base64 {}'.format(remote_path), cwd='/tmp', timeout=30,
            )
            if result['exit_code'] == 0 and result['result']:
                return b64.b64decode(result['result'].strip())
            # Last resort: cat for text files
            result = self.exec(
                'cat {}'.format(remote_path), cwd='/tmp', timeout=30,
            )
            return result['result'].encode('utf-8')

    def list_files_native(self, remote_path):
        """List files in a directory using Daytona native FS API."""
        return self.sandbox.fs.list_files(remote_path)

    def pause_sandbox(self):
        """Pause (stop) the sandbox, preserving filesystem state."""
        self.sandbox.stop(timeout=60)

    def resume_sandbox(self):
        """Resume (start) a stopped sandbox."""
        self.sandbox.start(timeout=60)

    def destroy(self):
        if self.sandbox:
            try:
                self.daytona.delete(self.sandbox, timeout=30)
                print('[Daytona SDK] Destroyed sandbox: {}'.format(
                    self.sandbox_id))
            except Exception as e:
                print('[Daytona SDK] Cleanup error: {}'.format(e))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.destroy()
        return False
