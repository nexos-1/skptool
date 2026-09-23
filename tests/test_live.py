"""Tests fuer den Live-Modus (skptool.live + blender_scripts/live_server.py).

Start: .venv\\Scripts\\python -m unittest tests.test_live -v

Die Live-Tests starten ein echtes Blender MIT Fenster (klein, ohne Fokus), weil bpy.app.timers
nur mit laufender Oberflaeche arbeiten. Am Ende wird Blender ueber den quit-Befehl beendet und
notfalls hart abgebrochen. Ohne Blender werden sie uebersprungen.
SKPTOOL_SKIP_GUI_TESTS=1 ueberspringt sie ebenfalls (z. B. auf Rechnern ohne Bildschirm).
"""
import io
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from skptool import live
from skptool.blender import BlenderError, find_blender

ROOT = Path(__file__).resolve().parents[1]

try:
    BLENDER = find_blender()
except BlenderError:
    BLENDER = None
GUI_OK = BLENDER is not None and not os.environ.get("SKPTOOL_SKIP_GUI_TESTS")

MAKE_BLEND = r"""
import bpy, sys
bpy.ops.wm.read_factory_settings(use_empty=True)
for name, x in (("Wuerfel", 0.0), ("Kiste", 3.0)):
    me = bpy.data.meshes.new(name)
    me.from_pydata([(-.5, -.5, 0), (.5, -.5, 0), (.5, .5, 0), (-.5, .5, 0),
                    (-.5, -.5, 1), (.5, -.5, 1), (.5, .5, 1), (-.5, .5, 1)], [],
                   [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)])
    ob = bpy.data.objects.new(name, me)
    ob.location.x = x
    bpy.context.scene.collection.objects.link(ob)
bpy.context.scene.render.engine = "CYCLES"
bpy.ops.wm.save_as_mainfile(filepath=sys.argv[-1])
"""


def _cli(*args):
    """skptool live ... ueber den echten Parser aus cli.py."""
    from skptool import cli
    a = cli.build_parser().parse_args(["live", *args])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = a.func(a)
    return rc, buf.getvalue()


def _blender_pids():
    if os.name != "nt":
        return set()
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq blender.exe", "/FO", "CSV", "/NH"],
                         capture_output=True).stdout.decode("latin-1")
    return {int(line.split('","')[1]) for line in out.splitlines() if line.startswith('"blender.exe"')}


