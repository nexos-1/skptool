"""Tests fuer tools/aufruf_einrichten.py, den gehaerteten skptool.cmd und den Suchpfad-Schutz in skptool/__init__.py.

Start: .venv\\Scripts\\python -m unittest tests.test_aufruf -v
Das Skript wird nur gegen temporaere Ordner ausgefuehrt, nie gegen den echten %USERPROFILE%\\.local\\bin.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from skptool import __version__

ROOT = Path(__file__).resolve().parents[1]
SKRIPT = ROOT / "tools" / "aufruf_einrichten.py"
S2017 = ROOT / "samples" / "stuhl_tisch_2017.skp"
WINDOWS = os.name == "nt"
HAT_VENV = (ROOT / ".venv" / ("Scripts/python.exe" if WINDOWS else "bin/python")).is_file()
SH = shutil.which("sh") or next((p for p in (r"C:\Program Files\Git\bin\sh.exe",) if WINDOWS and Path(p).is_file()), None)

_spec = importlib.util.spec_from_file_location("aufruf_einrichten", SKRIPT)
ae = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ae)

FEINDLICH = ("glob", "json", "numpy", "skptool")


def sauberes_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "NoDefaultCurrentDirectoryInExePath")}
    env.update(extra)
    return env


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="skptool_aufruf_"))
        self.ziel = self.tmp / "bin"
        self.ziel.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def einrichten(self, *args, cwd=None, path=None):
        env = sauberes_env()
        if path is not None:
            env["PATH"] = path
        return subprocess.run([sys.executable, str(SKRIPT), *args], cwd=cwd or self.tmp, env=env,
                              capture_output=True, text=True, timeout=60)

    def feindlicher_ordner(self, module=FEINDLICH):
        """Ein Modellordner mit Python-Dateien, die beim Import eine Markierungsdatei anlegen wuerden."""
        d = self.tmp / "modell mit & zeichen"
        d.mkdir()
        for mod in module:
            (d / f"{mod}.py").write_text(f"open({str(self.tmp / ('PWNED_' + mod))!r}, 'w')\n")
        return d

    def gepwnt(self):
        return sorted(p.name for p in self.tmp.glob("PWNED_*"))


# ---------------------------------------------------------------- Einrichtungsskript

class TestEinrichten(Tmp):
    def starter(self):
        return self.ziel / ae.starter_name()

    def test_trockenlauf_schreibt_nichts(self):
        r = self.einrichten("--ziel", str(self.ziel))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Trockenlauf", r.stdout)
        self.assertIn(str(self.starter()), r.stdout)
        self.assertEqual(list(self.ziel.iterdir()), [])
        # auch ein noch fehlender Zielordner wird im Trockenlauf nicht angelegt
        r = self.einrichten("--ziel", str(self.tmp / "neu" / "bin"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.tmp / "neu").exists())

    def test_ja_legt_den_starter_an(self):
        r = self.einrichten("--ja", "--ziel", str(self.ziel))
        self.assertEqual(r.returncode, 0, r.stderr)
        text = self.starter().read_bytes().decode("ascii" if WINDOWS else "utf-8")
        self.assertIn(ae.MARKE, text)
        self.assertTrue(ae.ist_eigener_starter(self.starter()))
        if WINDOWS:
            self.assertTrue(text.startswith("@echo off\r\n"))
            self.assertIn(f'"{ROOT / "skptool.cmd"}" %*', text)
            self.assertNotIn("PYTHONPATH", text)
        else:
            self.assertTrue(os.access(self.starter(), os.X_OK))
            self.assertIn("-P -m skptool", text)
        # zweiter Lauf ersetzt den eigenen Starter
        r = self.einrichten("--ja", "--ziel", str(self.ziel))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Ersetzt", r.stdout)
        self.assertEqual(sorted(p.name for p in self.ziel.iterdir()), [ae.starter_name()])

    def test_fehlender_zielordner_wird_mit_ja_angelegt(self):
        ziel = self.tmp / "neu" / "bin"
        r = self.einrichten("--ja", "--ziel", str(ziel))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(ae.ist_eigener_starter(ziel / ae.starter_name()))

    def test_fremde_datei_wird_nie_ueberschrieben(self):
        for name in (ae.starter_name(), "skptool.exe", "SKPTOOL.bat"):
            fremd = self.ziel / name
            fremd.write_bytes(b"fremd")
            for args in (("--ja",), ()):
                r = self.einrichten(*args, "--ziel", str(self.ziel))
                self.assertEqual(r.returncode, 1, name)
                self.assertIn("fremdes skptool", r.stderr)
            self.assertEqual(fremd.read_bytes(), b"fremd")
            self.assertEqual([p.name for p in self.ziel.iterdir()], [name])
            fremd.unlink()

    def test_entfernen_loescht_nur_den_eigenen_starter(self):
        r = self.einrichten("--entfernen", "--ja", "--ziel", str(self.ziel))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Nichts zu entfernen", r.stdout)
        self.assertEqual(self.einrichten("--ja", "--ziel", str(self.ziel)).returncode, 0)
        r = self.einrichten("--entfernen", "--ziel", str(self.ziel))  # Trockenlauf
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Wuerde entfernen", r.stdout)
        self.assertTrue(self.starter().exists())
        r = self.einrichten("--entfernen", "--ja", "--ziel", str(self.ziel))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.starter().exists())
        # eine fremde Datei mit demselben Namen bleibt
        self.starter().write_bytes(b"@echo off\r\nrem von jemand anderem\r\n")
        r = self.einrichten("--entfernen", "--ja", "--ziel", str(self.ziel))
        self.assertEqual(r.returncode, 1)
        self.assertIn("Markierung fehlt", r.stderr)
        self.assertTrue(self.starter().exists())

    def test_relativer_oder_falscher_zielordner_wird_abgelehnt(self):
        for ziel in ("bin", "." + os.sep + "bin", "..", "C:bin" if WINDOWS else "bin/x", "\\bin" if WINDOWS else "x"):
            r = self.einrichten("--ja", "--ziel", ziel, cwd=self.tmp)
            self.assertEqual(r.returncode, 1, ziel)
            self.assertIn("absoluter Pfad", r.stderr)
        self.assertEqual(list(self.ziel.iterdir()), [])
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["bin"])
        datei = self.tmp / "datei"
        datei.write_text("x")
        self.assertEqual(self.einrichten("--ja", "--ziel", str(datei)).returncode, 1)
        r = self.einrichten("--ja", "--ziel", str(ROOT))
        self.assertEqual(r.returncode, 1)
        self.assertIn("Projektordner", r.stderr)
        self.assertEqual(self.einrichten("--unbekannt").returncode, 1)

    def test_warnt_bei_fehlendem_pfad_und_anderem_skptool_davor(self):
        r = self.einrichten("--ziel", str(self.ziel), path=os.environ.get("PATH", ""))
        self.assertIn("steht nicht im PATH", r.stdout)
        r = self.einrichten("--ziel", str(self.ziel), path=os.pathsep.join([str(self.ziel), os.environ.get("PATH", "")]))
        self.assertNotIn("steht nicht im PATH", r.stdout)
        self.assertNotIn("anderes skptool", r.stdout)
        anderer = self.tmp / "anderer"
        anderer.mkdir()
        fremd = anderer / ("skptool.exe" if WINDOWS else "skptool")
        fremd.write_bytes(b"x")
        fremd.chmod(0o755)
        r = self.einrichten("--ziel", str(self.ziel), path=os.pathsep.join([str(anderer), str(self.ziel)]))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("anderes skptool", r.stdout)
        self.assertIn(str(fremd), r.stdout)
        # ein skptool im aktuellen Ordner zaehlt nicht (wird nie gestartet), auch nicht ueber "." oder leere Eintraege
        r = self.einrichten("--ziel", str(self.ziel), cwd=anderer,
                            path=os.pathsep.join(["", ".", "anderer", str(self.ziel)]))
        self.assertNotIn("anderes skptool", r.stdout)

    def test_starter_inhalte(self):
        text = ae.inhalt_posix("/opt/mein 'repo'")
        zeilen = text.splitlines()
        self.assertEqual(zeilen[0], "#!/bin/sh")
        self.assertIn(ae.MARKE, zeilen[1])
        self.assertIn("PYTHONPATH='/opt/mein '\\''repo'\\'''", text)
        self.assertEqual(zeilen[-1], "exec '/opt/mein '\\''repo'\\''/.venv/bin/python' -P -m skptool \"$@\"")
        with self.assertRaises(ae.Abbruch):
            ae.inhalt_posix("/opt/a\nb")
        with self.assertRaises(ae.Abbruch):
            ae.inhalt_windows('C:\\a"b')
        sep = "\\" if WINDOWS else "/"
        self.assertIn(f'"C:\\100%%{sep}skptool.cmd" %*', ae.inhalt_windows("C:\\100%"))

    @unittest.skipUnless(SH, "keine sh gefunden")
    def test_posix_starter_in_sh(self):
        """Der erzeugte Shell-Starter mit einer Attrappe als Python: Argumente und PYTHONPATH kommen richtig an."""
        repo = self.tmp / "mein 'repo' $x"
        (repo / ".venv" / "bin").mkdir(parents=True)
        fake = repo / ".venv" / "bin" / "python"
        fake.write_bytes(b'#!/bin/sh\nprintf "PP=%s\\n" "$PYTHONPATH"\nfor a in "$@"; do printf "ARG=%s\\n" "$a"; done\n')
        fake.chmod(0o755)
        starter = self.tmp / "skptool"
        starter.write_bytes(ae.inhalt_posix(repo.as_posix()).encode())
        starter.chmod(0o755)
        r = subprocess.run([SH, starter.as_posix(), "--version", "a b", "$HOME"], cwd=self.tmp,
                           env=sauberes_env(PYTHONPATH=".:"), capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), [f"PP={repo.as_posix()}", "ARG=-P", "ARG=-m", "ARG=skptool",
                                                 "ARG=--version", "ARG=a b", "ARG=$HOME"])


# ---------------------------------------------------------------- erzeugter Starter von ueberall

@unittest.skipUnless(WINDOWS, "skptool.cmd gibt es nur unter Windows")
@unittest.skipUnless(HAT_VENV, "der Starter braucht die .venv im Projektordner")
class TestStarterVonUeberall(Tmp):
    def setUp(self):
        super().setUp()
        r = self.einrichten("--ja", "--ziel", str(self.ziel))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.starter = self.ziel / "skptool.cmd"
        self.modell = self.feindlicher_ordner()

    def starten(self, *args, via_name=False, **env):
        e = sauberes_env(**env)
        if via_name:  # Aufruf nur ueber den Namen, wie im Terminal
            e["PATH"] = str(self.ziel) + os.pathsep + e.get("PATH", "")
        cmd = ["cmd", "/c", "skptool" if via_name else str(self.starter), *args]
        return subprocess.run(cmd, cwd=self.modell, env=e, capture_output=True, text=True, timeout=120)

    def test_starter_laedt_nichts_aus_dem_aktuellen_ordner(self):
        for via_name in (False, True):
            r = self.starten("--version", via_name=via_name)
            self.assertEqual(self.gepwnt(), [], via_name)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(f"skptool {__version__}", r.stdout)

    def test_starter_mit_boesem_pythonpath(self):
        for pp in (";.", ".", ";;", ".;.\\;relativ;C:rel", ";.;..\\modell mit & zeichen"):
            for via_name in (False, True):
                r = self.starten("--version", via_name=via_name, PYTHONPATH=pp)
                self.assertEqual(self.gepwnt(), [], pp)
                self.assertEqual(r.returncode, 0, (pp, r.stderr))
                self.assertIn(f"skptool {__version__}", r.stdout)

    def test_absoluter_pythonpath_bleibt_nutzbar(self):
        extra = self.tmp / "extra & mehr!"
        extra.mkdir()
        # Kopie des Projektstarters, die am Ende den bereinigten PYTHONPATH ausgibt statt Python zu starten
        probe = self.tmp / "probe.cmd"
        text = (ROOT / "skptool.cmd").read_text(encoding="ascii").replace(
            '"%~dp0.venv\\Scripts\\python.exe" -P -m skptool %*', "set PYTHONPATH")
        self.assertIn("set PYTHONPATH\n", text)
        probe.write_text(text.replace("%~dp0", str(ROOT) + "\\"), encoding="ascii")
        r = subprocess.run(["cmd", "/c", str(probe)], cwd=self.modell, capture_output=True, text=True, timeout=60,
                           env=sauberes_env(PYTHONPATH=f";{extra};.;relativ;;\\\\server\\freigabe"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), f"PYTHONPATH={ROOT}\\.;{extra};\\\\server\\freigabe")

    @unittest.skipUnless(S2017.exists(), "Beispieldatei fehlt")
    def test_argumente_und_exitcode_kommen_durch(self):
        d = self.modell / "mit leerzeichen"
        d.mkdir()
        shutil.copy(S2017, d / "ein modell.skp")
        r = self.starten("info", str(d / "ein modell.skp"), via_name=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Table", r.stdout)
        r = self.starten("info", str(d / "fehlt.skp"), via_name=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.gepwnt(), [])


# ---------------------------------------------------------------- Suchpfad-Schutz beim Import

class TestSuchpfadSchutz(Tmp):
    def test_aktueller_ordner_fliegt_aus_sys_path(self):
        d = self.feindlicher_ordner(("glob", "json"))
        code = "import skptool, sys, json, glob; print(json.dumps([sys.path, json.__file__]))"
        r = subprocess.run([sys.executable, "-c", code], cwd=d, capture_output=True, text=True, timeout=60,
                           env=sauberes_env(PYTHONPATH=os.pathsep.join([str(ROOT), "", ".", str(d)])))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.gepwnt(), [])
        pfad, json_datei = json.loads(r.stdout)
        for eintrag in pfad:
            self.assertNotIn(eintrag, ("", "."))
            self.assertNotEqual(os.path.normcase(os.path.realpath(eintrag)), os.path.normcase(os.path.realpath(d)))
        self.assertIn(str(ROOT), pfad)
        self.assertNotEqual(Path(json_datei).parent, d)

    def test_projektordner_als_aktueller_ordner_bleibt(self):
        r = subprocess.run([sys.executable, "-c", "import sys, skptool; print(repr(sys.path[0]))"], cwd=ROOT,
                           capture_output=True, text=True, timeout=60, env=sauberes_env())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "''")


if __name__ == "__main__":
    unittest.main()
