"""End-to-End-Tests fuer skptool. Start: .venv\\Scripts\\python -m unittest discover -s tests -v

Blender-Tests laufen nur, wenn Blender gefunden wird (sonst werden sie uebersprungen).
"""
import io
import json
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import numpy as np
from openskp import SkpFile

from skptool import cli, core
from skptool.gltf_writer import split_sheared
from skptool.blender import BlenderError, find_blender

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
S2017 = SAMPLES / "stuhl_tisch_2017.skp"
S2025 = SAMPLES / "leer_2025.skp"
# Dateien mit Inhalten Dritter liegen nicht im Repository: tools/beispiele_laden.py laedt sie
S2020 = SAMPLES / "extern" / "gondel_2020.skp"
S2026 = SAMPLES / "extern" / "gross_2026.skp"
EXTERN_HINT = "Beispieldatei fehlt, laden mit: python tools/beispiele_laden.py"
need_2020 = unittest.skipUnless(S2020.exists(), EXTERN_HINT)
need_2026 = unittest.skipUnless(S2026.exists(), EXTERN_HINT)

try:
    find_blender()
    HAVE_BLENDER = True
except BlenderError:
    HAVE_BLENDER = False


def run_cli(*args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            cli.main(list(args))
        except SystemExit as exc:
            code = exc.code
    return code, buf.getvalue()


def faces_of(path):
    m = SkpFile.open(str(path)).parse()
    return len(m.root.faces) + sum(len(d.faces) for d in m.definitions.values()), m


def edge_and_back_stats(path):
    """Platzierte sichtbare Kanten und Flaechen mit Rueckseitenmaterial (wie in SketchUp sichtbar)."""
    m = core.model_of(core.open_skp(path))
    memo = {}

    def count(d, depth=0):
        if id(d) in memo:
            return memo[id(d)]
        tot = [sum(1 for e in d.edges.values() if not e.soft and not e.hidden), len(d.faces),
               sum(1 for f in d.faces.values() if f.back_material_id is not None)]
        if depth < 64:
            for inst in d.instances:
                ref = m.definitions.get(inst.ref_idx)
                if ref is not None:
                    tot = [a + b for a, b in zip(tot, count(ref, depth + 1))]
        memo[id(d)] = tot
        return tot

    return count(m.root)


def placed_count(m, name):
    """Wie oft eine Definition im Modell tatsaechlich steht (ueber alle Verschachtelungen)."""
    def walk(d, depth=0):
        n = 0
        for inst in d.instances:
            ref = m.definitions.get(inst.ref_idx)
            if ref is not None and depth < 64:
                n += (ref.name == name) + walk(ref, depth + 1)
        return n
    return walk(m.root)


def tree_signature(m):
    """Verschachtelung als vergleichbarer Baum: (Name, Flaechen, sortierte Kinder)."""
    def walk(d, depth=0):
        kids = [] if depth >= 64 else sorted(walk(m.definitions[i.ref_idx], depth + 1)
                                             for i in d.instances if i.ref_idx in m.definitions)
        return (d.name, len(d.faces), tuple(kids))
    return walk(m.root)[1:]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="skptool_test_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestInfo(Base):
    def test_versions_and_content(self):
        code, out = run_cli("info", "--json", str(S2017))
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["version"], "17.0.1")
        self.assertEqual([m["name"] for m in data["materials"]], ["Walnut", "Oak", "Charcoal Linen", "Brass"])
        self.assertEqual(data["size_m"], {"width": 1.245, "depth": 0.508, "height": 0.864})

    @need_2026
    def test_2026_file_is_readable(self):
        data = core.info(S2026)
        self.assertTrue(data["version"].startswith("26."))
        self.assertGreater(data["faces_total"], 40000)

    def test_not_a_sketchup_file(self):
        bogus = self.tmp / "kaputt.skp"
        bogus.write_bytes(b"hello world" * 10)
        code, _ = run_cli("info", str(bogus))
        self.assertEqual(code, 1)


