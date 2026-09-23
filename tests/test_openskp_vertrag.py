"""Vertragstests fuer die OpenSKP-Interna, auf die skptool sich stuetzt.

skptool ist auf openskp 1.2.0 gepinnt und nutzt interne Funktionen, Attribute und
Fehlermeldungen. Aendert eine neue OpenSKP-Version davon etwas, soll das hier laut scheitern,
statt dass Texturen still schief liegen oder Farben fehlen. Kein Blender noetig.

Start: .venv\\Scripts\\python -m unittest tests.test_openskp_vertrag -v
"""
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


def _png(path, rgb):
    from PIL import Image
    Image.new("RGB", (8, 8), rgb).save(path)
    return str(path)


# ---------------------------------------------------------------- Testdaten: Binaerdump wie aus Blender

ROT = math.radians(30)


def _rotated_square(cx, cy, size):
    """Quadrat in der XY-Ebene, um 30 Grad gedreht: die erste Kante liegt nicht auf einer Achse,
    OpenSKPs eigene Basis (erste Kante) und die des Lesers (aus der Normale) unterscheiden sich."""
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
        self.assertEqual(_params(UPSTREAM._face_uv_basis), ["points", "normal"])
        self.assertEqual(_params(core._uv_basis_like_sketchup), _params(UPSTREAM._face_uv_basis))
        self.assertEqual(_params(CREATE._solve_uv_matrix), ["pairs", "basis"])
        self.assertEqual(_params(_face_groups.face_uv_basis), ["n"])

    def test_write_face_ruft_die_ersetzten_namen_als_modulglobale_auf(self):
        """Nur dann wirkt das Ersetzen im Modul. Ohne diese Namen im Code von write_face (bzw. in
        _uv_matrix_for_face) liefe die Ersetzung ins Leere."""
        self.assertIn("_uv_matrix_for_face", CREATE._ArchiveWriter.write_face.__code__.co_names)
        self.assertIn("_face_uv_basis", UPSTREAM._uv_matrix_for_face.__code__.co_names)
        self.assertIn("_solve_uv_matrix", UPSTREAM._uv_matrix_for_face.__code__.co_names)

    def test_ersetzungen_sind_aktiv(self):
        self.assertIs(CREATE._uv_matrix_for_face, core._uv_matrix_for_face)
        self.assertIs(CREATE._face_uv_basis, core._uv_basis_like_sketchup)
        self.assertIs(core._sketchup_face_uv_basis, _face_groups.face_uv_basis)

    def test_leser_basis_haengt_nur_an_der_normale(self):
        xr, yr = _face_groups.face_uv_basis((0.0, 0.0, 1.0))
        np.testing.assert_allclose(xr, (1, 0, 0), atol=1e-12)
        np.testing.assert_allclose(yr, (0, 1, 0), atol=1e-12)
        a = core._uv_basis_like_sketchup(_rotated_square(0, 0, 1), (0.0, 0.0, 1.0))
        np.testing.assert_allclose(a, (xr, yr), atol=1e-12)

    def test_edit_interna(self):
        self.assertEqual(_params(edit._definition_order), ["model"])
        self.assertEqual(_params(edit._definition_has_content), ["defn", "def_builders"])
        self.assertEqual(_params(edit._replay_body), ["target", "defn", "model", "material_slots", "layer_slots",
                                                     "warnings", "context", "def_builders"])

    def test_write_edge_chain_signatur(self):
        self.assertEqual(_params(CREATE._ArchiveWriter._write_edge_chain)[:6],
                         ["self", "points", "vertex_slots", "edge_registry", "closed", "hidden_edges"])

    def test_oeffentliche_writer_aufrufe_mit_den_genutzten_schluesselwoertern(self):
        need = {
            SkpBuilder.add_texture_material: {"name", "image_path", "applied_height", "opacity"},
            SkpBuilder.add_material: {"name", "rgba", "opacity"},
            SkpBuilder.add_layer: {"name", "color", "hidden"},
            SkpBuilder.add_component_definition: {"name"},
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
        self.assertEqual(_params(instanced_scene.build_instanced_scene), ["parsed"])
        self.assertEqual(_params(scene.build_scene), ["parsed"])
        self.assertIsNone(SkpFile.open(str(S2017))._parsed)  # core.build_scene prueft "is None"

    def test_felder_der_szenenobjekte(self):
        """Felder, die gltf_writer.py und core.py lesen."""
        want = {
            instanced_scene.InstancedScene: {"scene_hierarchy", "mesh_resources", "gltf_materials", "textures"},
            instanced_scene.InstancedMeshResource: {"id", "definition_id", "definition_name", "primitives"},
            instanced_scene.LocalPrimitive: {"positions", "normals", "uvs", "indices", "material_index"},
            instanced_scene.InstancedNode: {"name", "definition_name", "layer", "matrix", "position_mm",
                                            "mesh_resource_id", "children"},
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
            for name in ("name", "color", "transparency", "id", "texture", "colorized"):
                self.assertTrue(hasattr(mt, name), name)
        for layer in m.layers:
            for name in ("name", "hidden", "color_r", "color_g", "color_b"):
                self.assertTrue(hasattr(layer, name), name)
        defs = [m.root, *m.definitions.values()]
        for name in ("faces", "edges", "vertices", "instances", "name", "is_image"):
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
                mock.patch.object(core, "_uv_basis_like_sketchup", wraps=core._uv_basis_like_sketchup) as basis:
            stats = core.write_skp_from_bin(header, out)
        self.assertEqual(matrix.call_count, 3, "write_face ruft _uv_matrix_for_face nicht mehr je Flaeche auf")
        self.assertGreaterEqual(basis.call_count, 3, "skptools Texturmatrix wird nicht benutzt")
        self.assertEqual(stats.get("perspective"), 1)
        self.assertNotIn("uv_dropped", stats, "verzerrte Textur ging verloren")
        self.assertEqual(stats["faces"], 3)
        worst, checked = uv_errors(out, faces)
        self.assertGreaterEqual(checked, 12)
        self.assertLess(worst, 1e-4)

    def test_basis_ersatz_greift_auch_fuer_openskps_eigene_texturmatrix(self):
        """Rueckfallebene: ruft OpenSKP die eigene _uv_matrix_for_face auf, muss sie ueber das
        Modul das ersetzte _face_uv_basis nehmen (sonst liegen gedrehte Flaechen schief)."""
        faces = list(AFFINE_FACES)
        header = write_dump(self.tmp, faces, self.materials(), hard_first_edge=False)
        out = self.tmp / "t.skp"
        original = types.FunctionType(UPSTREAM._uv_matrix_for_face.__code__, vars(CREATE), "_uv_matrix_for_face")
        with mock.patch.object(CREATE, "_uv_matrix_for_face", original), \
                mock.patch.object(CREATE, "_face_uv_basis", wraps=CREATE._face_uv_basis) as basis:
            core.write_skp_from_bin(header, out)
        self.assertEqual(basis.call_count, 2)
        worst, checked = uv_errors(out, faces)
        self.assertGreaterEqual(checked, 8)
        self.assertLess(worst, 1e-4)

    def test_ohne_ersatz_waere_die_textur_falsch(self):
        """Gegenprobe: mit OpenSKPs eigener Basis liegt die gedrehte Flaeche schief. Faellt dieser
        Test, rechnen Leser und Writer inzwischen gleich und der Ersatz ist zu ueberdenken."""
        faces = [AFFINE_FACES[0]]
        header = write_dump(self.tmp, faces, self.materials(), hard_first_edge=False)
        out = self.tmp / "t.skp"
        with mock.patch.object(CREATE, "_uv_matrix_for_face", UPSTREAM._uv_matrix_for_face):
            core.write_skp_from_bin(header, out)
        worst, checked = uv_errors(out, faces)
        self.assertGreaterEqual(checked, 4)
        self.assertGreater(worst, 0.05)

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
            for name in ("_face_uv_basis", "_uv_matrix_for_face", "_solve_uv_matrix"):
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
                    self.assertIn("1.2.0", msg)
        finally:
            vars(CREATE).clear()
            vars(CREATE).update(saved)
        self.assertIs(CREATE._uv_matrix_for_face, saved["_uv_matrix_for_face"])


if __name__ == "__main__":
    unittest.main()
