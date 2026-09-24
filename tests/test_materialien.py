"""Materialien: Namen je Flaeche und getoente Texturen (Colorize).

Start: .venv\\Scripts\\python -m unittest tests.test_materialien -v
Blender-Tests laufen nur, wenn Blender gefunden wird; Tests mit fremden Beispieldateien nur, wenn
samples/extern/ geladen ist (tools/beispiele_laden.py).
"""
import collections
import hashlib
import io
import json
import shutil
import struct
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import numpy as np
from openskp import SkpFile

from skptool import cli, core, einfaerben
from skptool.blender import BlenderError, find_blender

ROOT = Path(__file__).resolve().parents[1]
S2020 = ROOT / "samples" / "extern" / "gondel_2020.skp"
S2026 = ROOT / "samples" / "extern" / "gross_2026.skp"
EXTERN_HINT = "Beispieldatei fehlt, laden mit: python tools/beispiele_laden.py"

try:
    find_blender()
    HAVE_BLENDER = True
except BlenderError:
    HAVE_BLENDER = False
need_blender = unittest.skipUnless(HAVE_BLENDER, "Blender nicht gefunden")


def run_cli(*args):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with redirect_stdout(out), redirect_stderr(err):
        try:
            cli.main(list(args))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue() + err.getvalue()


def face_materials(path):
    """Zaehler (Definitionsname, Vorderseite, Rueckseite) ueber alle Flaechen, wie platziert nicht
    vervielfacht."""
    m = core.model_of(core.open_skp(path))
    byid = m.materials_by_id
    out = collections.Counter()
    for d in [m.root, *m.definitions.values()]:
        for f in d.faces.values():
            front, back = byid.get(f.material_id), byid.get(f.back_material_id)
            out[(d.name, front.name if front else None, back.name if back else None)] += 1
    return out


def glb_json(path):
    data = Path(path).read_bytes()
    n = struct.unpack("<I", data[12:16])[0]
    return json.loads(data[20:20 + n]), data


def muster_png(path):
    """Bunte 16x16-Textur mit Verlauf, damit eine Toenung sichtbar etwas aendert."""
    from PIL import Image
    x, y = np.meshgrid(np.arange(16), np.arange(16))
    px = np.stack([x * 15, y * 15, (x + y) * 7 + 20], -1).astype(np.uint8)
    Image.fromarray(px, "RGB").save(path)
    return str(path)


def zwei_weisse(path):
    """Zwei Materialien mit gleicher Farbe (weiss) in einer Komponente, dazu ein unbenutztes."""
    b = core.create()
    eins = b.add_material("Weiss_Eins", [255, 255, 255])
    zwei = b.add_material("Weiss_Zwei", [255, 255, 255])
    b.add_material("Unbenutzt", [10, 200, 30])
    with b.add_component_definition("Kiste") as kiste:
        kiste.add_face([(0, 0, 0), (100, 0, 0), (100, 100, 0), (0, 100, 0)], material=eins, back_material=eins)
        kiste.add_face([(0, 0, 0), (0, 100, 0), (0, 100, 100), (0, 0, 100)], material=zwei, back_material=zwei)
        kiste.add_face([(0, 0, 0), (100, 0, 0), (100, 0, 100), (0, 0, 100)], material=zwei)
    b.add_instance(kiste, name="Kiste", translation=(0.0, 0.0, 0.0))
    core.save_atomic(b, path)
    return path


def getoent(path, tmp, tint=(200, 40, 10)):
    """Modell mit einer eingefaerbten Textur (wie SketchUp: Originalbild plus Zielfarbe)."""
    b = core.create()
    mat = b.add_texture_material("Holz_getoent", muster_png(tmp / "muster.png"))
    assert core.set_texture_colorize(b, tint)
    plain = b.add_texture_material("Holz", muster_png(tmp / "muster2.png"))
    core.set_texture_average_color(b, (120, 120, 120))
    with b.add_component_definition("Brett") as brett:
        brett.add_face([(0, 0, 0), (100, 0, 0), (100, 100, 0), (0, 100, 0)], material=mat)
        brett.add_face([(0, 0, 0), (0, 100, 0), (0, 100, 100), (0, 0, 100)], material=plain)
    b.add_instance(brett, name="Brett", translation=(0.0, 0.0, 0.0))
    core.save_atomic(b, path)
    return path


