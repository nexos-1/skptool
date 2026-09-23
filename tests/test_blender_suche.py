"""Reihenfolge der gefundenen Blender-Installationen (skptool.blender._version_key), ohne Dateizugriff.

Start: .venv\\Scripts\\python -m unittest tests.test_blender_suche -v
"""
import os
import unittest
from unittest import mock

from skptool import blender


def _best(cands):
    return sorted(cands, key=blender._version_key)[-1]


class TestBlenderReihenfolge(unittest.TestCase):
    def test_posix_install_locations_beat_user_folders(self):
        with mock.patch.object(blender.os, "name", "posix"), \
                mock.patch.dict(os.environ, {"ProgramFiles": "", "ProgramFiles(x86)": ""}):
            for protected in ("/usr/bin/blender", "/usr/local/bin/blender", "/opt/blender-4.2/blender",
                              "/snap/bin/blender", "/Applications/Blender.app/Contents/MacOS/Blender"):
                self.assertTrue(blender._version_key(protected)[0], protected)
                # hoehere Versionsnummer im Benutzerordner gewinnt trotzdem nicht
                self.assertEqual(_best(["/home/eve/blender-9.9/blender", protected]), protected)
            for loose in ("/home/eve/blender-5.2/blender", "/tmp/blender", "/optx/blender",
                          "/usr/binx/blender", "/Users/eve/Applications/Blender.app/Contents/MacOS/Blender"):
                self.assertFalse(blender._version_key(loose)[0], loose)
            # innerhalb der geschuetzten Orte die hoechste Version
            self.assertEqual(_best(["/opt/blender-5.2/blender", "/opt/blender-4.2/blender"]),
                             "/opt/blender-5.2/blender")

    def test_posix_locations_not_protected_on_windows(self):
        # unter Windows koennte jeder Benutzer C:\opt oder C:\usr\bin anlegen
        with mock.patch.object(blender.os, "name", "nt"), \
                mock.patch.dict(os.environ, {"ProgramFiles": "", "ProgramFiles(x86)": ""}):
            self.assertFalse(blender._version_key("/opt/blender-5.2/blender")[0])
            self.assertFalse(blender._version_key("/usr/bin/blender")[0])

    @unittest.skipUnless(os.name == "nt", "Windows-Pfade")
    def test_program_files_beats_localappdata(self):
        env = {"ProgramFiles": r"C:\Program Files", "ProgramFiles(x86)": r"C:\Program Files (x86)"}
        pf = r"C:\Program Files\Blender Foundation\Blender 4.2\blender.exe"
        pf_new = r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe"
        user = r"C:\Users\eve\AppData\Local\Blender Foundation\Blender 9.9\blender.exe"
        fake = r"C:\Program Files Evil\Blender Foundation\Blender 9.9\blender.exe"
        with mock.patch.dict(os.environ, env):
            self.assertEqual(_best([user, pf]), pf)
            self.assertEqual(_best([pf, user, pf_new]), pf_new)
            self.assertFalse(blender._version_key(fake)[0])
            self.assertTrue(blender._version_key(pf.lower())[0])  # Gross/Klein egal


class TestZielformate(unittest.TestCase):
    def test_unsupported_target_refused_before_blender(self):
        # Blender 5 hat kein Collada mehr: .dae (und alles andere Unbekannte) gar nicht erst starten
        import io
        import tempfile
        from contextlib import redirect_stderr, redirect_stdout
        from pathlib import Path

        from skptool import cli
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "quelle.blend"
            src.write_bytes(b"BLENDER-v500 kein echtes Blend")
            for ext in (".dae", ".dxf", ".3ds"):
                out = Path(tmp) / f"ziel{ext}"
                with self.subTest(ext=ext), mock.patch.object(cli, "run_bridge") as bridge, \
                        redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as cm:
                        cli.main(["convert", str(src), "-o", str(out), "-q"])
                    self.assertIn(f"Zielformat {ext} wird nicht unterstuetzt", str(cm.exception.code))
                    bridge.assert_not_called()
                    self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()


class TestKeinePosixPfadeUnterWindows(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "nur unter Windows sinnvoll")
    def test_usr_bin_is_not_searched_on_windows(self):
        from skptool import blender as bl
        gesehen = []
        with mock.patch.object(bl.glob, "glob", side_effect=lambda m: gesehen.append(m) or []),                 mock.patch.dict(os.environ, {"SKPTOOL_BLENDER": ""}),                 mock.patch.object(bl, "_on_path", return_value=None):
            with self.assertRaises(bl.BlenderError):
                bl.find_blender()
        self.assertFalse([m for m in gesehen if m.startswith(("/usr", "/snap", "/Applications"))], gesehen)

