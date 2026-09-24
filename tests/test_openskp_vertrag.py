"""Vertragstests fuer die OpenSKP-Interna, auf die skptool sich stuetzt.

skptool ist auf openskp 1.3.0 gepinnt und nutzt interne Funktionen, Attribute und
Fehlermeldungen. Aendert eine neue OpenSKP-Version davon etwas, soll das hier laut scheitern,
statt dass Texturen still schief liegen oder Farben fehlen. Kein Blender noetig.

Start: .venv\\Scripts\\python -m unittest tests.test_openskp_vertrag -v
"""
import collections
import dataclasses
import importlib
import importlib.util
import inspect
import json
import math
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from openskp import SkpFile, _core, _face_groups, edit, instanced_scene, scene
from openskp.create import ComponentDefinitionBuilder, SkpBuilder, SkpWriteError

from skptool import core

ROOT = Path(__file__).resolve().parents[1]
S2017 = ROOT / "samples" / "stuhl_tisch_2017.skp"
CREATE = importlib.import_module("openskp.create")  # das Modul, nicht die Funktion create()


def _upstream_create():
    """Unveraenderte Kopie von openskp/create.py (skptools Ersetzungen fehlen darin)."""
    spec = importlib.util.spec_from_file_location("openskp._create_unveraendert", CREATE.__file__)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


UPSTREAM = _upstream_create()


def _params(func):
    return list(inspect.signature(func).parameters)


def persistente_ids(path):
    """Alle persistenten IDs einer Datei im 2017-Format: ([(Lesefunktion, ID), ...] ohne ID 0,
    Zaehlerstand im Dateikopf). ID 0 tragen strukturelle Objekte (Schleifen, Kantenseiten, leere
    Attributbehaelter, Schrift), sie zaehlen nicht mit.

    Seit OpenSKP 1.3.0 vergibt der Writer die IDs aus EINEM Zaehler ueber alle Abschnitte
    (Materialien, Ebenen, Definitionen, Modell) und schreibt den Stand an _PID_COUNTER_POS.
    core.write_text/write_dimension holen ihre ID ueber _ArchiveWriter._preamble() aus demselben
    Zaehler. Gelesen wird ueber openskp.legacy._preamble; derselbe Datensatz kann dabei mehrfach
    gelesen werden (Ebenen, Materialien), gezaehlt wird er je Position einmal."""
    from openskp import legacy

    found, seen = [], set()
    original = legacy._preamble

    def spy(ar, r):
        start = (id(r.data), r.pos)
        pre = original(ar, r)
        if start not in seen:
            seen.add(start)
            if pre["pid"]:
                found.append((inspect.currentframe().f_back.f_code.co_name, pre["pid"]))
        return pre

    with mock.patch.object(legacy, "_preamble", spy):
        SkpFile.open(str(path)).parse()
    counter = int.from_bytes(Path(path).read_bytes()[CREATE._PID_COUNTER_POS:CREATE._PID_COUNTER_POS + 4],
                             "little")
    return found, counter


def _png(path, rgb):
    from PIL import Image
    Image.new("RGB", (8, 8), rgb).save(path)
    return str(path)


# ---------------------------------------------------------------- Testdaten: Binaerdump wie aus Blender

ROT = math.radians(30)


def _rotated_square(cx, cy, size):
    """Quadrat in der XY-Ebene, um 30 Grad gedreht: die erste Kante liegt nicht auf einer Achse,
    eine Basis aus der ersten Kante (OpenSKP 1.2.0) und die des Lesers (aus der Normale)
    unterscheiden sich."""
    c, s = math.cos(ROT), math.sin(ROT)
    return [(cx + size * (x * c - y * s), cy + size * (x * s + y * c), 0.0)
            for x, y in ((0, 0), (1, 0), (1, 1), (0, 1))]


def _affine_uv(p):
    return (0.7 * p[0] + 0.2 * p[1] + 0.1, -0.3 * p[0] + 0.9 * p[1] + 0.25 * p[2] + 0.05)


# Flaechen in Metern: (Punkte, Materialindex, UV je Ecke oder None)
AFFINE_FACES = [
    (_rotated_square(0.0, 0.0, 1.0), 0, "affin"),
    # schraege Flaeche, erste Kante schraeg zur Falllinie
    ([(3.0, 0.0, 0.0), (4.0, 0.3, 0.5), (3.6, 1.3, 1.1), (2.6, 1.0, 0.6)], 0, "affin"),
]
PERSPECTIVE_FACE = (_rotated_square(6.0, 0.0, 1.0), 1, [(0, 0), (1, 0), (0.8, 1), (0.2, 1)])
# Unterseite (Normale -Z): OpenSKP 1.3.0 legt die Basis dafuer auf (-X, +Y) statt (X, -Y)
DOWN_FACE = (list(reversed(_rotated_square(12.0, 0.0, 1.0))), 0, "affin")
DOWN_PERSPECTIVE_FACE = (list(reversed(_rotated_square(15.0, 0.0, 1.0))), 1,
                         list(reversed(PERSPECTIVE_FACE[2])))
NON_PLANAR_FACE = ([(9.0, 0.0, 0.0), (10.0, 0.0, 0.0), (10.0, 1.0, 0.3), (9.0, 1.0, 0.0)], -1, None)


def write_dump(tmp, faces, materials, hard_first_edge=True):
    """Kleiner skptool-Binaerdump (Format wie bridge.py, Version 4): eine Gruppe, alle Flaechen."""
    verts, lt, lv, pm, uv = [], [], [], [], []
    for pts, mat, uvs in faces:
        if uvs == "affin":
            uvs = [_affine_uv(p) for p in pts]
        lt.append(len(pts))
        pm.append(mat)
        for p in pts:
            lv.append(len(verts))
            verts.append(p)
        if mat >= 0 and materials[mat].get("image"):
            uv += list(uvs)
    hard = [(0, 1)] if hard_first_edge else []
    bin_path = Path(tmp) / "dump.bin"
    with open(bin_path, "wb") as fh:
        for arr, dt in ((verts, np.float32), (lt, np.int32), (lv, np.int32), (pm, np.int32),
                        (uv, np.float32), (hard, np.int32), ([-1] * len(lt), np.int32)):
            fh.write(np.asarray(arr, dt).tobytes())
    header = {"format": core.DUMP_FORMAT, "version": 4, "unit": "m", "bin": bin_path.name,
              "materials": materials,
              "definitions": [{"name": "Flaechen", "offset": 0, "nverts": len(verts), "npolys": len(lt),
                               "nloops": len(lv), "nuv": len(uv), "nhard": len(hard), "nbuv": 0, "uses": 1}],
              "instances": [{"name": "Flaechen", "definition": 0, "parent": -1, "layer": "",
                             "hidden": False, "material": -1, "skp_definition": "",
                             "matrix": [float(v) for v in np.eye(4).reshape(-1)]}],
              "layers": []}
    header_path = Path(tmp) / "dump.json"
    header_path.write_text(json.dumps(header), encoding="utf-8")
    return header_path


