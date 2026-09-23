"""Verschachtelung einer .skp-Datei als Baum ausgeben (Definitionen, Platzierungen, Flaechen).

Aufruf: .venv\\Scripts\\python tools\\baum.py datei.skp [max_tiefe]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from skptool import core  # noqa: E402
from skptool.vergleich import baum_zeilen  # noqa: E402


def tree(model, max_depth=4):
    return baum_zeilen(model, max_depth)


if __name__ == "__main__":
    m = core.model_of(core.open_skp(sys.argv[1]))
    print("\n".join(tree(m, int(sys.argv[2]) if len(sys.argv) > 2 else 4)))
    print("platzierte Flaechen:", core.placed_face_count(m))
