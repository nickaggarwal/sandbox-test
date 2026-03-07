"""
E2B Sandbox Integration.

Same interface as DaytonaSandboxRunner so the parallel profiler
can swap providers transparently.

Uses the `e2b-code-interpreter` package with the Sandbox.create() API.
"""
import base64
import io
import os
import tarfile
import time

from e2b_code_interpreter import Sandbox


class E2BSandboxRunner:
    """Manages E2B sandbox lifecycle for RL agent execution."""

    def __init__(self, api_key):
        self.api_key = api_key
        self.sandbox = None
        self.sandbox_id = None

    def create_sandbox(self):
        """Create a new E2B Python sandbox."""
        self.sandbox = Sandbox.create(
            timeout=300,
            envs={
                'DJANGO_SETTINGS_MODULE': 'calendly_project.settings',
                'PYTHONDONTWRITEBYTECODE': '1',
            },
            api_key=self.api_key,
        )
        self.sandbox_id = self.sandbox.sandbox_id
        print('[E2B] Created sandbox: {}'.format(self.sandbox_id))
        return {'id': self.sandbox_id}

    def exec(self, command, cwd='/home/user/app', timeout=300):
        """Execute a shell command in the E2B sandbox."""
        try:
            full_cmd = 'cd {} && {}'.format(cwd, command)
            result = self.sandbox.commands.run(full_cmd, timeout=timeout)
            exit_code = result.exit_code
            stdout = result.stdout or ''
            stderr = result.stderr or ''
            output = stdout
            if stderr:
                output = output + '\n' + stderr if output else stderr
            return {
                'exit_code': exit_code,
                'result': output,
            }
        except Exception as e:
            return {'exit_code': -1, 'result': str(e)}

    def upload_project(self, project_dir, remote_dir='app'):
        """Upload project files using E2B filesystem API."""
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

        print('  Uploading {} files via E2B filesystem...'.format(
            len(files_to_upload)))

        # Create tar.gz in memory
        buf = io.BytesIO()
        with tarfile.open(mode='w:gz', fileobj=buf) as tar:
            for local_path, rel_path in files_to_upload:
                tar.add(local_path, arcname=rel_path)
        tar_bytes = buf.getvalue()

        print('  Archive: {:.1f}KB compressed'.format(len(tar_bytes) / 1024))

        remote_base = '/home/user/{}'.format(remote_dir)

        # Create dir and upload tar
        self.exec('mkdir -p {}'.format(remote_base), cwd='/home/user')

        # Write tar.gz to sandbox filesystem
        tar_path = '/tmp/project.tar.gz'
        self.sandbox.files.write(tar_path, tar_bytes)

        # Extract
        result = self.exec(
            'tar xzf {} -C {}'.format(tar_path, remote_base),
            cwd='/home/user',
        )
        if result['exit_code'] != 0:
            raise RuntimeError('E2B tar extract failed: {}'.format(
                result['result']))

        # Clean up tar
        self.exec('rm -f {}'.format(tar_path), cwd='/home/user')
        print('  Upload complete')

    def setup_environment(self):
        """Install dependencies and run migrations."""
        commands = [
            'pip install django djangorestframework pytz gymnasium numpy',
            'python manage.py migrate --run-syncdb',
        ]
        results = []
        for cmd in commands:
            print('[E2B] Running: {}'.format(cmd))
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
        """Download a file from the sandbox."""
        try:
            content = self.sandbox.files.read(remote_path)
            if isinstance(content, bytes):
                return content.decode('utf-8')
            return content
        except Exception:
            result = self.exec(
                'cat {}'.format(remote_path), cwd='/home/user'
            )
            return result['result']

    def upload_file_native(self, content, remote_path):
        """Upload a single file using E2B native FS API."""
        if isinstance(content, str):
            content = content.encode('utf-8')
        self.sandbox.files.write(remote_path, content)

    def download_file_native(self, remote_path):
        """Download a single file using E2B native FS API. Returns bytes."""
        content = self.sandbox.files.read(remote_path, format='bytes')
        return bytes(content)

    def list_files_native(self, remote_path):
        """List files in a directory using E2B native FS API."""
        return self.sandbox.files.list(remote_path)

    def pause_sandbox(self):
        """Pause the sandbox (E2B native pause, preserves full state)."""
        # SDK compatibility:
        # - Newer e2b exposes `beta_pause()`
        # - Some variants may only expose class-level pause helper
        if hasattr(self.sandbox, 'beta_pause'):
            self.sandbox.beta_pause()
            return
        if hasattr(Sandbox, 'beta_pause'):
            Sandbox.beta_pause(self.sandbox_id, api_key=self.api_key)
            return
        raise AttributeError('E2B pause API not available in installed SDK')

    def resume_sandbox(self):
        """Resume a paused sandbox via Sandbox.connect()."""
        self.sandbox = Sandbox.connect(
            self.sandbox_id,
            api_key=self.api_key,
        )

    def destroy(self):
        if self.sandbox:
            try:
                self.sandbox.kill()
                print('[E2B] Destroyed sandbox: {}'.format(self.sandbox_id))
            except Exception as e:
                print('[E2B] Cleanup error: {}'.format(e))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.destroy()
        return False
