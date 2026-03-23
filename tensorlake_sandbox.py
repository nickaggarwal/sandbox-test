"""
TensorLake Sandbox Integration.

Same interface as DaytonaSandboxRunner, E2BSandboxRunner, etc. so the
parallel profiler can swap providers transparently.

Uses the `tensorlake` package with SandboxClient + Sandbox APIs.
Auth via TENSORLAKE_API_KEY env var or explicit api_key parameter.
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

from tensorlake.sandbox import SandboxClient


class TensorLakeSandboxRunner:
    """Manages TensorLake sandbox lifecycle for RL agent execution."""

    def __init__(self, api_key=None):
        self.api_key = api_key or os.environ.get('TENSORLAKE_API_KEY', '')
        self.client = SandboxClient(api_key=self.api_key)
        self.sandbox = None
        self.sandbox_id = None

    def create_sandbox(self):
        """Create a new TensorLake sandbox and wait for it to be running."""
        resp = self.client.create(
            image='python:3.12-slim',
            cpus=4.0,
            memory_mb=8192,
            ephemeral_disk_mb=10240,
            timeout_secs=600,
        )
        self.sandbox_id = resp.sandbox_id
        print('[TensorLake] Created sandbox: {}'.format(self.sandbox_id[:20]))

        # Poll until running
        for _ in range(60):
            info = self.client.get(self.sandbox_id)
            if str(info.status) == 'SandboxStatus.RUNNING':
                break
            time.sleep(0.5)

        self.sandbox = self.client.connect(self.sandbox_id)
        return {'id': self.sandbox_id}

    def exec(self, command, cwd='/root/app', timeout=300):
        """Execute a shell command in the TensorLake sandbox."""
        try:
            result = self.sandbox.run(
                'bash',
                args=['-c', 'export PIP_BREAK_SYSTEM_PACKAGES=1 && cd {} && {}'.format(cwd, command)],
                working_dir='/',
                timeout=float(timeout),
            )
            stdout = result.stdout or ''
            stderr = result.stderr or ''
            output = stdout
            if stderr:
                output = output + '\n' + stderr if output else stderr
            return {'exit_code': result.exit_code, 'result': output}
        except Exception as e:
            return {'exit_code': -1, 'result': str(e)}

    def upload_project(self, project_dir, remote_dir='app'):
        """Upload project files to the TensorLake sandbox."""
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

        print('  Uploading {} files via TensorLake filesystem...'.format(
            len(files_to_upload)))

        # Create tar.gz in memory
        buf = io.BytesIO()
        with tarfile.open(mode='w:gz', fileobj=buf) as tar:
            for local_path, rel_path in files_to_upload:
                tar.add(local_path, arcname=rel_path)
        tar_bytes = buf.getvalue()

        print('  Archive: {:.1f}KB compressed'.format(len(tar_bytes) / 1024))

        remote_base = '/root/{}'.format(remote_dir)

        # Create dir and upload tar
        self.exec('mkdir -p {}'.format(remote_base), cwd='/root')

        tar_path = '/tmp/project.tar.gz'
        self.sandbox.write_file(tar_path, tar_bytes)

        # Extract
        result = self.exec(
            'tar xzf {} -C {}'.format(tar_path, remote_base),
            cwd='/root',
        )
        if result['exit_code'] != 0:
            raise RuntimeError('TensorLake tar extract failed: {}'.format(
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
            print('[TensorLake] Running: {}'.format(cmd))
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
            content = self.sandbox.read_file(remote_path)
            if isinstance(content, bytes):
                return content.decode('utf-8')
            return content
        except Exception:
            result = self.exec(
                'cat {}'.format(remote_path), cwd='/root'
            )
            return result['result']

    def upload_file_native(self, content, remote_path):
        """Upload a single file using TensorLake native FS API."""
        if isinstance(content, str):
            content = content.encode('utf-8')
        self.sandbox.write_file(remote_path, content)

    def download_file_native(self, remote_path):
        """Download a single file using TensorLake native FS API. Returns bytes."""
        data = self.sandbox.read_file(remote_path)
        if isinstance(data, str):
            return data.encode('utf-8')
        return bytes(data)

    def list_files_native(self, remote_path):
        """List files in a directory using TensorLake native FS API."""
        try:
            listing = self.sandbox.list_directory(remote_path)
            return listing.entries
        except Exception:
            result = self.exec(
                'ls -1 {}'.format(remote_path), cwd='/tmp', timeout=15,
            )
            if result['exit_code'] == 0:
                return [f for f in result['result'].strip().split('\n') if f]
            return []

    def pause_sandbox(self):
        """Snapshot the sandbox (TensorLake uses snapshots for pause)."""
        snap_resp = self.client.snapshot(self.sandbox_id)
        self._snapshot_id = snap_resp.snapshot_id
        # Wait for snapshot to complete
        for _ in range(60):
            snap_info = self.client.get_snapshot(self._snapshot_id)
            if str(snap_info.status) == 'SnapshotStatus.COMPLETED':
                break
            time.sleep(1)
        # Terminate the original sandbox
        self.sandbox.terminate()

    def resume_sandbox(self):
        """Resume from snapshot by creating a new sandbox from the snapshot."""
        resp = self.client.create(
            snapshot_id=self._snapshot_id,
            cpus=4.0,
            memory_mb=8192,
            ephemeral_disk_mb=10240,
            timeout_secs=600,
        )
        old_id = self.sandbox_id
        self.sandbox_id = resp.sandbox_id
        # Wait for running
        for _ in range(60):
            info = self.client.get(self.sandbox_id)
            if str(info.status) == 'SandboxStatus.RUNNING':
                break
            time.sleep(0.5)
        self.sandbox = self.client.connect(self.sandbox_id)
        # Clean up old sandbox
        try:
            self.client.delete(old_id)
        except Exception:
            pass

    def destroy(self):
        if self.sandbox:
            try:
                self.sandbox.terminate()
                self.client.delete(self.sandbox_id)
                print('[TensorLake] Destroyed sandbox: {}'.format(
                    self.sandbox_id[:20] if self.sandbox_id else 'N/A'))
            except Exception as e:
                print('[TensorLake] Cleanup error: {}'.format(e))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.destroy()
        return False
