# Architecture rules

These apply to all code. Full rationale: `docs/TECHNICAL_SPEC.md` section 2.

- Layers: `app/api` → `app/services` (and `app/onboarding`, `app/tasks`) → `app/domain` and the public interfaces of `app/modules/*`.
- `app/domain` is pure Python: no I/O, no framework imports.
- `app/modules/*` never import services, routes, onboarding or tasks.
- Vendor SDKs (`claude_agent_sdk`, `keyring`, `httpx` for fetching, rendering libraries) are imported only inside the provider package that wraps them.
- Routes call services; they don't run SQL, call providers or hold business rules.
- Providers are chosen only in `app/wiring.py` and injected into services.
- `uv run lint-imports` enforces this; never weaken a contract to make it pass. Propose an ADR instead.
