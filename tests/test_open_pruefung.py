"""skptool open: alle Ablehnungen kommen, bevor eine Datei geschrieben wird. Ohne Blender-Fenster.

Start: .venv\\Scripts\\python -m unittest tests.test_open_pruefung -v

Fehler vorher: skptool open x.skp --live wandelte erst um (x.blend ueberschrieben) und lehnte danach ab,
weil x_bearbeitet.skp schon existierte. Ausserdem ueberschrieb open eine vorhandene x.blend still, auch
wenn sie neuer war als x.skp (Aenderungen aus Blender weg). Die Umwandlung und der Fensterstart sind
hier ersetzt: sie duerfen bei einer Ablehnung gar nicht erst aufgerufen werden.
"""
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from skptool import live

from tests.test_diff import run_cli

ROOT = Path(__file__).resolve().parents[1]
S2017 = ROOT / "samples" / "stuhl_tisch_2017.skp"
KEIN_BLENDER = "C:/gibt/es/nicht/blender.exe" if os.name == "nt" else "/gibt/es/nicht/blender"


def zustand(ordner: Path) -> dict:
    """Dateiname -> (Groesse, mtime_ns, Inhalt), um jede Aenderung zu sehen."""
    return {p.name: (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes()) for p in sorted(ordner.iterdir())}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="skptool_open_"))
        self.work = self.tmp / "arbeit"
        self.work.mkdir()
        self.skp = self.work / "x.skp"
        shutil.copyfile(S2017, self.skp)
        self.blend = self.work / "x.blend"
        state_dir = self.tmp / "status"
        state_dir.mkdir(mode=0o700)
        self.state = state_dir / "live.json"
        env = mock.patch.dict(os.environ, {live.STATE_ENV: str(self.state)})
        env.start()
        self.addCleanup(env.stop)
        # Umwandlung und Fenster ersetzen: aufgerufen werden duerfen sie nur, wenn alle Pruefungen durch sind
        self.umgewandelt = []

        def fake_convert(a):
            self.umgewandelt.append(Path(a.output))
            return 0

        for ziel, ersatz in (("skptool.cli.cmd_convert", fake_convert),
                             ("skptool.cli.launch_gui", mock.MagicMock()),
                             ("skptool.live.open_live", mock.MagicMock(return_value=0))):
            p = mock.patch(ziel, ersatz)
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def open_(self, *args):
        return run_cli("open", str(self.skp), "--blender", KEIN_BLENDER, *args)

    def abgelehnt(self, args, text, vorher=None):
        vorher = vorher if vorher is not None else zustand(self.work)
        code, out, err = self.open_(*args)
        self.assertEqual(code, 1, out + err)
        self.assertIn(text, err)
        self.assertEqual(self.umgewandelt, [], "Umwandlung trotz Ablehnung gestartet")
        self.assertEqual(zustand(self.work), vorher, "Dateien trotz Ablehnung angelegt oder geaendert")
        return err

    def blend_mit_zeit(self, delta_s):
        self.blend.write_bytes(b"BLENDER-v500 in Blender bearbeitet")
        t = self.skp.stat().st_mtime + delta_s
        os.utime(self.blend, (t, t))


class TestLiveAblehnungen(Base):
    def test_existing_export_target_refused_before_anything_is_written(self):
        (self.work / "x_bearbeitet.skp").write_bytes(b"alter Export")
        err = self.abgelehnt(["--live"], "x_bearbeitet.skp gibt es schon")
        self.assertIn("Nichts geschrieben", err)
        self.assertFalse(self.blend.exists())

    def test_export_target_is_source_refused(self):
        self.abgelehnt(["--live", "--export-skp", str(self.skp)], "darf nicht die Originaldatei sein")

    def test_export_target_is_the_blend_or_not_skp_refused(self):
        self.abgelehnt(["--live", "--export-skp", str(self.blend)], "braucht eine .skp-Datei")
        self.abgelehnt(["--live", "--export-skp", str(self.work / "y.glb")], "braucht eine .skp-Datei")

    def test_running_live_session_refused_before_conversion(self):
        """Auf dem Port der Statusdatei antwortet etwas: Ablehnung ohne Umwandlung."""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        port = srv.getsockname()[1]

        def antworten():
            srv.settimeout(15)
            try:
                while True:
                    conn, _ = srv.accept()
                    with conn:
                        conn.sendall(b'{"kein": "skptool"}\n')
            except OSError:
                pass

        t = threading.Thread(target=antworten, daemon=True)
        t.start()
        self.addCleanup(srv.close)
        fd = os.open(self.state, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write('{"port": %d, "token": "%s", "pid": 1}' % (port, "a" * 32))
        self.abgelehnt(["--live"], f"Auf Port {port} laeuft noch etwas")
        self.assertFalse(self.blend.exists())

    def test_live_with_newer_blend_refused(self):
        self.blend_mit_zeit(+60)
        self.abgelehnt(["--live"], "ist neuer als x.skp")


class TestVorhandeneBlend(Base):
    def test_newer_blend_is_not_overwritten(self):
        self.blend_mit_zeit(+60)
        err = self.abgelehnt([], "ist neuer als x.skp")
        self.assertIn("Nichts geschrieben", err)
        self.assertIn(f'skptool open "{self.blend}"', err)
        self.assertIn("--force", err)
        self.assertEqual(self.blend.read_bytes(), b"BLENDER-v500 in Blender bearbeitet")

    def test_newer_blend_with_force_is_regenerated(self):
        self.blend_mit_zeit(+60)
        code, out, err = self.open_("--force")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.umgewandelt, [self.blend])
        self.assertIn(f"Erzeuge {self.blend} neu aus x.skp (--force: die vorhandene, neuere .blend wird ersetzt)", out)
        self.assertIn(f"Blender startet mit {self.blend}", out)

    def test_older_blend_is_regenerated_and_named(self):
        self.blend_mit_zeit(-60)
        code, out, err = self.open_()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.umgewandelt, [self.blend])
        self.assertIn(f"Erzeuge {self.blend} neu aus x.skp (die vorhandene .blend ist aelter als x.skp)", out)

    def test_new_blend_names_the_file(self):
        code, out, err = self.open_()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.umgewandelt, [self.blend])
        self.assertIn(f"Erzeuge {self.blend} aus x.skp", out)

    def test_output_newer_blend_refused_too(self):
        anders = self.work / "anders.blend"
        anders.write_bytes(b"bearbeitet")
        t = time.time() + 60
        os.utime(anders, (t, t))
        self.abgelehnt(["-o", str(anders)], "ist neuer als x.skp")

    def test_output_must_be_blend(self):
        self.abgelehnt(["-o", str(self.work / "x.glb")], "-o muss eine .blend-Datei sein")

    def test_blend_input_with_ops_refused(self):
        self.blend_mit_zeit(0)
        vorher = zustand(self.work)
        code, out, err = run_cli("open", str(self.blend), "--blender", KEIN_BLENDER,
                                 "--ops", '[{"op": "summary"}]')
        self.assertEqual(code, 1)
        self.assertIn("--ops geht beim Oeffnen nur beim Umwandeln", err)
        self.assertEqual(zustand(self.work), vorher)


if __name__ == "__main__":
    unittest.main()
