"""
Custom Docker Image Benchmark for Sandbox Profiling.

Tests how each provider handles building and launching sandboxes from
custom Docker images -- measuring image build time, sandbox creation
from custom image, and whether pre-baked dependencies speed up the
workflow.

Provider capabilities:
- Daytona: Image.debian_slim().pip_install().run_commands() (runtime build)
- Modal:   modal.Image.debian_slim().pip_install().run_commands() (runtime build)
- E2B:     Template-based (pre-built, no runtime build)
- Blaxel:  Direct Docker image string (pre-existing, no runtime build)

NOTE: Like fanout, this benchmark manages its own sandbox lifecycle
internally (creates and destroys its own runners).
"""
import os
import time

# Fix SSL cert verification on macOS
try:
    import certifi
    os.environ.setdefault('SSL_CERT_FILE', certifi.where())
except ImportError:
    pass

from run_parallel_profiled import StepProfile


# Packages to pre-install in the custom image
CUSTOM_DEPS = ['django', 'djangorestframework', 'pytest', 'flake8', 'numpy']

# Verification import command
VERIFY_CMD = (
    "python -c \""
    "import django, rest_framework, pytest, flake8, numpy; "
    "print('OK')"
    "\""
)

# Workload: numpy matrix multiply + Django version check
WORKLOAD_SCRIPT = (
    "python -c \""
    "import numpy as np; "
    "a = np.random.rand(200, 200); "
    "b = np.random.rand(200, 200); "
    "c = np.dot(a, b); "
    "print('matrix shape:', c.shape); "
    "import django; "
    "print('django version:', django.get_version()); "
    "print('WORKLOAD_OK')"
    "\""
)


def _get_base_dir(provider):
    """Get the base directory for this benchmark in a given provider."""
    if provider == 'daytona':
        return '/home/daytona/docker_bench'
    elif provider == 'blaxel':
        return '/blaxel/docker_bench'
    elif provider == 'modal':
        return '/root/docker_bench'
    else:
        return '/home/user/docker_bench'


def _supports_image_build(provider):
    """Check if provider supports runtime image building."""
    return provider in ('daytona', 'modal')


# ── Daytona Custom Image Builder ──────────────────────────────────

def _build_daytona_image():
    """Build a custom Daytona image with pre-installed deps."""
    from daytona import Image
    return (
        Image.debian_slim('3.12')
        .pip_install(*CUSTOM_DEPS)
        .run_commands(
            'python -c "import django; print(django.get_version())"'
        )
    )


def _create_daytona_sandbox(api_key, image):
    """Create a Daytona sandbox from a custom image."""
    from daytona import Daytona, DaytonaConfig, CreateSandboxFromImageParams, Resources
    daytona = Daytona(DaytonaConfig(api_key=api_key, target='us'))
    sandbox = daytona.create(
        CreateSandboxFromImageParams(
            image=image,
            resources=Resources(cpu=4, memory=8, disk=10),
            auto_stop_interval=30,
        ),
        timeout=300,
    )
    return daytona, sandbox


def _create_daytona_default_sandbox(api_key):
    """Create a Daytona sandbox with pre-warmed default (for comparison)."""
    from daytona import Daytona, DaytonaConfig, CreateSandboxBaseParams
    daytona = Daytona(DaytonaConfig(api_key=api_key, target='us'))
    sandbox = daytona.create(
        CreateSandboxBaseParams(
            language='python',
            auto_stop_interval=30,
        ),
        timeout=120,
    )
    return daytona, sandbox


# ── Modal Custom Image Builder ────────────────────────────────────

def _build_modal_image():
    """Build a custom Modal image with pre-installed deps."""
    import modal
    return (
        modal.Image.debian_slim(python_version='3.12')
        .pip_install(*CUSTOM_DEPS)
        .run_commands([
            'python -c "import django; print(django.get_version())"'
        ])
    )


def _create_modal_sandbox(image):
    """Create a Modal sandbox from a custom image."""
    import modal
    app = modal.App.lookup("sandbox-rl-test", create_if_missing=True)
    sandbox = modal.Sandbox.create(
        image=image,
        app=app,
        timeout=600,
        cpu=4.0,
        memory=8192,
    )
    return app, sandbox


