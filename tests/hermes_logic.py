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

from blueocean_mcp.vector_store import _PROJECT_NAME_RE as SERVER_PROJECT_NAME_RE

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
        "stripe": "sk_live_" + "a" * 19992,
        "aiza": "AIza" * 5000,
        "asia": "ASIA" * 5000,
        "hf_": "hf_" * 6666,
        "glpat": "glpat-" * 3333,
        "npm_": "npm_" * 5000,
        "xapp": "xapp-" * 4000,
        "basic": "Authorization: Basic " + "a" * 19979,
        "basic*": "Authorization: Basic " * 952,
        "_key": "_key=" * 4000,
        "a_key": "a_key" * 4000,
        "passphrase": "passphrase:" * 1818,
        "credentials": "credentials " * 1666,
        "url no @": "x://u:" + "a" * 19994,
        "url a@": "x://u:" + "a@" * 9997,
        "url empty user": "x://:" * 4000,
        "url many": "a://b:" * 3333,
        "url slashes": "x://u:" + "/" * 19994,
        "pgp": "-----BEGIN PGP PRIVATE KEY BLOCK-----\n" + "A" * 19962,
        "pgp*": "-----BEGIN PGP PRIVATE KEY BLOCK" * 625,
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


def test_redaction_formats() -> None:
    print("== text: Stripe, Google, ASIA, hf/glpat/npm/xapp, PGP, Basic, *_KEY=, URL passwords ==")
    stripe = "sk_" "live_51HxYzAbCdEfGhIjKlMnOpQrStUv"
    google = "AIzaSyD-9tSrke72PouQMnMX-a7eZSW0jkFMBWY"
    leaks = {
        f"here is my key {stripe}": "51HxYzAbCdEfGhIjKlMnOpQrStUv",
        "restricted rk_" "test_51HxYzAbCdEfGhIjKlMnOp ok": "51HxYzAbCdEfGhIjKlMnOp",
        f"STRIPE_KEY={stripe}": "51HxYzAbCdEfGhIjKlMnOpQrStUv",
        f"use {google}": google,
        "https://maps.googleapis.com/api?key=AIzaSyD-9tSrke72PouQMnMX": "AIzaSyD",
        "temp creds ASIAIOSFODNN7EXAMPLE ok": "ASIAIOSFODNN7EXAMPLE",
        "token is hf_AbCdEfGhIjKlMnOpQrStUvWxYz123456": "hf_AbCdEfGhIjKlMnOpQrStUvWxYz123456",
        "glpat-xxxxxxxxxxxxxxxxxxxx1 works": "glpat-xxxxxxxxxxxxxxxxxxxx1",
        "npm_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789": "npm_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
        "xapp-1-A0123456789-0123456789012-abcdef0123456789": "abcdef0123456789",
        "-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQOYBF0AbCdEfGh\n"
        "-----END PGP PRIVATE KEY BLOCK-----": "lQOYBF0AbCdEfGh",
        'curl -H "Authorization: Basic dXNlcjpodW50ZXIyMjIy" https://x': "dXNlcjpodW50ZXIyMjIy",
        "ENCRYPTION_KEY=9f8e7d6c5b4a39281706f5e4d3c2b1a0": "9f8e7d6c5b4a39281706f5e4d3c2b1a0",
        "signing-key: 9f8e7d6c5b4a": "9f8e7d6c5b4a",
        "CREDENTIALS=abc123secretvalue": "abc123secretvalue",
        "credential = abc123secretvalue": "abc123secretvalue",
        "passphrase: correct horse battery": "correct",
        "postgres://app:ab/cd+ef@db.local/x": "ab/cd+ef",
        "DATABASE_URL=postgres://app:ab/cd@db/x": "ab/cd",
        "postgres://app:p@ss@db.local/x": "p@ss",
        # Redis has no username: the password follows an empty userinfo.
        "redis://:s3cretPw42@cache.local:6379/0": "s3cretPw42",
        "REDIS_URL=rediss://:s3cretPw42@cache.local:6380": "s3cretPw42",
        # Thai letters directly before a token: no \b boundary there.
        f"คีย์คือ{stripe}": "51HxYzAbCdEfGhIjKlMnOpQrStUv",
        "โทเคนhf_AbCdEfGhIjKlMnOpQrStUvWxYz123456นะ": "AbCdEfGhIjKlMnOpQrStUvWxYz123456",
    }
    for raw, secret in leaks.items():
        out = text.clean_turn_side(raw)
        assert secret not in out, f"secret survived redaction: {raw!r} -> {out!r}"
        assert "[REDACTED]" in out, out
    assert (text.redact("postgres://app:ab/cd+ef@db.local/x")
            == "postgres://app:[REDACTED]@db.local/x")
    # The password runs to the last `@`, so one holding a raw `@` is dropped whole.
    assert text.redact("postgres://app:p@ss@db.local/x") == "postgres://app:[REDACTED]@db.local/x"
    assert (text.redact("redis://:s3cretPw42@cache.local:6379/0")
            == "redis://:[REDACTED]@cache.local:6379/0")

    # A key name only counts in an assignment (`:` or `=`), so prose is left alone.
    prose = [
        "the monkey key is under the mat",
        "we keep the primary key and the foreign key in the credentials table",
        "the passphrase is written down somewhere safe",
        "Basic authentication works fine behind the proxy",
        "my-keyboard: a mechanical one",
        "see https://example.com/docs/a@b for details",
        "visit https://example.com:8080/docs today",
    ]
    for line in prose:
        assert text.redact(line) == line, f"ordinary prose was redacted: {line!r} -> {text.redact(line)!r}"
    print("  OK")


