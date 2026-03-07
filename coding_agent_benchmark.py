"""
Coding Agent Benchmark for Sandbox Profiling.

Runs the real LLM-powered CodingAgent (from coding_agent.py) through its
generate -> test -> score -> fix loop, profiling each iteration as a
separate StepProfile.

Unlike iteration_loop_benchmark.py (which uses pre-written broken/fixed code
to measure pure sandbox I/O speed), this benchmark includes actual LLM
inference time and measures end-to-end agent performance.

Steps:
1. agent_setup       -- Create sandbox, install Django deps
2. agent_upload_project -- Upload calendly Django project (if not bootstrap)
3. agent_iteration_1..N -- Each LLM iteration: generate code, upload, test, score
4. agent_final_result   -- Summary: total iterations, best reward, pass/fail
"""
import os
import re
import time

from run_parallel_profiled import StepProfile

from coding_agent import (
    CodingAgent,
    compute_reward,
    format_reward_for_llm,
    get_working_dir,
)

DEFAULT_TASK = (
    "Build a Calendly-like scheduling engine with conflict detection, "
    "timezone handling, and buffer enforcement between meetings. "
    "The engine should support creating users, defining availability windows, "
    "booking appointments with conflict checking, and enforcing configurable "
    "buffer times between consecutive meetings."
)


