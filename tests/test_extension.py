"""Tests fuer die Blender-Erweiterung skptool_io (blender_extension/skptool_io).
Start: .venv\\Scripts\\python -m unittest tests.test_extension -v

- RunnerTest und ManifestTest laufen ohne Blender (runner.py braucht kein bpy).
- ExtensionBlenderTest baut die .zip (tools/extension_bauen.py), installiert sie mit
  "blender --command extension install-file" in Benutzerordner in einem Temp-Ordner
  (BLENDER_USER_RESOURCES und BLENDER_USER_*), fuehrt in "blender -b --factory-startup" Import und
  Export aus (tests/extension_in_blender.py) und prueft das Ergebnis mit skptool list, info und diff.
  Die echte Blender-Einrichtung des Benutzers muss dabei unveraendert bleiben (wird verglichen).
  skptool laeuft dabei aus einem Temp-Projektordner mit eigener venv, die die Pakete des
  laufenden Pythons mitbenutzt, und einer Kopie von skptool/ aus diesem Checkout.
  Ohne Blender uebersprungen.
"""
import importlib.util
import json
import os
import re
import shutil
import site
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from skptool.blender import BlenderError, find_blender

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "blender_extension" / "skptool_io"
STUHL = ROOT / "samples" / "stuhl_tisch_2017.skp"
WINDOWS = os.name == "nt"

try:
    BLENDER = find_blender()
except BlenderError:
    BLENDER = None


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


runner = _load("skptool_io_runner", EXT / "runner.py")
bauen = _load("extension_bauen", ROOT / "tools" / "extension_bauen.py")


def venv_python(proj: Path) -> Path:
    return proj / ".venv" / ("Scripts/python.exe" if WINDOWS else "bin/python")


