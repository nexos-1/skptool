"""Tests fuer skptool diff. Start: .venv\\Scripts\\python -m unittest tests.test_diff -v

Der Blender-Test (Rundreise mit Geometrievergleich) wird ohne Blender uebersprungen.
"""
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from skptool import cli, core
from skptool.blender import BlenderError, find_blender

ROOT = Path(__file__).resolve().parents[1]
S2017 = ROOT / "samples" / "stuhl_tisch_2017.skp"
MM = 1 / 25.4  # Zoll je mm

try:
    find_blender()
    HAVE_BLENDER = True
except BlenderError:
    HAVE_BLENDER = False


def run_cli(*args):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with redirect_stdout(out), redirect_stderr(err):
        try:
            cli.main(list(args))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            if not isinstance(exc.code, int) and exc.code:
                err.write(str(exc.code))
    return code, out.getvalue(), err.getvalue()


def mini_modell(path, rot=(200, 30, 30), versatz_mm=0.0, extra_ebene=None, ebene="Basis"):
    """Kleines Modell: zwei Materialien, eine Ebene, eine Komponente "Kiste" zweimal platziert."""
    b = core.create()
    m_rot = b.add_material("Rot", list(rot))
    m_blau = b.add_material("Blau", [20, 40, 220])
    lay = b.add_layer(ebene)
    if extra_ebene:
        b.add_layer(extra_ebene)
    with b.add_component_definition("Kiste") as kiste:
        kiste.add_face([(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0)], material=m_rot)
        kiste.add_face([(0, 0, 0), (0, 10, 0), (0, 10, 10), (0, 0, 10)], material=m_blau)
    b.add_face([(-50, -50, 0), (-40, -50, 0), (-40, -40, 0)], material=m_blau)
    b.add_instance(kiste, name="Kiste", translation=(0.0, 0.0, 0.0), layer=lay)
    b.add_instance(kiste, name="Kiste", translation=(100.0 + versatz_mm * MM, 0.0, 0.0), layer=lay)
    core.save_atomic(b, path)
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="skptool_diff_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def diff_json(self, a, b, *extra):
        code, out, err = run_cli("diff", str(a), str(b), "--json", *extra)
        self.assertIn(code, (0, 1), err)
        return code, json.loads(out)


