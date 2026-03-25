"""
Blaxel Sandbox Integration.

Same interface as DaytonaSandboxRunner and E2BSandboxRunner so the parallel
profiler can swap providers transparently.

Uses the `blaxel` package with async SandboxInstance API, bridged to sync.
"""
import asyncio
import io
import os
import tarfile
import threading
import time
import uuid


def _get_sandbox_instance_class(api_key, workspace):
    """Import SandboxInstance after setting env vars (SDK reads them at import)."""
    os.environ['BL_API_KEY'] = api_key
    os.environ['BL_WORKSPACE'] = workspace
    from blaxel.core import SandboxInstance
    return SandboxInstance


class BlaxelSandboxRunner:
    """Manages Blaxel sandbox lifecycle for RL agent execution."""

    _global_lock = threading.Lock()
    _global_loop = None

    def __init__(self, api_key, workspace='inferless'):
        self.api_key = api_key
        self.workspace = workspace
        self._SandboxInstance = _get_sandbox_instance_class(api_key, workspace)
        self.sandbox = None
        self.sandbox_id = None

    def _run(self, coro):
        """Run an async coroutine synchronously on a shared event loop."""
        with self._global_lock:
            if self.__class__._global_loop is None or self.__class__._global_loop.is_closed():
                self.__class__._global_loop = asyncio.new_event_loop()
            return self.__class__._global_loop.run_until_complete(coro)

    def create_sandbox(self):
        """Create a new Blaxel sandbox using the py-app image."""
        name = 'rl-test-{}'.format(uuid.uuid4().hex[:8])
        self.sandbox = self._run(self._SandboxInstance.create({
            'name': name,
            'image': 'blaxel/py-app:latest',
            'memory': 4096,
            'vcpu': 4,
        }))
        self.sandbox_id = name
        print('[Blaxel] Created sandbox: {}'.format(self.sandbox_id))
        return {'id': self.sandbox_id}

    def exec(self, command, cwd='/blaxel/app', timeout=300):
        """Execute a shell command in the Blaxel sandbox."""
        try:
            process = self._run(self.sandbox.process.exec({
                'command': command,
                'working_dir': cwd,
                'wait_for_completion': True,
                'timeout': timeout * 1000,
            }))
            exit_code = process.exit_code
            stdout = process.stdout or ''
            stderr = process.stderr or ''
            output = stdout
            if stderr:
                output = output + '\n' + stderr if output else stderr
            return {'exit_code': exit_code, 'result': output}
        except Exception as e:
            return {'exit_code': -1, 'result': str(e)}

    def upload_project(self, project_dir, remote_dir='app'):
        """Upload project files using Blaxel filesystem API."""
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

        print('  Uploading {} files via Blaxel filesystem...'.format(
            len(files_to_upload)))

        # Create tar.gz in memory
        buf = io.BytesIO()
        with tarfile.open(mode='w:gz', fileobj=buf) as tar:
            for local_path, rel_path in files_to_upload:
                tar.add(local_path, arcname=rel_path)
        tar_bytes = buf.getvalue()

        print('  Archive: {:.1f}KB compressed'.format(len(tar_bytes) / 1024))

        remote_base = '/blaxel/{}'.format(remote_dir)

        # Create dir and upload tar
        self.exec('mkdir -p {}'.format(remote_base), cwd='/blaxel')

        tar_path = '/tmp/project.tar.gz'
        self._run(self.sandbox.fs.write_binary(tar_path, tar_bytes))

        # Extract
        result = self.exec(
            'tar xzf {} -C {}'.format(tar_path, remote_base),
            cwd='/blaxel',
        )
        if result['exit_code'] != 0:
            raise RuntimeError('Blaxel tar extract failed: {}'.format(
                result['result']))

        # Clean up tar
        self.exec('rm -f {}'.format(tar_path), cwd='/blaxel')
        print('  Upload complete')

    def setup_environment(self):
        """Install dependencies and run migrations."""
        commands = [
            'pip install django djangorestframework pytz gymnasium numpy',
            'python manage.py migrate --run-syncdb',
        ]
        results = []
        for cmd in commands:
            print('[Blaxel] Running: {}'.format(cmd))
            result = self.exec(cmd, cwd='/blaxel/app', timeout=180)
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
            cwd='/blaxel/app',
            timeout=120,
        )

    def run_rl_training(self, episodes=50, max_steps=20):
        return self.exec(
            'python run_rl_agent.py --episodes {} --max-steps {}'.format(
                episodes, max_steps
            ),
            cwd='/blaxel/app',
            timeout=600,
        )

    def download_file(self, remote_path):
        """Download a file from the sandbox."""
        try:
            content = self._run(self.sandbox.fs.read(remote_path))
            return content
        except Exception:
            result = self.exec(
                'cat {}'.format(remote_path), cwd='/blaxel'
            )
            return result['result']

    def upload_file_native(self, content, remote_path):
        """Upload a single file using Blaxel native FS API."""
        if isinstance(content, str):
            self._run(self.sandbox.fs.write(remote_path, content))
        else:
            self._run(self.sandbox.fs.write_binary(remote_path, content))

    def download_file_native(self, remote_path):
        """Download a single file using Blaxel native FS API. Returns bytes."""
        try:
            content = self._run(self.sandbox.fs.read_binary(remote_path))
            return content
        except Exception:
            # Fallback to text read for non-binary files
            content = self._run(self.sandbox.fs.read(remote_path))
            if isinstance(content, str):
                return content.encode('utf-8')
            return content

    def list_files_native(self, remote_path):
        """List files in a directory using Blaxel native FS API."""
        result = self._run(self.sandbox.fs.ls(remote_path))
        return result

    def pause_sandbox(self):
        """Pause sandbox if supported by SDK."""
        if hasattr(self.sandbox, 'pause'):
            self._run(self.sandbox.pause())
            return
        raise NotImplementedError('Blaxel SDK does not expose pause()')

    def resume_sandbox(self):
        """Resume sandbox if supported by SDK."""
        if hasattr(self.sandbox, 'resume'):
            self._run(self.sandbox.resume())
            return
        raise NotImplementedError('Blaxel SDK does not expose resume()')

    def destroy(self):
        if self.sandbox:
            try:
                self._run(self.sandbox.delete())
                print('[Blaxel] Destroyed sandbox: {}'.format(
                    self.sandbox_id))
            except Exception as e:
                print('[Blaxel] Cleanup error: {}'.format(e))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.destroy()
        return False