def uv_errors(skp_path, faces):
    """Groesste Abweichung der gelesenen UVs von den geschriebenen und Zahl der geprueften Ecken.

    Gelesen wird mit OpenSKPs Leser, der nachweislich wie SketchUp rechnet (siehe core.py)."""
    want = {}
    for pts, _, uvs in faces:
        if uvs is None:
            continue
        if uvs == "affin":
            uvs = [_affine_uv(p) for p in pts]
        for p, t in zip(pts, uvs):
            want[tuple(round(c, 3) for c in p)] = t
    sc = core.build_scene(core.open_skp(skp_path))
    checked, worst = 0, 0.0
    for prim in sc.glb_primitives:
        pbr = sc.gltf_materials[prim.material_index]["pbrMetallicRoughness"]
        if "baseColorTexture" not in pbr or not len(prim.uvs):
            continue
        pos = [prim.positions[i:i + 3] for i in range(0, len(prim.positions), 3)]
        uvs = [prim.uvs[i:i + 2] for i in range(0, len(prim.uvs), 2)]
        for (x, y, z), (u, v) in zip(pos, uvs):  # Szene ist Y-oben: (x, y, z) = (x, -z, y)
            key = (round(x, 3), round(-z, 3), round(y, 3))
            if key in want:
                checked += 1
                worst = max(worst, abs(u - want[key][0]), abs(v - want[key][1]))
    return worst, checked


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="skptool_vertrag_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def materials(self):
        return [{"name": "Rot", "rgba": [255, 255, 255, 255], "alpha": 1.0,
                 "image": _png(self.tmp / "rot.png", (200, 50, 50))},
                {"name": "Blau", "rgba": [255, 255, 255, 255], "alpha": 1.0,
                 "image": _png(self.tmp / "blau.png", (30, 60, 210))}]


# ---------------------------------------------------------------- a) Namen und Signaturen