def _create_modal_default_sandbox():
    """Create a Modal sandbox with default image (for comparison)."""
    import modal
    app = modal.App.lookup("sandbox-rl-test", create_if_missing=True)
    sandbox = modal.Sandbox.create(
        image=modal.Image.debian_slim(python_version='3.12'),
        app=app,
        timeout=600,
        cpu=4.0,
        memory=8192,
    )
    return app, sandbox


# ── E2B / Blaxel Default Sandbox ──────────────────────────────────

def _create_e2b_sandbox(api_key):
    """Create an E2B sandbox (template-based, no custom image build)."""
    from e2b_code_interpreter import Sandbox
    sandbox = Sandbox.create(timeout=300, api_key=api_key)
    return sandbox


def _create_blaxel_sandbox(api_key, workspace='inferless'):
    """Create a Blaxel sandbox (image-based, no custom build)."""
    import os
    import uuid
    os.environ['BL_API_KEY'] = api_key
    os.environ['BL_WORKSPACE'] = workspace
    from blaxel.core import SandboxInstance
    import asyncio
    loop = asyncio.new_event_loop()
    name = 'docker-bench-{}'.format(uuid.uuid4().hex[:8])
    sandbox = loop.run_until_complete(SandboxInstance.create({
        'name': name,
        'image': 'blaxel/py-app:latest',
        'memory': 8192,
        'vcpu': 4,
    }))
    return sandbox, loop, name


# ── Exec helpers ──────────────────────────────────────────────────

def _exec_daytona(sandbox, command, cwd='/home/daytona/docker_bench', timeout=300):
    """Run a command in a Daytona sandbox."""
    try:
        response = sandbox.process.exec(command, cwd=cwd, timeout=timeout)
        return {'exit_code': response.exit_code, 'result': response.result or ''}
    except Exception as e:
        return {'exit_code': -1, 'result': str(e)}


def _exec_modal(sandbox, command, cwd='/root/docker_bench', timeout=300):
    """Run a command in a Modal sandbox."""
    try:
        full_cmd = 'cd {} && {}'.format(cwd, command)
        process = sandbox.exec("bash", "-c", full_cmd)
        process.wait()
        stdout = process.stdout.read()
        stderr = process.stderr.read()
        output = stdout
        if stderr:
            output = output + '\n' + stderr if output else stderr
        return {'exit_code': process.returncode, 'result': output}
    except Exception as e:
        return {'exit_code': -1, 'result': str(e)}


def _exec_e2b(sandbox, command, cwd='/home/user/docker_bench', timeout=300):
    """Run a command in an E2B sandbox."""
    try:
        full_cmd = 'cd {} && {}'.format(cwd, command)
        result = sandbox.commands.run(full_cmd, timeout=timeout)
        stdout = result.stdout or ''
        stderr = result.stderr or ''
        output = stdout
        if stderr:
            output = output + '\n' + stderr if output else stderr
        return {'exit_code': result.exit_code, 'result': output}
    except Exception as e:
        return {'exit_code': -1, 'result': str(e)}


def _exec_blaxel(sandbox, loop, command, cwd='/blaxel/docker_bench',
                 timeout=300):
    """Run a command in a Blaxel sandbox."""
    try:
        process = loop.run_until_complete(sandbox.process.exec({
            'command': command,
            'working_dir': cwd,
            'wait_for_completion': True,
            'timeout': timeout * 1000,
        }))
        stdout = process.stdout or ''
        stderr = process.stderr or ''
        output = stdout
        if stderr:
            output = output + '\n' + stderr if output else stderr
        return {'exit_code': process.exit_code, 'result': output}
    except Exception as e:
        return {'exit_code': -1, 'result': str(e)}


# ── Benchmark Steps ──────────────────────────────────────────────

def _step_build_image(provider):
    """Step 1: Build a custom image with pre-installed deps."""
    step = StepProfile(name='docker_build_image', started_at=time.time())

    if not _supports_image_build(provider):
        step.ended_at = time.time()
        step.duration_s = 0.0
        step.success = True
        step.detail = 'template-based, no runtime build ({})'.format(provider)
        return step, None

    try:
        if provider == 'daytona':
            image = _build_daytona_image()
        else:  # modal
            image = _build_modal_image()

        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = True
        step.detail = 'custom image built with {} deps'.format(len(CUSTOM_DEPS))
        return step, image

    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = 'build failed: {}'.format(str(e)[:200])
        return step, None


