r"""Texturlage zweier .skp-Dateien vergleichen, gelesen wie SketchUp (OpenSKP-Leser).

Aufruf: .venv\Scripts\python tools\texturvergleich.py original.skp rundreise.skp

Schluessel je texturierter Ecke: Position (mm) + UV modulo 1 (auf 1/100 gerundet). Anteil der
Original-Ecken, die in der Rundreise mit gleicher Texturlage vorkommen; dazu je Originaltextur.
Die Rechnung steckt in skptool/vergleich.py (auch fuer skptool diff --texturen)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from skptool.vergleich import _als_void as as_void  # noqa: E402,F401
from skptool.vergleich import textur_vergleich  # noqa: E402
from skptool.vergleich import textur_zeilen as rows  # noqa: E402


def main(original, rundreise):
    ka, na = rows(original, True)
    kb, _ = rows(rundreise)
    if not len(ka):
        sys.exit("Das Original hat keine texturierten Flaechen, es gibt nichts zu vergleichen.")
    r = textur_vergleich(ka, na, kb)
    hit_pos, hit = r["hit_pos"], r["hit"]
    print(f"texturierte Ecken im Original {len(ka)}, davon Position vorhanden {hit_pos.mean():.1%}, "
          f"gleiche Texturlage {hit[hit_pos].mean():.1%}")
    for name, anteil, anzahl in r["je_textur"]:
        print(f"   {name:30} {anteil:6.1%}  ({anzahl} Ecken)")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
