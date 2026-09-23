"""skptool von jedem Ordner aus aufrufbar machen, ohne den PATH zu aendern.

Legt in einem Ordner, der schon im PATH steht, einen kleinen Starter an:
  Windows: skptool.cmd, ruft den skptool.cmd dieses Projekts mit absolutem Pfad auf
  sonst:   skptool (Shell-Skript), startet .venv/bin/python -P -m skptool mit PYTHONPATH=Projektordner
Standard-Zielordner: %USERPROFILE%\\.local\\bin (Windows) bzw. ~/.local/bin.

Aufruf (aus dem Projektordner):
  .venv\\Scripts\\python tools\\aufruf_einrichten.py               zeigt nur, was passieren wuerde
  .venv\\Scripts\\python tools\\aufruf_einrichten.py --ja          legt den Starter an
  .venv\\Scripts\\python tools\\aufruf_einrichten.py --entfernen --ja   entfernt ihn wieder
  --ziel ORDNER   anderer Zielordner (absoluter Pfad)

Sicherheit: Es wird nur geschrieben, wenn --ja angegeben ist. Eine fremde skptool-Datei im Zielordner wird
nie ueberschrieben, --entfernen loescht nur einen Starter mit der Markierungszeile unten.
"""
import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKE = "skptool-aufruf: angelegt von tools/aufruf_einrichten.py (Markierung, bitte nicht aendern)"


class Abbruch(Exception):
    pass


def standard_ziel(windows=os.name == "nt"):
    basis = os.environ.get("USERPROFILE") if windows else None
    return Path(basis or Path.home()) / ".local" / "bin"


def starter_name(windows=os.name == "nt"):
    return "skptool.cmd" if windows else "skptool"


def inhalt_windows(repo):
    """Starter fuer cmd/PowerShell: ruft nur den skptool.cmd des Projekts auf, der Rest passiert dort."""
    ziel = str(Path(repo) / "skptool.cmd")
    if any(c in ziel for c in '"\r\n'):
        raise Abbruch(f"Projektpfad mit Anfuehrungszeichen oder Zeilenumbruch wird nicht unterstuetzt: {ziel!r}")
    ziel = ziel.replace("%", "%%")  # in .cmd-Dateien steht %% fuer ein einzelnes %
    # ohne "call": der Projektstarter uebernimmt, sein Exitcode ist der Exitcode des Aufrufs
    return f'@echo off\r\nrem {MARKE}\r\n"{ziel}" %*\r\n'


def _sh_wort(s):
    return "'" + s.replace("'", "'\\''") + "'"


def inhalt_posix(repo):
    """Starter fuer sh: nur der Projektordner im PYTHONPATH, -P gegen den aktuellen Ordner."""
    repo = str(repo)
    if any(c in repo for c in "\r\n\0"):
        raise Abbruch(f"Projektpfad mit Zeilenumbruch wird nicht unterstuetzt: {repo!r}")
    return ("#!/bin/sh\n"
            f"# {MARKE}\n"
            f"PYTHONPATH={_sh_wort(repo)}\n"
            "export PYTHONPATH\n"
            f'exec {_sh_wort(repo + "/.venv/bin/python")} -P -m skptool "$@"\n')


def ist_eigener_starter(pfad):
    try:
        with open(pfad, "rb") as fh:
            kopf = fh.read(4096).decode("latin-1")
    except OSError:
        return False
    return any(MARKE in zeile for zeile in kopf.splitlines()[:5])


def _gleich(a, b):
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except (OSError, ValueError):
        return False


def pfad_ordner():
    """PATH-Eintraege wie bei der Programmsuche, aber ohne leere, "." und andere relative Eintraege:
    die stuenden fuer den aktuellen Ordner, und aus dem wird nie etwas gestartet."""
    for e in os.environ.get("PATH", "").split(os.pathsep):
        e = e.strip().strip('"')
        if _absolut(e):
            yield e


def _absolut(p):
    if os.name == "nt":  # nur C:\... oder \\server\..., nicht \ordner (haengt vom aktuellen Laufwerk ab)
        return p[1:3] in (":\\", ":/") or p[:2] in ("\\\\", "//")
    return p.startswith("/")


def endungen(windows=os.name == "nt"):
    if not windows:
        return [""]
    return [x.lower() for x in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if x.strip()]


def fremde_skptool_dateien(ordner, eigener_name):
    if not ordner.is_dir():
        return []
    fremd = []
    for p in sorted(ordner.iterdir()):
        n = p.name.lower()
        if n != "skptool" and not n.startswith("skptool."):
            continue
        if n == eigener_name.lower() and p.is_file() and ist_eigener_starter(p):
            continue
        fremd.append(p)
    return fremd


def skptool_in(ordner):
    """Das skptool, das die Programmsuche in diesem Ordner fand (Reihenfolge wie PATHEXT), oder None."""
    for e in endungen():
        p = os.path.join(ordner, "skptool" + e)
        if os.path.isfile(p) and (os.name == "nt" or os.access(p, os.X_OK)):
            return p
    return None


