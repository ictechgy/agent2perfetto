"""Codex CLI adapter tests: rollout JSONL → Agent Trace IR → trace.

The fixture (tests/fixtures/codex_session.jsonl) uses the same record shapes
as yield-audit's codex fixture builders — the two projects cross-check their
parsers on this shared format grounding.
"""

import json
from pathlib import Path

import pytest

from agent2perfetto.codex import parse_codex_file, parse_codex_lines
from agent2perfetto.ir import from_json, to_json
from agent2perfetto.parser import StrictParseError
from agent2perfetto.trace import build_trace

FIXTURES = Path(__file__).parent / "fixtures"
FIXED_TS = "2026-09-05T00:00:00Z"


def build_codex():
    return build_trace(parse_codex_file(FIXTURES / "codex_session.jsonl"), generated_at=FIXED_TS)


def test_codex_session_parses_into_ir():
    trace_ir = parse_codex_file(FIXTURES / "codex_session.jsonl")
    assert trace_ir.vendor == "codex"
    assert trace_ir.source == "codex-rollout-jsonl"
    assert [s.session_id for s in trace_ir.sessions] == ["cccccccc-0000-4000-8000-000000000001"]
    sess = trace_ir.sessions[0]
    assert sess.cwd == "/tmp/demo"
    turns = sess.turns
    assert [t.kind for t in turns] == [
        "user_turn", "model_call", "tool_turn", "model_call", "tool_turn", "model_call",
    ]
    assert all(t.model == "gpt-5.1-codex" for t in turns if t.kind == "model_call")
    assert turns[0].user_prompt == "List the files, then run the tests."
    # shell tool normalized to Bash, list command joined
    assert [(c.id, c.name, c.input) for c in turns[1].tool_calls] == [
        ("call_001", "Bash", {"command": "ls -la"})
    ]
    assert [(c.id, c.name, c.input) for c in turns[3].tool_calls] == [
        ("call_002", "Bash", {"command": "python -m pytest -q"})
    ]
    # usage merged from the following token_count events (cached -> cache_read)
    assert turns[1].usage == {
        "input_tokens": 100, "output_tokens": 90,
        "cache_read_input_tokens": 200, "cache_creation_input_tokens": 0,
    }
    assert turns[5].usage["input_tokens"] == 20
    # result turns: envelope output text + exit-code error flag
    assert turns[2].tool_results[0].content == "file_a.py\nfile_b.py"
    assert turns[2].tool_results[0].is_error is False
    assert turns[4].tool_results[0].is_error is True
    # token_count / session_meta / turn_context records are not turns
    assert len(turns) == 6


def test_codex_counter_values_match_hand_computed_sums():
    trace = build_codex()
    counters = [e for e in trace["traceEvents"] if e["ph"] == "C"]
    # fixture last_token_usage per call: (input, cached)
    #   call 1 @1s: (100, 200)   call 2 @3s: (30, 400)   call 3 @6.5s: (20, 500)
    # counters fire at the model_call message timestamps (usage merge)
    expected = [
        (
            1_000_000,
            {
                "ctx_total": 300, "ctx_input": 100, "ctx_cache_read": 200, "ctx_cache_create": 0,
                "spend_total": 300, "spend_input": 100, "spend_cache_read": 200, "spend_cache_create": 0,
            },
        ),
        (
            3_000_000,
            {
                "ctx_total": 430, "ctx_input": 30, "ctx_cache_read": 400, "ctx_cache_create": 0,
                "spend_total": 730, "spend_input": 130, "spend_cache_read": 600, "spend_cache_create": 0,
            },
        ),
        (
            6_500_000,
            {
                "ctx_total": 520, "ctx_input": 20, "ctx_cache_read": 500, "ctx_cache_create": 0,
                "spend_total": 1250, "spend_input": 150, "spend_cache_read": 1100, "spend_cache_create": 0,
            },
        ),
    ]
    assert [(c["ts"], c["args"]) for c in counters] == expected


def test_codex_tools_flows_and_metadata():
    trace = build_codex()
    events = trace["traceEvents"]
    md = trace["metadata"]
    assert md["source"] == "codex-rollout-jsonl"
    assert md["models_observed"] == ["gpt-5.1-codex"]
    process_names = [
        e["args"]["name"] for e in events if e["ph"] == "M" and e["name"] == "process_name"
    ]
    assert process_names == ["agent session cccccccc-0000-4000-8000-000000000001"]
    tool_calls = [e for e in events if e["ph"] == "X" and e["cat"] == "tool_call"]
    assert [e["name"] for e in tool_calls] == ["Bash", "Bash"]
    # call at 1s ends when its result arrives at 2s; second runs 3s -> 5s
    assert (tool_calls[0]["ts"], tool_calls[0]["dur"]) == (1_000_000, 1_000_000)
    assert (tool_calls[1]["ts"], tool_calls[1]["dur"]) == (3_000_000, 2_000_000)
    starts = [e for e in events if e["ph"] == "s"]
    finishes = [e for e in events if e["ph"] == "f"]
    assert [e["id"] for e in starts] == [e["id"] for e in finishes] == [1, 2]
    user_markers = [e for e in events if e["ph"] == "i"]
    assert len(user_markers) == 1 and user_markers[0]["ts"] == 0
    # error result keeps its flag; success result does not
    results = [e for e in events if e["ph"] == "X" and e["cat"] == "tool_result"]
    assert [e["args"]["is_error"] for e in results] == [False, True]


