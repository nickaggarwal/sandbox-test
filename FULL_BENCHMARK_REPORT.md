# Cloud Sandbox Benchmark Report: Full Results

**Providers tested**: Daytona, E2B, Blaxel, Modal, Runloop
**Date**: March 20, 2026 (updated with Runloop provider across all 9 benchmarks)
**Python**: 3.12 (host and sandbox images)
**SDKs**: Daytona v0.148, E2B Code Interpreter v2.4.1, Blaxel v0.2.44, Modal v0.74+, Runloop API Client (latest)
**Instance specs**: Daytona (python:3.12-slim, 4 CPU, 8GB RAM, 10GB disk), E2B (default), Blaxel (4 vCPU, 8GB), Modal (4 CPU, 8GB), Runloop (default devbox)

> **Note**: Daytona uses `CreateSandboxFromImageParams(image='python:3.12-slim')` with `Resources(cpu=4, memory=8, disk=10)` for fast boot (~0.8s) with multi-core CPU. This runs as root with home at `/root/`. The host has 48 CPUs visible to the sandbox.

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

---

## 1. RL Compute Benchmark

**What it tests**: End-to-end performance for compute-heavy workloads -- creating a sandbox, uploading a Django project, installing dependencies, running 29 unit tests, training an RL agent (REINFORCE policy gradient, 50 episodes, 25 max steps), and retrieving results.

**Why it matters**: This is the most realistic workload for agents running long compute jobs in sandboxes. It tests the full pipeline from sandbox creation to result extraction, with the majority of time spent on CPU-bound RL training.

| Step | Daytona | E2B | Blaxel | Modal | Runloop |
|------|---------|-----|--------|-------|---------|
| Create Sandbox | 0.9s | 0.2s | 0.4s | 0.3s | 1.7s |
| Upload Project | 1.8s | 0.4s | 0.5s | 2.9s | 2.1s |
| Install Deps | 5.6s | 5.5s | 6.6s | 7.2s | 7.3s |
| Run Tests (29) | 6.1s | 7.7s | 6.7s | 9.7s | 9.7s |
| RL Training | 147.8s† | 287s* | 120s** | 542.2s | 338.4s† |
| Retrieve Results | 0.5s | ~1s | ~1s | 1.3s | 0.1s |
| **Total** | **164.7s**† | **301.1s** | **135.2s** | **564.0s** | **359.7s**† |

\* E2B hit connection timeouts on some long runs
\** Blaxel hit timeout/HTML error on some runs
† Daytona and Runloop ran 30 episodes / 15 max-steps; others ran 50/25

**Summary**: Daytona with python:3.12-slim achieves fast boot (0.9s) and the fastest RL training at 147.8s (30 ep/15 steps) with 29/29 tests and best reward 15.1, benefiting from 4 CPU allocation. Runloop completed the full pipeline reliably (29/29 tests, best reward 15.1, avg 10.81) at 359.7s total -- RL training took 338.4s (30 ep/15 steps), placing it between E2B and Modal. Modal completed the full RL pipeline reliably (29/29 tests, best reward 14.54) but was the slowest at 564s total. E2B was faster but experienced connection drops on runs exceeding 5 minutes. Blaxel posted the fastest raw times when it worked, but had stability issues on long-running compute. Note: Runloop devboxes lack `libsqlite3.so.0` by default; the benchmark downloads the library from Debian repos and uses `LD_LIBRARY_PATH` to make Django/SQLite work.

---

## 2. Filesystem I/O Benchmark

**What it tests**: Agent-style file operations -- generating 10 Python source files (small to 20KB) via exec, compiling them to .pyc, uploading 5 files (32KB total) via native FS API, downloading 9 files back, round-tripping pip wheel packages (download in sandbox, retrieve via FS API, upload back, verify checksums), and listing directory contents.

**Why it matters**: Coding agents constantly read and write files in sandboxes. The speed of native filesystem APIs directly determines how fast an agent can upload code patches, download build artifacts, and manage project files. Slow file I/O creates a bottleneck on every iteration.

| Step | Daytona | E2B | Blaxel | Modal | Runloop |
|------|---------|-----|--------|-------|---------|
| Code Generation (10 files) | 1.8s | 1.1s | 1.2s | 3.9s | 5.3s |
| Build/Compile (.pyc) | 0.4s | 0.2s | 0.3s | 0.8s | 1.0s |
| Native File Upload (5 files) | 1.5s | 0.3s | 0.3s | 6.1s | 1.7s |
| Native File Download (9 files) | 2.6s | 0.5s | 0.7s | 6.8s | 2.3s |
| Pip Package I/O (1MB round-trip) | 6.8s | 0.6s | 0.2s | 9.8s | 12.0s |
| List & Verify | 0.3s | 0.1s | 0.2s | 0.5s | 1.3s |
| **Total** | **14.7s** | **2.8s** | **3.7s** | **30.8s** | **25.0s** |

| Operation | Daytona | E2B | Blaxel | Modal | Runloop |
|-----------|---------|-----|--------|-------|---------|
| Upload per file | ~0.32s | ~0.06s | ~0.06s | ~1.22s | ~0.34s |
| Download per file | ~0.31s | ~0.06s | ~0.08s | ~0.76s | ~0.26s |
| Large file (710KB) | ~1.7s | ~0.3s | ~0.1s | ~3.3s | ~3.0s |

