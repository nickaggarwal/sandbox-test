#!/usr/bin/env python3
"""
LLM-Powered Coding Agent with Multi-Objective Reward.

Uses Google Gemini or Claude (via Vertex AI) to generate/improve code,
executes and tests it in a cloud sandbox, then scores the result using
the same reward function as the RL agent.

The agent loop:
1. Read the current code + reward breakdown
2. Ask the LLM to generate improved code
3. Upload the code to the sandbox
4. Run tests in the sandbox
5. Compute multi-objective reward (correctness, quality, domain)
6. Feed the reward breakdown back to the LLM
7. Repeat until reward threshold met or max iterations reached

Supported LLM backends:
  --llm gemini           Uses Gemini API (GEMINI_API_KEY)
  --llm vertex-claude    Uses Claude via Vertex AI (gcloud auth)

Bootstrap mode:
  Use --bootstrap-app to generate the full Django app in sandbox
  without uploading the local core app.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

import requests

# Reward functions — static analysis runs locally on code text
from rewards.reward_functions import (
    RewardBreakdown,
    RewardCalculator,
    SyntaxReward,
    ZeroConflictReward,
    ReadabilityReward,
    ModularityReward,
    EfficiencyPenalty,
    BufferEnforcementReward,
    TimezoneConsistencyReward,
    AvailabilityMaskingReward,
)


# Load .env
def _load_env():
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, val = line.split('=', 1)
                    os.environ.setdefault(key.strip(), val.strip())

_load_env()


# ── Sandbox Provider Factory ──────────────────────────────────────────

def create_runner(provider):
    """Create a sandbox runner for the given provider."""
    if provider == 'daytona':
        from daytona_sandbox import DaytonaSandboxRunner
        return DaytonaSandboxRunner(api_key=os.environ.get('DAYTONA_API_KEY', ''))
    elif provider == 'e2b':
        from e2b_sandbox import E2BSandboxRunner
        return E2BSandboxRunner(api_key=os.environ.get('E2B_API_KEY', ''))
    elif provider == 'blaxel':
        from blaxel_sandbox import BlaxelSandboxRunner
        return BlaxelSandboxRunner(api_key=os.environ.get('BLAXEL_API_KEY', ''))
    elif provider == 'modal':
        from modal_sandbox import ModalSandboxRunner
        return ModalSandboxRunner()
    else:
        raise ValueError('Unknown provider: {}'.format(provider))


def get_working_dir(provider):
    """Get the remote working directory for a provider."""
    dirs = {
        'daytona': '/home/daytona/app',
        'e2b': '/home/user/app',
        'blaxel': '/blaxel/app',
        'modal': '/root/app',
    }
    return dirs.get(provider, '/root/app')


# ── LLM Clients ──────────────────────────────────────────────────────

class GeminiClient:
    """Google Gemini REST API client."""

    ENDPOINT = (
        'https://generativelanguage.googleapis.com/v1beta'
        '/models/{model}:generateContent'
    )

    def __init__(self, api_key=None, model='gemini-2.5-flash-lite'):
        self.api_key = api_key or os.environ.get('GEMINI_API_KEY', '')
        self.model = model
        if not self.api_key:
            raise ValueError(
                'Gemini API key required. Set GEMINI_API_KEY in .env or pass --api-key.'
            )

    @property
    def name(self):
        return 'gemini/{}'.format(self.model)

    def generate(self, prompt, system_prompt=''):
        """Call Gemini generateContent and return the text response."""
        url = self.ENDPOINT.format(model=self.model)

        contents = []
        if system_prompt:
            contents.append({
                'role': 'user',
                'parts': [{'text': system_prompt}],
            })
            contents.append({
                'role': 'model',
                'parts': [{'text': 'Understood. I will follow these instructions.'}],
            })

        contents.append({
            'role': 'user',
            'parts': [{'text': prompt}],
        })

        body = {
            'contents': contents,
            'generationConfig': {
                'maxOutputTokens': 8192,
                'temperature': 0.2,
            },
        }

        resp = requests.post(
            url,
            params={'key': self.api_key},
            headers={'Content-Type': 'application/json'},
            json=body,
            timeout=120,
        )

        if resp.status_code != 200:
            raise RuntimeError(
                'Gemini API error {}: {}'.format(resp.status_code, resp.text[:500])
            )

        data = resp.json()
        candidates = data.get('candidates', [])
        if not candidates:
            raise RuntimeError('Gemini returned no candidates: {}'.format(
                json.dumps(data)[:500]))

        parts = candidates[0].get('content', {}).get('parts', [])
        return ''.join(p.get('text', '') for p in parts)


class VertexClaudeClient:
    """Claude via Google Cloud Vertex AI."""

    ENDPOINT = (
        'https://{endpoint}/v1/projects/{project}/locations/{location}'
        '/publishers/anthropic/models/{model}:rawPredict'
    )

    def __init__(self, model='claude-sonnet-4-20250514', project_id=None,
                 location=None):
        self.model = model
        self.project_id = project_id or os.environ.get(
            'VERTEX_PROJECT_ID', 'my-kube-project-425518')
        self.location = location or os.environ.get('VERTEX_LOCATION', 'global')
        self.endpoint = os.environ.get(
            'VERTEX_ENDPOINT', 'aiplatform.googleapis.com')

    @property
    def name(self):
        return 'vertex-claude/{}'.format(self.model)

    def _get_access_token(self):
        """Get a GCP access token via gcloud CLI."""
        result = subprocess.run(
            ['gcloud', 'auth', 'print-access-token'],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(
                'gcloud auth failed: {}'.format(result.stderr.strip()))
        return result.stdout.strip()

    def generate(self, prompt, system_prompt=''):
        """Call Claude via Vertex AI and return the text response."""
        token = self._get_access_token()

        url = self.ENDPOINT.format(
            endpoint=self.endpoint,
            project=self.project_id,
            location=self.location,
            model=self.model,
        )

        messages = [{'role': 'user', 'content': prompt}]

        body = {
            'anthropic_version': 'vertex-2023-10-16',
            'max_tokens': 8192,
            'temperature': 0.2,
            'messages': messages,
        }
        if system_prompt:
            body['system'] = system_prompt

        resp = requests.post(
            url,
            headers={
                'Authorization': 'Bearer {}'.format(token),
                'Content-Type': 'application/json; charset=utf-8',
            },
            json=body,
            timeout=120,
        )

        if resp.status_code != 200:
            raise RuntimeError(
                'Vertex AI error {}: {}'.format(resp.status_code, resp.text[:500])
            )

        data = resp.json()
        content = data.get('content', [])
        text_parts = [c['text'] for c in content if c.get('type') == 'text']
        return ''.join(text_parts)


def create_llm_client(llm_backend, model=None, api_key=None):
    """Factory for LLM clients."""
    if llm_backend == 'gemini':
        return GeminiClient(
            api_key=api_key,
            model=model or 'gemini-2.5-flash-lite',
        )
    elif llm_backend == 'vertex-claude':
        return VertexClaudeClient(
            model=model or 'claude-sonnet-4-20250514',
        )
    else:
        raise ValueError('Unknown LLM backend: {}'.format(llm_backend))


# ── Reward Evaluation ────────────────────────────────────────────────

def compute_reward(code, file_contents, test_output):
    """Compute the multi-objective reward from code text + sandbox test output.

    Static analysis (syntax, patterns, readability) runs locally.
    Test pass rate is parsed from the sandbox test output string.
    """
    breakdown = RewardBreakdown()

    # 1. Functional Correctness
    breakdown.syntax_success = SyntaxReward.evaluate(code)
    breakdown.zero_conflict = ZeroConflictReward.evaluate(code)

    # Parse test results from sandbox output
    breakdown.test_pass_rate = _parse_test_score(test_output)

    # 2. Code Quality
    breakdown.readability = ReadabilityReward.evaluate(code)
    breakdown.modularity = ModularityReward.evaluate(file_contents)
    breakdown.efficiency_penalty = EfficiencyPenalty.evaluate(code)

    # 3. Domain-Specific
    breakdown.buffer_enforcement = BufferEnforcementReward.evaluate(code)
    breakdown.timezone_consistency = TimezoneConsistencyReward.evaluate(code)
    breakdown.availability_masking = AvailabilityMaskingReward.evaluate(code)

    return breakdown


def _parse_test_score(test_output):
    """Parse test pass rate (0-10) from Django/pytest test output."""
    if not test_output:
        return 0.0

    # Django format: "Ran 29 tests in 5.982s" + "OK" or "FAILED (failures=N, errors=M)"
    ran_match = re.search(r'Ran (\d+) test', test_output)
    if ran_match:
        total = int(ran_match.group(1))
        if total == 0:
            return 0.0

        # Check for failures/errors
        fail_match = re.search(
            r'FAILED.*?failures=(\d+)', test_output)
        err_match = re.search(
            r'FAILED.*?errors=(\d+)', test_output)
        failures = int(fail_match.group(1)) if fail_match else 0
        errors = int(err_match.group(1)) if err_match else 0
        passed = total - failures - errors

        if 'OK' in test_output and failures == 0 and errors == 0:
            passed = total

        return (max(0, passed) / total) * 10.0

    # pytest format: "X passed, Y failed"
    pytest_match = re.search(r'(\d+) passed', test_output)
    pytest_fail = re.search(r'(\d+) failed', test_output)
    if pytest_match:
        passed = int(pytest_match.group(1))
        failed = int(pytest_fail.group(1)) if pytest_fail else 0
        total = passed + failed
        if total > 0:
            return (passed / total) * 10.0

    return 0.0


def format_reward_for_llm(breakdown):
    """Format reward breakdown as text for the LLM prompt."""
    return (
        'REWARD BREAKDOWN (max ~33 total):\n'
        '  CORRECTNESS (weight 1.0):\n'
        '    Syntax valid:      {:.1f}/1\n'
        '    Test pass rate:    {:.1f}/10\n'
        '    Conflict detect:   {:.1f}/5\n'
        '  CODE QUALITY (weight 0.5):\n'
        '    Readability:       {:.1f}/2\n'
        '    Modularity:        {:.1f}/3\n'
        '    Efficiency:        {:.1f}\n'
        '  DOMAIN LOGIC (weight 0.8):\n'
        '    Buffer enforce:    {:.1f}/4\n'
        '    Timezone handling: {:.1f}/5\n'
        '    Avail masking:     {:.1f}/3\n'
        '  TOTAL REWARD: {:.2f}\n'
    ).format(
        breakdown.syntax_success,
        breakdown.test_pass_rate,
        breakdown.zero_conflict,
        breakdown.readability,
        breakdown.modularity,
        breakdown.efficiency_penalty,
        breakdown.buffer_enforcement,
        breakdown.timezone_consistency,
        breakdown.availability_masking,
        breakdown.total_reward,
    )


# ── Coding Agent ─────────────────────────────────────────────────────

class CodingAgent:
    """LLM-powered coding agent with multi-objective reward scoring."""

    def __init__(
        self,
        provider='e2b',
        llm_backend='gemini',
        model=None,
        max_iterations=5,
        api_key=None,
        reward_threshold=25.0,
        bootstrap_app=False,
    ):
        self.provider = provider
        self.max_iterations = max_iterations
        self.reward_threshold = reward_threshold
        self.bootstrap_app = bootstrap_app
        self.llm = create_llm_client(llm_backend, model=model, api_key=api_key)
        self.runner = None
        self.work_dir = get_working_dir(provider)

    def _extract_code_blocks(self, text):
        """Extract Python code blocks from LLM response."""
        blocks = []
        in_block = False
        current = []
        for line in text.split('\n'):
            fence = line.strip().lower()
            if fence.startswith('```python') or fence.startswith('```py'):
                in_block = True
                current = []
            elif line.strip() == '```' and in_block:
                in_block = False
                blocks.append('\n'.join(current))
            elif in_block:
                current.append(line)
        return blocks

    def setup_sandbox(self):
        """Create sandbox and install base dependencies."""
        print('[Agent] Creating {} sandbox...'.format(self.provider))
        self.runner = create_runner(self.provider)
        self.runner.create_sandbox()
        print('[Agent] Sandbox ready: {}'.format(self.runner.sandbox_id))

        self.runner.exec('mkdir -p {}'.format(self.work_dir), cwd='/tmp', timeout=60)

        if self.bootstrap_app:
            print('[Agent] Clearing workspace for bootstrap mode...')
            if self.work_dir.startswith('/'):
                self.runner.exec(
                    'rm -rf {}/*'.format(self.work_dir),
                    cwd='/tmp',
                    timeout=60,
                )

        print('[Agent] Installing base dependencies...')
        result = self.runner.exec(
            'pip install django djangorestframework pytz',
            cwd=self.work_dir,
            timeout=180,
        )
        print('[Agent] pip install exit code: {}'.format(result['exit_code']))

    def upload_code(self, filename, code):
        """Upload a code file to the sandbox."""
        remote_path = '{}/{}'.format(self.work_dir, filename)
        parent = os.path.dirname(remote_path)
        if parent:
            self.runner.exec('mkdir -p {}'.format(parent), cwd='/tmp', timeout=60)
        self.runner.upload_file_native(code, remote_path)

    def run_tests(self, test_command):
        """Run tests in the sandbox and return results."""
        result = self.runner.exec(test_command, cwd=self.work_dir, timeout=120)
        return {
            'exit_code': result['exit_code'],
            'output': result['result'],
            'passed': result['exit_code'] == 0,
        }

    def generate_code(self, task, context='', reward_text='', test_output=''):
        """Ask the LLM to generate or fix code for a task."""
        system = (
            "You are an expert Python developer building a Calendly-like "
            "scheduling engine. Generate clean, working code.\n"
            "Always include code in ```python blocks.\n"
            "Each code block MUST start with a first-line filename marker like:\n"
            "# filename: scheduling/engine.py\n"
            "Return full file contents for each file you provide.\n"
            "You are scored on a multi-objective reward function:\n"
            "- Correctness: syntax validity, test pass rate, conflict detection\n"
            "- Quality: readability (PEP8, docstrings), modularity, efficiency\n"
            "- Domain: buffer enforcement, timezone handling, availability masking\n"
            "Maximize the total reward by improving the weakest scoring areas."
        )

        prompt = 'Task: {}\n'.format(task)
        if context:
            prompt += '\nExisting code:\n```python\n{}\n```\n'.format(context)
        if reward_text:
            prompt += '\n{}\n'.format(reward_text)
        if test_output:
            prompt += '\nTest output (last 2000 chars):\n```\n{}\n```\n'.format(
                test_output[-2000:])
            prompt += (
                '\nAnalyze the reward breakdown above. Focus on improving '
                'the lowest-scoring areas. Fix any test failures.'
            )

        response = self.llm.generate(prompt, system_prompt=system)
        return response, self._extract_code_blocks(response)

    def iterate(self, task, files=None, test_command='python manage.py test scheduling --verbosity=2'):
        """Main agent loop: generate -> test -> score -> repeat."""
        files = files or {}
        current_code = dict(files)
        results = []
        effective_task = task

        if self.bootstrap_app and not current_code:
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

        print('\n' + '=' * 70)
        print('  CODING AGENT (with reward function)')
        print('  Sandbox:          {}'.format(self.provider))
        print('  LLM:              {}'.format(self.llm.name))
        print('  Task:             {}'.format(task[:60]))
        print('  Max iterations:   {}'.format(self.max_iterations))
        print('  Reward threshold: {:.1f}'.format(self.reward_threshold))
        print('  Bootstrap app:    {}'.format(self.bootstrap_app))
        print('=' * 70 + '\n')

        best_reward = 0.0
        best_code = {}

        for i in range(self.max_iterations):
            print('[Iter {}/{}] Generating code via {}...'.format(
                i + 1, self.max_iterations, self.llm.name))

            # Build context from current files
            context = ''
            for fname, code in current_code.items():
                context += '# --- {} ---\n{}\n\n'.format(fname, code)

            # Get previous reward and test output
            reward_text = ''
            test_output = ''
            if results:
                reward_text = results[-1].get('reward_text', '')
                test_output = results[-1].get('test_output', '')

            # Ask LLM
            response_text, code_blocks = self.generate_code(
                effective_task,
                context=context,
                reward_text=reward_text,
                test_output=test_output,
            )

            print('[Iter {}/{}] LLM returned {} code blocks'.format(
                i + 1, self.max_iterations, len(code_blocks)))

            # Upload generated code
            for j, code in enumerate(code_blocks):
                filename = 'generated_{}.py'.format(j) if j > 0 else 'solution.py'
                header_match = re.search(
                    r'^\s*#\s*filename\s*:\s*(.+)$',
                    code,
                    flags=re.IGNORECASE | re.MULTILINE,
                )
                if header_match:
                    filename = header_match.group(1).strip()

                print('  Uploading: {}'.format(filename))
                self.upload_code(filename, code)
                current_code[filename] = code

            # Run tests in sandbox
            print('[Iter {}/{}] Running tests...'.format(
                i + 1, self.max_iterations))
            test_result = self.run_tests(test_command)

            # Compute reward (static analysis locally, test score from output)
            all_code = '\n\n'.join(current_code.values())
            breakdown = compute_reward(
                code=all_code,
                file_contents=current_code,
                test_output=test_result['output'],
            )
            reward_display = format_reward_for_llm(breakdown)

            # Track best
            if breakdown.total_reward > best_reward:
                best_reward = breakdown.total_reward
                best_code = dict(current_code)

            iteration = {
                'iteration': i + 1,
                'code_blocks': len(code_blocks),
                'test_passed': test_result['passed'],
                'test_exit_code': test_result['exit_code'],
                'test_output': test_result['output'],
                'reward_breakdown': breakdown.to_dict(),
                'reward_total': breakdown.total_reward,
                'reward_text': reward_display,
                'llm_response_length': len(response_text),
            }
            results.append(iteration)

            # Print reward summary
            status = 'PASS' if test_result['passed'] else 'FAIL'
            print('[Iter {}/{}] Tests: {}  |  Reward: {:.2f}  |  Best: {:.2f}'.format(
                i + 1, self.max_iterations, status,
                breakdown.total_reward, best_reward))
            print('  Correctness: {:.1f}  Quality: {:.1f}  Domain: {:.1f}'.format(
                breakdown.correctness_total,
                breakdown.quality_total,
                breakdown.domain_total))

            # Show last few lines of test output
            lines = test_result['output'].strip().split('\n')
            for line in lines[-3:]:
                print('  {}'.format(line))

            # Check if reward threshold met
            if breakdown.total_reward >= self.reward_threshold:
                print('\n[Agent] Reward threshold {:.1f} reached on iteration {}!'.format(
                    self.reward_threshold, i + 1))
                break

            if test_result['passed'] and breakdown.total_reward >= self.reward_threshold * 0.8:
                print('\n[Agent] Tests pass and reward {:.1f} is close to threshold.'.format(
                    breakdown.total_reward))
                break
        else:
            print('\n[Agent] Max iterations reached. Best reward: {:.2f}'.format(
                best_reward))

        return {
            'task': task,
            'provider': self.provider,
            'llm': self.llm.name,
            'iterations': results,
            'total_iterations': len(results),
            'best_reward': best_reward,
            'final_reward': results[-1]['reward_total'] if results else 0,
            'final_passed': results[-1]['test_passed'] if results else False,
            'files': current_code,
            'best_files': best_code,
        }

    def cleanup(self):
        """Destroy the sandbox."""
        if self.runner:
            print('[Agent] Cleaning up sandbox...')
            self.runner.destroy()

    def __enter__(self):
        self.setup_sandbox()
        return self

    def __exit__(self, *args):
        self.cleanup()
        return False


# ── Entry Points ─────────────────────────────────────────────────────

def run_coding_agent(
    task,
    provider='e2b',
    llm_backend='gemini',
    model=None,
    max_iterations=5,
    test_command='python manage.py test scheduling --verbosity=2',
    files=None,
    project_dir=None,
    api_key=None,
    reward_threshold=25.0,
    bootstrap_app=False,
):
    """Run the full coding agent pipeline with reward scoring."""
    bootstrap_app = bootstrap_app or (project_dir is None)

    with CodingAgent(
        provider=provider,
        llm_backend=llm_backend,
        model=model,
        max_iterations=max_iterations,
        api_key=api_key,
        reward_threshold=reward_threshold,
        bootstrap_app=bootstrap_app,
    ) as agent:
        if project_dir:
            print('[Agent] Uploading project from {}...'.format(project_dir))
            agent.runner.upload_project(project_dir)
        elif bootstrap_app:
            print('[Agent] Bootstrap mode enabled (no core app upload).')

        result = agent.iterate(
            task=task,
            files=files,
            test_command=test_command,
        )

        os.makedirs('rl_output', exist_ok=True)
        report_path = 'rl_output/coding_agent_report.json'
        with open(report_path, 'w') as f:
            json.dump(result, f, indent=2, default=str)
        print('\n[Agent] Report saved to: {}'.format(report_path))

        # Print final summary
        print('\n' + '=' * 70)
        print('  FINAL RESULTS')
        print('=' * 70)
        print('  Iterations:   {}'.format(result['total_iterations']))
        print('  Best reward:  {:.2f}'.format(result['best_reward']))
        print('  Final reward: {:.2f}'.format(result['final_reward']))
        print('  Tests passed: {}'.format(result['final_passed']))
        print('=' * 70)

        return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='LLM coding agent with multi-objective reward scoring'
    )
    parser.add_argument(
        'task',
        help='Description of the coding task',
    )
    parser.add_argument(
        '--provider',
        choices=['daytona', 'e2b', 'blaxel', 'modal'],
        default='e2b',
    )
    parser.add_argument(
        '--llm',
        choices=['gemini', 'vertex-claude'],
        default='gemini',
        help='LLM backend: gemini (API key) or vertex-claude (gcloud auth)',
    )
    parser.add_argument(
        '--model',
        help='Model name (default: gemini-2.5-flash-lite or claude-sonnet-4-20250514)',
    )
    parser.add_argument(
        '--max-iterations', type=int, default=5,
        help='Max generate-test-fix cycles',
    )
    parser.add_argument(
        '--test-command',
        default='python manage.py test scheduling --verbosity=2',
        help='Command to run tests in the sandbox',
    )
    parser.add_argument(
        '--project-dir',
        help='Local project directory to upload to sandbox',
    )
    parser.add_argument(
        '--api-key',
        help='API key for the LLM (or set in .env)',
    )
    parser.add_argument(
        '--reward-threshold', type=float, default=25.0,
        help='Stop when total reward reaches this value',
    )
    parser.add_argument(
        '--bootstrap-app',
        action='store_true',
        help='Generate the Django app from scratch in sandbox (no local core app upload).',
    )

    args = parser.parse_args()

    run_coding_agent(
        task=args.task,
        provider=args.provider,
        llm_backend=args.llm,
        model=args.model,
        max_iterations=args.max_iterations,
        test_command=args.test_command,
        project_dir=args.project_dir,
        api_key=args.api_key,
        reward_threshold=args.reward_threshold,
        bootstrap_app=args.bootstrap_app,
    )
