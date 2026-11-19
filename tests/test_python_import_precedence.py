"""Regression coverage for Python 3.14 stdlib import precedence and engine refresh."""
from __future__ import annotations

from pathlib import Path
from unittest import TestCase, mock
import tempfile

import spore_agentic

ROOT = Path(__file__).resolve().parents[1]


class StdlibImportPrecedenceTests(TestCase):
    def test_spore_helper_search_path_does_not_shadow_stdlib(self):
        # Upstream work/scratch/dis.py must not shadow Python's standard dis
        # while argparse (via _colorize) initializes on Python 3.14.
        for path in (
            ROOT / "engine" / "expand_queue.py",
            ROOT / "engine" / "spore_provider_fix" / "spore_parallel_sqlite.py",
        ):
            with self.subTest(path=path):
                code = path.read_text(encoding="utf-8")
                self.assertIn("sys.path.append(", code)
                self.assertNotIn("sys.path.insert(0", code)

    def test_install_command_deploys_scripts_and_preserves_existing_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            existing_prompt = repo / "work/scratch/spore_first_pass_prompt.txt"
            existing_prompt.parent.mkdir(parents=True)
            existing_prompt.write_text("my custom prompt\n", encoding="utf-8")
            outdated = repo / "spore_parallel_sqlite.py"
            outdated.write_text("old version\n", encoding="utf-8")
            with mock.patch.object(spore_agentic, "ensure_stopped"):
                # Exercise the installer directly; the CLI's repo validator
                # checks for a real upstream checkout, which this fixture is not.
                spore_agentic.install(repo)
            self.assertEqual(existing_prompt.read_text(), "my custom prompt\n")
            self.assertIn("sys.path.append(", outdated.read_text())
            backup_root = repo / "work/scratch/codex-pass1/sqlite-backups"
            backups = sorted(backup_root.glob("agentic-scripts-*/spore_parallel_sqlite.py"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(), "old version\n")