class TestNativeExport(Base):
    def test_all_native_formats(self):
        for ext in (".glb", ".obj", ".stl", ".ply", ".dxf", ".ifc", ".json"):
            out = self.tmp / f"x{ext}"
            code, text = run_cli("convert", str(S2017), "-o", str(out))
            self.assertEqual(code, 0, text)
            self.assertGreater(out.stat().st_size, 1000, ext)
        self.assertTrue((self.tmp / "x.glb").read_bytes().startswith(b"glTF"))
        self.assertIn("IFC4", (self.tmp / "x.ifc").read_text(errors="ignore")[:2000])

    def test_glb_names_layers_and_paint_of_unnamed_instances(self):
        """Unbenannte Instanzen: OpenSKP 1.3.0 nennt sie "Component_<n>" oder nach der Definition.
        skptool bleibt bei Instanzname, sonst Definitionsname (auch "Group#1"), und Ebene sowie
        Bemalung (bei Dateien vor 2021 von skptool ergaenzt) gehen dabei nicht verloren."""
        b = core.create()
        mat = b.add_material("Rot", [200, 0, 0, 255])
        lay = b.add_layer("Rahmen")
        with b.add_component_definition("Group#1") as g:
            g.add_face([(0, 0, 0), (10, 0, 0), (10, 10, 0)])
        with b.add_component_definition("Tisch") as t:
            t.add_face([(0, 0, 0), (10, 0, 0), (10, 0, 10)])
        b.add_instance(g, translation=(10, 0, 0), layer=lay, material=mat, name="")
        b.add_instance(g, translation=(30, 0, 0), name="")
        b.add_instance(t, translation=(50, 0, 0), layer=lay, name="")
        b.add_instance(t, translation=(70, 0, 0), name="Links")
        src = self.tmp / "gruppen.skp"
        core.save_atomic(b, src)
        out = self.tmp / "gruppen.glb"
        core.export_native(core.open_skp(src), out)
        data = out.read_bytes()
        gltf = json.loads(data[20:20 + int.from_bytes(data[12:16], "little")])
        got = sorted((n["name"], n["extras"]["skp_layer"], n["extras"].get("skp_paint")) for n in gltf["nodes"])
        self.assertEqual(got, [("Group#1", "Layer0", None), ("Group#1", "Rahmen", "Rot"),
                               ("Links", "Layer0", None), ("Tisch", "Rahmen", None)])

    def test_ifc_is_millimetres_z_up(self):
        """Seit OpenSKP 1.3.0: IFC-Koordinaten in Millimetern (wie die erklaerte Einheit) und Z oben.
        Vorher standen dort Zoll-Werte unter der Einheit mm, das Modell lag auf der Seite."""
        out = self.tmp / "stuhl.ifc"
        core.export_native(core.open_skp(S2017), out)
        text = out.read_text(encoding="utf-8")
        self.assertIn("IFCSIUNIT(*,.LENGTHUNIT.,.MILLI.,.METRE.)", text)
        pts = [tuple(float(v) for v in m.group(1).split(","))
               for m in re.finditer(r"\((-?[\d.eE+-]+,-?[\d.eE+-]+,-?[\d.eE+-]+)\)", text)]
        z = [p[2] for p in pts]
        self.assertAlmostEqual(max(z) - min(z), 864.0, delta=1.0)  # Tischhoehe 0,864 m

    def test_batch_with_format_and_outdir(self):
        code, text = run_cli("convert", str(SAMPLES / "*_20*.skp"), "-f", "glb", "-d", str(self.tmp))
        self.assertEqual(code, 0, text)
        self.assertEqual(sorted(p.name for p in self.tmp.glob("*.glb")), ["leer_2025.glb", "stuhl_tisch_2017.glb"])


class TestLegacyRewrite(Base):
    @need_2026
    def test_2026_to_2017_keeps_materials_and_opacity(self):
        out = self.tmp / "auto_2017.skp"
        stats = core.rewrite_legacy(S2026, out)
        self.assertEqual(stats["version"], "{17.0.1}")
        faces, m = faces_of(out)
        self.assertGreaterEqual(faces, stats["faces_source"] - 5)  # Triangulierung erhoeht die Zahl
        src = SkpFile.open(str(S2026)).parse()
        want = {mt.name: round(mt.transparency, 2) for mt in src.materials if mt.id is not None}
        got = {mt.name: round(mt.transparency, 2) for mt in m.materials}
        self.assertEqual(want, got)
        self.assertEqual(sum(1 for mt in m.materials if mt.texture), 15)

    @need_2020
    def test_per_face_paint_is_identical_for_2020_file(self):
        out = self.tmp / "gondel_2017.skp"
        core.rewrite_legacy(S2020, out)

        def paint(path):
            m = SkpFile.open(str(path)).parse()
            nm = lambda i: m.materials_by_id[i].name if i in m.materials_by_id else ""  # noqa: E731
            return sorted((d.name, nm(f.material_id), nm(f.back_material_id))
                          for d in m.definitions.values() for f in d.faces.values())
        self.assertEqual(paint(S2020), paint(out))

    def test_applied_texture_size_and_placement_survive(self):
        """Seit OpenSKP 1.3.0 skaliert add_face die Texturpunkte mit der Materialgroesse. Die echte
        Kachelgroesse bleibt so erhalten (Materialbrowser, in SketchUp neu bemalte Flaechen), die
        Texturlage jeder Flaeche auch: positioniert, projiziert, oben wie unten."""
        from PIL import Image

        png = self.tmp / "holz.png"
        Image.new("RGB", (8, 8), (150, 90, 40)).save(png)
        b = core.create()
        mat = b.add_texture_material("Holz", str(png), applied_width=20.0, applied_height=10.0)
        b.add_face([(0, 0, 0), (40, 0, 0), (40, 30, 0), (0, 30, 0)], material=mat,
                   front_uv=[((0, 0, 0), (0.1, 0.0)), ((40, 0, 0), (1.5, 0.2)), ((0, 30, 0), (-0.3, 2.0))])
        b.add_face([(50, 0, 0), (90, 0, 0), (90, 30, 0), (50, 30, 0)], material=mat)
        b.add_face([(0, 40, 5), (0, 70, 5), (40, 70, 5), (40, 40, 5)], material=mat)  # Unterseite
        src = self.tmp / "quelle.skp"
        core.save_atomic(b, src)
        out = self.tmp / "neu.skp"
        core.rewrite_legacy(src, out)

        def sizes(path):
            return {mt.name: (round(mt.texture.width, 6), round(mt.texture.height, 6))
                    for mt in SkpFile.open(str(path)).parse().materials if mt.texture}

        def uvs(path):
            sc = core.build_scene(core.open_skp(path))
            got = {}
            for prim in sc.glb_primitives:
                if not len(prim.uvs):
                    continue
                pos = np.asarray(prim.positions, np.float64).reshape(-1, 3)
                for p, t in zip(pos, np.asarray(prim.uvs, np.float64).reshape(-1, 2)):
                    got[tuple(np.round(p, 4))] = t
            return got

        self.assertEqual(sizes(src), {"Holz": (20.0, 10.0)})
        self.assertEqual(sizes(out), sizes(src))
        a, c = uvs(src), uvs(out)
        self.assertEqual(len(a), 12)
        self.assertEqual(set(a), set(c))
        self.assertLess(max(float(np.abs(a[k] - c[k]).max()) for k in a), 1e-4)

    def test_face_me_components_survive(self):
        """"Immer zur Kamera drehen" und "Schatten zur Sonne" (2D-Personen, Baeume) kann der
        OpenSKP-Writer seit 1.3.0 schreiben, rewrite_legacy uebernimmt beides."""
        b = core.create()
        with b.add_component_definition("Person", always_faces_camera=True, shadows_face_sun=True) as p:
            p.add_face([(0, 0, 0), (10, 0, 0), (10, 0, 20)])
        with b.add_component_definition("Kiste") as k:
            k.add_face([(0, 0, 0), (10, 0, 0), (10, 10, 0)])
        b.add_instance(p, translation=(0, 0, 0))
        b.add_instance(k, translation=(20, 0, 0))
        src = self.tmp / "person.skp"
        core.save_atomic(b, src)
        out = self.tmp / "person_2017.skp"
        core.rewrite_legacy(src, out)

        def flags(path):
            return {d.name: (d.always_faces_camera, d.shadows_face_sun)
                    for d in SkpFile.open(str(path)).parse().definitions.values()}

        self.assertEqual(flags(src), {"Person": (True, True), "Kiste": (False, False)})
        self.assertEqual(flags(out), flags(src))


