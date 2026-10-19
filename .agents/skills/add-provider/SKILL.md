---
name: add-provider
description: "Procedure for adding or changing a provider behind the AI, search, calendar or secrets modules: protocol conformance, capability flags, credentials, budgets, contract tests and settings fields. Use whenever work touches app/modules/*/providers/ or app/wiring.py."
---

# Add or change a provider

Providers make the app swappable. A new provider must not change any service or route.

1. **Protocol.** Implement the module's protocol from spec 2.4 in `app/modules/<module>/providers/<name>.py`. The vendor SDK is imported only in that file (import-linter enforces it).
2. **Capabilities.** Declare them honestly: AI (`web_search`, `speech_to_text`, `text_to_speech`, `reports_token_usage`), search (`mode`, `storage_allowed`, `reports_usage`), calendar (`supports_sync`). Unknown means `None` or `False`, never a guess.
3. **Credentials.** Read secrets only through `SecretStore`, at call time. Never log them, store them in the database, or return them. Map auth, quota, network and installation failures to the errors in spec 2.11.
4. **Isolation (AI providers).** No tools unless the request asks for web search; an empty temporary working directory; no loading of developer settings (spec 2.9 item 8). Check option names against the installed SDK version.
5. **Budgets.** Report usage in the provider's unit (requests, or tokens when reported). If the provider reports remaining quota (for example in response headers), expose it so the usage service can reconcile. Web search tool calls go through the request's `on_tool_use` hook.
6. **Settings and setup guide.** Add the provider's fields (key, server address, model choices) to the settings schema, and make sure the setup guide's provider step and Settings > Providers render them. "Test connection" must work.
7. **Wiring.** Register the provider in `app/wiring.py`.
8. **Tests.**
   - Run the module's shared contract suite in `tests/contract/` against the new provider using a recorded or fake transport.
   - Unit tests for error mapping and capability flags.
   - A live test in `tests/live/` (skipped unless `JOBPIPELINE_LIVE_TESTS=1`) with the smallest possible call.
9. **Review.** `security-reviewer` reviews every provider change.
10. **Decision.** Record the provider and any terms-of-use findings (for example limits on storing results) in `docs/DECISIONS.md`.