def colorized(path):
    return {mt.name: (bool(mt.colorized), tuple(mt.color[:3]),
                      hashlib.sha256(mt.texture.data).hexdigest() if mt.texture and mt.texture.data else None)
            for mt in core.model_of(core.open_skp(path)).materials if mt.texture}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="skptool_mat_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------- Namen je Flaeche

class TestNamenJeFlaeche(Base):
    def test_glb_trennt_materialien_gleicher_farbe(self):
        src = zwei_weisse(self.tmp / "weiss.skp")
        glb = self.tmp / "weiss.glb"
        self.assertEqual(run_cli("convert", str(src), "-o", str(glb), "-q")[0], 0)
        js, _ = glb_json(glb)
        used = collections.Counter()
        for mesh in js["meshes"]:
            for p in mesh["primitives"]:
                used[js["materials"][p["material"]]["name"]] += js["accessors"][p["indices"]]["count"] // 3
        # je Quadrat 2 Dreiecke; Weiss_Zwei: ein doppelseitiges und ein einseitiges Quadrat
        self.assertEqual(used["Weiss_Eins"], 2)
        self.assertEqual(used["Weiss_Zwei"], 4)
        unbemalt = [m for m in js["materials"] if m["name"] == "SketchUp_Standard"]
        self.assertTrue(unbemalt and all(m["extras"]["skp_default"] for m in unbemalt))

    def test_ohne_namensliste_wird_wie_frueher_geraten(self):
        """Szene direkt aus OpenSKP (ohne core.build_instanced_scene): Rueckfall ueber die Farbe."""
        from openskp import instanced_scene

        from skptool.gltf_writer import _material_names
        skp = core.open_skp(zwei_weisse(self.tmp / "weiss.skp"))
        isc = instanced_scene.build_instanced_scene(skp._parsed)
        names = {m["name"] for m in _material_names(core.model_of(skp), isc, True)}
        self.assertIn("Weiss_Eins", names)
        self.assertNotIn("Weiss_Zwei", names)  # das bekannte Raten: das erste weisse gewinnt

    @need_blender
    def test_blender_rundreise_behaelt_beide_namen(self):
        src = zwei_weisse(self.tmp / "weiss.skp")
        blend, back = self.tmp / "weiss.blend", self.tmp / "zurueck.skp"
        self.assertEqual(run_cli("convert", str(src), "-o", str(blend), "-q")[0], 0)
        self.assertEqual(run_cli("convert", str(blend), "-o", str(back), "-q")[0], 0)
        a, b = face_materials(src), face_materials(back)
        self.assertEqual(a, b)
        names = {mt.name for mt in core.model_of(core.open_skp(back)).materials}
        self.assertIn("Unbenutzt", names)  # unbenutztes Material bleibt im Modell

    @need_blender
    @unittest.skipUnless(S2020.exists(), EXTERN_HINT)
    def test_gondel_mat1_bleibt_mat1(self):
        """Gondel: "mat1" (114 Flaechen in "1#2") kam frueher als "*5" zurueck, beide weiss."""
        blend, back = self.tmp / "gondel.blend", self.tmp / "gondel.skp"
        self.assertEqual(run_cli("convert", str(S2020), "-o", str(blend), "-q")[0], 0)
        self.assertEqual(run_cli("convert", str(blend), "-o", str(back), "-q")[0], 0)

        def front_names(path):
            c = collections.Counter()
            for (d, front, _back), n in face_materials(path).items():
                c[(d, front)] += n
            return c

        a, b = front_names(S2020), front_names(back)
        self.assertEqual(a[("1#2", "mat1")], 114)
        self.assertEqual(b[("1#2", "mat1")], 114)
        self.assertEqual(a[("1#2", "*5")], 0)
        self.assertEqual(b[("1#2", "*5")], 0)
        code, out = run_cli("diff", str(S2020), str(back), "--nur", "materialien")
        self.assertEqual(code, 0, out)