class TestDiffStruktur(Base):
    def test_same_file_is_equal(self):
        code, out, err = run_cli("diff", str(S2017), str(S2017))
        self.assertEqual(code, 0, out + err)
        self.assertIn("Ergebnis: gleich", out)
        self.assertIn("Platzierungen: 15 gleich, 0 nur in A, 0 nur in B, 0 geaendert", out)

    def test_rewrite_is_structurally_equal(self):
        out = self.tmp / "umgeschrieben.skp"
        core.rewrite_legacy(S2017, out)
        code, data = self.diff_json(S2017, out)
        self.assertEqual(code, 0, json.dumps(data["abschnitte"], indent=1)[:3000])
        self.assertTrue(data["gleich"])
        self.assertEqual(data["abschnitte"]["definitionen"]["gleich"], 9)

    def test_exactly_the_three_changes(self):
        a = mini_modell(self.tmp / "a.skp")
        b = mini_modell(self.tmp / "b.skp", rot=(250, 30, 30), versatz_mm=25.0, extra_ebene="Extra")
        code, data = self.diff_json(a, b)
        self.assertEqual(code, 1)
        self.assertFalse(data["gleich"])
        ab = data["abschnitte"]
        eintraege = {name: [(e["art"], e["name"]) for e in s["eintraege"]] for name, s in ab.items()}
        self.assertEqual(eintraege, {
            "modell": [],
            "ebenen": [("nur_in_b", "Extra")],
            "materialien": [("geaendert", "Rot")],
            "definitionen": [],
            "platzierungen": [("geaendert", "ROOT / Kiste")],
        })
        self.assertIn("Farbe #c81e1e -> #fa1e1e", ab["materialien"]["eintraege"][0]["text"])
        verschoben = ab["platzierungen"]["eintraege"][0]
        self.assertEqual(verschoben["text"], "verschoben um 25 mm")
        self.assertAlmostEqual(verschoben["verschoben_mm"], 25.0, places=3)
        self.assertEqual(verschoben["a"]["definition"], "Kiste")
        self.assertEqual(verschoben["a"]["ebene"], "Basis")
        self.assertAlmostEqual(verschoben["a"]["matrix"][0][3], 2540.0, places=3)  # 100 Zoll in mm
        self.assertAlmostEqual(verschoben["b"]["matrix"][0][3], 2565.0, places=3)
        self.assertEqual(verschoben["b"]["matrix"][3], [0.0, 0.0, 0.0, 1.0])
        self.assertEqual(ab["platzierungen"]["gleich"], 1)
        self.assertEqual(ab["ebenen"]["gleich"], 2)  # Layer0 und Basis
        # Textausgabe: Kopfzeile je Abschnitt mit Zaehlern, dann die Eintraege
        code, out, _ = run_cli("diff", str(a), str(b))
        self.assertEqual(code, 1)
        self.assertIn("Ebenen: 2 gleich, 0 nur in A, 1 nur in B, 0 geaendert", out)
        self.assertIn("nur in B:   Extra", out)
        self.assertIn("geaendert:  ROOT / Kiste  verschoben um 25 mm", out)
        self.assertIn("Ergebnis: verschieden (3 Unterschiede)", out)
        # -q: nur der Rueckgabewert
        self.assertEqual(run_cli("diff", "-q", str(a), str(b))[:2], (1, ""))
        # --nur: nur die gewaehlten Abschnitte
        code, data = self.diff_json(a, b, "--nur", "ebenen,definitionen")
        self.assertEqual(set(data["abschnitte"]), {"ebenen", "definitionen"})
        self.assertEqual(code, 1)
        self.assertEqual(self.diff_json(a, b, "--nur", "definitionen")[0], 0)

    def test_rotation_layer_and_extra_placement(self):
        a = mini_modell(self.tmp / "a.skp")
        b = core.create()
        m_rot = b.add_material("Rot", [200, 30, 30])
        m_blau = b.add_material("Blau", [20, 40, 220])
        lay = b.add_layer("Basis")
        andere = b.add_layer("Andere")
        with b.add_component_definition("Kiste") as kiste:
            kiste.add_face([(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0)], material=m_rot)
            kiste.add_face([(0, 0, 0), (0, 10, 0), (0, 10, 10), (0, 0, 10)], material=m_blau)
        b.add_face([(-50, -50, 0), (-40, -50, 0), (-40, -40, 0)], material=m_blau)
        b.add_instance(kiste, name="Kiste", translation=(0.0, 0.0, 0.0), layer=andere)
        b.add_instance(kiste, name="Kiste", translation=(100.0, 0.0, 0.0), layer=lay,
                       matrix3x3=(0, -1, 0, 1, 0, 0, 0, 0, 1))
        b.add_instance(kiste, name="Kiste", translation=(0.0, 500.0, 0.0), layer=lay)
        core.save_atomic(b, self.tmp / "b.skp")
        code, data = self.diff_json(a, self.tmp / "b.skp", "--nur", "platzierungen")
        self.assertEqual(code, 1)
        texte = sorted((e["art"], e["text"]) for e in data["abschnitte"]["platzierungen"]["eintraege"])
        self.assertEqual(texte, [("geaendert", "Ebene Basis -> Andere"), ("geaendert", "gedreht oder skaliert"),
                                 ("nur_in_b", "Kiste bei (0, 12700, 0) mm")])

    def test_tolerance_boundary(self):
        a = mini_modell(self.tmp / "a.skp")
        b = mini_modell(self.tmp / "b.skp", versatz_mm=1.0)
        self.assertEqual(run_cli("diff", "-q", str(a), str(b), "--toleranz", "1.01")[0], 0)
        self.assertEqual(run_cli("diff", "-q", str(a), str(b), "--toleranz", "0.99")[0], 1)
        # Standard 0.1 mm: 0,09 mm gleich, 0,11 mm verschieden
        self.assertEqual(run_cli("diff", "-q", str(a), str(mini_modell(self.tmp / "c.skp", versatz_mm=0.09)))[0], 0)
        self.assertEqual(run_cli("diff", "-q", str(a), str(mini_modell(self.tmp / "d.skp", versatz_mm=0.11)))[0], 1)
        for bad in ("0", "-1", "nan", "abc"):
            self.assertEqual(run_cli("diff", str(a), str(b), "--toleranz", bad)[0], 2, bad)
        self.assertEqual(run_cli("diff", str(a), str(b), "--nur", "ebenen,quatsch")[0], 2)

    def test_errors_give_exit_code_2(self):
        kaputt = self.tmp / "kaputt.skp"
        kaputt.write_bytes(b"das ist keine SketchUp-Datei" * 10)
        code, out, err = run_cli("diff", str(S2017), str(kaputt))
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("keine SketchUp-Datei", err)
        code, out, err = run_cli("diff", str(self.tmp / "fehlt.skp"), str(S2017))
        self.assertEqual(code, 2)
        self.assertIn("Datei nicht gefunden", err)
        abgeschnitten = self.tmp / "abgeschnitten.skp"
        abgeschnitten.write_bytes(S2017.read_bytes()[:4000])
        code, out, err = run_cli("diff", "-q", str(S2017), str(abgeschnitten))
        self.assertEqual(code, 2, out + err)
        self.assertIn("FEHLER", err)

    def test_json_has_fixed_keys(self):
        code, data = self.diff_json(S2017, S2017)
        self.assertEqual(code, 0)
        self.assertEqual(set(data), {"gleich", "a", "b", "abschnitte"})
        self.assertIs(data["gleich"], True)
        self.assertEqual(list(data["abschnitte"]),
                         ["modell", "ebenen", "materialien", "definitionen", "platzierungen"])
        for ab in data["abschnitte"].values():
            self.assertLessEqual({"gleich", "nur_in_a", "nur_in_b", "geaendert", "eintraege"}, set(ab))
        for seite in ("a", "b"):
            self.assertEqual(data[seite]["version"], "17.0.1")
            self.assertEqual(data[seite]["platzierungen"], 15)
            self.assertEqual(data[seite]["flaechen_platziert"], 128)
            self.assertEqual(data[seite]["baum"][0], "Modell: 0 Flaechen, 2 Platzierungen")

    def test_limit_and_all(self):
        a = self.tmp / "a.skp"
        b = core.create()
        for i in range(30):
            b.add_layer(f"Ebene{i:02d}")
        b.add_face([(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0)])
        core.save_atomic(b, a)
        leer = core.create()
        leer.add_face([(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0)])
        core.save_atomic(leer, self.tmp / "b.skp")
        code, out, _ = run_cli("diff", str(a), str(self.tmp / "b.skp"))
        self.assertEqual(code, 1)
        self.assertEqual(out.count("nur in A:"), 25)
        self.assertIn("... 5 weitere Eintraege, alle anzeigen mit --all", out)
        code, out, _ = run_cli("diff", str(a), str(self.tmp / "b.skp"), "--all")
        self.assertEqual(out.count("nur in A:"), 30)

    def test_layer_names_cannot_control_the_terminal(self):
        a = mini_modell(self.tmp / "a.skp")
        b = mini_modell(self.tmp / "b.skp", extra_ebene="\x1b]52;c;Y2FsYy5leGU=\x07Ebene\u202e")
        code, out, err = run_cli("diff", str(a), str(b))
        self.assertEqual(code, 1, err)
        self.assertIn("Ebene", out)
        for bad in ("\x1b", "\x07", "\u202e"):
            self.assertNotIn(bad, out + err)
        self.assertIn("\\x1b", out)


