"""Laeuft INNERHALB von Blender (blender -b --python bridge.py -- <modus> ...).

Modi:
  import  --glb X --meta Y --out A [--out B ...] [--keep-triangles]
          GLB aus OpenSKP laden, Namen/Ebenen/Materialnamen wiederherstellen, Ausgaben schreiben.
          GLB mit geteilten Meshes und Knotenhierarchie, Ebene und geerbte Bemalung stehen
          als Knoten-Extras (skp_layer, skp_paint) am Objekt.
  load    --in X --out A [--out B ...]
          Beliebige Blender-lesbare Datei laden und in andere Formate schreiben.
  dump    --in X --json Y [--bin Z] [--images DIR] [--allow-external]
          Beliebige Datei laden und Geometrie fuer den SKP-Writer ausgeben: JSON-Kopf Y plus
          Binaerdaten Z (Standard Y + ".bin"), instanzerhaltend (Format siehe dump_meshes_bin).
Ausgabe-Endungen: .blend .fbx .obj .stl .ply .usd .usda .usdc .usdz .abc .glb .gltf .png

Umgebungsvariable SKPTOOL_TIMING=1 gibt Zeiten je Schritt auf stdout aus.
"""
import argparse
import json
import math
import os
import re
import shutil
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # ops.py liegt daneben

import bmesh
import bpy
import ops as skp_ops
import refcheck
import numpy as np
from mathutils import Matrix, Vector

RESULT_PREFIX = "SKPTOOL_RESULT "
DEFAULT_MAT = "SketchUp_Standard"   # unbemalte Flaechen; beim Rueckweg wieder "kein Material"
DEFAULT_RGB = (0.86, 0.86, 0.84)
DUMP_FORMAT = "skptool-dump-bin"
_BLENDER_SUFFIX = re.compile(r"\.\d{3}$")
DUMP_VERSION = 4  # 2: + harte Kanten + Rueckseitenmaterial, 3: + Rueckseiten-UV,
                  # 4: + Hierarchie (parent, Matrix relativ zum Elternobjekt, Leerobjekte)
MESHY = {"MESH", "CURVE", "SURFACE", "META", "FONT"}

_T0 = [time.perf_counter()]
_TIMING = bool(os.environ.get("SKPTOOL_TIMING"))
_ACC = {}


def _lap(name):
    if _TIMING:
        now = time.perf_counter()
        print(f"SKPTOOL_TIMING {name:36s} {now - _T0[0]:8.2f}s", flush=True)
        _T0[0] = now


def _acc(name, t0):
    now = time.perf_counter()
    _ACC[name] = _ACC.get(name, 0.0) + now - t0
    return now


def _flush_acc():
    if _TIMING:
        for k, v in _ACC.items():
            print(f"SKPTOOL_TIMING   {k:34s} {v:8.2f}s", flush=True)
    _ACC.clear()


def emit(data):
    print(RESULT_PREFIX + json.dumps(data), flush=True)


def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


# ---------------------------------------------------------------- Laden / Schreiben

def load_any(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".blend":
        bpy.ops.wm.open_mainfile(filepath=path)
        return
    reset_scene()
    if ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=path)
    elif ext == ".stl":
        bpy.ops.wm.stl_import(filepath=path)
    elif ext == ".ply":
        bpy.ops.wm.ply_import(filepath=path)
    elif ext in (".usd", ".usda", ".usdc", ".usdz"):
        bpy.ops.wm.usd_import(filepath=path)
    elif ext == ".abc":
        bpy.ops.wm.alembic_import(filepath=path)
    else:
        raise ValueError(f"Blender kann '{ext}' nicht importieren")


def load_checked(path, allow_external=False):
    """Fremde Datei laden: vorher auf Netzwerk- und externe Verweise pruefen (refcheck.precheck),
    danach externe Dateien entfernen, bevor irgendetwas ausgewertet wird. Rueckgabe: Verweise."""
    refcheck.precheck(path, allow_external)
    load_any(path)
    return refcheck.strip_external(allow_external)


def write_any(path, render_size=(1600, 1000)):
    path = os.path.abspath(path)
    ext = os.path.splitext(path)[1].lower()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if ext == ".blend":
        bpy.ops.file.pack_all()
        bpy.ops.wm.save_as_mainfile(filepath=path, compress=True)
    elif ext == ".fbx":
        bpy.ops.export_scene.fbx(filepath=path, path_mode="COPY", embed_textures=True)
    elif ext == ".obj":
        bpy.ops.wm.obj_export(filepath=path, path_mode="COPY")
    elif ext == ".stl":
        bpy.ops.wm.stl_export(filepath=path)
    elif ext == ".ply":
        bpy.ops.wm.ply_export(filepath=path)
    elif ext in (".usd", ".usda", ".usdc", ".usdz"):
        bpy.ops.wm.usd_export(filepath=path)
    elif ext == ".abc":
        bpy.ops.wm.alembic_export(filepath=path)
    elif ext in (".glb", ".gltf"):
        bpy.ops.export_scene.gltf(filepath=path, export_format="GLB" if ext == ".glb" else "GLTF_SEPARATE")
    elif ext == ".dae" and hasattr(bpy.ops.wm, "collada_export"):
        bpy.ops.wm.collada_export(filepath=path)
    elif ext == ".png":
        render_preview(path, *render_size)
    else:
        raise ValueError(f"Blender kann '{ext}' nicht schreiben")
    return path


# ---------------------------------------------------------------- SKP-Import-Nachbearbeitung

def _principled(mat):
    if not mat or not mat.use_nodes or not mat.node_tree:
        return None
    for node in mat.node_tree.nodes:
        if node.type == "BSDF_PRINCIPLED":
            return node
    return None


