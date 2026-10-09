# Failure-injection tests

Integration tests that inject failures (provider errors, kills, restarts, full disks) and check
recovery (spec 4.1). Every test here is marked `integration`. Wait on conditions with
`tests/waiting.wait_until`, never with fixed sleeps.
