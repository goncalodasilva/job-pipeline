---
paths:
  - "tests/**"
---

# Test rules

- Name acceptance tests `test_ac_m<n>_<k>_<what>` so acceptance can map criteria to tests.
- Use markers: `integration`, `contract`, `e2e`, `smoke`, `live`, `slow` (spec 7.2).
- No network, no real providers, no real credentials, no real personal data. Use `tests/fakes/` and fictional fixtures.
- Deterministic: `FrozenClock`, `SequentialIdFactory`, temp data folders, no sleeps.
- Live tests skip unless `JOBPIPELINE_LIVE_TESTS=1`.
- Never skip, xfail, loosen or delete a test to get green without the user's approval.
- UI scenarios cover a keyboard-only path, a 390x844 viewport and an axe scan.
