"""Doku gegen den echten Stand pruefen: Schalter, Links, Beispiele, Stil.

Start: .venv\\Scripts\\python -m unittest tests.test_doku -v

- Jeder Schalter (--xyz) in Backticks in README.md und README.en.md gibt es wirklich, in einem
  Beispiel "skptool <befehl> ..." sogar genau bei diesem Befehl.
- Relative Links und Bilder in allen Markdown-Dateien zeigen auf vorhandene Dateien, Anker auf
  vorhandene Ueberschriften (nach GitHub-Regeln).
- Kein langer Gedankenstrich (U+2014) in einer Markdown-Datei.
- Beide READMEs zeigen dieselben skptool-Beispiele (Befehl und Schalter, Dateinamen duerfen
  uebersetzt sein) und verweisen aufeinander.
"""
import re
import shutil
import subprocess
import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from skptool import cli  # noqa: E402

READMES = ("README.md", "README.en.md")
EM_DASH = chr(0x2014)  # nicht als Zeichen im Quelltext, sonst findet test_stil diese Datei
FLAG = re.compile(r"(?<![\w-])(--?[A-Za-z][\w-]*)")
LONG_FLAG = re.compile(r"(?<![\w-])(--[A-Za-z][\w-]*)")
EXAMPLE = re.compile(r"(?:^|[\s|(\"'])skptool\s+([a-z]+)\b([^|]*)")
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
FENCE = re.compile(r"^\s*```")


def markdown_files():
    """Alle Markdown-Dateien des Repositorys (per git, sonst die im Hauptordner und in docs/)."""
    git = shutil.which("git")
    if git:
        try:
            out = subprocess.run([git, "ls-files", "-z", "--", "*.md"], cwd=ROOT, capture_output=True,
                                 timeout=60, check=True).stdout
            files = [ROOT / n for n in out.decode("utf-8", "replace").split("\0") if n]
            files += [ROOT / n for n in READMES + ("SECURITY.en.md",)]  # neu, evtl. noch nicht committet
            return sorted({p for p in files if p.is_file()})
        except (subprocess.SubprocessError, OSError):
            pass
    return sorted(set(ROOT.glob("*.md")) | set((ROOT / "docs").glob("*.md")))


def split_markdown(text):
    """Liefert (Codezeilen aus ```-Bloecken, Inline-Code-Spannen, Text ohne Code)."""
    fenced, prose, inside = [], [], False
    for line in text.splitlines():
        if FENCE.match(line):
            inside = not inside
            continue
        (fenced if inside else prose).append(line)
    prose_text = "\n".join(prose)
    spans = re.findall(r"`([^`\n]+)`", prose_text)
    return fenced, spans, re.sub(r"`[^`\n]+`", "", prose_text)


def parser_flags():
    """{befehl: Menge der Schalter} aus dem echten argparse-Parser von skptool."""
    ap = cli.build_parser()
    out = {"": set(ap._option_string_actions)}
    for action in ap._actions:
        if hasattr(action, "choices") and isinstance(action.choices, dict):
            for name, sub in action.choices.items():
                out[name] = set(sub._option_string_actions)
    return out


def tool_flags():
    """Schalter der Hilfsskripte in tools/ (dort baut main() den Parser erst beim Aufruf)."""
    found = set()
    for py in (ROOT / "tools").glob("*.py"):
        found |= set(re.findall(r"add_argument\(\s*\"(--[\w-]+)\"", py.read_text(encoding="utf-8")))
    return found


def examples(text):
    """skptool-Beispiele als (befehl, sortierte Schalter), aus Codebloecken und Inline-Code."""
    fenced, spans, _ = split_markdown(text)
    found = []
    for chunk in fenced + spans:
        for m in EXAMPLE.finditer(chunk):
            found.append((m.group(1), tuple(sorted(FLAG.findall(m.group(2))))))
    return found


def github_anchors(text):
    """Anker, die GitHub fuer die Ueberschriften einer Markdown-Datei vergibt."""
    anchors, seen = set(), Counter()
    fenced_free = []
    inside = False
    for line in text.splitlines():
        if FENCE.match(line):
            inside = not inside
            continue
        if not inside:
            fenced_free.append(line)
    for line in fenced_free:
        m = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if not m:
            continue
        title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", m.group(1))  # Links: nur der Text
        slug = re.sub(r"[^\w\- ]", "", title.replace("`", "").strip().lower()).replace(" ", "-")
        anchors.add(slug if not seen[slug] else f"{slug}-{seen[slug]}")
        seen[slug] += 1
    return anchors


