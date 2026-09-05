# Agent Trace IR — schema v1 (draft proposal)

The vendor-neutral intermediate representation at the center of agent2perfetto's
conversion pipeline:

```
<vendor session log> ──adapter──▶ Agent Trace IR ──emitter──▶ Perfetto trace JSON
                                        │
                                        └── to_json/from_json ──▶ the schema on this page
```

Only adapters are vendor-specific. The IR and the emitters are not: a new agent
log format is a new adapter, and a new output (OTel spans, a local warehouse) is
a new emitter. The IR is a data contract, not a product.

- **Version**: `agent_trace_ir: 1` (this document). Consumers must reject
  unknown versions; producers must not repurpose existing fields.
- **Status**: implemented in `src/agent2perfetto/ir.py`
  (`to_json` / `from_json` are the normative serializer); consumed by the
  Perfetto emitter in `trace.py`.
- **Design rule**: the IR carries what the harnesses actually report, with
  source order preserved. It does not interpret (no derived "context size",
  no guessed durations) — interpretation belongs to emitters.

## Structure

```json
{
  "agent_trace_ir": 1,
  "vendor": "claude-code",
  "source": "claude-code-jsonl",
  "stats": {
    "total_lines": 0,
    "malformed_lines": 0,
    "skipped_records": 0,
    "records_with_unknown_fields": 0
  },
  "sessions": [
    {
      "session_id": "sess-…",
      "cwd": "/path/to/workdir",
      "turns": [ { … Turn … } ]
    }
  ]
}
```

| Field | Meaning |
| --- | --- |
| `agent_trace_ir` | Schema version, currently `1`. |
| `vendor` / `source` | Harness identity and the source log format, as chosen by the adapter. |
| `stats` | What the adapter had to skip or tolerate — observability of the adapter itself. |
| `sessions` | One per `session_id`, sorted by id. |

### Turn

One timeline event as the harness reported it.

```json
{
  "seq": 3,
  "timestamp": "2026-09-05T12:00:03.000Z",
  "epoch_us": 1785993603000000,
  "session_id": "sess-…",
  "kind": "model_call",
  "vendor_type": "assistant",
  "uuid": "u-0004",
  "parent_uuid": "u-0003",
  "model": "claude-…",
  "usage": {
    "input_tokens": 30,
    "output_tokens": 120,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 400
  },
  "text": "…assistant text blocks…",
  "user_prompt": "…the human prompt…",
  "tool_calls": [ { "id": "toolu_…", "name": "Bash", "input": { … } } ],
  "tool_results": [ { "tool_use_id": "toolu_…", "content": …, "is_error": false } ]
}
```

| Field | Meaning |
| --- | --- |
| `seq` | Position in the source file (0-based across the whole log). Timeline determinism depends on it surviving normalization. |
| `timestamp` / `epoch_us` | The harness-reported time, raw and as integer microseconds since the epoch. |
| `kind` | Neutral vocabulary: `model_call`, `user_turn`, `tool_turn`, `system`, `summary`. |
| `vendor_type` | The adapter's original type string, kept for debugging round-trips. |
| `usage` | The four usage counters that call reported; `0` where the vendor omitted a field. Absent for non-model turns. |
| `tool_calls` / `tool_results` | Calls issued by this turn; results delivered by this turn. `content` is any JSON value. |

A turn that both prompts and returns results keeps whichever fields the vendor
gave; `kind` records the dominant role (`tool_turn` when results ride along).

## Guarantees

- **Lossless ordering**: flattening all sessions' turns and sorting by
  `(epoch_us, seq)` reproduces the source timeline.
- **Round-trip**: `from_json(to_json(ir))` renders byte-identical emitter
  output (frozen by `tests/test_ir.py`).
- **Tolerance lives in adapters**: an adapter skips malformed records and
  reports the counts in `stats`; the IR itself is always well-formed.

## Non-goals

- No derived semantics: context occupancy, spend trajectories, and durations
  are emitter concerns (see the Perfetto emitter's approximation note).
- No provider-side truth: the IR records client-reported usage only.