def _step_create_sandbox(provider, api_key, image):
    """Step 2: Create a sandbox from the custom image."""
    step = StepProfile(name='docker_create_sandbox', started_at=time.time())

    try:
        sandbox_ctx = None

        if provider == 'daytona':
            if image:
                daytona, sandbox = _create_daytona_sandbox(api_key, image)
                sandbox_ctx = ('daytona', daytona, sandbox)
            else:
                daytona, sandbox = _create_daytona_default_sandbox(api_key)
                sandbox_ctx = ('daytona', daytona, sandbox)

        elif provider == 'modal':
            if image:
                app, sandbox = _create_modal_sandbox(image)
                sandbox_ctx = ('modal', app, sandbox)
            else:
                app, sandbox = _create_modal_default_sandbox()
                sandbox_ctx = ('modal', app, sandbox)

        elif provider == 'e2b':
            sandbox = _create_e2b_sandbox(api_key)
            sandbox_ctx = ('e2b', None, sandbox)

        elif provider == 'blaxel':
            sandbox, loop, name = _create_blaxel_sandbox(api_key)
            sandbox_ctx = ('blaxel', loop, sandbox, name)

        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = True
        step.detail = 'sandbox created from {} image in {:.2f}s'.format(
            'custom' if image else 'default', step.duration_s)
        return step, sandbox_ctx

    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = 'create failed: {}'.format(str(e)[:200])
        return step, None


def _run_exec(provider, sandbox_ctx, command, cwd=None):
    """Run a command using the right exec for the provider."""
    if cwd is None:
        cwd = _get_base_dir(provider)

    if provider == 'daytona':
        _, _, sandbox = sandbox_ctx
        return _exec_daytona(sandbox, command, cwd=cwd)
    elif provider == 'modal':
        _, _, sandbox = sandbox_ctx
        return _exec_modal(sandbox, command, cwd=cwd)
    elif provider == 'e2b':
        _, _, sandbox = sandbox_ctx
        return _exec_e2b(sandbox, command, cwd=cwd)
    elif provider == 'blaxel':
        _, loop, sandbox = sandbox_ctx[:3]
        return _exec_blaxel(sandbox, loop, command, cwd=cwd)


def _step_verify_deps(provider, sandbox_ctx):
    """Step 3: Verify that pre-installed packages are available."""
    step = StepProfile(name='docker_verify_deps', started_at=time.time())
    base_dir = _get_base_dir(provider)

    try:
        # Ensure base dir exists
        _run_exec(provider, sandbox_ctx, 'mkdir -p {}'.format(base_dir),
                  cwd='/tmp')

        if not _supports_image_build(provider):
            # For E2B/Blaxel: must pip install at runtime first
            pip_start = time.time()
            pip_result = _run_exec(
                provider, sandbox_ctx,
                'pip install {}'.format(' '.join(CUSTOM_DEPS)),
                cwd=base_dir,
            )
            pip_dur = time.time() - pip_start
            if pip_result['exit_code'] != 0:
                step.ended_at = time.time()
                step.duration_s = step.ended_at - step.started_at
                step.success = False
                step.detail = 'pip install failed: {}'.format(
                    pip_result['result'][-200:])
                return step

            # Now verify
            result = _run_exec(provider, sandbox_ctx, VERIFY_CMD, cwd=base_dir)
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at
            step.success = result['exit_code'] == 0 and 'OK' in result['result']
            step.detail = (
                'runtime pip install needed ({:.1f}s), '
                'then verify={}'
            ).format(pip_dur, 'OK' if step.success else 'FAIL')
        else:
            # For Daytona/Modal: deps should already be in the image
            result = _run_exec(provider, sandbox_ctx, VERIFY_CMD, cwd=base_dir)
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at
            step.success = result['exit_code'] == 0 and 'OK' in result['result']
            step.detail = 'pre-baked deps verify={}, no pip install needed'.format(
                'OK' if step.success else 'FAIL')

    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = 'verify failed: {}'.format(str(e)[:200])

    return step


