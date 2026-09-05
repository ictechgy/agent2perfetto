# agent2perfetto

**Don't build a viewer — put agent traces in the best one.**

`agent2perfetto` converts agent session logs into
[Perfetto](https://perfetto.dev/) / Chrome Trace Event JSON that loads in
[ui.perfetto.dev](https://ui.perfetto.dev) — the free, browser-based trace viewer Google built
for Chrome and Android profiling. You get hierarchical slices, counter time series, flow
arrows, and PerfettoSQL over your agent session without anyone writing a line of viewer code.
Supported inputs: **Claude Code** session JSONL and **Codex CLI** rollout logs (auto-detected;
`--format` to override).

## The problem

Agent observability today is bipolar:

- **Vendor SaaS dashboards** (LangSmith, LangFuse): cloud accounts, proprietary UIs, your
  traces leaving your machine.
- **Text dumps and aggregators** (ccusage): totals and averages, no time axis.

But an agent session *is* a hierarchical event stream on a timeline: session → turns → tool
calls → results. The one local UI that renders exactly that shape — millions of events,
layered slices, counter tracks, flows — already exists, is free, runs in your browser, and
officially accepts arbitrary trace-like data. Nobody was sending agent traces to it.
This project is exactly that adapter.

## Quickstart

Requires Python 3.10+, stdlib only, zero runtime network calls.

```bash
# from PyPI
pip install agent2perfetto

# or from a checkout
pip install .

# convert the checked-in example
agent2perfetto examples/sample_session.jsonl
# -> examples/sample_session.perfetto.json

# convert one of your own sessions and open the viewer
agent2perfetto ~/.claude/projects/<project>/session.jsonl --open

# Codex CLI rollout logs work too (format is auto-detected)
agent2perfetto ~/.codex/sessions/2026/09/05/rollout-*.jsonl --open
```

`--open` runs macOS `open https://ui.perfetto.dev` (falls back to your browser elsewhere),
then prints drag-and-drop instructions. Drag the generated `.perfetto.json` into the UI —
the trace is parsed **in your browser's memory and never uploaded**. (For a fully offline
setup, Perfetto's UI also ships as a WASM bundle you can serve from localhost.)

Self-check a trace file any time:

```bash
python scripts/validate_trace.py examples/sample_session.perfetto.json
```

## What the trace looks like (track map)

| Agent concept      | Perfetto element                                                              |
| ------------------ | ----------------------------------------------------------------------------- |
| Session            | process (one process per `sessionId`, named `agent session <id>`)              |
| Assistant turns    | complete slices (`X`) on the **turns** thread                                  |
| Tool calls         | complete slices named by tool (`Bash`, `Read`, …) on the **tools** thread       |
| Tool results       | complete slices (`result <tool>`) on the **tools** thread                       |
| Call → result      | flow arrows (`s`/`f` events paired by `tool_use_id`)                           |
| Context occupancy  | counter tracks `ctx_total`, `ctx_input`, `ctx_cache_read`, `ctx_cache_create` — per-call occupancy |
| Session spend      | counter tracks `spend_total`, `spend_input`, `spend_cache_read`, `spend_cache_create` — cumulative |
| User prompts       | instant markers on the **turns** thread                                        |

Timeline rules (deterministic — same log in, same trace out):

- `ts` = microseconds since the earliest timestamp in the file; the original epoch time and
  ISO base timestamp are kept in `metadata`.
- A turn/tool slice runs from its message timestamp to the next event's timestamp (for a
  tool call, to its result's arrival). The final slice of a stream has nothing to bound it,
  so it gets a 1 s estimate flagged `"dur_estimated": true` in its args.
- Counters are emitted at every assistant message. `ctx_*` is **per-call occupancy**: the
  `input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens` that *that single
  call* reported (plus their total) — the model's context on that turn. `spend_*` is the
  **cumulative** sum of the same fields across the session — the billing trajectory.

## Approximation honesty

The context lane is an **approximation**. `ctx_*` counters report the usage fields each API
call claimed; they do not reproduce the provider-side context window (cache lifetime,
truncation, and system-prompt composition are not observable from the log). `spend_*`
counters re-bill cached tokens every call, so they grow monotonically and are **not** context
size. Slice durations that had no bounding event are estimates. This note ships inside every
trace under `metadata.approximation_note`, and the caveats above live in `metadata`.

## Privacy: local by construction

The converter is a file-to-file transform with **zero network calls**. `--open` merely opens
a URL in your browser; the trace itself travels by drag-and-drop and is processed in the
browser locally. Nothing is uploaded, no account exists, no telemetry exists.

## How this differs from adjacent tools

| Adjacent tool                     | Difference                                                                    |
| --------------------------------- | ----------------------------------------------------------------------------- |
| LangSmith / LangFuse              | Web SaaS with their own UIs and cloud storage. Here: local file → a standard format; viewer cost = 0. |
| ccusage / tokscale                | Aggregates (how much was spent). Here: a time-space map (what ate context, when). |
| Perfetto itself                   | Provides the viewer and format but has no agent-domain adapter — that gap is exactly this project. |
| Perfetto MCP servers              | The reverse direction (LLMs analyzing Perfetto traces). Here a human looks.    |
| One-off "LLM trace JSON" scripts  | A maintained adapter with golden-fixture tests against schema drift, plus lane semantics. |

## Limits & roadmap

- v0.1 supported Claude Code JSONL only. v0.2 introduces the vendor-neutral
  **Agent Trace IR** ([docs/agent-trace-ir.md](docs/agent-trace-ir.md)): new agent
  logs enter as adapters into the same IR, so the same viewer, lanes, and
  PerfettoSQL work across harnesses. The **Codex CLI** rollout adapter landed in
  v0.2.1 (cross-validated against the yield-audit Codex parser on shared
  fixtures; codex `cached_input_tokens` maps to `cache_read`, codex reports no
  cache-creation counter, and codex shell tools render as `Bash` with list
  commands joined). Subagent/async slices are also planned.
- Perfetto's JSON format is the legacy entry point; a proto-format exporter is on the roadmap.
- Multiple `sessionId`s in one file become multiple processes; per-subagent processes and
  context-composition lanes (system prompt / files / MCP schemas) are future work.

## Development

```bash
python3 -m venv /tmp/a2p-venv && . /tmp/a2p-venv/bin/activate
pip install -e .
pip install pytest   # test-only dependency; the package itself needs nothing
pytest
```

Layout: `src/agent2perfetto/` — a three-stage pipeline (`parser` adapter →
`ir` Agent Trace IR → `trace` Perfetto emitter → `cli`), synthetic fixtures and
a frozen golden trace under `tests/fixtures/`, the example session and its
generated trace under `examples/`, and the IR schema at
[docs/agent-trace-ir.md](docs/agent-trace-ir.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
