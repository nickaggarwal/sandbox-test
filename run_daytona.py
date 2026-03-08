#!/usr/bin/env python3
"""
Main entry point: Run the RL agent inside a Daytona sandbox.

1. Creates a Daytona sandbox
2. Uploads the project
3. Installs dependencies
4. Runs Django tests
5. Runs the RL training agent
6. Retrieves results
"""
import argparse
import json
import os
import sys

from daytona_sandbox import DaytonaSandboxRunner

DAYTONA_API_KEY = os.environ.get('DAYTONA_API_KEY', '')


def run_in_sandbox(
    episodes: int = 50,
    max_steps: int = 20,
    project_dir: str = '.',
):
    """Execute the full pipeline inside a Daytona sandbox."""

    print("=" * 70)
    print("Daytona Sandbox - RL Scheduling Agent")
    print("=" * 70)

    runner = DaytonaSandboxRunner(api_key=DAYTONA_API_KEY)

    try:
        # Step 1: Create sandbox
        print("\n[1/6] Creating Daytona sandbox...")
        runner.create_sandbox()

        # Step 2: Upload project
        print("\n[2/6] Uploading project files...")
        runner.upload_project(project_dir)

        # Step 3: Setup environment
        print("\n[3/6] Installing dependencies...")
        setup_results = runner.setup_environment()
        for r in setup_results:
            if r['exit_code'] != 0:
                print(f"  WARNING: Command failed with exit code {r['exit_code']}")

        # Step 4: Verify structure
        print("\n[4/6] Verifying project structure...")
        verify = runner.exec('ls -la')
        print(verify['result'][:500])
        verify2 = runner.exec('ls -la scheduling/')
        print(verify2['result'][:500])

        # Step 5: Run tests
        print("\n[5/6] Running Django tests...")
        test_result = runner.run_tests()
        print(f"  Tests exit code: {test_result['exit_code']}")
        output = test_result['result']
        if output:
            lines = output.strip().split('\n')
            for line in lines[-15:]:
                print(f"  {line}")

        # Step 6: Run RL training
        print(f"\n[6/6] Running RL training ({episodes} episodes, {max_steps} steps)...")
        rl_result = runner.run_rl_training(
            episodes=episodes, max_steps=max_steps,
        )
        print(f"  RL exit code: {rl_result['exit_code']}")
        output = rl_result['result']
        if output:
            lines = output.strip().split('\n')
            for line in lines[-35:]:
                print(f"  {line}")

        # Retrieve results
        print("\n[*] Retrieving training results...")
        try:
            results_json = runner.download_file('/home/daytona/app/rl_output/training_results.json')
            results = json.loads(results_json)

            os.makedirs('rl_output', exist_ok=True)
            with open('rl_output/sandbox_results.json', 'w') as f:
                json.dump(results, f, indent=2)

            print(f"\n{'='*70}")
            print("RESULTS FROM DAYTONA SANDBOX")
            print(f"{'='*70}")
            print(f"Best reward: {results.get('best_reward', 'N/A')}")
            print(f"Best actions: {results.get('best_actions', 'N/A')}")
            print(f"Final avg reward (last 10): {results.get('final_avg_reward', 'N/A')}")
            print(f"Results saved to: rl_output/sandbox_results.json")
            print(f"{'='*70}")

            return results

        except Exception as e:
            print(f"  Could not retrieve results: {e}")
            return {'error': str(e), 'rl_output': output}

    finally:
        print("\n[*] Cleaning up sandbox...")
        runner.destroy()


def run_locally(
    episodes: int = 50,
    max_steps: int = 20,
    project_dir: str = '.',
):
    """Run the RL agent locally (fallback)."""
    print("=" * 70)
    print("Local Execution - RL Scheduling Agent")
    print("=" * 70)

    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'calendly_project.settings')

    import django
    django.setup()

    from run_rl_agent import train

    results = train(
        episodes=episodes,
        max_steps=max_steps,
        project_dir=project_dir,
    )
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Run RL scheduling agent in Daytona sandbox'
    )
    parser.add_argument(
        '--mode', choices=['sandbox', 'local'], default='sandbox',
        help='Run in Daytona sandbox or locally',
    )
    parser.add_argument('--episodes', type=int, default=50)
    parser.add_argument('--max-steps', type=int, default=20)
    parser.add_argument('--project-dir', default='.')

    args = parser.parse_args()

    if args.mode == 'sandbox':
        run_in_sandbox(
            episodes=args.episodes,
            max_steps=args.max_steps,
            project_dir=args.project_dir,
        )
    else:
        run_locally(
            episodes=args.episodes,
            max_steps=args.max_steps,
            project_dir=args.project_dir,
        )
