"""Codex CLI adapter: rollout session JSONL → Agent Trace IR.

Reads the local JSONL "rollout" session logs (``~/.codex/sessions/YYYY/MM/DD/
rollout-<ts>-<uuid>.jsonl``) produced by the OpenAI Codex CLI and normalizes
them into :class:`agent2perfetto.ir.AgentTrace` — the emitter is untouched.

Schema grounding (rollout format observed 2026-09, shared with the
yield-audit Codex adapter; parsed defensively — when a future client renames
keys, records degrade to skips, never crashes):

- Every record carries ``timestamp`` (ISO-8601 UTC) and a ``type``:
  ``session_meta``, ``turn_context``, ``response_item``, or ``event_msg``.
- ``session_meta.payload`` carries the rollout's ``id`` and ``cwd``.
- ``turn_context.payload`` repeats ``cwd`` and carries the ``model`` in
  effect for subsequent turns.
- ``response_item.payload`` is a Chat-Completions-style item: ``message``
  (``role`` + ``content`` text blocks), ``function_call``
  (``call_id``/``name``/``arguments``), ``function_call_output``
  (``call_id``/``output`` JSON envelope with ``metadata.exit_code``).
- ``event_msg.payload`` of type ``token_count`` carries
  ``info.last_token_usage`` (``input_tokens``, ``cached_input_tokens``,
  ``output_tokens``).

IR mapping (claude-shaped timeline, see docs/agent-trace-ir.md):

- assistant ``message`` → ``model_call`` turn; the first ``token_count``
  after it is merged in as that call's usage (approximation: usage lands on
  the message timestamp, not the later event timestamp; duplicates never
  overwrite).
- user ``message`` → ``user_turn``; other roles → ``system``.
- ``function_call`` → a tool call attached to the most recent ``model_call``
  (a new one is synthesized when none precedes it, so tool calls never
  vanish). Shell-family tool names (``shell``/``local_shell``/
  ``exec_command``) normalize to ``Bash`` — the canonical set yield-audit
  uses — with list-valued commands joined to one string.
- ``function_call_output`` → ``tool_turn`` carrying the output envelope's
  ``output`` text; ``is_error`` from a nonzero ``exit_code``.
- Usage key mapping: ``cached_input_tokens`` → ``cache_read_input_tokens``;
  codex reports no cache-creation counter, so ``cache_creation_input_tokens``
  stays 0.
"""

from __future__ import annotations

import json
import sys

from .ir import (
    KIND_MODEL_CALL,
    KIND_SYSTEM,
    KIND_TOOL_TURN,
    KIND_USER_TURN,
    AgentTrace,
    IRSession,
    IRStats,
    IRToolCall,
    IRToolResult,
    IRTurn,
)
from .parser import ParseStats, StrictParseError, epoch_us_from_iso

KNOWN_TOP_LEVEL_FIELDS = frozenset({"timestamp", "type", "payload"})
KNOWN_TYPES = frozenset({"session_meta", "turn_context", "response_item", "event_msg"})

SHELL_TOOLS = {"shell", "local_shell", "exec_command"}
_MESSAGE_ROLES = {"assistant", "user"}

_usage_keys = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def _num(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    try:
        return int(value)
    except (ValueError, OverflowError):
        return 0


def _usage(info: dict) -> dict:
    usage = info.get("last_token_usage")
    if not isinstance(usage, dict):
        usage = info.get("total_token_usage")
    if not isinstance(usage, dict):
        usage = {}
    return {
        "input_tokens": _num(usage.get("input_tokens")),
        "output_tokens": _num(usage.get("output_tokens")),
        "cache_read_input_tokens": _num(usage.get("cached_input_tokens")),
        "cache_creation_input_tokens": 0,
    }


def _is_empty_usage(usage: dict) -> bool:
    return all(usage.get(key, 0) == 0 for key in _usage_keys)


def _message_texts(payload: dict) -> list:
    content = payload.get("content")
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    texts = []
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            texts.append(block["text"])
    return texts


def _parse_arguments(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, RecursionError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _output_content(raw) -> object:
    """The output envelope's text when parseable, else the raw value."""
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, RecursionError):
            return raw
        if isinstance(parsed, dict) and isinstance(parsed.get("output"), str):
            return parsed["output"]
    return raw


def _output_is_error(raw) -> bool:
    if not isinstance(raw, str) or not raw:
        return False
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, RecursionError):
        return False
    if not isinstance(parsed, dict):
        return False
    for candidate in (parsed.get("metadata"), parsed):
        if isinstance(candidate, dict) and isinstance(candidate.get("exit_code"), (int, float)):
            return candidate["exit_code"] != 0
    return False