def run_coding_agent_benchmark(
    runner,
    provider,
    project_dir='.',
    llm_backend='gemini',
    llm_model=None,
    llm_api_key=None,
    max_iterations=3,
    reward_threshold=25.0,
    bootstrap_app=False,
    task=None,
):
    """Execute the coding agent benchmark with per-iteration profiling.

    Args:
        runner: Unused (agent manages its own sandbox). Kept for API compat.
        provider: 'daytona', 'e2b', 'blaxel', or 'modal'.
        project_dir: Local project directory to upload (default '.').
        llm_backend: 'gemini' or 'vertex-claude'.
        llm_model: Optional model name override.
        llm_api_key: Optional API key (falls back to env vars).
        max_iterations: Max generate-test-fix cycles.
        reward_threshold: Stop when total reward reaches this.
        bootstrap_app: If True, LLM generates the app from scratch.
        task: Optional task description override.

    Returns:
        list[StepProfile]: Profiling data for each benchmark step.
    """
    task = task or DEFAULT_TASK
    test_command = 'python manage.py test scheduling --verbosity=2'
    steps = []

    agent = CodingAgent(
        provider=provider,
        llm_backend=llm_backend,
        model=llm_model,
        max_iterations=max_iterations,
        api_key=llm_api_key,
        reward_threshold=reward_threshold,
        bootstrap_app=bootstrap_app,
    )

    # ── Step 1: Setup sandbox ──
    print('    [AGENT] Step 1: Setting up sandbox...')
    step = StepProfile(name='agent_setup', started_at=time.time())
    try:
        agent.setup_sandbox()
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = True
        step.detail = 'sandbox={}, provider={}, llm={}'.format(
            agent.runner.sandbox_id, provider, agent.llm.name)
    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = 'setup failed: {}'.format(str(e)[:200])
        steps.append(step)
        return steps
    steps.append(step)
    print('    [AGENT]   {:.1f}s - {}'.format(step.duration_s, step.detail))

    # ── Step 2: Upload project ──
    print('    [AGENT] Step 2: Uploading project...')
    step = StepProfile(name='agent_upload_project', started_at=time.time())
    try:
        if bootstrap_app:
            step.detail = 'bootstrap mode (no project upload)'
        else:
            agent.runner.upload_project(project_dir)
            step.detail = 'uploaded from {}'.format(project_dir)
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = True
    except Exception as e:
        step.ended_at = time.time()
        step.duration_s = step.ended_at - step.started_at
        step.success = False
        step.detail = 'upload failed: {}'.format(str(e)[:200])
    steps.append(step)
    print('    [AGENT]   {:.1f}s - {}'.format(step.duration_s, step.detail))

    # ── Steps 3..N: Iteration loop ──
    current_code = {}
    best_reward = 0.0
    best_code = {}
    last_reward_text = ''
    last_test_output = ''

    effective_task = task
    if bootstrap_app and not current_code:
        effective_task = (
            task
            + '\n\nBootstrap requirements:\n'
            + '- Build the full Django app from scratch in this sandbox.\n'
            + '- Create at least these files: manage.py, '
            + 'calendly_project/settings.py, calendly_project/urls.py, '
            + 'calendly_project/asgi.py, calendly_project/wsgi.py, '
            + 'scheduling/models.py, scheduling/engine.py, '
            + 'scheduling/serializers.py, scheduling/views.py, '
            + 'scheduling/urls.py, scheduling/tests.py.\n'
            + '- Ensure imports and Django settings allow '
            + '`python manage.py test scheduling` to run.\n'
            + '- Every file must be in its own python code block with '
            + 'a `# filename: <path>` first line.\n'
        )

    print('\n    [AGENT] Starting {} iterations (llm={}, threshold={})...'.format(
        max_iterations, agent.llm.name, reward_threshold))

    for i in range(max_iterations):
        iter_num = i + 1
        step_name = 'agent_iteration_{}'.format(iter_num)
        print('    [AGENT] Step {}: Iteration {}/{}...'.format(
            2 + iter_num, iter_num, max_iterations))

        step = StepProfile(name=step_name, started_at=time.time())

        try:
            # Build context from current files
            context = ''
            for fname, code in current_code.items():
                context += '# --- {} ---\n{}\n\n'.format(fname, code)

            # Ask LLM to generate code
            llm_start = time.time()
            response_text, code_blocks = agent.generate_code(
                effective_task,
                context=context,
                reward_text=last_reward_text,
                test_output=last_test_output,
            )
            llm_time = time.time() - llm_start

            print('    [AGENT]   LLM returned {} blocks in {:.1f}s'.format(
                len(code_blocks), llm_time))

            # Upload generated code
            upload_start = time.time()
            for j, code in enumerate(code_blocks):
                filename = 'generated_{}.py'.format(j) if j > 0 else 'solution.py'
                header_match = re.search(
                    r'^\s*#\s*filename\s*:\s*(.+)$',
                    code,
                    flags=re.IGNORECASE | re.MULTILINE,
                )
                if header_match:
                    filename = header_match.group(1).strip()
                agent.upload_code(filename, code)
                current_code[filename] = code
            upload_time = time.time() - upload_start

            # Run tests
            test_start = time.time()
            test_result = agent.run_tests(test_command)
            test_time = time.time() - test_start
            last_test_output = test_result['output']

            # Compute reward
            all_code = '\n\n'.join(current_code.values())
            breakdown = compute_reward(
                code=all_code,
                file_contents=current_code,
                test_output=test_result['output'],
            )
            last_reward_text = format_reward_for_llm(breakdown)

            if breakdown.total_reward > best_reward:
                best_reward = breakdown.total_reward
                best_code = dict(current_code)

            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at
            step.success = True

            status = 'PASS' if test_result['passed'] else 'FAIL'
            step.detail = (
                'tests={}, reward={:.1f}, best={:.1f}, '
                'llm={:.1f}s, upload={:.1f}s, test={:.1f}s, '
                'blocks={}'
            ).format(
                status, breakdown.total_reward, best_reward,
                llm_time, upload_time, test_time,
                len(code_blocks),
            )

            print('    [AGENT]   {:.1f}s - {}'.format(
                step.duration_s, step.detail))

            # Check if threshold met
            if breakdown.total_reward >= reward_threshold:
                print('    [AGENT]   Reward threshold {:.1f} reached!'.format(
                    reward_threshold))
                steps.append(step)
                break

            if (test_result['passed'] and
                    breakdown.total_reward >= reward_threshold * 0.8):
                print('    [AGENT]   Tests pass, reward {:.1f} near threshold.'.format(
                    breakdown.total_reward))
                steps.append(step)
                break

        except Exception as e:
            step.ended_at = time.time()
            step.duration_s = step.ended_at - step.started_at
            step.success = False
            step.detail = 'iteration {} failed: {}'.format(iter_num, str(e)[:200])
            print('    [AGENT]   {:.1f}s - {}'.format(
                step.duration_s, step.detail))

        steps.append(step)

    # ── Final result summary ──
    step = StepProfile(name='agent_final_result', started_at=time.time())
    iterations_run = sum(1 for s in steps if s.name.startswith('agent_iteration_'))
    step.ended_at = time.time()
    step.duration_s = 0.0
    step.success = best_reward >= reward_threshold * 0.5
    step.detail = 'iterations={}, best_reward={:.1f}, threshold={:.1f}, llm={}'.format(
        iterations_run, best_reward, reward_threshold, agent.llm.name)
    steps.append(step)
    print('    [AGENT] Final: {}'.format(step.detail))

    # Cleanup
    try:
        agent.cleanup()
    except Exception:
        pass

    return steps
