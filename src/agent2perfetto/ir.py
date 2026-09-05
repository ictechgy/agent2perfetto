"""Agent Trace IR — the vendor-neutral intermediate representation.

The conversion pipeline is three-stage: an adapter parses one vendor's
session log into typed records, this module normalizes those records into
the IR, and emitters render the IR (Perfetto JSON today; other outputs
later). Only adapters are vendor-specific; the IR and the emitters are not.

The IR is a data contract, not a product: ``to_json`` / ``from_json`` make
it stable as a JSON schema so other tools (a Codex adapter, yield-audit)
can produce or consume it without importing the parser.

Hierarchy: ``AgentTrace -> IRSession -> IRTurn``; a turn carries optional
``tool_calls`` and ``tool_results``. Every turn keeps ``seq`` — its position
in the source file — because timeline determinism (events sorted by
(ts, rank, seq)) depends on source order surviving normalization.

Turn kinds are vendor-neutral vocabulary:

* ``model_call``  — one assistant/API call: model, usage, text, tool_calls
* ``user_turn``   — a human prompt (``user_prompt``; may be None)
* ``tool_turn``   — tool results delivered back to the harness
* ``system`` / ``summary`` — metadata records; carried because they still
  anchor the timeline (base timestamp, next-event bounds), not rendered.
"""

from __future__ import annotations

from dataclasses import dataclass, field

IR_VERSION = 1

KIND_MODEL_CALL = "model_call"
KIND_USER_TURN = "user_turn"
KIND_TOOL_TURN = "tool_turn"
KIND_SYSTEM = "system"
KIND_SUMMARY = "summary"

# Claude Code record type -> neutral turn kind. Records of unknown type never
# reach the IR (the parser skips and counts them).
_CLAUDE_KINDS = {
    "assistant": KIND_MODEL_CALL,
    "user": KIND_USER_TURN,  # upgraded to tool_turn below when results ride along
    "system": KIND_SYSTEM,
    "summary": KIND_SUMMARY,
}


@dataclass
class IRToolCall:
    id: str
    name: str
    input: dict


@dataclass
class IRToolResult:
    tool_use_id: str
    content: object
    is_error: bool = False


@dataclass
class IRTurn:
    seq: int
    epoch_us: int
    timestamp_raw: str
    session_id: str
    kind: str
    vendor_type: str
    uuid: str | None = None
    parent_uuid: str | None = None
    model: str | None = None
    usage: dict = field(default_factory=dict)
    text: str | None = None
    user_prompt: str | None = None
    tool_calls: list = field(default_factory=list)
    tool_results: list = field(default_factory=list)


@dataclass
class IRSession:
    session_id: str
    cwd: str | None = None
    turns: list = field(default_factory=list)


@dataclass
class IRStats:
    total_lines: int = 0
    malformed_lines: int = 0
    skipped_records: int = 0
    records_with_unknown_fields: int = 0


@dataclass
class AgentTrace:
    vendor: str
    source: str
    sessions: list
    stats: IRStats = field(default_factory=IRStats)
    ir_version: int = IR_VERSION

    def events(self) -> list:
        """All turns of all sessions in global timeline order.

        Emitters must consume turns through this: source order (seq) and
        cross-session adjacency drive the deterministic (ts, rank, seq)
        event ordering and the next-event duration bounds.
        """
        return sorted(
            (t for s in self.sessions for t in s.turns),
            key=lambda t: (t.epoch_us, t.seq),
        )


def from_claude_session(session) -> AgentTrace:
    """Normalize a parsed Claude Code session (parser.Session) into the IR."""
    sessions: dict = {}
    turns: list = []
    for r in session.records:
        kind = _CLAUDE_KINDS.get(r.type)
        if kind is None:  # defensive: parser already filters unknown types
            continue
        if r.type == "user" and r.tool_results:
            kind = KIND_TOOL_TURN
        turn = IRTurn(
            seq=r.seq,
            epoch_us=r.epoch_us,
            timestamp_raw=r.timestamp_raw,
            session_id=r.session_id,
            kind=kind,
            vendor_type=r.type,
            uuid=r.uuid,
            parent_uuid=r.parent_uuid,
            model=r.model,
            usage=dict(r.usage),
            text=r.text,
            user_prompt=r.user_prompt,
            tool_calls=[IRToolCall(id=tu.id, name=tu.name, input=dict(tu.input)) for tu in r.tool_uses],
            tool_results=[
                IRToolResult(tool_use_id=tr.tool_use_id, content=tr.content, is_error=tr.is_error)
                for tr in r.tool_results
            ],
        )
        turns.append(turn)
        sess = sessions.setdefault(r.session_id, IRSession(session_id=r.session_id))
        if sess.cwd is None and r.cwd is not None:
            sess.cwd = r.cwd
        sess.turns.append(turn)

    st = session.stats
    return AgentTrace(
        vendor="claude-code",
        source="claude-code-jsonl",
        sessions=[sessions[sid] for sid in sorted(sessions)],
        stats=IRStats(
            total_lines=st.total_lines,
            malformed_lines=st.malformed_lines,
            skipped_records=st.skipped_records,
            records_with_unknown_fields=st.records_with_unknown_fields,
        ),
    )