def _mat_rgb(mat):
    node = _principled(mat)
    if node is None:
        return None
    c = node.inputs["Base Color"].default_value
    return tuple(round(c[i] * 255) for i in range(3))


def _mat_image(mat):
    if not mat or not mat.use_nodes or not mat.node_tree:
        return None
    for node in mat.node_tree.nodes:
        if node.type == "TEX_IMAGE" and node.image:
            return node.image
    return None


def _backface_duplicates(me, slot_default, slot_textured=None):
    """Indizes der Flaechen, die als Vorder-/Rueckseiten-Duplikat wegfallen (numpy, O(n log n)).

    Gleiche Regel wie die fruehere Python-Schleife: Schluessel einer Flaeche ist die sortierte
    Liste ihrer auf 5 Stellen gerundeten Eckkoordinaten. Je Gruppe gleicher Schluessel bleibt die
    erste texturierte Flaeche, sonst die erste bemalte, sonst die erste ueberhaupt. Texturen
    zuerst, weil nur die behaltene Seite ihre Texturkoordinaten mitnimmt; eine reine Farbe auf
    der anderen Seite braucht keine und geht als Rueckseitenmaterial mit.
    """
    npoly = len(me.polygons)
    if npoly < 2:
        return np.empty(0, np.int64), np.empty(0, np.int64), None
    nv, nl = len(me.vertices), len(me.loops)
    co = np.empty(nv * 3, np.float32)
    me.vertices.foreach_get("co", co)
    q = np.rint(co.reshape(-1, 3).astype(np.float64) * 1e5).astype(np.int64)
    del co
    # Rang jeder gerundeten Position: gleiche Position -> gleicher Rang
    order = np.lexsort((q[:, 2], q[:, 1], q[:, 0]))
    qs = q[order]
    new = np.ones(nv, bool)
    new[1:] = (qs[1:] != qs[:-1]).any(1)
    rank = np.empty(nv, np.int64)
    rank[order] = np.cumsum(new) - 1
    del q, qs, new, order
    lv = np.empty(nl, np.int32)
    me.loops.foreach_get("vertex_index", lv)
    ls = np.empty(npoly, np.int32)
    me.polygons.foreach_get("loop_start", ls)
    lt = np.empty(npoly, np.int32)
    me.polygons.foreach_get("loop_total", lt)
    mi = np.empty(npoly, np.int32)
    me.polygons.foreach_get("material_index", mi)
    sd = np.array(list(slot_default) + [False], bool)
    st = np.array(list(slot_textured or [False] * len(slot_default)) + [False], bool)
    mic = np.clip(mi, 0, len(slot_default))
    priority = np.where(st[mic], 0, np.where(sd[mic], 2, 1))  # 0 Textur, 1 Farbe, 2 unbemalt
    doomed, keepers = [], []
    for n in np.unique(lt).tolist():
        f = np.flatnonzero(lt == n)
        if len(f) < 2:
            continue
        keys = rank[lv[ls[f, None] + np.arange(n)]]
        keys.sort(axis=1)
        o = np.lexsort([f, priority[f]] + [keys[:, k] for k in range(n - 1, -1, -1)])
        ks = keys[o]
        dup = np.zeros(len(f), bool)
        dup[1:] = (ks[1:] == ks[:-1]).all(1)
        fo = f[o]
        run_start = np.maximum.accumulate(np.where(~dup, np.arange(len(f)), 0))
        doomed.append(fo[dup])
        keepers.append(fo[run_start][dup])
    if not doomed:
        return np.empty(0, np.int64), np.empty(0, np.int64), rank
    return np.concatenate(doomed), np.concatenate(keepers), rank


BACK_UV = "SketchUp_Rueckseite"  # Texturkoordinaten der Rueckseite (eigene UV-Ebene)


def _keep_back_uv(me, doomed, keepers, rank, mi, slot_textured):
    """Texturkoordinaten einer texturierten Rueckseite in die UV-Ebene BACK_UV der behaltenen
    Flaeche uebernehmen (Zuordnung der Ecken ueber die Position). Ohne das verliert eine Flaeche
    mit zwei verschiedenen Texturen (z. B. Scheinwerferglas) die Lage der Rueckseitentextur."""
    main = me.uv_layers.active
    if main is None or not len(doomed) or rank is None:
        return
    st = np.array(list(slot_textured) + [False], bool)
    sel = st[np.clip(mi[doomed], 0, len(slot_textured))]
    if not sel.any():
        return
    d_f, k_f = doomed[sel], keepers[sel]
    npoly, nl = len(me.polygons), len(me.loops)
    ls = np.empty(npoly, np.int32)
    me.polygons.foreach_get("loop_start", ls)
    lt = np.empty(npoly, np.int32)
    me.polygons.foreach_get("loop_total", lt)
    lv = np.empty(nl, np.int32)
    me.loops.foreach_get("vertex_index", lv)
    uv = np.empty(nl * 2, np.float32)
    main.data.foreach_get("uv", uv)
    uv = uv.reshape(-1, 2)

    def loops_of(faces):
        cnt = lt[faces]
        start = np.repeat(ls[faces], cnt)
        offs = np.arange(cnt.sum()) - np.repeat(np.cumsum(cnt) - cnt, cnt)
        return start + offs, cnt

    r = np.int64(rank.max() + 1)
    d_loops, d_cnt = loops_of(d_f)
    key_d = np.repeat(k_f.astype(np.int64), d_cnt) * r + rank[lv[d_loops]]
    k_loops, k_cnt = loops_of(k_f)
    key_k = np.repeat(k_f.astype(np.int64), k_cnt) * r + rank[lv[k_loops]]
    order = np.argsort(key_d, kind="stable")
    pos = np.clip(np.searchsorted(key_d[order], key_k), 0, len(key_d) - 1)
    ok = key_d[order][pos] == key_k
    active_index = me.uv_layers.active_index
    layer = me.uv_layers.get(BACK_UV) or me.uv_layers.new(name=BACK_UV, do_init=True)
    back_uv = np.empty(nl * 2, np.float32)
    layer.data.foreach_get("uv", back_uv)
    back_uv = back_uv.reshape(-1, 2)
    back_uv[k_loops[ok]] = uv[d_loops[order[pos[ok]]]]
    layer.data.foreach_set("uv", back_uv.ravel())
    me.uv_layers.active_index = active_index  # sichtbar bleibt die Vorderseite


