# M1 live check

Real-provider checks for M1 that CI can't run: AC-M1.9 and the open questions from ADR-045 ("M1.25 live check") and ADR-059. You run them on macOS or Linux with your own credentials. Windows is not supported (ADR-061).

Never paste a credential into a command line, a file in this repository or an issue. The steps below read it into a shell variable without echoing it, so it stays out of your shell history.

Record each result as a short line under M1.25 in `docs/PROGRESS.md` (or in a new ADR when a result changes a decision). Tick the box here only in your local copy.

## 1. Live tests (AC-M1.9)

The live tests build the app's real wiring and Claude provider, save the credential under the app's own keychain item in an in-memory keychain (never your system keychain), call "Test connection" once per mode with the Fast model, and scan the files the Claude CLI wrote plus logs and output for the credential. They never read your own `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY` or keychain.

```bash
read -rs JOBPIPELINE_LIVE_CLAUDE_OAUTH_TOKEN   # paste the token from `claude setup-token`, then Enter
read -rs JOBPIPELINE_LIVE_ANTHROPIC_API_KEY    # optional: paste an API key, then Enter
export JOBPIPELINE_LIVE_CLAUDE_OAUTH_TOKEN JOBPIPELINE_LIVE_ANTHROPIC_API_KEY
JOBPIPELINE_LIVE_TESTS=1 uv run pytest -m live tests/live -s
unset JOBPIPELINE_LIVE_CLAUDE_OAUTH_TOKEN JOBPIPELINE_LIVE_ANTHROPIC_API_KEY
```

Both gates are needed: `JOBPIPELINE_LIVE_TESTS=1` and `-m live`. Add `-k subscription_token` or `-k api_key` to run one mode. Without its variable the API-key test skips (ADR-044); the token test fails and names its variable.

Don't add `-l`/`--showlocals`, `-vv` or `--junitxml` to a live run: they can print or store local values from a failing call.

- [ ] `test_ac_m1_9_test_connection_with_subscription_token` passes.
- [ ] `test_ac_m1_9_test_connection_with_api_key` passes (or skips because you have no API key; say so).
- [ ] Keychain item per mode: the tests assert that token mode reads only service `jobpipeline`, item `claude_code_oauth_token`, and API-key mode only `anthropic_api_key`, and that nothing is written. Record that both assertions passed.
- [ ] First start with an empty config folder: the tests assert the per-call `CLAUDE_CONFIG_DIR` and working folder are empty when the CLI starts and removed afterwards. Record that they passed.
- [ ] Files the CLI writes per mode: `-s` prints the names (never contents) of the files the CLI wrote in its private folders. Record the list per mode.

If the network is down, "Test connection" waits up to 60 seconds before it reports no connection. That is the configured timeout, not a hang.

## 2. ADR-045 open questions

These need your observation; record each answer with the date.

- [ ] `MAX_TURNS = 2` is enough: the live connection tests return "Connected" with structured output, without a "max turns" error.
- [ ] `--tools ""` with `permission_mode="dontAsk"` still allows the internal `StructuredOutput` tool (same evidence as above).
- [ ] Blanked `""` values count as unset: only the base URL and `CLAUDE_CODE_USE_*` switches can be observed in live tests (the test harness removes credential variables for every test). With any observable variables set in your shell, the live tests still reach Anthropic directly with the given credential.
- [ ] Token mode when you are also signed in to Claude Code on this machine (a keychain login): the test uses the given token, not your login. One way to tell: run the token test with a deliberately revoked token; it must fail with an auth error rather than pass on your login.
- [ ] Managed (enterprise) settings and their hooks: if your machine has managed Claude Code settings, note whether they loaded during the live test (for example a hook's side effect). If it has none, write "not applicable".
- [ ] The hand-written replay fixtures in `tests/fakes/` match the real stream: no parse warnings or unexpected message types in the live run's log output.

## 3. In the running app

Start with fresh temporary folders so nothing touches your real data:

```bash
DATA="$(mktemp -d)"; CONF="$(mktemp -d)"
JOBPIPELINE_CONFIG_DIR="$CONF" uv run jobpipeline --data-dir "$DATA"
```

- [ ] The setup guide opens at its first step. On the AI provider step, save your token and press "Test connection": the result says "Connected".
- [ ] Your system keychain now holds an item for service `jobpipeline` (macOS: Keychain Access; Linux: `secret-tool search service jobpipeline`). Remove it afterwards from Settings > Providers.
- [ ] No credential in the data folder: read the token without echoing, then search for it:
  ```bash
  read -rs T; [ -n "$T" ] && { grep -rlF --binary-files=text -f <(printf '%s\n' "$T") "$DATA" "$CONF" && echo "FOUND" || echo "not found"; }; unset T
  ```
  Paste the whole token (it won't echo), press Enter, and you'll see "not found" if the check passes.

## 4. Files in ~/.claude* (ADR-045)

The live tests set `CLAUDE_CONFIG_DIR` to an empty temporary folder, so the CLI starts fresh. Files under `~/.claude*` per mode (ADR-045) are not listed by the live test: check them by hand before and after the run:

```bash
ls -la ~/.claude* 2>/dev/null
```

Record any differences you see.

## 5. Data-folder lock (ADR-059, ADR-072)

Deferred by your decision at M1.25 (ADR-072). No check needed now.

## When you're done

Tell Claude the results (or add them to `docs/PROGRESS.md` yourself) so they can be recorded in M1.25's evidence and the milestone acceptance report.
