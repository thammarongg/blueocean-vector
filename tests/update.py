"""Update in disposable checkouts; Docker and uv are isolated test doubles.

Run with: python3 -m tests.update
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.repo = base / "checkout"
        self.home = base / "home"
        self.home.mkdir()
        self.bin = base / "bin"
        self.bin.mkdir()
        self.env = {**os.environ, "HOME": str(self.home),
                    "PATH": f"{self.bin}:{os.environ['PATH']}",
                    "UPDATE_LOG": str(base / "commands")}
        self.log = base / "commands"
        upstream = base / "upstream"
        upstream.mkdir()
        self.git(upstream, "init", "-q")
        (upstream / "scripts").mkdir()
        script = ROOT / "scripts/update.sh"
        if script.exists():
            shutil.copy2(script, upstream / "scripts/update.sh")
        skill = upstream / "skills/blueocean-memory/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("repository skill\n")
        self.git(upstream, "add", ".")
        self.git(upstream, "-c", "user.name=Test", "-c", "user.email=test@example.com",
                 "commit", "-qm", "initial")
        self.git(base, "clone", "-q", str(upstream), str(self.repo))
        # A subsequent commit proves the updater pulls before copying the skill.
        skill.write_text("updated repository skill\n")
        self.git(upstream, "add", ".")
        self.git(upstream, "-c", "user.name=Test", "-c", "user.email=test@example.com",
                 "commit", "-qm", "update skill")
        for command in ("docker", "uv"):
            stub = self.bin / command
            stub.write_text('#!/bin/bash\n'
                            'if [ "$*" = "compose version" ]; then exit 0; fi\n'
                            f'echo "{command} $*" >> "$UPDATE_LOG"\n'
                            f'[ "${{FAIL_COMMAND:-}}" != "{command}" ]\n')
            stub.chmod(0o755)
        self.canonical = self.home / ".agents/skills/blueocean-memory/SKILL.md"
        self.canonical.parent.mkdir(parents=True)

    def git(self, cwd, *args):
        subprocess.run(["git", *args], cwd=cwd, env=self.env,
                       check=True, capture_output=True)

    def run_update(self, *args):
        return subprocess.run(["bash", str(self.repo / "scripts/update.sh"), *args],
                              cwd=self.home, env=self.env, capture_output=True, text=True,
                              check=False)

    def test_changed_skill_is_backed_up_and_visible_through_symlink(self):
        self.canonical.write_text("local customizations\n")
        link = self.home / "agent-skill"
        link.symlink_to(self.canonical.parent, target_is_directory=True)
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((link / "SKILL.md").read_text(), "updated repository skill\n")
        backups = list(self.canonical.parent.glob("SKILL.md.bak.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "local customizations\n")
        commands = self.log.read_text()
        self.assertIn("docker compose up -d --build --wait", commands)
        self.assertIn("uv sync --extra dev", commands)

    def test_identical_skill_is_not_rewritten(self):
        self.canonical.write_text("updated repository skill\n")
        before = self.canonical.stat().st_mtime_ns
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.canonical.stat().st_mtime_ns, before)
        self.assertEqual(list(self.canonical.parent.glob("SKILL.md.bak.*")), [])

    def test_missing_skill_is_installed_and_stdio_skips_docker(self):
        result = self.run_update("--stdio")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.canonical.read_text(), "updated repository skill\n")
        self.assertNotIn("docker", self.log.read_text())

    def test_dirty_checkout_stops_before_updating(self):
        (self.repo / "skills/blueocean-memory/SKILL.md").write_text("uncommitted")
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())
        self.assertFalse(self.canonical.exists())

    def test_pull_failure_stops_before_updating(self):
        self.git(self.repo, "remote", "set-url", "origin", str(self.repo / "missing"))
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())
        self.assertFalse(self.canonical.exists())

    def test_build_failure_preserves_installed_skill(self):
        self.canonical.write_text("old skill")
        self.env["FAIL_COMMAND"] = "docker"
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.canonical.read_text(), "old skill")

    def test_dependency_failure_preserves_skill_and_does_not_rebuild(self):
        self.canonical.write_text("old skill")
        self.env["FAIL_COMMAND"] = "uv"
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.canonical.read_text(), "old skill")
        self.assertNotIn("docker compose up", self.log.read_text())


if __name__ == "__main__":
    unittest.main()