class TestNamenUndSignaturen(unittest.TestCase):
    def test_create_ist_funktion_und_modul_getrennt(self):
        from openskp import create
        self.assertTrue(callable(create))
        self.assertIsInstance(create(), SkpBuilder)
        self.assertIsInstance(CREATE, types.ModuleType)

    def test_ersetzte_funktionen_haben_passende_signaturen(self):
        # skptools Ersatz muss so aufrufbar sein wie das Original, das write_face aufruft
        self.assertEqual(_params(UPSTREAM._uv_matrix_for_face), ["points", "pairs", "normal"])
        self.assertEqual(_params(core._uv_matrix_for_face), _params(UPSTREAM._uv_matrix_for_face))
        self.assertEqual(_params(CREATE._solve_uv_matrix), ["pairs", "basis"])
        self.assertEqual(_params(_face_groups.face_uv_basis), ["n"])

    def test_write_face_ruft_die_ersetzten_namen_als_modulglobale_auf(self):
        """Nur dann wirkt das Ersetzen im Modul. Ohne diese Namen im Code von write_face (bzw. in
        _uv_matrix_for_face) liefe die Ersetzung ins Leere."""
        self.assertIn("_uv_matrix_for_face", CREATE._ArchiveWriter.write_face.__code__.co_names)
        self.assertIn("face_uv_basis", UPSTREAM._uv_matrix_for_face.__code__.co_names)
        self.assertIn("_solve_uv_matrix", UPSTREAM._uv_matrix_for_face.__code__.co_names)

    def test_ersetzungen_sind_aktiv(self):
        self.assertIs(CREATE._uv_matrix_for_face, core._uv_matrix_for_face)
        self.assertIs(core._sketchup_face_uv_basis, _face_groups.face_uv_basis)

    def test_writer_und_leser_teilen_die_basis(self):
        """Seit 1.3.0 rechnet der Writer mit der Basis des Lesers, deshalb ersetzt skptool sie nicht
        mehr. Kommt die Basis aus der ersten Kante zurueck, muss der Ersatz wieder her."""
        self.assertIs(CREATE.face_uv_basis, _face_groups.face_uv_basis)
        self.assertIs(UPSTREAM.face_uv_basis, _face_groups.face_uv_basis)
        self.assertFalse(hasattr(CREATE, "_face_uv_basis"), "Basis aus der ersten Kante ist zurueck")

    def test_leser_basis_haengt_nur_an_der_normale(self):
        xr, yr = _face_groups.face_uv_basis((0.0, 0.0, 1.0))
        np.testing.assert_allclose(xr, (1, 0, 0), atol=1e-12)
        np.testing.assert_allclose(yr, (0, 1, 0), atol=1e-12)
        # nach unten: um 180 Grad gedreht (-X, +Y) wie SketchUp (OpenSKP 1.3.0, vorher (X, -Y))
        xr, yr = _face_groups.face_uv_basis((0.0, 0.0, -1.0))
        np.testing.assert_allclose(xr, (-1, 0, 0), atol=1e-12)
        np.testing.assert_allclose(yr, (0, 1, 0), atol=1e-12)
        # fast waagerecht (Neigung 1e-4): SketchUp nimmt noch die Weltachsen (Toleranz 1e-3)
        n = (1e-4, 0.0, math.sqrt(1 - 1e-8))
        xr, yr = _face_groups.face_uv_basis(n)
        np.testing.assert_allclose(xr, (1, 0, 0), atol=1e-12)
        # schraeg: xr = normiert(Z x n), unabhaengig von den Eckpunkten
        n = np.array([0.3, -0.4, 0.8]) / np.linalg.norm([0.3, -0.4, 0.8])
        xr, yr = _face_groups.face_uv_basis(tuple(n))
        np.testing.assert_allclose(xr, np.array([0.4, 0.3, 0.0]) / 0.5, atol=1e-12)
        np.testing.assert_allclose(yr, np.cross(n, xr), atol=1e-12)

    def test_edit_interna(self):
        self.assertEqual(_params(edit._definition_order), ["model"])
        self.assertEqual(_params(edit._definition_has_content), ["defn", "def_builders"])
        # core._replay_body ersetzt edit._replay_body und ruft dessen Bausteine auf
        self.assertEqual(_params(edit._edge_map), ["defn"])
        self.assertEqual(_params(edit._replay_face), ["target", "face", "defn", "edges", "model", "material_slots",
                                                     "warnings", "context"])
        # core._replay_instance ersetzt edit._replay_instance (alle Attribut-Woerterbuecher)
        self.assertEqual(_params(edit._material_slot), ["material_id", "model", "slots"])
        self.assertIn("add_face", edit._replay_face.__code__.co_names)  # core._SoftDiagonals faengt das ab

    def test_write_edge_chain_signatur(self):
        self.assertEqual(_params(CREATE._ArchiveWriter._write_edge_chain)[:6],
                         ["self", "points", "vertex_slots", "edge_registry", "closed", "hidden_edges"])

    def test_oeffentliche_writer_aufrufe_mit_den_genutzten_schluesselwoertern(self):
        need = {
            SkpBuilder.add_texture_material: {"name", "image_path", "applied_width", "applied_height", "opacity"},
            SkpBuilder.add_material: {"name", "rgba", "opacity"},
            SkpBuilder.add_layer: {"name", "color", "hidden"},
            SkpBuilder.add_component_definition: {"name", "always_faces_camera", "shadows_face_sun"},
            SkpBuilder.add_group: {"name", "translation", "matrix3x3", "material", "layer", "hidden"},
            SkpBuilder.add_instance: {"translation", "matrix3x3", "material", "layer", "hidden", "name"},
            ComponentDefinitionBuilder.add_instance: {"translation", "matrix3x3", "material", "layer",
                                                      "hidden", "name"},
            ComponentDefinitionBuilder.add_group_instance: {"translation", "matrix3x3", "material", "layer",
                                                            "hidden", "name"},
        }
        face_kw = {"material", "back_material", "front_uv", "back_uv", "soft_edges", "smooth_edges",
                   "holes", "auto_triangulate"}
        need[SkpBuilder.add_face] = face_kw
        need[ComponentDefinitionBuilder.add_face] = face_kw
        for func, names in need.items():
            with self.subTest(func=func.__qualname__):
                self.assertLessEqual(names, set(_params(func)))
        self.assertTrue(callable(SkpBuilder.to_bytes))

    def test_lese_interna(self):
        self.assertEqual(_params(_core.multiply_matrices), ["parent", "child"])
        ident = [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1.0]
        moved = [1, 0, 0, 0, 1, 0, 0, 0, 1, 5, 6, 7, 1.0]
        self.assertEqual([float(v) for v in _core.multiply_matrices(ident, moved)[9:12]], [5.0, 6.0, 7.0])
        # core.build_scene/build_instanced_scene uebergeben beide Schluesselwoerter
        self.assertEqual(_params(instanced_scene.build_instanced_scene), ["parsed", "name_override_keys"])
        self.assertEqual(_params(scene.build_scene), ["parsed", "name_override_keys", "include_curve_sets"])
        self.assertIsNone(SkpFile.open(str(S2017))._parsed)  # core.build_scene prueft "is None"
        # Triangulierung: der Szenenaufbau holt sie zur Laufzeit ueber das Modul, nur dann greift
        # core._triangulate_face_3d
        self.assertIn("triangulate_face_3d", _face_groups.build_local_face_groups.__code__.co_names
                      + tuple(n for c in _face_groups.build_local_face_groups.__code__.co_consts
                              if isinstance(c, types.CodeType) for n in c.co_names))
        self.assertIs(_core.triangulate_face_3d, core._triangulate_face_3d)

    def test_felder_der_szenenobjekte(self):
        """Felder, die gltf_writer.py und core.py lesen."""
        want = {
            instanced_scene.InstancedScene: {"scene_hierarchy", "mesh_resources", "gltf_materials", "textures"},
            instanced_scene.InstancedMeshResource: {"id", "definition_id", "definition_name", "primitives"},
            instanced_scene.LocalPrimitive: {"positions", "normals", "uvs", "indices", "material_index"},
            instanced_scene.InstancedNode: {"name", "name_is_generated", "definition_name", "layer", "matrix",
                                            "position_mm", "mesh_resource_id", "children"},
            scene.SceneTexture: {"data", "filename", "mime_type"},
            scene.GlbPrimitive: {"positions", "indices", "uvs", "material_index"},
            scene.Scene: {"glb_primitives", "gltf_materials"},
        }
        for cls, fields in want.items():
            with self.subTest(cls=cls.__name__):
                self.assertLessEqual(fields, {f.name for f in dataclasses.fields(cls)})

    def test_geparstes_modell_hat_die_genutzten_felder(self):
        skp = core.open_skp(S2017)
        m = core.model_of(skp)
        self.assertIsNotNone(skp._parsed)
        self.assertTrue(dataclasses.is_dataclass(m))  # rewrite_legacy nutzt dataclasses.replace
        for name in ("root", "definitions", "materials", "materials_by_id", "layers", "pages", "version"):
            self.assertTrue(hasattr(m, name), name)
        for mt in m.materials:
            # colorize_type: gltf_writer._colorize (einfaerben.py); texture.width/height: _tile_size
            for name in ("name", "color", "transparency", "id", "texture", "colorized", "colorize_type"):
                self.assertTrue(hasattr(mt, name), name)
            if mt.texture is not None:
                for name in ("data", "filename", "width", "height"):
                    self.assertTrue(hasattr(mt.texture, name), name)
        for layer in m.layers:
            for name in ("name", "hidden", "color_r", "color_g", "color_b"):
                self.assertTrue(hasattr(layer, name), name)
        defs = [m.root, *m.definitions.values()]
        for name in ("faces", "edges", "vertices", "instances", "name", "is_image", "always_faces_camera",
                     "shadows_face_sun"):
            self.assertTrue(hasattr(m.root, name), name)
        insts = [i for d in defs for i in d.instances]
        self.assertTrue(insts)
        for name in ("ref_idx", "matrix", "name", "layer", "material_id"):
            self.assertTrue(hasattr(insts[0], name), name)
        edges = [e for d in defs for e in d.edges.values()]
        self.assertTrue(edges)
        for name in ("soft", "hidden", "v1_id", "v2_id"):
            self.assertTrue(hasattr(edges[0], name), name)
        isc = instanced_scene.build_instanced_scene(skp._parsed)
        self.assertTrue(isc.mesh_resources)
        self.assertIsNotNone(isc.scene_hierarchy)


