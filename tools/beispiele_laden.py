r"""Zusaetzliche Testdateien aus dem OpenSKP-Repository laden (nicht Teil dieses Repos).

Aufruf: .venv\Scripts\python tools\beispiele_laden.py

Die Dateien enthalten Inhalte Dritter (u. a. ein Automodell mit Herstellerlogo und Namen fremder
Personen in Dateipfaden) und werden deshalb nicht mitgeliefert. Geladen wird ein fester Stand
(Commit unten), jede Datei wird per SHA-256 geprueft und nur bei Uebereinstimmung gespeichert.
Ziel: samples/extern/ (von git ignoriert). Ohne diese Dateien werden die betroffenen Tests
uebersprungen.
"""
import hashlib
import sys
import urllib.request
from pathlib import Path

COMMIT = "cb57112da14e8fac5a61e484c5cf21cd08a8791a"
BASE = f"https://raw.githubusercontent.com/iamahsanmehmood/openskp/{COMMIT}/"
FILES = {
    # Zielname: (Pfad im OpenSKP-Repository, SHA-256)
    "gross_2026.skp": ("packages/cpp/tests/fixtures/coedge_orientation_regression.skp",
                       "c644fc408f034946453c62813c493c4f3cb03f52828aae9c2713370562e804b4"),
    "gondel_2020.skp": ("packages/python/tests/fixtures/gondola_v20.skp",
                        "181f5ee604aa0fc115eed42240571099a65817bf4e49ef39f87577554d319d92"),
}
MAX_BYTES = 20 * 1024 * 1024
TARGET = Path(__file__).resolve().parents[1] / "samples" / "extern"


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as resp:  # nur https auf raw.githubusercontent.com
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Datei ist unerwartet gross")
    return data


def main() -> int:
    TARGET.mkdir(parents=True, exist_ok=True)
    rc = 0
    for name, (path, sha) in FILES.items():
        dst = TARGET / name
        if dst.exists() and hashlib.sha256(dst.read_bytes()).hexdigest() == sha:
            print(f"vorhanden  {name}")
            continue
        try:
            data = fetch(BASE + path)
        except Exception as exc:  # Netzwerk, HTTP-Fehler
            print(f"FEHLER     {name}: {exc}", file=sys.stderr)
            rc = 1
            continue
        got = hashlib.sha256(data).hexdigest()
        if got != sha:
            print(f"FEHLER     {name}: Pruefsumme stimmt nicht ({got}), nicht gespeichert", file=sys.stderr)
            rc = 1
            continue
        tmp = dst.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(dst)
        print(f"geladen    {name} ({len(data) // 1024} KB)")
    return rc


if __name__ == "__main__":
    sys.exit(main())
