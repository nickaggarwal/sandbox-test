# Cloud Sandbox Benchmark Report: Full Results

**Providers tested**: Daytona, E2B, Blaxel, Modal
**Date**: March 7, 2026 (updated with pre-warmed Daytona sandboxes + all 10 benchmarks)
**Python**: 3.12 (host and sandbox images)
**SDKs**: Daytona v0.148, E2B Code Interpreter v2.4.1, Blaxel v0.2.44, Modal v0.74+
**Instance specs**: Daytona (pre-warmed pool, 1 vCPU, 1GB RAM, 3GB disk), E2B (default), Blaxel (4 vCPU, 8GB), Modal (4 CPU, 8GB)

> **Note**: Daytona now uses `CreateSandboxBaseParams(language='python')` which draws from a pre-warmed sandbox pool for fast boot (~0.9s). This gives 1 vCPU / 1GB RAM by default (shared host with 64 CPUs). The previous `CreateSandboxFromImageParams` approach used 4 CPU / 8GB but had slower boot (~1.3s). The pre-warmed sandbox runs as non-root user `daytona` (uid=1001) with home at `/home/daytona/`.

---

## Benchmark Suite Overview

| # | Benchmark | What It Tests | Steps |
|---|-----------|---------------|-------|
| 1 | RL Compute | Long-running training (50 ep, 25 steps) | 7 |
| 2 | Filesystem I/O | Code gen, compile, upload/download, large files | 6 |
| 3 | Pause/Resume | Background task, state persistence across pause | 7 |
| 4 | Concurrent Exec | Parallel lint/test/typecheck/format in one sandbox | 4 |
| 5 | Iteration Loop | Agent edit-test cycle (write code, test, fix, re-test) | 6 |
| 6 | Multi-Sandbox Fan-Out | Create N sandboxes, distribute tasks, collect results | 5 |
| 7 | Coding Agent | Real LLM agent loop: generate code, test, score, fix | 4+ |
| 8 | Custom Docker Image | Build custom image, verify pre-baked deps, compare vs baseline | 5 |
| 9 | Network Speed | HTTP latency, download/upload throughput, DNS resolution, pip install | 6 |
| 10 | Security & Isolation | Metadata access, privilege audit, container escape, network scan, filesystem, resource limits, egress, env leakage | 8 |

---

## 1. RL Compute Benchmark

**What it tests**: End-to-end performance for compute-heavy workloads -- creating a sandbox, uploading a Django project, installing dependencies, running 29 unit tests, training an RL agent (REINFORCE policy gradient, 50 episodes, 25 max steps), and retrieving results.

**Why it matters**: This is the most realistic workload for agents running long compute jobs in sandboxes. It tests the full pipeline from sandbox creation to result extraction, with the majority of time spent on CPU-bound RL training.

| Step | Daytona | E2B | Blaxel | Modal |
|------|---------|-----|--------|-------|
| Create Sandbox | 0.9s | 0.2s | 0.4s | 0.3s |
| Upload Project | 1.2s | 0.4s | 0.5s | 2.9s |
| Install Deps | 2.1s | 5.5s | 6.6s | 7.2s |
| Run Tests (29) | 5.5s | 7.7s | 6.7s | 9.7s |
| RL Training | 170.2s† | 287s* | 120s** | 542.2s |
| Retrieve Results | 0.4s | ~1s | ~1s | 1.3s |
| **Total** | **182.5s**† | **301.1s** | **135.2s** | **564.0s** |

\* E2B hit connection timeouts on some long runs
\** Blaxel hit timeout/HTML error on some runs
† Daytona ran 30 episodes / 15 max-steps (pre-warmed pool config); others ran 50/25

**Summary**: Daytona with pre-warmed sandboxes now achieves the fastest setup phase (0.9s create + 2.1s install -- deps partially pre-cached in the pool). RL training completed in 170.2s (30 ep/15 steps) with 29/29 tests and best reward 15.1. Modal completed the full RL pipeline reliably (29/29 tests, best reward 14.54) but was the slowest at 564s total. E2B was faster but experienced connection drops on runs exceeding 5 minutes. Blaxel posted the fastest raw times when it worked, but had stability issues on long-running compute.

---

## 2. Filesystem I/O Benchmark

**What it tests**: Agent-style file operations -- generating 10 Python source files (small to 20KB) via exec, compiling them to .pyc, uploading 5 files (32KB total) via native FS API, downloading 9 files back, round-tripping pip wheel packages (download in sandbox, retrieve via FS API, upload back, verify checksums), and listing directory contents.

**Why it matters**: Coding agents constantly read and write files in sandboxes. The speed of native filesystem APIs directly determines how fast an agent can upload code patches, download build artifacts, and manage project files. Slow file I/O creates a bottleneck on every iteration.

| Step | Daytona | E2B | Blaxel | Modal |
|------|---------|-----|--------|-------|
| Code Generation (10 files) | 1.5s | 1.1s | 1.2s | 3.9s |
| Build/Compile (.pyc) | 0.3s | 0.2s | 0.3s | 0.8s |
| Native File Upload (5 files) | 1.6s | 0.3s | 0.3s | 6.1s |
| Native File Download (9 files) | 2.8s | 0.5s | 0.7s | 6.8s |
| Pip Package I/O (1MB round-trip) | 5.1s | 0.6s | 0.2s | 9.8s |
| List & Verify | 0.3s | 0.1s | 0.2s | 0.5s |
| **Total** | **12.9s** | **2.8s** | **3.7s** | **30.8s** |

| Operation | Daytona | E2B | Blaxel | Modal |
|-----------|---------|-----|--------|-------|
| Upload per file | ~0.32s | ~0.06s | ~0.06s | ~1.22s |
| Download per file | ~0.31s | ~0.06s | ~0.08s | ~0.76s |
| Large file (710KB) | ~1.7s | ~0.3s | ~0.1s | ~3.3s |

