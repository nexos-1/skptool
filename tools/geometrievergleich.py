r"""Platzierte Geometrie zweier .skp-Dateien vergleichen (Weltkoordinaten, wie SketchUp liest).

Aufruf: .venv\Scripts\python tools\geometrievergleich.py a.skp b.skp [toleranz_mm]

Vergleicht die Menge aller platzierten Eckpunkte (auf die Toleranz gerastert) und die Anzahl
Dreiecke. Unabhaengig davon, ob die Datei flach oder verschachtelt aufgebaut ist."""
import gc
import sys

import numpy as np

from skptool import core


def points(path, tol_mm):
    sc = core.build_scene(core.open_skp(path))
    pts, tris = [], 0
    for prim in sc.glb_primitives:
        p = np.frombuffer(prim.positions, np.float32).reshape(-1, 3).astype(np.float64)
        pts.append(np.rint(p * 1000.0 / tol_mm).astype(np.int64))
        tris += len(prim.indices) // 3
    del sc
    gc.collect()
    allp = np.unique(np.concatenate(pts), axis=0) if pts else np.empty((0, 3), np.int64)
    return allp, tris


def compare(a, b, tol_mm=1.0):
    pa, ta = points(a, tol_mm)
    pb, tb = points(b, tol_mm)
    va = {tuple(r) for r in pa.tolist()}
    vb = {tuple(r) for r in pb.tolist()}

    def near(src, dst):  # Rasterkanten: auch Nachbarzellen zaehlen
        hit = 0
        for x, y, z in src:
            if (x, y, z) in dst or any((x + i, y + j, z + k) in dst
                                        for i in (-1, 0, 1) for j in (-1, 0, 1) for k in (-1, 0, 1)):
                hit += 1
        return hit

    return {"punkte_a": len(va), "punkte_b": len(vb), "dreiecke_a": ta, "dreiecke_b": tb,
            "a_in_b": near(va, vb) / max(1, len(va)), "b_in_a": near(vb, va) / max(1, len(vb))}


if __name__ == "__main__":
    tol = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
    r = compare(sys.argv[1], sys.argv[2], tol)
    print(f"Punkte: {r['punkte_a']} / {r['punkte_b']}, Dreiecke: {r['dreiecke_a']} / {r['dreiecke_b']}")
    print(f"Punkte von a in b (+-{tol} mm): {r['a_in_b']:.4%}, von b in a: {r['b_in_a']:.4%}")
