"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import webbrowser
from pathlib import Path

from . import __version__
from .parser import StrictParseError, parse_file
from .trace import build_trace

PERFETTO_UI_URL = "https://ui.perfetto.dev"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent2perfetto",
        description=(
            "Convert a Claude Code session JSONL into a Perfetto/Chrome trace "
            "JSON that loads in ui.perfetto.dev."
        ),
    )
    parser.add_argument("input", help="Claude Code session log (.jsonl)")
    parser.add_argument(
        "-o",
        "--output",
        help="output trace path (default: <input stem>.perfetto.json next to the input)",
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

    try:
        session = parse_file(src, strict=args.strict)
    except StrictParseError as exc:
        print(f"agent2perfetto: strict mode: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"agent2perfetto: cannot read {src}: {exc}", file=sys.stderr)
        return 1

    trace = build_trace(session)
    dst.write_text(json.dumps(trace, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    stats = session.stats
    print(
        f"agent2perfetto: parsed {len(session.records)} record(s) from {src} "
        f"({stats.malformed_lines} malformed, {stats.skipped_records} skipped)"
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