def fake_project(base: Path, with_venv=True) -> Path:
    """Minimaler skptool-Projektordner fuer die Aufloesung (Dateien leer, es wird nichts gestartet)."""
    (base / "skptool").mkdir(parents=True)
    (base / "skptool" / "__init__.py").write_text("")
    (base / "skptool" / "cli.py").write_text("")
    if with_venv:
        py = venv_python(base)
        py.parent.mkdir(parents=True)
        py.write_text("")
        if not WINDOWS:
            py.chmod(0o755)
    return base


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="skptool_ext_runner_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def refused(self, setting, pattern):
        with self.assertRaises(runner.SkptoolNotFound) as cm:
            runner.resolve(setting)
        self.assertRegex(str(cm.exception), pattern)

    def test_leer_relativ_fehlend(self):
        self.refused("", "No skptool path")
        self.refused("skptool.cmd", "absolute")
        self.refused(str(self.tmp / "nichts" / "skptool.exe"), "Not found")
        self.refused(str(self.tmp), "not a skptool project")

    def test_projektordner_starter_und_python(self):
        proj = fake_project(self.tmp / "proj")
        py = venv_python(proj)
        settings = [proj, py]
        if WINDOWS:  # unter Linux/macOS ist proj/skptool der Paketordner, dort gibt es keinen Starter
            launcher = proj / "skptool.cmd"
            launcher.write_text("@echo off\n")
            settings.insert(1, launcher)
        for setting in settings:
            cmd = runner.resolve(str(setting))
            self.assertEqual(cmd.argv, [str(py), "-P", "-m", "skptool"], setting)
            self.assertEqual(cmd.env_extra, {"PYTHONPATH": str(proj)}, setting)

    def test_projekt_ohne_venv(self):
        self.refused(str(fake_project(self.tmp / "proj", with_venv=False)), "has no venv")

    def test_starter_von_aufruf_einrichten(self):
        """Der Starter aus tools/aufruf_einrichten.py wird aufgeloest, nie selbst gestartet."""
        auf = _load("aufruf_einrichten", ROOT / "tools" / "aufruf_einrichten.py")
        self.assertEqual(runner.STARTER_MARKE, auf.MARKE)
        proj = fake_project(self.tmp / "mit & prozent%")
        text = auf.inhalt_windows(proj) if WINDOWS else auf.inhalt_posix(proj)
        starter = self.tmp / "bin" / auf.starter_name()
        starter.parent.mkdir()
        starter.write_bytes(text.encode("utf-8"))
        cmd = runner.resolve(str(starter))
        self.assertEqual(cmd.env_extra, {"PYTHONPATH": str(proj)})
        self.assertEqual(cmd.argv[0], str(venv_python(proj)))

    @unittest.skipUnless(WINDOWS, "nur Windows")
    def test_fremde_batch_datei_wird_nicht_gestartet(self):
        for name in ("skptool.cmd", "irgendwas.bat"):
            p = self.tmp / name
            p.write_text('@echo off\r\n"C:\\x\\skptool.cmd" %*\r\n')
            self.refused(str(p), "not started directly")

    @unittest.skipIf(WINDOWS, "nur POSIX")
    def test_nicht_ausfuehrbar(self):
        p = self.tmp / "skptool"
        p.write_text("#!/bin/sh\n")
        p.chmod(0o644)
        self.refused(str(p), "not executable")

    def test_autodetect_nur_mit_markierung(self):
        fremd = self.tmp / ("skptool.cmd" if WINDOWS else "skptool")
        fremd.write_text("@echo off\n")
        with mock.patch.object(runner, "default_starter", return_value=fremd):
            self.assertIsNone(runner.autodetect())

    def test_umgebung(self):
        cmd = runner.Command(["x"], {"PYTHONPATH": "/proj"})
        with mock.patch.dict(os.environ, {"PYTHONHOME": "/boese", "PYTHONSTARTUP": "/boese.py",
                                          "PYTHONPATH": "/anders"}):
            env = runner.build_env(cmd)
        self.assertEqual(env["PYTHONPATH"], "/proj")
        self.assertNotIn("PYTHONHOME", env)
        self.assertNotIn("PYTHONSTARTUP", env)

    def test_job_fortschritt_und_hinweise(self):
        code = ("import sys, time\n"
                "print('  [   0.1s] Lese stuhl.skp ein', file=sys.stderr, flush=True)\n"
                "print('Hinweis: 2 Verweis(e) auf externe Dateien nicht uebernommen:', file=sys.stderr)\n"
                "print('  C:/tex/a.png', file=sys.stderr)\n"
                "print('OK   a -> b  (1.0s, fertig)')\n")
        job = runner.Job([sys.executable, "-c", code], None, str(self.tmp), 60)
        self.assertEqual(job.wait(), 0)
        self.assertEqual(job.progress(), "Lese stuhl.skp ein")
        self.assertEqual(runner.notices(job), ["Hinweis: 2 Verweis(e) auf externe Dateien nicht uebernommen:",
                                               "  C:/tex/a.png"])
        self.assertEqual(job.stdout, ["OK   a -> b  (1.0s, fertig)"])

    def test_job_zeitlimit_beendet_prozessbaum(self):
        # Kindprozess startet selbst einen Enkel (wie skptool Blender), beide muessen beendet werden
        marker = self.tmp / "enkel_lebt.txt"
        enkel = f"import time, pathlib; time.sleep(4); pathlib.Path({str(marker)!r}).write_text('x')"
        code = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {enkel!r}]); time.sleep(60)"
        job = runner.Job([sys.executable, "-c", code], None, str(self.tmp), 1.5)
        t = time.monotonic()
        rc = job.wait(tick=0.1)
        self.assertLess(time.monotonic() - t, 30)
        self.assertNotEqual(rc, 0)
        self.assertTrue(job.timed_out)
        self.assertIn("did not finish", job.error_text())
        time.sleep(5)
        self.assertFalse(marker.exists(), "Enkelprozess lief nach dem Zeitlimit weiter")

    def test_fehlertext(self):
        code = ("import sys\nprint('  [   0.1s] Lese x ein', file=sys.stderr)\n"
                "print('FEHLER x.skp: keine SketchUp-Datei (Dateikopf fehlt)', file=sys.stderr)\nsys.exit(1)\n")
        job = runner.Job([sys.executable, "-c", code], None, str(self.tmp), 60)
        self.assertEqual(job.wait(), 1)
        self.assertEqual(job.error_text(), "FEHLER x.skp: keine SketchUp-Datei (Dateikopf fehlt)")


class ManifestTest(unittest.TestCase):
    def test_manifest(self):
        with open(EXT / "blender_manifest.toml", "rb") as fh:
            m = tomllib.load(fh)
        self.assertEqual(m["schema_version"], "1.0.0")
        self.assertEqual(m["id"], "skptool_io")
        self.assertEqual(m["type"], "add-on")
        self.assertEqual(m["blender_version_min"], "4.2.0")
        self.assertEqual(set(m["permissions"]), {"files"}, "nur Dateizugriff, kein Netzwerk")
        for text in [m["tagline"], *m["permissions"].values()]:
            self.assertLessEqual(len(text), 64)
            self.assertRegex(text, r"[A-Za-z0-9]$")
        self.assertNotIn("wheels", m, "OpenSKP und andere Pakete werden nicht mitgeliefert")

    def test_keine_shell_kein_code_aus_dateien(self):
        verboten = ["shell=" + "True", "os." + "system", "os." + "popen", "ev" + "al(", "ex" + "ec(",
                    "urllib", "socket", "http.client", "requests"]
        for py in EXT.glob("*.py"):
            src = py.read_text(encoding="utf-8")
            for bad in verboten:
                self.assertNotIn(bad, src, f"{py.name}: {bad}")

    def test_kein_openskp_in_der_erweiterung(self):
        for p in EXT.rglob("*"):
            self.assertNotIn("openskp", p.name.lower())
            if p.suffix == ".py":
                self.assertIsNone(re.search(r"^\s*(import|from)\s+(openskp|skptool)\b",
                                            p.read_text(encoding="utf-8"), re.M), p.name)