class TestDoku(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.texts = {name: (ROOT / name).read_text(encoding="utf-8") for name in READMES}
        cls.flags = parser_flags()
        cls.all_flags = set().union(*cls.flags.values()) | tool_flags()

    def test_backtick_flags_exist(self):
        wrong = []
        for name, text in self.texts.items():
            _, spans, _ = split_markdown(text)
            for span in spans:
                for flag in LONG_FLAG.findall(span):
                    if flag not in self.all_flags:
                        wrong.append(f"{name}: {flag} in `{span}`")
        self.assertEqual(wrong, [], "Schalter gibt es nicht: " + "; ".join(wrong))

    def test_example_flags_belong_to_their_command(self):
        wrong = []
        for name, text in self.texts.items():
            for cmd, flags in examples(text):
                if cmd not in self.flags:
                    wrong.append(f"{name}: unbekannter Befehl skptool {cmd}")
                    continue
                wrong += [f"{name}: skptool {cmd} kennt {f} nicht" for f in flags if f not in self.flags[cmd]]
        self.assertEqual(wrong, [], "; ".join(wrong))

    def test_readmes_show_same_examples(self):
        de, en = (Counter(examples(self.texts[n])) for n in READMES)
        self.assertTrue(de, "keine skptool-Beispiele gefunden")
        self.assertEqual(de - en, Counter(), "nur in README.md")
        self.assertEqual(en - de, Counter(), "nur in README.en.md")

    def test_language_switch(self):
        for a, b in (("README.md", "README.en.md"), ("SECURITY.md", "SECURITY.en.md")):
            for src, dst in ((a, b), (b, a)):
                head = (ROOT / src).read_text(encoding="utf-8").splitlines()[:5]
                self.assertTrue(any(f"]({dst})" in line for line in head),
                                f"{src}: Sprachumschalter auf {dst} fehlt oben")

    def test_relative_links_resolve(self):
        broken = []
        for md in markdown_files():
            text = md.read_text(encoding="utf-8")
            _, _, prose = split_markdown(text)
            for target in LINK.findall(prose):
                if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I):  # https:, mailto: ...
                    continue
                path, _, anchor = target.partition("#")
                dest = (md.parent / path).resolve() if path else md
                rel = md.relative_to(ROOT)
                if not dest.exists():
                    broken.append(f"{rel}: {target} (Datei fehlt)")
                    continue
                if anchor and dest.suffix.lower() == ".md":
                    if anchor not in github_anchors(dest.read_text(encoding="utf-8")):
                        broken.append(f"{rel}: {target} (Anker fehlt)")
        self.assertEqual(broken, [], "; ".join(broken))

    def test_no_em_dash_in_markdown(self):
        found = []
        for md in markdown_files():
            for no, line in enumerate(md.read_text(encoding="utf-8").splitlines(), 1):
                if EM_DASH in line:
                    found.append(f"{md.relative_to(ROOT)}:{no}")
        self.assertEqual(found, [], "Langer Gedankenstrich (U+2014) in: " + ", ".join(found))

    def test_selfcheck_helpers(self):
        """Die Hilfsfunktionen erkennen Fehler wirklich (sonst waeren die Tests oben wertlos)."""
        self.assertEqual(examples("```\nskptool diff a.skp b.skp --geometrie -q\n```\n"),
                         [("diff", ("--geometrie", "-q"))])
        self.assertEqual(examples("`Get-Content x -Raw | skptool edit h.skp -o n.skp --ops -`"),
                         [("edit", ("--ops", "-o"))])
        self.assertIn("für-ki-assistenten-mcp", github_anchors("### Für KI-Assistenten (MCP)\n"))
        self.assertIn("sicherheit", github_anchors("## Sicherheit\n"))
        self.assertNotIn("--gibt-es-nicht", self.all_flags)
        self.assertIn("--export-skp", self.flags["open"])


if __name__ == "__main__":
    unittest.main()
