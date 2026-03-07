#!/usr/bin/env python3
"""
RL Agent for generating/improving Calendly-like scheduling code.

Uses a simple policy gradient (REINFORCE) algorithm with the
Gymnasium environment to learn which code modifications produce
the highest reward.

Improvements:
- Epsilon-greedy exploration schedule
- Difficulty curriculum for skeleton generator
- Proper env.close() cleanup
- Better logging and result tracking

Can run locally or inside a Daytona/E2B sandbox.
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime

import numpy as np

# Ensure Django is set up before importing models
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'calendly_project.settings')

import django
django.setup()

import gymnasium as gym

# Import our custom environment (triggers registration)
from rewards.gym_env import SchedulingCodeEnv, CODE_ACTIONS


class RLAgent:
    """
    Simple policy-gradient agent (REINFORCE with baseline).

    The agent learns a softmax policy over discrete code actions.
    Includes epsilon-greedy exploration for better coverage.
    """

    def __init__(
        self,
        n_actions: int,
        obs_dim: int,
        learning_rate: float = 0.01,
        gamma: float = 0.99,
        seed: int = 42,
        epsilon_start: float = 0.5,
        epsilon_end: float = 0.05,
        epsilon_decay_episodes: int = 40,
    ):
        self.n_actions = n_actions
        self.obs_dim = obs_dim
        self.lr = learning_rate
        self.gamma = gamma
        self.rng = np.random.RandomState(seed)

        # Epsilon-greedy exploration schedule
        self.epsilon_start = epsilon_start
        self.epsilon_end = epsilon_end
        self.epsilon_decay_episodes = epsilon_decay_episodes
        self.current_epsilon = epsilon_start

        # Linear policy: weights map observation -> action preferences
        self.weights = self.rng.randn(obs_dim, n_actions) * 0.01
        self.bias = np.zeros(n_actions)

        # Baseline (moving average of returns)
        self.baseline = 0.0
        self.baseline_alpha = 0.1

        # Training history
        self.episode_rewards = []
        self.episode_lengths = []
        self.best_reward = float('-inf')
        self.best_actions = []
        self.action_counts = defaultdict(int)

    def _softmax(self, x: np.ndarray) -> np.ndarray:
        """Numerically stable softmax."""
        x = x - np.max(x)
        exp_x = np.exp(x)
        return exp_x / (exp_x.sum() + 1e-8)

    def get_action_probs(self, obs: np.ndarray) -> np.ndarray:
        """Compute action probabilities from observation."""
        logits = obs @ self.weights + self.bias
        return self._softmax(logits)

    def update_epsilon(self, episode: int):
        """Decay epsilon linearly over the configured schedule."""
        if episode >= self.epsilon_decay_episodes:
            self.current_epsilon = self.epsilon_end
        else:
            frac = episode / max(self.epsilon_decay_episodes, 1)
            self.current_epsilon = (
                self.epsilon_start
                + (self.epsilon_end - self.epsilon_start) * frac
            )

    def select_action(self, obs: np.ndarray) -> int:
        """Sample an action with epsilon-greedy exploration."""
        if self.rng.random() < self.current_epsilon:
            # Random exploration
            action = self.rng.randint(self.n_actions)
        else:
            # Policy sampling
            probs = self.get_action_probs(obs)
            action = self.rng.choice(self.n_actions, p=probs)

        self.action_counts[action] += 1
        return action

    def select_action_greedy(self, obs: np.ndarray) -> int:
        """Select the highest-probability action (for evaluation)."""
        probs = self.get_action_probs(obs)
        return int(np.argmax(probs))

    def update(
        self,
        observations: list,
        actions: list,
        rewards: list,
    ):
        """REINFORCE policy gradient update."""
        T = len(rewards)
        if T == 0:
            return

        # Compute discounted returns
        returns = np.zeros(T)
        G = 0
        for t in reversed(range(T)):
            G = rewards[t] + self.gamma * G
            returns[t] = G

        # Update baseline
        mean_return = np.mean(returns)
        self.baseline = (
            self.baseline_alpha * mean_return
            + (1 - self.baseline_alpha) * self.baseline
        )

        # Advantage
        advantages = returns - self.baseline

        # Policy gradient update
        for t in range(T):
            obs = observations[t]
            action = actions[t]
            advantage = advantages[t]

            probs = self.get_action_probs(obs)

            # Gradient of log-probability
            grad = np.zeros(self.n_actions)
            grad[action] = 1.0 - probs[action]
            for a in range(self.n_actions):
                if a != action:
                    grad[a] = -probs[a]

            # Update weights
            outer = np.outer(obs, grad)
            self.weights += self.lr * advantage * outer
            self.bias += self.lr * advantage * grad

    def save(self, filepath: str):
        """Save agent parameters."""
        np.savez(
            filepath,
            weights=self.weights,
            bias=self.bias,
            baseline=np.array([self.baseline]),
            episode_rewards=np.array(self.episode_rewards),
        )

    def load(self, filepath: str):
        """Load agent parameters."""
        data = np.load(filepath)
        self.weights = data['weights']
        self.bias = data['bias']
        self.baseline = float(data['baseline'][0])
        self.episode_rewards = data['episode_rewards'].tolist()


def train(
    episodes: int = 50,
    max_steps: int = 20,
    lr: float = 0.01,
    gamma: float = 0.99,
    seed: int = 42,
    project_dir: str = '.',
    output_dir: str = 'rl_output',
    curriculum: bool = True,
    difficulty_start: float = 1.0,
    difficulty_end: float = 0.3,
    epsilon_start: float = 0.5,
    epsilon_end: float = 0.05,
):
    """Run RL training loop."""

    os.makedirs(output_dir, exist_ok=True)

    # Create environment
    env = SchedulingCodeEnv(
        project_dir=project_dir,
        max_steps=max_steps,
        render_mode='ansi',
        difficulty=difficulty_start,
    )

    agent = RLAgent(
        n_actions=env.action_space.n,
        obs_dim=env.observation_space.shape[0],
        learning_rate=lr,
        gamma=gamma,
        seed=seed,
        epsilon_start=epsilon_start,
        epsilon_end=epsilon_end,
        epsilon_decay_episodes=int(episodes * 0.8),
    )

    print(f"{'='*70}")
    print(f"RL Agent Training - Calendly Scheduling Code Generator")
    print(f"{'='*70}")
    print(f"Episodes: {episodes}")
    print(f"Max steps/episode: {max_steps}")
    print(f"Actions available: {len(CODE_ACTIONS)}")
    print(f"Observation dim: {env.observation_space.shape[0]}")
    print(f"Difficulty: {difficulty_start} -> {difficulty_end}")
    print(f"Epsilon: {epsilon_start} -> {epsilon_end}")
    print(f"Curriculum weights: {curriculum}")
    print(f"{'='*70}\n")

    all_results = []

    try:
        for episode in range(episodes):
            # Curriculum learning: adjust weights over time
            if curriculum:
                env.reward_calculator.update_weights(episode, episodes)

            # Difficulty curriculum: start hard (all stripped), get easier
            if difficulty_start != difficulty_end:
                progress = episode / max(episodes - 1, 1)
                env.difficulty = (
                    difficulty_start
                    + (difficulty_end - difficulty_start) * progress
                )

            # Update exploration rate
            agent.update_epsilon(episode)

            obs, info = env.reset()

            ep_observations = []
            ep_actions = []
            ep_rewards = []
            ep_action_names = []

            for step in range(max_steps):
                action = agent.select_action(obs)
                next_obs, reward, terminated, truncated, info = env.step(action)

                ep_observations.append(obs)
                ep_actions.append(action)
                ep_rewards.append(reward)
                ep_action_names.append(info.get('action_name', ''))

                obs = next_obs

                if terminated or truncated:
                    break

            # Update agent
            agent.update(ep_observations, ep_actions, ep_rewards)

            total_reward = sum(ep_rewards)
            agent.episode_rewards.append(total_reward)
            agent.episode_lengths.append(len(ep_rewards))

            # Track best
            if total_reward > agent.best_reward:
                agent.best_reward = total_reward
                agent.best_actions = ep_action_names.copy()

            # Render final state
            render_output = env.render()

            # Log progress
            breakdown = info.get('reward_breakdown', {})
            result = {
                'episode': episode,
                'total_reward': total_reward,
                'final_score': breakdown.get('total_reward', 0),
                'steps': len(ep_rewards),
                'actions': ep_action_names,
                'breakdown': breakdown,
                'epsilon': agent.current_epsilon,
                'difficulty': env.difficulty,
            }
            all_results.append(result)

            avg_reward = np.mean(agent.episode_rewards[-10:])
            unique_actions = len(set(ep_action_names))
            print(
                f"Episode {episode+1:3d}/{episodes} | "
                f"Reward: {total_reward:7.2f} | "
                f"Avg(10): {avg_reward:7.2f} | "
                f"Best: {agent.best_reward:7.2f} | "
                f"Steps: {len(ep_rewards):2d} | "
                f"Unique: {unique_actions:2d} | "
                f"eps={agent.current_epsilon:.2f} | "
                f"diff={env.difficulty:.2f} | "
                f"Score: {breakdown.get('total_reward', 0):.2f}"
            )

            if render_output and episode % 10 == 0:
                print(render_output)

    finally:
        # Always restore original engine.py
        env.close()

    # Save results
    agent.save(os.path.join(output_dir, 'agent_weights.npz'))

    # Action distribution analysis
    action_dist = {}
    for idx, count in sorted(agent.action_counts.items()):
        name = CODE_ACTIONS[idx] if idx < len(CODE_ACTIONS) else f'unknown-{idx}'
        action_dist[name] = count

    results_summary = {
        'training_config': {
            'episodes': episodes,
            'max_steps': max_steps,
            'lr': lr,
            'gamma': gamma,
            'curriculum': curriculum,
            'difficulty_start': difficulty_start,
            'difficulty_end': difficulty_end,
            'epsilon_start': epsilon_start,
            'epsilon_end': epsilon_end,
        },
        'best_reward': agent.best_reward,
        'best_actions': agent.best_actions,
        'final_avg_reward': float(np.mean(agent.episode_rewards[-10:])),
        'episode_rewards': agent.episode_rewards,
        'action_distribution': action_dist,
        'results': all_results,
    }

    results_path = os.path.join(output_dir, 'training_results.json')
    with open(results_path, 'w') as f:
        json.dump(results_summary, f, indent=2, default=str)

    print(f"\n{'='*70}")
    print("TRAINING COMPLETE")
    print(f"{'='*70}")
    print(f"Best reward: {agent.best_reward:.2f}")
    print(f"Best action sequence: {agent.best_actions}")
    print(f"Final avg reward (last 10): {np.mean(agent.episode_rewards[-10:]):.2f}")
    print(f"\nAction distribution:")
    for name, count in sorted(action_dist.items(), key=lambda x: -x[1]):
        print(f"  {name:<30} {count:>4}")
    print(f"\nResults saved to: {results_path}")
    print(f"Weights saved to: {os.path.join(output_dir, 'agent_weights.npz')}")

    return results_summary


def evaluate(
    project_dir: str = '.',
    weights_path: str = 'rl_output/agent_weights.npz',
    max_steps: int = 20,
):
    """Evaluate a trained agent greedily."""
    env = SchedulingCodeEnv(
        project_dir=project_dir,
        max_steps=max_steps,
        render_mode='human',
        difficulty=1.0,
    )

    agent = RLAgent(
        n_actions=env.action_space.n,
        obs_dim=env.observation_space.shape[0],
    )
    agent.load(weights_path)

    try:
        obs, info = env.reset()
        total_reward = 0
        actions_taken = []

        for step in range(max_steps):
            action = agent.select_action_greedy(obs)
            obs, reward, terminated, truncated, info = env.step(action)
            total_reward += reward
            actions_taken.append(info.get('action_name', ''))

            env.render()

            if terminated or truncated:
                break

        print(f"\nEvaluation total reward: {total_reward:.2f}")
        print(f"Actions: {actions_taken}")
    finally:
        env.close()

    return total_reward


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='RL Agent for Calendly-like scheduling code'
    )
    parser.add_argument(
        '--mode', choices=['train', 'evaluate'], default='train',
    )
    parser.add_argument('--episodes', type=int, default=50)
    parser.add_argument('--max-steps', type=int, default=20)
    parser.add_argument('--lr', type=float, default=0.01)
    parser.add_argument('--gamma', type=float, default=0.99)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--project-dir', default='.')
    parser.add_argument('--output-dir', default='rl_output')
    parser.add_argument('--weights', default='rl_output/agent_weights.npz')
    parser.add_argument(
        '--no-curriculum', action='store_true',
        help='Disable curriculum learning',
    )
    parser.add_argument('--difficulty-start', type=float, default=1.0)
    parser.add_argument('--difficulty-end', type=float, default=0.3)
    parser.add_argument('--epsilon-start', type=float, default=0.5)
    parser.add_argument('--epsilon-end', type=float, default=0.05)

    args = parser.parse_args()

    if args.mode == 'train':
        train(
            episodes=args.episodes,
            max_steps=args.max_steps,
            lr=args.lr,
            gamma=args.gamma,
            seed=args.seed,
            project_dir=args.project_dir,
            output_dir=args.output_dir,
            curriculum=not args.no_curriculum,
            difficulty_start=args.difficulty_start,
            difficulty_end=args.difficulty_end,
            epsilon_start=args.epsilon_start,
            epsilon_end=args.epsilon_end,
        )
    else:
        evaluate(
            project_dir=args.project_dir,
            weights_path=args.weights,
            max_steps=args.max_steps,
        )
