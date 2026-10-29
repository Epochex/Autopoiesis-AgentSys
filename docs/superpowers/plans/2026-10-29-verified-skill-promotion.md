# Verified Skill Promotion Implementation Plan

**Goal:** Never turn a metadata-only induced description into an executable capability.
**Architecture:** Induction proposes metadata. A trusted application integration supplies a read-only implementation and an independent result verifier. Promotion checks coverage and golden routing before replaying the implementation. Unknown requests remain unresolved without that integration.
**Tech stack:** Python 3.11, Pydantic, pytest; no new core runtime dependencies; declare the gateway's existing PyYAML import in its optional extra.

## Constraints and review focus

- Do not import the dirty experimental checkout or publish private fixtures.
- Do not execute write/approval-required candidates during replay.
- A matching description, nonempty evidence list or truthy self-score is not verification.
- Preserve existing golden routing before invoking a candidate.
- Provider failures must leave the request unresolved and the registry unchanged.
- Python/Linux is the existing supported backend; record Windows baseline limitations.

## Tasks

- [x] Add failing tests for metadata-only promotion, a real file-backed read-only implementation, failed/invalid verification, non-read-only replay, and golden hijacking.
- [x] Add `SkillImplementation(handler, verify_result)` and a keyword-only promotion argument; remove fabricated induced-handler execution.
- [x] Wire an optional trusted `implementation_provider` through router/factory; test default unresolved and verified opt-in routing.
- [x] Run the supported full backend suite, document the contract and run fresh branch review.
- [ ] Commit, create a PR, require successful relevant CI, merge to main, and verify remote SHA.

## Baseline

`438656c`: Windows full-suite collection fails on the existing unconditional `fcntl` imports. Linux validation is required, rather than a fake lock shim or skipped collection failures.

## Independent review and regressions

The independent reviewer found two important defects: required-request handlers passed replay but failed execute_chain's case/state signature, and tag-only golden checks missed description-based route hijacking. Both were reproduced with new failing tests, then fixed using an execution adapter and actual-planner comparison. The three skill/router test files pass 34 cases. Trusted callbacks remain application code, not a sandbox; golden routing is not a full domain-quality experiment.

A separate real POSIX non-executable-file regression reproduced PermissionError escaping the diagnostic runtime. The runtime now returns structured OSError refusals without changing its allowlist or invoking a shell. The combined focused governance/runtime and corrected baseline fixture tests pass 179 cases, with 5 explicit host skips.

Original Linux baseline: 9 failed, 1453 passed, 59 skipped. All nine failures were traced before changes: four R450-specific assertions ran on unrelated WSL interfaces/hostname, two live-stream tests attempted unrelated external alert setup, one memory promotion fixture aged against the wall clock, one startup fixture used /data, and the command-launch failure above. No promotion/safety threshold or production service setup was weakened.

## Final local validation

- `python scripts/test_backend.py release -q --tb=short`: **1478 passed, 63 skipped**, one existing Starlette/httpx deprecation warning, Linux CPython 3.11.18. Skips include absent R450 dataset/services, PostgreSQL test DSN and optional model/vector dependencies; they are not claimed as passed integration coverage.
- `python -m domains.network_rca.phase15`: exits successfully with explicitly mock-labeled baselines and `ready=false` for the absent real manifest. No real held-out quality claim.
- `git diff --check`: clean.
- GitHub PR CI and merge verification follow this local validation; remote outcomes are recorded in the PR rather than asserted ahead of execution.