def test_codex_ir_json_roundtrip_preserves_output():
    trace_ir = parse_codex_file(FIXTURES / "codex_session.jsonl")
    rebuilt = from_json(to_json(trace_ir))
    assert build_trace(rebuilt, generated_at=FIXED_TS) == build_codex()


def test_codex_tolerance_and_strict():
    bad = [
        "{not json",
        "[1, 2]",  # non-object
        json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user"}}),  # no ts
        json.dumps({"timestamp": "2026-09-05T12:00:00.000Z", "type": "mystery", "payload": {}}),
        json.dumps({
            "timestamp": "2026-09-05T12:00:00.000Z", "type": "session_meta",
            "payload": {"id": "s1", "cwd": "/w"}, "weather": "sunny",
        }),
        json.dumps({
            "timestamp": "2026-09-05T12:00:01.000Z", "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        }),
    ]
    trace_ir = parse_codex_lines(bad)
    st = trace_ir.stats
    assert st.total_lines == 6
    assert st.malformed_lines == 2
    assert st.skipped_records == 2  # no-timestamp + unknown type
    assert st.records_with_unknown_fields == 1
    assert [t.user_prompt for t in trace_ir.events()] == ["hi"]

    with pytest.raises(StrictParseError):
        parse_codex_lines(bad, strict=True)


def test_codex_deep_line_counted_not_fatal():
    deep_line = "[" * 100_000 + "]" * 100_000
    trace_ir = parse_codex_lines([deep_line])
    assert trace_ir.stats.malformed_lines == 1


def test_codex_usage_merge_rules():
    def msg(ts, role="assistant", text="t"):
        return json.dumps({
            "timestamp": ts, "type": "response_item",
            "payload": {"type": "message", "role": role,
                        "content": [{"type": "output_text" if role == "assistant" else "input_text", "text": text}]},
        })

    def tc(ts, usage):
        return json.dumps({
            "timestamp": ts, "type": "event_msg",
            "payload": {"type": "token_count", "info": {"last_token_usage": usage}},
        })

    lines = [
        tc("2026-09-05T12:00:00.000Z", {"input_tokens": 9}),  # before any model call: ignored
        msg("2026-09-05T12:00:01.000Z"),
        tc("2026-09-05T12:00:02.000Z", {"input_tokens": 10, "cached_input_tokens": 5, "output_tokens": 1}),
        tc("2026-09-05T12:00:03.000Z", {"input_tokens": 99}),  # duplicate for the same call: never overwrites
        msg("2026-09-05T12:00:04.000Z"),
        tc("2026-09-05T12:00:05.000Z", {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}),  # empty: ignored
    ]
    trace_ir = parse_codex_lines(lines)
    calls = [t for t in trace_ir.events() if t.kind == "model_call"]
    assert len(calls) == 2
    assert calls[0].usage["input_tokens"] == 10
    assert calls[0].usage["cache_read_input_tokens"] == 5
    assert calls[1].usage["input_tokens"] == 0


def test_codex_function_call_without_preceding_message():
    lines = [
        json.dumps({
            "timestamp": "2026-09-05T12:00:00.000Z", "type": "response_item",
            "payload": {"type": "function_call", "call_id": "c1", "name": "shell",
                        "arguments": json.dumps({"command": "ls"})},
        }),
        json.dumps({
            "timestamp": "2026-09-05T12:00:01.000Z", "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "c1",
                        "output": json.dumps({"output": "ok", "metadata": {"exit_code": 0}})},
        }),
    ]
    trace_ir = parse_codex_lines(lines)
    events = trace_ir.events()
    assert [t.kind for t in events] == ["model_call", "tool_turn"]
    assert events[0].tool_calls[0].name == "Bash"
    trace = build_trace(trace_ir)
    assert [e["name"] for e in trace["traceEvents"] if e.get("cat") == "tool_call"] == ["Bash"]


def test_codex_apply_patch_name_kept_and_reasoning_skipped():
    lines = [
        json.dumps({
            "timestamp": "2026-09-05T12:00:00.000Z", "type": "response_item",
            "payload": {"type": "function_call", "call_id": "p1", "name": "apply_patch",
                        "arguments": json.dumps({"input": "*** Begin Patch\n*** Update File: a.py\n@@\n-x\n+y\n"})},
        }),
        json.dumps({
            "timestamp": "2026-09-05T12:00:00.500Z", "type": "response_item",
            "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "hmm"}]},
        }),
    ]
    trace_ir = parse_codex_lines(lines)
    events = trace_ir.events()
    assert len(events) == 1  # reasoning is not a timeline event
    assert events[0].tool_calls[0].name == "apply_patch"
    assert events[0].tool_calls[0].input["input"].startswith("*** Begin Patch")


def test_detect_format_and_cli_roundtrip(tmp_path, capsys):
    from agent2perfetto.cli import detect_format, main

    assert detect_format(FIXTURES / "codex_session.jsonl") == "codex"
    assert detect_format(FIXTURES / "valid_session.jsonl") == "claude"
    assert detect_format(FIXTURES / "empty.jsonl") == "claude"  # fallback

    out = tmp_path / "codex.json"
    rc = main([str(FIXTURES / "codex_session.jsonl"), "-o", str(out)])
    assert rc == 0
    trace = json.loads(out.read_text(encoding="utf-8"))
    assert trace["metadata"]["source"] == "codex-rollout-jsonl"
    assert "18 trace event(s)" in capsys.readouterr().out  # 3 M + 6 X + 3 C + 2 s + 2 f + 1 i + ... (see trace)
