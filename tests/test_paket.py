"""Paketbeschreibung (pyproject.toml), gebautes Wheel und installierter Befehl skptool.

Start: .venv\\Scripts\\python -m unittest tests.test_paket -v

Die Bau- und Installationstests brauchen uv (sonst uebersprungen). Sie bauen aus einer Kopie des Pakets
in einem temporaeren Ordner (setuptools nur mit Pruefsumme aus tools/paketbau.lock) und installieren in
eine frische venv (Abhaengigkeiten nur mit Pruefsummen aus requirements.lock). Beim ersten Lauf ohne
uv-Cache laedt das aus dem Netz.
SKPTOOL_INSTALLIERT=<Pfad zum installierten skptool-Befehl> prueft stattdessen einen schon installierten
Befehl (CI-Job "Paket", tools/linux_e2e.sh).
"""
import ast
import email.parser
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

from skptool import __version__

ROOT = Path(__file__).resolve().parents[1]
PAKET = ROOT / "skptool"
S2017 = ROOT / "samples" / "stuhl_tisch_2017.skp"
WINDOWS = os.name == "nt"
UV = os.environ.get("SKPTOOL_UV") or shutil.which("uv")

with open(ROOT / "pyproject.toml", "rb") as _fh:
    PYPROJECT = tomllib.load(_fh)


def requirements_pins(pfad: Path) -> list[str]:
    """Anforderungen aus einer requirements-Datei ohne Kommentare, Pruefsummen und Fortsetzungszeilen."""
    pins = []
    for zeile in pfad.read_text(encoding="utf-8").splitlines():
        zeile = zeile.split("#", 1)[0].strip().rstrip("\\").strip()
        if zeile and not zeile.startswith("-"):
            pins.append(zeile)
    return pins


def quell_module() -> set[str]:
    """Alle .py-Dateien des Pakets relativ zu ROOT, wie sie im Wheel stehen muessen."""
    return {p.relative_to(ROOT).as_posix() for p in PAKET.rglob("*.py") if "__pycache__" not in p.parts}


def sauberes_env(**extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "PYTHONHOME", "PYTHONSAFEPATH", "VIRTUAL_ENV", "PYTHONSTARTUP")}
    env.update(extra)
    return env


# ---------------------------------------------------------------- pyproject.toml

