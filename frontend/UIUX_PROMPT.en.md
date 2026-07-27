# Autopoiesis Situational-Awareness Console · Complete UI/UX Prompt (English)

> Purpose: paste this whole document into any model as a system / first-turn prompt. It lets that model keep building new pages and components for this project in exactly the same visual and interaction language **without reading the existing code**.
> Precedence: this document outranks the model's own aesthetic defaults. Wherever it conflicts with "generic modern web design best practice", this document wins.

---

## 0. What product you are building

An **operations console for a self-evolving, long-horizon agent system (Autopoiesis)**: network root-cause analysis on a real corporate LAN, plus security posture, memory evolution, and self-pentest verification.

- The audience is network operations engineers, security analysts, and technical interviewers evaluating the system.
- The interface shows **the record of one real system run** — not a marketing page, not a mockup, not a BI report.
- The product has **4 tabs × 2 scenarios**:
  - Tabs: `situation (console)` / `long trajectory` / `self-pentest` / `hybrid retrieval`
  - Scenarios: `live (internal network)` / `bench (benchmark)`
  - **Hard rule: switching scenario changes the data source, never the UI.** The same tab renders the **same components** in both scenarios, fed from a different endpoint. Never build a second, look-alike component for the second scenario.

---

## 1. The design thesis, in one line

**Light-paper technical instrument + editorial print.**
Cold, precise, diagrammatic, audit-friendly, system-level, restrained. It should read like an instrument's readout panel or a printed technical report — never like a SaaS dashboard.

The test to apply to any design: **"Is the computation itself visible? Can the reader read the real content?"** If a design can only display a container of results and cannot explain the mechanism, it is wrong.

---

## 2. Hard prohibitions (each one cost this project a rewrite — do not repeat them)

### 2.1 Visuals that are never acceptable
- ❌ **Dark theme.** This console is LIGHT. A component that goes dark reads as broken, not as compliant.
- ❌ **Card grids** as the primary page language — including "neat modules with title bars and borders". A card is a static container: it displays results and cannot explain mechanism or algorithm.
- ❌ Glassmorphism, neumorphism, frosted blur, glow, stacked shadows, toy-like large radii.
- ❌ Decorative 3D, gradient fills, rainbow chart palettes, pie charts, donuts, speedometer gauges, radar charts (unless a radar genuinely improves top-level comparison).
- ❌ **More than one saturated accent family on the same screen.** One screen, one accent family.
- ❌ **"AI-generated art" feel**: a single free canvas with rulers, pixel-coordinate readouts, proximity webs, circles scaled by value. This "guess the meaning from the size" abstraction was explicitly judged cheap.
- ❌ Purely abstract algorithm diagrams (ribbons, score bars, D1/D2 document aliases) **as the body of a page** — the viewer cannot see the actual content or understand what was done.
- ❌ And the opposite failure too: **dumping walls of plain text**.

### 2.2 What the right answer is
> **Real content as the substance, carried by a flowing, interactive UX.**

The correct form for algorithmic pages (retrieval, pentest, trajectory) is a **replayable record of a real case**:
a real case selector at the top → ▶ play / pause / click a stage to jump → each step expands to **real** document titles, real body text, **matched-term highlighting**, and journey badges → the final step shows the actual context passed downstream, with the token budget filling up.
Abstract graphics may only act as **connective tissue** between real content — never as the subject.

---

## 3. Design tokens (copy these hex values verbatim; do not re-tune them)

Re-declare these tokens at the root selector of every component stylesheet so each component is self-contained:

```css
.xx {
  /* paper surfaces · three layers */
  --paper:   #e7e7e3;   /* page ground */
  --paper-2: #efeee9;   /* raised: hover, expanded regions, module fill */
  --paper-3: #dddcd5;   /* recessed: wells, masked idle steps, code backing */

  /* ink · type and structure */
  --ink:      #0d0d0d;  /* primary type, 2px structural frames (17.4:1) */
  --ink-soft: #2a2a27;  /* secondary body copy */

  /* neutrals */
  --gray: #605f5a;      /* secondary gray that MAY carry text (4.94:1, AA) */
  --rule: #cfcfca;      /* hairlines only — never carries text */
  --quiet:#8c8c88;      /* quiet marks / strokes only — never carries text */

  /* the single accent */
  --acid:     #ccff00;
  --acid-ink: #0d0d0d;

  /* severity family (algorithmic + security pages only) */
  --hi:  #c3384a;       /* high / external offense (aliases: --hot, --wan) */
  --mid: #a86a12;       /* medium / watch (alias: --amber) */
  --lo:  #8a8a86;       /* low / informational */

  /* internal-network / structure family */
  --teal:    #12615c;   /* teal AS TYPE (5.4:1): internal, read-only-safe, defence holding */
  --net:     #1c8f88;   /* teal as a stroke on marks */
  --wan-ink: #a5233a;   /* crimson AS TYPE (5.0:1) */

  /* hierarchy tiers — structural identity, NOT threat level */
  --tier-core:   #0d0d0d;
  --tier-subnet: #2a2a27;
  --tier-edge:   #605f5a;
  --tier-leaf:   #8c8c88;

  /* type families */
  --font-display: 'IBM Plex Sans Condensed', sans-serif;  /* weight 600, headings only */
  --font-mono:    'IBM Plex Mono', monospace;             /* 400/500, body and all data */

  /* motion */
  --ease-guide: cubic-bezier(0.2, 0, 0, 1);
  --t-mark:  160ms;   /* mark state change */
  --t-point: 240ms;   /* pointing */
  --t-set:   300ms;   /* settling into place */
  --t-wipe:  360ms;   /* masked reveal */
  --t-count: 500ms;   /* number count-up */
}
```

### 3.1 The accent law (the rule most often broken — hold it)
- `--acid #ccff00` is the **only** accent, and it is a **fill only**. It must **never** be bare text on paper (insufficient contrast).
- It carries exactly one meaning, and only one per screen: **interaction** — selected / acted on by the agent / changed at the current step.
- It must **never** encode tier, severity, or category. Distinguish categories by **structure** — frame weight, hatching, dot grid, rule position, solid vs dashed — never by adding another color family.
- On the situation (console) view, severity is expressed **purely through ink weight and structure**, never hue: high = full ink, filled mark, hatch/ping ring; watch = gray, filled mark, dashed ring; ok = hollow mark on paper.
- The crimson and teal families are allowed only on **two mutually exclusive screens** (the WAN-ingress view vs. the drilled-in segment view). They never appear together.

---

## 4. Typography

| Role | Spec |
|---|---|
| Page masthead title | `--font-display` 600 / `clamp(30px, 3.2vw, 52px)` / `line-height:.9` / `letter-spacing:-.02em` / uppercase; wrap the key word in `<mark>` → `background:var(--acid); color:var(--acid-ink); padding:0 .05em` |
| Kicker / code above the title | 9.5px / `letter-spacing:.2em` / uppercase / `--ink-soft` |
| Thesis line (under the title) | 12.5px / `line-height:1.5` / `--ink-soft` / `max-width:88ch` |
| Section label | 10.5px / 600 / `.1em` / uppercase / `border-bottom:2px solid var(--ink)` |
| Body | 11–13.5px mono / `line-height:1.45–1.5` / measure capped at 78–88ch |
| Micro labels, pills, units | 8–9.5px / `letter-spacing:.06–.12em` / uppercase |
| Large figures | 26px / `font-variant-numeric: tabular-nums` / unit as an inline `<i>` at `.38em`, colored `--gray` |

- Body copy across the whole app is **monospace**, not sans. The condensed sans is reserved for large headings.
- Every number uses `font-variant-numeric: tabular-nums` so digits do not jitter.
- No oversized decorative headings that outshout the data; no weak low-contrast body text.

---

## 5. Frames and geometry