class TestDiffGeometrie(Base):
    def test_geometry_detects_moved_points(self):
        a = mini_modell(self.tmp / "a.skp")
        b = mini_modell(self.tmp / "b.skp", versatz_mm=5.0)
        code, data = self.diff_json(a, b, "--geometrie", "--nur", "ebenen")
        self.assertEqual(code, 1)
        geo = data["abschnitte"]["geometrie"]
        self.assertGreater(geo["nur_in_a"], 0)
        self.assertGreater(geo["nur_in_b"], 0)
        self.assertEqual(self.diff_json(a, a, "--geometrie", "--texturen")[0], 0)

    @unittest.skipUnless(HAVE_BLENDER, "Blender nicht gefunden")
    def test_blender_round_trip_is_equal_with_geometry(self):
        blend, back = self.tmp / "stuhl.blend", self.tmp / "zurueck.skp"
        self.assertEqual(run_cli("convert", str(S2017), "-o", str(blend), "-q")[0], 0)
        self.assertEqual(run_cli("convert", str(blend), "-o", str(back), "-q")[0], 0)
        code, out, err = run_cli("diff", str(S2017), str(back), "--geometrie")
        self.assertEqual(code, 0, out + err)
        self.assertIn("Geometrie: 288 Punkte gleich, 0 nur in A, 0 nur in B, 0 geaendert", out)


if __name__ == "__main__":
    unittest.main()