def warnungen(ziel, datei):
    """Hinweise zum PATH, aendern aber nichts am Ergebnis."""
    out = []
    ordner = list(pfad_ordner())
    if not any(_gleich(o, ziel) for o in ordner):
        out.append(f"Warnung: {ziel} steht nicht im PATH. Der Starter wird erst gefunden, wenn der Ordner "
                   "im PATH steht (danach ein neues Terminal oeffnen).")
    projekt = [ROOT / "skptool.cmd", ROOT / "skptool"]
    for o in ordner:
        if _gleich(o, ziel):
            break  # hier liegt gleich unser Starter, der wird zuerst gefunden
        treffer = skptool_in(o)
        if treffer and any(_gleich(treffer, p) for p in projekt):
            out.append(f"Hinweis: {treffer} (der Starter dieses Projekts) wird vor {datei} gefunden, "
                       "das ist harmlos.")
            break
        if treffer:
            out.append(f"Warnung: im PATH wird vorher ein anderes skptool gefunden: {treffer}. "
                       f"Der neue Starter {datei} kommt dann nicht zum Zug.")
            break
    return out


def kodieren(text, windows):
    if not windows:
        return text.encode("utf-8"), None
    try:
        return text.encode("ascii"), None
    except UnicodeEncodeError:
        pass
    # cmd liest .cmd-Dateien in der OEM-Codepage der Konsole
    try:
        return text.encode("oem"), ("Hinweis: der Projektpfad enthaelt Nicht-ASCII-Zeichen. Der Starter ist in der "
                                    "OEM-Codepage gespeichert und funktioniert nur, solange die Konsole diese nutzt "
                                    "(kein chcp 65001).")
    except (UnicodeEncodeError, LookupError):
        raise Abbruch("Der Projektpfad enthaelt Zeichen, die cmd in einer .cmd-Datei nicht lesen kann. "
                      "Projekt bitte in einen Ordner mit einfachem Namen legen.") from None


def schreiben(datei, daten, windows):
    datei.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".skptool-", dir=datei.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(daten)
        if not windows:
            os.chmod(tmp, 0o755)
        os.replace(tmp, datei)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def ausfuehren(args):
    windows = os.name == "nt"
    ziel = Path(args.ziel) if args.ziel else standard_ziel(windows)
    if not ziel.is_absolute():
        raise Abbruch(f"--ziel muss ein absoluter Pfad sein, nicht {str(ziel)!r}. "
                      "Ein relativer Pfad haengt vom aktuellen Ordner ab.")
    if ziel.exists() and not ziel.is_dir():
        raise Abbruch(f"{ziel} ist kein Ordner.")
    if _gleich(ziel, ROOT):
        raise Abbruch(f"{ziel} ist der Projektordner selbst, bitte einen Ordner im PATH waehlen.")
    datei = ziel / starter_name(windows)
    trocken = not args.ja
    if trocken:
        print("Trockenlauf: es wird nichts geschrieben oder geloescht. Zum Ausfuehren --ja anhaengen.")

    if args.entfernen:
        if not datei.exists():
            print(f"Nichts zu entfernen: {datei} gibt es nicht.")
            return 0
        if not (datei.is_file() and ist_eigener_starter(datei)):
            raise Abbruch(f"{datei} wurde nicht von diesem Skript angelegt (Markierung fehlt) und bleibt, wie es ist.")
        if trocken:
            print(f"Wuerde entfernen: {datei}")
        else:
            datei.unlink()
            print(f"Entfernt: {datei}")
        return 0

    fremd = fremde_skptool_dateien(ziel, datei.name)
    if fremd:
        raise Abbruch("Im Zielordner liegt schon ein fremdes skptool, es wird nicht ueberschrieben: "
                      + ", ".join(str(p) for p in fremd))
    if windows and not (ROOT / "skptool.cmd").is_file():
        raise Abbruch(f"{ROOT / 'skptool.cmd'} fehlt, der Starter haette nichts aufzurufen.")
    python = ROOT / ".venv" / ("Scripts/python.exe" if windows else "bin/python")
    text = inhalt_windows(ROOT) if windows else inhalt_posix(ROOT)
    daten, hinweis = kodieren(text, windows)
    for w in warnungen(ziel, datei):
        print(w)
    if not python.is_file():
        print(f"Warnung: {python} fehlt noch. Erst die venv anlegen (siehe README), sonst startet der Aufruf nicht.")
    if hinweis:
        print(hinweis)
    aktion = "ersetzen" if datei.exists() else "anlegen"
    if trocken:
        print(f"Wuerde {aktion}: {datei}")
        print("Inhalt:")
        print(text.replace("\r\n", "\n"), end="")
        return 0
    if not ziel.exists():
        print(f"Lege Ordner an: {ziel}")
    schreiben(datei, daten, windows)
    print(f"{'Ersetzt' if aktion == 'ersetzen' else 'Angelegt'}: {datei}")
    print("Test in einem neuen Terminal, aus einem beliebigen Ordner: skptool --version")
    return 0


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # Exitcode 1 statt 2, wie beim Rest des Skripts
        self.print_usage(sys.stderr)
        print(f"Fehler: {message}", file=sys.stderr)
        sys.exit(1)


def main(argv=None):
    ap = _Parser(description="skptool von jedem Ordner aus aufrufbar machen (Starter in einem Ordner im PATH).")
    ap.add_argument("--ja", action="store_true", help="wirklich schreiben bzw. loeschen (sonst nur anzeigen)")
    ap.add_argument("--ziel", help=f"Zielordner, absolut (Standard: {standard_ziel()})")
    ap.add_argument("--entfernen", action="store_true", help="den eigenen Starter wieder entfernen")
    args = ap.parse_args(argv)
    try:
        return ausfuehren(args)
    except Abbruch as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
