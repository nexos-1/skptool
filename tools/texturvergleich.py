r"""Texturlage zweier .skp-Dateien vergleichen, gelesen wie SketchUp (OpenSKP-Leser).

Aufruf: .venv\Scripts\python tools\texturvergleich.py original.skp rundreise.skp

Schluessel je texturierter Ecke: Position (mm) + UV modulo 1 (auf 1/100 gerundet). Anteil der
Original-Ecken, die in der Rundreise mit gleicher Texturlage vorkommen; dazu je Originaltextur."""
import gc
import sys

import numpy as np

from skptool import core


def rows(path, with_names=False):
    sc = core.build_scene(core.open_skp(path))
    keys, names = [], []
    for prim in sc.glb_primitives:
        pbr = sc.gltf_materials[prim.material_index]["pbrMetallicRoughness"]
        if "baseColorTexture" not in pbr or not len(prim.uvs):
            continue
        pos = np.rint(np.frombuffer(prim.positions, np.float32).reshape(-1, 3) * 1000).astype(np.int64)
        uv = np.asarray(prim.uvs, np.float64).reshape(-1, 2)
        fr = np.rint((uv % 1.0) * 100).astype(np.int64) % 100
        k = np.concatenate([pos, fr], axis=1)
        keys.append(k)
        if with_names:
            names += [sc.textures[pbr["baseColorTexture"]["index"]].filename.split("\\")[-1][:28]] * len(k)
    del sc
    gc.collect()
    return (np.concatenate(keys) if keys else np.empty((0, 5), np.int64)), np.array(names)


def as_void(a):
    a = np.ascontiguousarray(a)
    return a.view(np.dtype((np.void, a.dtype.itemsize * a.shape[1]))).ravel()


ka, na = rows(sys.argv[1], True)
kb, _ = rows(sys.argv[2])
posb = as_void(np.ascontiguousarray(kb[:, :3]))
hit_pos = np.isin(as_void(np.ascontiguousarray(ka[:, :3])), posb)
hit = np.isin(as_void(ka), as_void(kb))
del kb, posb
if not len(ka):
    sys.exit("Das Original hat keine texturierten Flaechen, es gibt nichts zu vergleichen.")
print(f"texturierte Ecken im Original {len(ka)}, davon Position vorhanden {hit_pos.mean():.1%}, "
      f"gleiche Texturlage {hit[hit_pos].mean():.1%}")
for name in np.unique(na[hit_pos]):
    sel = hit_pos & (na == name)
    print(f"   {name:30} {hit[sel].mean():6.1%}  ({sel.sum()} Ecken)")