class TestPyproject(unittest.TestCase):
    def test_version_kommt_aus_dem_paket(self):
        projekt = PYPROJECT["project"]
        self.assertNotIn("version", projekt, "Version nur an einer Stelle: skptool/__init__.py")
        self.assertIn("version", projekt["dynamic"])
        self.assertEqual(PYPROJECT["tool"]["setuptools"]["dynamic"]["version"], {"attr": "skptool.__version__"})
        # setuptools liest die Version statisch (ohne Import); das geht nur mit einer einfachen Zuweisung
        baum = ast.parse((PAKET / "__init__.py").read_text(encoding="utf-8"))
        werte = [k.value.value for k in baum.body if isinstance(k, ast.Assign) and isinstance(k.value, ast.Constant)
                 and any(isinstance(t, ast.Name) and t.id == "__version__" for t in k.targets)]
        self.assertEqual(werte, [__version__])

    def test_abhaengigkeiten_wie_requirements(self):
        pins = requirements_pins(ROOT / "requirements.txt")
        self.assertTrue(pins)
        self.assertEqual(sorted(PYPROJECT["project"]["dependencies"]), sorted(pins),
                         "pyproject.toml [project] dependencies muss requirements.txt entsprechen")
        for p in pins:
            self.assertRegex(p, r"^[A-Za-z0-9_.-]+==[0-9][A-Za-z0-9.]*$", "nur feste Versionen")

    def test_bauwerkzeug_gepinnt_und_mit_pruefsumme(self):
        requires = PYPROJECT["build-system"]["requires"]
        self.assertEqual(PYPROJECT["build-system"]["build-backend"], "setuptools.build_meta")
        self.assertEqual(len(requires), 1)
        self.assertRegex(requires[0], r"^setuptools==\d+\.\d+\.\d+$")
        lock = (ROOT / "tools" / "paketbau.lock").read_text(encoding="utf-8")
        self.assertEqual(requirements_pins(ROOT / "tools" / "paketbau.lock"), requires)
        self.assertGreaterEqual(lock.count("--hash=sha256:"), 1)

    def test_einstiegspunkt(self):
        self.assertEqual(PYPROJECT["project"]["scripts"], {"skptool": "skptool.cli:main"})
        from skptool import cli
        self.assertTrue(callable(cli.main))

    def test_lizenz_und_python(self):
        projekt = PYPROJECT["project"]
        self.assertEqual(projekt["license"], "MIT")
        for muster in projekt["license-files"]:
            self.assertTrue(list(ROOT.glob(muster)), muster)
        self.assertEqual(projekt["requires-python"], ">=3.12")
        self.assertFalse([c for c in projekt.get("classifiers", []) if c.startswith("License ::")],
                         "SPDX-Lizenz und License-Klassifikator schliessen sich aus")

    def test_paketdaten_decken_alle_laufzeitdateien_ab(self):
        st = PYPROJECT["tool"]["setuptools"]
        pakete = set(st["packages"])
        # jeder Ordner im Paket mit .py-Dateien ist als Paket genannt (auch blender_scripts ohne __init__.py)
        ordner = {".".join(p.parent.relative_to(ROOT).parts) for p in PAKET.rglob("*.py")
                  if "__pycache__" not in p.parts}
        self.assertEqual(ordner, pakete)
        self.assertIn("skptool.blender_scripts", pakete)
        # jede Datei in blender_scripts (Laufzeit: blender.SCRIPTS, live.SERVER_SCRIPT, mcp_server.OPS_DATEI)
        muster = st["package-data"]["skptool.blender_scripts"]
        dateien = [p.name for p in (PAKET / "blender_scripts").iterdir() if p.is_file()]
        self.assertTrue(dateien)
        for name in dateien:
            self.assertTrue(any(fnmatch.fnmatch(name, m) for m in muster), name)
        from skptool import blender, live, mcp_server
        for pfad in (blender.SCRIPTS / "bridge.py", live.SERVER_SCRIPT, mcp_server.OPS_DATEI):
            self.assertTrue(pfad.is_file(), pfad)
            self.assertEqual(pfad.parent, PAKET / "blender_scripts")


# ---------------------------------------------------------------- Bau und Installation

_GEBAUT: dict = {}


