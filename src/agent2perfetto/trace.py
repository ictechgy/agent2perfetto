"""Build a Perfetto-compatible Chrome Trace Event JSON dict from an Agent Trace IR.

This is the Perfetto emitter of the three-stage pipeline
(adapter → IR (ir.py) → emitter): it consumes the vendor-neutral IR, so any
adapter (Claude Code today, other agent logs later) renders through the
same mapping.
"""

from __future__ import annotations

import bisect
import json
from datetime import datetime, timezone

from . import __version__, ir as ir_module
from .ir import KIND_MODEL_CALL, AgentTrace
from .lanes import APPROXIMATION_NOTE, OCC_COUNTERS, SPEND_COUNTERS, compute_context_lanes

TURNS_TID = 1
TOOLS_TID = 2
TURN_THREAD_NAME = "turns"
TOOL_THREAD_NAME = "tools"
CONTENT_PREVIEW_CHARS = 500
# Fallback duration for a slice with no later event to bound it (the final
# turn of a stream). Marked as estimated in the slice args.
ESTIMATED_TURN_DUR_US = 1_000_000

_COUNTER_ORDER = OCC_COUNTERS + SPEND_COUNTERS
_RANK = {
    "counter": 0,
    "turn": 1,
    "tool_use": 2,
    "tool_result": 3,
    "user_prompt": 4,
    "flow_start": 5,
    "flow_end": 6,
}