def real_config_dir() -> Path:
    if WINDOWS:
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Blender Foundation"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Blender"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "blender"


# Schreibt nur ein Blender mit Fenster (andere Sitzungen auf dem Rechner), nie ein Hintergrundlauf
_GUI_ONLY = {"recent-files.txt", "recent-searches.txt", "bookmarks.txt"}


def snapshot(folder: Path) -> dict:
    out = {}
    if not folder.exists():
        return out
    for dirpath, dirs, files in os.walk(folder):
        for n in dirs + files:
            if n in _GUI_ONLY:
                continue
            p = Path(dirpath) / n
            try:
                st = p.stat()
            except OSError:
                continue
            out[str(p.relative_to(folder))] = (st.st_mtime_ns, st.st_size if n in files else -1)
    return out


@unittest.skipUnless(BLENDER, "Blender nicht installiert")
class ExtensionBlenderTest(unittest.TestCase):
    """Bauen, isoliert installieren, in Blender importieren und exportieren."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="skptool_ext_test_"))
        cls.real = real_config_dir()
        cls.before = snapshot(cls.real)
        cls.env = bauen.isolierte_umgebung(cls.tmp / "blender_user")
        cls.zip = bauen.bauen(cls.tmp / "dist", BLENDER)
        r = subprocess.run([BLENDER, "--factory-startup", "--command", "extension", "install-file",
                            "-r", "user_default", str(cls.zip)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=cls.env, timeout=300, stdin=subprocess.DEVNULL)
        cls.install_log = r.stdout + r.stderr
        cls.install_rc = r.returncode
        cls.project = cls._make_project()
        cls.out = cls.tmp / "out"
        cls.out.mkdir()
        job = cls.tmp / "job.json"
        job.write_text(json.dumps({"skp": str(STUHL), "skptool": str(cls.project), "out": str(cls.out)}),
                       encoding="utf-8")
        t = time.time()
        r = subprocess.run([BLENDER, "-b", "--factory-startup", "--python-exit-code", "3", "--python",
                            str(ROOT / "tests" / "extension_in_blender.py"), "--", str(job)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=cls.env,
                           timeout=1800, stdin=subprocess.DEVNULL)
        cls.blender_seconds = time.time() - t
        cls.log = r.stdout + r.stderr
        line = next((l for l in r.stdout.splitlines() if l.startswith("SKPTOOL_EXT_TEST ")), None)
        cls.res = json.loads(line[len("SKPTOOL_EXT_TEST "):]) if line else {"crash": cls.log[-4000:]}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def _make_project(cls) -> Path:
        """Projektordner wie nach der Installation: skptool/ plus .venv, die die Pakete des laufenden
        Pythons mitbenutzt (numpy, openskp), ohne etwas herunterzuladen."""
        proj = cls.tmp / "projekt"
        shutil.copytree(ROOT / "skptool", proj / "skptool", ignore=shutil.ignore_patterns("__pycache__"))
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(proj / ".venv")], check=True,
                       timeout=300, stdin=subprocess.DEVNULL, capture_output=True)
        purelib = subprocess.run([str(venv_python(proj)), "-c",
                                  "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                                 capture_output=True, text=True, check=True, timeout=120,
                                 stdin=subprocess.DEVNULL).stdout.strip()
        pakete = [p for p in site.getsitepackages() + [site.getusersitepackages()] if os.path.isdir(p)]
        Path(purelib, "skptool_test_pakete.pth").write_text("\n".join(pakete) + "\n", encoding="utf-8")
        return proj

    def skptool(self, *args):
        env = dict(self.env, PYTHONPATH=str(self.project))
        r = subprocess.run([sys.executable, "-P", "-m", "skptool", *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=900, stdin=subprocess.DEVNULL,
                           cwd=str(self.tmp))
        return r.returncode, r.stdout, r.stderr

    def check(self):
        self.assertNotIn("crash", self.res, self.res.get("crash"))

    def test_01_installiert_und_aktiviert(self):
        self.assertEqual(self.install_rc, 0, self.install_log)
        installed = self.tmp / "blender_user" / "extensions" / "user_default" / "skptool_io"
        self.assertTrue((installed / "blender_manifest.toml").is_file(), self.install_log)
        self.check()
        self.assertTrue(self.res["enabled"])
        self.assertTrue(Path(self.res["addon_file"]).resolve().is_relative_to(installed.resolve()),
                        self.res["addon_file"])

    def test_02_menues_registriert(self):
        self.check()
        self.assertTrue(self.res["import_menu"], "File > Import > SketchUp fehlt")
        self.assertTrue(self.res["export_menu"], "File > Export > SketchUp fehlt")
        self.assertTrue(self.res["file_handler"])
        self.assertEqual(self.res["ops"], [True, True])

    def test_03_fehlerfaelle(self):
        self.check()
        self.assertRegex(self.res["relative_path"][0], "ERROR: .*absolute")
        self.assertRegex(self.res["missing_path"][0], "ERROR: .*Not found")
        self.assertRegex(self.res["broken_file"][0], "ERROR: .*(FEHLER|SketchUp)")
        self.assertEqual(self.res["objects_after_errors"], ["Camera", "Cube", "Light"])
        self.assertRegex(self.res["export_empty_sel"][0], "ERROR: .*Nothing selected")
        self.assertEqual(self.res["leftover_temp_dirs"], [], "Temp-Ordner nicht aufgeraeumt")

    def test_04_import_wie_skptool_list(self):
        self.check()
        self.assertEqual(self.res["import"], ["FINISHED"], self.log[-3000:])
        rc, out, err = self.skptool("list", str(STUHL), "--json", "-q")
        self.assertEqual(rc, 0, err)
        summary = json.loads(out)["summary"]
        st = self.res["after_import"]
        self.assertEqual(st["mesh_objects"] - 1, summary["objects"])  # ohne den Wuerfel der Startszene
        self.assertEqual(st["unique_meshes"] - 1, summary["unique_meshes"], "Instanzen muessen geteilt bleiben")
        self.assertEqual(sorted(set(st["collections"]) - {"Collection"}),
                         sorted(l["name"] for l in summary["layers"]))
        self.assertEqual(sorted(set(st["materials"]) - {"Material"}), sorted(summary["materials"]))
        imported = set(st["objects"]) - {"Camera", "Cube", "Light"}
        self.assertEqual(set(st["selected"]), imported, "nach dem Import ist genau das Importierte ausgewaehlt")

    def test_05_export_auswahl_gleich_quelle(self):
        self.check()
        self.assertEqual(self.res["export_sel"], ["FINISHED"], self.log[-3000:])
        rc, out, err = self.skptool("diff", str(STUHL), str(self.out / "auswahl.skp"), "--geometrie")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("Ergebnis: gleich", out)

    def test_06_export_szene_mit_und_ohne_modifikatoren(self):
        self.check()
        self.assertEqual(self.res["export_all"], ["FINISHED"], self.log[-3000:])
        self.assertEqual(self.res["export_nomod"], ["FINISHED"], self.log[-3000:])
        self.assertEqual(self.res["cube_modifiers_after"], 1, "die offene Szene darf nicht veraendert werden")
        rc, out, _ = self.skptool("diff", str(STUHL), str(self.out / "szene.skp"), "-q")
        self.assertEqual(rc, 1, "ganze Szene enthaelt zusaetzlich den Wuerfel")
        faces = {}
        for name in ("auswahl", "szene", "szene_ohne_mod"):
            rc, out, err = self.skptool("info", str(self.out / f"{name}.skp"), "--json")
            self.assertEqual(rc, 0, err)
            faces[name] = json.loads(out)["faces_total"]
        self.assertEqual(faces["szene"] - faces["auswahl"], 18, faces)  # Wuerfel mit Array x3
        self.assertEqual(faces["szene_ohne_mod"] - faces["auswahl"], 6, faces)  # Wuerfel allein

    def test_07_szeneneinheit_zentimeter(self):
        """Szene in cm (unit_settings.scale_length 0.01): Modell wird 100-mal kleiner geschrieben.
        Prueft auch --unit-scale von skptool convert (Meter pro Blender-Einheit)."""
        self.check()
        self.assertEqual(self.res["export_cm"], ["FINISHED"], self.log[-3000:])
        sizes = {}
        for name in ("auswahl", "auswahl_cm"):
            rc, out, err = self.skptool("info", str(self.out / f"{name}.skp"), "--json")
            self.assertEqual(rc, 0, err)
            s = json.loads(out)["size_m"]  # auf mm gerundet
            sizes[name] = [s["width"], s["depth"], s["height"]]
        for a, b in zip(sizes["auswahl"], sizes["auswahl_cm"]):
            self.assertAlmostEqual(b, a / 100, delta=0.0006, msg=sizes)

    def test_99_echte_blender_einrichtung_unveraendert(self):
        after = snapshot(self.real)
        changed = sorted(k for k in set(self.before) | set(after) if self.before.get(k) != after.get(k))
        self.assertEqual(changed, [], f"in {self.real} geaendert")
        self.assertFalse(any("skptool_io" in k for k in after), "Erweiterung in der echten Einrichtung")
