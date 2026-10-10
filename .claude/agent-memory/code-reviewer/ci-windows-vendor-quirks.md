---
name: ci-windows-vendor-quirks
description: Review notes on CI workflows, Windows-only pitfalls, uvicorn start-up/signals, keyring and dependency wheels; read when a change touches CI, the CLI start-up, secrets storage or dependencies
metadata:
  type: project
---

Moved from MEMORY.md on 2026-10-10 to keep the index short. Windows work is suspended (ADR-061), so the Windows items matter only if it resumes.

- gitleaks pre-commit hook entry is `gitleaks git --pre-commit --staged`; `pre-commit run --all-files` does not widen it (no-op on a clean checkout). CI/history scans need a separate `gitleaks git` (fetch-depth 0) or a manual-stage hook with overridden entry.
- Vendored files checked by byte hash (axe, htmx) need `.gitattributes` `-text` (or LF-normalised hashing): Windows CI checkout with autocrlf rewrites LF to CRLF and breaks the hash test.
- CI workflows: `concurrency` keyed only on workflow+ref with `cancel-in-progress: true` lets a push cancel dispatch/schedule evidence runs on the same ref; workflow_dispatch and schedule only work once the file is on the default branch (main), so cross-OS evidence before merge needs a PR to main.
- uvicorn 0.54 `Server.run` raises SystemExit(3) on lifespan or bind failure (never returns with started=False); lifespan tracebacks arrive as message text (starlette format_exc), not exc_info. Fakes of `serve` must model this; prefer handing uvicorn a pre-bound socket.
- `sys.excepthook` installed and restored in a `finally` around the protected code never fires for main-thread errors (restored before the interpreter calls it); catch explicitly instead.
- uvicorn re-raises captured signals after shutdown (`signal.raise_signal`): SIGINT -> KeyboardInterrupt, but Windows SIGBREAK has no Python handler, so the CRT default ends the process with exit 3 unless the app installs one.
- New deps: check uv.lock wheel tags cover all three CI OSes (claude-agent-sdk 0.2.163 has no Windows wheel, sdist has no bundled CLI); env passed to vendor subprocesses that merge over os.environ must blank every credential-routing variable, not just the key.
- keyring 25: macOS `delete_password` wraps every error (denied, auth) in PasswordDeleteError, chainer delete returns None silently; `get_keyring()` raises on a bad PYTHON_KEYRING_BACKEND. Guard tests that name an autouse fixture don't prove it is autouse.
- Hypothesis path strategies: on Windows `base.joinpath("a:b")` / `"D:"` yields a drive-relative (non-absolute) path; random text segments containing `X:` make "absolute path" round-trip tests flaky on Windows CI only. Filter `:` or build with PurePosixPath.
