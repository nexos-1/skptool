"""--ops - (Operationen aus stdin) fuer edit und live.

Start: .venv\\Scripts\\python -m unittest tests.test_opsjson -v
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from skptool import core, opsjson
from skptool.blender import BlenderError, find_blender
from skptool.opsjson import MAX_BYTES, load_ops

ROOT = Path(__file__).resolve().parents[1]
S2017 = ROOT / "samples" / "stuhl_tisch_2017.skp"

try:
    find_blender()
    HAVE_BLENDER = True
except BlenderError:
    HAVE_BLENDER = False


def _skptool(*args, stdin: bytes = b""):
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, "-m", "skptool", *args], input=stdin, capture_output=True,
                          cwd=ROOT, env=env, timeout=600)


class TestOpsStdin(unittest.TestCase):
    def test_reads_list_and_single_op(self):
        ops = [{"op": "move", "select": {"name": "Stuhl*"}, "by": [0, 0, 1]}]
        self.assertEqual(load_ops("-", stdin=io.BytesIO(json.dumps(ops).encode())), ops)
        self.assertEqual(load_ops(" - ", stdin=io.BytesIO(b'\xef\xbb\xbf{"op": "summary"}\r\n')),
                         [{"op": "summary"}])  # BOM und CRLF wie aus PowerShell
        self.assertEqual(load_ops("-", stdin=io.BytesIO(b'\xef\xbb\xbf\xef\xbb\xbf[{"op": "summary"}]\r\n')),
                         [{"op": "summary"}])  # PowerShell 5.1 mit $OutputEncoding = UTF8: BOM doppelt
        umlaut = "St\xfchle"  # als Escape, der Quelltext bleibt ASCII
        self.assertEqual(load_ops("-", stdin=io.BytesIO(json.dumps({"op": "list", "select": {"name": umlaut}},
                                                                   ensure_ascii=False).encode()))[0]["select"]["name"],
                         umlaut)

    def test_same_strict_rules_as_text(self):
        for raw in (b"", b"  \n", b"{kein json", b'[{"op": "move", "by": [NaN, 0, 0]}]', b"[1, 2]",
                    b'"move"', b"[" * 100000):
            with self.subTest(raw=raw[:30]), self.assertRaises(SystemExit):
                load_ops("-", stdin=io.BytesIO(raw))
        too_many = json.dumps([{"op": "summary"}] * (opsjson.MAX_OPS + 1)).encode()
        with self.assertRaises(SystemExit):
            load_ops("-", stdin=io.BytesIO(too_many))

    def test_size_limit_reads_at_most_max_plus_one(self):
        class Stream(io.BytesIO):
            asked = []

            def read(self, n=-1):
                Stream.asked.append(n)
                return super().read(n)

        big = b'[{"op": "summary", "x": "' + b"a" * MAX_BYTES + b'"}]'
        with self.assertRaises(SystemExit) as cm:
            load_ops("-", stdin=Stream(big))
        self.assertIn("groesser", str(cm.exception.code))
        self.assertEqual(Stream.asked, [MAX_BYTES + 1])

    def test_real_stdin_is_read_once(self):
        # convert mit mehreren Eingaben ruft load_ops je Datei auf: stdin nur einmal lesen
        old_text, old_stdin = opsjson._stdin_text, sys.stdin
        fake = io.TextIOWrapper(io.BytesIO(b'{"op": "summary"}'), encoding="utf-8")
        try:
            opsjson._stdin_text, sys.stdin = None, fake
            self.assertEqual(load_ops("-"), [{"op": "summary"}])
            self.assertEqual(load_ops("-"), [{"op": "summary"}])
        finally:
            opsjson._stdin_text, sys.stdin = old_text, old_stdin

    def test_cli_edit_and_live_reject_bad_stdin_before_blender(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "x.skp"
            res = _skptool("edit", str(S2017), "-o", str(out), "--ops", "-", "-q", stdin=b"{kein json")
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("kein gueltiges JSON", res.stderr.decode("utf-8", "replace"))
            self.assertFalse(out.exists())
        res = _skptool("live", "--ops", "-", stdin=b"")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("stdin ist leer", res.stderr.decode("utf-8", "replace"))

    @unittest.skipUnless(HAVE_BLENDER, "Blender nicht installiert")
    def test_cli_edit_reads_ops_from_stdin(self):
        ops = [{"op": "add_box", "name": "Stdin_Box", "size": 0.3, "at": [-1, 0, 0], "layer": "AusStdin"}]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "x.skp"
            res = _skptool("edit", str(S2017), "-o", str(out), "--ops", "-", "-q",
                           stdin=json.dumps(ops).encode("utf-8"))
            self.assertEqual(res.returncode, 0, res.stderr.decode("utf-8", "replace"))
            m = core.model_of(core.open_skp(out))
            self.assertIn("AusStdin", [l.name for l in m.layers])
            self.assertEqual(core.placed_face_count(m), 128 + 6)


if __name__ == "__main__":
    unittest.main()
