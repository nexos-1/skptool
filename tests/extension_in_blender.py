"""Laeuft IN Blender (blender -b --factory-startup --python extension_in_blender.py -- AUFTRAG.json),
gestartet von tests/test_extension.py. Aktiviert die installierte Erweiterung skptool_io, prueft die
Menues und fuehrt Import und Export aus. Ergebnis als JSON-Zeile mit Praefix, Pruefungen macht der Test.
"""
import glob
import json
import os
import sys
import tempfile
import traceback

import bpy

PREFIX = "SKPTOOL_EXT_TEST "
MODULE = "bl_ext.user_default.skptool_io"


def temp_dirs():
    return set(glob.glob(os.path.join(tempfile.gettempdir(), "skptool_io_*")))


def call(op, **kw):
    try:
        return sorted(op(**kw))
    except RuntimeError as exc:  # Operator meldet ERROR -> RuntimeError im Hintergrund
        return ["ERROR: " + str(exc).strip().splitlines()[-1][:300]]


def scene_state():
    sc = bpy.context.scene
    meshes = [o for o in sc.objects if o.type == "MESH"]
    return {"objects": sorted(o.name for o in sc.objects),
            "mesh_objects": len(meshes),
            "unique_meshes": len({o.data.name for o in meshes}),
            "empties": sum(o.type == "EMPTY" for o in sc.objects),
            "collections": sorted(c.name for c in sc.collection.children),
            "materials": sorted({s.material.name for o in meshes for s in o.material_slots if s.material}),
            "selected": sorted(o.name for o in bpy.context.selected_objects),
            "hidden_cols": sorted(c.name for c in sc.collection.children if c.hide_viewport)}


def main():
    job = json.load(open(sys.argv[sys.argv.index("--") + 1], encoding="utf-8"))
    out = job["out"]
    res = {}
    bpy.ops.preferences.addon_enable(module=MODULE)
    mod = sys.modules.get(MODULE)
    res["enabled"] = bool(mod) and MODULE in bpy.context.preferences.addons
    res["addon_file"] = getattr(mod, "__file__", "")
    res["import_menu"] = any(getattr(f, "__module__", "") == MODULE and f.__name__ == "menu_import"
                             for f in bpy.types.TOPBAR_MT_file_import._dyn_ui_initialize())
    res["export_menu"] = any(getattr(f, "__module__", "") == MODULE and f.__name__ == "menu_export"
                             for f in bpy.types.TOPBAR_MT_file_export._dyn_ui_initialize())
    res["file_handler"] = hasattr(bpy.types, "SKPTOOL_IO_FH_skp")
    res["ops"] = [hasattr(bpy.ops.import_scene, "skptool_skp"), hasattr(bpy.ops.export_scene, "skptool_skp")]
    prefs = bpy.context.preferences.addons[MODULE].preferences
    tmp_before = temp_dirs()

    # Fehlerfaelle: relativer Pfad, kein skptool, kaputte Datei
    prefs.skptool_path = "//skptool.exe"
    res["relative_path"] = call(bpy.ops.import_scene.skptool_skp, filepath=job["skp"])
    prefs.skptool_path = os.path.join(out, "gibt_es_nicht", "skptool.exe")
    res["missing_path"] = call(bpy.ops.import_scene.skptool_skp, filepath=job["skp"])
    prefs.skptool_path = job["skptool"]
    kaputt = os.path.join(out, "kaputt.skp")
    with open(kaputt, "wb") as fh:
        fh.write(b"keine SketchUp-Datei")
    res["broken_file"] = call(bpy.ops.import_scene.skptool_skp, filepath=kaputt)
    res["objects_after_errors"] = sorted(o.name for o in bpy.context.scene.objects)

    # Import in die Startszene (Cube, Light, Camera in "Collection")
    res["import"] = call(bpy.ops.import_scene.skptool_skp, filepath=job["skp"])
    res["after_import"] = scene_state()

    # Export nur der Auswahl (nach dem Import ist genau das Importierte ausgewaehlt)
    res["export_sel"] = call(bpy.ops.export_scene.skptool_skp, filepath=os.path.join(out, "auswahl.skp"),
                             use_selection=True)
    # ganze Szene, mit Modifikator am Wuerfel: einmal ausgewertet, einmal ohne
    cube = bpy.data.objects["Cube"]
    arr = cube.modifiers.new("Array", "ARRAY")
    arr.count = 3
    res["export_all"] = call(bpy.ops.export_scene.skptool_skp, filepath=os.path.join(out, "szene.skp"))
    res["export_nomod"] = call(bpy.ops.export_scene.skptool_skp, filepath=os.path.join(out, "szene_ohne_mod.skp"),
                               apply_modifiers=False)
    res["cube_modifiers_after"] = len(cube.modifiers)
    # Szenen-Einheit Zentimeter: 1 Blender-Einheit = 1 cm
    for ob in bpy.context.scene.objects:
        ob.select_set(ob.name in res["after_import"]["selected"])
    bpy.context.scene.unit_settings.scale_length = 0.01
    res["export_cm"] = call(bpy.ops.export_scene.skptool_skp, filepath=os.path.join(out, "auswahl_cm.skp"),
                            use_selection=True)
    bpy.context.scene.unit_settings.scale_length = 1.0
    # Nichts ausgewaehlt
    for ob in bpy.context.scene.objects:
        ob.select_set(False)
    res["export_empty_sel"] = call(bpy.ops.export_scene.skptool_skp, filepath=os.path.join(out, "leer.skp"),
                                   use_selection=True)
    repeated_import(job, out, res)
    res["leftover_temp_dirs"] = sorted(temp_dirs() - tmp_before)
    res["is_dirty_file_path"] = bpy.data.filepath
    print(PREFIX + json.dumps(res), flush=True)