class TestClientWithoutBlender(unittest.TestCase):
    def _state(self, data):
        """Statusdatei so anlegen wie der echte Server: nur fuer den eigenen Benutzer lesbar (0600).
        Unter Linux und macOS lehnt der Client sonst jede Datei zu Recht als nicht privat ab."""
        self.state.write_text(json.dumps(data), encoding="utf-8")
        os.chmod(self.state, 0o600)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="skptool_live_"))
        self.state = self.tmp / "live.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_state_file_gives_german_hint(self):
        with self.assertRaises(live.LiveError) as cm:
            live.ping(self.state)
        self.assertIn("Kein Live-Blender aktiv, starte mit skptool open <datei> --live", str(cm.exception))

    def test_stale_state_file_is_removed(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()  # Port ist jetzt frei, niemand hoert zu
        self._state({"port": port, "token": "t" * 43, "pid": 1})
        with self.assertRaises(live.LiveError) as cm:
            live.status(self.state)
        self.assertIn("Kein Live-Blender aktiv", str(cm.exception))
        self.assertFalse(self.state.exists())

    def test_cli_without_blender_fails_cleanly(self):
        old = os.environ.get(live.STATE_ENV)
        os.environ[live.STATE_ENV] = str(self.state)
        try:
            with redirect_stdout(io.StringIO()):
                err = io.StringIO()
                sys_stderr, sys.stderr = sys.stderr, err
                try:
                    rc, _ = _cli("--status")
                finally:
                    sys.stderr = sys_stderr
        finally:
            if old is None:
                os.environ.pop(live.STATE_ENV, None)
            else:
                os.environ[live.STATE_ENV] = old
        self.assertEqual(rc, 1)
        self.assertIn("Kein Live-Blender aktiv", err.getvalue())

    def test_fake_server_cannot_move_files(self):
        """Ein fremder Prozess auf dem Port (z. B. nach einem Absturz) bekommt weder das Token noch
        darf er dem Client einen Pfad unterschieben."""
        import threading
        victim = self.tmp / "wichtig.txt"
        victim.write_text("bleibt")
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        seen = []

        def fake():
            conn, _ = srv.accept()
            with conn:
                conn.sendall(json.dumps({"hello": "f" * 32}).encode() + b"\n")
                req = b""
                while not req.endswith(b"\n"):
                    req += conn.recv(65536)
                seen.append(req)
                conn.sendall(json.dumps({"ok": True, "path": str(victim), "proof": "0" * 64}).encode() + b"\n")

        th = threading.Thread(target=fake, daemon=True)
        th.start()
        token = "geheimes-token-" + "x" * 30
        self._state({"port": srv.getsockname()[1], "token": token, "pid": 1})
        with self.assertRaises(live.LiveError):
            live.screenshot(self.tmp / "bild.png", state=self.state)
        th.join(10)
        srv.close()
        self.assertEqual(victim.read_text(), "bleibt")
        self.assertFalse((self.tmp / "bild.png").exists())
        self.assertEqual(len(seen), 1, "der falsche Server wurde gar nicht angesprochen")
        self.assertNotIn(token.encode(), seen[0])  # das Token ging nie ueber die Leitung

    @unittest.skipIf(os.name == "nt", "Dateirechte 0644 gibt es nur unter Linux und macOS")
    def test_state_file_readable_by_others_is_refused(self):
        self.state.write_text(json.dumps({"port": 1, "token": "t" * 43, "pid": 1}), encoding="utf-8")
        os.chmod(self.state, 0o644)
        with self.assertRaises(live.LiveError) as cm:
            live.status(self.state)
        self.assertIn("nicht privat", str(cm.exception))
        self.assertTrue(self.state.exists())  # eine fremde Datei wird nie geloescht

    def test_bad_ops_json_is_rejected_before_sending(self):
        with self.assertRaises(SystemExit):
            _cli("--ops", "{kein json")

    def test_select_for_screenshot_is_parsed_strictly(self):
        self.assertEqual(live._parse_select("Stuhl*"), {"name": "Stuhl*"})
        self.assertEqual(live._parse_select('{"layer": "Moebel"}'), {"layer": "Moebel"})
        self.assertIsNone(live._parse_select(None))
        for bad in ("", "   ", '{"name": NaN}', "{kaputt", "x" * 5000):
            with self.assertRaises(SystemExit):
                live._parse_select(bad)

    def test_no_em_dash_in_live_files(self):
        for p in (ROOT / "skptool" / "live.py", ROOT / "skptool" / "blender_scripts" / "live_server.py",
                  Path(__file__)):
            self.assertNotIn(chr(0x2014), p.read_text(encoding="utf-8"), p)


@unittest.skipUnless(GUI_OK, "Blender nicht installiert oder GUI-Tests abgeschaltet")
class TestLiveBlender(unittest.TestCase):
    """Ein Blender-Fenster fuer alle Tests dieser Klasse (Start etwa 3 bis 5 s)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="skptool_live_"))
        cls.state = cls.tmp / "live.json"
        cls.blend = cls.tmp / "klein.blend"
        cls.target = cls.tmp / "klein_live.skp"
        cls.pids_before = _blender_pids()
        mk = cls.tmp / "mk.py"
        mk.write_text(MAKE_BLEND, encoding="utf-8")
        subprocess.run([BLENDER, "-b", "--factory-startup", "--python", str(mk), "--", str(cls.blend)],
                       capture_output=True, timeout=120, check=True)
        t0 = time.perf_counter()
        cls.proc, cls.info = live.launch(
            cls.blend, export_skp=cls.target, state=cls.state, timeout=120, log=str(cls.tmp / "blender.log"),
            extra_args=["--factory-startup", "-p", "60", "60", "960", "600", "--no-window-focus"])
        cls.startup_s = time.perf_counter() - t0
        print(f"\n  Live-Blender bereit nach {cls.startup_s:.2f} s (Port {cls.info['port']})", file=sys.stderr)

    @classmethod
    def tearDownClass(cls):
        try:
            if cls.proc.poll() is None:
                try:
                    live.wait_for_export(cls.state, timeout=120)
                    live.quit_blender(cls.state, force=True)
                except live.LiveError:
                    pass
                try:
                    cls.proc.wait(30)
                except subprocess.TimeoutExpired:
                    cls.proc.kill()
                    cls.proc.wait(10)
        finally:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    # -------------------------------------------------------------- Hilfen

    def req(self, cmd, **kw):
        return live.request(cmd, self.state, 60, **kw)

    def center(self, name):
        res = live.run_ops([{"op": "list", "select": {"name": name}}], self.state)["results"][0]
        self.assertEqual(res["count"], 1)
        return res["objects"][0]["center"]

    def raw(self, payload, auth=False):
        """Rohe Anfrage. payload: bytes, oder dict (dann mit gueltiger Anmeldung, wenn auth=True)."""
        with socket.create_connection(("127.0.0.1", self.info["port"]), timeout=15) as c:
            hello = b""
            while not hello.endswith(b"\n"):
                hello += c.recv(1)
            if isinstance(payload, dict):
                sn = json.loads(hello)["hello"]
                if auth:
                    cn = "a" * 32
                    payload = {**payload, "cnonce": cn, "auth": live.mac(self.info["token"], "client", sn, cn)}
                payload = json.dumps(payload).encode() + b"\n"
            c.sendall(payload)
            try:
                c.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = c.recv(65536)
                if not chunk:
                    break
                buf += chunk
        return json.loads(buf.decode("utf-8"))

    # -------------------------------------------------------------- Tests

    def test_01_state_file_and_ping(self):
        data = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(data["pid"], self.proc.pid)
        self.assertGreaterEqual(len(data["token"]), 32)
        self.assertEqual(Path(data["blend"]).resolve(), self.blend.resolve())
        self.assertTrue(live.ping(self.state)["pong"])

    def test_02_latency(self):
        times = []
        for i in range(20):
            t = time.perf_counter()
            live.run_ops([{"op": "move", "select": {"name": "Kiste"}, "by": [0, 0.01, 0]}], self.state)
            times.append((time.perf_counter() - t) * 1000)
        live.undo(20, self.state)
        idle = []
        for _ in range(3):
            time.sleep(2.5)  # Timer faellt in den Ruhetakt
            t = time.perf_counter()
            live.ping(self.state)
            idle.append((time.perf_counter() - t) * 1000)
        print(f"\n  ops-Latenz im Takt: Median {statistics.median(times):.1f} ms, max {max(times):.1f} ms; "
              f"erste Anfrage nach Pause: {', '.join(f'{v:.0f}' for v in idle)} ms", file=sys.stderr)
        self.assertLess(statistics.median(times), 250)
        self.assertEqual(self.center("Kiste"), [3.0, 0.0, 0.5])

    def test_03_ops_move_and_undo(self):
        self.assertEqual(self.center("Wuerfel"), [0.0, 0.0, 0.5])
        resp = live.run_ops([{"op": "move", "select": {"name": "Wuerfel"}, "by": [0, 0, 2]},
                             {"op": "scale", "select": {"name": "Wuerfel"}, "factor": 2, "pivot": "bottom"}],
                            self.state)
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["undo_step"], "skptool: move, scale")
        self.assertEqual(self.center("Wuerfel"), [0.0, 0.0, 3.0])
        self.assertEqual(live.undo(1, self.state)["undone"], 1)  # ein Schritt fuer den ganzen Stapel
        self.assertEqual(self.center("Wuerfel"), [0.0, 0.0, 0.5])

    def test_04_failing_op_reports_error(self):
        resp = live.run_ops([{"op": "move", "select": {"name": "GibtEsNicht"}, "by": [1, 0, 0]}], self.state)
        self.assertFalse(resp["ok"])
        self.assertIn("Keine Objekte passen", resp["error"])
        self.assertIsNone(resp["undo_step"])
        resp = live.run_ops([{"op": "exec", "code": "print(1)"}], self.state)
        self.assertIn("Unbekannte Operation", resp["results"][0]["error"])

    def test_04b_measure_makes_no_undo_step(self):
        resp = live.run_ops([{"op": "measure", "select": {"name": "Kiste"}, "to_object": {"name": "Wuerfel"}}],
                            self.state)
        self.assertTrue(resp["ok"], resp)
        self.assertIsNone(resp["undo_step"])  # nur gelesen, also kein Rueckgaengig-Schritt
        self.assertAlmostEqual(resp["results"][0]["distance"], 3.0, places=5)

    def test_05_security(self):
        before = self.center("Wuerfel")
        move = {"cmd": "ops", "ops": [{"op": "move", "select": {"name": "Wuerfel"}, "by": [5, 0, 0]}]}
        r = self.raw({**move, "cnonce": "b" * 32, "auth": "0" * 64})  # falsche Anmeldung
        self.assertFalse(r["ok"])
        self.assertIn("Zugriff verweigert", r["error"])
        r = self.raw(move)  # ganz ohne Anmeldung
        self.assertIn("Zugriff verweigert", r["error"])
        r = self.raw({**move, "token": self.info["token"]})  # altes Protokoll: Token im Klartext
        self.assertIn("Zugriff verweigert", r["error"])
        r = self.raw({"cmd": "ping", "cnonce": "c" * 32, "auth": "\ud800" * 64})  # kaputtes Unicode
        self.assertFalse(r["ok"])
        r = self.raw(b"[" * 200_000 + b"\n")  # sehr tief verschachtelt
        self.assertFalse(r["ok"])
        r = self.raw(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")  # z. B. aus einem Browser
        self.assertFalse(r["ok"])
        r = self.raw(b'{"token": "' + b"a" * (1024 * 1024 + 10) + b'"}\n')
        self.assertIn("zu gross", r["error"])
        r = self.raw({"cmd": "python"}, auth=True)
        self.assertIn("Unbekannter Befehl", r["error"])
        self.assertIn("proof", r)  # Antworten nach gueltiger Anmeldung tragen den Beweis des Servers
        self.assertEqual(self.center("Wuerfel"), before)
        # nur auf 127.0.0.1 erreichbar
        other = [a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                 if not a[4][0].startswith("127.")]
        for ip in other[:2]:
            with self.assertRaises(OSError):
                socket.create_connection((ip, self.info["port"]), timeout=2).close()

    def test_05b_slow_senders_cannot_block_the_server(self):
        slow = []
        for _ in range(8):  # alle Plaetze belegen und tropfenweise senden
            c = socket.create_connection(("127.0.0.1", self.info["port"]), timeout=15)
            c.sendall(b"{")
            slow.append(c)
        try:
            t = time.monotonic()
            while time.monotonic() - t < 14:
                for c in slow:
                    try:
                        c.sendall(b" ")
                    except OSError:
                        pass
                time.sleep(1)
                try:
                    if live.ping(self.state, timeout=5)["pong"]:
                        break
                except live.LiveError:
                    continue
            self.assertTrue(live.ping(self.state, timeout=10)["pong"])
            self.assertLess(time.monotonic() - t, 14)  # Gesamtfrist 10 s je Anfrage greift
        finally:
            for c in slow:
                c.close()

    def test_06_screenshot_leaves_no_traces(self):
        st0 = live.status(self.state)
        out = self.tmp / "bilder" / "modell.png"
        t = time.perf_counter()
        resp = live.screenshot(out, width=800, height=500, state=self.state)
        total = (time.perf_counter() - t) * 1000
        self.assertTrue(out.exists())
        self.assertEqual(out.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        st1 = live.status(self.state)
        self.assertEqual(st1["all_objects"], st0["all_objects"])  # keine Kamera zurueckgeblieben
        self.assertEqual(st1["engine"], "CYCLES")                  # Render-Einstellung wiederhergestellt
        self.assertEqual(st1["camera"], st0["camera"])
        vp = live.screenshot(self.tmp / "ansicht.png", view="viewport", state=self.state)
        win = live.screenshot(self.tmp / "fenster.png", view="window", state=self.state)
        for p in (vp["path"], win["path"]):
            self.assertEqual(Path(p).read_bytes()[:4], b"\x89PNG")
        print(f"\n  Bild model 800x500: {resp['ms']:.0f} ms in Blender, {total:.0f} ms gesamt; "
              f"viewport {vp['ms']:.0f} ms, window {win['ms']:.0f} ms", file=sys.stderr)

    def test_06b_screenshot_frames_selection(self):
        from PIL import Image

        def filled(path):
            """Anteil der Bildpunkte, die nicht Hintergrund sind (Hintergrund = Ecke oben links)."""
            im = Image.open(path).convert("RGB")
            px, (w, h) = im.load(), im.size
            bg = px[0, 0]
            hits = sum(1 for x in range(0, w, 2) for y in range(0, h, 2)
                       if sum(abs(a - b) for a, b in zip(px[x, y], bg)) > 40)
            return hits / ((w + 1) // 2 * ((h + 1) // 2))

        st0 = live.status(self.state)
        whole = live.screenshot(self.tmp / "alles.png", width=400, height=250, state=self.state)
        box = live.screenshot(self.tmp / "kiste.png", width=400, height=250, state=self.state,
                              select={"name": "Kiste"})
        self.assertEqual(box["framed"], 1)
        self.assertNotIn("framed", whole)
        a, b = filled(whole["path"]), filled(box["path"])
        # beide Wuerfel im Gesamtbild sind klein, die Kiste allein fuellt das Bild
        self.assertGreater(b, 2 * a, (a, b))
        for kw, msg in (({"select": {"name": "GibtEsNicht"}}, "Keine Objekte passen"),
                        ({"select": "Kiste"}, "select muss ein Objekt sein"),
                        ({"select": {"name": "Kiste"}, "view": "viewport"}, "nur mit view model")):
            with self.assertRaises(live.LiveError) as cm:
                live.screenshot(self.tmp / "nie.png", width=200, height=100, state=self.state, **kw)
            self.assertIn(msg, str(cm.exception))
        self.assertFalse((self.tmp / "nie.png").exists())
        st1 = live.status(self.state)
        self.assertEqual(st1["all_objects"], st0["all_objects"])  # keine Kamera zurueckgeblieben
        self.assertEqual(st1["camera"], st0["camera"])
        old = os.environ.get(live.STATE_ENV)
        os.environ[live.STATE_ENV] = str(self.state)
        try:
            rc, out = _cli("--screenshot", str(self.tmp / "cli_kiste.png"), "--select", "Kis*",
                           "--width", "320", "--height", "200")
            self.assertEqual(rc, 0, out)
            self.assertTrue((self.tmp / "cli_kiste.png").exists())
            with self.assertRaises(SystemExit):
                _cli("--status", "--select", "Kiste")  # --select nur zusammen mit --screenshot
        finally:
            if old is None:
                os.environ.pop(live.STATE_ENV, None)
            else:
                os.environ[live.STATE_ENV] = old

    def test_07_cli_commands(self):
        old = os.environ.get(live.STATE_ENV)
        os.environ[live.STATE_ENV] = str(self.state)
        try:
            rc, out = _cli("--ops", '[{"op": "move", "select": {"name": "Kiste"}, "by": [0, 0, 1]}]')
            self.assertEqual(rc, 0, out)
            self.assertIn("moved=1", out)
            self.assertIn("skptool: move", out)
            rc, out = _cli("--undo")
            self.assertIn("1 Schritt", out)
            rc, out = _cli("--status", "--json")
            data = json.loads(out)
            self.assertEqual(data["status"]["objects"], 2)
            rc, out = _cli("--screenshot", str(self.tmp / "cli.png"), "--width", "320", "--height", "200")
            self.assertEqual(rc, 0, out)
            self.assertTrue((self.tmp / "cli.png").exists())
        finally:
            if old is None:
                os.environ.pop(live.STATE_ENV, None)
            else:
                os.environ[live.STATE_ENV] = old

    def test_08_launch_refuses_second_session(self):
        with self.assertRaises(live.LiveError) as cm:
            live.launch(self.blend, state=self.state, wait=False)
        self.assertIn("Es laeuft schon", str(cm.exception))

    def test_09_save_triggers_skp_export(self):
        live.run_ops([{"op": "move", "select": {"name": "Kiste"}, "by": [0, 0, 1]},
                      {"op": "set_material", "select": {"name": "Kiste"}, "material": "Rot",
                       "color": [200, 30, 30]}], self.state)
        self.assertTrue(live.status(self.state)["dirty"])
        with self.assertRaises(live.LiveError) as cm:  # Beenden mit ungespeicherten Aenderungen
            live.quit_blender(self.state)
        self.assertIn("ungespeicherte", str(cm.exception))
        t = time.perf_counter()
        resp = live.save(self.state, wait_export=True, export_timeout=300)
        secs = time.perf_counter() - t
        ex = resp["export"]
        self.assertEqual(ex["state"], "ok", ex)
        self.assertFalse(live.status(self.state)["dirty"])
        self.assertTrue(self.target.exists())
        print(f"\n  Speichern + .skp-Export: {secs:.1f} s ({ex['message']})", file=sys.stderr)
        from openskp import SkpFile
        model = SkpFile.open(str(self.target)).parse()
        names = {d.name for d in model.definitions.values()}
        self.assertTrue({"Kiste", "Wuerfel"} <= names, names)
        mats = {m.name for m in model.materials}
        self.assertIn("Rot", mats)
        # zweites Speichern waehrend des Exports: genau ein weiterer Lauf folgt
        live.run_ops([{"op": "move", "select": {"name": "Kiste"}, "by": [0, 0, -1]}], self.state)
        live.save(self.state)
        time.sleep(1.2)
        live.run_ops([{"op": "move", "select": {"name": "Kiste"}, "by": [0, 0, 1]}], self.state)
        live.save(self.state)
        ex = live.wait_for_export(self.state, timeout=300)
        self.assertEqual(ex["state"], "ok", ex)
        self.assertIn(ex["runs"], (2, 3))

    def test_99_quit_cleans_up(self):
        live.wait_for_export(self.state, timeout=300)
        resp = live.quit_blender(self.state)  # alles gespeichert, also ohne force
        self.assertTrue(resp["quitting"])
        self.proc.wait(60)
        self.assertFalse(self.state.exists(), "Statusdatei muss beim Beenden verschwinden")
        time.sleep(0.5)
        self.assertNotIn(self.proc.pid, _blender_pids())
        stray = _blender_pids() - self.pids_before
        self.assertEqual(stray, set(), f"uebrige Blender-Prozesse: {stray}")
        with self.assertRaises(live.LiveError) as cm:
            live.ping(self.state)
        self.assertIn("Kein Live-Blender aktiv", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
