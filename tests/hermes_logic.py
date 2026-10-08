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
import time
from pathlib import Path

from ._hermes import load_plugin_module

config = load_plugin_module("config")
text = load_plugin_module("text")
project = load_plugin_module("project")


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


def test_redaction_gaps() -> None:
    print("== text: prefixed, quoted, PEM, URL credentials; unclosed fence ==")
    leaks = {
        "GITHUB_TOKEN=ghx1234567890abcdef": "ghx1234567890abcdef",
        "export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG": "wJalrXUtnFEMI/K7MDENG",
        "my_token=abc123": "abc123",
        "db-password: s3cr3t": "s3cr3t",
        '{"password": "hunter2"}': "hunter2",
        'password = "my pass phrase"': "pass phrase",
        "api_key='k-123 456'": "123 456",
        "token: Bearer abcdefgh12345678": "abcdefgh12345678",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\nq1w2e3r4t5\n"
        "-----END RSA PRIVATE KEY-----": "MIIEowIBAAKCAQEA",
        "postgres://app:pa55word@db.local:5432/x": "pa55word",
        "รหัสGITHUB_TOKEN=abc999": "abc999",
    }
    for raw, secret in leaks.items():
        out = text.redact(raw)
        assert secret not in out, f"secret survived redaction: {raw!r} -> {out!r}"
        assert "[REDACTED]" in out
    assert "postgres://app:[REDACTED]@db.local" in text.redact("postgres://app:pa55word@db.local:5432/x")

    prose = "use Bearer authentication scheme here"
    assert text.redact(prose) == prose

    stripped = text.strip_memory_context("hi <memory-context>recalled stuff")
    assert "recalled stuff" not in stripped and stripped.startswith("hi")
    print("  OK")


def test_bounded_work() -> None:
    print("== text: adversarial input stays fast; Bearer and PEM edge cases ==")
    adversarial = {
        "a*": "a" * 20000,
        "-*": "-" * 20000,
        "a-": "a-" * 10000,
        "a.": "a." * 10000,
        "token": "token" * 4000,
        "password": "password" * 2500,
        "x://": "x://" + "a" * 19996,
        "a-a://": "a-a://" * 3333,
        "fence": "<memory-context>" * 1250,
        "pem": "-----BEGIN RSA PRIVATE KEY-----\n" + "A" * 19968,
        "quote": 'password="' + "a" * 19990,
        "bearer": "Bearer " + "a" * 19993,
    }
    worst = 0.0
    for name, raw in adversarial.items():
        for fn in (text.redact, text.strip_memory_context, text.clean_turn_side):
            start = time.perf_counter()
            fn(raw)
            took = time.perf_counter() - start
            worst = max(worst, took)
            assert took < 0.25, f"{fn.__name__} took {took:.2f}s on {name!r}"
    print(f"  slowest call: {worst:.3f}s")

    start = time.perf_counter()
    assert len(text.clean_turn_side("word " * 10000)) <= text.TRACE_SIDE_LIMIT
    assert time.perf_counter() - start < 0.25

    assert "abcdefgh12345" not in text.redact("token: Bearer abcdefgh12345")
    prose = "use Bearer authentication scheme here"
    assert text.redact(prose) == prose

    out = text.redact("-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\nkqhkiG9w0B")
    assert "MIIEvQIBADANBg" not in out and "kqhkiG9w0B" not in out
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


def test_match_project() -> None:
    print("== project: names found in Thai/English text, not inside tokens ==")
    known = ["blueocean", "blueocean-vector", "kops", "tidepool", "thalassaops",
             "auth-test-project"]
    ignore = ["*test*", "*-project", "probe-*", "*-probe"]
    cases = {
        "ช่วยดู blueocean-vector หน่อย": "blueocean-vector",
        # \b would see no boundary between Thai letters and the name.
        "ดูblueocean-vectorให้หน่อย": "blueocean-vector",
        "BlueOcean-Vector status?": "blueocean-vector",
        "just blueocean please": "blueocean",
        "kopsctl is a different tool": None,
        "open ~/Projects/Repo/kops/main.go": "kops",
        "auth-test-project and tidepool": "tidepool",
        "tidepool and thalassaops": "thalassaops",
        "nothing relevant here": None,
        "": None,
    }
    for message, expected in cases.items():
        got = project.match_project(message, known, ignore)
        assert got == expected, f"{message!r}: expected {expected!r}, got {got!r}"
    assert project.match_project(None, known, ignore) is None
    print("  OK")


def test_tracker() -> None:
    print("== project: latest signal wins, per session, seen ids ==")
    known, ignore = ["tidepool", "thalassaops", "kops"], []
    t = project.ProjectTracker()
    assert t.observe("s1", "work on tidepool", known, ignore) == "tidepool"
    assert t.observe("s1", "now continue", known, ignore) == "tidepool", "must be sticky"
    assert t.observe("s1", "switch to thalassaops", known, ignore) == "thalassaops"
    t.set_active("s1", "kops")
    assert t.active("s1") == "kops"
    assert t.active("s2") is None, "sessions must not share a project"

    t.mark_seen("s1", ["x", "y"])
    snapshot = t.seen("s1")
    snapshot.add("z")
    assert t.seen("s1") == {"x", "y"}, "seen() must return a copy"

    t.switch("s1", "s1", carry_project=True)  # rewound: same id
    assert t.active("s1") == "kops" and t.seen("s1") == set()

    t.mark_seen("s1", ["x"])
    t.switch("s1", "s3", carry_project=True)  # compression / resume
    assert t.active("s3") == "kops" and t.seen("s3") == set()

    t.switch("s3", "s4", carry_project=False)  # /new, /reset
    assert t.active("s4") is None
    print("  OK")


def test_catalog() -> None:
    print("== project: catalog caches for its TTL and survives failures ==")
    now = [1000.0]
    calls = []
    responses = [["b", "a"], RuntimeError("down"), ["c"]]

    def fetch():
        calls.append(1)
        value = responses[len(calls) - 1]
        if isinstance(value, Exception):
            raise value
        return value

    cat = project.ProjectCatalog(fetch, ttl=300.0, clock=lambda: now[0])
    assert cat.names() == ["a", "b"] and len(calls) == 1
    assert cat.names() == ["a", "b"] and len(calls) == 1, "must serve from cache"
    now[0] += 301
    assert cat.names() == ["a", "b"] and len(calls) == 2, "failed refresh keeps the old list"
    now[0] += 301
    assert cat.names() == ["c"] and len(calls) == 3

    empty = project.ProjectCatalog(lambda: (_ for _ in ()).throw(RuntimeError("down")))
    assert empty.names() == [], "no cache and a failed fetch must give []"
    print("  OK")


def main() -> None:
    test_config()
    test_fence_and_redaction()
    test_redaction_gaps()
    test_bounded_work()
    test_turn_cleaning()
    test_unseen_and_format()
    test_match_project()
    test_tracker()
    test_catalog()
    print("\nHERMES LOGIC TEST PASSED")


if __name__ == "__main__":
    main()
