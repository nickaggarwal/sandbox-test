"""
Modal Sandbox Integration.

Same interface as DaytonaSandboxRunner, E2BSandboxRunner, and
BlaxelSandboxRunner so the parallel profiler can swap providers
transparently.

Uses the `modal` package. Auth is via ~/.modal.toml (set with
`modal token set`).
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

import modal


class ModalSandboxRunner:
    """Manages Modal sandbox lifecycle for RL agent execution."""

    def __init__(self, **kwargs):
        self.app = modal.App.lookup("sandbox-rl-test", create_if_missing=True)
        self.sandbox = None
        self.sandbox_id = None
        self._snapshot_image = None
        self._image = modal.Image.debian_slim(python_version="3.12")
        self._cpu = 4.0
        self._memory_mb = 8192

    def create_sandbox(self):
        """Create a new Modal sandbox with Python 3.12."""
        self.sandbox = modal.Sandbox.create(
            image=self._image,
            app=self.app,
            timeout=600,
            cpu=self._cpu,
            memory=self._memory_mb,
        )
        self.sandbox_id = self.sandbox.object_id or 'modal-sandbox'
        print('[Modal] Created sandbox: {}'.format(self.sandbox_id[:20]))
        return {'id': self.sandbox_id}

    def exec(self, command, cwd='/root/app', timeout=300):
        """Execute a shell command in the Modal sandbox."""
        try:
            full_cmd = 'cd {} && {}'.format(cwd, command)
            process = self.sandbox.exec("bash", "-c", full_cmd)
            process.wait()
            exit_code = process.returncode
            stdout = process.stdout.read()
            stderr = process.stderr.read()
            output = stdout
            if stderr:
                output = output + '\n' + stderr if output else stderr
            return {'exit_code': exit_code, 'result': output}
        except Exception as e:
            return {'exit_code': -1, 'result': str(e)}

    def upload_project(self, project_dir, remote_dir='app'):
        """Upload project files to the Modal sandbox."""
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

        print('  Uploading {} files via Modal filesystem...'.format(
            len(files_to_upload)))

        # Create tar.gz in memory
        buf = io.BytesIO()
        with tarfile.open(mode='w:gz', fileobj=buf) as tar:
            for local_path, rel_path in files_to_upload:
                tar.add(local_path, arcname=rel_path)
        tar_bytes = buf.getvalue()

        print('  Archive: {:.1f}KB compressed'.format(len(tar_bytes) / 1024))

        remote_base = '/root/{}'.format(remote_dir)

        # Create dir
        self.exec('mkdir -p {}'.format(remote_base), cwd='/root')

        # Write tar via sandbox filesystem API
        tar_path = '/tmp/project.tar.gz'
        f = self.sandbox.open(tar_path, "wb")
        f.write(tar_bytes)
        f.close()

        # Extract
        result = self.exec(
            'tar xzf {} -C {}'.format(tar_path, remote_base),
            cwd='/root',
        )
        if result['exit_code'] != 0:
            raise RuntimeError('Modal tar extract failed: {}'.format(
                result['result']))

        # Clean up tar
        self.exec('rm -f {}'.format(tar_path), cwd='/root')
        print('  Upload complete')

    def setup_environment(self):
        """Install dependencies and run migrations."""
        commands = [
            'pip install django djangorestframework pytz gymnasium numpy',
            'python manage.py migrate --run-syncdb',
        ]
        results = []
        for cmd in commands:
            print('[Modal] Running: {}'.format(cmd))
            result = self.exec(cmd, cwd='/root/app', timeout=180)
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
            cwd='/root/app',
            timeout=120,
        )

    def run_rl_training(self, episodes=50, max_steps=20):
        return self.exec(
            'python run_rl_agent.py --episodes {} --max-steps {}'.format(
                episodes, max_steps
            ),
            cwd='/root/app',
            timeout=600,
        )

    def download_file(self, remote_path):
        """Download a file from the sandbox."""
        try:
            f = self.sandbox.open(remote_path, "r")
            content = f.read()
            f.close()
            return content
        except Exception:
            result = self.exec(
                'cat {}'.format(remote_path), cwd='/root'
            )
            return result['result']

    def upload_file_native(self, content, remote_path):
        """Upload a single file using Modal FS API."""
        if isinstance(content, str):
            f = self.sandbox.open(remote_path, "w")
        else:
            f = self.sandbox.open(remote_path, "wb")
        f.write(content)
        f.close()

    def download_file_native(self, remote_path):
        """Download a single file using Modal FS API. Returns bytes."""
        try:
            f = self.sandbox.open(remote_path, "rb")
            content = f.read()
            f.close()
            if content and len(content) > 0:
                if isinstance(content, str):
                    return content.encode('utf-8')
                return content
        except Exception:
            pass
        # Fallback: use exec + base64 for files that sandbox.open can't read
        import base64
        result = self.exec(
            'base64 < {}'.format(remote_path), cwd='/tmp', timeout=30)
        if result['exit_code'] == 0 and result['result'].strip():
            return base64.b64decode(result['result'].strip())
        return b''

    def list_files_native(self, remote_path):
        """List files in a directory using Modal FS API."""
        return self.sandbox.ls(remote_path)

    def pause_sandbox(self):
        """Snapshot filesystem and terminate current sandbox (pause proxy)."""
        if hasattr(self.sandbox, 'snapshot_filesystem'):
            self._snapshot_image = self.sandbox.snapshot_filesystem(timeout=55)
        elif hasattr(self.sandbox, '_experimental_snapshot'):
            self._snapshot_image = self.sandbox._experimental_snapshot()
        else:
            raise AttributeError('Modal snapshot API not available')

        # Simulate pause by stopping the current sandbox after snapshot.
        self.sandbox.terminate()
        self.sandbox = None

    def resume_sandbox(self):
        """Resume by creating a sandbox from the snapshot image."""
        if self._snapshot_image is None:
            raise RuntimeError('No Modal snapshot available for resume')

        self.sandbox = modal.Sandbox.create(
            image=self._snapshot_image,
            app=self.app,
            timeout=600,
            cpu=self._cpu,
            memory=self._memory_mb,
        )
        self.sandbox_id = self.sandbox.object_id or self.sandbox_id

    def destroy(self):
        if self.sandbox:
            try:
                self.sandbox.terminate()
                print('[Modal] Terminated sandbox: {}'.format(
                    self.sandbox_id[:20] if self.sandbox_id else 'N/A'))
            except Exception as e:
                print('[Modal] Cleanup error: {}'.format(e))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.destroy()
        return False
