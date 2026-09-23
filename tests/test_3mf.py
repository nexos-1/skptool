"""Tests fuer den nativen 3MF-Export (3D-Druck).

Start: .venv\\Scripts\\python -m unittest tests.test_3mf -v

Die 3MF wird mit zipfile und defusedxml zurueckgelesen und gegen die GLB-Ausgabe, die gebackene
OpenSKP-Szene und "skptool info" geprueft.
"""
import dataclasses
import io
import json
import re
import shutil
import struct
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from defusedxml import ElementTree as SafeET
from openskp import instanced_scene

from skptool import cli, core
from skptool.export_3mf import drop_back_sides, write_3mf, _welded_mesh
from skptool.gltf_writer import write_instanced_glb

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
S2017 = SAMPLES / "stuhl_tisch_2017.skp"
S2020 = SAMPLES / "extern" / "gondel_2020.skp"
S2026 = SAMPLES / "extern" / "gross_2026.skp"
EXTERN_HINT = "Beispieldatei fehlt, laden mit: python tools/beispiele_laden.py"

NS = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
CT_NS = "{http://schemas.openxmlformats.org/package/2006/content-types}"
REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
ST_NUMBER = re.compile(r"^((\-|\+)?(([0-9]+(\.[0-9]+)?)|(\.[0-9]+))((e|E)(\-|\+)?[0-9]+)?)$")
COLOR = re.compile(r"^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$")


def _num(text):
    assert ST_NUMBER.match(text), f"keine ST_Number: {text!r}"
    return float(text)


def read_3mf(path):
    """3MF zuruecklesen und die Struktur nach Core-Spezifikation pruefen. Rueckgabe: dict mit
    Materialnamen, Objekten, Komponenten, platzierten Dreiecken und Weltgrenzen (mm)."""
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        assert names[0] == "[Content_Types].xml", names
        ct = SafeET.fromstring(zf.read("[Content_Types].xml"))
        defaults = {d.get("Extension"): d.get("ContentType") for d in ct.iter(CT_NS + "Default")}
        assert defaults["rels"] == "application/vnd.openxmlformats-package.relationships+xml", defaults
        assert defaults["model"] == "application/vnd.ms-package.3dmanufacturing-3dmodel+xml", defaults
        rels = SafeET.fromstring(zf.read("_rels/.rels"))
        targets = [r.get("Target") for r in rels.iter(REL_NS + "Relationship")
                   if r.get("Type") == "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"]
        assert len(targets) == 1 and targets[0].lstrip("/") in names, targets
        for info in zf.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0), info
        model = SafeET.fromstring(zf.read(targets[0].lstrip("/")))
    assert model.tag == "{%s}model" % NS["m"] and model.get("unit") == "millimeter"

    bases = model.findall("m:resources/m:basematerials", NS)
    assert len(bases) == 1
    pid = bases[0].get("id")
    mat_names = []
    for b in bases[0].findall("m:base", NS):
        assert COLOR.match(b.get("displaycolor")), b.get("displaycolor")
        mat_names.append(b.get("name"))

    objects = {}  # id -> dict
    mesh_count = 0
    for obj in model.findall("m:resources/m:object", NS):
        oid = obj.get("id")
        assert oid not in objects and oid != pid, oid
        assert obj.get("type") == "model"
        mesh = obj.find("m:mesh", NS)
        comps = obj.find("m:components", NS)
        assert (mesh is None) != (comps is None), "Objekt braucht genau Netz oder Komponenten"
        if mesh is not None:
            mesh_count += 1
            assert obj.get("pid") == pid and obj.get("pindex") is not None
            assert 0 <= int(obj.get("pindex")) < len(mat_names)
            verts = np.array([[_num(v.get(k)) for k in "xyz"] for v in mesh.iterfind("m:vertices/m:vertex", NS)])
            tris = []
            for t in mesh.iterfind("m:triangles/m:triangle", NS):
                idx = [int(t.get(k)) for k in ("v1", "v2", "v3")]
                assert len(set(idx)) == 3 and min(idx) >= 0 and max(idx) < len(verts), idx
                if t.get("p1") is not None:
                    assert t.get("pid") == pid and 0 <= int(t.get("p1")) < len(mat_names)
                tris.append(idx)
            objects[oid] = {"verts": verts, "tris": np.array(tris, np.int64).reshape(-1, 3),
                            "name": obj.get("name")}
        else:
            assert obj.get("pid") is None and obj.get("pindex") is None, "Baugruppe darf kein pid haben"
            parts = []
            for c in comps.findall("m:component", NS):
                ref = c.get("objectid")
                assert ref in objects, f"Objekt {ref} vor seiner Definition benutzt"
                parts.append((ref, _matrix(c.get("transform"))))
            objects[oid] = {"components": parts, "name": obj.get("name")}

    items = model.findall("m:build/m:item", NS)
    assert items, "build ohne item"
    lo, hi = np.full(3, np.inf), np.full(3, -np.inf)
    placed_tris, n_components, dets = 0, 0, []

    def walk(oid, m, depth=0):
        nonlocal lo, hi, placed_tris, n_components
        assert depth < 16
        o = objects[oid]
        if "verts" in o:
            w = o["verts"] @ m[:3] + m[3]
            lo, hi = np.minimum(lo, w.min(axis=0)), np.maximum(hi, w.max(axis=0))
            placed_tris += len(o["tris"])
            return
        for ref, cm in o["components"]:
            n_components += 1
            dets.append(float(np.linalg.det(cm[:3])))
            walk(ref, np.vstack([cm[:3] @ m[:3], cm[3] @ m[:3] + m[3]]), depth + 1)

    for it in items:
        assert it.get("objectid") in objects
        walk(it.get("objectid"), _matrix(it.get("transform")))
    return {"materials": mat_names, "mesh_objects": mesh_count, "components": n_components,
            "placed_triangles": placed_tris, "unique_triangles": sum(len(o["tris"]) for o in objects.values()
                                                                     if "tris" in o),
            "size_mm": hi - lo, "min_det": min(dets) if dets else 1.0, "objects": objects}


