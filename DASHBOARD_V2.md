# SAD Dashboard V2: Agents + Brain + Infra Docs

The dashboard is a local, offline-bundled React/TypeScript application with a
Python/stdlib localhost API. It preserves the Spore-inspired instrument panels,
existing decompiler telemetry, launch controls and function detail screens,
while providing a separately managed, interactive Collective Memory view.

## Application boundaries

- dashboard_web/src/App.tsx: shell, shared telemetry polling and one permanent bottom command bar.
- dashboard_web/src/components/AgentDock.tsx: permanent inline decompiler launch/stop controls.
- dashboard_web/src/views/InfraDocs.tsx: read-only, annotated system diagram grounded in actual controller, API, persistence and consolidator modules.
- dashboard_web/src/components/ErrorModal.tsx: scrollable full consolidation error detail with clipboard copy.
- dashboard_web/src/components/KnowledgeDock.tsx: consolidator control panel available in Brain view.
- dashboard_web/src/views/Operations.tsx: existing worker fleet, byte metrics,
  function explorer, expedition controls, provisional planning, allowlist editor,
  claimed-user selection, attempt history and checker diagnostics.
- dashboard_web/src/views/Knowledge.tsx: knowledge health, visual evidence graph,
  hybrid search, technique explorer, historical findings, annotations,
  consolidation errors and service/budget management.
- dashboard_web/src/components/: shared presentation primitives, inspectable
  graph, semantic search console. Zero direct filesystem/SQLite/service access.
- dashboard_web/src/api.ts: typed same-origin HTTP and anti-CSRF client.
- dashboard/server.py: localhost-only API router, static assets and strict
  control-mode + token + Origin checks. No business logic in request handlers.
- dashboard/knowledge.py: independent knowledge database read models and
  search; a narrow, audited-by-source annotation boundary is the only edit
  to the knowledge DB. No writes to the decompiler's operational SQLite.
- dashboard/consolidation.py: whitelist-only systemd user unit controls
  and atomically written runtime settings. No arbitrary shell input.
- spore_dashboard_data.py: existing operational read models, retained.
- spore_dashboard_control.py: existing operational worker/scope controls,
  retained, with their existing budget/key/worker safety rules.
- spore_technique_consolidator.py: DeepSeek worker, unchanged model and
  provider; reads optional dashboard budget settings between batches.
- spore_dashboard.py: backwards-compatible import shim.

The compiled frontend is checked in at dashboard_web/dist by CI, so no
Node.js, Vite or npm process is needed on the WSL runtime machine.
Dashboard HTML, CSS and JavaScript serve from 127.0.0.1:8765. The UI makes
same-origin API requests, and no public API port is introduced.

## Compact styling and scroll performance

The dashboard has a compact density override in
dashboard_web/src/compact.css, imported after the original component styles.
It deliberately retains the original SAD instrument look: steel-blue rails,
rounded mission panels, Spore-style status colors and green primary actions.

The first React implementation was too spacious and expensive to scroll on
large Windows/Chromium displays. The compact mode addresses this structurally:

- Removes fixed-background wallpaper repainting and panel backdrop blur.
- Restores a viewport-bounded mission-control shell on sufficiently tall
  desktop displays, with scroll limited to necessary data areas.
- Keeps 16 agent cards small and fixed-height instead of stretching the grid
  to the entire document's height.
- One permanent Spore-style bottom bar shows SAD, Agents and Brain navigation, agent controls and actual backend connection status in that order. Consolidation controls are available inside Brain, not as a second bottom bubble.
- Restores the original clickable SVG technique-to-finding source-link map within a fixed, scrollable panel. Graph contents cannot change the size of neighbouring panels.
- Loads only the active React view and pauses its requests when it is
  unmounted or the browser is hidden. Polling is serialized, abortable and
  cannot pile up overlapping requests.
- Uses local CSS and static assets only, with no animations or layout zoom
  transforms for the graphs and data tables.

The original dashboard remains at /legacy for visual reference. The compact
patch does not modify worker operations, evidence storage, model selection,
paid OpenRouter requests or the API authorization boundary.

The fast regression suite runs tests/test_dashboard_compact.py alongside the
React/TypeScript build and existing backend security checks. Performance
should still be verified in a real Windows browser, because these tests
cannot measure GPU compositor frame time.

## Run

From the SAD checkout:

~~~bash
cd ~/spore-agentic-decompiler
python3 spore_agentic.py dashboard --control
~~~

Open http://127.0.0.1:8765. Operations opens by default with the agent fleet. The single command bar remains present in both views, with Agents and Brain tabs, unchanged agent controls and the backend connection indicator.
The old dashboard remains accessible at http://127.0.0.1:8765/legacy and
is used automatically if the compiled frontend does not exist.

The dashboard itself does not start the DeepSeek consolidator or decompiler
workers on page load. Both are independently managed processes. Knowledge
shows their current persisted state. In --control mode, the Knowledge view
can pause/start an already installed systemd user service and edit its
daily cost cap, batch size and polling interval. Budget settings take effect
at the next consolidation cycle, without restarting the daemon.

Opening the dashboard read-only (without --control) never authorizes
mutations. Notes, pins and service actions are disabled in that mode.

## Build/development

Runtime: Python 3.11+, no extra dashboard Python dependencies.
Development/build: Node 22+, npm, React 19, TypeScript 6 and Vite 8.

