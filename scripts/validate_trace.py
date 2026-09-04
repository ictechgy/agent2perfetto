#!/usr/bin/env python3
"""Sanity-check an agent2perfetto (Chrome Trace Event JSON) trace file.

Checks the invariants Perfetto's JSON importer relies on:
  - valid JSON with a traceEvents list (and metadata dict)
  - every event carries the fields its ph requires
  - ts values are monotonically non-decreasing in array order
  - complete events (X) have dur >= 0, counters (C) have numeric args

Usage: python scripts/validate_trace.py TRACE.json [MORE.json ...]
Exit code 0 = all files valid, 1 = at least one file failed.
"""

from __future__ import annotations

import argparse
import json
import sys

REQUIRED = {
    "M": {"name", "pid", "args"},
    "X": {"name", "pid", "tid", "ts", "dur"},
    "C": {"name", "pid", "tid", "ts", "args"},
    "i": {"name", "pid", "tid", "ts"},
    "I": {"name", "pid", "tid", "ts"},
    "s": {"name", "pid", "tid", "ts", "id"},
    "t": {"name", "pid", "tid", "ts", "id"},
    "f": {"name", "pid", "tid", "ts", "id"},
    "b": {"name", "pid", "tid", "ts", "id"},
    "e": {"name", "pid", "tid", "ts", "id"},
    "n": {"name", "pid", "tid", "ts", "id"},
    "B": {"name", "pid", "tid", "ts"},
    "E": {"pid", "tid", "ts"},
}


def validate(path: str) -> list:
    errors = []
    with open(path, "r", encoding="utf-8") as f:
        try:
            doc = json.load(f)
        except json.JSONDecodeError as exc:
            return [f"not valid JSON: {exc}"]

    if not isinstance(doc, dict):
        return ["top level must be a JSON object"]
    events = doc.get("traceEvents")
    if not isinstance(events, list):
        return ['missing a "traceEvents" list']
    if not isinstance(doc.get("metadata", {}), dict):
        errors.append('"metadata" must be an object')

    prev_ts = None
    counts: dict = {}
    for i, ev in enumerate(events):
        where = f"{path}: event[{i}]"
        if not isinstance(ev, dict):
            errors.append(f"{where}: not an object")
            continue
        ph = ev.get("ph")
        counts[ph] = counts.get(ph, 0) + 1
        needed = REQUIRED.get(ph, {"name", "pid", "ts"})
        missing = sorted(k for k in needed if ev.get(k) is None)
        if missing:
            errors.append(f"{where}: ph={ph!r} missing field(s): {sorted(missing)}")
        ts = ev.get("ts")
        if not isinstance(ts, (int, float)):
            errors.append(f"{where}: ts must be a number, got {ts!r}")
        elif prev_ts is not None and ts < prev_ts:
            errors.append(f"{where}: ts {ts} < previous ts {prev_ts} (must be non-decreasing)")
        prev_ts = ts if isinstance(ts, (int, float)) else prev_ts
        if ph == "X":
            dur = ev.get("dur")
            if not isinstance(dur, (int, float)) or dur < 0:
                errors.append(f"{where}: dur must be a number >= 0, got {dur!r}")
        if ph == "C":
            args = ev.get("args")
            if not isinstance(args, dict) or not args:
                errors.append(f"{where}: counter events need non-empty args")
            elif not all(isinstance(v, (int, float)) for v in args.values()):
                errors.append(f"{where}: counter args values must be numbers")

    summary = ", ".join(f"{k or '?'}:{v}" for k, v in sorted(counts.items()))
    print(f"{path}: {len(events)} traceEvents ({summary})")
    return errors


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Validate a Chrome/Perfetto JSON trace file.")
    ap.add_argument("traces", nargs="+", help="trace JSON file(s) to validate")
    args = ap.parse_args(argv)

    all_errors = []
    for path in args.traces:
        try:
            all_errors.extend(validate(path))
        except OSError as exc:
            all_errors.append(f"{path}: cannot read: {exc}")

    if all_errors:
        print("\nFAILED:")
        for err in all_errors:
            print(f"  - {err}")
        return 1
    print("\nAll traces valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