def _step_run_workload(provider, sandbox_ctx):
    """Step 4: Run a compute workload to verify the custom environment."""
    step = StepProfile(name='docker_run_workload', started_at=time.time())
    base_dir = _get_base_dir(provider)

    try:
        result = _run_exec(provider, sandbox_ctx, WORKLOAD_SCRIPT,
                           cwd=base_dir)
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = (result['exit_code'] == 0
                        and 'WORKLOAD_OK' in result['result'])
        step.detail = result['result'].strip()[-200:]

    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = 'workload failed: {}'.format(str(e)[:200])

    return step


def _step_stock_boot(provider, api_key):
    """Step 5: Boot a sandbox from stock (default) image to measure baseline boot time."""
    step = StepProfile(name='docker_stock_boot', started_at=time.time())

    baseline_sandbox_ctx = None
    try:
        if provider == 'daytona':
            daytona, sandbox = _create_daytona_default_sandbox(api_key)
            baseline_sandbox_ctx = ('daytona', daytona, sandbox)
        elif provider == 'modal':
            app, sandbox = _create_modal_default_sandbox()
            baseline_sandbox_ctx = ('modal', app, sandbox)
        elif provider == 'e2b':
            sandbox = _create_e2b_sandbox(api_key)
            baseline_sandbox_ctx = ('e2b', None, sandbox)
        elif provider == 'blaxel':
            sandbox, loop, name = _create_blaxel_sandbox(api_key)
            baseline_sandbox_ctx = ('blaxel', loop, sandbox, name)

        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = True
        step.detail = 'stock image boot: {:.2f}s'.format(step.duration_s)

    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = 'stock boot failed: {}'.format(str(e)[:200])

    finally:
        if baseline_sandbox_ctx:
            _destroy_sandbox(provider, baseline_sandbox_ctx)

    return step


def _step_comparison(provider, api_key, custom_create_dur, custom_verify_dur,
                     stock_boot_dur):
    """Step 6: Compare custom image vs stock image boot + runtime pip install."""
    step = StepProfile(name='docker_comparison', started_at=time.time())

    if not _supports_image_build(provider):
        step.ended_at = time.time()
        step.duration_s = 0.0
        step.success = True
        step.detail = (
            'stock_boot={:.2f}s, custom_create={:.1f}s, verify(+pip)={:.1f}s'
        ).format(stock_boot_dur, custom_create_dur, custom_verify_dur)
        return step

    # Create a default sandbox + pip install to measure the full baseline
    baseline_sandbox_ctx = None
    try:
        # Create default sandbox
        default_start = time.time()
        if provider == 'daytona':
            daytona, sandbox = _create_daytona_default_sandbox(api_key)
            baseline_sandbox_ctx = ('daytona', daytona, sandbox)
        else:  # modal
            app, sandbox = _create_modal_default_sandbox()
            baseline_sandbox_ctx = ('modal', app, sandbox)
        default_create_dur = time.time() - default_start

        base_dir = _get_base_dir(provider)
        _run_exec(provider, baseline_sandbox_ctx,
                  'mkdir -p {}'.format(base_dir), cwd='/tmp')

        # Pip install on default sandbox
        pip_start = time.time()
        pip_result = _run_exec(
            provider, baseline_sandbox_ctx,
            'pip install {}'.format(' '.join(CUSTOM_DEPS)),
            cwd=base_dir,
        )
        pip_dur = time.time() - pip_start

        # Verify on default sandbox
        verify_result = _run_exec(
            provider, baseline_sandbox_ctx, VERIFY_CMD, cwd=base_dir)
        baseline_total = default_create_dur + pip_dur
        custom_total = custom_create_dur + custom_verify_dur

        if baseline_total > 0 and custom_total > 0:
            speedup = baseline_total / custom_total
        else:
            speedup = 0.0

        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = True
        step.detail = (
            'stock_boot={:.2f}s, baseline(create+pip)={:.1f}s, '
            'custom(create+verify)={:.1f}s, speedup={:.2f}x'
        ).format(stock_boot_dur, baseline_total, custom_total, speedup)

    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = 'comparison failed: {}'.format(str(e)[:200])

    finally:
        # Destroy baseline sandbox
        if baseline_sandbox_ctx:
            _destroy_sandbox(provider, baseline_sandbox_ctx)

    return step


