---
name: onboarding-step
description: "Procedure for adding or changing a step of the setup guide (app/onboarding/): step registry, startup detection, persistence, redirect rules and tests. Use whenever work touches the first-run experience."
---

# Add or change a setup guide step

The setup guide is described in `docs/references/detailed-design.md` ("Today and the setup guide") and milestone M1 of the spec.

1. **Define the step** as a class in `app/onboarding/steps/<name>.py` with: `id`, `title`, `required`, `order`, `is_complete(state) -> bool`, `render(ctx)` returning the fragment, and `submit(form) -> StepResult`. Register it in `app/onboarding/registry.py`. Steps only call services; they never touch providers or the database directly.
2. **Persist on every valid submit** in `setup_state` (`pending`, `done`, `skipped`) so closing the app never loses progress. Optional steps support "Skip for now".
3. **Detection** (`app/onboarding/detection.py`) must keep its four cases: no bootstrap config; config without a database; incomplete required steps; complete setup with a broken provider (banner, not wizard). Adding a required step means existing users with complete setup see a banner that links to it, not the whole wizard.
4. **Secrets** entered in a step go straight to `SecretStore` and are never echoed back.
5. **Done step** lists skipped steps with links; Today lists them too.
6. **Tests:**
   - table-driven detection tests covering the four cases and the new step;
   - integration tests for submit, persistence, resume after restart and redirects (303 and `HX-Redirect`);
   - e2e: complete the guide with the fake provider at desktop and phone sizes, keyboard only, with axe.
7. **Copy:** plain and warm, explaining why each step matters, in the tone of the detailed design.
