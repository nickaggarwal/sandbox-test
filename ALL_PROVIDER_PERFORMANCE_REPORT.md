# All-Provider Performance Report (Bootup, Compute, Filesystem, RL, Pause/Resume)

Date: 2026-03-07

## Run Inputs

- Main all-provider run: `venv/bin/python -u run_parallel_profiled.py --benchmark all --provider all --sandboxes 4 --episodes 10 --max-steps 10 --max-workers 6 --stagger 0.5`
- Blaxel isolated correction run: `venv/bin/python -u run_parallel_profiled.py --benchmark all --provider blaxel --sandboxes 1 --episodes 10 --max-steps 10 --max-workers 1 --stagger 0`
- E2B scale run (5 containers): `venv/bin/python -u run_parallel_profiled.py --benchmark pause --provider e2b --sandboxes 5 --max-workers 5 --stagger 0.5`

## Resource Normalization

- Daytona: 4 CPU / 8 GB RAM / 10 GB disk configured in runner.
- Blaxel: 4 vCPU / 8 GB RAM configured in runner.
- Modal: 4 CPU / 8 GB RAM configured in runner.
- E2B: default instance shape from provider; this SDK path does not expose CPU/RAM controls.

## Bootup (Create Sandbox)

| Provider | Bootup Time | Runtime Spec |
|---|---:|---|
| daytona | 0.8s | 4 CPU, 8 GB RAM, 10 GB disk (configured) |
| e2b | 0.2s | Provider default (SDK does not expose CPU/RAM sizing here) |
| blaxel | 0.4s | 4 vCPU, 8 GB RAM (configured) |
| modal | 0.4s | 4 CPU, 8 GB RAM (configured) |

## Compute + RL

| Provider | Compute Total (deps+tests+RL) | Install Deps | Run Tests | RL Training | RL Iteration |
|---|---:|---:|---:|---:|---:|
| daytona | 87.9s | 5.3s | 10.6s | 72.1s | 7.21s/episode |
| e2b | 92.9s | 4.7s | 7.2s | 81.0s | 8.11s/episode |
| blaxel | 90.8s | 6.7s | 6.7s | 77.4s | 7.74s/episode |
| modal | 124.1s | 7.3s | 9.8s | 107.0s | 10.70s/episode |

## Filesystem

| Provider | FS Total | Codegen | Upload | Download | Pip Package IO | Pip IO Integrity |
|---|---:|---:|---:|---:|---:|---|
| daytona | 13.3s | 1.4s | 1.6s | 2.7s | 5.5s | OK |
| e2b | 5.3s | 0.9s | 0.3s | 0.5s | 3.0s | OK |
| blaxel | 8.5s | 1.3s | 0.6s | 0.9s | 4.6s | FAIL |
| modal | 38.0s | 7.8s | 4.0s | 7.4s | 13.0s | OK |

## Pause/Resume + State Persistence

| Provider | Pause Pipeline Total | Pause | Resume | Verify State |
|---|---:|---|---|---|
| daytona | 37.4s | 21.5s (OK) | 1.4s (OK) | 5/5 files, json=OK, bin=OK |
| e2b | 15.0s | 0.8s (OK) | 0.2s (OK) | 5/5 files, json=OK, bin=OK |
| blaxel | 4.6s | 0.0s (UNSUPPORTED/FAIL) | 0.0s (UNSUPPORTED/FAIL) | 5/5 files, json=OK, bin=FAIL |
| modal | 31.9s | 1.7s (OK) | 0.1s (OK) | 5/5 files, json=OK, bin=OK |

## Scale Check (E2B, 5 Parallel Containers)

- Wall-clock: **16.8s**
- Parallel speedup: **4.44x**
- Avg pause latency: **0.99s**
- Avg resume latency: **0.19s**
- State-verify success: **5/5**

## Notes

- Blaxel pause/resume remains unsupported in current SDK surface (`pause()` / `resume()` unavailable).
- Blaxel pip wheel roundtrip integrity check currently fails in this benchmark path; raw upload/download steps still succeed.
- Modal pause/resume now uses snapshot-filesystem + recreate from snapshot image.