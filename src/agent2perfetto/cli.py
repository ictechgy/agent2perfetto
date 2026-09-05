"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import webbrowser
from pathlib import Path

from . import __version__
from .codex import parse_codex_file
from .ir import from_claude_session
from .parser import StrictParseError, parse_file
from .trace import build_trace

PERFETTO_UI_URL = "https://ui.perfetto.dev"

_CLAUDE_TYPES = {"user", "assistant", "system", "summary"}
_CODEX_TYPES = {"session_meta", "turn_context", "response_item", "event_msg"}


def detect_format(src: Path) -> str:
    """'claude' or 'codex', decided by the first recognizable record.

    Claude and codex record types are disjoint, and codex records always wrap
    their content in a top-level 'payload' — either signal decides. Files
    with no recognizable early record fall back to claude (v0.1 behavior).
    """
    with src.open("r", encoding="utf-8", errors="replace") as fh:
        for _, raw in zip(range(64), fh):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, RecursionError):
                continue
            if not isinstance(obj, dict):
                continue
            if "payload" in obj or obj.get("type") in _CODEX_TYPES:
                return "codex"
            if "message" in obj or obj.get("type") in _CLAUDE_TYPES:
                return "claude"
    return "claude"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent2perfetto",
        description=(
            "Convert an agent session log into a Perfetto/Chrome trace "
            "JSON that loads in ui.perfetto.dev."
        ),
    )
    parser.add_argument("input", help="agent session log (.jsonl): Claude Code or Codex CLI rollout")
    parser.add_argument(
        "-o",
        "--output",
        help="output trace path (default: <input stem>.perfetto.json next to the input)",
    )
    parser.add_argument(
        "--format",
        choices=("auto", "claude", "codex"),
        default="auto",
        help="input log format (default: auto-detect from the first records)",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help=f"open {PERFETTO_UI_URL} so you can drag and drop the trace (browser-local; never uploaded)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="fail on malformed or unusable lines instead of skipping them",
    )
    parser.add_argument("--version", action="version", version=f"agent2perfetto {__version__}")
    args = parser.parse_args(argv)

    src = Path(args.input)
    if not src.is_file():
        print(f"agent2perfetto: input file not found: {src}", file=sys.stderr)
        return 1
    dst = Path(args.output) if args.output else src.with_name(src.stem + ".perfetto.json")

    fmt = args.format if args.format != "auto" else detect_format(src)
    try:
        if fmt == "codex":
            # Three-stage pipeline: adapter → IR → Perfetto emitter.
            trace_ir = parse_codex_file(src, strict=args.strict)
            trace = build_trace(trace_ir)
            record_count = len(trace_ir.events())
        else:
            session = parse_file(src, strict=args.strict)
            trace_ir = from_claude_session(session)
            trace = build_trace(trace_ir)
            record_count = len(session.records)
    except StrictParseError as exc:
        print(f"agent2perfetto: strict mode: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"agent2perfetto: cannot read {src}: {exc}", file=sys.stderr)
        return 1

    dst.write_text(json.dumps(trace, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    stats = trace_ir.stats
    print(
        f"agent2perfetto: parsed {record_count} record(s) from {src} ({fmt})"
        f" ({stats.malformed_lines} malformed, {stats.skipped_records} skipped)"
    )
    print(f"agent2perfetto: wrote {len(trace['traceEvents'])} trace event(s) -> {dst}")

    if args.open:
        _open_viewer(dst)
    return 0


def _open_viewer(trace_path: Path) -> None:
    print(f"agent2perfetto: opening {PERFETTO_UI_URL} ...")
    if sys.platform == "darwin":
        subprocess.run(["open", PERFETTO_UI_URL], check=False)
    else:
        webbrowser.open(PERFETTO_UI_URL)
    print(f"agent2perfetto: drag and drop this file into the Perfetto UI: {trace_path}")
    print("agent2perfetto: the UI processes traces locally in your browser; nothing is uploaded.")


if __name__ == "__main__":
    sys.exit(main())