# ---------------------------------------------------------------- b) Ersetzungen greifen wirklich

class TestErsetzungenGreifen(Base):
    def test_texturmatrix_und_basis_werden_beim_schreiben_benutzt(self):
        faces = [*AFFINE_FACES, PERSPECTIVE_FACE]
        header = write_dump(self.tmp, faces, self.materials(), hard_first_edge=False)
        out = self.tmp / "t.skp"
        with mock.patch.object(CREATE, "_uv_matrix_for_face", wraps=CREATE._uv_matrix_for_face) as matrix, \
                mock.patch.object(core, "_sketchup_face_uv_basis", wraps=core._sketchup_face_uv_basis) as basis:
            stats = core.write_skp_from_bin(header, out)
        self.assertEqual(matrix.call_count, 3, "write_face ruft _uv_matrix_for_face nicht mehr je Flaeche auf")
        self.assertGreaterEqual(basis.call_count, 3, "skptools Texturmatrix wird nicht benutzt")
        self.assertEqual(stats.get("perspective"), 1)
        self.assertNotIn("uv_dropped", stats, "verzerrte Textur ging verloren")
        self.assertEqual(stats["faces"], 3)
        worst, checked = uv_errors(out, faces)
        self.assertGreaterEqual(checked, 12)
        self.assertLess(worst, 1e-4)

    def test_openskps_eigene_texturmatrix_trifft_gedrehte_schraege_und_untere_flaechen(self):
        """Grund fuer den Wegfall des Basis-Ersatzes: OpenSKPs unveraenderte _uv_matrix_for_face
        (1.3.0) legt gedrehte, schraege und nach unten zeigende Flaechen so, wie der Leser (und
        SketchUp) sie liest. Faellt dieser Test, braucht der affine Fall wieder einen Ersatz."""
        faces = [*AFFINE_FACES, DOWN_FACE]
        header = write_dump(self.tmp, faces, self.materials(), hard_first_edge=False)
        out = self.tmp / "t.skp"
        with mock.patch.object(CREATE, "_uv_matrix_for_face", UPSTREAM._uv_matrix_for_face):
            core.write_skp_from_bin(header, out)
        worst, checked = uv_errors(out, faces)
        self.assertGreaterEqual(checked, 12)
        self.assertLess(worst, 1e-4)

    def test_skptools_texturmatrix_trifft_untere_flaechen(self):
        faces = [DOWN_FACE, DOWN_PERSPECTIVE_FACE]
        header = write_dump(self.tmp, faces, self.materials(), hard_first_edge=False)
        out = self.tmp / "t.skp"
        stats = core.write_skp_from_bin(header, out)
        self.assertEqual(stats.get("perspective"), 1)
        self.assertNotIn("uv_dropped", stats)
        worst, checked = uv_errors(out, faces)
        self.assertGreaterEqual(checked, 8)
        self.assertLess(worst, 1e-4)

    def test_gegenprobe_falsche_basis_waere_sichtbar(self):
        """Gegenprobe fuer die Tests oben: rechnete der Writer in einer anderen Basis als der Leser
        (wie OpenSKP 1.2.0 mit der ersten Kante), laege die gedrehte Flaechentextur schief."""
        faces = [AFFINE_FACES[0]]
        header = write_dump(self.tmp, faces, self.materials(), hard_first_edge=False)
        out = self.tmp / "t.skp"
        c, s = math.cos(ROT), math.sin(ROT)
        first_edge = lambda n: ((c, s, 0.0), (-s, c, 0.0))  # noqa: E731
        with mock.patch.object(core, "_sketchup_face_uv_basis", first_edge):
            core.write_skp_from_bin(header, out)
        worst, checked = uv_errors(out, faces)
        self.assertGreaterEqual(checked, 4)
        self.assertGreater(worst, 0.05)

    def test_texturpunkte_werden_mit_der_kachelgroesse_skaliert(self):
        """Seit 1.3.0 skaliert add_face die Texturpunkte (in Kacheln) mit der Kachelgroesse des
        Materials, bevor skptools Texturmatrix sie bekommt. Affin und perspektivisch muessen die
        gelesenen UVs trotz 20 x 10 Zoll je Kachel die geschriebenen sein."""
        self.assertIn("_scale_pins", ComponentDefinitionBuilder.add_face.__code__.co_names)
        self.assertIn("_scale_pins", SkpBuilder.add_face.__code__.co_names)
        b = SkpBuilder()
        mat = b.add_texture_material("Holz", _png(self.tmp / "holz.png", (150, 90, 40)),
                                     applied_width=20.0, applied_height=10.0)
        sq = [(x * 40.0, y * 40.0, z) for x, y, z in _rotated_square(0, 0, 1)]
        want = [(0.0, 0.0), (1.0, 0.0), (0.8, 1.0), (0.2, 1.0)]
        affine = [(x + 100.0, y, z) for x, y, z in sq]
        with mock.patch.object(core, "_sketchup_face_uv_basis", wraps=core._sketchup_face_uv_basis) as basis:
            b.add_face(sq, material=mat, front_uv=list(zip(sq, want)))
            b.add_face(affine, material=mat, front_uv=[(p, _affine_uv(p)) for p in affine[:3]])
        self.assertEqual(basis.call_count, 2)
        out = self.tmp / "k.skp"
        core.save_atomic(b, out)
        sizes = {mt.name: (mt.texture.width, mt.texture.height)
                 for mt in SkpFile.open(str(out)).parse().materials if mt.texture}
        self.assertEqual(sizes, {"Holz": (20.0, 10.0)})
        inch = [(tuple(c * core.INCH for c in p), t) for p, t in zip(sq, want)]
        inch += [(tuple(c * core.INCH for c in p), _affine_uv(p)) for p in affine]
        worst, checked = uv_errors(out, [([p for p, _ in inch], 0, [t for _, t in inch])])
        self.assertEqual(checked, 8)
        self.assertLess(worst, 1e-4)

    def test_durchschnittsfarbe_ersetzt_den_platzhalter(self):
        b = SkpBuilder()
        self.assertIsInstance(b._material_writer.buf, bytearray)
        before = b._material_writer.buf.count(core._AVG_PLACEHOLDER)
        b.add_texture_material("Rot", _png(self.tmp / "rot.png", (200, 50, 50)))
        self.assertEqual(b._material_writer.buf.count(core._AVG_PLACEHOLDER), before + 1,
                         "OpenSKP schreibt den Weiss-Platzhalter nicht mehr, Durchschnittsfarbe fehlt")
        core.set_texture_average_color(b, (200, 50, 50))
        self.assertEqual(b._material_writer.buf.count(core._AVG_PLACEHOLDER), before)
        self.assertIn(bytes([200, 50, 50, 254, 0, 200, 50, 50, 254]), b._material_writer.buf)

    def test_durchschnittsfarbe_kommt_in_der_datei_an(self):
        faces = [*AFFINE_FACES, PERSPECTIVE_FACE]
        header = write_dump(self.tmp, faces, self.materials(), hard_first_edge=False)
        out = self.tmp / "t.skp"
        with mock.patch.object(core, "set_texture_average_color", wraps=core.set_texture_average_color) as spy:
            core.write_skp_from_bin(header, out)
        self.assertEqual(spy.call_count, 2)
        colors = {mt.name: tuple(mt.color[:3]) for mt in SkpFile.open(str(out)).parse().materials if mt.texture}
        self.assertEqual(colors, {"Rot": (200, 50, 50), "Blau": (30, 60, 210)})
        for rgb in colors.values():
            self.assertNotEqual(rgb, (255, 255, 255))

    def test_harte_kanten_ueber_write_edge_chain(self):
        faces = [AFFINE_FACES[0], AFFINE_FACES[1]]
        header = write_dump(self.tmp, faces, self.materials(), hard_first_edge=True)
        out = self.tmp / "t.skp"
        stats = core.write_skp_from_bin(header, out)
        self.assertEqual(stats["hard_edges"], 1)
        m = SkpFile.open(str(out)).parse()
        edges = [e for d in [m.root, *m.definitions.values()] for e in d.edges.values()]
        self.assertEqual(len(edges), 8)
        self.assertEqual(sum(1 for e in edges if not e.soft and not e.hidden), 1)

    def test_write_edge_chain_liefert_drei_werte(self):
        b = SkpBuilder()
        with b.add_group(name="g") as g:
            res = g._skp._definition_writer._write_edge_chain([(0, 0, 0), (1, 0, 0)], g._vertex_slots,
                                                              g._edge_registry, False)
            self.assertEqual(len(res), 3)
            slots, senses, new = res
            self.assertEqual((len(slots), len(senses), new), (1, 1, 1))
            g._new_entity_count += new
            g.add_face([(0, 0, 0), (1, 0, 0), (1, 1, 0)])
        m_path = self.tmp / "k.skp"
        core.save_atomic(b, m_path)
        self.assertEqual(len(SkpFile.open(str(m_path)).parse().definitions), 1)

    def test_lose_kanten_im_modell_ueber_geometry_writer(self):
        """core._predeclare_edges schreibt ins Modell selbst ueber _geometry_writer und zaehlt
        _face_count hoch, sonst lehnt to_bytes ein Modell nur aus losen Kanten ab."""
        b = SkpBuilder()
        b._ensure_geometry_writer()
        self.assertEqual(b._face_count, 0)
        _, _, new = b._geometry_writer._write_edge_chain([(0, 0, 0), (1, 0, 0)], b._vertex_slots,
                                                         b._edge_registry, False, False, True, True)
        b._new_entity_count += new
        b._face_count += 1
        m_path = self.tmp / "lose.skp"
        core.save_atomic(b, m_path)
        edges = list(SkpFile.open(str(m_path)).parse().root.edges.values())
        self.assertEqual([(e.soft, e.smooth, e.hidden) for e in edges], [(True, True, False)])

    def test_materialname_haengt_an_der_farbe(self):
        """core ersetzt _face_groups.resolve_color und instanced_scene.build_local_face_groups:
        die Farbe traegt den Materialnamen, die Gruppen sind wie bei OpenSKP nach
        (Farbe, doppelseitig, Textur, Deckkraft) geschluesselt und build_instanced_scene packt den
        Schluessel in genau diese vier Teile aus. Aendert OpenSKP das, soll es hier scheitern."""
        self.assertIs(_face_groups.resolve_color, core._resolve_color_named)
        self.assertIs(_face_groups._add_face_side, core._add_face_side_sided)
        self.assertIs(instanced_scene.build_local_face_groups, core._build_groups_recording)
        self.assertEqual(_params(core._add_face_side_openskp)[4:7], ["color", "double_sided", "reverse"])
        def codes(code):  # Funktion samt verschachtelter Funktionen
            yield code
            for c in code.co_consts:
                if inspect.iscode(c):
                    yield from codes(c)

        def names(func):
            return {n for c in codes(func.__code__) for n in c.co_names}

        self.assertIn("resolve_color", names(_face_groups.build_local_face_groups))
        self.assertIn("_add_face_side", names(_face_groups.build_local_face_groups))
        self.assertIn("build_local_face_groups", names(instanced_scene.build_instanced_scene))
        self.assertIn("build_local_face_groups", names(scene.build_scene))
        # get_material_index in build_instanced_scene: Schluessel (color, double_sided, texture_index, ...)
        getter = next(c for c in codes(instanced_scene.build_instanced_scene.__code__)
                      if c.co_name == "get_material_index")
        self.assertEqual(getter.co_varnames[:3], ("color", "double_sided", "texture_index"))
        weiss = {"name": "A", "color": {"r": 255, "g": 255, "b": 255}}
        auch_weiss = {"name": "B", "color": {"r": 255, "g": 255, "b": 255}}
        a, b = _face_groups.resolve_color(weiss), _face_groups.resolve_color(auch_weiss)
        self.assertEqual(tuple(a), (255, 255, 255))
        self.assertEqual(core._resolve_color_openskp(weiss), (255, 255, 255))
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, (255, 255, 255))  # unbemalt (Tupel ohne Namen) ist ein eigenes Material
        self.assertEqual(a, _face_groups.resolve_color(dict(weiss)))
        self.assertEqual(len({(a, True, None, 1.0), (b, True, None, 1.0)}), 2)
        r, g, bl = a  # so packt get_material_index aus
        self.assertEqual((r, g, bl, a.material), (255, 255, 255, "A"))
        self.assertNotEqual(a, core._named_color(a, "A", back=True))  # Rueckseite: eigene Gruppe

    def test_vorderseiten_vor_rueckseiten(self):
        """Flaeche vorne A, hinten B (gleiche Farbe): zwei Primitive, die Vorderseite zuerst, auch
        wenn die Rueckseitengruppe zuerst entsteht (erste Flaeche nur hinten bemalt)."""
        b = SkpBuilder()
        eins = b.add_material("A", [255, 255, 255])
        zwei = b.add_material("B", [255, 255, 255])
        with b.add_component_definition("Teil") as teil:
            teil.add_face([(0, 0, 5), (10, 0, 5), (10, 10, 5), (0, 10, 5)], back_material=zwei)
            teil.add_face([(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0)], material=eins, back_material=zwei)
        b.add_instance(teil, translation=(0.0, 0.0, 0.0))
        path = self.tmp / "seiten.skp"
        core.save_atomic(b, path)
        isc = core.build_instanced_scene(core.open_skp(path))
        (res,) = isc.mesh_resources
        names = [isc.skp_material_names[p.material_index] for p in res.primitives]
        # vorne: A, unbemalt; hinten: B (beide Flaechen in einer Gruppe)
        self.assertEqual(sorted(names[:2], key=str), ["A", None])
        self.assertEqual(names[2:], ["B"])
        self.assertEqual(len(res.primitives[2].indices), 12)
        # gleiche glTF-Materialien sind zusammengefasst
        dump = [json.dumps(m, sort_keys=True) + str(n) for m, n in zip(isc.gltf_materials, isc.skp_material_names)]
        self.assertEqual(len(dump), len(set(dump)))

    def test_szene_trennt_materialien_gleicher_farbe(self):
        b = SkpBuilder()
        eins = b.add_material("Eins", [255, 255, 255])
        zwei = b.add_material("Zwei", [255, 255, 255])
        with b.add_component_definition("Teil") as teil:
            teil.add_face([(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0)], material=eins)
            teil.add_face([(0, 0, 5), (10, 0, 5), (10, 10, 5), (0, 10, 5)], material=zwei)
        b.add_instance(teil, translation=(0.0, 0.0, 0.0))
        path = self.tmp / "zwei_weiss.skp"
        core.save_atomic(b, path)
        skp = core.open_skp(path)
        roh = instanced_scene.build_instanced_scene(skp._parsed)
        self.assertFalse(hasattr(roh, "skp_material_names"))
        isc = core.build_instanced_scene(skp)
        # je Material ein glTF-Material; dazu die unbemalten Rueckseiten (Name None)
        self.assertEqual(sorted(n for n in isc.skp_material_names if n), ["Eins", "Zwei"])
        used = {p.material_index for r in isc.mesh_resources for p in r.primitives}
        self.assertEqual({isc.skp_material_names[i] for i in used}, {"Eins", "Zwei", None})
        # ohne die Ersetzung fasst OpenSKP beide zu einem Material zusammen (Gegenprobe)
        with mock.patch.object(_face_groups, "resolve_color", core._resolve_color_openskp):
            pur = instanced_scene.build_instanced_scene(skp._parsed)
        weiss = [m for m in pur.gltf_materials if m["pbrMetallicRoughness"]["baseColorFactor"][:3] == [1, 1, 1]]
        self.assertEqual(len(weiss), 1)

    def test_colorize_im_texturdatensatz(self):
        """core.set_texture_colorize setzt im Texturdatensatz von OpenSKPs Writer die Zielfarbe mit
        Alpha 255 und das Kennzeichen im zweiten u32 danach, so wie openskp.legacy es liest."""
        b = SkpBuilder()
        mat = b.add_texture_material("Getoent", _png(self.tmp / "g.png", (120, 120, 120)))
        buf = b._material_writer.buf
        i = buf.rfind(core._AVG_PLACEHOLDER)
        self.assertGreaterEqual(i, 0)
        self.assertEqual(bytes(buf[i + 9:i + 9 + len(core._COLORIZE_TAIL)]), core._COLORIZE_TAIL,
                         "Aufbau des Texturdatensatzes hinter der Durchschnittsfarbe hat sich geaendert")
        self.assertTrue(core.set_texture_colorize(b, (200, 40, 10)))
        self.assertFalse(core.set_texture_colorize(b, (1, 2, 3)))  # kein zweites Mal
        with b.add_component_definition("Teil") as teil:
            teil.add_face([(0, 0, 0), (10, 0, 0), (10, 10, 0)], material=mat)
        b.add_instance(teil, translation=(0.0, 0.0, 0.0))
        path = self.tmp / "getoent.skp"
        core.save_atomic(b, path)
        (mt,) = [m for m in SkpFile.open(str(path)).parse().materials if m.texture]
        self.assertTrue(mt.colorized)
        self.assertEqual(tuple(mt.color[:3]), (200, 40, 10))
        self.assertTrue(mt.texture.data)

    def test_colorize_mit_echter_kachelgroesse(self):
        """rewrite_legacy uebergibt seit OpenSKP 1.3.0 die Kachelgroesse der Quelle. Die beiden f64
        stehen VOR dem Bildpfad und der Durchschnittsfarbe, der Datensatz dahinter bleibt gleich:
        Colorize, Kachelgroesse und Deckkraft muessen zusammen ankommen, ein zweites (normales)
        Texturmaterial davor bleibt unberuehrt."""
        b = SkpBuilder()
        plain = b.add_texture_material("Normal", _png(self.tmp / "n.png", (10, 200, 10)),
                                       applied_width=12.0, applied_height=24.0)
        core.set_texture_average_color(b, (10, 200, 10))
        mat = b.add_texture_material("Getoent", _png(self.tmp / "g.png", (120, 120, 120)),
                                     applied_width=37.5, applied_height=80.25, opacity=0.5)
        buf = b._material_writer.buf
        i = buf.rfind(core._AVG_PLACEHOLDER)
        self.assertEqual(bytes(buf[i + 9:i + 9 + len(core._COLORIZE_TAIL)]), core._COLORIZE_TAIL)
        self.assertEqual(bytes(buf[i + 9 + len(core._COLORIZE_TAIL):]), CREATE._f64(0.5) + b"\x01",
                         "hinter dem Colorize-Block steht nur noch die Deckkraft")
        self.assertTrue(core.set_texture_colorize(b, (200, 40, 10)))
        with b.add_component_definition("Teil") as teil:
            teil.add_face([(0, 0, 0), (10, 0, 0), (10, 10, 0)], material=mat)
            teil.add_face([(0, 0, 5), (10, 0, 5), (10, 10, 5)], material=plain)
        b.add_instance(teil, translation=(0.0, 0.0, 0.0))
        path = self.tmp / "getoent_kachel.skp"
        core.save_atomic(b, path)
        mats = {m.name: m for m in SkpFile.open(str(path)).parse().materials if m.texture}
        g, n = mats["Getoent"], mats["Normal"]
        self.assertTrue(g.colorized)
        self.assertEqual(tuple(g.color[:3]), (200, 40, 10))
        self.assertEqual((g.texture.width, g.texture.height), (37.5, 80.25))
        self.assertAlmostEqual(g.transparency, 0.5)
        self.assertFalse(n.colorized)
        self.assertEqual(tuple(n.color[:3]), (10, 200, 10))
        self.assertEqual((n.texture.width, n.texture.height), (12.0, 24.0))


