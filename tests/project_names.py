"""Strict project-name validation.

collection_name() must reject anything that isn't already in canonical
form, instead of silently normalizing different spellings ("Team A",
"team/a", "TEAM-A") into the same Qdrant collection -- that kind of silent
merge is a real cross-project data-mixing bug, not a hypothetical one, once
different agents/tools are each independently guessing a project's name.

Run with:
    uv run python -m tests.project_names
"""

from blueocean_mcp.vector_store import collection_name


def main() -> None:
    print("== valid names pass through unchanged ==")
    assert collection_name("demo-project") == "blueocean_demo-project"
    assert collection_name("a") == "blueocean_a"
    assert collection_name("team_a-2") == "blueocean_team_a-2"
    assert collection_name("a" * 100) == "blueocean_" + "a" * 100
    print("  ok")

    print("== invalid names are rejected, not silently normalized ==")
    bad_names = [
        "Team A",       # spaces + uppercase
        "team/a",       # slash
        "TEAM-A",       # uppercase
        " team-a",      # leading space
        "team-a ",      # trailing space
        "",             # empty
        "-team",        # must start with alnum
        "team a/b",     # space + slash
        "a" * 101,      # over the length ceiling
    ]
    for bad in bad_names:
        try:
            collection_name(bad)
            raise AssertionError(f"expected ValueError for {bad!r}")
        except ValueError as e:
            print(f"  rejected {bad!r}: {e}")

    print("== error message suggests a valid slug ==")
    try:
        collection_name("Team A")
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "team-a" in str(e), f"expected suggested slug 'team-a' in error, got: {e}"
        print(f"  suggestion present: {e}")

    print("\nPROJECT NAME VALIDATION TEST PASSED")


if __name__ == "__main__":
    main()