BACK_ATTR = "skp_back_material"  # Materialslot der Rueckseite + 1, 0 = keine


def _clean_mesh(me, default_mats, keep_triangles, hard=None):
    """Rueckseiten-Duplikate entfernen, Doppelpunkte verschmelzen, Dreiecke zu Flaechen.

    hard = flache Liste harter SketchUp-Kanten (lokal, Meter) oder None. Mit hard
    werden Kanten weich/hart markiert (sharp_edge) und Flaechen glatt schattiert, und die
    Rueckseitenbemalung bleibt im Flaechenattribut skp_back_material erhalten."""
    t = time.perf_counter()
    slot_default = [bool(m and m.name in default_mats) for m in me.materials]
    slot_textured = [bool(m and m.name not in default_mats and _mat_image(m)) for m in me.materials]
    doomed, keepers, rank = _backface_duplicates(me, slot_default, slot_textured)
    npoly = len(me.polygons)
    mi = np.empty(npoly, np.int32)
    me.polygons.foreach_get("material_index", mi)
    back = np.zeros(npoly, np.int32)
    if len(me.materials):
        # glTF doubleSided (use_backface_culling aus) = beide Seiten gleich bemalt
        ds = np.array([bool(m) and not m.use_backface_culling and m.name not in default_mats
                       for m in me.materials] + [False], bool)
        mic = np.clip(mi, 0, len(me.materials))
        back = np.where(ds[mic], mi + 1, 0).astype(np.int32)
        sd = np.array(slot_default + [True], bool)
        if len(doomed):
            painted = ~sd[mic[doomed]]
            back[keepers[painted]] = mi[doomed[painted]] + 1
    attr = me.attributes.get(BACK_ATTR) or me.attributes.new(BACK_ATTR, "INT", "FACE")
    attr.data.foreach_set("value", back)
    _keep_back_uv(me, doomed, keepers, rank, mi, slot_textured)
    t = _acc("backface_dups_numpy", t)
    bm = bmesh.new()
    bm.from_mesh(me)
    t = _acc("bm.from_mesh", t)
    if len(doomed):
        bm.faces.ensure_lookup_table()
        faces = bm.faces
        bmesh.ops.delete(bm, geom=[faces[i] for i in doomed.tolist()], context="FACES_ONLY")
    t = _acc("bmesh.ops.delete", t)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
    t = _acc("remove_doubles", t)
    delimit = {"MATERIAL", "UV"}
    if hard is not None:
        h = np.asarray(hard, np.float64).reshape(-1, 2, 3).tolist()
        hard_keys = {tuple(sorted((tuple(a), tuple(b)))) for a, b in h}
        for f in bm.faces:
            f.smooth = True
        for e in bm.edges:
            a, b = e.verts
            e.smooth = tuple(sorted((tuple(a.co), tuple(b.co)))) not in hard_keys
        blay = bm.faces.layers.int.get(BACK_ATTR)
        for e in bm.edges:
            lf = e.link_faces
            if len(lf) == 2 and lf[0][blay] != lf[1][blay]:
                e.seam = True
        delimit = {"MATERIAL", "UV", "SHARP", "SEAM"}
    if not keep_triangles:
        bmesh.ops.dissolve_limit(bm, angle_limit=math.radians(0.05), use_dissolve_boundaries=False,
                                 verts=bm.verts, edges=bm.edges, delimit=delimit)
    if hard is not None:
        for e in bm.edges:
            e.seam = False
    t = _acc("dissolve_limit", t)
    bm.to_mesh(me)
    bm.free()
    t = _acc("bm.to_mesh+free", t)
    if "custom_normal" in me.attributes:  # Blender 4.4+: direkt, ohne Operator
        me.attributes.remove(me.attributes["custom_normal"])
    if me.has_custom_normals:  # nur aeltere Blender-Versionen; braucht ein Objekt mit diesem Mesh
        ob = next((o for o in bpy.data.objects if o.data == me), None)
        if ob is not None:
            with bpy.context.temp_override(object=ob, active_object=ob):
                try:
                    bpy.ops.mesh.customdata_custom_splitnormals_clear()
                except Exception:
                    pass
    if hard is None:
        me.shade_flat()
    _acc("normals_clear+shade_flat", t)
    return len(doomed)


def _paint_material(paint, by_name):
    mat = bpy.data.materials.get(paint)
    if mat is None and paint in by_name:
        src = by_name[paint]
        mat = bpy.data.materials.new(paint)
        mat.use_nodes = True
        rgb = [c / 255 for c in src["rgb"]]
        alpha = float(src.get("alpha", 1.0))
        node = _principled(mat)
        node.inputs["Base Color"].default_value = (*rgb, 1.0)
        node.inputs["Alpha"].default_value = alpha
        mat.diffuse_color = (*rgb, alpha)
    return mat


