# AGENTS.md — agent2perfetto

Converts a Claude Code session log (JSONL) into Perfetto / Chrome Trace Event JSON that
loads in ui.perfetto.dev. The thesis: don't build a viewer — emit a standard format and
let Perfetto's free browser viewer do the rendering. See README.md for track map,
approximation notes, and differentiation.

## Layout

```
src/agent2perfetto/
  parser.py  tolerant JSONL → typed Records + ParseStats (skip+count malformed/unknown; --strict raises)
  lanes.py   context lanes: ctx_* = per-call occupancy, spend_* = cumulative sums
  trace.py   Chrome Trace Event builder (M/X/C/i/s+f), deterministic ordering, _shallow_truncate previews
  cli.py     argparse CLI: input, -o, --open (browser-local only), --strict
tests/fixtures/   synthetic valid/malformed/empty sessions + FROZEN golden_trace_events.json
examples/         sample session + its generated trace (checked in)
scripts/validate_trace.py   stdlib self-check for any output trace
```

## Commands

```bash
pip install -e . && pip install pytest   # zero runtime deps; pytest is test-only
pytest
agent2perfetto examples/sample_session.jsonl --open
python scripts/validate_trace.py <trace.json>   # self-check any output
```

## Invariants — do not break

- **Zero runtime dependencies**, Python ≥ 3.10. `datetime.fromisoformat` on 3.10 rejects
  `Z` and >6-digit fractions — `parser.epoch_us_from_iso` normalizes both; don't regress.
- **Determinism**: same session in → byte-identical `traceEvents` out (only
  `metadata.generated_at` varies). Events sort by `(ts, rank, seq)`; metadata events go
  first; timestamps are µs since the earliest record and must be monotonically
  non-decreasing with `dur >= 0`.
- **Counter semantics**: `ctx_*` = occupancy of ONE assistant call (that call's
  input + cache_read + cache_creation). `spend_*` = cumulative sums across the session.
  `cache_read` re-bills the same cached tokens every call, so spend is a billing
  trajectory, **never** context size. `APPROXIMATION_NOTE` (lanes.py) and the README
  "Approximation honesty" section must stay in sync with this.
- **Tolerance**: malformed JSON lines, non-object lines, unknown record types, and
  missing/bad timestamps are skipped and counted (warnings to stderr); unknown top-level
  fields are counted, not dropped silently; `--strict` turns the first problem into a
  failure. Known open issue: `json.loads` raises `RecursionError` on ~100k+-deep lines —
  widen the except clause if you fix it rather than letting it crash the run.
- **Previews are bounded** by `_shallow_truncate` (500 chars): result previews and user
  prompts. Known open issue: tool_use `args.input` is still embedded verbatim — wrap it
  in `_shallow_truncate` if you touch `trace.py`.
- **Privacy**: never read or copy real `~/.claude` transcripts into this repo. Fixtures
  are synthetic with realistic field shapes. `--open` only opens a URL in the browser;
  the trace is never uploaded.

## Changing the trace mapping (lanes / trace)

1. Update `lanes.py` / `trace.py`.
2. Regenerate the golden: run `build_trace(parse_file(tests/fixtures/valid_session.jsonl))`
   and write `traceEvents` to `tests/fixtures/golden_trace_events.json` — but **hand-verify
   counter values and durations against the fixture's usage fields first** (the golden
   test freezes exactly what you generate).
3. Update the hand-computed expectations in `tests/test_trace.py::test_counter_values_match_hand_computed_sums`.
4. Update the README track map + approximation sections if track names or semantics changed.

## Testing gotchas

- `pip install pytest` into the same venv as `-e .`; tests import `agent2perfetto` from
  the editable install.
- The example trace under `examples/` is generated output — regenerate it whenever the
  mapping changes so the checked-in example stays truthful.
