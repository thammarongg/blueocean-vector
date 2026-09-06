"""`install_skill.sh` can seed the canonical skill, not just link to it.

The skill's text is what every agent on a machine reads before touching
memory, but it lived only at `~/.agents/skills/blueocean-memory/SKILL.md` --
outside this repo, outside any git repo. `install_skill.sh` linked to that
path and refused to run when it was missing, so a fresh clone could not
install the skill at all, and the content had no version control.

The repo now carries the canonical copy under `skills/`, and the installer
seeds `~/.agents` from it when nothing is there yet. It must NOT overwrite a
canonical file that already exists: a machine may have local edits, and
silently clobbering them would be the same class of data loss the memory
store is careful about.

Run with:
    uv run python -m tests.install_skill
"""

import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "install_skill.sh"
REPO_SKILL = REPO_ROOT / "skills" / "blueocean-memory" / "SKILL.md"

CANONICAL_REL = ".agents/skills/blueocean-memory/SKILL.md"
LINK_REL = ".claude/skills/blueocean-memory"


def run(home: Path, *args: str) -> subprocess.CompletedProcess:
    # check=False: the assertions below inspect returncode themselves, and
    # give a far better message than CalledProcessError would.
    return subprocess.run(
        [str(SCRIPT), *args],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        capture_output=True,
        text=True,
        check=False,
    )


def main() -> None:
    print("== the repo carries the canonical skill ==")
    assert REPO_SKILL.is_file(), f"missing {REPO_SKILL.relative_to(REPO_ROOT)}"
    repo_text = REPO_SKILL.read_text()
    assert repo_text.startswith("---"), "skill has no YAML frontmatter"
    assert "name: blueocean-memory" in repo_text
    print(f"  {REPO_SKILL.relative_to(REPO_ROOT)} ({len(repo_text)} chars)")

    print("== a fresh machine gets the canonical file seeded from the repo ==")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        (home / ".claude").mkdir()  # pretend Claude Code is installed

        result = run(home, "claude")
        assert result.returncode == 0, (
            f"installer failed on a machine with no canonical skill:\n"
            f"{result.stdout}\n{result.stderr}"
        )

        canonical = home / CANONICAL_REL
        assert canonical.is_file(), (
            "installer did not seed the canonical skill; it still only links "
            f"to a file it cannot create.\n{result.stdout}\n{result.stderr}"
        )
        assert canonical.read_text() == repo_text, (
            "seeded canonical file does not match the repo copy"
        )
        print(f"  seeded {CANONICAL_REL}")

        link = home / LINK_REL
        assert link.is_symlink(), f"{LINK_REL} is not a symlink"
        assert (link / "SKILL.md").read_text() == repo_text, (
            "the tool's symlink does not resolve to the seeded skill"
        )
        print(f"  linked {LINK_REL} -> resolves to the seeded skill")

    print("== an existing canonical file is never overwritten ==")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        (home / ".claude").mkdir()
        canonical = home / CANONICAL_REL
        canonical.parent.mkdir(parents=True)
        local_edit = "---\nname: blueocean-memory\n---\n\nLOCAL EDIT, DO NOT CLOBBER\n"
        canonical.write_text(local_edit)

        result = run(home, "claude")
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert canonical.read_text() == local_edit, (
            "installer overwrote a canonical skill that already existed -- "
            "local edits must survive"
        )
        print("  local edits survived")

    print("== --list still works on a fresh machine ==")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        result = run(home, "--list")
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert "blueocean-memory" in result.stdout
        print("  --list ok")

    print("== the shipped copy matches what this machine actually serves ==")
    live = Path.home() / CANONICAL_REL
    if live.is_file():
        if live.read_text() == repo_text:
            print("  this machine's canonical skill matches the repo copy")
        else:
            print("  NOTE: this machine's canonical skill differs from the repo copy")
    else:
        print("  (no canonical skill on this machine to compare)")

    print("\nAll install_skill checks passed.")


if __name__ == "__main__":
    main()
