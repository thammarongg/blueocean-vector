"""The `instructions` field says only things an agent can act on.

`instructions` is a start-of-session channel: it is delivered once, at
handshake, and can never be re-sent. That makes every clause in it expensive,
and a clause agents cannot follow worse than useless -- it teaches them to
skim the block that carries the clauses they *can* follow.

Two findings from the "End-of-session memory reliability" map constrain what
belongs here:

- "BEFORE ENDING a session ... call memory_summarize_session" asked the agent
  to act at a moment it cannot observe. Measured across 21 real sessions: one
  called it, and that one called it 76% of the way through and kept working.
  Removed rather than reworded.
- "WHILE WORKING: memory_store ..." named an importance to use but never a
  moment to write. "While working" is a state, not an event. It needs a
  trigger the agent can actually recognise -- reaching a decision.

Run with:
    uv run python -m tests.instructions
"""

import asyncio
import re

from blueocean_mcp.server import build_server
from blueocean_mcp.vector_store import _PROJECT_NAME_RE, _suggest_slug


def main() -> None:
    instructions = build_server().instructions or ""
    assert instructions, "server shipped with no instructions at all"

    print("== no clause asks the agent to act at an ending ==")
    for dead in ("BEFORE ENDING", "before ending"):
        assert dead not in instructions, (
            f"instructions still contain {dead!r}: the agent is never present "
            "at an ending, so this clause cannot be followed"
        )
    print("  the end-of-session clause is gone")

    print("== writing is triggered by a decision, not by a state ==")
    lowered = instructions.lower()
    assert "decide" in lowered or "decision" in lowered, (
        "no clause ties writing to reaching a decision -- without an "
        "observable trigger this is the 'while working' failure again"
    )
    print("  a decision-shaped trigger is present")

    print("== the start-of-session clause survives ==")
    assert "memory_manifest" in instructions
    assert "memory_search" in instructions
    print("  manifest + search still instructed at the start")

    print("== the directory name is turned into a project name the server accepts ==")
    # The server rejects names like `Projects`, so "use the directory's name"
    # alone sends agents a name that fails on their first call.
    start = instructions.index("AT THE START")
    clause = instructions[start : instructions.index("\n\n", start)]
    assert "directory's name" in clause, "the directory-name rule is gone"
    for phrase in ("lowercase", "a-z", "0-9", "collapse", "trim"):
        assert phrase in clause, (
            f"the start-of-session clause does not state the project-name rule "
            f"(missing {phrase!r})"
        )
    examples = re.findall(r"`([^`]+)` -> `([^`]+)`", clause)
    assert ("Projects", "projects") in examples, f"missing `Projects` -> `projects`: {examples}"
    assert ("My App", "my-app") in examples, f"missing `My App` -> `my-app`: {examples}"
    for folder, project in examples:
        assert _suggest_slug(folder) == project, (
            f"instructions say {folder!r} -> {project!r}, "
            f"_suggest_slug gives {_suggest_slug(folder)!r}"
        )
        assert _PROJECT_NAME_RE.match(project), f"{project!r} is not a valid project name"
    print(f"  conversion rule stated; examples match _suggest_slug: {examples}")

    print("== storing as you go is still instructed ==")
    assert "memory_store" in instructions
    print("  memory_store still instructed")

    print("== summarising is offered, never demanded at an ending ==")
    if "memory_summarize_session" in instructions:
        idx = instructions.index("memory_summarize_session")
        window = instructions[max(0, idx - 200) : idx]
        assert "ENDING" not in window.upper(), (
            "memory_summarize_session is still tied to an ending"
        )
    print("  no ending-conditioned summarise demand")

    check_tool_descriptions()

    print("\nAll instruction checks passed.")


def check_tool_descriptions() -> None:
    """Under 2026-07-28 the instructions arrive only in server/discover, which
    a client MAY skip (the SDK's own pinned-version mode does). Every client
    calls tools/list, so the two triggers an agent can act on live in the
    descriptions too."""
    tools = {t.name: (t.description or "") for t in asyncio.run(build_server().list_tools())}
    manifest = tools["memory_manifest"].lower()
    assert "start of a session" in manifest, (
        "memory_manifest's description must say to call it at the start of a session"
    )
    store = tools["memory_store"].lower()
    assert "decided" in store and "right then" in store, (
        "memory_store's description must carry the save-as-you-go trigger: "
        "write right then, when something is decided"
    )
    assert "ending" not in store and "before ending" not in manifest, (
        "issue #10: an ending is not a moment an agent can act on"
    )
    print("  tool descriptions carry the start-of-session and decision triggers")


if __name__ == "__main__":
    main()
