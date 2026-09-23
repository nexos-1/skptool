"""Stilregeln fuer das ganze Repository.

Start: .venv\\Scripts\\python -m unittest tests.test_stil -v
"""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".py", ".md", ".txt", ".cmd", ".json", ".toml", ".yml", ".yaml", ".html"}
EM_DASH = chr(0x2014)  # nicht als Zeichen im Quelltext, sonst findet der Test sich selbst


def tracked_text_files():
    """Alle von git verfolgten Textdateien (nach Endung); None, wenn git fehlt oder kein Repository."""
    git = shutil.which("git")
    if not git:
        return None
    try:
        out = subprocess.run([git, "ls-files", "-z"], cwd=ROOT, capture_output=True, timeout=60, check=True).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    names = [n for n in out.decode("utf-8", "replace").split("\0") if n]
    return [ROOT / n for n in names if Path(n).suffix.lower() in TEXT_SUFFIXES]


class TestStil(unittest.TestCase):
    def test_no_em_dash_in_tracked_text_files(self):
        files = tracked_text_files()
        if files is None:
            self.skipTest("git nicht verfuegbar oder kein git-Repository")
        self.assertTrue(files, "keine Textdateien gefunden")
        found = []
        for p in files:
            if not p.is_file():  # geloescht, aber noch nicht committet
                continue
            for no, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if EM_DASH in line:
                    found.append(f"{p.relative_to(ROOT)}:{no}")
        self.assertEqual(found, [], "Langer Gedankenstrich (U+2014) statt '-' in: " + ", ".join(found[:20]))


if __name__ == "__main__":
    unittest.main()
