"""Build a Perfetto-compatible Chrome Trace Event JSON dict from a parsed session."""

from __future__ import annotations

import bisect
import json
from datetime import datetime, timezone

from . import __version__
from .lanes import APPROXIMATION_NOTE, OCC_COUNTERS, SPEND_COUNTERS, compute_context_lanes
from .parser import Session

SOURCE_NAME = "claude-code-jsonl"
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


def _pair_tool_calls(records):
    """Pair tool_use blocks with the first later tool_result sharing their id.

    Returns (pairs, unmatched_uses) where pairs maps tool_use_id to
    (use_record, ToolUse, result_record, ToolResult) and unmatched_uses is a
    list of (use_record, ToolUse) with no later result (orphan results are
    still rendered as slices by the caller).
    """
    uses = [(r, tu) for r in records for tu in r.tool_uses]
    results_by_id: dict = {}
    for r in records:
        for tr in r.tool_results:
            results_by_id.setdefault(tr.tool_use_id, []).append(r)
    pairs = {}
    matched = set()
    for use_rec, tu in uses:
        for result_rec in results_by_id.get(tu.id, ()):
            if result_rec.seq > use_rec.seq:
                result = next(t for t in result_rec.tool_results if t.tool_use_id == tu.id)
                pairs[tu.id] = (use_rec, tu, result_rec, result)
                matched.add(tu.id)
                break
    unmatched = [(r, tu) for r, tu in uses if tu.id not in matched]
    return pairs, unmatched


def _metadata(session: Session, models: list, base_us, generated_at) -> dict:
    stats = session.stats
    md = {
        "source": SOURCE_NAME,
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


def build_trace(session: Session, *, generated_at: str | None = None) -> dict:
    """Return {"traceEvents": [...], "displayTimeUnit": "ms", "metadata": {...}}.

    Deterministic: the same parsed session yields byte-identical traceEvents
    (only metadata.generated_at varies between runs).
    """
    records = list(session.records)
    models = sorted({r.model for r in records if r.model})
    if not records:
        return {
            "traceEvents": [],
            "displayTimeUnit": "ms",
            "metadata": _metadata(session, models, None, generated_at),
        }

    base_us = min(r.epoch_us for r in records)
    times = sorted({r.epoch_us for r in records})
    pids = {sid: pid for pid, sid in enumerate(sorted({r.session_id for r in records}), start=1)}

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

    pairs, unmatched_uses = _pair_tool_calls(records)
    flow_ids = {tool_use_id: i for i, tool_use_id in enumerate(sorted(pairs), start=1)}

    for r in sorted(records, key=lambda r: (r.epoch_us, r.seq)):
        ts = r.epoch_us - base_us
        nxt = next_us(r.epoch_us)
        pid = pids[r.session_id]
        if r.type == "assistant":
            dur_estimated = nxt is None
            turn_dur = (nxt - r.epoch_us) if nxt is not None else ESTIMATED_TURN_DUR_US
            args = {"model": r.model, "uuid": r.uuid, "usage": dict(r.usage)}
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
            for tu in r.tool_uses:
                pair = pairs.get(tu.id)
                if pair is not None:
                    tool_dur = pair[2].epoch_us - r.epoch_us
                else:
                    tool_dur = (nxt - r.epoch_us) if nxt is not None else 0
                emit(
                    ts,
                    _RANK["tool_use"],
                    {
                        "ph": "X",
                        "cat": "tool_call",
                        "name": tu.name,
                        "pid": pid,
                        "tid": TOOLS_TID,
                        "ts": ts,
                        "dur": max(0, tool_dur),
                        "args": {
                            "tool_use_id": tu.id,
                            "input": tu.input,
                            "usage": dict(r.usage),
                        },
                    },
                )
                if pair is not None:
                    fid = flow_ids[tu.id]
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
        elif r.type == "user":
            if r.tool_results:
                for tr in r.tool_results:
                    use = pairs.get(tr.tool_use_id)
                    name = f"result {use[1].name}" if use else "tool result"
                    dur = (nxt - r.epoch_us) if nxt is not None else 0
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
            elif r.user_prompt is not None:
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
                        "args": {"preview": _preview(r.user_prompt)},
                    },
                )

    for sample in compute_context_lanes(records):
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
        "metadata": _metadata(session, models, base_us, generated_at),
    }
