# Sandbox Platform Benchmark Report

## Test Setup

- **Workload**: Django scheduling app (Calendly-like) with 29 unit tests + RL agent training (REINFORCE policy gradient, 50 episodes, 25 max steps)
- **Filesystem Benchmark**: 10 Python files generated, compiled to .pyc, 5 files uploaded/downloaded via native FS API, 1MB JSON round-trip
- **Pause/Resume Benchmark**: Background task, state file persistence, sandbox pause, resume, state verification, compute restart
- **Instance Specs**: Daytona (4 CPU, 8GB RAM, 10GB disk), E2B (default), Blaxel (4 vCPU, 8GB RAM)
- **Python**: 3.12 (host), 3.12 (sandbox images)
- **SDKs**: Daytona v0.149, E2B Code Interpreter v2.4.1, Blaxel v0.2.44

---

## 1. RL Compute Benchmark (50 episodes, 25 steps)

| Step | Daytona | E2B | Blaxel |
|------|---------|-----|--------|
| Create Sandbox | 1.1s | 0.2s | 0.4s |
| Upload Project | 1.0s | 0.4s | 0.5s |
| Install Deps | 5.1s | 5.5s | 6.6s |
| Run Tests (29) | 10.5s | 7.7s | 6.7s |
| RL Training | **368.3s** | 287s* | 120s** |
| Total | **389.1s** | 301.1s | 135.2s |

\* E2B RL training had connection timeout at 287s on some runs
\** Blaxel RL training hit timeout/HTML error page on some runs

**Verdict**: Daytona completed RL training most reliably at scale. E2B is faster but hit connection limits on long runs. Blaxel was fastest when it worked but less stable for long-running compute.

---

## 2. Filesystem I/O Benchmark

| Step | Daytona | E2B | Blaxel |
|------|---------|-----|--------|
| Code Generation (10 files) | 3.8s | 1.1s | 1.2s |
| Build/Compile (.pyc) | 0.7s | 0.2s | 0.3s |
| Native File Upload (5 files, 32KB) | 2.3s | 0.3s | 0.3s |
| Native File Download (9 files) | 4.3s | 0.5s | 0.7s |
| Large File I/O (1MB JSON) | 1.7s | 0.6s | 0.2s |
| List & Verify | 0.8s | 0.1s | 0.2s |
| **Total FS** | **15.7s** | **2.8s** | **3.7s** |

**Verdict**: E2B leads filesystem performance (2.8s), Blaxel close second (3.7s), Daytona significantly slower (15.7s). Blaxel wins large file I/O (0.2s).

### Native FS API Speed Comparison

| Operation | Daytona | E2B | Blaxel | E2B vs Daytona |
|-----------|---------|-----|--------|----------------|
| Upload (per file) | ~0.46s | ~0.06s | ~0.06s | **8x faster** |
| Download (per file) | ~0.48s | ~0.06s | ~0.08s | **8x faster** |
| Large file upload (710KB) | ~1.2s | ~0.3s | ~0.1s | **4-12x faster** |

---

## 3. Async Task + Pause/Resume Benchmark

| Step | Daytona | E2B | Blaxel |
|------|---------|-----|--------|
| Start Background Task | 2.6s | 12.3s* | 2.3s |
| Write State Files | 1.1s | 0.4s | 0.1s |
| **Pause Sandbox** | **11.5s** | **0.9s** | N/A** |
| **Resume Sandbox** | **0.7s** | **0.2s** | N/A** |
| Verify State After Resume | 2.5s | 0.5s | 0.4s |
| Restart Compute | 0.9s | 0.3s | 0.2s |
| Download & Verify | 2.5s | 0.3s | 0.3s |
| **Total** | **23.3s** | **15.4s** | **4.4s** |

\* E2B bg task ran to completion (20/20 steps) before pause due to the 2s wait
\** Blaxel SDK does not expose pause/resume APIs

### Pause/Resume Deep Dive

| Metric | Daytona | E2B |
|--------|---------|-----|
| Pause Mechanism | `sandbox.stop()` | `sandbox.pause()` (native) |
| Resume Mechanism | `sandbox.start()` | `Sandbox.connect(id)` (native) |
| Pause Latency | **11.2-14.1s** | **0.8-0.9s** |
| Resume Latency | **0.6-0.9s** | **0.2s** |
| Filesystem Preserved | Yes | Yes |
| Binary Data Integrity | Yes | Partial* |
| JSON Data Integrity | Yes | Yes |
| Compute After Resume | Yes | Yes |

\* E2B `files.read()` returned different byte representation for binary files after resume