# ---------------------------------------------------------------- c) Fehlermeldungen

class TestFehlermeldungen(Base):
    def test_nicht_eben_meldet_coplanar(self):
        b = SkpBuilder()
        with self.assertRaises(SkpWriteError) as cm:
            b.add_face(NON_PLANAR_FACE[0])
        self.assertIn("coplanar", str(cm.exception))
        with SkpBuilder().add_group(name="g") as g:
            with self.assertRaises(SkpWriteError) as cm:
                g.add_face(NON_PLANAR_FACE[0])
            g.add_face([(0, 0, 0), (1, 0, 0), (1, 1, 0)])
        self.assertIn("coplanar", str(cm.exception))

    def test_add_face_safe_zerlegt_nicht_ebene_flaechen(self):
        stats = {"triangulated": 0, "skipped": 0}
        b = SkpBuilder()
        with core.tolerant_faces(stats):
            b.add_face(NON_PLANAR_FACE[0])
        self.assertEqual(stats, {"triangulated": 1, "skipped": 0})

    def test_unbrauchbare_texturpunkte_sind_skpwriteerror(self):
        """add_face_safe faengt nur SkpWriteError und schreibt die Flaeche dann ohne Textur."""
        pts = _rotated_square(0, 0, 1)
        same = [(p, (0.5, 0.5)) for p in pts[:3]]
        b = SkpBuilder()
        with self.assertRaises(SkpWriteError):
            b.add_face(pts, front_uv=same)
        stats = {"triangulated": 0, "skipped": 0}
        with core.tolerant_faces(stats):
            b.add_face(pts, front_uv=same)
        self.assertEqual(stats.get("uv_dropped"), 1)
        self.assertEqual(stats["skipped"], 0)

    def test_leeres_modell_meldet_no_geometry(self):
        with self.assertRaises(SkpWriteError) as cm:
            SkpBuilder().to_bytes()
        self.assertIn("no geometry", str(cm.exception))
        target = self.tmp / "leer.skp"
        target.write_bytes(b"vorher")
        with self.assertRaises(ValueError) as cm:
            core.save_atomic(SkpBuilder(), target)
        self.assertIn("Keine Flaechen", str(cm.exception))
        self.assertEqual(target.read_bytes(), b"vorher")


