# Daytona vs E2B Sandbox Comparison

## Test Setup
- **Workload**: Django scheduling app (Calendly-like) with 29 unit tests + RL agent training (REINFORCE policy gradient with Gymnasium)
- **Configurations**: 10 episodes/10 steps (fast) and 30 episodes/15 steps (long)
- **Runs**: 5 head-to-head sessions, 4 sandboxes each (2 Daytona + 2 E2B), all parallel
- **Daytona SDK**: v0.148 (official Python SDK, `Image.debian_slim('3.12')`)
- **E2B SDK**: `e2b-code-interpreter` with `Sandbox.create()`

## Why Daytona Wins on Compute

| Metric | Daytona (avg) | E2B (avg) | Winner |
|--------|--------------|-----------|--------|
| **RL Training (10ep)** | **72-80s** | 80-83s | Daytona ~10% faster |
| **RL Training (30ep)** | **224-252s** | 259-267s | Daytona ~8% faster |
| **Pip Install** | **4.7-5.5s** | 4.8-5.4s | Tie |
| **Django Tests** | **6.0-7.0s** | 7.1-7.8s | Daytona ~10% faster |
| **Total (avg)** | **180s** | 188s | Daytona ~4% faster |

### Daytona Advantages

1. **Faster compute execution**: Daytona sandboxes consistently run RL training 8-12% faster than E2B. For a 30-episode training run, that's ~20s saved per sandbox. At scale (hundreds of sandboxes), this compounds significantly.

2. **Configurable resources**: Daytona's `Image.debian_slim()` + `Resources(cpu=2, memory=4, disk=10)` gives explicit control over sandbox specs. E2B uses a fixed default template.

3. **Native filesystem API**: `sandbox.fs.upload_file()` and `sandbox.fs.download_file()` work directly with bytes, no encoding needed. Clean and efficient.

4. **Official Python SDK**: Full-featured SDK with sync/async clients, sessions, git operations, code interpreter, and PTY support. More comprehensive than E2B's SDK.

5. **Session-based execution**: `create_session()` and `execute_session_command()` support long-running interactive processes, which E2B lacks at the SDK level.

6. **Custom Docker images**: Can build sandboxes from any Docker image (`Image.debian_slim`, custom images), giving full control over the runtime environment.

### E2B Advantages

1. **Faster sandbox creation**: 0.1-0.5s vs Daytona's 0.8-1.9s. E2B spins up sandboxes ~3x faster.

2. **Faster file uploads**: 0.6-0.8s vs Daytona's 1.3-1.7s. E2B's file system is slightly snappier for small transfers.

3. **No exec timeout limit**: E2B's `commands.run()` supports arbitrary timeouts. Daytona has a hard ~60s server-side timeout on `process.exec()`, requiring a `nohup` + polling workaround for long commands.

4. **Simpler API**: `Sandbox.create()` → `sandbox.commands.run()` → `sandbox.kill()`. Fewer parameters to configure.

### Gotchas Discovered

| Issue | Daytona | E2B |
|-------|---------|-----|
| **Exec timeout** | Hard 60s server limit; must use background + poll for long commands | No limit; timeout parameter works as expected |
| **Home directory** | `/root` (not `/home/daytona` as docs imply) | `/home/user` |
| **SSL certs** | Needs `certifi` on macOS with Python 3.12 | Works out of the box |
| **API key passing** | Via `DaytonaConfig(api_key=...)` | Via `Sandbox.create(api_key=...)` kwarg |

## Verdict

**Daytona is the better choice for compute-heavy workloads** like RL training, test suites, and CI/CD pipelines where execution speed matters more than sandbox spin-up time. The 8-12% compute advantage is consistent across runs and compounds at scale.

**E2B is better for rapid prototyping and short-lived tasks** where sub-second sandbox creation and simpler API matter more than raw compute performance.

For this project (RL agent training in sandboxes), **Daytona delivers ~4% faster end-to-end** with meaningfully faster compute steps, making it the recommended provider.

## Raw Data (Latest Run)

```
Daytona avg total: 180.2s | E2B avg total: 188.0s
Daytona RL train:  158.6s | E2B RL train:  174.3s
Parallel speedup:  2.49x across 4 sandboxes
All runs: 29/29 tests passed, RL best reward 15.1
```
