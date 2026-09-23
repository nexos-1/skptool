"""Blender finden und Bridge-Skripte im Hintergrundmodus ausfuehren."""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).parent / "blender_scripts"
RESULT_PREFIX = "SKPTOOL_RESULT "
BLENDER_TIMEOUT = int(os.environ.get("SKPTOOL_TIMEOUT", "3600"))


class BlenderError(RuntimeError):
    pass


# Linux/macOS: nur mit Administratorrechten beschreibbare Installationsorte
_PROTECTED_POSIX = ("/usr/bin", "/usr/local/bin", "/opt", "/snap/bin", "/Applications")


def _version_key(path: str):
    """Programmordner vor Benutzerordnern (dort koennte jeder Prozess eine Kopie ablegen),
    innerhalb davon die hoechste Version."""
    bases = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")]
    if os.name != "nt":  # unter Windows koennte jeder Benutzer C:\opt oder C:\usr anlegen
        bases += _PROTECTED_POSIX
    p = os.path.normcase(path)
    protected = any(p.startswith(os.path.normcase(b).rstrip("\\/") + os.sep) for b in bases if b)
    nums = re.findall(r"(\d+)\.(\d+)", path)
    return (protected, tuple(int(n) for n in nums[-1]) if nums else (0, 0))


def _on_path(name: str) -> str | None:
    """Wie shutil.which, aber nur in absoluten PATH-Eintraegen und nie im aktuellen Ordner.

    Windows-Python durchsucht bei which() sonst zuerst den aktuellen Ordner: eine blender.bat in
    einem entpackten Download-Ordner wuerde dann ausgefuehrt."""
    cwd = os.path.normcase(os.path.realpath(os.getcwd()))
    exts = [""] if os.name != "nt" else [".exe"]
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry or not os.path.isabs(entry):
            continue
        if os.path.normcase(os.path.realpath(entry)) == cwd:
            continue
        for ext in exts:
            cand = os.path.join(entry, name + ext)
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return os.path.realpath(cand)
    return None


def find_blender(explicit: str | None = None) -> str:
    """Reihenfolge: --blender, SKPTOOL_BLENDER, Standard-Installationsorte (Programmordner zuerst,
    neueste Version), dann PATH (nur absolute Eintraege, nie der aktuelle Ordner, nur .exe)."""
    for cand in (explicit, os.environ.get("SKPTOOL_BLENDER")):
        if cand:
            if Path(cand).is_file():
                return str(Path(cand).resolve())
            raise BlenderError(f"Blender nicht gefunden unter: {cand}")
    cands: list[str] = []
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("LOCALAPPDATA")):
        if base and os.path.isabs(base):
            cands += glob.glob(os.path.join(base, "Blender Foundation", "Blender*", "blender.exe"))
            cands += glob.glob(os.path.join(base, "Steam", "steamapps", "common", "Blender", "blender.exe"))
    if os.name != "nt":  # unter Windows waere /usr/bin der Ordner C:\usr\bin, den jeder anlegen kann
        cands += glob.glob("/Applications/Blender*.app/Contents/MacOS/Blender")
        cands += glob.glob("/usr/bin/blender") + glob.glob("/snap/bin/blender")
    if cands:
        return sorted(cands, key=_version_key)[-1]
    found = _on_path("blender")
    if found:
        return found
    raise BlenderError(
        "Blender wurde nicht gefunden. Installiere Blender (4.2 oder neuer) oder setze "
        "SKPTOOL_BLENDER auf den Pfad zu blender.exe."
    )


def run_bridge(args: list[str], blender: str | None = None, verbose: bool = False) -> dict:
    """Fuehrt blender_scripts/bridge.py headless aus und liefert dessen JSON-Ergebnis."""
    exe = find_blender(blender)
    # -Y: Python in fremden .blend-Dateien (Treiber, Text-Module) nie automatisch ausfuehren
    cmd = [exe, "-b", "--factory-startup", "-Y", "--python-exit-code", "3",
           "--python", str(SCRIPTS / "bridge.py"), "--", *args]
    env = dict(os.environ, SKPTOOL_TIMING="1") if verbose else None
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                              timeout=BLENDER_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise BlenderError(f"Blender hat nach {BLENDER_TIMEOUT} s nicht geantwortet und wurde beendet "
                           "(Grenze anhebbar mit SKPTOOL_TIMEOUT in Sekunden)") from None
    out = proc.stdout + "\n" + proc.stderr
    if verbose:
        keep = ("SKPTOOL_TIMING", "Error", "Traceback")
        print("\n".join(line for line in out.splitlines() if line.startswith(keep)))
    result = None
    for line in proc.stdout.splitlines():
        if line.startswith(RESULT_PREFIX):
            result = json.loads(line[len(RESULT_PREFIX):])
    if proc.returncode != 0 or result is None or result.get("error"):
        msg = (result or {}).get("error") or "\n".join(out.strip().splitlines()[-25:])
        raise BlenderError(f"Blender-Schritt fehlgeschlagen:\n{msg}")
    return result


def launch_gui(blend_file: str, blender: str | None = None) -> None:
    exe = find_blender(blender)
    subprocess.Popen([exe, "-Y", blend_file], close_fds=True)  # -Y: keine Skripte aus der Datei
