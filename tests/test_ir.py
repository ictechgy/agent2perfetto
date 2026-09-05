"""Agent Trace IR tests: schema stability + the bypass regression contract.

The v0.2 refactor routes conversion through the IR (adapter → IR → emitter).
The output mapping did not change, and these tests prove it: the IR path must
produce byte-identical traceEvents to the frozen golden, the direct session
path, and the IR JSON round-trip.
"""

import json
from pathlib import Path

from agent2perfetto.ir import KIND_MODEL_CALL, AgentTrace, from_claude_session, from_json, to_json
from agent2perfetto.parser import parse_file, parse_lines
from agent2perfetto.trace import build_trace, build_trace_from_ir

FIXTURES = Path(__file__).parent / "fixtures"
FIXED_TS = "2026-09-05T00:00:00Z"


def valid_ir():
    return from_claude_session(parse_file(FIXTURES / "valid_session.jsonl"))


def test_ir_hierarchical_shape():
    trace_ir = valid_ir()
    assert trace_ir.vendor == "claude-code"
    assert trace_ir.source == "claude-code-jsonl"
    assert trace_ir.ir_version == 1
    assert [s.session_id for s in trace_ir.sessions] == ["sess-fixture-0001"]
    sess = trace_ir.sessions[0]
    assert sess.cwd == "/tmp/demo"
    kinds = [t.kind for t in sess.turns]
    assert kinds == ["user_turn", "model_call", "tool_turn", "model_call", "tool_turn", "model_call"]
    assert [t.vendor_type for t in sess.turns] == ["user", "assistant", "user", "assistant", "user", "assistant"]
    call = sess.turns[1].tool_calls[0]
    assert (call.id, call.name, call.input) == ("toolu_001", "Bash", {"command": "ls -la"})
    assert sess.turns[2].tool_results[0].tool_use_id == "toolu_001"
    assert sess.turns[1].usage["input_tokens"] == 100


def test_events_flatten_in_global_order():
    trace_ir = valid_ir()
    events = trace_ir.events()
    assert [(t.epoch_us, t.seq) for t in events] == sorted((t.epoch_us, t.seq) for t in events)


def test_bypass_ir_path_matches_golden():
    """The 기획서 v0.2.0 contract: routing through the IR leaves the frozen
    Perfetto output untouched."""
    golden = json.loads((FIXTURES / "golden_trace_events.json").read_text(encoding="utf-8"))
    trace = build_trace_from_ir(valid_ir(), generated_at=FIXED_TS)
    assert trace["traceEvents"] == golden


def test_session_path_and_ir_path_are_identical():
    session = parse_file(FIXTURES / "valid_session.jsonl")
    via_session = build_trace(session, generated_at=FIXED_TS)
    via_ir = build_trace_from_ir(from_claude_session(session), generated_at=FIXED_TS)
    assert via_session == via_ir


def test_ir_json_roundtrip_preserves_output():
    trace_ir = valid_ir()
    rebuilt = from_json(to_json(trace_ir))
    assert build_trace_from_ir(rebuilt, generated_at=FIXED_TS) == build_trace_from_ir(
        trace_ir, generated_at=FIXED_TS
    )
    assert rebuilt.sessions[0].cwd == "/tmp/demo"
    assert rebuilt.stats.total_lines == 6
    assert rebuilt.events()[0].user_prompt == "List the files, then run the tests."


def test_ir_json_schema_keys():
    obj = to_json(valid_ir())
    assert set(obj) == {"agent_trace_ir", "vendor", "source", "stats", "sessions"}
    session = obj["sessions"][0]
    assert set(session) == {"session_id", "cwd", "turns"}
    turn = session["turns"][1]
    assert set(turn) == {
        "seq",
        "timestamp",
        "epoch_us",
        "session_id",
        "kind",
        "vendor_type",
        "uuid",
        "parent_uuid",
        "model",
        "usage",
        "text",
        "user_prompt",
        "tool_calls",
        "tool_results",
    }
    assert turn["kind"] == KIND_MODEL_CALL
    assert turn["tool_calls"][0] == {"id": "toolu_001", "name": "Bash", "input": {"command": "ls -la"}}


def test_ir_json_rejects_unknown_version():
    import pytest

    with pytest.raises(ValueError):
        from_json({"agent_trace_ir": 99, "sessions": []})


def test_multi_session_ir_path_matches_direct_path():
    """pids, counter ordering, and duration bounds are global; the IR grouping
    must not change any of them."""
    lines = []
    for sid, ts in (("sess-b", "2026-09-05T12:00:00.000Z"), ("sess-a", "2026-09-05T12:00:01.000Z")):
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": ts,
                    "uuid": f"u-{sid}",
                    "sessionId": sid,
                    "message": {
                        "role": "assistant",
                        "model": "m",
                        "content": [],
                        "usage": {"input_tokens": 5, "cache_read_input_tokens": 7},
                    },
                }
            )
        )
    session = parse_lines(lines)
    assert build_trace(session, generated_at=FIXED_TS) == build_trace_from_ir(
        from_claude_session(session), generated_at=FIXED_TS
    )


def test_empty_session_ir_roundtrip():
    trace_ir = from_claude_session(parse_file(FIXTURES / "empty.jsonl"))
    assert trace_ir.sessions == []
    assert build_trace_from_ir(trace_ir)["traceEvents"] == []


def test_ir_from_json_roundtrip_is_json_serializable():
    obj = to_json(valid_ir())
    json.dumps(obj)
    assert isinstance(from_json(obj), AgentTrace)