class _State:
    """Per-file ingestion state: sessions, context, stats."""

    def __init__(self) -> None:
        self.sessions: dict = {}
        self.ctx: dict = {}  # session_id, cwd, model from meta/turn_context
        self.last_model_call: dict = {}  # session_id -> IRTurn of latest model_call
        self.stats = ParseStats()


def _ingest_session_meta(payload: dict, state: _State) -> None:
    session_id = payload.get("id")
    if isinstance(session_id, str) and session_id:
        state.ctx["session_id"] = session_id
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        state.ctx["cwd"] = cwd


def _ingest_turn_context(payload: dict, state: _State) -> None:
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        state.ctx["cwd"] = cwd
    model = payload.get("model")
    if isinstance(model, str) and model:
        state.ctx["model"] = model


def _session(state: _State, session_id: str) -> IRSession:
    sess = state.sessions.get(session_id)
    if sess is None:
        sess = IRSession(session_id=session_id, cwd=state.ctx.get("cwd"))
        state.sessions[session_id] = sess
    return sess


def _emit_turn(turn: IRTurn, state: _State) -> IRTurn:
    sess = _session(state, turn.session_id)
    turn.seq = len(sess.turns)
    sess.turns.append(turn)
    return turn


def _ingest_message(payload: dict, epoch_us: int, timestamp_raw: str, session_id: str, state: _State):
    role = payload.get("role")
    texts = _message_texts(payload)
    text = "\n".join(texts) if texts else None
    if role == "assistant":
        turn = _emit_turn(
            IRTurn(
                seq=0,
                epoch_us=epoch_us,
                timestamp_raw=timestamp_raw,
                session_id=session_id,
                kind=KIND_MODEL_CALL,
                vendor_type="response_item/message/assistant",
                model=state.ctx.get("model"),
                text=text,
                usage={},
            ),
            state,
        )
        state.last_model_call[session_id] = turn
    elif role == "user":
        _emit_turn(
            IRTurn(
                seq=0,
                epoch_us=epoch_us,
                timestamp_raw=timestamp_raw,
                session_id=session_id,
                kind=KIND_USER_TURN,
                vendor_type="response_item/message/user",
                user_prompt=text,
            ),
            state,
        )
    else:
        _emit_turn(
            IRTurn(
                seq=0,
                epoch_us=epoch_us,
                timestamp_raw=timestamp_raw,
                session_id=session_id,
                kind=KIND_SYSTEM,
                vendor_type=f"response_item/message/{role}",
                text=text,
            ),
            state,
        )


def _ingest_function_call(payload: dict, epoch_us: int, timestamp_raw: str, session_id: str, state: _State):
    call_id = payload.get("call_id")
    name = payload.get("name")
    if not isinstance(call_id, str) or not call_id or not isinstance(name, str):
        return
    args = _parse_arguments(payload.get("arguments"))
    if name in SHELL_TOOLS:
        name = "Bash"
        command = args.get("command", args.get("cmd"))
        if isinstance(command, list):
            args = {"command": " ".join(str(part) for part in command)}
        elif isinstance(command, str) and command:
            args = {"command": command}
    turn = state.last_model_call.get(session_id)
    if turn is None:
        turn = _emit_turn(
            IRTurn(
                seq=0,
                epoch_us=epoch_us,
                timestamp_raw=timestamp_raw,
                session_id=session_id,
                kind=KIND_MODEL_CALL,
                vendor_type="response_item/function_call",
                model=state.ctx.get("model"),
                usage={},
            ),
            state,
        )
        state.last_model_call[session_id] = turn
    turn.tool_calls.append(IRToolCall(id=call_id, name=name, input=args))