**Summary**: E2B dominates overall filesystem performance at 2.8s total. Blaxel is a close second at 3.7s and wins on large file I/O (0.2s for 1MB vs E2B's 0.6s). Runloop performs well on per-file upload (0.34s) and download (0.26s) -- comparable to Daytona -- but its pip package round-trip is slow (12.0s). Modal is the slowest at 30.8s. Daytona improved to 12.9s with pre-warmed sandboxes. For file-heavy agent workflows, E2B or Blaxel should be preferred.

---

## 3. Async Task + Pause/Resume Benchmark

**What it tests**: Starting a background Python task that writes progress to disk, writing checkpoint files (JSON, YAML, 10KB binary) while the task runs, pausing the sandbox mid-execution, resuming it, verifying all filesystem state survived the pause/resume cycle (including binary data integrity), restarting a compute task after resume, and downloading all results.

**Why it matters**: Agents often need to suspend work and resume later -- for cost savings, context switching between tasks, or handling interruptions. Pause/resume preserves the full sandbox state without re-creating from scratch. This benchmark measures whether you can trust the sandbox to come back exactly as you left it.

| Step | Daytona | E2B | Blaxel | Modal | Runloop |
|------|---------|-----|--------|-------|---------|
| Start Background Task | 2.5s | 12.3s* | 2.3s | 3.1s | 13.0s |
| Write State Files | 0.9s | 0.4s | 0.1s | 1.8s | 0.6s |
| Pause Sandbox | 22.3s | 0.9s | N/A** | 1.5s | N/A*** |
| Resume Sandbox | 0.7s | 0.2s | N/A** | 0.1s | N/A*** |
| Verify State After Resume | 2.2s | 0.5s | 0.4s | 7.1s | 1.2s |
| Restart Compute | 0.7s | 0.3s | 0.2s | 2.6s | 0.9s |
| Download & Verify | 1.7s | 0.3s | 0.3s | 4.6s | 1.2s |
| **Total** | **32.5s** | **15.4s** | **4.4s** | **22.7s** | **20.3s** |

\* E2B bg task completed before pause due to timing
\** Blaxel SDK does not expose pause/resume APIs
\*** Runloop pause/resume requires account capability (ACCOUNT_CAPABILITY_DEVBOX_SUSPEND_AND_RESUME)

| Metric | Daytona | E2B | Modal | Runloop |
|--------|---------|-----|-------|---------|
| Pause mechanism | `sandbox.stop()` | `sandbox.pause()` (native) | Snapshot-based | `devbox.suspend()` (capability gated) |
| Resume mechanism | `sandbox.start()` | `Sandbox.connect(id)` | New sandbox from snapshot | `devbox.resume()` |
| Pause latency | 19-26s | 0.8-0.9s | 1.5s | N/A (403) |
| Resume latency | 0.6-0.7s | 0.2s | 0.1s | N/A (403) |
| Filesystem preserved | Yes | Yes | Yes | Yes (no pause) |
| Binary integrity | Yes | Partial* | Yes | Yes (no pause) |

\* E2B `files.read()` returned different byte representation for binary files after resume

**Summary**: E2B has native pause/resume with sub-second latency (0.9s pause, 0.2s resume), making it ideal for suspend/resume workflows. Modal supports pause via snapshots with 1.5s pause and 0.1s resume -- the fastest resume of all providers -- but its slow file I/O (7.1s state verify, 4.6s download) brings the total to 22.7s. Daytona uses a stop/start mechanism that takes 19-26s to pause (slower with pre-warmed sandboxes) but reliably preserves all state including binary data. Runloop has suspend/resume APIs but they require a specific account capability not available in the test account; state writes and downloads work well (0.4s write, 1.0s download). Blaxel has no pause/resume API -- its fast "total" time reflects skipping those steps.

---

## 4. Concurrent Exec Benchmark

**What it tests**: Whether a sandbox can handle multiple simultaneous `exec()` calls -- the pattern agents use when firing lint, test, typecheck, and format commands in parallel rather than sequentially. Uploads a Python project with source files, installs pytest/flake8/mypy/black, then runs the same 4 commands first sequentially (baseline) then concurrently via threads.

**Why it matters**: A coding agent that can run `flake8`, `pytest`, `mypy`, and `black` at the same time instead of one-by-one gets feedback 3-5x faster per iteration. This directly translates to faster agent loops. If a sandbox serializes exec() calls, the agent gains nothing from concurrent design.

| Provider | Sequential (4 cmds) | Concurrent (4 cmds) | Speedup | Setup | Total |
|----------|--------------------|--------------------|---------|-------|-------|
| **Daytona** | 2.7s | 0.8s | **3.38x** | 5.5s | 10.3s |
| **E2B** | 2.23s | 0.66s | **3.40x** | 3.9s | 8.1s |
| **Blaxel** | 2.23s | 0.87s | **2.56x** | 5.4s | 9.7s |
| **Modal** | 3.38s | 0.67s | **5.06x** | 9.1s | 15.6s |
| **Runloop** | 3.74s | 2.39s | **1.57x** | 6.8s | 16.1s |

| Command (concurrent) | Daytona | E2B | Blaxel | Modal | Runloop |
|---------|---------|-----|--------|-------|---------|
| flake8 | 2.07s | 0.22s | 0.30s | 0.45s | 1.52s |
| pytest | 3.23s | 0.52s | 0.34s | 0.76s | 1.73s |
| mypy | 0.89s | 1.20s | 1.29s | 1.57s | 2.29s |
| black | 2.29s | 0.29s | 0.46s | 0.60s | 2.39s |

**Summary**: All 5 providers support true parallel exec -- none serialize calls. Modal shows the highest speedup (5.06x) because its sequential commands are the slowest. Daytona with python:3.12-slim (4 CPU) now achieves 3.38x speedup (0.8s concurrent vs 2.7s sequential) and is the second-fastest total at 10.3s. E2B wins on total pipeline time (8.1s) due to faster sandbox creation and pip install. Runloop shows the lowest speedup (1.57x) with higher concurrent command latency (~2.3s for mypy/black), suggesting some exec overhead.

---

## 5. Iteration Loop Benchmark

**What it tests**: The core coding agent cycle -- upload deliberately broken code (a Calculator class with bugs), run tests to detect failures, upload a fixed version, run tests to confirm the fix, add a new feature (power method + new tests), and run final validation. Measures the round-trip latency for each step of the write-test-fix loop.

**Why it matters**: This is the single most important benchmark for coding agents. Every agent iteration involves writing code, running tests, reading output, and writing a fix. The total loop time directly determines how many iterations an agent can complete in a given time budget. A 2x faster loop means 2x more attempts at solving a problem.

| Step | Daytona | E2B | Blaxel | Modal | Runloop |
|------|---------|-----|--------|-------|---------|
| Upload broken code (+ pip install) | 2.6s | 1.16s | 2.93s | 5.23s | 3.8s |
| Run tests (detect 3 failures) | 0.5s | 0.53s | 0.39s | 1.43s | 0.8s |
| Upload fix (overwrite 1 file) | 0.3s | 0.05s | 0.04s | 0.56s | 0.24s |
| Run tests (all 6 pass) | 0.5s | 0.45s | 0.29s | 0.82s | 0.7s |
| Add feature (update 2 files) | 0.6s | 0.09s | 0.14s | 1.19s | 0.35s |
| Final validation (9/9 pass) | 0.5s | 0.44s | 0.30s | 0.92s | 0.7s |
| **Total (excl. create/destroy)** | **5.0s** | **2.72s** | **4.09s** | **10.15s** | **6.6s** |
| **Total (incl. create/destroy)** | **6.3s** | **3.82s** | **5.12s** | **12.63s** | **9.9s** |

| Operation | Daytona | E2B | Blaxel | Modal | Runloop |
|-----------|---------|-----|--------|-------|---------|
| File overwrite | 0.3s | 0.05s | 0.04s | 0.56s | 0.24s |
| Test exec (avg) | 0.5s | 0.47s | 0.33s | 1.06s | 0.73s |
| Multi-file update | 0.6s | 0.09s | 0.14s | 1.19s | 0.35s |

**Summary**: E2B delivers the fastest total iteration loop (3.8s including sandbox lifecycle). Its file operations are extremely fast (0.05s per overwrite). Daytona with python:3.12-slim (4 CPU) dramatically improved test execution (0.5s avg, down from 1.6s with 1 vCPU) and is now second-fastest at 6.3s total. Blaxel has the fastest single test execution (0.29-0.39s). Runloop is competitive on file overwrites (0.24s) and multi-file updates (0.35s) but has slower test execution (0.73s avg), landing at 9.9s total. Modal is the slowest overall.

---

## 6. Multi-Sandbox Fan-Out Benchmark

**What it tests**: Spinning up 10 sandboxes simultaneously, uploading a compute script to all of them concurrently, running different compute tasks on each (factorial, fibonacci, prime sieve, sort -- cycled across sandboxes), collecting results from all sandboxes, and destroying them all. Every step uses ThreadPoolExecutor for maximum parallelism.

**Why it matters**: Advanced agents fan out across multiple sandboxes to try different approaches simultaneously, run tests on different configurations, or split large workloads. The fan-out pattern requires fast sandbox creation, efficient parallel I/O, and reliable concurrent operations. Slow sandbox creation is the biggest bottleneck since it's multiplied by N.

**Results (10 sandboxes, all providers):**

| Step | Daytona | E2B | Blaxel | Modal | Runloop |
|------|---------|-----|--------|-------|---------|
| Create 10 sandboxes | 1.7s | 2.4s | 3.9s | 0.9s | 4.0s |
| Upload code to all | 1.9s | 0.2s | 3.3s | 7.5s | 1.0s |
| Run 10 different tasks | 0.3s | 0.1s | 1.6s | 0.6s | 0.6s |
| Collect results | 1.2s | 0.1s | 0.5s | 0.7s | 0.4s |
| Destroy all | 0.3s | 0.2s | 1.3s | 0.1s | 0.4s |
| Custom image (10 sandboxes) | 1.5s | — | — | 0.6s | — |
| **Total** | **7.0s** | **3.0s** | **10.6s** | **10.4s** | **6.2s** |

| Metric | Daytona | E2B | Blaxel | Modal | Runloop |
|--------|---------|-----|--------|-------|---------|
| Avg creation per sandbox | 0.17s | 0.24s | 0.39s | 0.09s | 0.40s |
| Avg upload per sandbox | 0.19s | 0.02s | 0.33s | 0.75s | 0.10s |
| Avg compute per task | 0.03s | 0.01s | 0.16s | 0.06s | 0.06s |
| Total I/O (upload + collect) | 3.1s | 0.3s | 3.8s | 8.2s | 1.4s |
| Custom image avg per sandbox | 0.15s | — | — | 0.06s | — |

**Head-to-head (fan-out, 10 sandboxes):**

| Operation | Winner | Value | Runner-up |
|-----------|--------|-------|-----------|
| Sandbox creation (10x) | Modal | 0.9s (0.09s each) | Daytona (1.7s) |
| Code upload (10x) | E2B | 0.2s (0.02s each) | Runloop (1.0s) |
| Task execution (10x) | E2B | 0.1s | Daytona (0.3s) |
| Result collection (10x) | E2B | 0.1s | Runloop (0.4s) |
| Sandbox destroy (10x) | Modal | 0.1s | E2B (0.2s) |
| Custom image (10x) | Modal | 0.6s (0.06s each) | Daytona (1.5s) |
| Total pipeline | E2B | 3.0s | Runloop (6.2s) |

**Summary**: E2B dominates the 10-sandbox fan-out at 3.0s total. Runloop is a strong second at 6.2s -- fast upload (1.0s for 10 sandboxes, 0.10s each) and efficient result collection (0.4s). Daytona comes in at 7.0s with python:3.12-slim -- fastest stock creation (1.7s for 10, 0.17s each) and fast custom images (1.5s). Modal has the fastest per-sandbox creation (0.09s each) but its slow file uploads (7.5s) drag total to 10.4s. Blaxel is slowest overall (10.6s) due to high upload and compute times.

---

## 7. Coding Agent Benchmark

**What it tests**: The real LLM-powered coding agent loop -- creating a sandbox, uploading the Django scheduling project, then running 3 iterations of: ask Gemini to generate/fix code, upload the code, run Django tests (29 tests), compute a multi-objective reward score (correctness, code quality, domain logic), and feed the score back to the LLM.

**Why it matters**: Unlike the synthetic Iteration Loop benchmark (#5) which uses pre-written code and measures pure sandbox speed, this benchmark measures **end-to-end agent performance** including LLM inference latency. It answers: "How fast can a real coding agent iterate on a real codebase using this sandbox?" The results are non-deterministic since the LLM generates different code each run.

**LLM**: Gemini 2.5 Flash Lite (via REST API), 3 iterations, reward threshold 25.0

| Step | E2B | Blaxel | Modal | Daytona | Runloop |
|------|-----|--------|-------|---------|---------|
| Setup (sandbox + pip install) | 3.8s | 4.6s | 7.8s | 4.1s | 5.9s |
| Upload Project (38 files) | 0.3s | 0.4s | 1.9s | 0.8s | 1.9s |
| Iteration 1 (LLM + test) | 7.1s | 13.0s | 19.3s | 13.1s | 11.3s |
| Iteration 2 (LLM + test) | 30.7s | 33.0s | 28.6s | 33.7s | 20.3s |
| Iteration 3 (LLM + test) | 19.1s | 23.9s | 28.6s | 27.0s | 18.8s |
| **Total** | **61.1s** | **75.1s** | **86.4s** | **79.0s** | **58.2s** |

| Metric | E2B | Blaxel | Modal | Daytona | Runloop |
|--------|-----|--------|-------|---------|---------|
| Best reward | 7.7 | 16.4 | 14.4 | 15.6 | 8.8 |
| Sandbox overhead (setup+upload) | 4.1s | 5.0s | 9.7s | 4.9s | 7.8s |
| Avg iteration time | 19.0s | 23.3s | 25.5s | 24.6s | 16.8s |

**Iteration Loop (synthetic) vs Coding Agent (LLM) comparison:**

| | Iteration Loop | Coding Agent |
|---|---|---|
| **E2B** | 3.8s (6 steps) | 61.1s (3 LLM iterations) |
| **Runloop** | 9.9s | 58.2s |
| **Blaxel** | 5.1s | 75.1s |
| **Daytona** | 6.3s | 79.0s |
| **Modal** | 12.6s | 86.4s |

**Summary**: Runloop edges out E2B as the fastest total agent loop (58.2s vs 61.1s), driven by faster LLM iteration times (16.8s avg vs 19.0s). E2B has the lowest sandbox overhead (4.1s setup+upload) but Runloop's iteration speed compensates. Iteration times are dominated by LLM latency (5-34s per call), making the sandbox speed difference less impactful than in synthetic benchmarks. The best reward scores varied across providers (7.7 to 16.4) due to non-deterministic LLM outputs, not sandbox differences. For real agent workloads, sandbox choice matters most for setup overhead; once running, LLM API latency is the bottleneck.

---

## 8. Custom Docker Image Benchmark

**What it tests**: How each provider handles building and launching sandboxes from custom Docker images with pre-installed dependencies (Django, DRF, pytest, flake8, numpy). Measures image build time, sandbox creation from the custom image, dependency verification (pre-baked vs runtime pip install), and runs a compute workload to confirm the environment works. Compares custom image performance against a baseline of default image + pip install at runtime.

**Why it matters**: Production agents benefit from custom images with pre-baked dependencies -- it eliminates pip install latency on every sandbox creation. The ability to build and use custom images determines whether an agent can amortize setup cost across many sandbox launches.

| Step | Daytona | E2B | Blaxel | Modal | Runloop |
|------|---------|-----|--------|-------|---------|
| Build Custom Image | 0.0s (runtime) | 0.0s (template) | 0.0s (pre-existing) | 0.2s (runtime) | 0.0s (template) |
| Create Sandbox (custom) | 0.9s | 1.4s | 3.0s | 0.8s | 1.8s |
| Verify Pre-baked Deps | 0.7s (pre-baked) | 4.1s (pip needed) | 7.4s (pip needed) | 2.9s (pre-baked) | 7.9s (pip needed) |
| Run Compute Workload | 0.3s | 0.2s | 0.2s | 0.6s | 0.4s |
| Stock Image Boot | 0.78s | 0.14s | 0.33s | 0.31s | 1.45s |
| Comparison | baseline=6.3s, custom=1.6s, **3.88x** | stock=0.14s, custom=1.4s | stock=0.33s, custom=3.0s | baseline=6.8s, custom=3.8s, **1.81x** | stock=1.45s, custom=1.8s |
| **Total** | **10.5s** | **6.1s** | **11.2s** | **14.7s** | **11.9s** |

**Stock vs Custom Image Boot Times:**

| Provider | Stock Image Boot | Custom Image Create | Deps Verification | Approach |
|----------|-----------------|--------------------|--------------------|----------|
| E2B | 0.14s | 1.4s | 4.1s (runtime pip) | Template-based, no custom build |
| Daytona | 0.78s | 0.9s | 0.7s (pre-baked, OK) | `python:3.12-slim` + `Image.debian_slim().pip_install()` for custom |
| Modal | 0.31s | 0.8s | 2.9s (pre-baked, OK) | Runtime `Image.debian_slim().pip_install()` |
| Blaxel | 0.33s | 3.0s | 7.4s (runtime pip) | Pre-existing Docker Hub image |
| Runloop | 1.45s | 1.8s | 7.9s (runtime pip) | Default devbox, no custom image build |

**Custom image speedup:**

| Provider | Baseline (stock create + pip) | Custom (create + verify) | Speedup |
|----------|-------------------------------|--------------------------|---------|
| Daytona | 6.3s | 1.6s | **3.88x** |
| Modal | 6.8s | 3.8s | **1.81x** |

**Summary**: E2B wins the Docker benchmark at 6.1s total. Daytona with python:3.12-slim dramatically improved custom image performance -- 0.9s create (was 20.3s), achieving a **3.88x speedup** over baseline (1.6s custom vs 6.3s stock+pip). Modal also demonstrates a clear 1.81x speedup. Runloop at 11.5s requires runtime pip install (8.0s) with no custom image build API. Daytona now has the fastest custom image speedup of all providers.

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

| Metric | Daytona | E2B | Blaxel | Modal | Runloop |
|--------|---------|-----|--------|-------|---------|
| HTTP Latency (avg) | 29.8ms | 17.6ms | 58.6ms | 24.6ms | 61.4ms |
| Download 10MB | 80.68 MB/s | 56.09 MB/s | 57.31 MB/s | 74.32 MB/s | 87.37 MB/s |
| Download 100MB (sustained) | 107.12 MB/s | 53.93 MB/s | 66.70 MB/s | 84.91 MB/s | 134.84 MB/s |
| Upload 5MB | 4.09 MB/s | 2.87 MB/s | 3.22 MB/s | 0.74 MB/s | 5.50 MB/s |
| DNS Resolution (avg) | 2.68ms | 3.10ms | 12.12ms | 4.06ms | 1.78ms |
| pip install requests | 1.29s | 1.46s | 1.13s | 1.63s | 1.85s |

| Step (wall-clock) | Daytona | E2B | Blaxel | Modal | Runloop |
|-------------------|---------|-----|--------|-------|---------|
| net_latency | 0.6s | 0.3s | 0.7s | 1.4s | 1.2s |
| net_download | 0.5s | 0.4s | 0.5s | 1.3s | 1.0s |
| net_download_large | 1.4s | 1.9s | 1.7s | 2.4s | 1.6s |
| net_upload | 1.6s | 2.0s | 1.8s | 8.0s | 1.8s |
| net_dns | 0.3s | 0.2s | 0.3s | 1.6s | 0.8s |
| net_pip_install | 3.2s | 2.4s | 2.7s | 5.4s | 4.7s |
| **Total** | **7.6s** | **8.8s** | **10.6s** | **22.8s** | **14.1s** |

**Head-to-head (network):**

| Operation | Winner | Value | Runner-up |
|-----------|--------|-------|-----------|
| HTTP latency | E2B | 17.6ms | Modal (24.6ms) |
| Download throughput (10MB) | Daytona | 80.68 MB/s | Runloop (79.82 MB/s) |
| Sustained download (100MB) | Runloop | 138.51 MB/s | Daytona (107.12 MB/s) |
| Upload throughput | Runloop | 4.12 MB/s | Daytona (4.09 MB/s) |
| DNS resolution | Runloop | 1.65ms | Daytona (2.68ms) |
| pip install | Blaxel | 1.13s | Daytona (1.29s) |
| Total benchmark time | Daytona | 7.6s | E2B (8.8s) |

**Summary**: Runloop delivers the fastest download throughput (87.37 MB/s single, 134.84 MB/s sustained), the fastest upload (5.50 MB/s), and the fastest DNS resolution (1.78ms). Daytona leads on total benchmark time (7.6s). E2B has the lowest HTTP latency (17.6ms). Blaxel has the fastest pip install (1.13s). Modal achieves high download throughput (84.91 MB/s sustained) but has very slow upload (0.74 MB/s). Runloop's higher HTTP latency (61.4ms) and slower pip install (1.85s) bring its total to 14.1s.

---

## Overall Rankings

### By Benchmark (fastest total)

| Benchmark | 1st | 2nd | 3rd | 4th | 5th |
|-----------|-----|-----|-----|-----|-----|
| RL Compute | Blaxel (135s)* | Daytona (165s)† | E2B (301s)* | Runloop (360s)† | Modal (564s) |
| Filesystem I/O | E2B (2.8s) | Blaxel (3.7s) | Daytona (14.7s) | Runloop (25.0s) | Modal (30.8s) |
| Pause/Resume | E2B (15.4s) | Runloop (20.3s)*** | Modal (22.7s) | Daytona (32.5s) | Blaxel (4.4s)** |
| Concurrent Exec | E2B (8.1s) | Blaxel (9.7s) | Daytona (10.3s) | Modal (15.6s) | Runloop (16.1s) |
| Iteration Loop | E2B (3.8s) | Blaxel (5.1s) | Daytona (6.3s) | Runloop (9.9s) | Modal (12.6s) |
| Fan-Out (10 sandboxes) | E2B (3.0s) | Runloop (6.2s) | Daytona (7.0s) | Modal (10.4s) | Blaxel (10.6s) |
| Coding Agent | Runloop (58s) | E2B (61s) | Blaxel (75s) | Daytona (79s) | Modal (86s) |
| Custom Docker | E2B (6.1s) | Daytona (10.5s) | Blaxel (11.2s) | Runloop (11.9s) | Modal (14.7s) |
| Network Speed | Daytona (7.6s) | E2B (8.8s) | Blaxel (10.6s) | Runloop (14.1s) | Modal (22.8s) |

\* Blaxel/E2B had intermittent stability issues on long RL runs
\** Blaxel skipped pause/resume (no API)
\*** Runloop pause/resume requires account capability (skipped, other steps OK)
† Daytona and Runloop ran 30ep/15steps; others ran 50ep/25steps

### By Use Case

| Use Case | Best Provider | Why |
|----------|--------------|-----|
| **Long RL/ML training** | Daytona | Most reliable for 5+ min compute, no timeout limits with exec_long() |
| **Coding agent iteration** | E2B | Fastest edit-test loop (3.8s), 0.05s file overwrites |
| **Parallel tool execution** | E2B | Fastest concurrent exec (0.66s for 4 cmds), 3.40x speedup |
| **Multi-sandbox fan-out** | E2B | 3.0s total for 10 sandboxes, fastest I/O (0.3s upload+collect) |
| **Pause/resume workflows** | E2B | Sub-second native pause (0.9s), instant resume (0.2s) |
| **Filesystem-heavy agents** | E2B | 5x faster per-file I/O than Daytona |
| **Large file processing** | Blaxel | 0.2s for 1MB round-trip (fastest of all) |
| **Short compute bursts** | Blaxel | Fastest test execution (0.29-0.39s per run) |
| **Custom environments** | Daytona | 3.88x speedup with custom images, fastest of all providers |
| **Fastest sandbox creation** | Modal | 0.09s avg per sandbox (10-sandbox fan-out) |
| **LLM coding agent loop** | Runloop | 58.2s total, fastest agent loop (16.8s avg iteration) |
| **Sustained downloads** | Runloop | 134.84 MB/s sustained download (fastest of all providers) |
| **DNS resolution** | Runloop | 1.78ms avg (fastest of all providers) |
| **Network upload** | Runloop | 5.50 MB/s upload throughput (fastest of all providers) |
| **Fan-out (runner-up)** | Runloop | 6.2s total for 10 sandboxes, fast I/O (1.4s upload+collect) |
| **API-heavy agents (many requests)** | E2B | Lowest HTTP latency (17.6ms) and fast DNS resolution (3.1ms) |

### Head-to-Head Winners (per operation)

| Operation | Winner | Time | Runner-up |
|-----------|--------|------|-----------|
| Sandbox creation (stock) | Modal | 0.09s | Daytona (0.17s) |
| File upload (per file) | E2B/Blaxel | 0.05s | Runloop (0.34s) |
| File download (per file) | E2B | 0.06s | Blaxel (0.08s) |
| Test execution | Blaxel | 0.29s | E2B (0.47s) |
| Concurrent exec (4 cmds) | E2B | 0.66s | Modal (0.67s) |
| Pause latency | E2B | 0.9s | Modal (1.5s) |
| Resume latency | E2B | 0.2s | Daytona (0.7s) |
| Large file I/O (1MB) | Blaxel | 0.2s | E2B (0.6s) |
| 10-sandbox fan-out (total) | E2B | 3.0s | Runloop (6.2s) |
| 10-sandbox creation | Modal | 0.9s (0.09s each) | Daytona (1.7s) |
| 10-sandbox fan-out (custom) | Modal | 0.6s | Daytona (1.5s) |
| HTTP latency | E2B | 17.6ms | Modal (24.6ms) |
| Download throughput (10MB) | Runloop | 87.37 MB/s | Daytona (80.68 MB/s) |
| Download throughput (sustained) | Runloop | 134.84 MB/s | Daytona (107.12 MB/s) |
| Upload throughput | Runloop | 5.50 MB/s | Daytona (4.09 MB/s) |
| DNS resolution | Runloop | 1.78ms | Daytona (2.68ms) |
| pip install | Blaxel | 1.13s | Daytona (1.29s) |
| Stock image boot | E2B | 0.14s | Modal (0.31s) |
| Custom image create | Modal | 0.8s | Daytona (0.9s) |
| Custom image speedup | Daytona | 3.88x | Modal (1.81x) |
| Pre-baked deps verify | Daytona | 0.7s | Modal (2.9s) |
| Sandbox destroy | Modal | 0.11s | E2B (0.16s) |

---

## Platform Capabilities Matrix

| Capability | Daytona | E2B | Blaxel | Modal | Runloop |
|-----------|---------|-----|--------|-------|---------|
| Sandbox creation | 0.8-0.9s | 0.1-0.3s | 0.3-0.5s | 0.3-0.8s | 1.3-2.3s |
| Custom CPU/Memory | Yes (via Resources) | No (fixed) | Yes | Yes | No (default) |
| Custom Docker images | Yes (0.9s create) | Template-based | Yes | Yes (fast: 0.8s) | No |
| Native pause/resume | No (stop/start) | Yes (sub-second) | No | Via snapshots | Yes (capability gated) |
| Exec timeout limit | 60s hard limit* | None | None | None | None |
| Parallel exec support | Yes (3.38x) | Yes (3.40x) | Yes (2.56x) | Yes (5.06x) | Yes (1.62x) |
| Native file upload | Yes | Yes | Yes (async) | Yes | Yes (upload + write) |
| Native file download | Yes | Yes | Yes (async) | Yes | Yes (download + read) |
| Directory listing API | Yes | Yes | Yes | Yes | Via exec |
| Snapshots | No | Yes | No | Yes | Yes (disk snapshots) |
| Network access | Yes | Yes | Yes | Yes | Yes |

\* Daytona has a server-side 60s timeout on `process.exec()`; requires `nohup` + polling workaround

---

## Gotchas & Workarounds

| Issue | Provider | Workaround |
|-------|----------|------------|
| 60s exec timeout | Daytona | `nohup` + polling via `exec_long()` |
| Runs as root | Daytona | python:3.12-slim runs as root; use `/root/app` as working dir |
| SSL cert errors on macOS | Daytona/Modal | `import certifi; os.environ['SSL_CERT_FILE'] = certifi.where()` |
| `Sandbox()` constructor deprecated | E2B | Use `Sandbox.create(api_key=...)` |
| Connection drops on long runs | E2B | RL training >5min may get chunked transfer errors |
| SDK raises on non-zero exit | E2B | Wrap commands with `; echo "EXIT=$?"` to capture exit codes |
| No pause/resume in SDK | Blaxel | Platform may auto-pause but SDK doesn't expose it |
| Event loop shared across threads | Blaxel | Create new `asyncio.new_event_loop()` per instance |
| Region warning | Blaxel | Set `BL_REGION` env var to suppress FutureWarning |
| Slow file I/O | Modal | `sandbox.open()` has high per-call overhead; batch when possible |
| Auth via config file | Modal | Uses `~/.modal.toml` (no api_key param), set via `modal token set` |
| `file.write()` uses `file_path` not `path` | Runloop | `file.write(file_path=..., contents=...)` for text; `file.upload(path=..., file=...)` for binary |
| Pause/resume capability gated | Runloop | Requires `ACCOUNT_CAPABILITY_DEVBOX_SUSPEND_AND_RESUME` on account |
| No custom image build API | Runloop | Use default devbox; deps must be installed at runtime via pip |
| Missing `libsqlite3.so.0` in devbox | Runloop | Download `.deb` from Debian repo, extract to `~/.local/lib/`, use `LD_LIBRARY_PATH` |
| No root/sudo access in devbox | Runloop | Cannot `apt-get install`; use pip or download prebuilt binaries |

---

## Raw Benchmark Data

```
=== DAYTONA (python:3.12-slim, 4 CPU, 8GB RAM, 10GB disk) ===
  RL:         164.7s total (147.8s training 30ep/15steps, 29/29 tests, best=15.1)
  FS:          14.7s total (1.8s codegen, 2.6s download, 6.8s large IO)
  Pause:       32.5s total (22.3s pause, 0.7s resume, state=OK)
  Concurrent:  10.3s total (2.7s seq, 0.8s conc, 3.38x speedup)
  Iteration:    6.3s total (0.3s overwrite, 0.5s test avg)
  Fan-out:      7.0s total (1.7s create 10 sandboxes, 0.3s compute, 1.5s custom image)
  Agent:       79.0s total (3 iters, best reward 15.6, llm=gemini-2.5-flash-lite)
  Docker:     10.5s total (build=0.0s, custom_create=0.9s, verify=0.7s pre-baked, stock_boot=0.78s, 3.88x speedup)
  Network:     7.6s total (29.8ms latency, 107.12 MB/s download, 4.09 MB/s upload, 1.29s pip)

=== E2B (default instance) ===
  RL:         301.1s total (287s training*, 29/29 tests)
  FS:           2.8s total (1.1s codegen, 0.5s download, 0.6s large IO)
  Pause:       15.4s total (0.9s pause, 0.2s resume, state=OK)
  Concurrent:   8.1s total (2.23s seq, 0.66s conc, 3.40x speedup)
  Iteration:    3.8s total (0.05s overwrite, 0.47s test avg)
  Fan-out:      3.0s total (2.4s create 10 sandboxes, 0.1s compute, 0.2s upload)
  Docker:      6.1s total (template, create=1.4s, pip=4.1s, stock_boot=0.14s)
  Network:     8.8s total (17.6ms latency, 53.93 MB/s download, 2.87 MB/s upload, 1.46s pip)

=== BLAXEL (4 vCPU, 8GB RAM) ===
  RL:         135.2s total (120s training*, 29/29 tests)
  FS:           3.7s total (1.2s codegen, 0.7s download, 0.2s large IO)
  Pause:        4.4s total (no pause API, state=OK without pause)
  Concurrent:   9.7s total (2.23s seq, 0.87s conc, 2.56x speedup)
  Iteration:    5.1s total (0.04s overwrite, 0.33s test avg)
  Fan-out:     10.6s total (3.9s create 10 sandboxes, 1.6s compute, 3.3s upload)
  Docker:     11.2s total (pre-existing, create=3.0s, pip=7.4s, stock_boot=0.33s)
  Network:     10.6s total (58.6ms latency, 66.70 MB/s download, 3.22 MB/s upload, 1.13s pip)

=== MODAL (4 CPU, 8GB) ===
  RL:         564.0s total (542.2s training, 29/29 tests, best reward 14.54)
  FS:          30.8s total (3.9s codegen, 6.8s download, 9.8s large IO)
  Pause:       22.7s total (1.5s pause, 0.1s resume, state=OK)
  Concurrent:  15.6s total (3.38s seq, 0.67s conc, 5.06x speedup)
  Iteration:   12.6s total (0.56s overwrite, 1.06s test avg)
  Fan-out:     10.4s total (0.9s create 10 sandboxes, 0.6s compute, 7.5s upload, 0.6s custom)
  Agent:       86.4s total (3 iters, best reward 14.4, llm=gemini-2.5-flash-lite)
  Docker:     14.7s total (build=0.2s, custom_create=0.8s, verify=2.9s pre-baked, stock_boot=0.31s, 1.81x speedup)
  Network:     22.8s total (24.6ms latency, 84.91 MB/s download, 0.74 MB/s upload, 1.63s pip)

=== CODING AGENT (all providers, Gemini 2.5 Flash Lite, 3 iterations) ===
  Runloop:     58.2s total (setup=7.8s, best_reward=8.8, avg_iter=16.8s)
  E2B:         61.1s total (setup=4.1s, best_reward=7.7)
  Daytona:     79.0s total (setup=4.9s, best_reward=15.6)
  Blaxel:      75.1s total (setup=5.0s, best_reward=16.4)
  Modal:       86.4s total (setup=9.7s, best_reward=14.4)

=== RUNLOOP (default devbox) ===
  RL:         359.7s total (338.4s training 30ep/15steps, 29/29 tests, best=15.1, avg=10.81)
  FS:          25.0s total (5.3s codegen, 2.3s download, 12.0s large IO) [10 sandboxes avg, 2 runs]
  Pause:       20.3s total (pause/resume N/A - capability gated, state=OK)
  Concurrent:  16.1s total (3.74s seq, 2.39s conc, 1.57x speedup)
  Iteration:    9.9s total (0.24s overwrite, 0.73s test avg)
  Fan-out:      6.2s total (4.0s create 10 sandboxes, 0.6s compute, 1.0s upload)
  Agent:       58.2s total (3 iters, best reward 8.8 avg, llm=gemini-2.5-flash-lite)
  Docker:     11.9s total (no custom build, create=1.8s, pip=7.9s, stock_boot=1.45s)
  Network:     14.1s total (61.4ms latency, 134.84 MB/s download, 5.50 MB/s upload, 1.85s pip)

NOTE: Daytona uses CreateSandboxFromImageParams(image='python:3.12-slim') with Resources(cpu=4, memory=8, disk=10).
* = had intermittent errors on some runs
```
