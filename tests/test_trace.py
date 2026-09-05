import json
from pathlib import Path

from agent2perfetto.parser import parse_file
from agent2perfetto.trace import build_trace

FIXTURES = Path(__file__).parent / "fixtures"


def build_valid():
    return build_trace(parse_file(FIXTURES / "valid_session.jsonl"))


def test_top_level_shape():
    trace = build_valid()
    assert set(trace) == {"traceEvents", "displayTimeUnit", "metadata"}
    assert trace["displayTimeUnit"] == "ms"
    md = trace["metadata"]
    assert md["source"] == "claude-code-jsonl"
    assert md["converter_version"] == "0.1.0"
    assert md["models_observed"] == ["claude-fixture-opus", "claude-fixture-sonnet"]
    assert md["generated_at"]
    assert "approximate" in md["approximation_note"]
    assert md["base_timestamp"] == "2026-09-05T12:00:00.000000Z"
    assert md["trace_clock"].startswith("microseconds")


def test_event_kinds_present():
    trace = build_valid()
    phases = {e["ph"] for e in trace["traceEvents"]}
    assert {"M", "X", "C", "i", "s", "f"} <= phases
    meta_names = {e["name"] for e in trace["traceEvents"] if e["ph"] == "M"}
    assert {"process_name", "thread_name"} <= meta_names
    turns_threads = {
        e["args"]["name"]
        for e in trace["traceEvents"]
        if e["ph"] == "M" and e["name"] == "thread_name"
    }
    assert {"turns", "tools"} <= turns_threads


def test_timestamps_monotonic_and_durations_nonnegative():
    trace = build_valid()
    events = trace["traceEvents"]
    ts_list = [e["ts"] for e in events]
    assert ts_list == sorted(ts_list)
    for e in events:
        if e["ph"] == "X":
            assert e["dur"] >= 0
        assert isinstance(e["ts"], int)


def test_counter_values_match_hand_computed_sums():
    trace = build_valid()
    counters = [e for e in trace["traceEvents"] if e["ph"] == "C"]
    # fixture usage per call: (input, cache_read, cache_create)
    #   call 1 @1s: (100, 200, 50)   call 2 @3s: (30, 400, 0)   call 3 @6.5s: (20, 500, 10)
    expected = [
        (
            1_000_000,
            {
                "ctx_total": 350, "ctx_input": 100, "ctx_cache_read": 200, "ctx_cache_create": 50,
                "spend_total": 350, "spend_input": 100, "spend_cache_read": 200, "spend_cache_create": 50,
            },
        ),
        (
            3_000_000,
            {
                # occupancy is THIS call only; spend is the running sum and
                # its cache_read (600) re-bills tokens ctx_cache_read (400) already counts
                "ctx_total": 430, "ctx_input": 30, "ctx_cache_read": 400, "ctx_cache_create": 0,
                "spend_total": 780, "spend_input": 130, "spend_cache_read": 600, "spend_cache_create": 50,
            },
        ),
        (
            6_500_000,
            {
                "ctx_total": 530, "ctx_input": 20, "ctx_cache_read": 500, "ctx_cache_create": 10,
                "spend_total": 1310, "spend_input": 150, "spend_cache_read": 1100, "spend_cache_create": 60,
            },
        ),
    ]
    assert [(c["ts"], c["args"]) for c in counters] == expected


def test_slices_and_flows_pair_by_tool_use_id():
    trace = build_valid()
    events = trace["traceEvents"]
    tool_calls = [e for e in events if e["ph"] == "X" and e["cat"] == "tool_call"]
    results = [e for e in events if e["ph"] == "X" and e["cat"] == "tool_result"]
    assert [e["name"] for e in tool_calls] == ["Bash", "Bash"]
    assert [e["args"]["tool_use_id"] for e in tool_calls] == ["toolu_001", "toolu_002"]
    assert [e["name"] for e in results] == ["result Bash", "result Bash"]
    # call at 1s ends when its result arrives at 2s; second call runs 3s -> 5s
    assert (tool_calls[0]["ts"], tool_calls[0]["dur"]) == (1_000_000, 1_000_000)
    assert (tool_calls[1]["ts"], tool_calls[1]["dur"]) == (3_000_000, 2_000_000)
    starts = [e for e in events if e["ph"] == "s"]
    finishes = [e for e in events if e["ph"] == "f"]
    assert [e["id"] for e in starts] == [e["id"] for e in finishes] == [1, 2]
    assert starts[0]["ts"] == tool_calls[0]["ts"]
    assert finishes[0]["ts"] == results[0]["ts"]


def test_last_turn_duration_is_estimated():
    trace = build_valid()
    turns = [e for e in trace["traceEvents"] if e.get("cat") == "turn"]
    assert turns[-1]["args"]["dur_estimated"] is True
    assert turns[-1]["dur"] > 0
    assert "dur_estimated" not in turns[0]["args"]


def test_empty_session_builds_valid_empty_trace():
    trace = build_trace(parse_file(FIXTURES / "empty.jsonl"))
    assert trace["traceEvents"] == []
    assert trace["displayTimeUnit"] == "ms"
    assert trace["metadata"]["models_observed"] == []
    assert "base_timestamp" not in trace["metadata"]


def test_unknown_session_gets_process_named():
    trace = build_valid()
    names = [
        e["args"]["name"] for e in trace["traceEvents"] if e["ph"] == "M" and e["name"] == "process_name"
    ]
    assert names == ["agent session sess-fixture-0001"]


def test_trace_events_are_json_serializable():
    trace = build_valid()
    json.dumps(trace)


def test_preview_is_bounded_for_huge_payloads():
    from agent2perfetto.trace import _preview

    huge = "x" * (5 * 1024 * 1024)
    assert len(_preview(huge)) < 600
    assert len(_preview({"blob": huge, "n": 1})) < 2000
    assert len(_preview([{"k": huge}] * 100)) < 2000
    assert _preview({"k": "v"}) == '{"k": "v"}'


def test_tool_use_input_is_bounded_in_trace_args():
    from agent2perfetto.parser import parse_lines
    from agent2perfetto.trace import _shallow_truncate

    huge = "y" * (5 * 1024 * 1024)
    line = json.dumps(
        {
            "type": "assistant",
            "timestamp": "2026-09-05T12:00:00.000Z",
            "uuid": "u-huge",
            "sessionId": "sess-huge",
            "message": {
                "role": "assistant",
                "model": "m",
                "content": [
                    {"type": "tool_use", "id": "toolu_big", "name": "Write", "input": {"content": huge}}
                ],
                "usage": {"input_tokens": 1},
            },
        }
    )
    session = parse_lines([line])
    assert len(session.records) == 1
    trace = build_trace(session)
    tool_events = [e for e in trace["traceEvents"] if e.get("cat") == "tool_call"]
    assert len(tool_events) == 1
    args_json = json.dumps(tool_events[0]["args"], ensure_ascii=False)
    assert len(args_json) < 10_000  # a 10MB input must not become a 10MB trace
    assert tool_events[0]["args"]["input"] == _shallow_truncate({"content": huge})
