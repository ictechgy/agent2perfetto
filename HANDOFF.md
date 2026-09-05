# HANDOFF — what the next session should do

State at writing: v0.1.0, 3 commits, 24/24 tests green, clean tree, **no remote, not on PyPI**.
Read `AGENTS.md` first (invariants, golden-regeneration recipe, counter semantics).

## 1. Do first — two open P2 bugs (both small, both probed & documented)

- [ ] **tool_use `input` is embedded verbatim/unbounded in trace args.** A 10MB Write input
      produces a 10MB trace (probed). Fix: wrap with the existing `_shallow_truncate` —
      `trace.py`, tool_use slice `args`: `"input": _shallow_truncate(tu.input)`.
      Add a bounded-size test next to `test_preview_is_bounded_for_huge_payloads`.
- [ ] **RecursionError crash on pathological lines.** A single ~100k-deep JSON line makes
      `json.loads` raise `RecursionError`, which the parser's `except json.JSONDecodeError`
      does not catch → whole conversion dies (probed at 200k; 20k is fine). Fix: catch
      `(json.JSONDecodeError, RecursionError)` in `parser.parse_lines` and count as
      malformed. Add a deep-line fixture + test.
- [ ] P3 while there: `tests/test_golden.py` docstring still says "cumulative token sums" —
      update wording to occupancy/spend.
- [ ] Commit as one "fix" commit, then regenerate `examples/sample_session.perfetto.json`
      if output changed (it shouldn't — the example has no huge inputs).

## 2. Ship it

- [ ] GitHub repo + push; CI (`.github/workflows/ci.yml`) has never run — confirm green on
      the 3.10 and 3.13 matrix.
- [ ] PyPI publish (`agent2perfetto` name — verify availability; low collision risk).
      Then flip README quickstart install line to real commands.

## 3. First-publication asset (from 기획서 strategy)

- [ ] Record the 1-minute demo GIF: one JSONL → `--open` → scroll Perfetto UI (turns/tools
      slices, flows, ctx/spend counters). This GIF *is* the launch — the tool sells itself
      visually. Use `examples/sample_session.jsonl` or a sanitized real session.

## 4. v0.2 — adapter line (from 기획서, in order)

- [ ] **OTel GenAI adapter**: second input format behind the same parser interface.
- [ ] **yield-audit export**: `yield export --perfetto` in `../yield-audit` calling this
      converter on its normalized event model (M5 cache-locality and M10 handoff lanes
      become native tracks). Cross-repo work — coordinate with the yield-audit HANDOFF.
- [ ] **Subagent/async slices**: subagent sessions as separate processes with `s`/`f`
      handoff arrows (Claude Code `isSidechain` records already parsed but unrendered).
- [ ] v0.3 line: proto-format exporter (Perfetto's recommended format), context-guard
      audit overlay (death/survival coloring on ctx lanes), "waste browser" (highlight
      retry chains).

## 5. Known limits to keep honest

- `ctx_*` is per-call occupancy from client-reported usage, not the provider-side context
  window — README + `metadata.approximation_note` must keep saying so.
- Flow `s`/`f` rendering in ui.perfetto.dev was never visually confirmed (headless dev
  env). Worst case the importer ignores them; slices/counters carry the information.
  Verify once with a browser and note the result here.

## Context pointers

- Golden regeneration procedure (hand-verify → freeze → update hand-computed tests) is in
  `AGENTS.md` — follow it exactly whenever `lanes.py`/`trace.py` mapping changes.
- Differentiation table in README (LangSmith/ccusage/Perfetto-MCP) is part of the launch
  story; keep it current when v0.2 adapters land.