def _iso_z(epoch_us: int) -> str:
    dt = datetime.fromtimestamp(epoch_us / 1_000_000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _now_iso_z() -> str:
    return _iso_z(int(datetime.now(tz=timezone.utc).timestamp() * 1_000_000))


def _shallow_truncate(value, limit: int = CONTENT_PREVIEW_CHARS):
    """Bound a parsed JSON value before serialization: slice long strings and
    cap collection width, so a multi-MB tool_result never gets fully
    re-serialized just to preview 500 characters."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + " ..."
    if isinstance(value, list):
        trimmed = [_shallow_truncate(v, limit) for v in value[:8]]
        if len(value) > 8:
            trimmed.append("... (%d more items)" % (len(value) - 8))
        return trimmed
    if isinstance(value, dict):
        out = {str(k): _shallow_truncate(v, limit) for k, v in list(value.items())[:8]}
        if len(value) > 8:
            out["..."] = "(%d more keys)" % (len(value) - 8)
        return out
    return value


def _preview(content) -> str:
    text = json.dumps(_shallow_truncate(content), ensure_ascii=False, sort_keys=True)
    if len(text) > CONTENT_PREVIEW_CHARS:
        return text[:CONTENT_PREVIEW_CHARS] + " ... (truncated)"
    return text


def _metadata_event(pid: int, tid: int, name: str, value: str) -> dict:
    return {"ph": "M", "name": name, "pid": pid, "tid": tid, "ts": 0, "args": {"name": value}}


def _pair_tool_calls(turns):
    """Pair tool_call blocks with the first later tool_result sharing their id.

    Returns (pairs, unmatched_calls) where pairs maps tool_use_id to
    (call_turn, IRToolCall, result_turn, IRToolResult) and unmatched_calls is a
    list of (turn, IRToolCall) with no later result (orphan results are
    still rendered as slices by the caller).
    """
    calls = [(t, c) for t in turns for c in t.tool_calls]
    results_by_id: dict = {}
    for t in turns:
        for tr in t.tool_results:
            results_by_id.setdefault(tr.tool_use_id, []).append(t)
    pairs = {}
    matched = set()
    for call_turn, call in calls:
        for result_turn in results_by_id.get(call.id, ()):
            if result_turn.seq > call_turn.seq:
                result = next(t for t in result_turn.tool_results if t.tool_use_id == call.id)
                pairs[call.id] = (call_turn, call, result_turn, result)
                matched.add(call.id)
                break
    unmatched = [(t, c) for t, c in calls if c.id not in matched]
    return pairs, unmatched


def _metadata(source: str, stats, models: list, base_us, generated_at) -> dict:
    md = {
        "source": source,
        "converter": f"agent2perfetto {__version__}",
        "converter_version": __version__,
        "models_observed": models,
        "generated_at": generated_at or _now_iso_z(),
        "approximation_note": APPROXIMATION_NOTE
        + " Slices whose duration has no later event to bound it are estimates.",
        "warnings": {
            "malformed_lines": stats.malformed_lines,
            "skipped_records": stats.skipped_records,
            "records_with_unknown_fields": stats.records_with_unknown_fields,
        },
    }
    if base_us is not None:
        md["base_timestamp"] = _iso_z(base_us)
        md["base_timestamp_epoch_us"] = base_us
        md["trace_clock"] = "microseconds since base_timestamp"
    return md


def build_trace(source, *, generated_at: str | None = None) -> dict:
    """Return {"traceEvents": [...], "displayTimeUnit": "ms", "metadata": {...}}.

    Accepts an AgentTrace IR or a parsed Claude Code session (parser.Session);
    the session is normalized through the IR either way. Deterministic: the
    same input yields byte-identical traceEvents (only metadata.generated_at
    varies between runs).
    """
    trace_ir = source if isinstance(source, AgentTrace) else ir_module.from_claude_session(source)
    return build_trace_from_ir(trace_ir, generated_at=generated_at)


def build_trace_from_ir(trace_ir: AgentTrace, *, generated_at: str | None = None) -> dict:
    """Render the Agent Trace IR as Perfetto/Chrome Trace Event JSON."""
    turns = trace_ir.events()
    models = sorted({t.model for t in turns if t.model})
    if not turns:
        return {
            "traceEvents": [],
            "displayTimeUnit": "ms",
            "metadata": _metadata(trace_ir.source, trace_ir.stats, models, None, generated_at),
        }

    base_us = min(t.epoch_us for t in turns)
    times = sorted({t.epoch_us for t in turns})
    pids = {sid: pid for pid, sid in enumerate(sorted({t.session_id for t in turns}), start=1)}

    meta = []
    for sid in sorted(pids):
        pid = pids[sid]
        meta.append(_metadata_event(pid, 0, "process_name", f"agent session {sid}"))
        meta.append(_metadata_event(pid, TURNS_TID, "thread_name", TURN_THREAD_NAME))
        meta.append(_metadata_event(pid, TOOLS_TID, "thread_name", TOOL_THREAD_NAME))

    pending = []  # (ts, rank, insertion_order, event)

    def emit(ts: int, rank: int, event: dict) -> None:
        pending.append((ts, rank, len(pending), event))

    def next_us(epoch_us: int):
        idx = bisect.bisect_right(times, epoch_us)
        return times[idx] if idx < len(times) else None

    pairs, unmatched_calls = _pair_tool_calls(turns)
    flow_ids = {tool_use_id: i for i, tool_use_id in enumerate(sorted(pairs), start=1)}

    for t in turns:
        ts = t.epoch_us - base_us
        nxt = next_us(t.epoch_us)
        pid = pids[t.session_id]
        if t.kind == KIND_MODEL_CALL:
            dur_estimated = nxt is None
            turn_dur = (nxt - t.epoch_us) if nxt is not None else ESTIMATED_TURN_DUR_US
            args = {"model": t.model, "uuid": t.uuid, "usage": dict(t.usage)}
            if dur_estimated:
                args["dur_estimated"] = True
            emit(
                ts,
                _RANK["turn"],
                {
                    "ph": "X",
                    "cat": "turn",
                    "name": "assistant turn",
                    "pid": pid,
                    "tid": TURNS_TID,
                    "ts": ts,
                    "dur": max(0, turn_dur),
                    "args": args,
                },
            )
            for call in t.tool_calls:
                pair = pairs.get(call.id)
                if pair is not None:
                    tool_dur = pair[2].epoch_us - t.epoch_us
                else:
                    tool_dur = (nxt - t.epoch_us) if nxt is not None else 0
                emit(
                    ts,
                    _RANK["tool_use"],
                    {
                        "ph": "X",
                        "cat": "tool_call",
                        "name": call.name,
                        "pid": pid,
                        "tid": TOOLS_TID,
                        "ts": ts,
                        "dur": max(0, tool_dur),
                        "args": {
                            "tool_use_id": call.id,
                            "input": _shallow_truncate(call.input),
                            "usage": dict(t.usage),
                        },
                    },
                )
                if pair is not None:
                    fid = flow_ids[call.id]
                    emit(
                        ts,
                        _RANK["flow_start"],
                        {
                            "ph": "s",
                            "cat": "tool_flow",
                            "name": "tool call",
                            "pid": pid,
                            "tid": TOOLS_TID,
                            "ts": ts,
                            "id": fid,
                        },
                    )
                    emit(
                        pair[2].epoch_us - base_us,
                        _RANK["flow_end"],
                        {
                            "ph": "f",
                            "cat": "tool_flow",
                            "name": "tool call",
                            "pid": pid,
                            "tid": TOOLS_TID,
                            "ts": pair[2].epoch_us - base_us,
                            "id": fid,
                            "bp": "e",
                        },
                    )
        elif t.kind == ir_module.KIND_TOOL_TURN:
            for tr in t.tool_results:
                use = pairs.get(tr.tool_use_id)
                name = f"result {use[1].name}" if use else "tool result"
                dur = (nxt - t.epoch_us) if nxt is not None else 0
                emit(
                    ts,
                    _RANK["tool_result"],
                    {
                        "ph": "X",
                        "cat": "tool_result",
                        "name": name,
                        "pid": pid,
                        "tid": TOOLS_TID,
                        "ts": ts,
                        "dur": max(0, dur),
                        "args": {
                            "tool_use_id": tr.tool_use_id,
                            "is_error": tr.is_error,
                            "result_preview": _preview(tr.content),
                        },
                    },
                )
        elif t.kind == ir_module.KIND_USER_TURN and t.user_prompt is not None:
            emit(
                ts,
                _RANK["user_prompt"],
                {
                    "ph": "i",
                    "cat": "user",
                    "name": "user prompt",
                    "pid": pid,
                    "tid": TURNS_TID,
                    "ts": ts,
                    "s": "t",
                    "args": {"preview": _preview(t.user_prompt)},
                },
            )

    for sample in compute_context_lanes(turns):
        ts = sample.epoch_us - base_us
        emit(
            ts,
            _RANK["counter"],
            {
                "ph": "C",
                "cat": "context",
                "name": "context tokens",
                "pid": pids[sample.session_id],
                "tid": TURNS_TID,
                "ts": ts,
                "args": {key: sample.values[key] for key in _COUNTER_ORDER},
            },
        )

    pending.sort(key=lambda item: (item[0], item[1], item[2]))
    trace_events = meta + [event for _, _, _, event in pending]

    return {
        "traceEvents": trace_events,
        "displayTimeUnit": "ms",
        "metadata": _metadata(trace_ir.source, trace_ir.stats, models, base_us, generated_at),
    }
