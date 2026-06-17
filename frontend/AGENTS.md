# AGENTS.md

## Repository purpose

This repository contains a front-end application for analytical product interfaces.
Agents working in this repository must prioritize:
- information hierarchy
- auditability
- structured comparison workflows
- production-grade implementation quality
- maintainable component architecture

This repository does NOT optimize for flashy mockups or generic SaaS dashboard aesthetics.

---

## Working style for Codex

When handling non-trivial UI tasks:

1. Inspect first
- Understand current route structure, page ownership, shared components, chart utilities, theme system, and state shape before editing.

2. Plan before implementation
- For major refactors, first produce:
  - target file map
  - component tree
  - data model changes
  - migration / deletion list

3. Implement in small reversible steps
- Prefer modular changes over monolithic rewrites unless a full replacement is explicitly requested.

4. Verify
- Run the relevant lint / type / test commands when available.
- Report what was verified and what could not be verified.

Do not make large speculative changes without grounding them in the actual codebase.

---

## Design philosophy

The repository follows a dark analytical visual language.

### Core design intent
Interfaces should feel:
- precise
- cold
- diagrammatic
- editorial
- audit-friendly
- system-level
- restrained

### Avoid
Do NOT introduce:
- colorful BI-style dashboards
- overly soft consumer card UI
- glassmorphism
- neumorphism
- toy-like rounded design systems
- decorative 3D effects
- rainbow chart palettes
- poster-style layouts that reduce usability

### Prefer
Prefer:
- strong hierarchy
- structured negative space
- disciplined contrast
- line systems
- framing systems
- annotation logic
- high information density with readable grouping

---

## Visual language rules

### Theme
Default theme is dark.

Use:
- deep graphite / near-black backgrounds
- charcoal surface layering
- off-white and muted gray text hierarchy
- a single strong accent color family only
- restrained chart palette

Preferred accent:
- vermilion / signal orange / deep red-orange

Optional very limited secondary muted accent:
- olive-gray / dusty green

Do not use multiple saturated accent families in the same screen.

### Composition
Use a point-line-plane based visual language:
- thin guide lines
- frame overlays
- structural dividers
- annotation connectors
- sectional visual grouping
- asymmetrical but controlled composition

### Typography
Typography must support dense analytical reading:
- clear heading hierarchy
- stable line length
- avoid oversized decorative headings
- avoid weak low-contrast body text

---

## Interaction and motion rules

Motion must feel structural, not playful.

### Use
- line growth
- masked reveal
- directional slide
- annotation trace
- connector reveal
- clipped panel transitions
- restrained plotted chart entry

### Avoid
- bouncy spring motion
- exaggerated hover scaling
- flashy hero transitions
- celebratory motion for normal actions

### Hover and focus
Use emphasis through:
- line activation
- annotation marker activation
- subtle contrast change
- border / underline / edge trace

Do not rely on large shadow or scaling changes.

---

## Page architecture rules

For complex analytical pages, prefer this structure:

1. Header / control layer
- title
- filters
- dataset or mode selectors
- export actions

2. Summary layer
- compact metric cards
- key deltas
- current comparison context

3. Analytical body
- tabbed or sectioned views
- one primary chart per section
- secondary breakdown views
- interpretation notes only when useful

4. Evidence / sample layer
- searchable and sortable table
- row-level drilldown
- structured detail panel

5. Audit layer
- evidence mapping
- replay metadata
- status and failure markers
- exportable detail

Do not collapse all content into one scrolling dashboard wall.

---

## Visualization rules

### Preferred
- grouped bar charts
- stacked bar charts
- heatmaps
- box plots
- scatter plots
- distributions
- structured analytical tables
- A/B compare panels
- evidence-reference mapping

### Limited use
- radar chart only when it clearly improves top-level comparison

### Avoid
- pie charts
- donut charts
- speedometer gauges
- 3D charts
- decorative chart gimmicks

Every chart must answer a clear analytical question.

---

## Data and comparison rules

When building comparison interfaces:
- compare on the same analysis unit
- preserve evidence traceability
- expose metadata needed for review
- keep future provider expansion in mind

If one side of the comparison does not exist yet:
- design typed placeholders explicitly
- do not hardcode fake semantics into permanent UI
- keep component contracts extensible

Preferred data concerns:
- provider type
- provider name
- metric bundle
- evidence references
- latency / cost / failure metadata
- replay metadata
- hallucination markers
- structured outputs

---

## Component engineering rules

### General
- Prefer small reusable components with explicit props
- Keep page orchestration separate from presentational components
- Avoid giant page files with mixed rendering, data shaping, and styling logic
- Extract shared primitives only when reuse is real, not hypothetical

### Styling
- Reuse theme tokens and spacing scales
- Avoid arbitrary one-off values unless necessary
- Prefer a coherent tokenized system for:
  - spacing
  - radii
  - border widths
  - line opacity
  - motion timing
  - layer elevation

### Accessibility
- Maintain readable contrast
- Preserve keyboard access for controls, tables, and drawers
- Do not hide essential state in color alone

---

## Refactor rules

When asked to replace an existing page:
- identify obsolete files explicitly
- remove dead code rather than leaving abandoned branches
- preserve valid shared primitives if still aligned with the target system
- document any intentional placeholders for future backend integration

Do not leave duplicate page systems unless the task explicitly asks for incremental coexistence.

---

## Validation requirements

Before finalizing, run the relevant commands if available in the repository:
- install command if needed
- lint
- type check
- unit tests
- build check for the touched surface if practical

If a command cannot run:
- state exactly why
- do not claim success without verification

---

## Definition of done

A UI task is done only if:

1. The implementation matches the requested page architecture
2. The visual language is consistent with this repository’s design philosophy
3. Dead or obsolete code introduced by the replaced page is removed
4. Components are modular and readable
5. The touched code passes relevant validation checks when possible
6. Future integration points are explicit rather than hidden in ad hoc code
7. The result is production-usable, not a static mockup

---

## Expected response format for major tasks

For major refactors, answer in this order:

1. Current understanding
2. Refactor plan
3. Files to remove / create / update
4. Implementation summary
5. Validation performed
6. Remaining placeholders / risks

Keep status updates compact and concrete.

---

## Repository-specific override slot

If this repository later defines:
- route naming conventions
- chart libraries
- state libraries
- theme tokens
- testing commands
- build commands
- folder ownership rules

append them below this section and follow the more specific local rule.