class TestTriangulation(unittest.TestCase):
    def test_concave_non_planar_polygon(self):
        poly = [(0, 0, 0), (2, 0, 0.01), (2, 2, 0), (1, 1, 0.02), (0, 2, 0)]
        tris = core.triangulate_polygon(poly)
        n = core._newell(poly)
        self.assertTrue(all(sum(a * b for a, b in zip(core._newell(t), n)) > 0 for t in tris))
        self.assertAlmostEqual(sum(0.5 * abs(core._newell(t)[2]) for t in tris), 3.0, places=3)

    def test_polygon_with_hole(self):
        outer = [(0, 0, 0), (4, 0, 0), (4, 4, 0.01), (0, 4, 0)]
        hole = [(1, 1, 0), (1, 3, 0), (3, 3, 0), (3, 1, 0)]
        tris = core.triangulate_polygon(outer, [hole])
        self.assertAlmostEqual(sum(0.5 * abs(core._newell(t)[2]) for t in tris), 12.0, places=2)


class TestSafety(Base):
    def _fake_skp(self, name, payload, entries=1):
        import io
        import zipfile
        head = S2025.read_bytes()[:200]
        cut = head.find(b"PK")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for i in range(entries):
                z.writestr(f"model{i}.dat", payload)
        path = self.tmp / name
        path.write_bytes(head[:cut] + buf.getvalue())
        return path

    def test_zip_bomb_is_rejected_before_parsing(self):
        bomb = self._fake_skp("bombe.skp", bytes(1) * (20 * 2**20))  # 20 MB Nullen, Verhaeltnis ~1000:1
        with self.assertRaises(core.UnsafeFileError):
            core.check_container(bomb)
        code, _ = run_cli("info", str(bomb))
        self.assertEqual(code, 1)

    def test_real_files_pass_the_container_check(self):
        for f in (S2026, S2020, S2017, S2025):
            if f.exists():
                core.check_container(f)

    def test_output_may_not_overwrite_input(self):
        src = self.tmp / "a.skp"
        shutil.copy(S2017, src)
        with self.assertRaises(SystemExit):
            cli.main(["convert", str(src), "-o", str(src)])
        self.assertEqual(src.read_bytes(), S2017.read_bytes())


class TestErrorMessages(Base):
    def _err(self, *args):
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, _ = run_cli(*args)
        return code, err.getvalue()

    def test_random_bytes_are_called_not_sketchup(self):
        f = self.tmp / "zufall.skp"
        f.write_bytes(bytes(range(256)) * 50)
        code, err = self._err("convert", str(f), "-o", str(self.tmp / "x.glb"), "-q")
        self.assertEqual(code, 1)
        self.assertIn("keine SketchUp-Datei", err)

    def test_truncated_file_is_called_damaged(self):
        f = self.tmp / "abgeschnitten.skp"
        f.write_bytes(S2025.read_bytes()[: 9_000])
        code, err = self._err("convert", str(f), "-o", str(self.tmp / "x.glb"), "-q")
        self.assertEqual(code, 1)
        self.assertIn("beschaedigt", err)
        self.assertNotIn(" @0x", err)  # keine Hex-Auszuege ohne -v


class TestShearSplit(unittest.TestCase):
    def test_random_matrices_are_reproduced_exactly(self):
        rng = np.random.default_rng(7)
        for trial in range(500):
            a = rng.normal(size=(3, 3))
            if trial % 2:
                a[:, 0] *= -1  # Spiegelung
            m = np.eye(4)
            m[:3, :3], m[:3, 3] = a, rng.normal(size=3)
            outer, inner = split_sheared(m.T.reshape(-1).tolist())
            o, i = (np.array(x).reshape(4, 4).T for x in (outer, inner))
            self.assertLess(np.abs(o @ i - m).max(), 1e-9)
            for part in (o, i):  # beide Teile ohne Scherung
                g = part[:3, :3].T @ part[:3, :3]
                self.assertLess(np.abs(g - np.diag(np.diag(g))).max(), 1e-9)

    def test_unsheared_matrix_stays_single(self):
        self.assertIsNone(split_sheared([2, 0, 0, 0, 0, 3, 0, 0, 0, 0, 1, 0, 5, 6, 7, 1])[1])