# ---------------------------------------------------------------- Colorize

class TestEinfaerbenFormel(unittest.TestCase):
    def test_durchschnitt_wird_zur_zielfarbe(self):
        """Nach SketchUps Verfahren landet die Durchschnittsfarbe genau auf der Materialfarbe."""
        for avg, ziel, art in [((139, 118, 89), (112, 88, 63), 0), ((8, 201, 241), (209, 103, 0), 0),
                               ((184, 184, 184), (234, 234, 234), 0), ((120, 120, 120), (200, 40, 10), 1),
                               ((139, 118, 89), (30, 90, 200), 1), ((200, 30, 30), (128, 128, 128), 0)]:
            with self.subTest(avg=avg, ziel=ziel, art=art):
                d = einfaerben.colorize_deltas(avg, ziel, art)
                px = einfaerben.colorize_pixels(np.array([[*avg, 77]], np.uint8), d, art)
                self.assertLessEqual(np.abs(px[0, :3].astype(int) - ziel).max(), 1)
                self.assertEqual(px[0, 3], 77)  # Alpha bleibt

    def test_einfaerben_setzt_einen_farbton(self):
        px = np.array([[255, 0, 0], [0, 255, 0], [30, 30, 200]], np.uint8)
        d = einfaerben.colorize_deltas((90, 90, 90), (0, 0, 255), einfaerben.TINT)
        out = einfaerben.colorize_pixels(px, d, einfaerben.TINT)
        h, _l, _s = einfaerben._rgb_to_hls(out.astype(np.float64) / 255)
        np.testing.assert_allclose(h, 240.0, atol=1.0)

    def test_grosse_bilder_werden_nicht_dekodiert(self):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("L", (1, 1)).save(buf, "PNG")
        data = bytearray(buf.getvalue())
        data[16:24] = struct.pack(">II", 40000, 40000)  # Kopf: 1,6 Mrd. Pixel
        self.assertIsNone(einfaerben.texture_average(bytes(data)))
        self.assertIsNone(einfaerben.colorized_png(bytes(data), (1, 2, 3)))


