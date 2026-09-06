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

from blueocean_mcp.server import build_server


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

    print("\nAll instruction checks passed.")


if __name__ == "__main__":
    main()