SHEAR_SCENE = """
import bpy, math, sys
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=1)
parent = bpy.context.object; parent.name = "Eltern"
parent.scale = (3.0, 1.0, 0.5); parent.rotation_euler = (0.3, 0.2, 0.9)
bpy.ops.mesh.primitive_cube_add(size=1)
child = bpy.context.object; child.name = "Kind"
child.parent = parent; child.location = (0.8, 0.4, 1.5); child.rotation_euler = (0.7, 0.0, 1.1)
bpy.ops.object.duplicate_move_linked()
twin = bpy.context.object; twin.name = "Zwilling"; twin.location = (-0.8, -0.3, 1.2)
bpy.ops.wm.save_as_mainfile(filepath=sys.argv[-1])
"""

BOUNDS = """
import bpy, json, sys
from mathutils import Vector
bpy.ops.wm.open_mainfile(filepath=sys.argv[-2])
out = {}
for o in bpy.data.objects:
    if o.type == "MESH":
        pts = [o.matrix_world @ v.co for v in o.data.vertices]
        out[o.name.split(".")[0]] = sorted([round(c, 4) for p in pts for c in p])
json.dump(out, open(sys.argv[-1], "w"))
"""


def run_blender_script(code, workdir, *args):
    import subprocess
    script = Path(workdir) / "blender_script.py"  # im Testordner, wird mit ihm geloescht
    script.write_text(code, encoding="utf-8")
    subprocess.run([find_blender(), "-b", "--factory-startup", "--python", str(script), "--", *map(str, args)],
                   check=True, capture_output=True)


WORLD_UVS = """
import bpy, json, sys
bpy.ops.wm.open_mainfile(filepath=sys.argv[-2])
out = []
for o in bpy.data.objects:
    if o.type != "MESH" or not o.data.uv_layers:
        continue
    uvl = o.data.uv_layers.active
    for p in o.data.polygons:
        for li in p.loop_indices:
            co = o.matrix_world @ o.data.vertices[o.data.loops[li].vertex_index].co
            out.append([[round(c, 3) for c in co], list(uvl.data[li].uv)])
json.dump(out, open(sys.argv[-1], "w"))
"""


def subprocess_run_scene(script, *args):
    import subprocess
    subprocess.run([find_blender(), "-b", "--factory-startup", "-Y", "--python", str(script), "--",
                    *map(str, args)], check=True, capture_output=True)


LINE_ONLY_SCENE = """
import bpy, sys
bpy.ops.wm.read_factory_settings(use_empty=True)
me = bpy.data.meshes.new("Linie")
me.from_pydata([(0, 0, 0), (1, 0, 0)], [(0, 1)], [])
bpy.context.scene.collection.objects.link(bpy.data.objects.new("Linie", me))
bpy.ops.wm.save_as_mainfile(filepath=sys.argv[-1])
"""

EXTERNAL_IMAGE_SCENE = """
import bpy, sys
bpy.ops.wm.read_factory_settings(use_empty=True)
img_path, out = sys.argv[-2], sys.argv[-1]
img = bpy.data.images.new("aussen", 4, 4)
img.filepath_raw = img_path; img.file_format = "PNG"; img.save()
bpy.ops.mesh.primitive_cube_add(size=1)
mat = bpy.data.materials.new("MitBild"); mat.use_nodes = True
tex = mat.node_tree.nodes.new("ShaderNodeTexImage"); tex.image = img
mat.node_tree.links.new(tex.outputs["Color"], mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"])
bpy.context.object.data.materials.append(mat)
bpy.ops.wm.save_as_mainfile(filepath=out)
"""


# In Blender gebaute Hierarchie: gedrehtes, ungleich skaliertes Leerobjekt "Regal" (Tag Aussen) mit
# zwei Brettern (gemeinsames Mesh, Tag Innen), geparentet wie mit Strg+P (Parent-Inverse-Matrix)
REGAL_SCENE = """
import bpy, sys, math, json
bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene
aussen = bpy.data.collections.new("Aussen"); sc.collection.children.link(aussen)
innen = bpy.data.collections.new("Innen"); sc.collection.children.link(innen)
regal = bpy.data.objects.new("Regal", None)
aussen.objects.link(regal)
regal.location = (1, 0, 0)
regal.rotation_euler = (0, 0, math.radians(90))
regal.scale = (1, 1, 2)
bpy.ops.mesh.primitive_cube_add(size=0.2, location=(0, 0, 0))
cube = bpy.context.object
me = cube.data
bpy.data.objects.remove(cube)
bretter = []
for name, loc in (("Brett", (1, 2, 0.5)), ("Brett", (3, 2, 0.5))):
    ob = bpy.data.objects.new(name, me)
    innen.objects.link(ob)
    ob.location = loc
    bpy.context.view_layer.update()
    ob.parent = regal
    ob.matrix_parent_inverse = regal.matrix_world.inverted()  # wie Strg+P "Objekt"
    bretter.append(ob)
bpy.context.view_layer.update()
world = [[list(v) for v in (o.matrix_world @ vv.co for vv in o.data.vertices)] for o in bretter]
json.dump(world, open(sys.argv[-1], "w"))
bpy.ops.wm.save_as_mainfile(filepath=sys.argv[-2])
"""