# ---------------------------------------------------------------- d) Texte, Bemassungen, Attribute

class TestAnmerkungenInterna(Base):
    """core.write_text/write_dimension schreiben dieselben Bytes wie add_text/add_dimension, nur
    auch in Definitionen und mit Sichtbarkeit. Dafuer nutzen sie Interna des Writers."""

    QUAD = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0)]

    def test_genutzte_namen_des_writers(self):
        for name in ("_DIM_FONT_PAYLOAD", "_TEXT_DELIM", "_DIM_DRAWBASE", "_f64", "_u32", "Point3d", "Timestamp"):
            self.assertTrue(hasattr(CREATE, name), name)
        w = CREATE._ArchiveWriter
        self.assertEqual(_params(w._drawbase), ["self", "mat", "layer", "hidden", "soft", "smooth"])
        self.assertEqual(_params(w._new_of_known_class), ["self", "class_name", "schema"])
        for name in ("_preamble", "_backref", "_write_str"):
            self.assertTrue(callable(getattr(w, name)), name)
        b = SkpBuilder()
        for name in ("_dim_font_slot", "_new_entity_count", "_face_count", "_definition_writer"):
            self.assertTrue(hasattr(b, name), name)
        self.assertTrue(callable(b._ensure_geometry_writer))
        self.assertEqual(_params(SkpBuilder.add_text), ["self", "text", "point", "leader"])
        self.assertEqual(_params(SkpBuilder.add_dimension), ["self", "p1", "p2", "offset"])
        for func in (SkpBuilder.add_instance, ComponentDefinitionBuilder.add_instance):
            self.assertIn("attribute_dicts", _params(func))

    def test_zeichenbasis_der_anmerkungen_ist_die_standardbasis(self):
        """add_text/add_dimension schreiben die feste Zeichenbasis, write_text/_dimension _drawbase()."""
        w = CREATE._ArchiveWriter(next_slot=1, class_slot={})
        w._drawbase()
        self.assertEqual(bytes(w.buf), CREATE._DIM_DRAWBASE)

    def test_text_und_mass_im_modell_byte_gleich(self):
        a, b = SkpBuilder(), SkpBuilder()
        for x in (a, b):
            x.add_face(self.QUAD)
        a.add_text("Hallo", (1, 2, 3), leader=(4, 5, 6))
        a.add_dimension((0, 0, 0), (10, 0, 0), offset=-7.5)
        a.add_text("Zweiter", (0, 0, 0))
        core.write_text(b, b, "Hallo", (1, 2, 3), (5, 7, 9))
        core.write_dimension(b, b, (0, 0, 0), (10, 0, 0), -7.5)
        core.write_text(b, b, "Zweiter", (0, 0, 0), (15, 15, 15))
        self.assertEqual(a.to_bytes(), b.to_bytes())

    def test_text_in_definition_wird_gelesen(self):
        b = SkpBuilder()
        with b.add_component_definition("Teil") as d:
            d.add_face(self.QUAD)
            core.write_text(d, b, "innen", (1, 1, 0), (2, 3, 4))
            core.write_dimension(d, b, (0, 0, 0), (0, 10, 0), 2.0)
        core.write_text(b, b, "aussen", (9, 9, 9), (9, 9, 9), hidden=True)  # Schrift als Rueckverweis
        out = self.tmp / "t.skp"
        out.write_bytes(b.to_bytes())
        skp = core.open_skp(out)
        m = core.model_of(skp)
        (defn,) = m.definitions.values()
        self.assertEqual([(t.text, t.point, t.label_point) for t in defn.texts],
                         [("innen", (1.0, 1.0, 0.0), (2.0, 3.0, 4.0))])
        self.assertEqual([(t.text, t.hidden) for t in m.root.texts], [("aussen", True)])
        # Rohergebnis: Endpunkte der Bemassungen je Definition (das Modell behaelt nur den Text)
        raw = skp._parsed["defs_dict"][defn.id]["builder"].dimensions
        self.assertEqual((raw[0]["a"], raw[0]["b"], raw[0]["offset"]), ((0.0, 0.0, 0.0), (0.0, 10.0, 0.0), 2.0))
        self.assertLessEqual({"a", "b", "offset", "text", "hidden"}, set(raw[0]))
        self.assertIn("ROOT", skp._parsed["defs_dict"])

    def test_felder_der_anmerkungen_im_modell(self):
        from openskp import model as M
        want = {M.TextEntity: {"text", "hidden", "point", "label_point"},
                M.Dimension: {"text", "hidden", "a", "b", "offset"},
                M.SectionPlane: {"plane", "name", "hidden"},
                M.Page: {"name"},
                M.Instance: {"attribute_dictionaries", "properties"},
                M.Definition: {"texts", "dimensions", "section_planes", "is_image"},
                M.SkpModel: {"pages", "dimensions"}}
        for cls, fields in want.items():
            with self.subTest(cls=cls.__name__):
                self.assertLessEqual(fields, {f.name for f in dataclasses.fields(cls)})

    def test_persistente_ids_aus_einem_zaehler(self):
        """OpenSKP 1.3.0 zaehlt die persistenten IDs ueber alle Abschnitte fort (vorher begann jeder
        Abschnitt neu, IDs doppelten sich). Texte und Masse aus write_text/write_dimension in
        Definitionen und im Modell muessen denselben Zaehler benutzen: jede ID genau einmal, der
        Zaehler im Dateikopf mindestens so hoch wie die groesste ID."""
        from openskp import legacy
        for reader in (legacy._read_text, legacy._read_dimlinear, legacy._read_face, legacy._read_instance):
            self.assertIn("_preamble", reader.__code__.co_names)  # nur dann sieht persistente_ids sie
        self.assertEqual(_params(legacy._preamble), ["ar", "r"])
        self.assertEqual(_params(CREATE._ArchiveWriter._preamble), ["self", "pid", "real_attrs"])
        self.assertIsInstance(CREATE._PID_COUNTER_POS, int)
        b = SkpBuilder()
        farbe = b.add_material("Farbe", [10, 20, 30])
        b.add_texture_material("Bild", _png(self.tmp / "b.png", (1, 2, 3)))
        ebene = b.add_layer("Ebene")
        with b.add_component_definition("Innen") as innen:
            innen.add_face(self.QUAD, material=farbe)
            core.write_text(innen, b, "innen", (1, 1, 0), (2, 3, 4))
            core.write_dimension(innen, b, (0, 0, 0), (0, 10, 0), 2.0)
        with b.add_component_definition("Aussen") as aussen:
            aussen.add_instance(innen, translation=(1, 2, 3), attribute_dicts=[("d", {"k": 1})])
            core.write_dimension(aussen, b, (0, 0, 0), (10, 0, 0), -1.0, text="x", hidden=True)
        b.add_instance(aussen, translation=(0, 0, 0), layer=ebene)
        b.add_face(self.QUAD)
        core.write_text(b, b, "Modell", (9, 9, 9), (9, 9, 12))
        b.add_text("OpenSKP", (0, 0, 0))
        out = self.tmp / "ids.skp"
        out.write_bytes(b.to_bytes())
        found, counter = persistente_ids(out)
        kinds = collections.Counter(kind for kind, _ in found)
        self.assertEqual((kinds["_read_text"], kinds["_read_dimlinear"]), (3, 2))
        self.assertGreaterEqual(kinds["_read_instance"], 2)
        ids = [pid for _, pid in found]
        doppelt = {pid: n for pid, n in collections.Counter(ids).items() if n > 1}
        self.assertEqual(doppelt, {}, "persistente IDs doppelt vergeben")
        self.assertGreaterEqual(counter, max(ids))

    def test_writer_texte_nur_bis_254_zeichen(self):
        """core._STR_MAX: laengere Texte bricht _write_str ab, nachdem schon Bytes geschrieben sind."""
        w = CREATE._ArchiveWriter(next_slot=1, class_slot={})
        w._write_str("x" * core._STR_MAX)
        with self.assertRaises(SkpWriteError):
            w._write_str("x" * (core._STR_MAX + 1))