def to_json(trace: AgentTrace) -> dict:
    """Serialize the IR to its stable JSON schema (see docs/agent-trace-ir.md)."""
    return {
        "agent_trace_ir": trace.ir_version,
        "vendor": trace.vendor,
        "source": trace.source,
        "stats": {
            "total_lines": trace.stats.total_lines,
            "malformed_lines": trace.stats.malformed_lines,
            "skipped_records": trace.stats.skipped_records,
            "records_with_unknown_fields": trace.stats.records_with_unknown_fields,
        },
        "sessions": [
            {
                "session_id": s.session_id,
                "cwd": s.cwd,
                "turns": [_turn_json(t) for t in sorted(s.turns, key=lambda t: (t.epoch_us, t.seq))],
            }
            for s in sorted(trace.sessions, key=lambda s: s.session_id)
        ],
    }


def _turn_json(t: IRTurn) -> dict:
    return {
        "seq": t.seq,
        "timestamp": t.timestamp_raw,
        "epoch_us": t.epoch_us,
        "session_id": t.session_id,
        "kind": t.kind,
        "vendor_type": t.vendor_type,
        "uuid": t.uuid,
        "parent_uuid": t.parent_uuid,
        "model": t.model,
        "usage": dict(t.usage),
        "text": t.text,
        "user_prompt": t.user_prompt,
        "tool_calls": [
            {"id": c.id, "name": c.name, "input": c.input} for c in t.tool_calls
        ],
        "tool_results": [
            {"tool_use_id": r.tool_use_id, "content": r.content, "is_error": r.is_error}
            for r in t.tool_results
        ],
    }


def from_json(obj: dict) -> AgentTrace:
    """Rebuild the IR from its JSON schema. Unknown fields are ignored so the
    schema can grow without breaking older consumers."""
    if not isinstance(obj, dict):
        raise TypeError("agent trace IR must be a JSON object")
    ir_version = obj.get("agent_trace_ir")
    if ir_version != IR_VERSION:
        raise ValueError(f"unsupported agent_trace_ir version: {ir_version!r}")
    stats_raw = obj.get("stats") or {}
    sessions = []
    for s in obj.get("sessions") or []:
        turns = [
            IRTurn(
                seq=t["seq"],
                epoch_us=t["epoch_us"],
                timestamp_raw=t.get("timestamp", ""),
                session_id=t.get("session_id") or s.get("session_id", "unknown-session"),
                kind=t.get("kind", KIND_SYSTEM),
                vendor_type=t.get("vendor_type", ""),
                uuid=t.get("uuid"),
                parent_uuid=t.get("parent_uuid"),
                model=t.get("model"),
                usage=dict(t.get("usage") or {}),
                text=t.get("text"),
                user_prompt=t.get("user_prompt"),
                tool_calls=[
                    IRToolCall(id=c["id"], name=c["name"], input=c.get("input") or {})
                    for c in t.get("tool_calls") or []
                    if isinstance(c, dict) and "id" in c and "name" in c
                ],
                tool_results=[
                    IRToolResult(
                        tool_use_id=r.get("tool_use_id", ""),
                        content=r.get("content"),
                        is_error=bool(r.get("is_error")),
                    )
                    for r in t.get("tool_results") or []
                    if isinstance(r, dict)
                ],
            )
            for t in s.get("turns") or []
            if isinstance(t, dict) and "seq" in t and "epoch_us" in t
        ]
        sessions.append(
            IRSession(session_id=s.get("session_id", "unknown-session"), cwd=s.get("cwd"), turns=turns)
        )
    return AgentTrace(
        vendor=obj.get("vendor", "unknown"),
        source=obj.get("source", "unknown"),
        sessions=sessions,
        stats=IRStats(
            total_lines=int(stats_raw.get("total_lines", 0)),
            malformed_lines=int(stats_raw.get("malformed_lines", 0)),
            skipped_records=int(stats_raw.get("skipped_records", 0)),
            records_with_unknown_fields=int(stats_raw.get("records_with_unknown_fields", 0)),
        ),
        ir_version=ir_version,
    )