# Blender haengt bei doppelten Namen ".001" an (zweiter Import, Umschalt+D, neue Collection mit
# vergebenem Namen). Beim Schreiben der .skp sollen daraus keine eigenen Tags, Materialien und
# Komponenten werden, solange es dasselbe ist.
DOPPELTE_NAMEN_SCENE = """
import bpy, sys
bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene

def col(name, tag=None, hidden=False):
    c = bpy.data.collections.new(name)
    sc.collection.children.link(c)
    if tag:
        c["skp_tag"] = tag
    c.hide_viewport = hidden
    return c

def mat(name, rgb):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    m.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (*rgb, 1)
    m.diffuse_color = (*rgb, 1)
    return m

def box(name, size, material=None):
    me = bpy.data.meshes.new(name)
    v = [(x, y, z) for x in (0, size[0]) for y in (0, size[1]) for z in (0, size[2])]
    me.from_pydata(v, [], [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)])
    if material:
        me.materials.append(material)
    return me

def place(name, me, c, x):
    ob = bpy.data.objects.new(name, me)
    c.objects.link(ob)
    ob.location = (x, 0, 0)

lack, lack_blau = mat("Lack", (1, 0, 0)), mat("Lack", (0, 0, 1))  # "Lack.001" sieht anders aus
holz, holz_kopie = mat("Holz", (0.5, 0.25, 0.0)), mat("Holz", (0.5, 0.25, 0.0))  # gleich
l0, l0b = col("Layer0"), col("Layer0")
h, hb = col("Holz"), col("Holz", hidden=True)
a, ab = col("Aus", hidden=True), col("Aus", hidden=True)
st, st1 = col("Stage"), col("Stage.001", tag="Stage.001")  # in SketchUp wirklich "Stage.001"
ez = col("Einzeln.001")
stuhl = col("Stuhl", tag="Chair")  # nach dem Import umbenannt
names = [m.name for m in (lack_blau, holz_kopie)] + [c.name for c in (l0b, hb, ab, st1)]
assert names == ["Lack.001", "Holz.001", "Layer0.001", "Holz.001", "Aus.001", "Stage.001"], names
# zuerst benutzt wird "Lack.001": trotzdem bekommt "Lack" den Namen ohne Endung
place("Platte", box("Platte", (1, 1, 0.1), lack_blau), l0, 0)
place("Latte", box("Latte", (1, 0.1, 0.1), lack), l0b, 1.5)
brett = box("Brett", (0.2, 0.5, 0.02), holz)
place("Brett_sichtbar", brett, h, 3)
place("Brett_versteckt", brett.copy(), hb, 4)                        # "Brett.001", gleicher Inhalt
place("Brett_anders", box("Brett", (0.3, 0.5, 0.02), holz_kopie), h, 5)  # "Brett.002", anders
place("Kiste_aus", box("Kiste_aus", (0.1, 0.1, 0.1)), a, 6)
place("Kiste_aus2", box("Kiste_aus2", (0.2, 0.1, 0.1)), ab, 7)
place("Kiste_stage", box("Kiste_stage", (0.3, 0.1, 0.1)), st, 8)
place("Kiste_stage1", box("Kiste_stage1", (0.4, 0.1, 0.1)), st1, 9)
place("Kiste_einzeln", box("Kiste_einzeln", (0.5, 0.1, 0.1)), ez, 10)
place("Kiste_stuhl", box("Kiste_stuhl", (0.6, 0.1, 0.1)), stuhl, 11)
bpy.ops.wm.save_as_mainfile(filepath=sys.argv[-1])
"""


