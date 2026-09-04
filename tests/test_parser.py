from pathlib import Path

import pytest

from agent2perfetto.parser import StrictParseError, parse_file

FIXTURES = Path(__file__).parent / "fixtures"


def test_valid_session_parses_all_records():
    session = parse_file(FIXTURES / "valid_session.jsonl")
    assert [r.type for r in session.records] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert session.stats.total_lines == 6
    assert session.stats.malformed_lines == 0
    assert session.stats.skipped_records == 0


def test_tool_blocks_and_usage_extracted():
    session = parse_file(FIXTURES / "valid_session.jsonl")
    assistant = session.records[1]
    assert [tu.name for tu in assistant.tool_uses] == ["Bash"]
    assert assistant.tool_uses[0].id == "toolu_001"
    assert assistant.tool_uses[0].input == {"command": "ls -la"}
    assert assistant.usage["input_tokens"] == 100
    assert assistant.usage["cache_read_input_tokens"] == 200
    assert assistant.model == "claude-fixture-sonnet"
    result = session.records[2]
    assert [tr.tool_use_id for tr in result.tool_results] == ["toolu_001"]
    assert result.user_prompt is None
    prompt = session.records[0]
    assert prompt.user_prompt == "List the files, then run the tests."
    assert prompt.tool_results == []


def test_malformed_lines_counted_not_fatal():
    session = parse_file(FIXTURES / "malformed.jsonl")
    stats = session.stats
    assert stats.total_lines == 6
    assert stats.malformed_lines == 2  # bad JSON + JSON non-object
    assert stats.skipped_no_timestamp == 1  # "not-a-timestamp"
    assert stats.records_with_unknown_fields == 1  # weather + mood
    assert stats.unknown_field_count == 2
    assert len(session.records) == 3  # lines 1, 5 (extra fields tolerated), 6
    assert [r.uuid for r in session.records] == ["m-1", "m-3", "m-4"]
    assert session.records[1].unknown_fields == ["mood", "weather"]
    assert session.records[2].usage["input_tokens"] == 10


def test_strict_mode_raises_on_malformed():
    with pytest.raises(StrictParseError):
        parse_file(FIXTURES / "malformed.jsonl", strict=True)


def test_strict_mode_accepts_clean_input():
    session = parse_file(FIXTURES / "valid_session.jsonl", strict=True)
    assert len(session.records) == 6


def test_empty_file_yields_no_records():
    session = parse_file(FIXTURES / "empty.jsonl")
    assert session.records == []
    assert session.stats.total_lines == 0