def fix_up_skp_import(meta, keep_triangles=False):
    """Nachbearbeitung der instanzerhaltenden GLB aus skptool.gltf_writer.

    Die GLB ist in Metern, Materialien tragen schon ihre SketchUp-Namen, Ebene und geerbte
    Bemalung stehen als Knoten-Extras (skp_layer, skp_paint) an den Objekten.
    """
    default_mats = set()
    for mat in bpy.data.materials:
        node = _principled(mat)
        if node is not None and mat.diffuse_color[3] < 0.999 and not node.inputs["Alpha"].is_linked:
            node.inputs["Alpha"].default_value = mat.diffuse_color[3]  # Glas auch in Eevee/Cycles
        if mat.get("skp_default") or mat.name.rsplit(".", 1)[0] == DEFAULT_MAT:
            default_mats.add(mat.name)
    _lap("materials")

    # Ebenen (Tags) als Collections
    layer_cols = {}
    root = bpy.context.scene.collection
    for layer in meta["layers"]:
        col = bpy.data.collections.new(layer["name"])
        root.children.link(col)
        layer_cols[layer["name"]] = (col, layer.get("hidden", False))

    def move_to(ob, layer):
        col, _hidden = layer_cols.get(layer) or (root, False)
        for c in list(ob.users_collection):
            if c != col:
                c.objects.unlink(ob)
        if ob.name not in col.objects:
            col.objects.link(ob)

    paint_of = {}  # Objektname -> Material der innersten bemalten Gruppe
    for ob in list(bpy.data.objects):
        move_to(ob, str(ob.get("skp_layer") or "Layer0"))
        paint = ob.get("skp_paint")
        if ob.type == "MESH" and paint and str(paint) != "None":
            paint_of[ob.name] = str(paint)

    for col, hidden in layer_cols.values():
        if hidden:
            col.hide_viewport = True
            col.hide_render = True
        # leere Ebenen bleiben erhalten: sie gehoeren zum SketchUp-Modell
    _lap("collections(+join)")

    # Je EINZIGARTIGEM Mesh (geteilte Meshes nur einmal): Duplikate entfernen, Flaechen bilden
    removed = 0
    hard_all = meta.get("hard_edges") or {}
    for me in bpy.data.meshes:
        if me.users == 0 or not me.polygons:
            continue
        key = me.get("skp_key")
        removed += _clean_mesh(me, default_mats, keep_triangles, hard_all.get(key) if key else None)
    _lap("mesh cleanup (per unique mesh)")
    _flush_acc()

    # Geerbte Gruppenbemalung: unbemalte Flaechen in bemalten Gruppen bekommen deren Material.
    # Bei geteilten Meshes als Objekt-Material (slot.link = 'OBJECT'), das Mesh bleibt unveraendert.
    by_name = {m["name"]: m for m in meta["materials"]}
    inherited = 0
    for ob_name, paint in paint_of.items():
        ob = bpy.data.objects.get(ob_name)
        if ob is None:
            continue
        mat = _paint_material(paint, by_name)
        if mat is None:
            continue
        for slot in ob.material_slots:
            if slot.material and slot.material.name in default_mats:
                slot.link = "OBJECT"  # Mesh ist geteilt, nur dieses Objekt bekommt die Farbe
                slot.material = mat
                inherited += 1
    # Nur noch unbenutzte Materialien aufraeumen
    for mat in list(bpy.data.materials):
        if mat.users == 0:
            bpy.data.materials.remove(mat)
    _lap("inherited_paint+mat_cleanup")
    return {"backface_duplicates_removed": removed, "inherited_paint_slots": inherited}


# ---------------------------------------------------------------- Vorschaubild

def stamp_flags(render):
    """Alle Schalter, mit denen Blender Metadaten ins Bild schreibt (use_stamp_*)."""
    return [p.identifier for p in render.bl_rna.properties
            if p.identifier.startswith("use_stamp") and p.type == "BOOLEAN" and not p.is_readonly]


def render_preview(path, width, height):
    scene = bpy.context.scene
    meshes = [o for o in scene.objects if o.type == "MESH" and o.visible_get()]
    if not meshes:
        raise ValueError("Keine sichtbare Geometrie zum Rendern")
    mn = Vector((math.inf,) * 3)
    mx = Vector((-math.inf,) * 3)
    for o in meshes:
        for c in o.bound_box:
            w = o.matrix_world @ Vector(c)
            mn = Vector(map(min, mn, w))
            mx = Vector(map(max, mx, w))
    center = (mn + mx) / 2
    radius = max((mx - mn).length / 2, 1e-3)
    cam_data = bpy.data.cameras.new("skptool_cam")
    cam = bpy.data.objects.new("skptool_cam", cam_data)
    scene.collection.objects.link(cam)
    direction = Vector((1.0, -1.3, 0.8)).normalized()
    cam_data.lens = 50
    cam_data.sensor_fit = "AUTO"
    fov = 2 * math.atan(cam_data.sensor_width / (2 * cam_data.lens))
    if width >= height:
        fov = fov * height / width
    dist = radius / math.sin(fov / 2) * 1.02
    cam.location = center + direction * dist
    cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()
    cam_data.clip_start = dist / 1000
    cam_data.clip_end = dist * 10
    scene.camera = cam
    if scene.world is None:
        scene.world = bpy.data.worlds.new("skptool_world")
    scene.world.color = (0.92, 0.93, 0.95)
    scene.render.engine = "BLENDER_WORKBENCH"
    sh = scene.display.shading
    sh.light = "STUDIO"
    sh.color_type = "TEXTURE"
    sh.show_object_outline = True
    sh.show_cavity = True
    sh.background_type = "WORLD"
    scene.view_settings.view_transform = "Standard"
    scene.render.resolution_x = width
    scene.render.resolution_y = height
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = path
    for flag in stamp_flags(scene.render):  # keine Metadaten (Dateipfad, Datum, Rechner) im PNG
        setattr(scene.render, flag, False)
    bpy.ops.render.render(write_still=True)
    bpy.data.objects.remove(cam)