class TestColorize(Base):
    def test_umschreiben_behaelt_toenung(self):
        src = getoent(self.tmp / "getoent.skp", self.tmp)
        before = colorized(src)
        self.assertEqual(before["Holz_getoent"][:2], (True, (200, 40, 10)))
        self.assertFalse(before["Holz"][0])
        out = self.tmp / "neu.skp"
        stats = core.rewrite_legacy(src, out)
        self.assertEqual(stats["warnings"], [])
        self.assertEqual(colorized(out), before)

    @unittest.skipUnless(S2026.exists(), EXTERN_HINT)
    def test_umschreiben_gross_2026(self):
        out = self.tmp / "gross.skp"
        stats = core.rewrite_legacy(S2026, out)
        self.assertFalse([w for w in stats["warnings"] if "Toenung" in w])
        a, b = colorized(S2026), colorized(out)
        self.assertEqual(sum(1 for v in a.values() if v[0]), 6)
        self.assertEqual(a, b)  # eingefaerbt, Zielfarbe und Originalbild

    def test_glb_hat_getoentes_bild_und_weissen_faktor(self):
        src = getoent(self.tmp / "getoent.skp", self.tmp)
        glb = self.tmp / "g.glb"
        self.assertEqual(run_cli("convert", str(src), "-o", str(glb), "-q")[0], 0)
        js, data = glb_json(glb)
        (mat,) = [m for m in js["materials"] if m["name"] == "Holz_getoent"]
        pbr = mat["pbrMetallicRoughness"]
        self.assertEqual(pbr["baseColorFactor"][:3], [1.0, 1.0, 1.0])
        self.assertEqual(mat["extras"]["skp_colorize_rgb"], [200, 40, 10])
        img = js["images"][js["textures"][pbr["baseColorTexture"]["index"]]["source"]]
        view = js["bufferViews"][img["bufferView"]]
        start = 12 + 8 + struct.unpack("<I", data[12:16])[0] + 8 + view.get("byteOffset", 0)
        from PIL import Image

        def rgb(raw):
            return np.asarray(Image.open(io.BytesIO(raw)).convert("RGB"))
        (mt,) = [m for m in core.model_of(core.open_skp(src)).materials if m.name == "Holz_getoent"]
        px = rgb(data[start:start + view["byteLength"]])
        want = rgb(einfaerben.colorized_png(mt.texture.data, (200, 40, 10), mt.colorize_type))
        np.testing.assert_array_equal(px, want)  # genau SketchUps Rechnung
        self.assertGreater(np.abs(px.astype(int) - rgb(mt.texture.data)).max(), 50)  # nicht das Original
        (plain,) = [m for m in js["materials"] if m["name"] == "Holz"]
        self.assertEqual(plain["pbrMetallicRoughness"]["baseColorFactor"][:3], [1.0, 1.0, 1.0])
        self.assertNotIn("skp_colorize_rgb", plain.get("extras", {}))

    @need_blender
    def test_blender_rundreise_behaelt_toenung(self):
        src = getoent(self.tmp / "getoent.skp", self.tmp)
        blend, back = self.tmp / "g.blend", self.tmp / "zurueck.skp"
        self.assertEqual(run_cli("convert", str(src), "-o", str(blend), "-q")[0], 0)
        self.assertEqual(run_cli("convert", str(blend), "-o", str(back), "-q")[0], 0)
        a, b = colorized(src), colorized(back)
        self.assertEqual({k: v[:2] for k, v in b.items() if k == "Holz_getoent"},
                         {"Holz_getoent": (True, (200, 40, 10))})
        self.assertFalse(b["Holz"][0])
        # Blender speichert das Bild als PNG neu, die Pixel bleiben das Original (nicht getoent)
        from PIL import Image

        def pixels(path):
            (mt,) = [m for m in core.model_of(core.open_skp(path)).materials if m.name == "Holz_getoent"]
            return np.asarray(Image.open(io.BytesIO(mt.texture.data)).convert("RGB"))
        np.testing.assert_array_equal(pixels(src), pixels(back))
        self.assertEqual(set(a), set(b))

    @need_blender
    @unittest.skipUnless(S2026.exists(), EXTERN_HINT)
    def test_blender_rundreise_gross_2026(self):
        blend, back = self.tmp / "gross.blend", self.tmp / "gross.skp"
        self.assertEqual(run_cli("convert", str(S2026), "-o", str(blend), "-q")[0], 0)
        self.assertEqual(run_cli("convert", str(blend), "-o", str(back), "-q", "--no-verify")[0], 0)
        a = core.model_of(core.open_skp(S2026))
        b = {mt.name: mt for mt in SkpFile.open(str(back)).parse().materials}
        for mt in a.materials:
            if mt.id is None:
                continue
            with self.subTest(material=mt.name):
                self.assertIn(mt.name, b)  # nichts umbenannt oder zusammengelegt
                self.assertEqual(bool(b[mt.name].colorized), bool(mt.colorized))
                self.assertAlmostEqual(b[mt.name].transparency, mt.transparency, places=2)
                if mt.colorized:
                    self.assertEqual(tuple(b[mt.name].color[:3]), tuple(mt.color[:3]))


# ---------------------------------------------------------------- Deckkraft von Texturen

