"""The Hermes provider's pure logic: no Hermes install, no server.

Covers what a turn's text goes through before it is stored (fence stripping,
secret redaction, clipping), how recalled entries are de-duplicated and
formatted, and how the plugin's settings file is read and written.

Run with:
    uv run python -m tests.hermes_logic
"""

import calendar
import json
import tempfile
from pathlib import Path

from ._hermes import load_plugin_module

config = load_plugin_module("config")
text = load_plugin_module("text")


def test_config() -> None:
    print("== config: defaults, round trip, clamping, junk ==")
    with tempfile.TemporaryDirectory() as home:
        assert config.load_config(home) == config.DEFAULTS, "missing file must give defaults"

        config.save_config(
            {"url": "http://example:1/mcp", "auth_token": "s3cret", "bogus": 1,
             "prefetch_max_tokens": 5000, "sync_turns": "false"},
            home,
        )
        raw = json.loads((Path(home) / config.CONFIG_FILE).read_text())
        assert "auth_token" not in raw, "a secret was written to the settings file"
        assert "bogus" not in raw, "an unknown key was written"

        loaded = config.load_config(home)
        assert loaded["url"] == "http://example:1/mcp"
        assert loaded["prefetch_max_tokens"] == 2000, "budget must clamp to 2000"
        assert loaded["sync_turns"] is False, "'false' must read as False"

        config.save_config({"prefetch_max_tokens": 10}, home)
        loaded = config.load_config(home)
        assert loaded["prefetch_max_tokens"] == 100, "budget must clamp to 100"
        assert loaded["url"] == "http://example:1/mcp", "save must merge, not replace"

        (Path(home) / config.CONFIG_FILE).write_text("{not json")
        assert config.load_config(home) == config.DEFAULTS, "malformed file must give defaults"

    assert config.ignore_globs("a, b,,") == ["a", "b"]
    assert config.ignore_globs(["x", " y "]) == ["x", "y"]
    assert config.ignore_globs(None) == []
    print("  OK")


def test_fence_and_redaction() -> None:
    print("== text: memory-context fence stripped, secrets redacted ==")
    fenced = "before <memory-context>\nrecalled entry\n</memory-context> after"
    stripped = text.strip_memory_context(fenced)
    assert "recalled entry" not in stripped and stripped.startswith("before")
    assert text.strip_memory_context(None) == ""

    secrets = {
        "Authorization: Bearer abcdefghijklmnop1234": "abcdefghijklmnop1234",
        "key sk-proj-ABCDEFGHIJKLMNOPQRST": "sk-proj-ABCDEFGHIJKLMNOPQRST",
        "aws AKIAIOSFODNN7EXAMPLE here": "AKIAIOSFODNN7EXAMPLE",
        "gh ghp_" + "a" * 36: "ghp_" + "a" * 36,
        "pat github_pat_" + "B" * 30: "github_pat_" + "B" * 30,
        "slack xoxb-123456789012-abcdefghij": "xoxb-123456789012-abcdefghij",
        "password=hunter2!": "hunter2!",
        "api_key: zzz999": "zzz999",
        # Thai letters count as \w, so a \b-anchored pattern misses these.
        "คีย์คือsk-ABCDEFGHIJKLMNOPQRSTUV": "sk-ABCDEFGHIJKLMNOPQRSTUV",
        "โทเคนAKIAIOSFODNN7EXAMPLEนะ": "AKIAIOSFODNN7EXAMPLE",
    }
    for raw, secret in secrets.items():
        out = text.redact(raw)
        assert secret not in out, f"secret survived redaction: {raw!r} -> {out!r}"
        assert "[REDACTED]" in out
    assert text.redact("password=hunter2!") == "password=[REDACTED]"

    harmless = "we chose redis for the token bucket and the skip-list"
    assert text.redact(harmless) == harmless, "ordinary prose was redacted"
    print("  OK")


def test_turn_cleaning() -> None:
    print("== text: turn sides cleaned, clipped, summarised ==")
    long_side = "x" * 5000
    cleaned = text.clean_turn_side(long_side)
    assert len(cleaned) <= text.TRACE_SIDE_LIMIT and cleaned.endswith("…")

    side = text.clean_turn_side("<memory-context>old</memory-context>token=abc123 hi")
    assert "old" not in side and "abc123" not in side and side.endswith("hi")

    assert text.trace_content("u", "a") == "User: u\n\nAssistant: a"
    summary = text.trace_summary("line one\n\n  line two " + "y" * 400, "ok\tdone")
    user_part, assistant_part = summary.split(" -> ")
    assert "\n" not in summary and "  " not in user_part
    assert len(user_part) <= text.SUMMARY_SIDE_LIMIT
    assert assistant_part == "ok done"
    print("  OK")


def test_unseen_and_format() -> None:
    print("== text: dedup by id, recall block format ==")
    ts = calendar.timegm((2026, 10, 8, 12, 0, 0))
    meta = {"area": "architecture", "module": "core", "importance": 5, "timestamp": ts}
    result = {
        "summary": [
            {"id": "a", "summary": "Seen already", "metadata": meta},
            {"id": "b", "summary": "Chose Redis", "metadata": meta},
        ],
        "full": [
            {"id": "a", "content": "old detail", "metadata": meta},
            {"id": "b", "content": "Redis won on latency", "metadata": meta},
        ],
    }
    summaries, fulls = text.unseen(result, {"a"})
    assert [r["id"] for r in summaries] == ["b"] and [r["id"] for r in fulls] == ["b"]
    assert text.unseen({}, set()) == ([], [])

    block = text.format_recall("proj", summaries, fulls)
    assert block.splitlines()[0] == "BlueOcean project memory - proj"
    assert "- [architecture/core · 2026-10-08 · importance 5] Chose Redis" in block
    assert "Details:" in block and "Redis won on latency" in block
    assert "old detail" not in block
    print("  OK")


def main() -> None:
    test_config()
    test_fence_and_redaction()
    test_turn_cleaning()
    test_unseen_and_format()
    print("\nHERMES LOGIC TEST PASSED")


if __name__ == "__main__":
    main()
