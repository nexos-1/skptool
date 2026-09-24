"""Laeuft in einem eigenen Blender im Hintergrund (nie im Fenster des Benutzers):

  blender -b --factory-startup -Y --python export_filter.py -- EIN.blend AUS.blend AUFTRAG.json

Oeffnet die Kopie der Szene, die das Add-on gespeichert hat, und bereitet sie fuer skptool vor:
  keep            Liste von Objektnamen (name_full): nur diese bleiben (Auswahl samt Kindern),
                  fehlt der Eintrag, bleiben alle
  strip_modifiers Modifikatoren entfernen (sonst wertet skptool sie aus, wie beim Befehl convert)
Objekte, deren Elternobjekt wegfaellt, behalten ihre Lage in der Welt. Collections, die erst durch
das Filtern leer werden, fallen weg (sie wuerden sonst leere SketchUp-Tags).
"""
import json
import sys

import bpy

RESULT = "SKPTOOL_IO_FILTER "


def main():
    src, dst, job_path = sys.argv[sys.argv.index("--") + 1:][:3]
    with open(job_path, encoding="utf-8") as fh:
        job = json.load(fh)
    bpy.ops.wm.open_mainfile(filepath=src, load_ui=False)
    scene = bpy.context.scene
    bpy.context.view_layer.update()
    keep = job.get("keep")
    removed = 0
    if keep is not None:
        keep = set(keep)
        world = {ob.name_full: ob.matrix_world.copy() for ob in scene.objects}
        had_objects = {c.name_full for c in bpy.data.collections if c.all_objects}
        doomed = [ob for ob in scene.objects if ob.name_full not in keep]
        orphans = [ob for ob in scene.objects if ob.name_full in keep and ob.parent is not None
                   and ob.parent.name_full not in keep]
        for ob in orphans:
            m = world[ob.name_full]
            ob.parent = None
            ob.matrix_world = m
        for ob in doomed:
            bpy.data.objects.remove(ob, do_unlink=True)
            removed += 1
        for col in list(bpy.data.collections):
            if col.name_full in had_objects and not col.all_objects:
                bpy.data.collections.remove(col)
    stripped = 0
    if job.get("strip_modifiers"):
        for ob in scene.objects:
            while ob.modifiers:
                ob.modifiers.remove(ob.modifiers[0])
                stripped += 1
    bpy.ops.wm.save_as_mainfile(filepath=dst, copy=True, compress=False)
    print(RESULT + json.dumps({"objects": len(scene.objects), "removed": removed, "modifiers_removed": stripped}),
          flush=True)


try:
    main()
except Exception as exc:  # Meldung fuer das Add-on, Rueckgabewert ueber --python-exit-code
    print(RESULT + json.dumps({"error": f"{type(exc).__name__}: {exc}"}), flush=True)
    raise