def data_state():
    sc = bpy.context.scene
    return {**scene_state(),
            "all_collections": sorted(c.name for c in bpy.data.collections),
            "all_materials": sorted(m.name for m in bpy.data.materials),
            "all_meshes": sorted(m.name for m in bpy.data.meshes),
            "per_collection": {c.name: len(c.objects) for c in sc.collection.children_recursive}}


def repeated_import(job, out, res):
    """Dieselbe Datei (mit Objekten auf den Tags Chair und Table) mehrmals in eine leere Szene."""
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob)
    for coll in (bpy.data.collections, bpy.data.meshes, bpy.data.materials, bpy.data.images):
        for idb in list(coll):
            if idb.name not in ("Render Result", "Viewer Node"):
                coll.remove(idb)
    res["empty_before_twice"] = data_state()
    res["import_twice"] = [call(bpy.ops.import_scene.skptool_skp, filepath=job["skp_tags"]) for _ in range(2)]
    res["after_twice"] = data_state()
    res["export_twice"] = call(bpy.ops.export_scene.skptool_skp, filepath=os.path.join(out, "doppelt.skp"))
    # Material in der Szene umfaerben: der dritte Import bringt das alte Walnut mit, das jetzt
    # anders aussieht. Es bleibt ein eigenes Material (Blender: "Walnut.001", SketchUp: "Walnut_2").
    walnut = bpy.data.materials["Walnut"]
    node = next(n for n in walnut.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    node.inputs["Base Color"].default_value = (1.0, 0.0, 0.0, 1.0)
    walnut.diffuse_color = (1.0, 0.0, 0.0, 1.0)
    res["import_third"] = call(bpy.ops.import_scene.skptool_skp, filepath=job["skp_tags"])
    res["after_third"] = data_state()
    res["export_third"] = call(bpy.ops.export_scene.skptool_skp, filepath=os.path.join(out, "dreifach.skp"))


try:
    main()
except Exception:
    traceback.print_exc()
    print(PREFIX + json.dumps({"crash": traceback.format_exc()}), flush=True)
    sys.exit(3)
