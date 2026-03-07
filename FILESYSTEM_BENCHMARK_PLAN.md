# Filesystem Benchmark Plan for Sandbox Profiling

## Context
Phases 1-5 (upload fix, E2B integration, parallel profiling, RL fix, comparison runs) are **complete**. This plan adds filesystem-intensive benchmark use cases where agents generate code, save build artifacts, and load them back — testing the native FS APIs of both Daytona and E2B side-by-side.

## Phase 6: Filesystem Benchmark

### 6a. Add native FS wrapper methods to both runners

**`daytona_sandbox.py`** — Add 3 methods after `download_file()` (~line 239):
- `upload_file_native(content: bytes, remote_path: str)` → `self.sandbox.fs.upload_file(content, remote_path)`
- `download_file_native(remote_path: str) -> bytes` → `self.sandbox.fs.download_file(remote_path)`
- `list_files_native(remote_path: str) -> list` → `self.sandbox.fs.list_files(remote_path)`

**`e2b_sandbox.py`** — Add 3 matching methods after `download_file()` (~line 157):
- `upload_file_native(content: bytes, remote_path: str)` → `self.sandbox.files.write(remote_path, content)`
- `download_file_native(remote_path: str) -> bytes` → `self.sandbox.files.read(remote_path, format='bytes')`
- `list_files_native(remote_path: str) -> list` → `self.sandbox.files.list(remote_path)`

### 6b. Create `filesystem_benchmark.py` (NEW)

Main function: `run_filesystem_benchmark(runner, provider) -> list[StepProfile]`

**6 benchmark steps:**

| Step | Name | What it does |
|------|------|-------------|
| 1 | `fs_code_generation` | Use `exec()` to generate 10 Python source files (small/medium/large) in sandbox — simulates agent writing code |
| 2 | `fs_build_compile` | Run `python3 -m compileall -b` to compile .py → .pyc build artifacts |
| 3 | `fs_upload_files` | Upload 5 test files individually via native FS API (config.json, .py, .csv, .md) — benchmarks per-file upload |
| 4 | `fs_download_files` | Download .pyc build artifacts + uploaded files back via native FS API — benchmarks per-file download |
| 5 | `fs_large_file_io` | Upload ~1MB JSON, process it in sandbox (python3 filter/transform), download result — tests large file round-trip |
| 6 | `fs_list_verify` | List all files recursively via exec + native FS list API, verify counts and sizes |

Helper functions:
- `generate_python_source(name, size_hint)` — creates valid Python classes/functions (~500B/~5KB/~20KB)
- `generate_large_json(size_bytes)` — creates ~1MB JSON with records array

Path handling: `base_dir = '/root/fs_bench'` (Daytona) or `'/home/user/fs_bench'` (E2B)

### 6c. Integrate into `run_parallel_profiled.py`

- Add `--benchmark` CLI arg: choices `rl` (default, existing), `fs` (filesystem only), `all` (both)
- Add `run_profiled_fs_benchmark()` pipeline function (create sandbox → run 6 FS steps → destroy)
- Update `run_parallel()` to dispatch to correct pipeline based on `cfg['benchmark']`
- Make `_print_report()` step_names dynamic (collect from actual profiles, use known ordering)
- Update CLI config generation for `--benchmark fs` and `--benchmark all`

## Files to Modify
- `daytona_sandbox.py` — add `upload_file_native`, `download_file_native`, `list_files_native`
- `e2b_sandbox.py` — add matching 3 methods
- `filesystem_benchmark.py` — **NEW** benchmark module with 6 steps + data generators
- `run_parallel_profiled.py` — add `--benchmark` flag, FS pipeline, dynamic step names

## Verification
```bash
# FS benchmark, Daytona vs E2B head-to-head
python run_parallel_profiled.py --benchmark fs --provider both --sandboxes 2

# FS benchmark, single provider
python run_parallel_profiled.py --benchmark fs --provider daytona --sandboxes 2

# Both RL + FS benchmarks combined
python run_parallel_profiled.py --benchmark all --provider both --sandboxes 4

# Existing RL benchmark unchanged (default)
python run_parallel_profiled.py --provider both --sandboxes 4 --vary-config
```

Expected output: Per-step timing comparison showing native FS API performance (upload/download speed), code generation throughput, build artifact creation, and large file round-trip times for Daytona vs E2B.

## Implementation Details

### Data Generators

`generate_python_source(name, size_hint)`:
- `small` (~500B): single class with 2 methods
- `medium` (~5KB): multiple classes with methods, imports
- `large` (~20KB): many classes with docstrings, validation logic

`generate_large_json(size_bytes=1_000_000)`:
- Creates JSON with `records` array, each record ~200 bytes
- ~5000 records for 1MB target

### Key API Differences

| Operation | Daytona | E2B |
|-----------|---------|-----|
| Upload file | `sandbox.fs.upload_file(bytes, path)` | `sandbox.files.write(path, bytes)` |
| Download file | `sandbox.fs.download_file(path) → bytes` | `sandbox.files.read(path, format='bytes') → bytearray` |
| List files | `sandbox.fs.list_files(path) → [FileInfo]` | `sandbox.files.list(path) → [EntryInfo]` |

### Gotchas
- Base64 encoding for exec()-based writes: limit large files to ~20KB to avoid command line length issues
- Daytona 60s exec timeout: none of the FS benchmark steps should approach this
- E2B `files.read` defaults to text format — must pass `format='bytes'` for binary .pyc files
- Path differences: Daytona `/root/fs_bench`, E2B `/home/user/fs_bench`
