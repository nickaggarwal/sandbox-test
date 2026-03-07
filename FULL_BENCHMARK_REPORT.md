# Cloud Sandbox Benchmark Report: Full Results

**Providers tested**: Daytona, E2B, Blaxel, Modal
**Date**: March 7, 2026 (updated with Modal full results + Coding Agent benchmark)
**Python**: 3.12 (host and sandbox images)
**SDKs**: Daytona v0.149, E2B Code Interpreter v2.4.1, Blaxel v0.2.44, Modal v0.74+
**Instance specs**: Daytona (4 CPU, 8GB, 10GB disk), E2B (default), Blaxel (4 vCPU, 8GB), Modal (4 CPU, 8GB)

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
| 9 | Network Speed | HTTP latency, download/upload throughput, DNS resolution, pip install | 5 |

---

## 1. RL Compute Benchmark

**What it tests**: End-to-end performance for compute-heavy workloads -- creating a sandbox, uploading a Django project, installing dependencies, running 29 unit tests, training an RL agent (REINFORCE policy gradient, 50 episodes, 25 max steps), and retrieving results.

**Why it matters**: This is the most realistic workload for agents running long compute jobs in sandboxes. It tests the full pipeline from sandbox creation to result extraction, with the majority of time spent on CPU-bound RL training.

| Step | Daytona | E2B | Blaxel | Modal |
|------|---------|-----|--------|-------|
| Create Sandbox | 1.1s | 0.2s | 0.4s | 0.3s |
| Upload Project | 1.0s | 0.4s | 0.5s | 2.9s |
| Install Deps | 5.1s | 5.5s | 6.6s | 7.2s |
| Run Tests (29) | 10.5s | 7.7s | 6.7s | 9.7s |
| RL Training | 368.3s | 287s* | 120s** | 542.2s |
| Retrieve Results | ~1s | ~1s | ~1s | 1.3s |
| **Total** | **389.1s** | **301.1s** | **135.2s** | **564.0s** |

\* E2B hit connection timeouts on some long runs
\** Blaxel hit timeout/HTML error on some runs

**Summary**: Modal completed the full RL pipeline reliably (29/29 tests, best reward 14.54) but was the slowest at 564s total, with RL training taking 542s -- 1.5x slower than Daytona and 4.5x slower than Blaxel. Modal's slow file I/O (2.9s project upload) and higher per-command latency compound over 50 training episodes. Daytona completed RL training most reliably at scale. E2B was faster but experienced connection drops on runs exceeding 5 minutes. Blaxel posted the fastest raw times when it worked, but had stability issues on long-running compute.

---

## 2. Filesystem I/O Benchmark

**What it tests**: Agent-style file operations -- generating 10 Python source files (small to 20KB) via exec, compiling them to .pyc, uploading 5 files (32KB total) via native FS API, downloading 9 files back, round-tripping pip wheel packages (download in sandbox, retrieve via FS API, upload back, verify checksums), and listing directory contents.

**Why it matters**: Coding agents constantly read and write files in sandboxes. The speed of native filesystem APIs directly determines how fast an agent can upload code patches, download build artifacts, and manage project files. Slow file I/O creates a bottleneck on every iteration.

| Step | Daytona | E2B | Blaxel | Modal |
|------|---------|-----|--------|-------|
| Code Generation (10 files) | 3.8s | 1.1s | 1.2s | 3.9s |
| Build/Compile (.pyc) | 0.7s | 0.2s | 0.3s | 0.8s |
| Native File Upload (5 files) | 2.3s | 0.3s | 0.3s | 6.1s |
| Native File Download (9 files) | 4.3s | 0.5s | 0.7s | 6.8s |
| Pip Package I/O (1MB round-trip) | 1.7s | 0.6s | 0.2s | 9.8s |
| List & Verify | 0.8s | 0.1s | 0.2s | 0.5s |
| **Total** | **15.7s** | **2.8s** | **3.7s** | **30.8s** |