# ---------------------------------------------------------------- Dump fuer den SKP-Writer

class _MaterialTable:
    """Materialtabelle des Dumps; Texturen werden beim ersten Gebrauch als PNG gespeichert."""

    def __init__(self, images_dir):
        self.images_dir = images_dir
        self.mats, self.index, self.by_look = [], {}, {}

    def id(self, mat):
        if _is_default(mat):
            return -1
        if mat.name in self.index:
            return self.index[mat.name]
        # Blender haengt bei Namensgleichheit ".001" an. Gleicher Grundname und gleiches Aussehen
        # = dasselbe SketchUp-Material; sonst behaelt der Writer beide unter eigenem Namen.
        base = _BLENDER_SUFFIX.sub("", mat.name)
        look = (base, tuple(_mat_rgb(mat) or ()), round(float(mat.diffuse_color[3]), 3),
                _mat_image(mat).name.split(".")[0] if _mat_image(mat) else None)
        if look in self.by_look:
            self.index[mat.name] = self.by_look[look]
            return self.index[mat.name]
        mats = self.mats
        rgb = _mat_rgb(mat)
        if rgb is None:
            rgb = tuple(round(c * 255) for c in mat.diffuse_color[:3])
        # Deckkraft kann im Alpha-Eingang, im Alpha der Grundfarbe oder (glTF-Import)
        # nur in der Viewport-Farbe stehen. Der kleinste Wert gewinnt.
        alpha = float(mat.diffuse_color[3])
        node = _principled(mat)
        if node is not None:
            if "Alpha" in node.inputs and not node.inputs["Alpha"].is_linked:
                alpha = min(alpha, float(node.inputs["Alpha"].default_value))
            alpha = min(alpha, float(node.inputs["Base Color"].default_value[3]))
        image_path = None
        img = _mat_image(mat)
        if img is not None:
            safe = "".join(ch if ch.isalnum() else "_" for ch in mat.name)[:60]
            src = bpy.path.abspath(img.filepath) if img.packed_file is None and img.filepath else ""
            ext = os.path.splitext(src)[1].lower()
            if src and os.path.isfile(src) and ext in (".png", ".jpg", ".jpeg"):
                # Bilddatei auf der Platte: unveraendert kopieren (kein Qualitaetsverlust)
                image_path = os.path.join(self.images_dir, f"{len(mats):04d}_{safe}{ext}")
                shutil.copyfile(src, image_path)
            else:
                if img.size[0] == 0:  # im Hintergrundmodus laedt Blender Pixel erst bei Bedarf
                    try:
                        img.reload()
                        _ = img.pixels[0]
                    except Exception:
                        pass
                if img.size[0] > 0:
                    image_path = os.path.join(self.images_dir, f"{len(mats):04d}_{safe}.png")
                    try:
                        img_copy = img.copy()
                        img_copy.filepath_raw = image_path
                        img_copy.file_format = "PNG"
                        img_copy.save()
                        bpy.data.images.remove(img_copy)
                    except Exception:
                        image_path = None
            if image_path and not os.path.exists(image_path):
                image_path = None
        self.index[mat.name] = self.by_look[look] = len(mats)
        mats.append({"name": base, "rgba": [*rgb, 255], "alpha": alpha, "image": image_path})
        return self.index[mat.name]


def _mesh_arrays(me):
    """Lokale Geometrie eines Meshes als numpy-Arrays (alles per foreach_get in C)."""
    nv, npoly, nl = len(me.vertices), len(me.polygons), len(me.loops)
    co = np.empty(nv * 3, np.float32)
    me.vertices.foreach_get("co", co)
    lt = np.empty(npoly, np.int32)
    me.polygons.foreach_get("loop_total", lt)
    ls = np.empty(npoly, np.int32)
    me.polygons.foreach_get("loop_start", ls)
    lv = np.empty(nl, np.int32)
    me.loops.foreach_get("vertex_index", lv)
    mi = np.empty(npoly, np.int32)
    me.polygons.foreach_get("material_index", mi)
    # Blender haelt Ecken flaechenweise zusammenhaengend; falls nicht, in Flaechenreihenfolge bringen
    if npoly and not (ls[0] == 0 and np.array_equal(ls[1:], np.cumsum(lt)[:-1])):
        lv = np.concatenate([lv[s:s + n] for s, n in zip(ls.tolist(), lt.tolist())])
    return co.reshape(-1, 3), lt, lv, mi, ls


