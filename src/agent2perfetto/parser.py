"""Parse Claude Code session JSONL into typed records.

Tolerates schema drift: malformed lines and unusable records are skipped and
counted, unknown top-level fields are kept out of the records but counted.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

KNOWN_TOP_LEVEL_FIELDS = frozenset(
    {
        "type",
        "timestamp",
        "uuid",
        "parentUuid",
        "sessionId",
        "cwd",
        "message",
        "subtype",
        "isSidechain",
        "userType",
        "version",
        "gitBranch",
        "leafUuid",
        "requestId",
    }
)

KNOWN_TYPES = frozenset({"user", "assistant", "system", "summary"})

_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

# datetime.fromisoformat in Python 3.10 rejects "Z" and fractions longer than
# 6 digits, so normalize both before parsing.
_FRACTION_RE = re.compile(r"\.(\d+)")

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class StrictParseError(RuntimeError):
    """Raised in strict mode on the first malformed or unusable line."""


@dataclass
class ToolUse:
    id: str
    name: str
    input: dict


@dataclass
class ToolResult:
    tool_use_id: str
    content: object
    is_error: bool = False


@dataclass
class Record:
    seq: int
    line_no: int
    type: str
    timestamp_raw: str
    epoch_us: int
    uuid: str | None
    parent_uuid: str | None
    session_id: str
    cwd: str | None
    model: str | None
    user_prompt: str | None
    text: str | None
    tool_uses: list
    tool_results: list
    usage: dict
    unknown_fields: list


@dataclass
class ParseStats:
    total_lines: int = 0
    blank_lines: int = 0
    malformed_lines: int = 0
    skipped_no_timestamp: int = 0
    skipped_bad_type: int = 0
    records_with_unknown_fields: int = 0
    unknown_field_count: int = 0

    @property
    def skipped_records(self) -> int:
        return self.skipped_no_timestamp + self.skipped_bad_type


@dataclass
class Session:
    records: list
    stats: ParseStats


def epoch_us_from_iso(ts: str) -> int:
    """Convert an ISO-8601 timestamp to exact integer microseconds since epoch."""
    s = ts.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    m = _FRACTION_RE.search(s)
    if m and len(m.group(1)) > 6:
        s = s[: m.start()] + "." + m.group(1)[:6] + s[m.end() :]
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = dt - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _usage(message: dict) -> dict:
    raw = message.get("usage")
    raw = raw if isinstance(raw, dict) else {}

    def num(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0
        try:
            return int(value)
        except (ValueError, OverflowError):
            return 0

    return {key: num(raw.get(key)) for key in _USAGE_KEYS}


def _extract_message(obj: dict):
    message = obj.get("message")
    if not isinstance(message, dict):
        message = {}
    model = message.get("model")
    model = model if isinstance(model, str) else None
    content = message.get("content")
    texts = []
    tool_uses = []
    tool_results = []
    user_prompt = None
    if isinstance(content, str):
        user_prompt = content
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif btype == "tool_use":
                tool_uses.append(
                    ToolUse(
                        id=str(block.get("id", "")),
                        name=str(block.get("name") or "unknown-tool"),
                        input=block.get("input") if isinstance(block.get("input"), dict) else {},
                    )
                )
            elif btype == "tool_result":
                tool_results.append(
                    ToolResult(
                        tool_use_id=str(block.get("tool_use_id", "")),
                        content=block.get("content"),
                        is_error=bool(block.get("is_error")),
                    )
                )
        # A message carrying tool_result blocks is a tool turn, not a user prompt.
        if not tool_results and texts:
            user_prompt = "\n".join(texts)
    usage = _usage(message)
    text = "\n".join(texts) if texts else None
    return model, user_prompt, text, tool_uses, tool_results, usage


def parse_lines(lines, *, strict: bool = False, stderr=None) -> Session:
    stderr = sys.stderr if stderr is None else stderr
    stats = ParseStats()
    records: list = []

    def warn(message: str) -> None:
        if strict:
            raise StrictParseError(message)
        print(f"agent2perfetto: {message}", file=stderr)

    for line_no, raw in enumerate(lines, 1):
        stats.total_lines += 1
        line = raw.strip()
        if not line:
            stats.blank_lines += 1
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            stats.malformed_lines += 1
            warn(f"line {line_no}: malformed JSON, line skipped")
            continue
        if not isinstance(obj, dict):
            stats.malformed_lines += 1
            warn(f"line {line_no}: expected a JSON object, line skipped")
            continue
        rtype = obj.get("type")
        if rtype not in KNOWN_TYPES:
            stats.skipped_bad_type += 1
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
            stats.skipped_no_timestamp += 1
            warn(f"line {line_no}: missing or unparsable timestamp, record skipped")
            continue
        unknown = sorted(k for k in obj if k not in KNOWN_TOP_LEVEL_FIELDS)
        if unknown:
            stats.records_with_unknown_fields += 1
            stats.unknown_field_count += len(unknown)
        model, user_prompt, text, tool_uses, tool_results, usage = _extract_message(obj)
        records.append(
            Record(
                seq=len(records),
                line_no=line_no,
                type=rtype,
                timestamp_raw=ts_raw,
                epoch_us=epoch_us,
                uuid=obj.get("uuid") if isinstance(obj.get("uuid"), str) else None,
                parent_uuid=obj.get("parentUuid") if isinstance(obj.get("parentUuid"), str) else None,
                session_id=obj.get("sessionId") if isinstance(obj.get("sessionId"), str) else "unknown-session",
                cwd=obj.get("cwd") if isinstance(obj.get("cwd"), str) else None,
                model=model,
                user_prompt=user_prompt,
                text=text,
                tool_uses=tool_uses,
                tool_results=tool_results,
                usage=usage,
                unknown_fields=unknown,
            )
        )

    if stats.unknown_field_count:
        print(
            f"agent2perfetto: ignored {stats.unknown_field_count} unknown field(s) "
            f"on {stats.records_with_unknown_fields} line(s)",
            file=stderr,
        )
    return Session(records=records, stats=stats)


def parse_file(path, *, strict: bool = False, stderr=None) -> Session:
    text = path.read_text(encoding="utf-8", errors="replace")
    return parse_lines(text.splitlines(), strict=strict, stderr=stderr)
