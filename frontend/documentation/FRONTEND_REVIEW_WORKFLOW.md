# Frontend Review Workflow

This file defines the required frontend delivery loop for the ContextHelix control console.

## Why This Exists

The console is not a generic dashboard.
It has to satisfy four things at the same time:

- stay faithful to runtime semantics
- feel operator-grade and visually intentional
- survive bad transport or degraded live data
- remain reviewable for demos, conference prep, and interviews

Code-only iteration is not enough.

## Mandatory Pipeline

Every frontend task should follow this sequence:

1. `state-first`
   - update `frontend/stories/control-loop-ui-states.json`
   - update fixtures if compare-mode or degraded states are involved
   - confirm the change still maps to real runtime fields

2. `implement`
   - change the smallest useful UI surface
   - keep motion tied to state changes, not decorative drift

3. `build`
   - run `npm run build`

4. `browser-check`
   - use Playwright MCP first
   - if Playwright MCP fails because of `root` / Chromium sandboxing, run an explicit fallback browser check
   - do not skip browser review

5. `screenshot`
   - save at least one screenshot artifact
   - record whether the screenshot came from Playwright MCP or a fallback path

6. `rubric`
   - write a short review covering:
     - result-first readability
     - process-to-result clarity
     - motion semantics
     - evidence usefulness
     - interaction hierarchy

7. `transport-aware`
   - if the page is visually broken, verify whether the failure is:
     - design
     - data
     - proxy / static asset / SSE transport

## Golden States

The following states should be kept stable as review references:

- healthy live suggestion with measured telemetry
- degraded live snapshot missing `timeline` / `stageTelemetry`
- transport fallback where the app shell survives missing external CSS
- cluster-near-trigger
- cluster-triggered
- compare-mode baseline vs augmented
- manual remediation boundary
- bilingual UI shell

## MCP Usage Rule

Playwright MCP is the preferred review tool when it can start successfully.

Use it for:

- render verification
- interaction replay
- screenshot capture
- quick class-of-failure diagnosis

When Playwright MCP cannot start in the current environment:

- say so explicitly
- use a fallback browser run
- keep the screenshot and critique requirement

## Review Output Template

Each frontend turn should end with:

1. what changed
2. what the browser actually showed
3. one screenshot path
4. one short critique
5. whether MCP or fallback browser review was used