def _matrix(text):
    """3MF-transform (Zeilenvektoren) als 4x3-Matrix: p' = p @ M[:3] + M[3]."""
    if text is None:
        return np.vstack([np.eye(3), np.zeros(3)])
    vals = [_num(v) for v in text.split()]
    assert len(vals) == 12, text
    return np.array(vals).reshape(4, 3)


def glb_triangles(path):
    """Dreiecke je Netz der GLB (eindeutige Geometrie), aus dem JSON-Teil gelesen."""
    data = Path(path).read_bytes()
    n = struct.unpack_from("<I", data, 12)[0]
    gltf = json.loads(data[20:20 + n])
    return sum(gltf["accessors"][p["indices"]]["count"] // 3 for m in gltf["meshes"] for p in m["primitives"])


def baked_bounds_and_triangles(skp):
    """Weltgrenzen (mm, SketchUp-Achsen: Breite x, Tiefe y, Hoehe z) und Dreiecke der gebackenen
    Szene, genau wie "skptool info" sie rechnet, nur ungerundet."""
    lo, hi, tris = np.full(3, np.inf), np.full(3, -np.inf), 0
    for prim in core.build_scene(skp).glb_primitives:
        pos = np.asarray(prim.positions, np.float64).reshape(-1, 3)
        if len(pos):
            lo, hi = np.minimum(lo, pos.min(axis=0)), np.maximum(hi, pos.max(axis=0))
        tris += len(prim.indices) // 3
    size = (hi - lo) * 1000.0  # glTF: x Breite, y Hoehe, z Tiefe
    return np.array([size[0], size[2], size[1]]), tris


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="skptool_3mf_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class Test3mfDateien(Base):
    maxDiff = None

    def _check_file(self, src, expect_instancing=False):
        skp = core.open_skp(src)
        model = core.model_of(skp)
        isc = instanced_scene.build_instanced_scene(skp._parsed)
        out = self.tmp / (src.stem + ".3mf")
        warnings = []
        st = write_3mf(model, isc, out, warn=warnings.append)
        got = read_3mf(out)

        # Dreiecke: 3MF plus entfernte Rueckseiten = GLB (eindeutig) bzw. gebackene Szene (platziert)
        glb = self.tmp / (src.stem + ".glb")
        glb_stats = write_instanced_glb(model, isc, core._instance_info(model), glb, textures=False)
        self.assertEqual(glb_triangles(glb), glb_stats["triangles"])
        self.assertEqual(got["unique_triangles"], st["triangles"])  # inkl. gespiegelter Varianten
        self.assertEqual(st["source_triangles"], glb_stats["triangles"])
        self.assertEqual(st["kept_triangles"] + st["back_sides_dropped"] + st["degenerate_dropped"],
                         st["source_triangles"])
        size_baked, tris_baked = baked_bounds_and_triangles(skp)
        self.assertEqual(got["placed_triangles"], st["placed_triangles"])
        self.assertEqual(got["placed_triangles"] + st["placed_back_sides_dropped"], tris_baked)
        self.assertEqual(st["degenerate_dropped"], 0)
        self.assertEqual(st["skipped_placements"], 0)

        # Masse: gegen die ungerundete gebackene Szene auf 0.1 mm, gegen "skptool info" (auf mm gerundet)
        np.testing.assert_allclose(got["size_mm"], size_baked, atol=0.1)
        info = core.info(src, with_bounds=True, skp=skp)["size_m"]
        np.testing.assert_allclose(got["size_mm"], [info["width"] * 1000, info["depth"] * 1000,
                                                    info["height"] * 1000], atol=0.5 + 0.1)
        self.assertGreater(got["min_det"], 0, "gespiegelte Platzierungen muessen als Netzvariante kommen")
        self.assertEqual(got["components"], st["components"])
        self.assertEqual(got["mesh_objects"], st["mesh_objects"])
        if expect_instancing:
            self.assertLess(got["mesh_objects"], got["components"])
        # Hinweise auf offene Netze: je Objekt eine Zeile (hoechstens 20) plus Zusammenfassung
        if st["open_objects"]:
            self.assertTrue(any("nicht geschlossen" in w for w in warnings), warnings)
        else:
            self.assertFalse(any("nicht" in w and "geschlossen" in w for w in warnings), warnings)

        # gleiche Eingabe, gleiche Bytes
        again = self.tmp / "nochmal" / out.name
        write_3mf(model, isc, again, warn=lambda _t: None)
        self.assertEqual(out.read_bytes(), again.read_bytes())
        return st, got

    def test_stuhl_tisch(self):
        st, got = self._check_file(S2017, expect_instancing=True)
        self.assertEqual(got["mesh_objects"], 8)
        self.assertEqual(got["components"], 14)
        self.assertEqual(st["source_triangles"], 392)
        self.assertEqual(got["placed_triangles"], 352)  # 704 in der GLB, jede Flaeche dort beidseitig
        self.assertEqual(st["back_sides_unclear"], 0)
        # offen: Tisch- und Stuhlbein, der runde Gleiter unten ist eine einzelne Flaeche ohne Rand
        self.assertEqual(st["open_objects"], 2)
        self.assertEqual(len(got["materials"]), 5)
        self.assertTrue(all(got["materials"]))

    @unittest.skipUnless(S2020.exists(), EXTERN_HINT)
    def test_gondel_mit_spiegelungen(self):
        st, got = self._check_file(S2020, expect_instancing=True)
        self.assertGreater(st["mirrored_variants"], 0)

    @unittest.skipUnless(S2026.exists(), EXTERN_HINT)
    def test_gross(self):
        st, got = self._check_file(S2026)
        self.assertGreater(st["mirrored_variants"], 0)


class Test3mfEinzelheiten(Base):
    @classmethod
    def setUpClass(cls):
        cls.skp = core.open_skp(S2017)
        cls.model = core.model_of(cls.skp)

    def isc(self):
        return instanced_scene.build_instanced_scene(self.skp._parsed)

    def test_rueckseiten_nur_als_umgekehrte_zwillinge_entfernt(self):
        """Es fallen nur Dreiecke weg, deren Punkte mit umgekehrtem Umlauf bleiben, und die
        behaltene Seite zeigt wie die SketchUp-Flaeche (geschlossene Teile: positives Volumen)."""
        for res in self.isc().mesh_resources:
            verts, pos32, tris, mats, _ = _welded_mesh(res)
            defn = self.model.definitions.get(res.definition_id)
            kept, _, dropped, unclear = drop_back_sides(verts, pos32, tris, mats, defn)
            self.assertEqual(len(kept) + dropped, len(tris))
            self.assertEqual(unclear, 0)
            before = {tuple(sorted(t)) for t in tris.tolist()}
            after = {tuple(sorted(t)) for t in kept.tolist()}
            self.assertEqual(before, after, res.definition_name)
            kept_dir = {tuple(np.roll(t, -int(np.argmin(t)))) for t in kept.tolist()}
            self.assertEqual(len(kept_dir), len(kept), "zwei gleich gewundene Kopien behalten")
            v = verts[kept]
            vol = np.einsum("ij,ij->i", v[:, 0], np.cross(v[:, 1], v[:, 2])).sum() / 6
            self.assertGreater(vol, 0, res.definition_name)

    def test_sonderzeichen_in_namen_bleiben_wohlgeformt(self):
        evil = 'Holz <&"> \'' + chr(1) + chr(0xD800) + "]]>&amp;"
        model = dataclasses.replace(self.model, materials=[dataclasses.replace(mt, name=evil + mt.name)
                                                           for mt in self.model.materials])
        isc = self.isc()
        for res in isc.mesh_resources:
            res.definition_name = '<Bein & "Fuss">' + chr(0x1B) + "[31m"
        out = self.tmp / "a&b 'c'.3mf"
        write_3mf(model, isc, out, warn=lambda _t: None)
        got = read_3mf(out)  # parst mit defusedxml, wirft bei kaputtem XML
        evil_names = [n for n in got["materials"] if n != "SketchUp_Standard"]
        self.assertTrue(evil_names, got["materials"])
        for name in evil_names:  # Steuerzeichen und einzelne Surrogate sind weg, der Rest woertlich
            self.assertTrue(name.startswith('Holz <&"> \']]>&amp;'), repr(name))
        names = {o["name"] for o in got["objects"].values()}
        self.assertIn('<Bein & "Fuss">[31m', names)
        self.assertIn("a&b 'c'", names)

    def test_leere_szene_und_grenzen_schreiben_nichts(self):
        out = self.tmp / "ziel.3mf"
        out.write_bytes(b"alt")
        empty = SimpleNamespace(scene_hierarchy=instanced_scene.InstancedNode(), mesh_resources=[],
                                gltf_materials=[], textures=[])
        with self.assertRaises(ValueError):
            write_3mf(self.model, empty, out, warn=lambda _t: None)
        with self.assertRaises(ValueError):
            write_3mf(self.model, self.isc(), out, max_components=5, warn=lambda _t: None)
        self.assertEqual(out.read_bytes(), b"alt")
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["ziel.3mf"], "Zwischendatei liegengeblieben")

    def test_nan_in_matrix_wird_abgelehnt(self):
        isc = self.isc()
        node = isc.scene_hierarchy.children[0]
        node.matrix = (float("nan"),) + tuple(node.matrix[1:])
        with self.assertRaises(ValueError):
            write_3mf(self.model, isc, self.tmp / "x.3mf", warn=lambda _t: None)
        self.assertFalse((self.tmp / "x.3mf").exists())

    def test_cli_convert_und_hinweise_auf_stderr(self):
        out = self.tmp / "stuhl.3mf"
        err, buf = io.StringIO(), io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            try:
                cli.main(["convert", str(S2017), "-o", str(out)])
                code = 0
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(code or 0, 0, buf.getvalue() + err.getvalue())
        self.assertIn("OK", buf.getvalue())
        self.assertIn("Leg_Table", err.getvalue())
        self.assertIn("nicht druckfertig geschlossen", err.getvalue())
        first = out.read_bytes()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            try:
                cli.main(["convert", str(S2017), "-o", str(out)])
            except SystemExit:
                pass
        self.assertEqual(out.read_bytes(), first)
        self.assertIn(".3mf", core.NATIVE_FORMATS)

    def test_trimesh_liest_die_datei(self):
        """Gegenprobe mit trimesh, falls dessen 3MF-Leser (braucht networkx und lxml) verfuegbar ist."""
        try:
            import trimesh
        except ImportError:
            self.skipTest("trimesh fehlt")
        out = self.tmp / "t.3mf"
        write_3mf(self.model, self.isc(), out, warn=lambda _t: None)
        try:
            scene = trimesh.load(str(out), file_type="3mf")
        except (ImportError, ModuleNotFoundError) as exc:
            self.skipTest(f"trimesh kann 3MF hier nicht lesen: {exc}")
        got = read_3mf(out)
        np.testing.assert_allclose(scene.extents, got["size_mm"], atol=0.1)


if __name__ == "__main__":
    unittest.main()
