"""Rundreise SketchUp -> Blender -> SketchUp behaelt die Geometrie.

Frueher gingen Punkte verloren: Blender verschmolz koplanare Flaechen auch ueber weiche
SketchUp-Kanten und loeste dabei innere Punkte und Punkte auf geraden Kanten auf (Gondel: 898 von
30334 Punkten, eine Gruppe schrumpfte von 1110 auf 756 Flaechen). Dazu kam OpenSKPs Triangulierung,
die bei konkaven Flaechen Teile verlor und aus Zwischenpunkten Dreiecke ohne Flaeche machte.

Start: .venv\\Scripts\\python -m unittest tests.test_rundreise_geometrie -v
Die Blender-Tests werden ohne Blender uebersprungen, der Gondel-Test ohne die externe Beispieldatei.
"""
import collections
import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from openskp import _core

from skptool import cli, core, vergleich
from skptool.blender import BlenderError, find_blender

ROOT = Path(__file__).resolve().parents[1]
S2020 = ROOT / "samples" / "extern" / "gondel_2020.skp"
TOL_MM = 0.1

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
    return code, out.getvalue(), err.getvalue()


def _flaeche(verts, tri):
    (ax, ay, _), (bx, by, _), (cx, cy, _) = (verts[i] for i in tri)
    return ((bx - ax) * (cy - ay) - (cx - ax) * (by - ay)) / 2


class TestTriangulierung(unittest.TestCase):
    """Ohne Blender: die Zerlegung, die Szene, GLB und diff benutzen (openskp._core, ersetzt)."""

    def test_concave_face_is_fully_covered(self):
        # Wand aus der Gondel (Agrupar#8, Meter): OpenSKP 1.2.0 deckte davon nur 3.048 m2 ab
        ring = [(0.032, 2.3657), (0, 2.3773), (0, 2.3373), (-0.0858, 2.3685), (-1.0556, 2.3373), (-1.0556, 0),
                (-1.2376, 0), (-1.2376, 1.5), (-2.4376, 1.5), (-2.4376, 2.5), (1.382, 2.5), (1.382, 1.5),
                (0.182, 1.5), (0.182, 0), (0.032, 0)][::-1]  # gegen den Uhrzeigersinn um +Z
        verts = {i + 1: (float(x), float(y), 0.0) for i, (x, y) in enumerate(ring)}
        tris = _core.triangulate_face_3d(verts, [list(verts)], (0.0, 0.0, 1.0))
        areas = [_flaeche(verts, t) for t in tris]
        self.assertAlmostEqual(sum(areas), 3.38939076, places=6)
        self.assertTrue(all(a > 1e-9 for a in areas), areas)  # kein Dreieck ohne Flaeche, Umlauf wie die Flaeche
        self.assertEqual({i for t in tris for i in t}, set(verts))  # jeder Punkt kommt vor

    def test_quad_with_point_on_edge_has_no_empty_triangle(self):
        # Dreieck mit Zwischenpunkt auf einer Kante (in der Gondel so vorhanden)
        verts = {1: (0.0, -0.41, 0.0), 2: (0.0, 0.0, 0.0), 3: (0.0, 0.41, 0.0), 4: (0.6, 0.0, 0.64)}
        normal = (0.73, 0.0, -0.68)
        tris = _core.triangulate_face_3d(verts, [[1, 2, 3, 4]], normal)
        self.assertEqual(len(tris), 2)
        self.assertEqual({i for t in tris for i in t}, {1, 2, 3, 4})
        for t in tris:
            a, b, c = (verts[i] for i in t)
            u = [b[k] - a[k] for k in range(3)]
            w = [c[k] - a[k] for k in range(3)]
            cross = (u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2], u[0] * w[1] - u[1] * w[0])
            self.assertGreater(sum(cross[k] * normal[k] for k in range(3)), 1e-6)

    def test_convex_quad_is_unchanged(self):
        verts = {1: (0.0, 0.0, 0.0), 2: (1.0, 0.0, 0.0), 3: (1.0, 1.0, 0.0), 4: (0.0, 1.0, 0.0)}
        self.assertEqual(_core.triangulate_face_3d(verts, [[1, 2, 3, 4]], (0.0, 0.0, 1.0)),
                         [[1, 2, 3], [1, 3, 4]])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="skptool_rundreise_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rundreise(self, src):
        blend, back = self.tmp / "modell.blend", self.tmp / "zurueck.skp"
        code, out, err = run_cli("convert", str(src), "-o", str(blend), "-q")
        self.assertEqual(code, 0, out + err)
        code, out, err = run_cli("convert", str(blend), "-o", str(back))
        self.assertEqual(code, 0, out + err)
        return back, out

    def assert_points_kept(self, a, b):
        pa, ta = vergleich.geometrie_punkte(a, TOL_MM)
        pb, tb = vergleich.geometrie_punkte(b, TOL_MM)
        r = vergleich.punkte_vergleich(pa, ta, pb, tb)
        self.assertEqual(r["treffer_a"], r["punkte_a"],
                         f"{r['punkte_a'] - r['treffer_a']} von {r['punkte_a']} Punkten fehlen nach der Rundreise")
        self.assertEqual(r["treffer_b"], r["punkte_b"], "Punkte ohne Gegenstueck im Original")
        return r


@unittest.skipUnless(HAVE_BLENDER, "Blender nicht gefunden")
class TestRundreiseWeicheKanten(Base):
    def test_flat_fan_with_soft_edges_keeps_center_point(self):
        """Flache Platte aus vier Dreiecken um einen Mittelpunkt, alle Kanten weich: frueher wurde
        daraus in Blender ein Viereck, der Mittelpunkt fehlte."""
        src = self.tmp / "platte.skp"
        b = core.create()
        rot = b.add_material("Rot", [200, 30, 30])
        with b.add_component_definition("Platte") as d:
            c = (5.0, 5.0, 0.0)
            ecken = [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (10.0, 10.0, 0.0), (0.0, 10.0, 0.0)]
            for i in range(4):
                d.add_face([ecken[i], ecken[(i + 1) % 4], c], material=rot, soft_edges=True, smooth_edges=True)
        b.add_instance(d, name="Platte", translation=(0.0, 0.0, 0.0))
        core.save_atomic(b, src)
        back, _ = self.rundreise(src)
        r = self.assert_points_kept(src, back)
        self.assertEqual(r["punkte_a"], 5)
        faces = [len(dd.faces) for dd in core.model_of(core.open_skp(back)).definitions.values()]
        self.assertEqual(faces, [4])


@unittest.skipUnless(HAVE_BLENDER, "Blender nicht gefunden")
@unittest.skipUnless(S2020.exists(), "Beispieldatei fehlt, laden mit: python tools/beispiele_laden.py")
class TestRundreiseGondel(Base):
    def test_gondola_keeps_all_placed_points(self):
        back, out = self.rundreise(S2020)
        self.assertIn(" 0 uebersprungen", out)
        r = self.assert_points_kept(S2020, back)
        self.assertEqual(r["punkte_a"], 30334)
        # die Gruppe aus 1110 flachen Dreiecken mit weichen Kanten bleibt vollstaendig
        faces = collections.Counter(len(d.faces) for d in core.model_of(core.open_skp(back)).definitions.values())
        self.assertEqual(faces[1110], 1)


if __name__ == "__main__":
    unittest.main()