**Verdict**: E2B has native pause/resume with sub-second latency. Daytona uses stop/start which takes 11-14s for pause. Blaxel has no pause/resume API. All providers preserve filesystem state.

---

## 4. Platform Capabilities Matrix

| Capability | Daytona | E2B | Blaxel |
|-----------|---------|-----|--------|
| Sandbox Creation | 0.7-1.5s | 0.1-0.3s | 0.3-0.4s |
| Custom CPU/Memory | Yes (4 CPU, 8GB) | No (fixed) | Yes (4 vCPU, 8GB) |
| Custom Docker Images | Yes | Template-based | Yes |
| Native Pause/Resume | No (stop/start) | Yes (sub-second) | No |
| Exec Timeout Limit | 60s hard limit* | None | None |
| Long-Running Tasks | Via nohup+poll | Direct | Direct |
| Native File Upload | Yes | Yes | Yes (async) |
| Native File Download | Yes | Yes | Yes (async) |
| Directory Listing API | Yes | Yes | Yes |
| Snapshots | No | Yes | No |

\* Daytona has a server-side 60s timeout on `process.exec()`; requires background + polling workaround

---

## 5. Overall Rankings

### By Use Case

| Use Case | Best Provider | Why |
|----------|--------------|-----|
| **Long RL Training** | Daytona | Most reliable for 5+ min compute, configurable resources |
| **Filesystem-Heavy Agents** | E2B | 8x faster native file I/O |
| **Pause/Resume Workflows** | E2B | Sub-second native pause (0.9s) vs Daytona's 11s stop |
| **Rapid Prototyping** | E2B | 0.1s sandbox creation, simple API |
| **Short Compute Tasks** | Blaxel | Fastest test execution, low latency |
| **Large File Processing** | Blaxel | 0.2s for 1MB round-trip (fastest) |
| **Custom Environments** | Daytona | Full Docker image control, 4 CPU / 8GB RAM |

### Head-to-Head Winners (per step)

| Step | Winner | Margin |
|------|--------|--------|
| create_sandbox | E2B (0.2s) | 5x vs Daytona |
| upload_project | E2B (0.4s) | 2.5x vs Daytona |
| install_deps | Daytona (5.1s) | 1.1x vs E2B |
| run_tests | Blaxel (6.7s) | 1.2x vs E2B |
| fs_upload (native) | E2B/Blaxel (0.3s) | 8x vs Daytona |
| fs_download (native) | E2B (0.5s) | 9x vs Daytona |
| large_file_io | Blaxel (0.2s) | 3x vs E2B |
| pause | E2B (0.9s) | 13x vs Daytona |
| resume | E2B (0.2s) | 3.5x vs Daytona |
| compute_restart | Blaxel (0.2s) | 1.5x vs E2B |

---

## 6. Gotchas & Workarounds

| Issue | Provider | Workaround |
|-------|----------|------------|
| 60s exec timeout | Daytona | `nohup` + polling via `exec_long()` |
| Home directory is `/root` | Daytona | Discovered empirically; docs imply `/home/daytona` |
| SSL cert errors on macOS | Daytona | `import certifi; os.environ['SSL_CERT_FILE'] = certifi.where()` |
| Python 3.9 type union syntax | Daytona SDK | Use Python 3.12 or `eval_type_backport` |
| `Sandbox()` constructor deprecated | E2B | Use `Sandbox.create(api_key=...)` instead |
| Connection drops on long runs | E2B | RL training >5min may get chunked transfer errors |
| No pause/resume in SDK | Blaxel | Platform may auto-pause but SDK doesn't expose it |
| Event loop shared across threads | Blaxel | Create new `asyncio.new_event_loop()` per instance |
| Disk limit 10GB | Daytona | Cannot exceed; contact support for higher limits |

---

## Raw Benchmark Data

```
=== DAYTONA (4 CPU, 8GB RAM, 10GB disk) ===
  RL:    389.1s total (368.3s training, 29/29 tests)
  FS:     15.7s total (3.8s codegen, 4.3s download, 1.7s large IO)
  Pause:  23.3s total (11.5s pause, 0.7s resume, state=OK)

=== E2B (default instance) ===
  RL:    301.1s total (287s training*, 29/29 tests)
  FS:      2.8s total (1.1s codegen, 0.5s download, 0.6s large IO)
  Pause:  15.4s total (0.9s pause, 0.2s resume, state=OK)

=== BLAXEL (4 vCPU, 8GB RAM) ===
  RL:    135.2s total (120s training*, 29/29 tests)
  FS:      3.7s total (1.2s codegen, 0.7s download, 0.2s large IO)
  Pause:   4.4s total (no pause API, state=OK without pause)

* = had intermittent errors on some runs
```
