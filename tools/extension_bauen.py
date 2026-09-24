"""Blender-Erweiterung skptool_io bauen (.zip) und pruefen.

Aufruf (aus dem Projektordner):
  .venv\\Scripts\\python tools\\extension_bauen.py                 baut nach dist\\skptool_io-<version>.zip
  .venv\\Scripts\\python tools\\extension_bauen.py --ziel ORDNER
  --blender PFAD   bestimmtes Blender (sonst wie skptool: SKPTOOL_BLENDER oder Installationsordner)

Nutzt "blender --command extension build" und danach "blender --command extension validate" auf der
fertigen .zip. Blender laeuft dabei mit eigenen, leeren Benutzerordnern (BLENDER_USER_RESOURCES in
einem Temp-Ordner), die echte Blender-Einrichtung bleibt unberuehrt.
Installieren: in Blender Edit > Preferences > Get Extensions > Install from Disk, die .zip waehlen.
"""
import argparse
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUELLE = ROOT / "blender_extension" / "skptool_io"
sys.path.insert(0, str(ROOT))


class Abbruch(Exception):
    pass


def isolierte_umgebung(ordner):
    """Umgebung fuer Blender, in der alle Benutzerordner in ORDNER liegen."""
    ordner = Path(ordner)
    env = dict(os.environ)
    env["BLENDER_USER_RESOURCES"] = str(ordner)
    for name, unter in (("CONFIG", "config"), ("SCRIPTS", "scripts"), ("EXTENSIONS", "extensions"),
                        ("DATAFILES", "datafiles")):
        env[f"BLENDER_USER_{name}"] = str(ordner / unter)
    for k in ("BLENDER_SYSTEM_SCRIPTS", "BLENDER_SYSTEM_EXTENSIONS", "PYTHONPATH", "PYTHONHOME"):
        env.pop(k, None)
    return env


def blender_finden(explizit=None):
    from skptool.blender import BlenderError, find_blender
    try:
        return find_blender(explizit)
    except BlenderError as exc:
        raise Abbruch(str(exc)) from None


def _blender(blender, args, env, timeout=300):
    r = subprocess.run([blender, "--factory-startup", "--command", "extension", *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", env=env, timeout=timeout,
                       stdin=subprocess.DEVNULL)
    return r.returncode, (r.stdout + r.stderr).strip()


def version():
    import tomllib
    with open(QUELLE / "blender_manifest.toml", "rb") as fh:
        return tomllib.load(fh)["version"]


def bauen(ziel, blender):
    """Baut die .zip nach ZIEL und prueft sie. Rueckgabe: Pfad der .zip."""
    ziel = Path(ziel).resolve()
    ziel.mkdir(parents=True, exist_ok=True)
    datei = ziel / f"skptool_io-{version()}.zip"
    with tempfile.TemporaryDirectory(prefix="skptool_ext_bau_") as tmp:
        env = isolierte_umgebung(tmp)
        rc, out = _blender(blender, ["build", "--source-dir", str(QUELLE), "--output-filepath", str(datei)], env)
        if rc != 0 or not datei.is_file():
            raise Abbruch(f"Bauen fehlgeschlagen (Rueckgabewert {rc}):\n{out}")
        pruefen(datei, blender, env)
    return datei


def pruefen(datei, blender, env=None):
    """blender --command extension validate auf der .zip, dazu ein Blick in den Inhalt."""
    if env is None:
        with tempfile.TemporaryDirectory(prefix="skptool_ext_pruef_") as tmp:
            return pruefen(datei, blender, isolierte_umgebung(tmp))
    rc, out = _blender(blender, ["validate", str(datei)], env)
    if rc != 0:
        raise Abbruch(f"Pruefung fehlgeschlagen (Rueckgabewert {rc}):\n{out}")
    with zipfile.ZipFile(datei) as z:
        namen = set(z.namelist())
    fehlt = {"blender_manifest.toml", "__init__.py", "operators.py", "runner.py", "export_filter.py"} - namen
    if fehlt:
        raise Abbruch(f"In {datei.name} fehlt: {', '.join(sorted(fehlt))}")
    unerwuenscht = [n for n in namen if "__pycache__" in n or n.endswith((".pyc", ".zip"))]
    if unerwuenscht:
        raise Abbruch(f"In {datei.name} liegt, was nicht hineingehoert: {', '.join(sorted(unerwuenscht))}")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Blender-Erweiterung skptool_io bauen und pruefen.")
    ap.add_argument("--ziel", default=str(ROOT / "dist"), help="Zielordner (Standard: dist im Projektordner)")
    ap.add_argument("--blender", help="Pfad zu Blender (4.2 oder neuer)")
    a = ap.parse_args(argv)
    try:
        blender = blender_finden(a.blender)
        datei = bauen(a.ziel, blender)
    except (Abbruch, OSError, subprocess.SubprocessError) as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1
    print(f"Gebaut und geprueft: {datei}")
    print("Installieren: Blender > Edit > Preferences > Get Extensions > Install from Disk")
    return 0


if __name__ == "__main__":
    sys.exit(main())