@unittest.skipUnless(HAVE_BLENDER, "Blender nicht installiert")
class TestBlender(Base):
    def test_blender_namensendungen_werden_zusammengefuehrt(self):
        src = self.tmp / "doppelt.blend"
        run_blender_script(DOPPELTE_NAMEN_SCENE, self.tmp, src)
        skp = self.tmp / "doppelt.skp"
        code, text = run_cli("convert", str(src), "-o", str(skp))
        self.assertEqual(code, 0, text)
        m = core.model_of(core.open_skp(skp))
        layers = {l.name: bool(l.hidden) for l in m.layers}
        self.assertEqual(layers, {"Layer0": False, "Holz": False, "Aus": True, "Stage": False,
                                  "Stage.001": False, "Einzeln.001": False, "Stuhl": False})
        inst = {i.name: i for i in m.root.instances}
        tag = {n: (i.layer if i.layer not in (None, "") else "Layer0") for n, i in inst.items()}
        self.assertEqual(tag, {"Platte": "Layer0", "Latte": "Layer0", "Brett_sichtbar": "Holz",
                               "Brett_versteckt": "Holz", "Brett_anders": "Holz", "Kiste_aus": "Aus",
                               "Kiste_aus2": "Aus", "Kiste_stage": "Stage", "Kiste_stage1": "Stage.001",
                               "Kiste_einzeln": "Einzeln.001", "Kiste_stuhl": "Stuhl"})
        # sichtbarer Tag Holz: nur das Objekt aus der ausgeblendeten "Holz.001" ist selbst verborgen
        self.assertEqual(sorted(n for n, i in inst.items() if i.hidden), ["Brett_versteckt"])
        colors = {mt.name: list(mt.color)[:3] for mt in m.materials}
        self.assertEqual(colors, {"Lack": [255, 0, 0], "Lack_2": [0, 0, 255], "Holz": [128, 64, 0]})
        # "Brett.001" mit gleichem Inhalt ist dieselbe Komponente, "Brett.002" eine eigene Definition
        self.assertEqual(inst["Brett_sichtbar"].ref_idx, inst["Brett_versteckt"].ref_idx)
        self.assertNotEqual(inst["Brett_sichtbar"].ref_idx, inst["Brett_anders"].ref_idx)
        self.assertEqual(m.definitions[inst["Brett_sichtbar"].ref_idx].name, "Brett")

    def test_external_files_are_dropped_unless_allowed(self):
        import contextlib
        outside = self.tmp / "anderswo"
        outside.mkdir()
        src = self.tmp / "fremd.blend"
        run_blender_script(EXTERNAL_IMAGE_SCENE, self.tmp, outside / "geheim.png", src)
        # Standard: nur eingebettete Daten, externe Bilder nur auf ausdruecklichen Wunsch
        for extra, expect_texture in (([], 0), (["--allow-external"], 1)):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code, text = run_cli("convert", str(src), "-o", str(self.tmp / "x.skp"), *extra)
            self.assertEqual(code, 0, text)
            self.assertIn("geheim.png", err.getvalue())
            self.assertIn(f"{expect_texture} Texturmaterialien", text)

    def test_blender_timeout_stops_the_job(self):
        from skptool import blender as bl
        # 0,05 s: so kurz, dass selbst ein schnell startendes Blender (Linux) sicher nicht fertig wird
        with mock.patch.object(bl, "BLENDER_TIMEOUT", 0.05):
            with self.assertRaises(BlenderError) as ctx:
                bl.run_bridge(["load", "--in", str(S2017), "--out", str(self.tmp / "x.fbx")])
        self.assertIn("nicht geantwortet", str(ctx.exception))

    def test_sheared_hierarchy_round_trip_is_exact(self):
        src = self.tmp / "schief.blend"
        run_blender_script(SHEAR_SCENE, self.tmp, src)
        skp = self.tmp / "schief.skp"
        code, text = run_cli("convert", str(src), "-o", str(skp))
        self.assertEqual(code, 0, text)
        self.assertIn("1 Komponenten mit 2 Platzierungen", text)  # Kind und Zwilling teilen ein Mesh
        back = self.tmp / "zurueck.blend"
        code, text = run_cli("convert", str(skp), "-o", str(back))
        self.assertEqual(code, 0, text)
        before, after = self.tmp / "a.json", self.tmp / "b.json"
        run_blender_script(BOUNDS, self.tmp, src, before)
        run_blender_script(BOUNDS, self.tmp, back, after)
        a, b = json.loads(before.read_text()), json.loads(after.read_text())
        self.assertIn("Kind", a)  # wirklich die Testszene, nicht Blenders Standardwuerfel
        self.assertEqual(sorted(a), sorted(b))
        for name in a:
            self.assertEqual(len(a[name]), len(b[name]), name)
            self.assertLess(max(abs(x - y) for x, y in zip(a[name], b[name])), 1e-3, name)

    def test_skp_to_blend_restores_names_and_materials(self):
        blend = self.tmp / "stuhl.blend"
        code, text = run_cli("convert", str(S2017), "-o", str(blend))
        self.assertEqual(code, 0, text)
        self.assertIn("14 Objekte", text)
        self.assertIn("128 Flaechen", text)  # exakt die SketchUp-Flaechen, keine Dreiecke/Duplikate

    @need_2020
    def test_layers_and_inherited_paint_for_legacy_file(self):
        blend = self.tmp / "gondel.blend"
        code, text = run_cli("convert", str(S2020), "-o", str(blend))
        self.assertEqual(code, 0, text)
        self.assertIn("Gondulas Laterais", text)  # 14 Gruppen liegen auf dieser Ebene
        back = self.tmp / "gondel.skp"
        code, text = run_cli("convert", str(blend), "-o", str(back))
        self.assertEqual(code, 0, text)
        m = SkpFile.open(str(back)).parse()
        self.assertIn("Gondulas Laterais", [l.name for l in m.layers])
        self.assertIn("[Color E01]", [mt.name for mt in m.materials])  # geerbte Gruppenfarbe

    @need_2026
    def test_soft_edges_and_back_materials_survive(self):
        blend, back = self.tmp / "auto.blend", self.tmp / "auto.skp"
        self.assertEqual(run_cli("convert", str(S2026), "-o", str(blend), "-q")[0], 0)
        self.assertEqual(run_cli("convert", str(blend), "-o", str(back), "-q", "--no-verify")[0], 0)
        (e0, f0, b0), (e1, f1, b1) = edge_and_back_stats(S2026), edge_and_back_stats(back)
        # vorher: 232.512 statt 86.162 sichtbare Kanten (Rundungen als Drahtgitter)
        self.assertLess(abs(e1 - e0) / e0, 0.05, (e0, e1))
        self.assertGreaterEqual(b1 / f1, b0 / f0)  # Rueckseitenbemalung nicht verloren

    def test_failed_write_keeps_existing_target(self):
        src = self.tmp / "nur_linie.blend"
        run_blender_script(LINE_ONLY_SCENE, self.tmp, src)
        target = self.tmp / "wichtig.skp"
        shutil.copy(S2017, target)
        code, _ = run_cli("convert", str(src), "-o", str(target), "-q")
        self.assertEqual(code, 1)
        self.assertEqual(target.read_bytes(), S2017.read_bytes())  # frueher: 0 Byte
        self.assertEqual(list(self.tmp.glob(".*skptool-tmp")), [])

    def test_layers_and_material_names_survive_round_trip(self):
        blend, back = self.tmp / "auto.blend", self.tmp / "auto.skp"
        self.assertEqual(run_cli("convert", str(S2017), "-o", str(blend), "-q")[0], 0)
        self.assertEqual(run_cli("convert", str(blend), "-o", str(back), "-q")[0], 0)
        m = core.model_of(core.open_skp(back))
        self.assertTrue({"Chair", "Table"} <= {l.name for l in m.layers})  # auch leere Ebenen
        self.assertFalse([mt.name for mt in m.materials if re.search(r"\.\d{3}$", mt.name)])

    def test_texture_placement_matches_sketchup(self):
        """Texturausrichtung wie SketchUp sie anzeigt, auch auf gedrehten und schraegen Flaechen.

        Der OpenSKP-Leser rechnet nachweislich wie SketchUp (Pruefung von texturtest.skp in
        SketchUp am 2026-09-22). Vorher lag die Textur auf in der Ebene gedrehten Flaechen schief."""
        from PIL import Image
        png = self.tmp / "textur.png"
        Image.new("RGB", (64, 64), (200, 50, 50)).save(png)
        blend, skp = self.tmp / "t.blend", self.tmp / "t.skp"
        subprocess_run_scene(ROOT / "tools" / "texturtest_scene.py", png, blend)
        self.assertEqual(run_cli("convert", str(blend), "-o", str(skp), "-q")[0], 0)
        expected = self.tmp / "uv.json"
        run_blender_script(WORLD_UVS, self.tmp, blend, expected)
        want = {tuple(k): v for k, v in json.loads(expected.read_text())}
        sc = core.build_scene(core.open_skp(skp))
        checked, worst = 0, 0.0
        for prim in sc.glb_primitives:
            pbr = sc.gltf_materials[prim.material_index]["pbrMetallicRoughness"]
            if "baseColorTexture" not in pbr or not len(prim.uvs):
                continue  # nur texturierte Seiten; untexturierte haben UVs in Zoll
            pos = [prim.positions[i:i + 3] for i in range(0, len(prim.positions), 3)]
            uvs = [prim.uvs[i:i + 2] for i in range(0, len(prim.uvs), 2)]
            for (x, y, z), (u, v) in zip(pos, uvs):  # Szene ist Y-oben: Blender (x, y, z) = (x, -z, y)
                key = (round(x, 3), round(-z, 3), round(y, 3))
                if key in want:
                    checked += 1
                    worst = max(worst, abs(u - want[key][0]), abs(v - want[key][1]))
        self.assertGreaterEqual(checked, 20)  # 6 Flaechen x 4 Ecken, je Seite
        self.assertLess(worst, 1e-3)

    @need_2026
    def test_real_model_texture_placement_survives(self):
        """Auto mit Rueckseiten-Texturen und verzerrten ("fixierte Pins") Texturen.

        Vorher 10,8 % (22-MB-Testmodell) bzw. 77,5 % (Auto) gleiche Texturlage, jetzt ueber 95 %."""
        blend, back = self.tmp / "auto.blend", self.tmp / "auto.skp"
        self.assertEqual(run_cli("convert", str(S2026), "-o", str(blend), "-q")[0], 0)
        self.assertEqual(run_cli("convert", str(blend), "-o", str(back), "-q", "--no-verify")[0], 0)

        def table(path):
            sc = core.build_scene(core.open_skp(path))
            rows = {}
            for prim in sc.glb_primitives:
                pbr = sc.gltf_materials[prim.material_index]["pbrMetallicRoughness"]
                if "baseColorTexture" not in pbr or not len(prim.uvs):
                    continue
                pos = np.round(np.frombuffer(prim.positions, np.float32).reshape(-1, 3), 3).tolist()
                for p, t in zip(pos, np.asarray(prim.uvs, np.float64).reshape(-1, 2).tolist()):
                    rows.setdefault(tuple(p), []).append(t)
            return rows

        a, b = table(S2026), table(back)
        shared = [k for k in a if k in b]
        same = sum(any(np.all(np.abs((np.subtract(u, w) + 0.5) % 1.0 - 0.5) < 0.01) for u in a[k] for w in b[k])
                   for k in shared)
        self.assertGreater(len(shared), 2000)
        self.assertGreater(same / len(shared), 0.90)

    def test_edit_operations_round_trip(self):
        ops = [
            {"op": "move", "select": {"name": "Table"}, "by": [0, 0.5, 0]},
            {"op": "set_material", "select": {"name": "Tabletop"}, "material": "Anthrazit", "color": [55, 58, 62]},
            {"op": "duplicate", "select": {"name": "Chair"}, "offset": [0.7, 0, 0], "count": 2},
            {"op": "add_box", "name": "Deko_Box", "size": 0.3, "at": [-1, 0, 0], "material": "Signalrot",
             "color": [220, 30, 30], "layer": "Deko"},
        ]
        out = self.tmp / "bearbeitet.skp"
        code, text = run_cli("edit", str(S2017), "-o", str(out), "--ops", json.dumps(ops), "-q")
        self.assertEqual(code, 0, text)
        self.assertIn("created=2", text)
        m = core.model_of(core.open_skp(out))
        self.assertIn("Anthrazit", [mt.name for mt in m.materials])
        self.assertIn("Deko", [l.name for l in m.layers])
        # 1 Stuhl + 2 Kopien: die Kopien sind gleich, also EINE Komponente "Chair" dreimal
        # platziert, darin 4 Beine (verschachtelt wie im Original) = 12 Beine im Modell
        self.assertEqual(placed_count(m, "Chair"), 3)
        self.assertEqual(placed_count(m, "Leg_Chair"), 12)
        chair = next(d for d in m.definitions.values() if d.name == "Chair")
        self.assertEqual(sum(m.definitions[i.ref_idx].name == "Leg_Chair" for i in chair.instances), 4)
        # Stuhl: Rahmen 24 + Lehne 6 + Polster 6 + Sitz 6 + 4 Beine x 7 = 70 Flaechen, Box 6
        self.assertEqual(core.placed_face_count(m), 128 + 2 * 70 + 6)

    def test_nesting_survives_round_trip(self):
        blend, back = self.tmp / "stuhl.blend", self.tmp / "zurueck.skp"
        self.assertEqual(run_cli("convert", str(S2017), "-o", str(blend), "-q")[0], 0)
        self.assertEqual(run_cli("convert", str(blend), "-o", str(back), "-q")[0], 0)
        orig, m = core.model_of(core.open_skp(S2017)), core.model_of(core.open_skp(back))
        # gleicher Baum: Chair > Backrest > BackrestFrame usw., Beine als Komponente
        self.assertEqual(tree_signature(m), tree_signature(orig))
        self.assertEqual(core.placed_face_count(m), 128)

    def test_edited_copy_gets_own_definition(self):
        ops = [
            {"op": "duplicate", "select": {"name": "Chair"}, "offset": [0.7, 0, 0]},
            {"op": "scale", "select": {"name": "Leg_Chair.004"}, "factor": 1.5},
        ]
        out = self.tmp / "zwei_stuehle.skp"
        code, text = run_cli("edit", str(S2017), "-o", str(out), "--ops", json.dumps(ops), "-q")
        self.assertEqual(code, 0, text)
        m = core.model_of(core.open_skp(out))
        names = sorted(d.name for d in m.definitions.values() if d.name.startswith("Chair"))
        self.assertEqual(names, ["Chair", "Chair#2"])  # Kopie mit geaendertem Bein ist eigenstaendig
        self.assertEqual(placed_count(m, "Leg_Chair"), 8)  # Bein-Geometrie bleibt eine Komponente
        self.assertEqual(sum(d.name == "Leg_Chair" for d in m.definitions.values()), 1)
        self.assertEqual(core.placed_face_count(m), 128 + 70)

    def test_blender_hierarchy_becomes_nested_groups(self):
        blend, world_json, out = self.tmp / "regal.blend", self.tmp / "welt.json", self.tmp / "regal.skp"
        run_blender_script(REGAL_SCENE, self.tmp, blend, world_json)
        self.assertEqual(run_cli("convert", str(blend), "-o", str(out), "-q")[0], 0)
        m = core.model_of(core.open_skp(out))
        self.assertEqual(len(m.root.instances), 1)
        regal = m.root.instances[0]
        self.assertEqual((m.definitions[regal.ref_idx].name, regal.layer), ("Regal", "Aussen"))
        inner = m.definitions[regal.ref_idx].instances
        self.assertEqual([i.layer for i in inner], ["Innen", "Innen"])
        self.assertEqual(len({i.ref_idx for i in inner}), 1)  # gemeinsames Mesh = eine Komponente
        # Weltlage wie in Blender (GLB ist Y-oben: Blender (x, y, z) = GLB (x, -z, y))
        sc = core.build_scene(core.open_skp(out))
        pts = np.concatenate([np.frombuffer(p.positions, np.float32).reshape(-1, 3) for p in sc.glb_primitives])
        want = np.array(json.loads(world_json.read_text()), np.float64).reshape(-1, 3)
        want = np.stack([want[:, 0], want[:, 2], -want[:, 1]], 1)
        worst = max(np.min(np.linalg.norm(pts - q, axis=1)) for q in want)
        self.assertLess(worst, 1e-4)

    def test_edit_with_unknown_selection_writes_nothing(self):
        out = self.tmp / "nicht_da.skp"
        ops = [{"op": "delete", "select": {"name": "gibt_es_nicht*"}}]
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, _ = run_cli("edit", str(S2017), "-o", str(out), "--ops", json.dumps(ops), "-q")
        self.assertEqual(code, 1)
        self.assertIn("Keine Objekte passen", err.getvalue())
        self.assertFalse(out.exists())

    def test_round_trip_with_edit(self):
        blend = self.tmp / "stuhl.blend"
        run_cli("convert", str(S2017), "-o", str(blend))
        back = self.tmp / "zurueck.skp"
        code, text = run_cli("convert", str(blend), "-o", str(back))
        self.assertEqual(code, 0, text)
        faces, m = faces_of(back)
        self.assertEqual(faces, 86)  # wie im Original: Komponenten bleiben Komponenten
        self.assertEqual(core.placed_face_count(m), 128)
        leg = [d for d in m.definitions.values() if d.name == "Leg_Chair"]
        self.assertEqual(len(leg), 1)  # eine Definition mit mehreren Platzierungen
        self.assertEqual(sorted(mt.name for mt in m.materials), ["Brass", "Charcoal Linen", "Oak", "Walnut"])

    def test_other_formats_via_blender(self):
        for ext in (".fbx", ".usdz", ".png"):
            out = self.tmp / f"g{ext}"
            code, text = run_cli("convert", str(S2017), "-o", str(out))
            self.assertEqual(code, 0, text)
            self.assertGreater(out.stat().st_size, 1000, ext)
        fbx_skp = self.tmp / "aus_fbx.skp"
        code, text = run_cli("convert", str(self.tmp / "g.fbx"), "-o", str(fbx_skp))
        self.assertEqual(code, 0, text)
        self.assertEqual(core.placed_face_count(faces_of(fbx_skp)[1]), 128)

    def test_open_creates_blend_and_launches(self):
        src = self.tmp / "s.skp"
        shutil.copy(S2017, src)
        with mock.patch.object(cli, "launch_gui") as launch:
            code, text = run_cli("open", str(src))
        self.assertEqual(code, 0, text)
        self.assertTrue((self.tmp / "s.blend").exists())
        launch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