def test_straddling_secret_is_not_cut() -> None:
    print("== text: a secret straddling the pre-clip is dropped, not half-kept ==")
    limit = text.TRACE_SIDE_LIMIT
    key = "sk-proj-SECRETabcdefghijklmnop0123456789"
    fillers = {
        "assignment": "token=" + "A" * 7000 + "\n",
        "two PEM blocks": ("-----BEGIN RSA PRIVATE KEY-----\n" + "M" * 3300
                           + "\n-----END RSA PRIVATE KEY-----\n") * 2,
    }
    for name, filler in fillers.items():
        pad = 4 * limit - len(filler) - 12  # the key starts 12 chars before the cut
        raw = filler + "x" * (pad - 1) + " " + key + " tail"
        out = text.clean_turn_side(raw)
        assert key[:12] not in out, f"{name}: a cut secret leaked: {out[-60:]!r}"
        assert len(out) <= limit
    raw = ("token=" + "A" * 7000 + "\n" + "y" * (4 * limit - 7007 - 25)
           + " postgres://app:Sup3rS3cretPassw0rd@db/x")
    out = text.clean_turn_side(raw)
    assert "Sup3rS3cr" not in out, f"a cut URL password leaked: {out[-60:]!r}"

    # No whitespace at all: fall back to the fixed cut, still bounded.
    assert len(text.clean_turn_side("z" * 50000)) <= limit
    print("  OK")


def test_query_is_redacted() -> None:
    print("== text: the recall query is redacted and clipped ==")
    query = text.clean_query("which cache? password=hunter2secret and sk-proj-ABCDEFGHIJKLMNOPQRST",
                             1000)
    assert "hunter2secret" not in query and "ABCDEFGHIJKLMNOPQRST" not in query, query
    assert query.startswith("which cache?")
    assert len(text.clean_query("word " * 100000, 1000)) <= 1000
    print("  OK")


def test_project_name_validation() -> None:
    print("== project: names checked against the server's rule ==")
    assert project.PROJECT_NAME_RE.pattern == SERVER_PROJECT_NAME_RE.pattern, (
        "the plugin's project-name rule drifted from the server's")
    for good in ("blueocean", "blue-ocean", "a", "x_1", "9lives"):
        assert project.valid_project_name(good), good
    for bad in ("Blue Ocean", "blue ocean", "", "-x", "a/b", "a" * 101):
        assert not project.valid_project_name(bad), bad
    assert project.suggest_project_name("Blue Ocean") == "blue-ocean"
    assert project.suggest_project_name("team/a!!") == "team-a"
    assert project.suggest_project_name("!!!") is None
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


