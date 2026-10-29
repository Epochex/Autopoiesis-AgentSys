# Verified skill promotion

Skill induction proposes a name, description and routing metadata. It does **not** generate an implementation of an unknown business operation. Previously, the default induced handler emitted `induced_skill_match` evidence and that nonempty result was sufficient to promote a new capability. An inventory-planning request could therefore be marked resolved without reading inventory or making a plan.

The default router now captures the request and candidate but returns `unresolved`. Opt-in promotion requires application-owned `SkillImplementation(handler, verify_result)`:

```python
from core.skills.induction import SkillImplementation, promote_skill

implementation = SkillImplementation(
    handler=read_inventory,       # reviewed, read-only executable application code
    verify_result=check_inventory_result,  # independent expected result / source checks
)
promoted = promote_skill(candidate, registry, replay_cases, implementation=implementation)
```

`verify_result(case, result)` must return the boolean `True`; a string score or `1` is insufficient. Failed execution, malformed evidence, wrong skill identity, mutating results, and the former matching-only placeholder are rejected. The candidate must be read-only and unfrozen. Duplicate, incomplete-coverage and golden-routing checks run before candidate execution. Golden replay compares both tag selection and the actual rule planner, including name/description scoring and full chain coverage. Registration adapts the executor's case/state call to the reviewed handler's request argument; subsequent calls execute the handler and read the source again rather than reuse replay output.

For routing integration, pass `implementation_provider(candidate, captured_case)` to `CascadingIntentRouter` or `build_enterprise_intent_router`. It returns a reviewed `SkillImplementation` or `None`. Missing or failed providers leave both routing and registry unchanged; the trace records the review outcome and reason. The default routed evaluation now reports the unsupported inventory case as unresolved rather than counting it as a new capability.

This callback is a trusted application boundary, not an untrusted code sandbox. The application owns the provider's read-only behavior and independent expected outcomes. A Python callable cannot be proven read-only by metadata, nor can a user-supplied verifier prove itself correct. Write-capable candidates require a separate reviewed execution/approval path and are not executed by this replay gate. Golden checks here protect **routing**; they are not a substitute for the domain's full workflow regression suite.

Run the file-backed integration and routing regressions:

```sh
python -m pytest tests_py/test_verified_skill_promotion.py tests_py/test_skill_induction.py tests_py/test_intent_router.py -q
python scripts/test_backend.py release -q
```

The file-backed test checks a reading against an independent expected value, changes the source after promotion, and confirms that the registered handler reads the new value. These are controlled software fixtures, not a physical printer or production diagnostic-quality experiment.

## Failure reporting and reproducible gates

Allowlisted diagnostics whose subprocess cannot start now return an explicit refusal with the exception type, including permission or invalid-executable errors. The allowlist is unchanged and no shell fallback is used. A real non-executable file regression exercises the OS failure rather than mocking subprocess behavior.

The full Linux baseline exposed unrelated fixture problems: R450-specific interface names also existed on WSL, old dossier timestamps decayed, a startup wiring test wrote to the production default database directory, and stream lifecycle tests attempted external ClickHouse/SSH setup. The tests now explicitly require the captured host dataset for the four host-specific assertions, freeze the dossier test clock, use a temporary SQLite path, and isolate alert setup in the two stream-lifecycle tests. Production gates and thresholds are unchanged; host checks still run or fail on a host carrying the real manifest. These fixture changes do not claim live deployment or domain-quality validation.
