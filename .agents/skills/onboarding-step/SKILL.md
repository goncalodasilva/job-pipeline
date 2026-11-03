---
name: onboarding-step
description: "Procedure for adding or changing a step of the setup guide (app/onboarding/): step registry, startup detection, persistence, redirect rules and tests. Use whenever work touches the first-run experience."
---

# Add or change a setup guide step

The setup guide is described in `docs/references/detailed-design.md` ("Today and the setup guide") and in milestones M1 and M2 of the spec. The guide has six steps today: welcome (10), AI provider (20), web search (30, optional, ADR-087), CV import (40, optional, ADR-095), job preferences (50) and done (60). `m1_registry()` builds all of them, although its name predates steps 3 and 4 (ADR-087 item 4).

1. **Define the step** as a `StepDefinition` (`id`, `title`, `required`, `order`, fragment `template`) registered in `app/onboarding/registry.py`, and a `StepHandler` in `app/onboarding/steps/<name>.py` with `context()` and `submit(form) -> StepResult` (forms are `Mapping[str, Sequence[str]]`). Wiring builds the handlers (`require_handlers` checks one per step). Handlers only call services; they never touch providers or the database directly. A handler marks its step done itself through `SetupProgress` when the step is really finished (ADR-052).
2. **Persist on every valid submit** in `setup_state` (`pending`, `done`, `skipped`) so closing the app never loses progress. Optional steps support "Skip for now". Step 3 is the example to copy: `app/onboarding/steps/web_search.py` (its budget fields reuse the usage budget fragment, a stale budget version answers 409 with the reload prompt, and the step announces its outcome before the next step's announcement, ADR-087).
3. **Detection** (`app/onboarding/detection.py`) must keep its four cases: no bootstrap config; config without a database; incomplete required steps; complete setup with a broken provider (banner, not wizard). Adding a required step means existing users with complete setup see a banner that links to it, not the whole wizard.
4. **Secrets** entered in a step go straight to `SecretStore` and are never echoed back.
5. **Done step** lists skipped steps with links (AC-M1.7; step 3 is the first real skipped step). Today will list them too once it shows the skipped-steps list in M5 (ADR-076 item 12); until then Today doesn't list them.
   Step 4 uses the shared `CvImportService`, with actual-byte upload limits and parsing off the event loop. It marks itself done when facts or a stored import exist, including when the provider or budget prevents starting; its outcome tells the user to start the kept upload from the Fact bank. It offers no budget dialog in the guide (ADR-095). A bad upload stays on the step with a field error. Detection of an existing import/fact happens when the workspace opens, never as a GET side effect.
6. **Tests:**
   - table-driven detection tests covering the four cases and the new step;
   - integration tests for submit, persistence, resume after restart and redirects (303 and `HX-Redirect`);
   - the registry test lists every step id and order, and the e2e guide walk and the "Step N of 6" assertions include every step (ADR-087 item 3);
   - e2e: complete the guide with the fake provider at desktop and phone sizes, keyboard only, with axe.
7. **Copy:** plain and warm, explaining why each step matters, in the tone of the detailed design.
8. **Changing the step count** touches the registry, the detection tests, the guide shell's `STEPS` (a test constant in `tests/e2e/test_setup_guide_shell.py`) and every "Step N of M" assertion (ADR-087). Once the step change is specified or approved, update these tests without separate approval, with the justification and verification required by spec 4.4/ADR-133. An unspecified change to the setup flow still needs the user's decision.