~~~bash
cd dashboard_web
npm ci
npm run typecheck
npm run build
~~~

The output goes to dashboard_web/dist and can be served immediately by the
Python application. For hot reload, start the Python dashboard on port 8765
and run npm run dev in dashboard_web; Vite proxies /api and /wallpaper to it.
Because the API token is embedded by the production Python HTML response,
mutation actions are not available in direct Vite development mode unless
a local authorized build is served through Python. Read-only data remains
available through the Vite proxy.

The dashboard-v2 CI workflow runs TypeScript typechecks, a production Vite
build, backend and security tests, then commits the offline assets and lockfile
on the same branch. The installed Python runtime does not call npm or pull
packages.

## Knowledge semantics and safeguards

The Knowledge view distinguishes:

1. Original function investigations and independent check outcomes.
2. DeepSeek-extracted techniques, always labeled observed/hypothetical.
3. Verbatim evidence links to the source investigation.
4. User-entered private notes and pins, stored separately and never able
   to change a source's checker result.

Search Collective Memory combines FTS5 and the existing local
BAAI/bge-small-en-v1.5 vector daemon if online. It returns both technical
techniques and function findings and opens their real source records.
The search does not call OpenRouter or pretend to be an LLM chat.
When the vector daemon is unavailable, FTS5 works without paid requests.

Sensitive capability separation:

- Browser never sees the OpenRouter API key or the repository's local paths
  except harmless evidence-path identifiers.
- The dashboard does not expose a generic subprocess or shell endpoint.
- Only --control can mutate state, using the per-server token and existing
  origin/localhost validation.
- Service actions target the single fixed systemd user service.
- Runtime consolidation configuration is validated and saved atomically
  with 0600 file permissions; invalid configs pause the worker.
- The decompiler's operational SQLite is only accessed through the existing
  operational telemetry and controller modules.
- Knowledge reads use SQLite read-only connections. Source-hash-invalid
  evidence links are excluded from retrieval and graph visualization.
- The original standalone consolidation queue, lock, retries, spending
  ledger and provider pinning are unchanged.

## Planned future separation

The operational telemetry and worker controller modules are substantial,
existing battle-tested code. They remain isolated behind the new dashboard
API rather than being rewritten wholesale during a UI migration. That keeps
the running decompiler stable. Their internals can later be divided into
separate read-model and service modules without another frontend rewrite.

This feature is developed on feature/dashboard-knowledge-v2. Main and dev
continue to work until the feature branch is validated and merged.

## Knowledge interaction performance

Knowledge now has a single native scrolling owner. Its previously nested result-list scroll is removed, and Knowledge panels/cards have static opaque backgrounds, no backdrop blur and no expensive hover transitions. The single bottom command bar remains mounted outside the scroll surface. CI validates builds and regression tests, but cannot measure actual Windows browser GPU frame time.

## Raw findings crash and render isolation

The Knowledge technique and raw-findings tabs use different item shapes. The polling hook previously retained the previous URL's data during the next query, so switching tabs rendered technique entries as findings and passed an absent status into the Tag component. Poll responses are now URL-keyed, and tags tolerate missing data defensively. The entire Knowledge component is memoized so the shell's three-second agent-snapshot refresh doesn't cause Brain rerenders. Search is memoized, and off-screen result cards use CSS content-visibility to skip rendering. The backend already uses ThreadingHTTPServer and paginates findings/techniques at 30 records per page; there is no full raw dump.

## Fixed workspace and Infra Docs

Agents, Brain and Infra Docs occupy the viewport above the single persistent 50px command bar. Brain no longer repeats a large title or the instructional How knowledge is formed pipeline. Its six metric cards, search bar, relationship graph, evidence explorer, consolidation controls, queue and error list are allocated fixed grid tracks. Contents scroll only inside the assigned panel, with stable card rows. Opening a result or detail uses a React portal so the overlay sits over the HUD, not behind it, and never expands an existing card. Consolidation errors show compact summaries in the sidebar; clicking opens a centered scrollable modal with the original error text and a Copy button. No error handling semantics are changed. Infra Docs is a separate #infra view, reached by its button immediately before CONNECTED, showing an annotated source-grounded topology diagram and design commentary.

## 2026-11-18: compact graph, Preview dialog and diagram-only Infra

Brain now launches the evidence map from a compact card. The map opens in a portaled overlay with drag-to-pan, wheel zoom, zoom buttons and fit-to-view, without altering main layout dimensions. The Agent dock Preview error and success response open a centered modal with a scrollable full response and Copy button, instead of squeezing an exception into the thin HUD notification. Infra Docs contains only the full-height annotated infrastructure topology diagram, not a heading, explanation sidebar, or additional title card. The node annotations provide all explanatory context.

## Infra Docs horizontal architecture and unified card styling

The Infra Docs canvas now lays out left-to-right (React dashboard → localhost HTTP API → split into two horizontal three-node flows). The agent branch shows controller → workers/checker → operational SQLite, and the Knowledge branch shows read model ← Knowledge SQLite ← independent DeepSeek consolidator. Annotations and paths stay inside fixed-height cards. The architecture diagram is the *entire* page, without an extra heading or design-commentary column. At narrower viewports, only the diagram canvas scrolls horizontally. Its gradient card surfaces, borders and inset bevels reuse the existing SAD panel style, and the same gradient style is restored to Brain panels, metrics and results, replacing mismatched flat cards.
