"""Blender-Skript: Testszene fuer die Texturausrichtung (6 Flaechen, eine Testtextur).

Aufruf: blender -b --factory-startup -Y --python tools/texturtest_scene.py -- <textur.png> <ziel.blend>
Jede Flaeche hat einen schwarzen Wuerfel an der Ecke mit UV (0, 1), dort muss das rote Feld "OL"
der Textur liegen. Fall 3, 5 und 6 sind nicht achsparallel und pruefen damit die Texturachsen.
"""
import math
import sys

import bpy
from mathutils import Euler, Vector

img_path, out = sys.argv[-2], sys.argv[-1]
bpy.ops.wm.read_factory_settings(use_empty=True)
img = bpy.data.images.load(img_path)
img.pack()
mat = bpy.data.materials.new("Texturtest")
mat.use_nodes = True
tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
tex.image = img
mat.node_tree.links.new(tex.outputs["Color"], mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"])
black = bpy.data.materials.new("Markierung")
black.use_nodes = True
black.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0, 0, 0, 1)
black.diffuse_color = (0, 0, 0, 1)


def plane(name, corners, uvs, marker_index=3):
    me = bpy.data.meshes.new(name)
    me.from_pydata([Vector(c) for c in corners], [], [(0, 1, 2, 3)])
    uvl = me.uv_layers.new(name="UVMap")
    for li, uv in zip(me.polygons[0].loop_indices, uvs):
        uvl.data[li].uv = uv
    me.materials.append(mat)
    bpy.context.scene.collection.objects.link(bpy.data.objects.new(name, me))
    bpy.ops.mesh.primitive_cube_add(size=0.08, location=Vector(corners[marker_index]))
    bpy.context.object.name = name + "_Markierung_OL"
    bpy.context.object.data.materials.append(black)


def transformed(corners, rot, offset):
    m = Euler(rot).to_matrix()
    return [tuple(m @ Vector(c) + Vector(offset)) for c in corners]


SQUARE = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
UV1 = [(0, 0), (1, 0), (1, 1), (0, 1)]
plane("1_Boden", SQUARE, UV1)
plane("2_Wand", [(2, 0, 0), (3, 0, 0), (3, 0, 1), (2, 0, 1)], UV1)
plane("3_Gedreht_2x", transformed([(0, 0, 0), (2, 0, 0), (2, 1, 0), (0, 1, 0)], (0, 0, math.radians(30)), (4, 0, 0)),
      [(0, 0), (2, 0), (2, 1), (0, 1)])
plane("4_UV_90Grad", [(7, 0, 0), (8, 0, 0), (8, 1, 0), (7, 1, 0)], [(0, 1), (0, 0), (1, 0), (1, 1)], marker_index=0)
plane("5_Wand_gedreht", transformed([(0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1)], (0, 0, math.radians(30)), (0, 3, 0)), UV1)
plane("6_Dach_schraeg_2x", transformed([(0, 0, 0), (2, 0, 0), (2, 1, 0), (0, 1, 0)],
                                      (math.radians(35), 0, math.radians(20)), (3, 3, 0)),
      [(0, 0), (2, 0), (2, 1), (0, 1)])
bpy.ops.wm.save_as_mainfile(filepath=out)
print("TESTSZENE_OK")