def _hard_edge_pairs(me, co, lt, lv, mi):
    """Kanten, die in SketchUp als Linie sichtbar sein sollen, als Punktindex-Paare.

    Blender-Semantik: hart = sharp_edge, oder eine angrenzende Flaeche flach schattiert
    (sharp_face), oder Rand/nicht-mannigfaltig. Ausnahme: zwei koplanare Flaechen mit gleichem
    Material ohne sharp_edge (Triangulierungslinien flacher Modelle) bleiben weich."""
    ne, npoly = len(me.edges), len(me.polygons)
    if ne == 0:
        return np.empty((0, 2), np.int32)
    ev = np.empty(ne * 2, np.int32)
    me.edges.foreach_get("vertices", ev)
    ev = ev.reshape(-1, 2)
    se = np.zeros(ne, bool)
    a = me.attributes.get("sharp_edge")
    if a is not None and a.domain == "EDGE":
        a.data.foreach_get("value", se)
    sf = np.zeros(npoly, bool)
    a = me.attributes.get("sharp_face")
    if a is not None and a.domain == "FACE":
        a.data.foreach_get("value", sf)
    le = np.empty(len(me.loops), np.int32)
    me.loops.foreach_get("edge_index", le)
    lp = np.repeat(np.arange(npoly), lt)
    cnt = np.bincount(le, minlength=ne)
    adj_sharp = np.zeros(ne, bool)
    np.logical_or.at(adj_sharp, le, sf[lp])
    # Rand-/Mehrfachkanten nicht pauschal hart: flach schattierte Modelle sind ueber adj_sharp
    # abgedeckt, bei glatten (auch aus SketchUp) entscheidet sharp_edge allein.
    hard = se | adj_sharp
    # koplanar + gleiches Material + kein sharp_edge -> weich
    two = np.flatnonzero((cnt == 2) & ~se)
    if len(two):
        order = np.argsort(le, kind="stable")
        starts = np.concatenate([[0], np.cumsum(cnt)[:-1]])
        f1 = lp[order[starts[two]]]
        f2 = lp[order[starts[two] + 1]]
        nrm = np.empty(npoly * 3, np.float32)
        me.polygons.foreach_get("normal", nrm)
        nrm = nrm.reshape(-1, 3)
        cop = (np.einsum("ij,ij->i", nrm[f1], nrm[f2]) > 0.99995) & (mi[f1] == mi[f2])
        hard[two[cop]] = False
    return ev[hard].astype(np.int32)


def _world_f32(co, mw):
    """mw @ v fuer alle Punkte, bitgleich zu mathutils (float32-Produkte, double-Summe)."""
    m = np.array(mw, dtype=np.float32)
    out = np.empty_like(co)
    for r in range(3):
        acc = (co[:, 0] * m[r, 0]).astype(np.float64)
        acc += (co[:, 1] * m[r, 1]).astype(np.float64)
        acc += (co[:, 2] * m[r, 2]).astype(np.float64)
        acc += np.float64(m[r, 3])
        out[:, r] = acc.astype(np.float32)
    return out


def _is_default(mat):
    return mat is None or _BLENDER_SUFFIX.sub("", mat.name) == DEFAULT_MAT


def _instance_entry(ob, d, inst_mat, scene_col, flatten):
    layer = next((c.name for c in ob.users_collection if c != scene_col), "")
    return {"name": ob.name, "definition": d, "parent": -1,
            "matrix": None if flatten else ob,  # _link_parents setzt die Matrix
            "layer": layer, "hidden": not ob.visible_get(), "material": inst_mat,
            "skp_definition": str(ob.get("skp_definition") or "")}


def _link_parents(instances, included):
    """Naechsten mitgeschriebenen Vorfahren als Elternteil eintragen, Matrix relativ zu ihm.

    Die Matrix ist das Produkt der matrix_local-Kette bis dorthin (bei glTF-Importen genau die
    Knotenmatrizen, gleiche Kopien also bitgleich). Nicht mitgeschriebene Vorfahren (Scher-
    Huellen, Kameras usw.) werden dabei durchlaufen. Bei Knochen- oder Punkt-Eltern gilt die
    Matrix relativ zur Weltmatrix des Vorfahren."""
    for inst in instances:
        ob = inst["matrix"]
        m = np.array(ob.matrix_local, np.float64)
        par, exact = ob.parent, ob.parent_type == "OBJECT"
        while par is not None and par.name not in included:
            m = np.array(par.matrix_local, np.float64) @ m
            exact = exact and par.parent_type == "OBJECT"
            par = par.parent
        if par is None:
            m = np.array(ob.matrix_world, np.float64)  # wie frueher: Blenders Weltmatrix
            inst["parent"] = -1
        else:
            if not exact:
                m = np.linalg.pinv(np.array(par.matrix_world, np.float64)) @ np.array(ob.matrix_world, np.float64)
            inst["parent"] = included[par.name]
        inst["matrix"] = m.reshape(-1).tolist()