# ---------------------------------------------------------------- Importpruefung in core.py

class TestImportpruefung(unittest.TestCase):
    def _load_core_copy(self):
        spec = importlib.util.spec_from_file_location("skptool._core_probe", core.__file__)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_fehlender_name_bricht_den_import_ab(self):
        saved = dict(vars(CREATE))
        try:
            for name in ("_uv_matrix_for_face", "_solve_uv_matrix", "face_uv_basis"):
                with self.subTest(name=name):
                    removed = vars(CREATE).pop(name)
                    try:
                        with self.assertRaises(ImportError) as cm:
                            self._load_core_copy()
                    finally:
                        setattr(CREATE, name, removed)
                    msg = str(cm.exception)
                    self.assertIn("OpenSKP-Version passt nicht zu skptool", msg)
                    self.assertIn(name, msg)
                    self.assertIn("1.3.0", msg)
        finally:
            vars(CREATE).clear()
            vars(CREATE).update(saved)
        self.assertIs(CREATE._uv_matrix_for_face, saved["_uv_matrix_for_face"])

    def test_andere_writer_basis_bricht_den_import_ab(self):
        """Rechnet der Writer wieder in einer eigenen Basis, darf skptool nicht still weiterlaufen."""
        with mock.patch.object(CREATE, "face_uv_basis", lambda n: ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0))):
            with self.assertRaises(ImportError) as cm:
                self._load_core_copy()
        self.assertIn("face_uv_basis", str(cm.exception))
        self.assertIs(CREATE.face_uv_basis, _face_groups.face_uv_basis)


if __name__ == "__main__":
    unittest.main()