def _destroy_sandbox(provider, sandbox_ctx):
    """Destroy a sandbox created by this benchmark."""
    try:
        if provider == 'daytona':
            _, daytona, sandbox = sandbox_ctx
            daytona.delete(sandbox, timeout=30)
        elif provider == 'modal':
            _, _, sandbox = sandbox_ctx
            sandbox.terminate()
        elif provider == 'e2b':
            _, _, sandbox = sandbox_ctx
            sandbox.kill()
        elif provider == 'blaxel':
            _, loop, sandbox = sandbox_ctx[:3]
            loop.run_until_complete(sandbox.delete())
    except Exception as e:
        print('    [DOCKER] Cleanup error: {}'.format(e))


# ── Main Benchmark Function ──────────────────────────────────────

def run_docker_benchmark(runner, provider, api_key=None):
    """Execute the custom Docker image benchmark.

    NOTE: The `runner` parameter is not used for execution (this benchmark
    creates its own sandboxes with custom images). It is accepted for
    interface consistency.

    Args:
        runner: Unused (for interface consistency). Pass None.
        provider: 'daytona', 'e2b', 'blaxel', or 'modal'.
        api_key: API key for the provider (not needed for Modal).

    Returns:
        list[StepProfile]: Profiling data for each benchmark step.
    """
    steps = []
    sandbox_ctx = None

    print('    [DOCKER] Provider: {} (image build={})'.format(
        provider, 'yes' if _supports_image_build(provider) else 'no'))

    # Step 1: Build custom image
    print('    [DOCKER] Step 1/6: Build custom image...')
    build_step, image = _step_build_image(provider)
    steps.append(build_step)
    print('    [DOCKER]   {:.1f}s - {}'.format(
        build_step.duration_s, build_step.detail))

    # Step 2: Create sandbox from custom image
    print('    [DOCKER] Step 2/6: Create sandbox (custom image)...')
    create_step, sandbox_ctx = _step_create_sandbox(provider, api_key, image)
    steps.append(create_step)
    print('    [DOCKER]   {:.1f}s - {}'.format(
        create_step.duration_s, create_step.detail))

    if not sandbox_ctx:
        print('    [DOCKER] Sandbox creation failed, aborting')
        return steps

    try:
        # Step 3: Verify deps
        print('    [DOCKER] Step 3/6: Verify pre-installed deps...')
        verify_step = _step_verify_deps(provider, sandbox_ctx)
        steps.append(verify_step)
        print('    [DOCKER]   {:.1f}s - {}'.format(
            verify_step.duration_s, verify_step.detail))

        # Step 4: Run workload
        print('    [DOCKER] Step 4/6: Run compute workload...')
        workload_step = _step_run_workload(provider, sandbox_ctx)
        steps.append(workload_step)
        print('    [DOCKER]   {:.1f}s - {}'.format(
            workload_step.duration_s, workload_step.detail))

        # Step 5: Stock image boot time (for comparison)
        print('    [DOCKER] Step 5/6: Stock image boot time...')
        stock_step = _step_stock_boot(provider, api_key)
        steps.append(stock_step)
        print('    [DOCKER]   {:.1f}s - {}'.format(
            stock_step.duration_s, stock_step.detail))

        # Step 6: Compare against baseline
        print('    [DOCKER] Step 6/6: Compare vs baseline...')
        compare_step = _step_comparison(
            provider, api_key,
            custom_create_dur=create_step.duration_s,
            custom_verify_dur=verify_step.duration_s,
            stock_boot_dur=stock_step.duration_s,
        )
        steps.append(compare_step)
        print('    [DOCKER]   {:.1f}s - {}'.format(
            compare_step.duration_s, compare_step.detail))

    finally:
        print('    [DOCKER] Destroying sandbox...')
        _destroy_sandbox(provider, sandbox_ctx)

    return steps
