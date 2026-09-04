import json
from pathlib import Path

from agent2perfetto.cli import main

FIXTURES = Path(__file__).parent / "fixtures"


def test_cli_writes_output_and_exits_zero(tmp_path, capsys):
    out = tmp_path / "out.json"
    rc = main([str(FIXTURES / "valid_session.jsonl"), "-o", str(out)])
    assert rc == 0
    trace = json.loads(out.read_text(encoding="utf-8"))
    assert trace["displayTimeUnit"] == "ms"
    assert len(trace["traceEvents"]) == 18
    assert trace["metadata"]["source"] == "claude-code-jsonl"
    captured = capsys.readouterr()
    assert "18 trace event(s)" in captured.out


def test_cli_default_output_name(tmp_path):
    src = tmp_path / "session.jsonl"
    src.write_text(
        (FIXTURES / "valid_session.jsonl").read_text(encoding="utf-8"), encoding="utf-8"
    )
    rc = main([str(src)])
    assert rc == 0
    default_out = tmp_path / "session.perfetto.json"
    assert default_out.is_file()
    trace = json.loads(default_out.read_text(encoding="utf-8"))
    assert trace["traceEvents"]


def test_cli_missing_input_exits_nonzero(tmp_path):
    rc = main([str(tmp_path / "nope.jsonl")])
    assert rc == 1


def test_cli_empty_input_produces_valid_empty_trace(tmp_path):
    out = tmp_path / "empty.json"
    rc = main([str(FIXTURES / "empty.jsonl"), "-o", str(out)])
    assert rc == 0
    trace = json.loads(out.read_text(encoding="utf-8"))
    assert trace["traceEvents"] == []
    assert isinstance(trace["metadata"], dict)


def test_cli_strict_fails_on_malformed(tmp_path):
    out = tmp_path / "strict.json"
    rc = main([str(FIXTURES / "malformed.jsonl"), "-o", str(out), "--strict"])
    assert rc == 1
    assert not out.exists()


def test_cli_lenient_mode_skips_malformed(tmp_path):
    out = tmp_path / "lenient.json"
    rc = main([str(FIXTURES / "malformed.jsonl"), "-o", str(out)])
    assert rc == 0
    trace = json.loads(out.read_text(encoding="utf-8"))
    assert trace["metadata"]["warnings"]["malformed_lines"] == 2
