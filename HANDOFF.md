# HANDOFF — what the next session should do

State at writing: v0.2.1 (Codex adapter) **live on PyPI** (`pip install agent2perfetto`)
and public at **github.com/ictechgy/agent2perfetto**, CI green on the 3.10/3.13 matrix.
P2 bugs fixed, Agent Trace IR refactor (기획서 v0.2.0) landed, all tests green, 기획서.md
purged from history + gitignored. Read `AGENTS.md` first (invariants, golden-regeneration
recipe, counter semantics).

## 1. DONE since last handoff (for context)

- [x] **P2 fixed — tool_use `input` bounded.** Wrapped in `_shallow_truncate`;
      bounded-size test in `tests/test_trace.py`.
- [x] **P2 fixed — RecursionError on ~100k-deep lines.** Parser counts them as
      malformed; lenient + strict tests in `tests/test_parser.py`.
- [x] **P3 fixed** — test_golden docstring wording (occupancy/spend).
- [x] **기획서-크로스에이전트-노멀라이저.md v0.2.0 landed**: pipeline is now
      adapter (parser.py) → Agent Trace IR (ir.py) → emitter (trace.py).
      Schema doc: `docs/agent-trace-ir.md`. Bypass regression
      (IR path == frozen golden == session path == JSON round-trip) in
      `tests/test_ir.py`. Example regenerated (metadata only — converter version
      0.2.0; traceEvents byte-identical).

## 2. Ship it

- [x] GitHub repo public + pushed: https://github.com/ictechgy/agent2perfetto
- [x] CI confirmed green on the 3.10 and 3.13 matrix. Cosmetic: actions warn about
      Node.js 20 deprecation (checkout@v4 / setup-python@v5 are force-run on Node 24) —
      bump the action versions some day; nothing fails today.
- [x] **PyPI: agent2perfetto 0.2.0 live** — trusted publishing via pending publisher,
      release-triggered (`.github/workflows/pypi.yml`, publish job runs in the `pypi`
      environment). Publishing recipe: bump `__version__`, push, `gh release create
      vX.Y.Z` — the workflow builds and uploads; the tag must match `__version__`.
      README quickstart flipped to `pip install agent2perfetto`.

## 3. First-publication asset

- [ ] Record the 1-minute demo GIF: one JSONL → `--open` → scroll Perfetto UI (turns/tools
      slices, flows, ctx/spend counters). This GIF *is* the launch — the tool sells itself
      visually. Use `examples/sample_session.jsonl` or a sanitized real session.
      Needs a human with a browser; cannot be produced headlessly.

## 4. Next code work (기획서 order)

- [x] **v0.2.1 — codex adapter landed**: `codex.py` parses rollout JSONL straight into
      `ir.AgentTrace` (no parser.py dependency beyond ISO normalization); CLI auto-detects
      the format (`--format {auto,claude,codex}`). Cross-validated against yield-audit's
      CodexAdapter on the shared fixture (tool uses incl. Bash normalization, error flags,
      token usage all agree — run their adapter with a SHARED ctx dict per file).
      Approximations documented in the module docstring: usage merges onto the assistant
      message timestamp; codex has no cache-creation counter.
- [x] **yield-audit export shipped** (yield-audit v0.5.0, on PyPI): `yield-audit
      export --perfetto` maps its vendor-neutral Session model onto this project's
      Agent Trace IR (`agent2perfetto>=0.2.1` behind an optional `[perfetto]` extra —
      yield-audit's zero-runtime-dep rule intact). First external IR consumer.
      Note: ApiCall cache_read/write map straight onto IR cache keys; ToolUse turns
      render as usage-less model_calls (0.2.2 lanes skip those, no zero-dip counters).
- [ ] **Subagent/async slices**: Claude Code `isSidechain` records are still parsed
      but unrendered.
- [ ] v0.3 line: `emit --otel` (IR → OTel spans), publish `docs/agent-trace-ir.md` as
      the standard proposal, context-guard audit overlay (death/survival coloring on
      ctx lanes), "waste browser" (highlight retry chains).

## 5. Known limits to keep honest

- `ctx_*` is per-call occupancy from client-reported usage, not the provider-side context
  window — README + `metadata.approximation_note` must keep saying so.
- Flow `s`/`f` rendering in ui.perfetto.dev was never visually confirmed (headless dev
  env). Worst case the importer ignores them; slices/counters carry the information.
  Verify once with a browser and note the result here.

## Context pointers

- Golden regeneration procedure (hand-verify → freeze → update hand-computed tests) is in
  `AGENTS.md` — follow it exactly whenever `lanes.py`/`trace.py` mapping changes. The IR
  bypass test (`tests/test_ir.py`) must stay green across any refactor.
- Differentiation table in README (LangSmith/ccusage/Perfetto-MCP) is part of the launch
  story; keep it current when v0.2 adapters land.
- Strategy docs: 기획서.md (v0.1 launch), 기획서-크로스에이전트-노멀라이저.md (IR/normalizer line).