def _ingest_function_call_output(payload: dict, epoch_us: int, timestamp_raw: str, session_id: str, state: _State):
    call_id = payload.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        return
    raw = payload.get("output")
    _emit_turn(
        IRTurn(
            seq=0,
            epoch_us=epoch_us,
            timestamp_raw=timestamp_raw,
            session_id=session_id,
            kind=KIND_TOOL_TURN,
            vendor_type="response_item/function_call_output",
            tool_results=[
                IRToolResult(
                    tool_use_id=call_id,
                    content=_output_content(raw),
                    is_error=_output_is_error(raw),
                )
            ],
        ),
        state,
    )


def _ingest_token_count(payload: dict, state: _State, session_id: str) -> None:
    if payload.get("type") != "token_count":
        return
    info = payload.get("info")
    if not isinstance(info, dict):
        return
    usage = _usage(info)
    if _is_empty_usage(usage):
        return
    turn = state.last_model_call.get(session_id)
    if turn is not None and not turn.usage:
        turn.usage = usage


def parse_codex_lines(lines, *, strict: bool = False, stderr=None) -> AgentTrace:
    """Normalize Codex rollout JSONL lines into an Agent Trace IR."""
    stderr = sys.stderr if stderr is None else stderr
    state = _State()

    def warn(message: str) -> None:
        if strict:
            raise StrictParseError(message)
        print(f"agent2perfetto: {message}", file=stderr)

    for line_no, raw in enumerate(lines, 1):
        state.stats.total_lines += 1
        line = raw.strip()
        if not line:
            state.stats.blank_lines += 1
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            state.stats.malformed_lines += 1
            warn(f"line {line_no}: malformed JSON, line skipped")
            continue
        if not isinstance(obj, dict):
            state.stats.malformed_lines += 1
            warn(f"line {line_no}: expected a JSON object, line skipped")
            continue
        rtype = obj.get("type")
        if rtype not in KNOWN_TYPES:
            state.stats.skipped_bad_type += 1
            warn(f"line {line_no}: unknown record type {rtype!r}, record skipped")
            continue
        ts_raw = obj.get("timestamp")
        epoch_us = None
        if isinstance(ts_raw, str) and ts_raw.strip():
            try:
                epoch_us = epoch_us_from_iso(ts_raw)
            except ValueError:
                epoch_us = None
        if epoch_us is None:
            state.stats.skipped_no_timestamp += 1
            warn(f"line {line_no}: missing or unparsable timestamp, record skipped")
            continue
        unknown = sorted(k for k in obj if k not in KNOWN_TOP_LEVEL_FIELDS)
        if unknown:
            state.stats.records_with_unknown_fields += 1
            state.stats.unknown_field_count += len(unknown)

        payload = obj.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        session_id = state.ctx.get("session_id")
        session_id = session_id if isinstance(session_id, str) and session_id else "unknown-session"

        if rtype == "session_meta":
            _ingest_session_meta(payload, state)
        elif rtype == "turn_context":
            _ingest_turn_context(payload, state)
        elif rtype == "response_item":
            ptype = payload.get("type")
            if ptype == "message":
                _ingest_message(payload, epoch_us, ts_raw, session_id, state)
            elif ptype == "function_call":
                _ingest_function_call(payload, epoch_us, ts_raw, session_id, state)
            elif ptype == "function_call_output":
                _ingest_function_call_output(payload, epoch_us, ts_raw, session_id, state)
            # other response_item types (reasoning, web_search, …) are not
            # timeline events; they are silently not rendered
        elif rtype == "event_msg":
            _ingest_token_count(payload, state, session_id)

    stats = state.stats
    if stats.unknown_field_count:
        print(
            f"agent2perfetto: ignored {stats.unknown_field_count} unknown field(s) "
            f"on {stats.records_with_unknown_fields} line(s)",
            file=stderr,
        )
    st = stats
    from .ir import IRStats

    return AgentTrace(
        vendor="codex",
        source="codex-rollout-jsonl",
        sessions=[state.sessions[sid] for sid in sorted(state.sessions)],
        stats=IRStats(
            total_lines=st.total_lines,
            malformed_lines=st.malformed_lines,
            skipped_records=st.skipped_records,
            records_with_unknown_fields=st.records_with_unknown_fields,
        ),
    )


def parse_codex_file(path, *, strict: bool = False, stderr=None) -> AgentTrace:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        return parse_codex_lines(fh, strict=strict, stderr=stderr)