def dump_meshes_bin(header_path, bin_path, images_dir, flatten=False):
    """Instanzerhaltender Binaer-Dump. Speicherbedarf O(groesstes Mesh), Laufzeit O(n) in C.

    Header (JSON, klein):
      {"format": "skptool-dump-bin", "version": 1, "unit": "m", "bin": <Dateiname>,
       "materials": [{"name", "rgba", "alpha", "image"}],
       "definitions": [{"name", "offset", "nverts", "npolys", "nloops", "nuv", "uses"}],
       "instances": [{"name", "definition": Index oder -1 (reiner Container), "parent": Index
                      in "instances" oder -1, "matrix": 16 floats zeilenweise, Meter, relativ
                      zum Elternobjekt (ohne Eltern: Welt), "layer", "hidden",
                      "material": Materialindex oder -1, "skp_definition": Name oder ""}]}
    Binaerdatei, je Definition ab "offset" hintereinander (little endian):
      verts   float32[nverts*3]  lokale Koordinaten (bei flatten: Weltkoordinaten)
      ltotal  int32[npolys]      Eckenzahl je Flaeche
      lverts  int32[nloops]      Punktindizes, flaechenweise
      pmat    int32[npolys]      Materialindex je Flaeche, -1 = Standard
      uv      float32[nuv*2]     UV nur fuer Ecken von Flaechen mit Texturmaterial (in Flaechen-
                                 reihenfolge); nuv = 0, wenn das Mesh keine UV-Ebene hat
    Eine Definition = ein geteiltes Mesh mit einer bestimmten effektiven Materialbelegung.
    Objekte, die nur Standard-Slots einheitlich per Objekt-Material ueberschreiben (geerbte
    Bemalung), teilen die Definition und tragen das Material an der Instanz.
    Objekte mit Modifikatoren, Formschluesseln oder Nicht-Mesh-Typen werden ausgewertet und
    bekommen eine eigene Definition.
    """
    os.makedirs(images_dir, exist_ok=True)
    mt = _MaterialTable(images_dir)
    scene_col = bpy.context.scene.collection
    depsgraph = None
    defs, def_index, instances = [], {}, []
    total_faces = placed_faces = 0
    included = {}  # Objektname -> Index in instances
    with open(bin_path, "wb") as fh:
        for ob in bpy.context.scene.objects:
            if ob.type not in MESHY and not (ob.type == "EMPTY" and not flatten
                                             and not ob.get("skp_shear_wrapper")):
                continue  # Scher-Huellen sind durchlaessig: ihr Kind traegt die volle Matrix
            if ob.type == "EMPTY":
                included[ob.name] = len(instances)
                instances.append(_instance_entry(ob, -1, -1, scene_col, flatten))
                continue
            t = time.perf_counter()
            evaluated = (flatten or ob.type != "MESH" or len(ob.modifiers) > 0
                         or getattr(ob.data, "shape_keys", None) is not None)
            if evaluated:
                if depsgraph is None:
                    depsgraph = bpy.context.evaluated_depsgraph_get()
                ev = ob.evaluated_get(depsgraph)
                try:
                    me = ev.to_mesh()
                except RuntimeError:
                    continue
                src = ("eval", ob.name)
            else:
                me, ev = ob.data, None
                src = ("mesh", me.name)
            t = _acc("mesh access/evaluate", t)
            if me is None or not me.polygons:
                if ev is not None:
                    ev.to_mesh_clear()
                if not flatten:  # kann noch Kinder tragen
                    included[ob.name] = len(instances)
                    instances.append(_instance_entry(ob, -1, -1, scene_col, flatten))
                continue
            slots = ob.material_slots
            eff = [mt.id(s.material) for s in slots] or [-1]
            inst_mat = -1
            if not evaluated and slots:
                # Objekt-Materialien nur auf Standard-Slots, alle gleich -> Instanzmaterial
                over = [i for i, s in enumerate(slots) if s.link == "OBJECT"
                        and (me.materials[i] if i < len(me.materials) else None) != s.material]
                if over and all(_is_default(me.materials[i] if i < len(me.materials) else None)
                                for i in over) and len({eff[i] for i in over}) == 1:
                    inst_mat = eff[over[0]]
                    eff = [-1 if i in over else e for i, e in enumerate(eff)]
            key = (src, tuple(eff))
            d = def_index.get(key)
            if d is None:
                co, lt, lv, mi, _ls = _mesh_arrays(me)
                if flatten:
                    co = _world_f32(co, ob.matrix_world)
                slot_ids = np.array(eff + [-1], np.int32)
                pmat = slot_ids[np.clip(mi, 0, len(eff))]
                pmat[mi >= len(eff)] = -1
                uv_layer = me.uv_layers.active
                textured = np.array([bool(m["image"]) for m in mt.mats] + [False], bool)
                uv = np.empty((0, 2), np.float32)
                if uv_layer is not None:
                    tex_poly = textured[np.where(pmat >= 0, pmat, len(mt.mats))]
                    if tex_poly.any():
                        all_uv = np.empty(len(me.loops) * 2, np.float32)
                        uv_layer.data.foreach_get("uv", all_uv)
                        loop_mask = np.repeat(tex_poly, lt)
                        uv = all_uv.reshape(-1, 2)[loop_mask]
                hard = _hard_edge_pairs(me, co, lt, lv, mi)
                pback = np.full(len(lt), -1, np.int32)
                ba = me.attributes.get("skp_back_material")
                if ba is not None and ba.domain == "FACE":
                    bv = np.empty(len(lt), np.int32)
                    ba.data.foreach_get("value", bv)
                    ok = (bv >= 1) & (bv <= len(eff))
                    pback[ok] = slot_ids[bv[ok] - 1]
                # Rueckseiten-UV fuer Flaechen mit texturierter Rueckseite (eigene UV-Ebene,
                # sonst dieselben Koordinaten wie vorne)
                buv = np.empty((0, 2), np.float32)
                back_tex = textured[np.where(pback >= 0, pback, len(mt.mats))]
                src_layer = me.uv_layers.get(BACK_UV) or uv_layer
                if src_layer is not None and back_tex.any():
                    all_b = np.empty(len(me.loops) * 2, np.float32)
                    src_layer.data.foreach_get("uv", all_b)
                    buv = all_b.reshape(-1, 2)[np.repeat(back_tex, lt)]
                entry = {"name": me.name if not evaluated else ob.name, "offset": fh.tell(),
                         "nverts": len(co), "npolys": len(lt), "nloops": len(lv), "nuv": len(uv),
                         "nhard": len(hard), "nbuv": len(buv), "uses": 0}
                for arr in (co, lt, lv, pmat, uv, hard, pback, buv):
                    fh.write(np.ascontiguousarray(arr).tobytes())
                d = def_index[key] = len(defs)
                defs.append(entry)
                total_faces += len(lt)
            t = _acc("arrays+write", t)
            defs[d]["uses"] += 1
            placed_faces += defs[d]["npolys"]
            included[ob.name] = len(instances)
            instances.append(_instance_entry(ob, d, inst_mat, scene_col, flatten))
            if ev is not None:
                ev.to_mesh_clear()
            _acc("instance bookkeeping", t)
    if not flatten:
        _link_parents(instances, included)
    if flatten:
        for inst in instances:
            inst["matrix"] = [float(v) for row in Matrix.Identity(4) for v in row]
    header = {"format": DUMP_FORMAT, "version": DUMP_VERSION, "unit": "m",
              "bin": os.path.basename(bin_path), "materials": mt.mats,
              "definitions": defs, "instances": instances,
              "layers": [c.name for c in bpy.data.collections]}
    with open(header_path, "w", encoding="utf-8") as fh:
        json.dump(header, fh)
    _flush_acc()
    _lap("dump bin (total)")
    meshes = sum(1 for i in instances if i["definition"] >= 0)
    return {"objects": meshes, "containers": len(instances) - meshes,
            "nested": sum(1 for i in instances if i["parent"] >= 0),
            "definitions": len(defs), "faces": placed_faces,
            "unique_faces": total_faces, "materials": len(mt.mats),
            "bin_bytes": os.path.getsize(bin_path)}