| Operation | Daytona | E2B | Blaxel | Modal |
|-----------|---------|-----|--------|-------|
| Upload per file | ~0.46s | ~0.06s | ~0.06s | ~1.22s |
| Download per file | ~0.48s | ~0.06s | ~0.08s | ~0.76s |
| Large file (710KB) | ~1.2s | ~0.3s | ~0.1s | ~3.3s |

**Summary**: E2B dominates overall filesystem performance at 2.8s total, with 8x faster per-file upload and download than Daytona. Blaxel is a close second at 3.7s and wins on large file I/O (0.2s for 1MB vs E2B's 0.6s). Modal is the slowest at 30.8s, with extremely high per-file overhead (1.22s upload, 0.76s download) and 9.8s for 1MB round-trip -- the `sandbox.open()` API has significant per-call latency. Daytona's filesystem is functional but significantly slower than E2B/Blaxel. For file-heavy agent workflows, E2B or Blaxel should be preferred.

---

## 3. Async Task + Pause/Resume Benchmark

**What it tests**: Starting a background Python task that writes progress to disk, writing checkpoint files (JSON, YAML, 10KB binary) while the task runs, pausing the sandbox mid-execution, resuming it, verifying all filesystem state survived the pause/resume cycle (including binary data integrity), restarting a compute task after resume, and downloading all results.

**Why it matters**: Agents often need to suspend work and resume later -- for cost savings, context switching between tasks, or handling interruptions. Pause/resume preserves the full sandbox state without re-creating from scratch. This benchmark measures whether you can trust the sandbox to come back exactly as you left it.

| Step | Daytona | E2B | Blaxel | Modal |
|------|---------|-----|--------|-------|
| Start Background Task | 2.6s | 12.3s* | 2.3s | 3.1s |
| Write State Files | 1.1s | 0.4s | 0.1s | 1.8s |
| Pause Sandbox | 11.5s | 0.9s | N/A** | 1.5s |
| Resume Sandbox | 0.7s | 0.2s | N/A** | 0.1s |
| Verify State After Resume | 2.5s | 0.5s | 0.4s | 7.1s |
| Restart Compute | 0.9s | 0.3s | 0.2s | 2.6s |
| Download & Verify | 2.5s | 0.3s | 0.3s | 4.6s |
| **Total** | **23.3s** | **15.4s** | **4.4s** | **22.7s** |

\* E2B bg task completed before pause due to timing
\** Blaxel SDK does not expose pause/resume APIs

| Metric | Daytona | E2B | Modal |
|--------|---------|-----|-------|
| Pause mechanism | `sandbox.stop()` | `sandbox.pause()` (native) | Snapshot-based |
| Resume mechanism | `sandbox.start()` | `Sandbox.connect(id)` | New sandbox from snapshot |
| Pause latency | 11-14s | 0.8-0.9s | 1.5s |
| Resume latency | 0.6-0.9s | 0.2s | 0.1s |
| Filesystem preserved | Yes | Yes | Yes |
| Binary integrity | Yes | Partial* | Yes |

\* E2B `files.read()` returned different byte representation for binary files after resume

**Summary**: E2B has native pause/resume with sub-second latency (0.9s pause, 0.2s resume), making it ideal for suspend/resume workflows. Modal supports pause via snapshots with 1.5s pause and 0.1s resume -- the fastest resume of all providers -- but its slow file I/O (7.1s state verify, 4.6s download) brings the total to 22.7s. Daytona uses a stop/start mechanism that takes 11-14s to pause but reliably preserves all state including binary data. Blaxel has no pause/resume API -- its fast "total" time reflects skipping those steps.

---

## 4. Concurrent Exec Benchmark

**What it tests**: Whether a sandbox can handle multiple simultaneous `exec()` calls -- the pattern agents use when firing lint, test, typecheck, and format commands in parallel rather than sequentially. Uploads a Python project with source files, installs pytest/flake8/mypy/black, then runs the same 4 commands first sequentially (baseline) then concurrently via threads.

**Why it matters**: A coding agent that can run `flake8`, `pytest`, `mypy`, and `black` at the same time instead of one-by-one gets feedback 3-5x faster per iteration. This directly translates to faster agent loops. If a sandbox serializes exec() calls, the agent gains nothing from concurrent design.

| Provider | Sequential (4 cmds) | Concurrent (4 cmds) | Speedup | Setup | Total |
|----------|--------------------|--------------------|---------|-------|-------|
| **Daytona** | 2.03s | 0.58s | **3.48x** | 4.8s | 10.8s |
| **E2B** | 2.23s | 0.66s | **3.40x** | 3.9s | 8.1s |
| **Blaxel** | 2.23s | 0.87s | **2.56x** | 5.4s | 9.7s |
| **Modal** | 3.38s | 0.67s | **5.06x** | 9.1s | 15.6s |

| Command | Daytona | E2B | Blaxel | Modal |
|---------|---------|-----|--------|-------|
| flake8 | 0.29s | 0.22s | 0.30s | 0.45s |
| pytest | 0.33s | 0.52s | 0.34s | 0.76s |
| mypy | 1.03s | 1.20s | 1.29s | 1.57s |
| black | 0.37s | 0.29s | 0.46s | 0.60s |

**Summary**: All 4 providers support true parallel exec -- none serialize calls. Modal shows the highest speedup (5.06x) because its sequential commands are the slowest, so concurrency provides the biggest relative gain. Daytona achieves the fastest absolute concurrent time (0.58s) and highest speedup among providers with fast sequential baselines (3.48x). E2B wins on total pipeline time (8.1s) due to faster sandbox creation and pip install. For agents designed to fire multiple commands in parallel, all providers deliver genuine speedups.

---

## 5. Iteration Loop Benchmark

**What it tests**: The core coding agent cycle -- upload deliberately broken code (a Calculator class with bugs), run tests to detect failures, upload a fixed version, run tests to confirm the fix, add a new feature (power method + new tests), and run final validation. Measures the round-trip latency for each step of the write-test-fix loop.

**Why it matters**: This is the single most important benchmark for coding agents. Every agent iteration involves writing code, running tests, reading output, and writing a fix. The total loop time directly determines how many iterations an agent can complete in a given time budget. A 2x faster loop means 2x more attempts at solving a problem.

| Step | Daytona | E2B | Blaxel | Modal |
|------|---------|-----|--------|-------|
| Upload broken code (+ pip install) | 1.92s | 1.16s | 2.93s | 5.23s |
| Run tests (detect 3 failures) | 0.29s | 0.53s | 0.39s | 1.43s |
| Upload fix (overwrite 1 file) | 0.27s | 0.05s | 0.04s | 0.56s |
| Run tests (all 6 pass) | 0.25s | 0.45s | 0.29s | 0.82s |
| Add feature (update 2 files) | 0.51s | 0.09s | 0.14s | 1.19s |
| Final validation (9/9 pass) | 0.25s | 0.44s | 0.30s | 0.92s |
| **Total (excl. create/destroy)** | **3.49s** | **2.72s** | **4.09s** | **10.15s** |
| **Total (incl. create/destroy)** | **6.30s** | **3.82s** | **5.12s** | **12.63s** |

| Operation | Daytona | E2B | Blaxel | Modal |
|-----------|---------|-----|--------|-------|
| File overwrite | 0.27s | 0.05s | 0.04s | 0.56s |
| Test exec (avg) | 0.26s | 0.47s | 0.33s | 1.06s |
| Multi-file update | 0.51s | 0.09s | 0.14s | 1.19s |

**Summary**: E2B delivers the fastest total iteration loop (3.8s including sandbox lifecycle). Its file operations are extremely fast (0.05s per overwrite) which is critical since agents write files on every iteration. Blaxel has the fastest single file overwrite (0.04s) and fast test execution (0.29-0.39s) but slower initial uploads due to async bridging overhead. Daytona has the fastest test execution (0.25s per run) but slower file writes. Modal is 3.3x slower than E2B across the board, with high latency on every operation. For coding agents optimizing for iteration speed, E2B is the clear winner.

---

## 6. Multi-Sandbox Fan-Out Benchmark

**What it tests**: Spinning up 3 sandboxes simultaneously, uploading a compute script to all of them concurrently, running a different compute task on each (factorial, fibonacci, prime sieve), collecting results from all sandboxes, and destroying them all. Every step uses ThreadPoolExecutor for maximum parallelism.

**Why it matters**: Advanced agents fan out across multiple sandboxes to try different approaches simultaneously, run tests on different configurations, or split large workloads. The fan-out pattern requires fast sandbox creation, efficient parallel I/O, and reliable concurrent operations. Slow sandbox creation is the biggest bottleneck since it's multiplied by N.

| Step | Daytona | E2B | Blaxel | Modal |
|------|---------|-----|--------|-------|
| Create 3 sandboxes | 3.51s | 1.05s | 1.47s | 0.81s |
| Upload code to all | 0.77s | 0.27s | 0.88s | 2.34s |
| Run 3 different tasks | 0.17s | 0.08s | 0.43s | 0.64s |
| Collect results | 0.36s | 0.06s | 0.13s | 0.71s |
| Destroy all | 0.29s | 0.16s | 0.46s | 0.11s |
| **Total** | **5.10s** | **1.62s** | **3.37s** | **4.61s** |

| Metric | Daytona | E2B | Blaxel | Modal |
|--------|---------|-----|--------|-------|
| Avg creation per sandbox | 1.17s | 0.35s | 0.49s | 0.27s |
| Total I/O (upload + collect) | 1.13s | 0.33s | 1.01s | 3.05s |
| Total compute | 0.17s | 0.08s | 0.43s | 0.64s |

**Summary**: E2B wins fan-out decisively at 1.6s total (3.1x faster than Daytona). It has the fastest upload, compute, and result collection. Modal has the fastest individual sandbox creation (0.27s avg) but its slow file I/O (2.34s upload, 0.71s collect) undermines the creation speed advantage. Blaxel is a solid second at 3.4s. Daytona is slowest for fan-out due to sandbox creation overhead (1.17s per sandbox). For agents that need to spin up multiple sandboxes to explore different strategies, E2B is the best choice.

---

## 7. Coding Agent Benchmark

**What it tests**: The real LLM-powered coding agent loop -- creating a sandbox, uploading the Django scheduling project, then running 3 iterations of: ask Gemini to generate/fix code, upload the code, run Django tests (29 tests), compute a multi-objective reward score (correctness, code quality, domain logic), and feed the score back to the LLM.

**Why it matters**: Unlike the synthetic Iteration Loop benchmark (#5) which uses pre-written code and measures pure sandbox speed, this benchmark measures **end-to-end agent performance** including LLM inference latency. It answers: "How fast can a real coding agent iterate on a real codebase using this sandbox?" The results are non-deterministic since the LLM generates different code each run.

**LLM**: Gemini 2.5 Flash Lite (via REST API), 3 iterations, reward threshold 25.0

| Step | E2B | Blaxel | Modal | Daytona |
|------|-----|--------|-------|---------|
| Setup (sandbox + pip install) | 3.8s | 4.6s | 7.8s | 6.5s |
| Upload Project (38 files) | 0.3s | 0.4s | 1.9s | 1.0s |
| Iteration 1 (LLM + test) | 7.1s | 13.0s | 19.3s | 16.0s |
| Iteration 2 (LLM + test) | 30.7s | 33.0s | 28.6s | 30.3s |
| Iteration 3 (LLM + test) | 19.1s | 23.9s | 28.6s | 34.1s |
| **Total** | **61.1s** | **75.1s** | **86.4s** | **88.2s** |

| Metric | E2B | Blaxel | Modal | Daytona |
|--------|-----|--------|-------|---------|
| Best reward | 7.7 | 16.4 | 14.4 | 14.4 |
| Sandbox overhead (setup+upload) | 4.1s | 5.0s | 9.7s | 7.5s |
| Avg iteration time | 19.0s | 23.3s | 25.5s | 26.8s |

**Iteration Loop (synthetic) vs Coding Agent (LLM) comparison:**

| | Iteration Loop | Coding Agent |
|---|---|---|
| **E2B** | 3.8s (6 steps) | 61.1s (3 LLM iterations) |
| **Blaxel** | 5.1s | 75.1s |
| **Modal** | 12.6s | 86.4s |
| **Daytona** | 6.3s | 88.2s |

**Summary**: E2B wins on total agent loop time (61.1s) primarily because of its fast sandbox setup (3.8s) and file upload (0.3s), which saves ~5s per run compared to Modal/Daytona. However, iteration times are dominated by LLM latency (5-32s per call), making the sandbox speed difference less impactful than in the synthetic benchmarks. The best reward scores varied across providers (7.7 to 16.4) due to non-deterministic LLM outputs, not sandbox differences. For real agent workloads, sandbox choice matters most for setup overhead; once running, LLM API latency is the bottleneck.

---

## Overall Rankings

### By Benchmark (fastest total)

| Benchmark | 1st | 2nd | 3rd | 4th |
|-----------|-----|-----|-----|-----|
| RL Compute | Blaxel (135s)* | E2B (301s)* | Daytona (389s) | Modal (564s) |
| Filesystem I/O | E2B (2.8s) | Blaxel (3.7s) | Daytona (15.7s) | Modal (30.8s) |
| Pause/Resume | E2B (15.4s) | Modal (22.7s) | Daytona (23.3s) | Blaxel (4.4s)** |
| Concurrent Exec | E2B (8.1s) | Blaxel (9.7s) | Daytona (10.8s) | Modal (15.6s) |
| Iteration Loop | E2B (3.8s) | Blaxel (5.1s) | Daytona (6.3s) | Modal (12.6s) |
| Fan-Out | E2B (1.6s) | Blaxel (3.4s) | Modal (4.6s) | Daytona (5.1s) |
| Coding Agent | E2B (61s) | Blaxel (75s) | Modal (86s) | Daytona (88s) |

\* Blaxel/E2B had intermittent stability issues on long RL runs
\** Blaxel skipped pause/resume (no API)

### By Use Case

| Use Case | Best Provider | Why |
|----------|--------------|-----|
| **Long RL/ML training** | Daytona | Most reliable for 5+ min compute, no timeout limits with exec_long() |
| **Coding agent iteration** | E2B | Fastest edit-test loop (3.8s), 0.05s file overwrites |
| **Parallel tool execution** | Daytona | Fastest concurrent exec (0.58s for 4 cmds), 3.48x speedup |
| **Multi-sandbox fan-out** | E2B | 3 sandboxes in 1.6s total, fastest I/O across sandboxes |
| **Pause/resume workflows** | E2B | Sub-second native pause (0.9s), instant resume (0.2s) |
| **Filesystem-heavy agents** | E2B | 8x faster per-file I/O than Daytona |
| **Large file processing** | Blaxel | 0.2s for 1MB round-trip (fastest of all) |
| **Short compute bursts** | Blaxel | Fastest test execution (0.25-0.39s per run) |
| **Custom environments** | Daytona | Full Docker image control, configurable CPU/RAM/disk |
| **Fastest sandbox creation** | Modal | 0.27s avg per sandbox (fastest cold start) |
| **LLM coding agent loop** | E2B | 61s total, fastest setup (4.1s) and upload (0.3s) |

### Head-to-Head Winners (per operation)

| Operation | Winner | Time | Runner-up |
|-----------|--------|------|-----------|
| Sandbox creation | Modal | 0.27s | E2B (0.35s) |
| File upload (per file) | E2B/Blaxel | 0.05s | Daytona (0.27s) |
| File download (per file) | E2B | 0.06s | Blaxel (0.08s) |
| Test execution | Daytona | 0.25s | Blaxel (0.29s) |
| Concurrent exec (4 cmds) | Daytona | 0.58s | E2B (0.66s) |
| Pause latency | E2B | 0.9s | Daytona (11.5s) |
| Resume latency | E2B | 0.2s | Daytona (0.7s) |
| Large file I/O (1MB) | Blaxel | 0.2s | E2B (0.6s) |
| 3-sandbox fan-out | E2B | 1.6s | Blaxel (3.4s) |
| Sandbox destroy | Modal | 0.11s | E2B (0.16s) |

---

## Platform Capabilities Matrix

| Capability | Daytona | E2B | Blaxel | Modal |
|-----------|---------|-----|--------|-------|
| Sandbox creation | 0.6-1.7s | 0.1-0.3s | 0.3-0.5s | 0.3-0.8s |
| Custom CPU/Memory | Yes | No (fixed) | Yes | Yes |
| Custom Docker images | Yes | Template-based | Yes | Yes |
| Native pause/resume | No (stop/start) | Yes (sub-second) | No | Via snapshots |
| Exec timeout limit | 60s hard limit* | None | None | None |
| Parallel exec support | Yes (3.48x) | Yes (3.40x) | Yes (2.56x) | Yes (5.06x) |
| Native file upload | Yes | Yes | Yes (async) | Yes |
| Native file download | Yes | Yes | Yes (async) | Yes |
| Directory listing API | Yes | Yes | Yes | Yes |
| Snapshots | No | Yes | No | Yes |

\* Daytona has a server-side 60s timeout on `process.exec()`; requires `nohup` + polling workaround

---

## Gotchas & Workarounds

| Issue | Provider | Workaround |
|-------|----------|------------|
| 60s exec timeout | Daytona | `nohup` + polling via `exec_long()` |
| Home directory is `/root` | Daytona | Discovered empirically; docs imply `/home/daytona` |
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
=== DAYTONA (4 CPU, 8GB RAM, 10GB disk) ===
  RL:         389.1s total (368.3s training, 29/29 tests)
  FS:          15.7s total (3.8s codegen, 4.3s download, 1.7s large IO)
  Pause:       23.3s total (11.5s pause, 0.7s resume, state=OK)
  Concurrent:  10.8s total (2.03s seq, 0.58s conc, 3.48x speedup)
  Iteration:    6.3s total (0.27s overwrite, 0.25s test avg)
  Fan-out:      5.1s total (3.51s create, 0.17s compute, 3 sandboxes)

=== E2B (default instance) ===
  RL:         301.1s total (287s training*, 29/29 tests)
  FS:           2.8s total (1.1s codegen, 0.5s download, 0.6s large IO)
  Pause:       15.4s total (0.9s pause, 0.2s resume, state=OK)
  Concurrent:   8.1s total (2.23s seq, 0.66s conc, 3.40x speedup)
  Iteration:    3.8s total (0.05s overwrite, 0.47s test avg)
  Fan-out:      1.6s total (1.05s create, 0.08s compute, 3 sandboxes)

=== BLAXEL (4 vCPU, 8GB RAM) ===
  RL:         135.2s total (120s training*, 29/29 tests)
  FS:           3.7s total (1.2s codegen, 0.7s download, 0.2s large IO)
  Pause:        4.4s total (no pause API, state=OK without pause)
  Concurrent:   9.7s total (2.23s seq, 0.87s conc, 2.56x speedup)
  Iteration:    5.1s total (0.04s overwrite, 0.33s test avg)
  Fan-out:      3.4s total (1.47s create, 0.43s compute, 3 sandboxes)

=== MODAL (4 CPU, 8GB) ===
  RL:         564.0s total (542.2s training, 29/29 tests, best reward 14.54)
  FS:          30.8s total (3.9s codegen, 6.8s download, 9.8s large IO)
  Pause:       22.7s total (1.5s pause, 0.1s resume, state=OK)
  Concurrent:  15.6s total (3.38s seq, 0.67s conc, 5.06x speedup)
  Iteration:   12.6s total (0.56s overwrite, 1.06s test avg)
  Fan-out:      4.6s total (0.81s create, 0.64s compute, 3 sandboxes)
  Agent:       86.4s total (3 iters, best reward 14.4, llm=gemini-2.5-flash-lite)

=== CODING AGENT (all providers, Gemini 2.5 Flash Lite, 3 iterations) ===
  E2B:         61.1s total (setup=4.1s, best_reward=7.7)
  Blaxel:      75.1s total (setup=5.0s, best_reward=16.4)
  Modal:       86.4s total (setup=9.7s, best_reward=14.4)
  Daytona:     88.2s total (setup=7.5s, best_reward=14.4)

* = had intermittent errors on some runs
```
