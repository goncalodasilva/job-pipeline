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
- The spec and approved ADRs govern both code and tests (spec 4.4, ADR-133). Correct new or existing tests, fixtures and helpers without separate approval when the requirement establishes the expected behavior. Record the requirement, why the old check was wrong, how the correction preserves meaningful coverage, and the checks actually run in task Evidence or the session log.
- Never skip, xfail, weaken or delete a test merely to get green, match a broken implementation or avoid a specified behavior. Preserve coverage of each requirement; replace obsolete checks with meaningful checks where needed. Ask when requirements conflict or the correct expectation is unclear. Product/spec changes still need the user's decision.
- UI scenarios cover a keyboard-only path, a 390x844 viewport and an axe scan.