class TestDeckkraftTexturen(Base):
    """Der Platzhalter der Durchschnittsfarbe (Alpha 254, 255 hiesse "eingefaerbt") ist keine Deckkraft.
    Vorher wurde jede von OpenSKP oder skptool geschriebene Textur in der GLB halbdurchsichtig
    (BLEND, 0.996) und kam ueber Blender mit Deckkraft 0.996 zurueck."""

    def modell(self):
        b = core.create()
        plain = b.add_texture_material("Holz", muster_png(self.tmp / "muster.png"))
        core.set_texture_average_color(b, (120, 120, 120))
        glas = b.add_texture_material("Glas", muster_png(self.tmp / "muster2.png"), opacity=0.5)
        farbe = b.add_material("Fast_deckend", [10, 200, 30, 254])
        rot = b.add_material("Rot_halb", [200, 10, 10], opacity=0.4)
        for i, m in enumerate((plain, glas, farbe, rot)):
            x = i * 120
            b.add_face([(x, 0, 0), (x + 100, 0, 0), (x + 100, 100, 0), (x, 100, 0)], material=m)
        path = self.tmp / "deckkraft.skp"
        core.save_atomic(b, path)
        return path

    def test_openskp_liest_platzhalter_als_deckkraft(self):
        """Vertrag: ohne skptools Ersatz liefert OpenSKP fuer den Platzhalter 254/255. Faellt das weg,
        ist der Ersatz ueberfluessig (dann hier und in core.py entfernen)."""
        orig = core._resolve_transparency_openskp
        self.assertAlmostEqual(orig({"color": {"r": 1, "g": 2, "b": 3, "a": 254}}), 254 / 255)
        self.assertEqual(core._resolve_transparency_deckend({"color": {"a": 254}}), 1.0)
        self.assertAlmostEqual(core._resolve_transparency_deckend({"color": {"a": 255}, "transparency": 0.5}), 0.5)
        # Textur: Platzhalter-Alpha zaehlt nicht, die echte Deckkraft bleibt genau
        glas = {"color": {"a": 254}, "texture": {"name": "x"}, "transparency": 0.5}
        self.assertAlmostEqual(orig(glas), 0.5 * 254 / 255)
        self.assertEqual(core._resolve_transparency_deckend(glas), 0.5)
        # Farbe ohne Textur: ein echtes Alpha bleibt wirksam
        self.assertAlmostEqual(core._resolve_transparency_deckend({"color": {"a": 128}}), 128 / 255)

    def test_glb_textur_deckend_glas_durchsichtig(self):
        src = self.modell()
        glb = self.tmp / "d.glb"
        code, out = run_cli("convert", str(src), "-o", str(glb), "-q")
        self.assertEqual(code, 0, out)
        doc, _ = glb_json(glb)
        mats = {m["name"]: m for m in doc["materials"]}
        self.assertEqual(mats["Holz"]["pbrMetallicRoughness"]["baseColorFactor"][3], 1.0)
        self.assertNotEqual(mats["Holz"].get("alphaMode"), "BLEND")
        self.assertEqual(mats["Fast_deckend"]["pbrMetallicRoughness"]["baseColorFactor"][3], 1.0)
        self.assertAlmostEqual(mats["Glas"]["pbrMetallicRoughness"]["baseColorFactor"][3], 0.5, places=3)
        self.assertEqual(mats["Glas"].get("alphaMode"), "BLEND")
        self.assertAlmostEqual(mats["Rot_halb"]["pbrMetallicRoughness"]["baseColorFactor"][3], 0.4, places=3)
        self.assertEqual(mats["Rot_halb"].get("alphaMode"), "BLEND")

    def test_blender_rundreise_behaelt_deckkraft(self):
        try:
            find_blender()
        except BlenderError:
            self.skipTest("Blender nicht gefunden")
        src = self.modell()
        blend, back = self.tmp / "d.blend", self.tmp / "zurueck.skp"
        for a, b in ((src, blend), (blend, back)):
            code, out = run_cli("convert", str(a), "-o", str(b), "-q")
            self.assertEqual(code, 0, out)
        deck = {mt.name: (mt.transparency if mt.transparency is not None else 1.0)
                for mt in core.model_of(core.open_skp(back)).materials}
        self.assertEqual(deck["Holz"], 1.0)
        self.assertAlmostEqual(deck["Glas"], 0.5, places=2)
        self.assertAlmostEqual(deck["Rot_halb"], 0.4, places=2)


if __name__ == "__main__":
    unittest.main()