# ---------------------------------------------------------------- Einstieg

def apply_ops(path):
    """Bearbeitungsoperationen aus einer JSON-Datei anwenden. Bei einem Fehler wird nichts
    geschrieben: RuntimeError mit den Ergebnissen bis dahin."""
    if not path:
        return None
    with open(path, encoding="utf-8") as fh:
        operations = json.load(fh)
    results = skp_ops.run(operations)
    failed = [r for r in results if not r["ok"]]
    if failed:
        raise OpsFailed(failed[0]["error"], results)
    _lap("ops")
    return results


class OpsFailed(RuntimeError):
    def __init__(self, msg, results):
        super().__init__(msg)
        self.results = results


def scene_stats():
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    return {
        "objects": len(meshes),
        "faces": sum(len(o.data.polygons) for o in meshes),
        "unique_meshes": len({o.data.name for o in meshes}),
        "unique_faces": sum(len(me.polygons) for me in {o.data for o in meshes}),
        "materials": len(bpy.data.materials),
        "images": len([i for i in bpy.data.images if i.size[0] > 0]),
        "collections": [c.name for c in bpy.data.collections],
    }


def main():
    argv = sys.argv[sys.argv.index("--") + 1:]
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["import", "load", "dump", "check"])
    ap.add_argument("--glb")
    ap.add_argument("--meta")
    ap.add_argument("--in", dest="inp")
    ap.add_argument("--out", action="append", default=[])
    ap.add_argument("--json")
    ap.add_argument("--bin")
    ap.add_argument("--flatten", action="store_true", help="dump: Weltkoordinaten, keine Instanzen")
    ap.add_argument("--images")
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=1000)
    ap.add_argument("--keep-triangles", action="store_true")
    ap.add_argument("--allow-external", action="store_true",
                    help="load/dump: lokale Dateien, auf die die Eingabe verweist, uebernehmen")
    ap.add_argument("--ops", help="JSON-Datei mit Bearbeitungsoperationen (siehe ops.py)")
    a = ap.parse_args(argv)
    try:
        if a.mode == "import":
            reset_scene()
            with open(a.meta, encoding="utf-8") as fh:
                meta = json.load(fh)
            _lap("startup+reset+meta")
            # FLAT: keine Custom-Normals anlegen, die ohnehin gleich wieder entfernt werden
            bpy.ops.import_scene.gltf(filepath=a.glb,
                                      import_shading=os.environ.get("SKPTOOL_GLTF_SHADING", "FLAT"))
            _lap("gltf_import")
            fix = fix_up_skp_import(meta, keep_triangles=a.keep_triangles)
            ops_result = apply_ops(a.ops)
            stats = {**scene_stats(), **fix, "ops": ops_result}
            _lap("scene_stats")
            written = [write_any(o, (a.width, a.height)) for o in a.out]
            _lap("write outputs")
            emit({"ok": True, "stats": stats, "written": written})
        elif a.mode == "check":
            # nur pruefen (vor dem Oeffnen in einem Blender-Fenster): nichts wird geschrieben
            ext = load_checked(a.inp, a.allow_external)
            emit({"ok": True, "stats": {"external_files": ext}})
        elif a.mode == "load":
            ext = load_checked(a.inp, a.allow_external)
            ops_result = apply_ops(a.ops)
            stats = {**scene_stats(), "external_files": ext, "external_dropped": not a.allow_external,
                     "ops": ops_result}
            written = [write_any(o, (a.width, a.height)) for o in a.out]
            emit({"ok": True, "stats": stats, "written": written})
        else:
            ext = load_checked(a.inp, a.allow_external)
            _lap("load input")
            ops_result = apply_ops(a.ops)
            images = a.images or os.path.join(os.path.dirname(os.path.abspath(a.json)), "images")
            stats = dump_meshes_bin(a.json, a.bin or a.json + ".bin", images, flatten=a.flatten)
            stats.update(external_files=ext, external_dropped=not a.allow_external, ops=ops_result)
            _lap("dump")
            emit({"ok": True, "stats": stats})
    except OpsFailed as exc:
        emit({"error": f"Bearbeitung abgebrochen: {exc}", "ops": exc.results})
        sys.exit(3)
    except refcheck.RefError as exc:
        emit({"error": str(exc), "refused": True})
        sys.exit(3)
    except Exception as exc:
        traceback.print_exc()
        emit({"error": f"{type(exc).__name__}: {exc}"})
        sys.exit(3)


main()
