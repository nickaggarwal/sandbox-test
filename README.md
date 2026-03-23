# Cloud Sandbox Benchmark Suite

A comprehensive benchmarking framework that compares **6 cloud sandbox providers** -- [Daytona](https://www.daytona.io/), [E2B](https://e2b.dev/), [Blaxel](https://blaxel.ai/), [Modal](https://modal.com/), [Runloop](https://www.runloop.ai/), and [TensorLake](https://tensorlake.ai/) -- across **9 benchmarks** designed to test real-world AI agent workloads.

The test workload is a Django-based scheduling app (Calendly-like) with 29 unit tests, paired with a reinforcement learning agent trained using REINFORCE policy gradient. Benchmarks measure everything from long-running compute to sub-second file overwrites.

---

## Project Structure

```
sandbox-test/
├── run_parallel_profiled.py        # Main orchestrator - runs benchmarks in parallel
│
├── Sandbox Runners (unified interface)
│   ├── daytona_sandbox.py          # Daytona SDK adapter
│   ├── e2b_sandbox.py              # E2B Code Interpreter adapter
│   ├── blaxel_sandbox.py           # Blaxel adapter (async-to-sync bridge)
│   ├── modal_sandbox.py            # Modal adapter
│   ├── runloop_sandbox.py          # Runloop adapter
│   └── tensorlake_sandbox.py       # TensorLake adapter
│
├── Benchmark Modules
│   ├── filesystem_benchmark.py     # File I/O: code gen, compile, upload/download
│   ├── async_task_benchmark.py     # Pause/resume: background tasks, state persistence
│   ├── concurrent_exec_benchmark.py # Parallel exec: lint + test + typecheck + format
│   ├── iteration_loop_benchmark.py # Agent loop: write code -> test -> fix -> re-test
│   ├── fanout_benchmark.py         # Fan-out: N sandboxes, different tasks, collect results
│   ├── coding_agent_benchmark.py   # Real LLM agent: generate -> test -> score -> fix
│   ├── docker_benchmark.py        # Custom Docker images: build, create, verify deps
│   └── network_benchmark.py       # Network speed: latency, throughput, DNS, pip install
│
├── Test Workload (Django App)
│   ├── calendly_project/           # Django project config (settings, urls, wsgi)
│   ├── scheduling/                 # Scheduling app - models, views, serializers, 29 tests
│   ├── rewards/                    # RL environment (Gymnasium), reward functions
│   ├── run_rl_agent.py             # RL agent runner (REINFORCE policy gradient)
│   └── manage.py                   # Django management script
│
├── Coding Agent
│   ├── coding_agent.py             # LLM-powered coding agent (Gemini / Claude via Vertex AI)
│   ├── run_daytona.py              # Daytona-specific runner
│   └── requirements.txt            # Python dependencies
│
├── Results
│   ├── rl_output/                  # Benchmark JSON reports, agent weights
│   ├── FULL_BENCHMARK_REPORT.md    # Complete results across all 9 benchmarks
│   ├── SANDBOX_BENCHMARK_REPORT.md # Original 3-provider report
│   └── DAYTONA_VS_E2B_SUMMARY.md   # Head-to-head comparison
│
└── .gitignore
```

---

## How It Works

### Sandbox Runners

Each provider has a runner class (`DaytonaSandboxRunner`, `E2BSandboxRunner`, `BlaxelSandboxRunner`, `ModalSandboxRunner`, `RunloopSandboxRunner`, `TensorLakeSandboxRunner`) that implements the same interface:

| Method | Description |
|--------|-------------|
| `create_sandbox()` | Spin up a new sandbox with Python 3.12 |
| `exec(command, cwd, timeout)` | Execute a shell command |
| `upload_file_native(content, path)` | Upload a file via native FS API |
| `download_file_native(path)` | Download a file via native FS API |
| `upload_project(dir)` | Upload an entire project as tar.gz |
| `setup_environment()` | Install pip dependencies, run migrations |
| `run_tests()` | Run Django test suite |
| `pause_sandbox()` / `resume_sandbox()` | Pause and resume (where supported) |
| `destroy()` | Tear down the sandbox |

This unified interface lets the orchestrator swap providers transparently.

### Benchmark Modules

Each benchmark module exports a single function: `run_*_benchmark(runner, provider) -> list[StepProfile]`

Every step is profiled with start/end timestamps, success/failure status, and a detail string. The orchestrator collects all `StepProfile` objects and generates comparison tables.

### Orchestrator

`run_parallel_profiled.py` ties everything together:

1. Parses CLI args (`--benchmark`, `--provider`, `--sandboxes`, etc.)
2. Generates config objects for each sandbox to launch
3. Runs all sandboxes in parallel via `ThreadPoolExecutor`
4. Each thread: creates sandbox -> runs benchmark -> destroys sandbox
5. Collects all profiles and prints a comparison report
6. Saves full JSON report to `rl_output/`

---

## Quick Start

### 1. Install Dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Set API Keys

Create a `.env` file:

```
DAYTONA_API_KEY=your_daytona_key
E2B_API_KEY=your_e2b_key
BLAXEL_API_KEY=your_blaxel_key
RUNLOOP_API_KEY=your_runloop_key
TENSORLAKE_API_KEY=your_tensorlake_key
GEMINI_API_KEY=your_gemini_key          # Required for --benchmark agent --llm gemini
```

For Modal, run `modal token set` (it uses `~/.modal.toml`).
For Claude via Vertex AI, run `gcloud auth login` (used by `--llm vertex-claude`).

### 3. Run Benchmarks

```bash
# Single benchmark, single provider
python run_parallel_profiled.py --benchmark iteration --provider e2b --sandboxes 1

# Compare two providers
python run_parallel_profiled.py --benchmark concurrent --provider both --sandboxes 2

# Compare all 6 providers
python run_parallel_profiled.py --benchmark fanout --provider all --sandboxes 4

# Run all 9 benchmarks across all providers
python run_parallel_profiled.py --benchmark all --provider all --sandboxes 4

# Run the LLM coding agent benchmark (requires GEMINI_API_KEY)
python run_parallel_profiled.py --benchmark agent --provider e2b --sandboxes 1 --llm gemini --agent-iterations 3
```

### CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `--benchmark` | `rl` | `rl`, `fs`, `pause`, `concurrent`, `iteration`, `fanout`, `agent`, `docker`, `network`, or `all` |
| `--provider` | `daytona` | `daytona`, `e2b`, `blaxel`, `modal`, `runloop`, `tensorlake`, `both` (Daytona+E2B), or `all` |
| `--sandboxes` | `3` | Number of parallel sandboxes per provider |
| `--max-workers` | `3` | Max concurrent threads |
| `--stagger` | `2.0` | Seconds between sandbox launches (reduces thundering herd) |
| `--episodes` | `30` | RL training episodes (for `rl` benchmark) |
| `--max-steps` | `15` | Max steps per RL episode |
| `--vary-config` | off | Use different RL configs per sandbox |
| `--llm` | `gemini` | LLM backend for agent benchmark: `gemini` or `vertex-claude` |
| `--llm-model` | auto | Model name override (e.g. `gemini-2.5-flash-lite`) |
| `--llm-api-key` | env | API key for LLM (or set `GEMINI_API_KEY` in `.env`) |
| `--agent-iterations` | `3` | Max generate-test-fix cycles for agent benchmark |
| `--reward-threshold` | `25.0` | Stop agent when reward reaches this value |
| `--bootstrap-app` | off | Agent generates Django app from scratch (no project upload) |

---

## The 9 Benchmarks

### 1. RL Compute (`--benchmark rl`)

Runs the full agent pipeline: create sandbox, upload Django project, install deps, run 29 tests, train an RL agent for 30 episodes, retrieve results. Tests sustained compute performance and reliability over minutes-long runs.

### 2. Filesystem I/O (`--benchmark fs`)

Generates 10 Python files, compiles to .pyc, uploads/downloads files via native FS API, round-trips 1MB JSON, and verifies integrity. Measures the per-file and large-file throughput that determines how fast agents can read and write code.

### 3. Pause/Resume (`--benchmark pause`)

Starts a background task, writes checkpoint files (JSON, YAML, binary), pauses the sandbox, resumes it, and verifies all state survived. Tests whether agents can suspend and resume work without data loss.

### 4. Concurrent Exec (`--benchmark concurrent`)

Fires 4 commands (`flake8`, `pytest`, `mypy`, `black`) sequentially then concurrently. Measures whether the sandbox supports true parallel execution -- a 2-5x speedup means agents get feedback much faster.

### 5. Iteration Loop (`--benchmark iteration`)

The core coding agent cycle: upload broken code, run tests to detect failures, upload a fix, run tests to confirm, add a feature, final validation. Measures the round-trip latency that directly limits how many iterations an agent can attempt.

### 6. Multi-Sandbox Fan-Out (`--benchmark fanout`)

Creates 10 sandboxes in parallel, uploads code to all, runs different compute tasks on each (factorial, fibonacci, prime sieve, sort -- cycled across sandboxes), collects results, destroys all. Tests how fast an agent can explore multiple strategies simultaneously.

### 7. Coding Agent (`--benchmark agent`)

Runs the real LLM-powered coding agent (`coding_agent.py`) through its full generate -> test -> score -> fix loop. The agent uses Gemini or Claude (via Vertex AI) to iteratively build and improve a Django scheduling engine, scored by a multi-objective reward function (correctness, code quality, domain logic).

**Requires:** `GEMINI_API_KEY` in `.env` (for Gemini) or `gcloud auth` configured (for Vertex Claude).

```bash
# 3 iterations with Gemini
python run_parallel_profiled.py --benchmark agent --provider e2b --sandboxes 1 --llm gemini --agent-iterations 3

# Bootstrap mode (LLM generates the entire app from scratch)
python run_parallel_profiled.py --benchmark agent --provider e2b --sandboxes 1 --llm gemini --bootstrap-app --agent-iterations 5
```

### 8. Custom Docker Image (`--benchmark docker`)

Tests how each provider handles building and launching sandboxes from custom Docker images with pre-installed dependencies (Django, DRF, pytest, flake8, numpy). Measures image build time, sandbox creation from the custom image, dependency verification (pre-baked vs runtime pip install), and runs a compute workload to confirm the environment works. Compares custom image performance against a baseline of default image + pip install at runtime.

**Key difference**: Daytona and Modal support runtime image building (deps baked into the image at build time). E2B uses pre-built templates, Blaxel uses pre-existing Docker Hub images, and Runloop/TensorLake require runtime pip install.

```bash
# Test on single provider
python run_parallel_profiled.py --benchmark docker --provider daytona --sandboxes 1

# Compare all providers
python run_parallel_profiled.py --benchmark docker --provider all --sandboxes 1
```

### 9. Network Speed (`--benchmark network`)

Measures raw network performance from inside each sandbox: HTTP round-trip latency (5x GET to `google.com/robots.txt`), download throughput (~10MB from Cloudflare), upload throughput (~5MB POST to httpbin.org), DNS resolution time for 5 hostnames, and real-world `pip install requests` speed. These metrics reflect the actual network experience agents have when fetching APIs, cloning repos, downloading packages, and uploading/downloading data.

```bash
# Test on single provider
python run_parallel_profiled.py --benchmark network --provider daytona --sandboxes 1

# Compare all providers
python run_parallel_profiled.py --benchmark network --provider all --sandboxes 1
```

#### Iteration Loop vs Coding Agent

| | Iteration Loop (`--benchmark iteration`) | Coding Agent (`--benchmark agent`) |
|---|---|---|
| **Code source** | Pre-written (deterministic) | LLM-generated (non-deterministic) |
| **What it measures** | Pure sandbox I/O + exec latency | End-to-end agent performance (LLM + sandbox) |
| **LLM required** | No | Yes (Gemini or Claude) |
| **Test workload** | Simple calculator (6-9 tests) | Django scheduling app (29 tests) |
| **Reward scoring** | None | Multi-objective (correctness, quality, domain) |
| **Typical duration** | 3-12s | 30-120s+ (depends on LLM latency) |
| **Use case** | Benchmark sandbox speed in isolation | Benchmark real agent workflow end-to-end |

---

## Results Summary

Full results with per-step timings: [FULL_BENCHMARK_REPORT.md](FULL_BENCHMARK_REPORT.md)

### Overall Rankings (fastest total per benchmark)

| Benchmark | 1st | 2nd | 3rd | 4th | 5th | 6th |
|-----------|-----|-----|-----|-----|-----|-----|
| RL Compute | Blaxel (135s)* | Daytona (165s)† | E2B (301s)* | Runloop (360s)† | TensorLake (365s)† | Modal (564s) |
| Filesystem I/O | E2B (2.8s) | Blaxel (3.7s) | Daytona (14.7s) | TensorLake (17.8s) | Runloop (25.0s) | Modal (30.8s) |
| Pause/Resume | Blaxel (4.4s)** | TensorLake (11.5s) | E2B (15.4s) | Runloop (20.3s)*** | Modal (22.7s) | Daytona (32.5s) |
| Concurrent Exec | E2B (8.1s) | Blaxel (9.7s) | Daytona (10.3s) | TensorLake (13.3s) | Modal (15.6s) | Runloop (16.1s) |
| Iteration Loop | E2B (3.8s) | Blaxel (5.1s) | Daytona (6.3s) | TensorLake (7.3s) | Runloop (9.9s) | Modal (12.6s) |
| Fan-Out (10 sandboxes) | E2B (3.0s) | Runloop (6.2s) | Daytona (7.0s) | Modal (10.4s) | Blaxel (10.6s) | TensorLake (21.1s) |
| Coding Agent**** | Runloop (58s) | E2B (61s) | Blaxel (75s) | Daytona (79s) | TensorLake (86s) | Modal (86s) |
| Custom Docker | E2B (6.1s) | Daytona (10.5s) | TensorLake (10.7s) | Blaxel (11.2s) | Runloop (11.9s) | Modal (14.7s) |
| Network Speed | Daytona (7.6s) | E2B (8.8s) | Blaxel (10.6s) | Runloop (14.1s) | TensorLake (14.1s) | Modal (22.8s) |

\* Had intermittent stability issues on long runs
\** Blaxel skipped pause/resume (no API)
\*** Runloop pause/resume requires account capability (skipped)
\**** Coding Agent times include LLM latency (Gemini 2.5 Flash Lite); not directly comparable to synthetic benchmarks
† Ran 30ep/15steps; others ran 50ep/25steps

### Best Provider by Use Case

| Use Case | Best Provider | Key Metric |
|----------|--------------|------------|
| Long compute (5+ min) | Daytona | Most reliable, configurable resources |
| Coding agent iteration | E2B | 3.8s loop, 0.05s file writes |
| Parallel tool execution | E2B | 0.66s for 4 concurrent commands |
| Multi-sandbox fan-out | E2B | 10 sandboxes in 3.0s total |
| Pause/resume | E2B | 0.9s pause, 0.2s resume |
| Large file processing | Blaxel | 0.2s for 1MB round-trip |
| Fastest cold start | Modal | 0.09s per sandbox (fan-out) |
| Custom Docker images | Daytona | 3.88x speedup with pre-baked deps |
| LLM coding agent loop | Runloop | 58.2s total, 16.8s avg iteration |
| Sustained downloads | Runloop | 134.84 MB/s download throughput |
| DNS resolution | TensorLake | 1.52ms avg DNS resolution |
| pip install speed | TensorLake | 0.95s pip install requests |
| Snapshot-based pause/resume | TensorLake | 2.5s pause, 0.9s resume |

### Key Findings

- **All 6 providers support true parallel exec** -- agents can fire lint/test/typecheck simultaneously for 2-5x speedups
- **E2B wins 5 of 9 benchmarks** on total time, driven by fast file I/O (5-8x faster than most) and sub-second sandbox creation
- **Daytona is the most reliable** for long-running compute (5+ minutes) where E2B and Blaxel can drop connections
- **Runloop wins the real-world coding agent benchmark** (58.2s) with the fastest avg iteration time (16.8s)
- **TensorLake excels at network operations** -- fastest DNS (1.52ms) and pip install (0.95s), competitive pause/resume via snapshots
- **Daytona and Modal lead on custom Docker images** -- both support runtime image building with pre-baked dependencies
- **Modal has the fastest sandbox creation** (0.09s avg) and highest parallel speedup (5.06x) but the slowest file I/O
- **Blaxel excels at short bursts** -- fastest test execution (0.29s) and large file I/O (0.2s for 1MB) but lacks pause/resume

---

## Platform Capabilities

| Capability | Daytona | E2B | Blaxel | Modal | Runloop | TensorLake |
|-----------|---------|-----|--------|-------|---------|------------|
| Sandbox creation | 0.8-0.9s | 0.1-0.3s | 0.3-0.5s | 0.3-0.8s | 1.3-2.3s | 1.0-1.2s |
| Custom CPU/Memory | Yes | No (fixed) | Yes | Yes | No (default) | Yes |
| Custom Docker images | Yes (runtime build) | Template-based | Yes (Docker Hub) | Yes (runtime build) | No | Yes (image param) |
| Native pause/resume | Stop/start (22s) | Yes (0.9s) | No | Via snapshots (1.5s) | Yes (capability gated) | Via snapshots (2.5s) |
| Exec timeout limit | 60s* | None | None | None | None | None |
| Parallel exec speedup | 3.38x | 3.40x | 2.56x | 5.06x | 1.57x | 1.95x |
| Native file upload/download | Yes | Yes | Yes (async) | Yes | Yes | Yes |
| Snapshots | No | Yes | No | Yes | Yes | Yes |
| Network access | Yes | Yes | Yes | Yes | Yes | Yes |
| Auth method | API key | API key | API key | `~/.modal.toml` | API key | API key |

\* Requires `nohup` + polling workaround for commands >60s

---

## Known Gotchas

| Issue | Provider | Workaround |
|-------|----------|------------|
| 60s exec timeout | Daytona | Use `exec_long()` (nohup + polling) |
| Home dir is `/root` not `/home/daytona` | Daytona | Use `/root` as base path |
| SSL cert errors on macOS | Daytona/Modal | `import certifi; os.environ['SSL_CERT_FILE'] = certifi.where()` |
| SDK raises on non-zero exit code | E2B | Wrap commands: `cmd; echo "EXIT=$?"` |
| Connection drops on runs >5min | E2B | Use Daytona for long compute |
| No pause/resume API | Blaxel | Not available in SDK |
| Async-to-sync bridging | Blaxel | Uses `asyncio.new_event_loop()` per instance |
| Region warning | Blaxel | Set `BL_REGION` env var to suppress FutureWarning |
| Slow `sandbox.open()` I/O | Modal | Batch file operations when possible |
| Auth via config file (no api_key param) | Modal | Run `modal token set` (uses `~/.modal.toml`) |
| `file.write()` uses `file_path` not `path` | Runloop | `file.write(file_path=..., contents=...)` for text; `file.upload(path=..., file=...)` for binary |
| Pause/resume capability gated | Runloop | Requires `ACCOUNT_CAPABILITY_DEVBOX_SUSPEND_AND_RESUME` on account |
| No custom image build API | Runloop | Use default devbox; deps must be installed at runtime via pip |
| Missing `libsqlite3.so.0` in devbox | Runloop | Download `.deb` from Debian repo, extract to `~/.local/lib/`, use `LD_LIBRARY_PATH` |
| No root/sudo access in devbox | Runloop | Cannot `apt-get install`; use pip or download prebuilt binaries |
| PEP 668 externally managed Python | TensorLake | Set `PIP_BREAK_SYSTEM_PACKAGES=1` or use `--break-system-packages` |
| Commands require bash wrapper | TensorLake | Use `sandbox.run('bash', args=['-c', cmd])` -- direct command strings fail |
| Sandbox starts in PENDING state | TensorLake | Poll `client.get(sandbox_id)` until status is `RUNNING` before connecting |
| Snapshot-based pause/resume | TensorLake | `client.snapshot()` then create new sandbox from `snapshot_id`; original must be terminated |