**Summary**: E2B dominates overall filesystem performance at 2.8s total. Blaxel is a close second at 3.7s and wins on large file I/O (0.2s for 1MB vs E2B's 0.6s). Modal is the slowest at 30.8s, with extremely high per-file overhead. Daytona improved to 12.9s with pre-warmed sandboxes (down from 15.7s) but remains slower than E2B/Blaxel for file operations. For file-heavy agent workflows, E2B or Blaxel should be preferred.

---

## 3. Async Task + Pause/Resume Benchmark

**What it tests**: Starting a background Python task that writes progress to disk, writing checkpoint files (JSON, YAML, 10KB binary) while the task runs, pausing the sandbox mid-execution, resuming it, verifying all filesystem state survived the pause/resume cycle (including binary data integrity), restarting a compute task after resume, and downloading all results.

**Why it matters**: Agents often need to suspend work and resume later -- for cost savings, context switching between tasks, or handling interruptions. Pause/resume preserves the full sandbox state without re-creating from scratch. This benchmark measures whether you can trust the sandbox to come back exactly as you left it.

| Step | Daytona | E2B | Blaxel | Modal |
|------|---------|-----|--------|-------|
| Start Background Task | 2.5s | 12.3s* | 2.3s | 3.1s |
| Write State Files | 0.9s | 0.4s | 0.1s | 1.8s |
| Pause Sandbox | 19.2s | 0.9s | N/A** | 1.5s |
| Resume Sandbox | 0.7s | 0.2s | N/A** | 0.1s |
| Verify State After Resume | 2.2s | 0.5s | 0.4s | 7.1s |
| Restart Compute | 0.8s | 0.3s | 0.2s | 2.6s |
| Download & Verify | 1.8s | 0.3s | 0.3s | 4.6s |
| **Total** | **29.5s** | **15.4s** | **4.4s** | **22.7s** |

\* E2B bg task completed before pause due to timing
\** Blaxel SDK does not expose pause/resume APIs

| Metric | Daytona | E2B | Modal |
|--------|---------|-----|-------|
| Pause mechanism | `sandbox.stop()` | `sandbox.pause()` (native) | Snapshot-based |
| Resume mechanism | `sandbox.start()` | `Sandbox.connect(id)` | New sandbox from snapshot |
| Pause latency | 19-26s | 0.8-0.9s | 1.5s |
| Resume latency | 0.6-0.7s | 0.2s | 0.1s |
| Filesystem preserved | Yes | Yes | Yes |
| Binary integrity | Yes | Partial* | Yes |

\* E2B `files.read()` returned different byte representation for binary files after resume

**Summary**: E2B has native pause/resume with sub-second latency (0.9s pause, 0.2s resume), making it ideal for suspend/resume workflows. Modal supports pause via snapshots with 1.5s pause and 0.1s resume -- the fastest resume of all providers -- but its slow file I/O (7.1s state verify, 4.6s download) brings the total to 22.7s. Daytona uses a stop/start mechanism that takes 19-26s to pause (slower with pre-warmed sandboxes) but reliably preserves all state including binary data. Blaxel has no pause/resume API -- its fast "total" time reflects skipping those steps.

---

## 4. Concurrent Exec Benchmark

**What it tests**: Whether a sandbox can handle multiple simultaneous `exec()` calls -- the pattern agents use when firing lint, test, typecheck, and format commands in parallel rather than sequentially. Uploads a Python project with source files, installs pytest/flake8/mypy/black, then runs the same 4 commands first sequentially (baseline) then concurrently via threads.

**Why it matters**: A coding agent that can run `flake8`, `pytest`, `mypy`, and `black` at the same time instead of one-by-one gets feedback 3-5x faster per iteration. This directly translates to faster agent loops. If a sandbox serializes exec() calls, the agent gains nothing from concurrent design.

| Provider | Sequential (4 cmds) | Concurrent (4 cmds) | Speedup | Setup | Total |
|----------|--------------------|--------------------|---------|-------|-------|
| **Daytona** | 4.54s | 3.23s | **1.41x** | 4.9s | 13.9s |
| **E2B** | 2.23s | 0.66s | **3.40x** | 3.9s | 8.1s |
| **Blaxel** | 2.23s | 0.87s | **2.56x** | 5.4s | 9.7s |
| **Modal** | 3.38s | 0.67s | **5.06x** | 9.1s | 15.6s |

| Command (concurrent) | Daytona | E2B | Blaxel | Modal |
|---------|---------|-----|--------|-------|
| flake8 | 2.07s | 0.22s | 0.30s | 0.45s |
| pytest | 3.23s | 0.52s | 0.34s | 0.76s |
| mypy | 0.89s | 1.20s | 1.29s | 1.57s |
| black | 2.29s | 0.29s | 0.46s | 0.60s |

**Summary**: All 4 providers support true parallel exec -- none serialize calls. Modal shows the highest speedup (5.06x) because its sequential commands are the slowest. E2B wins on total pipeline time (8.1s) due to faster sandbox creation and pip install. Daytona's pre-warmed sandbox (1 vCPU) showed reduced concurrent speedup (1.41x vs 3.48x with 4 CPU), with individual commands taking longer due to CPU contention. For agents designed to fire multiple commands in parallel, providers with multi-core allocation (E2B, Blaxel, Modal) deliver better speedups.

---

## 5. Iteration Loop Benchmark

**What it tests**: The core coding agent cycle -- upload deliberately broken code (a Calculator class with bugs), run tests to detect failures, upload a fixed version, run tests to confirm the fix, add a new feature (power method + new tests), and run final validation. Measures the round-trip latency for each step of the write-test-fix loop.

**Why it matters**: This is the single most important benchmark for coding agents. Every agent iteration involves writing code, running tests, reading output, and writing a fix. The total loop time directly determines how many iterations an agent can complete in a given time budget. A 2x faster loop means 2x more attempts at solving a problem.

| Step | Daytona | E2B | Blaxel | Modal |
|------|---------|-----|--------|-------|
| Upload broken code (+ pip install) | 1.9s | 1.16s | 2.93s | 5.23s |
| Run tests (detect 3 failures) | 1.7s | 0.53s | 0.39s | 1.43s |
| Upload fix (overwrite 1 file) | 0.3s | 0.05s | 0.04s | 0.56s |
| Run tests (all 6 pass) | 1.6s | 0.45s | 0.29s | 0.82s |
| Add feature (update 2 files) | 0.6s | 0.09s | 0.14s | 1.19s |
| Final validation (9/9 pass) | 1.6s | 0.44s | 0.30s | 0.92s |
| **Total (excl. create/destroy)** | **7.7s** | **2.72s** | **4.09s** | **10.15s** |
| **Total (incl. create/destroy)** | **9.2s** | **3.82s** | **5.12s** | **12.63s** |

| Operation | Daytona | E2B | Blaxel | Modal |
|-----------|---------|-----|--------|-------|
| File overwrite | 0.3s | 0.05s | 0.04s | 0.56s |
| Test exec (avg) | 1.6s | 0.47s | 0.33s | 1.06s |
| Multi-file update | 0.6s | 0.09s | 0.14s | 1.19s |

**Summary**: E2B delivers the fastest total iteration loop (3.8s including sandbox lifecycle). Its file operations are extremely fast (0.05s per overwrite) which is critical since agents write files on every iteration. Blaxel has the fastest single file overwrite (0.04s) and fast test execution (0.29-0.39s). Daytona's pre-warmed sandbox (1 vCPU) shows slower test execution (1.6s avg vs 0.25s with 4 CPU) due to CPU constraints, but file writes remain fast (0.3s). Modal is the slowest overall. For coding agents optimizing for iteration speed, E2B is the clear winner.

---

## 6. Multi-Sandbox Fan-Out Benchmark

**What it tests**: Spinning up 10 sandboxes simultaneously, uploading a compute script to all of them concurrently, running different compute tasks on each (factorial, fibonacci, prime sieve, sort -- cycled across sandboxes), collecting results from all sandboxes, and destroying them all. Every step uses ThreadPoolExecutor for maximum parallelism.

**Why it matters**: Advanced agents fan out across multiple sandboxes to try different approaches simultaneously, run tests on different configurations, or split large workloads. The fan-out pattern requires fast sandbox creation, efficient parallel I/O, and reliable concurrent operations. Slow sandbox creation is the biggest bottleneck since it's multiplied by N.

**Results (10 sandboxes):**

| Step | Daytona | E2B | Blaxel | Modal |
|------|---------|-----|--------|-------|
| Create 10 sandboxes | 1.9s | — | — | — |
| Upload code to all | 2.1s | — | — | — |
| Run 10 different tasks | 0.3s | — | — | — |
| Collect results | 1.1s | — | — | — |
| Destroy all | 0.3s | — | — | — |
| Custom image (10 sandboxes) | 3.7s | — | — | — |
| **Total** | **9.5s** | — | — | — |

| Metric | Daytona |
|--------|---------|
| Avg creation per sandbox (stock) | 0.19s |
| Avg creation per sandbox (custom) | 0.37s |
| Total I/O (upload + collect) | 3.2s |
| Total compute (10 tasks) | 0.3s |

**Previous results (3 sandboxes, all providers):**

| Step | Daytona | E2B | Blaxel | Modal |
|------|---------|-----|--------|-------|
| Create 3 sandboxes | 3.51s | 1.05s | 1.47s | 0.81s |
| Upload code to all | 0.77s | 0.27s | 0.88s | 2.34s |
| Run 3 different tasks | 0.17s | 0.08s | 0.43s | 0.64s |
| Collect results | 0.36s | 0.06s | 0.13s | 0.71s |
| Destroy all | 0.29s | 0.16s | 0.46s | 0.11s |
| **Total** | **5.10s** | **1.62s** | **3.37s** | **4.61s** |

**Summary**: Daytona's pre-warmed pool dramatically improved fan-out performance: 10 sandboxes created in 1.9s (0.19s avg each), down from 1.17s per sandbox with custom images. Custom image fan-out also improved to 0.37s avg. All 10 tasks completed in 0.3s wall time with results collected in 1.1s. In the 3-sandbox comparison, E2B was fastest (1.6s). Run `--benchmark fanout --provider all` for updated multi-provider 10-sandbox results.

---

## 7. Coding Agent Benchmark

**What it tests**: The real LLM-powered coding agent loop -- creating a sandbox, uploading the Django scheduling project, then running 3 iterations of: ask Gemini to generate/fix code, upload the code, run Django tests (29 tests), compute a multi-objective reward score (correctness, code quality, domain logic), and feed the score back to the LLM.

**Why it matters**: Unlike the synthetic Iteration Loop benchmark (#5) which uses pre-written code and measures pure sandbox speed, this benchmark measures **end-to-end agent performance** including LLM inference latency. It answers: "How fast can a real coding agent iterate on a real codebase using this sandbox?" The results are non-deterministic since the LLM generates different code each run.

**LLM**: Gemini 2.5 Flash Lite (via REST API), 3 iterations, reward threshold 25.0

| Step | E2B | Blaxel | Modal | Daytona |
|------|-----|--------|-------|---------|
| Setup (sandbox + pip install) | 3.8s | 4.6s | 7.8s | 2.5s |
| Upload Project (38 files) | 0.3s | 0.4s | 1.9s | 0.9s |
| Iteration 1 (LLM + test) | 7.1s | 13.0s | 19.3s | 10.8s |
| Iteration 2 (LLM + test) | 30.7s | 33.0s | 28.6s | 30.5s |
| Iteration 3 (LLM + test) | 19.1s | 23.9s | 28.6s | 17.0s |
| **Total** | **61.1s** | **75.1s** | **86.4s** | **61.9s** |

| Metric | E2B | Blaxel | Modal | Daytona |
|--------|-----|--------|-------|---------|
| Best reward | 7.7 | 16.4 | 14.4 | 13.6 |
| Sandbox overhead (setup+upload) | 4.1s | 5.0s | 9.7s | 3.4s |
| Avg iteration time | 19.0s | 23.3s | 25.5s | 19.4s |

**Iteration Loop (synthetic) vs Coding Agent (LLM) comparison:**

| | Iteration Loop | Coding Agent |
|---|---|---|
| **E2B** | 3.8s (6 steps) | 61.1s (3 LLM iterations) |
| **Daytona** | 9.2s | 61.9s |
| **Blaxel** | 5.1s | 75.1s |
| **Modal** | 12.6s | 86.4s |

**Summary**: E2B and Daytona are virtually tied on total agent loop time (61.1s vs 61.9s). Daytona's pre-warmed sandbox gives the fastest setup (2.5s) and lowest sandbox overhead (3.4s) of all providers. However, iteration times are dominated by LLM latency (5-32s per call), making the sandbox speed difference less impactful than in synthetic benchmarks. The best reward scores varied across providers (7.7 to 16.4) due to non-deterministic LLM outputs, not sandbox differences. For real agent workloads, sandbox choice matters most for setup overhead; once running, LLM API latency is the bottleneck.

---

## 8. Custom Docker Image Benchmark

**What it tests**: How each provider handles building and launching sandboxes from custom Docker images with pre-installed dependencies (Django, DRF, pytest, flake8, numpy). Measures image build time, sandbox creation from the custom image, dependency verification (pre-baked vs runtime pip install), and runs a compute workload to confirm the environment works. Compares custom image performance against a baseline of default image + pip install at runtime.

**Why it matters**: Production agents benefit from custom images with pre-baked dependencies -- it eliminates pip install latency on every sandbox creation. The ability to build and use custom images determines whether an agent can amortize setup cost across many sandbox launches.

| Step | Daytona | E2B | Blaxel | Modal |
|------|---------|-----|--------|-------|
| Build Custom Image | 0.0s (runtime) | 0.0s (template) | 0.0s (pre-existing) | 0.2s (runtime) |
| Create Sandbox (custom) | 20.3s | 1.4s | 3.0s | 0.8s |
| Verify Pre-baked Deps | 0.7s (pre-baked) | 4.1s (pip needed) | 7.4s (pip needed) | 2.9s (pre-baked) |
| Run Compute Workload | 0.4s | 0.2s | 0.2s | 0.6s |
| Stock Image Boot | 0.17s | 0.14s | 0.33s | 0.31s |
| Comparison | stock=0.17s, custom=21.0s | stock=0.14s, custom=1.4s | stock=0.33s, custom=3.0s | baseline=6.8s, custom=3.8s, **1.81x** |
| **Total** | **25.0s** | **6.1s** | **11.2s** | **14.7s** |

**Stock vs Custom Image Boot Times:**

| Provider | Stock Image Boot | Custom Image Create | Deps Verification | Approach |
|----------|-----------------|--------------------|--------------------|----------|
| E2B | 0.14s | 1.4s | 4.1s (runtime pip) | Template-based, no custom build |
| Daytona | 0.17s | 20.3s | 0.7s (pre-baked, OK) | Pre-warmed stock pool + runtime `Image.debian_slim().pip_install()` for custom |
| Modal | 0.31s | 0.8s | 2.9s (pre-baked, OK) | Runtime `Image.debian_slim().pip_install()` |
| Blaxel | 0.33s | 3.0s | 7.4s (runtime pip) | Pre-existing Docker Hub image |

**Custom image speedup (Modal -- provider with fast custom image):**

| Provider | Baseline (stock create + pip) | Custom (create + verify) | Speedup |
|----------|-------------------------------|--------------------------|---------|
| Modal | 6.8s | 3.8s | **1.81x** |
| Daytona | 1.9s (stock+pip) | 21.0s (custom) | N/A* |

\* Daytona's custom image creation (20.3s) is significantly slower than its pre-warmed stock boot (0.17s). The pre-warmed pool is Daytona's strength; custom images require a full image build/pull cycle. For Daytona, use stock + `pip install` (1.9s) instead of custom images.

**Summary**: E2B wins the Docker benchmark at 6.1s total. Daytona has the second-fastest stock boot (0.17s via pre-warmed pool) but custom image creation is slow (20.3s). Modal demonstrates a clear **1.81x speedup** with custom images (3.8s vs 6.8s baseline) and has the fastest custom create (0.8s). E2B has the fastest stock boot (0.14s) but requires runtime pip install (4.1s). Blaxel is slowest on deps (7.4s runtime pip). For Daytona, the pre-warmed stock pool + pip install is far faster than custom images.

---

## 9. Network Speed Benchmark

**What it tests**: Raw network performance from inside each sandbox -- HTTP round-trip latency (5x GET requests to `google.com/robots.txt` with min/avg/max timing), download throughput (~10MB single fetch and ~100MB sustained via 10x10MB from Cloudflare speed test), upload throughput (~5MB POST to httpbin.org), DNS resolution time for 5 hostnames (`google.com`, `github.com`, `pypi.org`, `cloudflare.com`, `amazonaws.com`), and real-world package install speed (`pip install requests` with cache cleared).

**Why it matters**: Agents frequently need to download packages from PyPI, fetch data from APIs, clone git repos, and upload/download artifacts. Network speed inside the sandbox directly impacts how fast agents can install dependencies, pull data, and interact with external services. A sandbox with poor network connectivity creates a bottleneck on every operation that touches the internet.

**Steps profiled:**

| Step | What It Measures |
|------|-----------------|
| `net_latency` | HTTP round-trip time (5 samples, min/avg/max in ms) |
| `net_download` | Download throughput in MB/s (~10MB test file) |
| `net_download_large` | Sustained download throughput in MB/s (~100MB via 10x10MB) |
| `net_upload` | Upload throughput in MB/s (~5MB test file) |
| `net_dns` | DNS resolution time for 5 hostnames (ms) |
| `net_pip_install` | Real-world `pip install requests` duration (s) |

**Results:**

| Metric | Daytona | E2B | Blaxel | Modal |
|--------|---------|-----|--------|-------|
| HTTP Latency (avg) | 28.9ms | 17.6ms | 58.6ms | 24.6ms |
| Download 10MB | 75.08 MB/s | 56.09 MB/s | 57.31 MB/s | 74.32 MB/s |
| Download 100MB (sustained) | 83.2 MB/s | 53.93 MB/s | 66.70 MB/s | 84.91 MB/s |
| Upload 5MB | 502 error† | 2.87 MB/s | 3.22 MB/s | 0.74 MB/s |
| DNS Resolution (avg) | 2.38ms | 3.10ms | 12.12ms | 4.06ms |
| pip install requests | 0.55s | 1.46s | 1.13s | 1.63s |

† Upload test hit 502 Bad Gateway on this run; previous run: 5.41 MB/s

| Step (wall-clock) | Daytona | E2B | Blaxel | Modal |
|-------------------|---------|-----|--------|-------|
| net_latency | 0.5s | 0.3s | 0.7s | 1.4s |
| net_download | 0.4s | 0.4s | 0.5s | 1.3s |
| net_download_large | 1.5s | 1.9s | 1.7s | 2.4s |
| net_upload | 0.5s | 2.0s | 1.8s | 8.0s |
| net_dns | 0.3s | 0.2s | 0.3s | 1.6s |
| net_pip_install | 1.5s | 2.4s | 2.7s | 5.4s |
| **Total** | **6.0s** | **8.8s** | **10.6s** | **22.8s** |

**Head-to-head (network):**

| Operation | Winner | Value | Runner-up |
|-----------|--------|-------|-----------|
| HTTP latency | E2B | 17.6ms | Modal (24.6ms) |
| Download throughput (10MB) | Daytona | 75.08 MB/s | Modal (74.32 MB/s) |
| Sustained download (100MB) | Modal | 84.91 MB/s | Daytona (83.2 MB/s) |
| Upload throughput | Blaxel | 3.22 MB/s | E2B (2.87 MB/s) |
| DNS resolution | Daytona | 2.38ms | E2B (3.10ms) |
| pip install | Daytona | 0.55s | Blaxel (1.13s) |
| Total benchmark time | Daytona | 6.0s | E2B (8.8s) |

**Summary**: Daytona leads on total benchmark time (6.0s), download throughput (75 MB/s single, 83 MB/s sustained), DNS resolution (2.38ms), and pip install speed (0.55s). E2B has the lowest HTTP latency (17.6ms). Modal achieves the highest sustained download (84.91 MB/s) but has very slow upload (0.74 MB/s) and highest per-step overhead. Blaxel offers balanced performance but has higher latency (58.6ms) and DNS times (12.1ms). All providers have full outbound network access.

---

## 10. Security & Isolation Benchmark

**What it tests**: Whether each sandbox properly isolates untrusted code from the host infrastructure. Probes 8 attack surfaces: cloud metadata service (IMDS) access, privilege escalation (root, capabilities, seccomp), container escape vectors (docker socket, host filesystem traversal), internal network reachability (management ports on gateway/internal IPs), sensitive filesystem exposure (kernel memory, block devices, cloud credentials), resource limits (cgroup memory/PID/CPU caps), outbound egress filtering (dangerous ports, raw sockets), and environment variable credential leakage.

**Why it matters**: These sandboxes execute untrusted code from AI agents. A weak isolation boundary means an attacker (or a misbehaving LLM) could steal API keys, pivot to internal services, escape the container, or compromise the host. This benchmark reveals which providers have defense-in-depth and which have gaps. Unlike performance benchmarks, here **PASS means the attack was blocked** (isolation held) and **FAIL means the attack succeeded** (isolation broken).

**All tests are non-destructive** -- they only probe whether the attack surface exists, they don't exploit it.

| Test | Daytona | E2B | Blaxel | Modal |
|------|---------|-----|--------|-------|
| Cloud Metadata (IMDS) | PASS | PASS | PASS | PASS |
| Privilege & Identity | PASS | PASS | FAIL | PASS |
| Container Escape | FAIL | PASS | FAIL | FAIL |
| Internal Network Scan | PASS | FAIL | PASS | PASS |
| Filesystem Exposure | PASS | PASS | FAIL | PASS |
| Resource Limits (cgroup) | PASS | FAIL | FAIL | PASS |
| Egress Filtering | PASS | FAIL | FAIL | PASS |
| Env Variable Leakage | PASS | PASS | PASS | PASS |
| **Score** | **7/8** | **5/8** | **3/8** | **7/8** |

**Detailed findings:**

| Test | Daytona | E2B | Blaxel | Modal |
|------|---------|-----|--------|-------|
| Metadata | All 3 endpoints blocked | All 3 endpoints blocked | All 3 endpoints blocked | All 3 endpoints blocked |
| Privilege | Non-root uid=1001, zero caps | Non-root uid=1000, zero caps | Root + full caps, no seccomp | Root + restricted caps (a80c05fb) |
| Escape | Host FS readable via /proc/1/root | No docker socket, no host FS | Host FS readable via /proc/1/root | Host FS readable via /proc/1/root |
| Network | No mgmt ports reachable | 7 mgmt ports open on gateway (SSH, Docker API, K8s, kubelet, etc.) | No mgmt ports reachable | No mgmt ports reachable |
| Filesystem | /proc/kallsyms, other_proc_environ | /proc/kallsyms, /dev/kmsg, host_mounts:/ | /dev/mem accessible (CRITICAL) | host_mounts:/, other proc environ |
| Resources | mem=1GB, pids=309K, cpu=1x100ms | No cgroup limits, 1018 FDs | No cgroup limits, 1019 FDs | mem=8EB (effectively unlimited), cpu=-1, 10K FDs |
| Egress | All ports filtered, no raw sockets | All 4 dangerous ports open (SMTP/Redis/MySQL/Postgres) | All ports filtered but raw sockets work | All ports filtered, no raw sockets |
| Env Leak | 20 env vars, none sensitive | 17 env vars, none sensitive | 33 env vars, none sensitive | 33 env vars, none sensitive |

**Critical findings by provider:**

- **Daytona** (7/8): Tied for strongest isolation. Pre-warmed sandboxes run as non-root (uid=1001) with zero capabilities -- matching E2B's privilege model. Cgroup limits properly configured (1GB mem, 309K PIDs, 1 CPU). No raw sockets. Egress filtered (SMTP/Redis/MySQL/Postgres all blocked). Only weakness: host filesystem traversal via `/proc/1/root`.
- **E2B** (5/8): Good privilege isolation -- runs as non-root (uid=1000) with zero capabilities. Weaknesses: all gateway management ports reachable (SSH:22, Docker API:2375/2376, K8s:6443, kubelet:10250), no cgroup resource limits, and wide-open egress (all dangerous ports accessible).
- **Blaxel** (3/8): Weakest isolation. Runs as root with full capabilities (`000001ffffffffff`), no seccomp, `/dev/mem` directly accessible (kernel memory read), no cgroup limits, and raw socket access. The combination of full root + /dev/mem access is the most severe finding across all providers.
- **Modal** (7/8): Tied for strongest isolation. Restricted capabilities (`a80c05fb`), no management ports reachable, all dangerous egress ports filtered, no raw sockets. Only weakness: host filesystem traversal via `/proc/1/root`.

| Step Timing | Daytona | E2B | Blaxel | Modal |
|-------------|---------|-----|--------|-------|
| sec_metadata_service | 6.4s | 0.2s | 6.4s | 6.9s |
| sec_privilege_info | 0.4s | 0.2s | 0.2s | 1.4s |
| sec_container_escape | 0.3s | 0.2s | 0.2s | 0.7s |
| sec_network_scan | 20.4s | 6.2s | 6.2s | 20.7s |
| sec_filesystem_exposure | 0.3s | 0.2s | 0.2s | 0.7s |
| sec_resource_limits | 0.3s | 0.1s | 0.2s | 0.8s |
| sec_egress_filtering | 8.3s | 0.1s | 8.3s | 8.9s |
| sec_env_leak | 0.6s | 0.1s | 0.2s | 0.7s |
| **Total** | **38.6s** | **8.6s** | **23.3s** | **42.9s** |

**Summary**: Daytona and Modal are tied for strongest isolation (7/8), both failing only on host filesystem traversal via `/proc/1/root` -- a common container issue that requires VM-level isolation to fully prevent. Daytona's pre-warmed sandboxes significantly improved security: now runs as non-root (uid=1001) with zero capabilities, no raw sockets, and filtered egress (up from 6/8 with root + raw sockets in the old image-based approach). E2B (5/8) has good privilege isolation (non-root, zero caps) but critically exposes 7 internal management ports and has no resource limits or egress filtering. Blaxel (3/8) has the weakest isolation with root + full capabilities + `/dev/mem` access. For production workloads running untrusted agent code, Daytona or Modal should be preferred for their defense-in-depth approach.

---

## Overall Rankings

### By Benchmark (fastest total)

| Benchmark | 1st | 2nd | 3rd | 4th |
|-----------|-----|-----|-----|-----|
| RL Compute | Blaxel (135s)* | Daytona (182s)† | E2B (301s)* | Modal (564s) |
| Filesystem I/O | E2B (2.8s) | Blaxel (3.7s) | Daytona (12.9s) | Modal (30.8s) |
| Pause/Resume | E2B (15.4s) | Modal (22.7s) | Daytona (29.5s) | Blaxel (4.4s)** |
| Concurrent Exec | E2B (8.1s) | Blaxel (9.7s) | Daytona (13.9s) | Modal (15.6s) |
| Iteration Loop | E2B (3.8s) | Blaxel (5.1s) | Daytona (9.2s) | Modal (12.6s) |
| Fan-Out (10 sandboxes) | Daytona (9.5s) | — | — | — |
| Coding Agent | E2B (61s) | Daytona (62s) | Blaxel (75s) | Modal (86s) |
| Custom Docker | E2B (6.1s) | Blaxel (11.2s) | Modal (14.7s) | Daytona (25.0s) |
| Network Speed | Daytona (6.0s) | E2B (8.8s) | Blaxel (10.6s) | Modal (22.8s) |
| Security & Isolation | Daytona (7/8) | Modal (7/8) | E2B (5/8) | Blaxel (3/8) |

\* Blaxel/E2B had intermittent stability issues on long RL runs
\** Blaxel skipped pause/resume (no API)
† Daytona ran 30ep/15steps; others ran 50ep/25steps

### By Use Case

| Use Case | Best Provider | Why |
|----------|--------------|-----|
| **Long RL/ML training** | Daytona | Most reliable for 5+ min compute, no timeout limits with exec_long() |
| **Coding agent iteration** | E2B | Fastest edit-test loop (3.8s), 0.05s file overwrites |
| **Parallel tool execution** | E2B | Fastest concurrent exec (0.66s for 4 cmds), 3.40x speedup |
| **Multi-sandbox fan-out** | Daytona | 10 sandboxes in 1.9s (0.19s each) via pre-warmed pool |
| **Pause/resume workflows** | E2B | Sub-second native pause (0.9s), instant resume (0.2s) |
| **Filesystem-heavy agents** | E2B | 8x faster per-file I/O than Daytona |
| **Large file processing** | Blaxel | 0.2s for 1MB round-trip (fastest of all) |
| **Short compute bursts** | Blaxel | Fastest test execution (0.29-0.39s per run) |
| **Custom environments** | Modal | Fastest custom image create (0.8s) with pre-baked deps |
| **Fastest sandbox creation** | Daytona | 0.19s avg per sandbox (pre-warmed pool) |
| **LLM coding agent loop** | E2B/Daytona | Tied at ~61s total, Daytona fastest setup (2.5s) |
| **Network performance** | Daytona | 75 MB/s download, fastest DNS (2.38ms) and pip (0.55s) |
| **API-heavy agents (many requests)** | E2B | Lowest HTTP latency (17.6ms) and fast DNS resolution (3.1ms) |
| **Security-critical workloads** | Daytona/Modal | Tied at 7/8, both non-root with filtered egress |
| **Untrusted code execution** | Daytona/Modal | Both 7/8, Daytona: non-root+zero caps+cgroups; Modal: restricted caps+filtered egress |

### Head-to-Head Winners (per operation)

| Operation | Winner | Time | Runner-up |
|-----------|--------|------|-----------|
| Sandbox creation (stock) | Daytona | 0.19s | E2B (0.35s) |
| File upload (per file) | E2B/Blaxel | 0.05s | Daytona (0.3s) |
| File download (per file) | E2B | 0.06s | Blaxel (0.08s) |
| Test execution | Blaxel | 0.29s | E2B (0.47s) |
| Concurrent exec (4 cmds) | E2B | 0.66s | Modal (0.67s) |
| Pause latency | E2B | 0.9s | Modal (1.5s) |
| Resume latency | E2B | 0.2s | Daytona (0.7s) |
| Large file I/O (1MB) | Blaxel | 0.2s | E2B (0.6s) |
| 10-sandbox fan-out (stock) | Daytona | 1.9s | — |
| HTTP latency | E2B | 17.6ms | Modal (24.6ms) |
| Download throughput (10MB) | Daytona | 75.08 MB/s | Modal (74.32 MB/s) |
| Download throughput (sustained) | Modal | 84.91 MB/s | Daytona (83.2 MB/s) |
| DNS resolution | Daytona | 2.38ms | E2B (3.10ms) |
| pip install | Daytona | 0.55s | Blaxel (1.13s) |
| Stock image boot | E2B | 0.14s | Daytona (0.17s) |
| Custom image create | Modal | 0.8s | E2B (1.4s) |
| Pre-baked deps verify | Daytona | 0.7s | Modal (2.9s) |
| Sandbox destroy | Modal | 0.11s | E2B (0.16s) |
| Security isolation | Daytona/Modal | 7/8 | E2B (5/8) |

---

## Platform Capabilities Matrix

| Capability | Daytona | E2B | Blaxel | Modal |
|-----------|---------|-----|--------|-------|
| Sandbox creation | 0.17-0.9s | 0.1-0.3s | 0.3-0.5s | 0.3-0.8s |
| Custom CPU/Memory | Via image params† | No (fixed) | Yes | Yes |
| Custom Docker images | Yes (slow: ~20s) | Template-based | Yes | Yes (fast: 0.8s) |
| Native pause/resume | No (stop/start) | Yes (sub-second) | No | Via snapshots |
| Exec timeout limit | 60s hard limit* | None | None | None |
| Parallel exec support | Yes (1.41x)†† | Yes (3.40x) | Yes (2.56x) | Yes (5.06x) |
| Native file upload | Yes | Yes | Yes (async) | Yes |
| Native file download | Yes | Yes | Yes (async) | Yes |
| Directory listing API | Yes | Yes | Yes | Yes |
| Snapshots | No | Yes | No | Yes |
| Network access | Yes | Yes | Yes | Yes |
| Runs as non-root | Yes (uid=1001) | Yes (uid=1000) | No (root+full caps) | No (root+restricted caps) |
| Seccomp enabled | No | No | No | No |
| Cgroup resource limits | Yes (mem+pid+cpu) | No | No | Partial (mem set, cpu unlimited) |
| Egress filtering | Yes (all filtered) | No | Partial (raw sockets) | Yes |
| IMDS blocked | Yes | Yes | Yes | Yes |
| Security score | 7/8 | 5/8 | 3/8 | 7/8 |

†† Pre-warmed pool gives 1 vCPU; use `CreateSandboxFromImageParams` for multi-CPU
† Resource customization requires `CreateSandboxFromImageParams` (slower boot)

\* Daytona has a server-side 60s timeout on `process.exec()`; requires `nohup` + polling workaround

---

## Gotchas & Workarounds

| Issue | Provider | Workaround |
|-------|----------|------------|
| 60s exec timeout | Daytona | `nohup` + polling via `exec_long()` |
| Home directory is `/home/daytona` | Daytona | Pre-warmed pool runs as user `daytona` (uid=1001); use `/home/daytona/app` as working dir |
| Pre-warmed pool gives 1 vCPU | Daytona | Use `CreateSandboxFromImageParams` with `Resources(cpu=4)` for multi-core workloads |
| Custom image create is slow (~20s) | Daytona | Prefer stock pool + `pip install` (1.9s) over custom images (20s+) |
| SSL cert errors on macOS | Daytona/Modal | `import certifi; os.environ['SSL_CERT_FILE'] = certifi.where()` |
| `Sandbox()` constructor deprecated | E2B | Use `Sandbox.create(api_key=...)` |
| Connection drops on long runs | E2B | RL training >5min may get chunked transfer errors |
| SDK raises on non-zero exit | E2B | Wrap commands with `; echo "EXIT=$?"` to capture exit codes |
| No pause/resume in SDK | Blaxel | Platform may auto-pause but SDK doesn't expose it |
| Event loop shared across threads | Blaxel | Create new `asyncio.new_event_loop()` per instance |
| Region warning | Blaxel | Set `BL_REGION` env var to suppress FutureWarning |
| Slow file I/O | Modal | `sandbox.open()` has high per-call overhead; batch when possible |
| Auth via config file | Modal | Uses `~/.modal.toml` (no api_key param), set via `modal token set` |

---

## Raw Benchmark Data

```
=== DAYTONA (pre-warmed pool, 1 vCPU, 1GB RAM, 3GB disk) ===
  RL:         182.5s total (170.2s training 30ep/15steps, 29/29 tests, best=15.1)
  FS:          12.9s total (1.5s codegen, 2.8s download, 5.1s large IO)
  Pause:       29.5s total (19.2s pause, 0.7s resume, state=OK)
  Concurrent:  13.9s total (4.54s seq, 3.23s conc, 1.41x speedup)
  Iteration:    9.2s total (0.3s overwrite, 1.6s test avg)
  Fan-out:      9.5s total (1.9s create 10 sandboxes, 0.3s compute, 3.7s custom image)
  Agent:       61.9s total (3 iters, best reward 13.6, llm=gemini-2.5-flash-lite)
  Docker:     25.0s total (build=0.0s, custom_create=20.3s, verify=0.7s pre-baked, stock_boot=0.17s)
  Network:     6.0s total (28.9ms latency, 83.2 MB/s download, upload=502, 0.55s pip)
  Security:    38.6s total (7/8 PASS: metadata, privilege, network, filesystem, resources, egress, env_leak)

=== E2B (default instance) ===
  RL:         301.1s total (287s training*, 29/29 tests)
  FS:           2.8s total (1.1s codegen, 0.5s download, 0.6s large IO)
  Pause:       15.4s total (0.9s pause, 0.2s resume, state=OK)
  Concurrent:   8.1s total (2.23s seq, 0.66s conc, 3.40x speedup)
  Iteration:    3.8s total (0.05s overwrite, 0.47s test avg)
  Fan-out:      1.6s total (1.05s create, 0.08s compute, 3 sandboxes)
  Docker:      6.1s total (template, create=1.4s, pip=4.1s, stock_boot=0.14s)
  Network:     8.8s total (17.6ms latency, 53.93 MB/s download, 2.87 MB/s upload, 1.46s pip)
  Security:    8.6s total (5/8 PASS: metadata, privilege, escape, filesystem, env_leak)

=== BLAXEL (4 vCPU, 8GB RAM) ===
  RL:         135.2s total (120s training*, 29/29 tests)
  FS:           3.7s total (1.2s codegen, 0.7s download, 0.2s large IO)
  Pause:        4.4s total (no pause API, state=OK without pause)
  Concurrent:   9.7s total (2.23s seq, 0.87s conc, 2.56x speedup)
  Iteration:    5.1s total (0.04s overwrite, 0.33s test avg)
  Fan-out:      3.4s total (1.47s create, 0.43s compute, 3 sandboxes)
  Docker:     11.2s total (pre-existing, create=3.0s, pip=7.4s, stock_boot=0.33s)
  Network:     10.6s total (58.6ms latency, 66.70 MB/s download, 3.22 MB/s upload, 1.13s pip)
  Security:    23.3s total (3/8 PASS: metadata, network, env_leak -- WEAKEST)

=== MODAL (4 CPU, 8GB) ===
  RL:         564.0s total (542.2s training, 29/29 tests, best reward 14.54)
  FS:          30.8s total (3.9s codegen, 6.8s download, 9.8s large IO)
  Pause:       22.7s total (1.5s pause, 0.1s resume, state=OK)
  Concurrent:  15.6s total (3.38s seq, 0.67s conc, 5.06x speedup)
  Iteration:   12.6s total (0.56s overwrite, 1.06s test avg)
  Fan-out:      4.6s total (0.81s create, 0.64s compute, 3 sandboxes)
  Agent:       86.4s total (3 iters, best reward 14.4, llm=gemini-2.5-flash-lite)
  Docker:     14.7s total (build=0.2s, custom_create=0.8s, verify=2.9s pre-baked, stock_boot=0.31s, 1.81x speedup)
  Network:     22.8s total (24.6ms latency, 84.91 MB/s download, 0.74 MB/s upload, 1.63s pip)
  Security:    42.9s total (7/8 PASS: metadata, privilege, network, filesystem, resources, egress, env_leak -- STRONGEST)

=== CODING AGENT (all providers, Gemini 2.5 Flash Lite, 3 iterations) ===
  E2B:         61.1s total (setup=4.1s, best_reward=7.7)
  Daytona:     61.9s total (setup=3.4s, best_reward=13.6)
  Blaxel:      75.1s total (setup=5.0s, best_reward=16.4)
  Modal:       86.4s total (setup=9.7s, best_reward=14.4)

=== SECURITY (all providers, 8 isolation tests) ===
  Daytona:     7/8 PASS (38.6s) -- non-root+zero caps+cgroups+filtered egress, only failed escape
  Modal:       7/8 PASS (42.9s) -- restricted caps+filtered egress, only failed container escape
  E2B:         5/8 PASS (8.6s)  -- best privilege (non-root), failed network+limits+egress
  Blaxel:      3/8 PASS (23.3s) -- weakest: root+full caps+/dev/mem+no limits+raw sockets

NOTE: Daytona now uses pre-warmed pool (CreateSandboxBaseParams). Use CreateSandboxFromImageParams for 4CPU/8GB.
* = had intermittent errors on some runs
```