def bauen() -> dict:
    """Wheel und sdist aus einer Kopie der Paketdateien bauen (einmal je Testlauf)."""
    if "fehler" in _GEBAUT:
        raise AssertionError(_GEBAUT["fehler"])
    if "wheel" in _GEBAUT:
        return _GEBAUT
    tmp = Path(tempfile.mkdtemp(prefix="skptool_paket_"))
    _GEBAUT["tmp"] = tmp
    quelle = tmp / "quelle"
    quelle.mkdir()
    for name in ("pyproject.toml", "MANIFEST.in", "README.md", "LICENSE"):
        shutil.copy2(ROOT / name, quelle / name)
    shutil.copytree(PAKET, quelle / "skptool", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # relative Pfade: manche uv-Versionen trennen --build-constraints an Leerzeichen
    shutil.copy2(ROOT / "tools" / "paketbau.lock", tmp / "paketbau.lock")
    r = subprocess.run([UV, "build", "--quiet", "--build-constraints", "paketbau.lock", "--require-hashes",
                        "--out-dir", "dist", "quelle"],
                       cwd=tmp, capture_output=True, text=True, timeout=600, env=sauberes_env())
    if r.returncode != 0:
        _GEBAUT["fehler"] = f"uv build gescheitert:\n{r.stdout}\n{r.stderr}"
        raise AssertionError(_GEBAUT["fehler"])
    dist = tmp / "dist"
    _GEBAUT["wheel"] = next(dist.glob("skptool-*.whl"))
    _GEBAUT["sdist"] = next(dist.glob("skptool-*.tar.gz"))
    return _GEBAUT


def installieren() -> Path:
    """Das gebaute Wheel in eine frische venv installieren, Rueckgabe: der Befehl skptool."""
    g = bauen()
    if "befehl" in g:
        return g["befehl"]
    venv = g["tmp"] / "venv"
    env = sauberes_env()
    r = subprocess.run([UV, "venv", "--quiet", "--python", sys.executable, str(venv)],
                       capture_output=True, text=True, timeout=300, env=env)
    if r.returncode != 0:
        raise AssertionError(f"uv venv gescheitert:\n{r.stderr}")
    py = venv / ("Scripts/python.exe" if WINDOWS else "bin/python")
    for args in (["--require-hashes", "--no-deps", "-r", str(ROOT / "requirements.lock")],
                 ["--no-deps", str(g["wheel"])]):
        r = subprocess.run([UV, "pip", "install", "--quiet", "--python", str(py), *args],
                           capture_output=True, text=True, timeout=600, env=env)
        if r.returncode != 0:
            raise AssertionError(f"uv pip install gescheitert:\n{r.stderr}")
    g["befehl"] = venv / ("Scripts/skptool.exe" if WINDOWS else "bin/skptool")
    return g["befehl"]


def tearDownModule():
    if "tmp" in _GEBAUT:
        shutil.rmtree(_GEBAUT["tmp"], ignore_errors=True)
    _GEBAUT.clear()


@unittest.skipUnless(UV, "uv nicht gefunden")
class TestGebaut(unittest.TestCase):
    def test_wheel_inhalt(self):
        with zipfile.ZipFile(bauen()["wheel"]) as z:
            namen = z.namelist()
            info = f"skptool-{__version__}.dist-info"
            meta = email.parser.Parser().parsestr(z.read(f"{info}/METADATA").decode("utf-8"))
            entry = z.read(f"{info}/entry_points.txt").decode("utf-8")
        module = {n for n in namen if n.startswith("skptool/")}
        self.assertEqual(module, quell_module(), "Wheel muss genau die .py-Dateien des Pakets enthalten")
        self.assertTrue({f"skptool/blender_scripts/{n}" for n in ("bridge.py", "ops.py", "refcheck.py",
                                                                  "live_server.py")} <= module)
        fremd = [n for n in namen if not (n.startswith("skptool/") or n.startswith(info + "/"))]
        self.assertEqual(fremd, [])
        self.assertIn(f"{info}/licenses/LICENSE", namen)
        self.assertEqual(meta["Name"], "skptool")
        self.assertEqual(meta["Version"], __version__)
        self.assertEqual(meta["License-Expression"], "MIT")
        self.assertEqual(meta["Requires-Python"], ">=3.12")
        self.assertEqual(sorted(meta.get_all("Requires-Dist")), sorted(requirements_pins(ROOT / "requirements.txt")))
        self.assertIn("skptool = skptool.cli:main", entry)

    def test_sdist_ohne_tests_und_beispiele(self):
        with tarfile.open(bauen()["sdist"]) as t:
            namen = [m.name for m in t.getmembers() if m.isfile()]
        wurzel = f"skptool-{__version__}/"
        self.assertTrue(all(n.startswith(wurzel) for n in namen))
        rel = [n[len(wurzel):] for n in namen]
        oben = {r.split("/", 1)[0] for r in rel}
        self.assertEqual(oben, {"LICENSE", "MANIFEST.in", "PKG-INFO", "README.md", "pyproject.toml", "setup.cfg",
                                "skptool", "skptool.egg-info"})
        self.assertEqual({r for r in rel if r.startswith("skptool/")}, quell_module())
        self.assertFalse([r for r in rel if r.lower().endswith((".skp", ".blend", ".glb", ".png"))])


# ---------------------------------------------------------------- installierter Befehl

def installierter_befehl() -> Path:
    pfad = os.environ.get("SKPTOOL_INSTALLIERT")
    return Path(pfad) if pfad else installieren()


@unittest.skipUnless(os.environ.get("SKPTOOL_INSTALLIERT") or UV,
                     "weder SKPTOOL_INSTALLIERT gesetzt noch uv gefunden")
class TestInstallierterBefehl(unittest.TestCase):
    """Der Starter von pip, uv und pipx ruft Python ohne -P auf. Sicher ist das trotzdem: sys.path[0] ist
    der Ordner des Starters (bin/ bzw. die .exe selbst), nicht der aktuelle Ordner. Diese Tests zeigen das
    mit echten Dateien, wie tests/test_aufruf.py fuer skptool.cmd."""

    @classmethod
    def setUpClass(cls):
        cls.befehl = installierter_befehl()
        if not cls.befehl.is_file():
            raise AssertionError(f"Befehl fehlt: {cls.befehl}")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="skptool_installiert_"))
        self.modell = self.tmp / "modell"
        self.modell.mkdir()
        # Python-Dateien, die beim Import eine Markierung anlegen wuerden, darunter ein falsches Paket skptool
        for mod in ("numpy", "json", "re", "glob", "argparse", "shutil", "subprocess", "zipfile", "openskp"):
            (self.modell / f"{mod}.py").write_text(f"open({str(self.tmp / ('PWNED_' + mod))!r}, 'w')\n")
        falsch = self.modell / "skptool"
        falsch.mkdir()
        (falsch / "__init__.py").write_text(f"open({str(self.tmp / 'PWNED_skptool')!r}, 'w')\n")
        (falsch / "cli.py").write_text(f"open({str(self.tmp / 'PWNED_skptool_cli')!r}, 'w')\ndef main(): pass\n")
        (falsch / "__main__.py").write_text(f"open({str(self.tmp / 'PWNED_skptool_main')!r}, 'w')\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def starten(self, *args, timeout=300):
        return subprocess.run([str(self.befehl), *args], cwd=self.modell, env=sauberes_env(),
                              capture_output=True, text=True, timeout=timeout)

    def gepwnt(self):
        return sorted(p.name for p in self.tmp.glob("PWNED_*"))

    def test_version_ohne_module_aus_dem_aktuellen_ordner(self):
        r = self.starten("--version")
        self.assertEqual(self.gepwnt(), [])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), f"skptool {__version__}")

    @unittest.skipUnless(S2017.exists(), "Beispieldatei fehlt")
    def test_info_convert_und_stapel_aus_feindlichem_ordner(self):
        shutil.copy(S2017, self.modell / "stuhl.skp")
        r = self.starten("info", "stuhl.skp")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Table", r.stdout)
        r = self.starten("convert", "stuhl.skp", "-o", "stuhl.glb", "-q")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertGreater((self.modell / "stuhl.glb").stat().st_size, 1000)
        # Stapel mit Arbeitsprozessen (python -P -m skptool ...) aus demselben Ordner
        shutil.copy(S2017, self.modell / "stuhl2.skp")
        r = self.starten("convert", "stuhl.skp", "stuhl2.skp", "-f", "glb", "-d", "aus", "--jobs", "2", "-q")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(p.name for p in (self.modell / "aus").iterdir()), ["stuhl.glb", "stuhl2.glb"])
        self.assertEqual(self.gepwnt(), [])

    def test_blender_skripte_sind_installiert(self):
        py = self.befehl.parent / ("python.exe" if WINDOWS else "python")
        if not py.exists():
            self.skipTest("kein Python neben dem Befehl (keine venv)")
        code = ("import json, skptool, skptool.blender as b, skptool.live as l, skptool.mcp_server as m; "
                "print(json.dumps([skptool.__file__, sorted(p.name for p in b.SCRIPTS.glob('*.py')), "
                "l.SERVER_SCRIPT.is_file(), m.OPS_DATEI.is_file()]))")
        r = subprocess.run([str(py), "-P", "-c", code], cwd=self.tmp, env=sauberes_env(),
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        datei, skripte, server, ops = json.loads(r.stdout)
        self.assertNotEqual(Path(datei).resolve().parent, PAKET.resolve(), "installiertes Paket, nicht die Quelle")
        self.assertEqual(skripte, sorted(p.name for p in (PAKET / "blender_scripts").glob("*.py")))
        self.assertTrue(server and ops)


if __name__ == "__main__":
    unittest.main()