def test_project_settings() -> None:
    print("== config: projects allowlist with aliases, sync_turns_platforms ==")
    assert config.DEFAULTS["projects"] == {}, "no allowlist by default: behavior unchanged"
    assert config.DEFAULTS["sync_turns_platforms"] == ["cli"], "Turn Traces off on chat apps"

    # The text form is what `hermes memory setup` can collect; JSON can hold the dict.
    text_form = "blueocean-vector:blueocean|bo, Thalassaops ,tidepool:"
    assert config.parse_projects(text_form) == {
        "blueocean-vector": ["blueocean", "bo"], "thalassaops": [], "tidepool": []}
    assert config.parse_projects({"Blueocean-Vector": ["BlueOcean", " "], "x": "a|b"}) == {
        "blueocean-vector": ["blueocean"], "x": ["a", "b"]}
    for junk in (None, 42, "", ":alias", {"": ["a"]}, ["blueocean-vector"]):
        assert config.parse_projects(junk) == {}, f"junk must parse to no allowlist: {junk!r}"
    assert config.parse_platforms("cli, Discord ,,") == ["cli", "discord"]
    assert config.parse_platforms(["CLI"]) == ["cli"]
    assert config.parse_platforms("") == [], "an empty list means no platform writes traces"

    with tempfile.TemporaryDirectory() as home:
        config.save_config({"projects": "blueocean-vector:blueocean",
                            "sync_turns_platforms": "cli,telegram"}, home)
        loaded = config.load_config(home)
        assert loaded["projects"] == {"blueocean-vector": ["blueocean"]}
        assert loaded["sync_turns_platforms"] == ["cli", "telegram"]
        (Path(home) / config.CONFIG_FILE).write_text(json.dumps(
            {"projects": {"blueocean-vector": ["blueocean"]}, "sync_turns_platforms": []}))
        loaded = config.load_config(home)
        assert loaded["projects"] == {"blueocean-vector": ["blueocean"]}
        assert loaded["sync_turns_platforms"] == []
    print("  OK")


def test_alias_matching() -> None:
    print("== project: an allowlist limits detection, aliases resolve to their project ==")
    allowed = project.alias_map({"blueocean-vector": ["blueocean", "bo"], "tidepool": []})
    assert allowed == {"blueocean-vector": "blueocean-vector", "blueocean": "blueocean-vector",
                       "bo": "blueocean-vector", "tidepool": "tidepool"}
    cases = {
        "update blueocean ให้เป็น hermes memory provider": "blueocean-vector",
        "ดูblueoceanหน่อย": "blueocean-vector",  # Thai glued on both sides
        "BlueOcean-Vector status?": "blueocean-vector",
        "tidepool and blueocean": "blueocean-vector",  # longest name wins
        "ช่วยเช็ค kubernetes cluster ใน Magnum หน่อย": None,  # not on the list
        "Kops กับ kubeadm ต่างกันยังไง": None,
        "bobcat": None,  # an alias never matches inside a word
        "": None,
    }
    for message, expected in cases.items():
        got = project.match_alias(message, allowed)
        assert got == expected, f"{message!r}: expected {expected!r}, got {got!r}"

    t = project.ProjectTracker()
    known = ["kops", "kubernetes", "blueocean-vector"]
    assert t.observe("s", "kops vs kubeadm", known, [], allowed=allowed) is None
    assert t.observe("s", "back to blueocean", known, [], allowed=allowed) == "blueocean-vector"
    assert t.observe("s", "and kubernetes?", known, [], allowed=allowed) == "blueocean-vector"
    print("  OK")


def main() -> None:
    test_config()
    test_project_settings()
    test_alias_matching()
    test_fence_and_redaction()
    test_redaction_gaps()
    test_redaction_formats()
    test_straddling_secret_is_not_cut()
    test_query_is_redacted()
    test_project_name_validation()
    test_bounded_work()
    test_turn_cleaning()
    test_unseen_and_format()
    test_match_project()
    test_tracker()
    test_catalog()
    print("\nHERMES LOGIC TEST PASSED")


if __name__ == "__main__":
    main()