- `border-radius: 0` — **effectively zero across the project**. At most one exception per stylesheet (e.g. drawing a padlock glyph).
- **2px solid var(--ink)** = structural division: masthead underline, section-label underline, scenario switcher frame, the left rule of each metric in a metric strip.
- **1.5px solid var(--ink)** = module frames, selector frames, command blocks.
- **1px solid var(--rule)** = hairline separators, pill borders, list-row dividers.
- **border-left: 2–3px** = state / severity encoding on a row, using `--hi/--mid/--lo` or `--teal`.
- **Forbidden**: box-shadow for depth, radii for softness, gradients for fills. Depth comes from the **three paper layers plus frame weight**.
- Canvas backdrop (topology / flow grounds): `linear-gradient(rgba(13,13,13,0.05) 1px, transparent 1px)` in both axes, cell 46–48px; when overlaying a fine grid, use a quarter cell at `0.03` alpha.
- Dangerous / approval-gated regions get **45° hatching**: `repeating-linear-gradient(-45deg, transparent 0 7px, color-mix(in srgb, var(--hi) 6%, transparent) 7px 8px)`.

---

## 6. Component vocabulary (these are the project's "words" — reuse them in new pages)

1. **Masthead** — kicker code → large title (with an acid `<mark>`) → thesis line → target row (authorization scope / CIDR / timestamp); on the right, a **metric strip** where each metric has `border-left:2px solid var(--ink)`, a large figure, and an 8px uppercase label.
2. **Section** — a 2px ink rule with an uppercase label sitting on it. No card shell.
3. **Selectors / switchers** — a row of buttons inside a 1.5–2px ink frame, divided by rules of the same weight. Selected state = ink fill with acid type (primary switch) or a `--paper-3` fill (secondary switch).
4. **Findings list** — each row: a 3px severity rule on the left → severity number (tabular) → kind pill (ink fill, or `--hi` fill knocked out in paper) → title → host/port → status pill on the right (🔒 gated = ink frame, 👁 read-only = teal frame). The whole row is a button and expands **in place**. No modals.
5. **The expansion is a runnable playbook** — goal → authorization prerequisite (`--paper-3` fill with a 2px ink left rule) → numbered steps (2px teal left rule; intrusive steps switch to `--hi` plus 45° hatching) → a read-only/intrusive mode pill per step → **command block** (ink fill, `#e9e9e2` mono type, a 34px copy button on the right whose glyph is acid) → a two-column verdict (confirm / mitigated) → remediation → an evidence-id trail.
6. **Replay transport** — `▶/❚❚` plus a progress bar and a counter ("event i/N · pass Pn"); the grid or step list reveals cell by cell as the playhead advances, the current cell gets a thin acid `now` frame, and figures count up.
7. **Topology canvas** — a full-bleed SVG, clustered by vendor/tier (sunflower packing), curved edges, hover lights the neighborhood subtree and opens a detail panel top-right, zoom and pan, a breadcrumb back to the global view. **The benchmark scenario reuses this exact component** by mapping the memory graph into the same data shape.
8. **Record block (the body of algorithmic pages)** — real document title, real body text, **matched-term highlighting**, hit chips, and journey badges (hit# → fused# → gate kept → context t); click to expand the real full text.
9. **Honest degradation marks** — an unavailable capability is **marked, not dimmed**. Strike through the name and add a small "Off" chip with a 1px frame and `--gray` type. Never crush it with `opacity:.4` — that hides the fact instead of stating it.
10. **Search bar** — debounced input with a results dropdown; each result carries a severity-colored left border; picking one drills into the subnet and focuses the target.

---

## 7. Interaction and motion

**Motion must be structural, not entertaining.**

- Allowed: line growth, masked wipe reveals, directional slide-in, annotation-connector tracing, clipped panel transitions, number count-up, restrained plotted chart entry.
- Forbidden: spring/bounce, hover scaling, flashy hero transitions, celebratory motion for ordinary actions, any glow.
- Hover/focus emphasis comes from line activation, marker activation, subtle contrast change, and border/underline/edge tracing. **Never** from large shadows or scaling.
- Standard reveal keyframe: `from { opacity:0; transform:translateY(-4px) } to { opacity:1; transform:none }` at `.35s ease`.
- **Every animation needs** a `@media (prefers-reduced-motion: reduce)` escape that shows everything immediately — not a slower version.
- Keyboard access is preserved for controls, tables, and drawers.

---

## 8. The honesty rules (the soul of this project — carry them across)

The console renders **a real kernel run**. Therefore:

1. **Every visual mark equals one real datum.** The frontend never synthesizes positions, scores, reasons, or diffs and presents them as system behaviour — that is the exact failure this UI was rebuilt to eliminate.
2. **If a value does not exist, say so on the surface.** Do not fill the gap. The backend reports which capabilities are actually wired (`capabilities`); respect it.
3. **Never render a constant as though it varied** (e.g. when decay is not wired, `strength` is 1.0 on every record — do not draw it as a curve).
4. **Report counts truthfully** from the real trace; never invent them. Data that only exists on the eval bench must be labeled as bench-only.
5. **Never fabricate a self-answering query** — always use the real case text.
6. **Gating must be visible**: read-only steps are labeled safe and are copy-and-run; intrusive/exploit steps are marked in crimson with hatching and "requires approval · do not run directly", and **no weaponizable payload is shown** (write `# GATED · payload/wordlist withheld`).
7. **Essential state is never encoded in color alone** — always pair it with text or structure.
8. Work that has not actually run is labeled "pending run" or "public baseline, for reference"; local scores are never fabricated.

---

## 9. Page architecture template

Organize complex analytical pages in this order. **Do not collapse everything into one scrolling wall.**

1. **Header / control layer** — title, scenario and dataset selection, mode switches, export.
2. **Summary layer** — compact metric strip, key deltas, current comparison context.
3. **Analytical body** — sectioned or stepped; one primary view per section (record, topology, flow) plus secondary breakdowns where they earn their space.
4. **Evidence / sample layer** — searchable and sortable table, row-level drilldown, structured detail panel.
5. **Audit layer** — evidence mapping, replay metadata, status and failure markers, exportable detail.

Chart selection: prefer grouped bars, stacked bars, heatmaps, box plots, scatter, distributions, structured analytical tables, A/B compare panels, and evidence-reference mapping. Every chart must answer a clear analytical question; if it cannot, delete it.

---

## 10. Stack and code conventions

- React 19 function components + TypeScript + Vite. Fonts via `@fontsource/ibm-plex-mono` and `@fontsource/ibm-plex-sans-condensed`.
- **Hand-written CSS, one `.css` file per component.** No Tailwind, no UI kit, no CSS-in-JS.
- Each component owns a **short unique namespace prefix** (`pt-` pentest / `rt-` retrieval / `tb-` bench trajectory / `fx-` trajectory HUD / `ls-` live situation / `mg-` memory graph / `bt2-` bench topology) to avoid global collisions.
- Re-declare tokens at the component root selector, plus a scoped `.xx *, .xx *::before, .xx *::after { box-sizing: border-box; }`.
- Visualization is **hand-authored SVG** by default (topology, Sankey ribbons, constellations, flows); echarts only for standard statistical charts; three / @react-three-fiber only for the single 3D scene.
- Data comes from `/api/rca/*` with explicit TypeScript interfaces; the scenario travels as a `?scenario=live|bench` query parameter and **must be in the `useEffect` dependency array**.
- Bilingual: every user-facing string goes through `i18n.ts` with `lang: 'zh' | 'en'`.
- Small reusable components with explicit props; keep page orchestration separate from presentational components; no giant page files mixing rendering, data shaping, and styling.
- When replacing a page, **delete the obsolete files explicitly**. Never leave two parallel page systems alive.

---

## 11. Definition of done

A UI task is done only when all of the following hold:

1. The visuals match the light-paper editorial system described here; nothing renders dark.
2. One accent family per screen; `--acid` means interaction only and never appears as bare text on paper.
3. `border-radius` is 0 (at most one glyph-drawing exception); depth comes from the three paper layers and frame weight, with no shadows or blur.
4. The page body is **real content plus interactive flow** — not a card grid, not an abstract diagram, not a wall of text.
5. Every visual mark traces to real backend data; unavailable capabilities are explicitly marked rather than hidden or faked.
6. All motion is structural and has a `prefers-reduced-motion` escape; contrast meets AA; no essential state is color-only.
7. Dead code from the replaced page is removed; components are modular and readable.
8. `npm run lint`, `tsc -b`, `npm run build`, and the relevant tests pass. If a command cannot run, state exactly why — never claim success without verification.

---

## 12. Response format for major tasks

Answer in this order: ① current understanding → ② refactor plan → ③ files to create / delete / update → ④ implementation summary → ⑤ validation performed → ⑥ remaining placeholders and risks. Keep status updates compact and concrete.
