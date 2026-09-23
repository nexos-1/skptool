r"""Platzierte Geometrie zweier .skp-Dateien vergleichen (Weltkoordinaten, wie SketchUp liest).

Aufruf: .venv\Scripts\python tools\geometrievergleich.py a.skp b.skp [toleranz_mm]

Vergleicht die Menge aller platzierten Eckpunkte (auf die Toleranz gerastert) und die Anzahl
Dreiecke. Unabhaengig davon, ob die Datei flach oder verschachtelt aufgebaut ist.
Die Rechnung steckt in skptool/vergleich.py (auch fuer skptool diff --geometrie)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from skptool.vergleich import geometrie_punkte as points  # noqa: E402,F401
from skptool.vergleich import geometrie_vergleich as compare  # noqa: E402


if __name__ == "__main__":
    tol = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
    r = compare(sys.argv[1], sys.argv[2], tol)
    print(f"Punkte: {r['punkte_a']} / {r['punkte_b']}, Dreiecke: {r['dreiecke_a']} / {r['dreiecke_b']}")
    print(f"Punkte von a in b (+-{tol} mm): {r['a_in_b']:.4%}, von b in a: {r['b_in_a']:.4%}")
