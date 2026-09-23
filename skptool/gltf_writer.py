"""Instanzerhaltender GLB-Writer fuer SketchUp-Modelle (numpy, ohne trimesh).

Jede eindeutige Geometrie (OpenSKP "mesh resource") wird genau einmal geschrieben und von
allen Platzierungen referenziert. Blender legt daraus verknuepfte Kopien an, Komponenten
bleiben also Komponenten. Einheiten: Meter, Y-up (glTF-Standard).

Jeder Knoten traegt in "extras":
  skp_layer       Ebene (Tag), auch fuer Dateien vor 2021 korrekt ermittelt
  skp_paint       geerbte Gruppenbemalung (Materialname) oder fehlt
  skp_definition  Name der SketchUp-Komponente
Materialien heissen wie in SketchUp. Unbemalte Flaechen bekommen "SketchUp_Standard".
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

import numpy as np

DEFAULT_MAT = "SketchUp_Standard"
DEFAULT_RGBA = [0.86, 0.86, 0.84, 1.0]
_ARRAY_BUFFER, _ELEMENT_ARRAY_BUFFER = 34962, 34963
_FLOAT, _UINT = 5126, 5125


_IDENTITY = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)


MAX_TEXTURE_SIDE = 16384
MAX_NODES = 10_000_000
_PLACEHOLDER_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c63f8ffff3f0005fe02fea7d6a4c50000000049454e44ae426082")


def _image_ok(data: bytes) -> bool:
    """Nur den Bildkopf lesen (kein Dekodieren): leere, kaputte oder riesige Bilder aussortieren,
    bevor sie Blender oder einen anderen Bildleser erreichen (Dekompressionsbomben)."""
    import io

    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
    except Exception:
        return False
    return 0 < w <= MAX_TEXTURE_SIDE and 0 < h <= MAX_TEXTURE_SIDE


def split_sheared(matrix16, tol: float = 1e-6):
    """Gescherte 4x4-Matrix (glTF, spaltenweise) exakt in zwei scherungsfreie zerlegen.

    Rueckgabe (aussen, innen) mit aussen @ innen == Matrix, oder (matrix, None) ohne Scherung.
    Singulaerwertzerlegung A = U S V^T: aussen = Verschiebung + U S, innen = V^T. Spiegelungen
    landen als negatives Vorzeichen in S, beide Drehungen bleiben echte Drehungen.
    """
    m = np.asarray(matrix16, np.float64).reshape(4, 4).T  # zeilenweise, Spalten = Achsen
    a = m[:3, :3]
    cols = [a[:, i] for i in range(3)]
    lens = [np.linalg.norm(c) for c in cols]
    if min(lens) < 1e-12:
        return list(map(float, matrix16)), None
    dots = [abs(np.dot(cols[i], cols[j])) / (lens[i] * lens[j]) for i, j in ((0, 1), (0, 2), (1, 2))]
    if max(dots) < tol:
        return list(map(float, matrix16)), None
    u, s, vt = np.linalg.svd(a)
    flip = np.diag([1.0, 1.0, -1.0])
    if np.linalg.det(u) < 0:
        u, s = u @ flip, s * np.array([1.0, 1.0, -1.0])
    if np.linalg.det(vt) < 0:
        vt, s = flip @ vt, s * np.array([1.0, 1.0, -1.0])
    outer = np.eye(4)
    outer[:3, :3] = u @ np.diag(s)
    outer[:3, 3] = m[:3, 3]
    inner = np.eye(4)
    inner[:3, :3] = vt
    return outer.T.reshape(-1).tolist(), inner.T.reshape(-1).tolist()


def _hard_edges(defn) -> list:
    """sichtbare (nicht weiche, nicht verborgene) Kanten einer Definition, lokal in
    Metern, bitgleich zu den GLB-Positionen (float32(Zoll * 0.0254)), Blender-Achsen (Z oben)."""
    out = []
    for e in defn.edges.values():
        if e.soft or e.hidden:
            continue
        a, b = defn.vertices.get(e.v1_id), defn.vertices.get(e.v2_id)
        if a is None or b is None:
            continue
        out += [a.x, a.y, a.z, b.x, b.y, b.z]
    arr = np.asarray(out, np.float64) * 0.0254
    return arr.astype(np.float32).astype(np.float64).tolist()


class _Buffer:
    def __init__(self):
        self.chunks: list[bytes] = []
        self.size = 0
        self.views: list[dict] = []
        self.accessors: list[dict] = []

    def _view(self, data: bytes, target=None) -> int:
        pad = -self.size % 4
        if pad:
            self.chunks.append(b"\0" * pad)
            self.size += pad
        view = {"buffer": 0, "byteOffset": self.size, "byteLength": len(data)}
        if target:
            view["target"] = target
        self.chunks.append(data)
        self.size += len(data)
        self.views.append(view)
        return len(self.views) - 1

    def accessor(self, arr: np.ndarray, kind: str, target, with_bounds=False) -> int:
        arr = np.ascontiguousarray(arr)
        acc = {"bufferView": self._view(arr.tobytes(), target),
               "componentType": _UINT if arr.dtype == np.uint32 else _FLOAT,
               "count": int(arr.shape[0]), "type": kind}
        if with_bounds and arr.size:
            acc["min"] = arr.min(axis=0).astype(float).tolist()
            acc["max"] = arr.max(axis=0).astype(float).tolist()
        self.accessors.append(acc)
        return len(self.accessors) - 1

    def image(self, data: bytes) -> int:
        return self._view(data)

    def blob(self) -> bytes:
        data = b"".join(self.chunks)
        return data + b"\0" * (-len(data) % 4)


def _material_names(model, isc, textures: bool) -> list[dict]:
    referenced = [mt for mt in model.materials if mt.id is not None]
    by_rgb: dict = {}
    for mt in referenced:
        if mt.color:
            by_rgb.setdefault(tuple(mt.color[:3]), mt.name)
    by_texture: dict = {}
    for mt in referenced:
        if mt.texture is not None and mt.texture.data:
            by_texture.setdefault(mt.texture.data, mt.name)
    out = []
    for gm in isc.gltf_materials:
        pbr = dict(gm["pbrMetallicRoughness"])
        tex_ref = pbr.pop("baseColorTexture", None)
        rgb = tuple(round(c * 255) for c in pbr["baseColorFactor"][:3])
        name, is_default = None, False
        if tex_ref is not None:
            tex = isc.textures[tex_ref["index"]]
            name = by_texture.get(tex.data) or Path(tex.filename or "Textur").stem
            if textures:
                pbr["baseColorTexture"] = {"index": tex_ref["index"]}
        if name is None:
            name = by_rgb.get(rgb)
        if name is None:
            name, is_default = DEFAULT_MAT, True
            pbr["baseColorFactor"] = list(DEFAULT_RGBA)
        mat = {"name": name, "pbrMetallicRoughness": pbr}
        for key in ("doubleSided", "alphaMode"):
            if key in gm:
                mat[key] = gm[key]
        if is_default:
            mat["extras"] = {"skp_default": True}
        out.append(mat)
    return out


def write_instanced_glb(model, isc, instance_info: dict, glb_path: Path, textures: bool = True) -> dict:
    """Schreibt die GLB. instance_info: {(pfad, x_mm, y_mm, z_mm): (ebene, bemalung)}."""
    buf = _Buffer()
    materials = _material_names(model, isc, textures)

    images, gl_textures, dropped = [], [], set()
    if textures and isc.textures:
        for ti, tex in enumerate(isc.textures):
            if not _image_ok(tex.data):
                dropped.add(ti)
                tex_data = _PLACEHOLDER_PNG  # Index bleibt stabil, Material nutzt dann nur die Farbe
            else:
                tex_data = tex.data
            images.append({"bufferView": buf.image(tex_data),
                           "mimeType": "image/png" if ti in dropped else (tex.mime_type or "image/png")})
        gl_textures = [{"source": i, "sampler": 0} for i in range(len(images))]
        for mat in materials:
            ref = mat["pbrMetallicRoughness"].get("baseColorTexture")
            if ref is not None and ref["index"] in dropped:
                del mat["pbrMetallicRoughness"]["baseColorTexture"]

    meshes, mesh_index = [], {}
    hard_edges = {}  # mesh-Schluessel -> float32-Liste (x1,y1,z1,x2,y2,z2)* in Blender-Metern
    for res in isc.mesh_resources:
        prims = []
        for prim in res.primitives:
            if not len(prim.indices):
                continue
            pos = np.frombuffer(prim.positions, dtype=np.float32).reshape(-1, 3) \
                if hasattr(prim.positions, "tobytes") else np.asarray(prim.positions, np.float32).reshape(-1, 3)
            attrs = {"POSITION": buf.accessor(pos, "VEC3", _ARRAY_BUFFER, with_bounds=True)}
            if prim.normals is not None and len(prim.normals) == len(prim.positions):
                nrm = np.asarray(prim.normals, dtype=np.float32).reshape(-1, 3)
                attrs["NORMAL"] = buf.accessor(nrm, "VEC3", _ARRAY_BUFFER)
            if prim.uvs is not None and len(prim.uvs) == 2 * len(pos):
                uv = np.asarray(prim.uvs, dtype=np.float32).reshape(-1, 2).copy()
                uv[:, 1] = 1.0 - uv[:, 1]  # glTF hat den Ursprung oben links, SketchUp unten links
                attrs["TEXCOORD_0"] = buf.accessor(uv, "VEC2", _ARRAY_BUFFER)
            idx = np.asarray(prim.indices, dtype=np.uint32)
            prims.append({"attributes": attrs, "mode": 4, "material": int(prim.material_index),
                          "indices": buf.accessor(idx, "SCALAR", _ELEMENT_ARRAY_BUFFER)})
        if prims:
            mesh_index[res.id] = len(meshes)
            meshes.append({"name": res.definition_name or res.id, "primitives": prims,
                           "extras": {"skp_key": res.id}})
            defn = model.root if res.definition_id == "ROOT" else model.definitions.get(res.definition_id)
            if defn is not None:
                hard_edges[res.id] = _hard_edges(defn)

    nodes: list[dict] = []
    stats = {"nodes": 0, "with_mesh": 0, "layers_fixed": 0, "sheared_split": 0}

    def emit(node, path: str) -> int:
        name = node.name or node.definition_name or "Gruppe"
        my_path = f"{path} / {name}"
        key = (my_path, *(round(v, 2) + 0.0 for v in node.position_mm))
        layer, paint = instance_info.get(key, (node.layer or "Layer0", None))
        if layer != (node.layer or "Layer0"):
            stats["layers_fixed"] += 1
        gl = {"name": name, "extras": {"skp_layer": layer or "Layer0",
                                       "skp_definition": node.definition_name or ""}}
        if paint:
            gl["extras"]["skp_paint"] = paint
        outer, inner = split_sheared(node.matrix)
        wrapper = None
        if inner is not None:
            # Gescherte Matrix: Blender kann Scherung nicht in einem Objekt speichern. Aussen
            # Verschiebung, Drehung und Skalierung, innen die Restdrehung; zusammen exakt.
            stats["sheared_split"] += 1
            wrapper = {"name": name, "matrix": outer, "extras": {"skp_layer": layer or "Layer0",
                                                                 "skp_shear_wrapper": True}}
            gl["matrix"] = inner
        elif any(abs(a - b) > 1e-12 for a, b in zip(node.matrix, _IDENTITY)):
            gl["matrix"] = [float(v) for v in node.matrix]
        if node.mesh_resource_id in mesh_index:
            gl["mesh"] = mesh_index[node.mesh_resource_id]
            stats["with_mesh"] += 1
        if len(nodes) > MAX_NODES:
            raise ValueError(f"mehr als {MAX_NODES} Knoten, Datei vervielfacht sich beim Aufbau")
        nodes.append(gl)
        me = len(nodes) - 1
        stats["nodes"] += 1
        kids = [emit(c, my_path) for c in node.children]
        if kids:
            nodes[me]["children"] = kids
        if wrapper is not None:
            wrapper["children"] = [me]
            nodes.append(wrapper)
            return len(nodes) - 1
        return me

    root = isc.scene_hierarchy
    roots = [emit(c, "ROOT") for c in root.children]
    if root.mesh_resource_id in mesh_index:  # lose Geometrie direkt im Modell
        nodes.append({"name": "Modell", "mesh": mesh_index[root.mesh_resource_id],
                      "extras": {"skp_layer": "Layer0", "skp_definition": ""}})
        roots.append(len(nodes) - 1)
        stats["with_mesh"] += 1

    gltf = {
        "asset": {"version": "2.0", "generator": "skptool"},
        "scene": 0, "scenes": [{"nodes": roots}],
        "nodes": nodes, "meshes": meshes, "materials": materials,
        "buffers": [{"byteLength": 0}], "bufferViews": buf.views, "accessors": buf.accessors,
    }
    if images:
        gltf.update(images=images, textures=gl_textures,
                    samplers=[{"wrapS": 10497, "wrapT": 10497, "magFilter": 9729, "minFilter": 9987}])
    blob = buf.blob()
    gltf["buffers"][0]["byteLength"] = len(blob)
    js = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    js += b" " * (-len(js) % 4)
    body = struct.pack("<II", len(js), 0x4E4F534A) + js + struct.pack("<II", len(blob), 0x004E4942) + blob
    Path(glb_path).write_bytes(struct.pack("<III", 0x46546C67, 2, 12 + len(body)) + body)
    stats["hard_edges"] = hard_edges
    stats.update(meshes=len(meshes), materials=len(materials), images=len(images), textures_dropped=len(dropped),
                 triangles=sum(int(buf.accessors[p["indices"]]["count"]) // 3
                               for m in meshes for p in m["primitives"]))
    return stats
