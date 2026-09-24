"""Paarung gleicher Platzierungen in skptool diff. Start: .venv\\Scripts\\python -m unittest tests.test_diff_paarung -v

Fehler vorher: wurde der Tisch verschoben, meldete diff jedes Bein darin als verschoben (48,64 mm und
1048,64 mm statt 500 mm fuer den Tisch), weil die Beine ueber die Weltlage und naechste Position zuerst
gepaart wurden. Jetzt: Kinder nur innerhalb eines Paars, Lage relativ zur uebergeordneten Platzierung,
gleiche zuerst, der Rest optimal (Ungarische Methode).
"""
import itertools
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from skptool import core, vergleich
from skptool.blender import BlenderError, find_blender

from tests.test_diff import run_cli

ROOT = Path(__file__).resolve().parents[1]
S2017 = ROOT / "samples" / "stuhl_tisch_2017.skp"
S2026 = ROOT / "samples" / "extern" / "gross_2026.skp"
MM = 1 / 25.4  # Zoll je mm
BEINE = [(2, 2, 0), (38, 2, 0), (2, 18, 0), (38, 18, 0)]  # Zoll

try:
    find_blender()
    HAVE_BLENDER = True
except BlenderError:
    HAVE_BLENDER = False


def tisch_modell(path, versatz_mm=(0.0, 0.0, 0.0), beine=BEINE, zweiter_tisch=True):
    """Komponente "Tisch" mit Platte und vier gleichen Beinen, einmal oder zweimal platziert."""
    b = core.create()
    with b.add_component_definition("Bein") as bein:
        bein.add_face([(0, 0, 0), (2, 0, 0), (2, 2, 0), (0, 2, 0)])
    with b.add_component_definition("Platte") as platte:
        platte.add_face([(0, 0, 0), (40, 0, 0), (40, 20, 0), (0, 20, 0)])
    with b.add_component_definition("Tisch") as tisch:
        tisch.add_face([(0, 0, 0), (1, 0, 0), (1, 1, 0)])
        for pos in beine:
            tisch.add_instance(bein, translation=tuple(float(v) for v in pos))
        tisch.add_instance(platte, translation=(0.0, 0.0, 30.0))
    b.add_instance(tisch, translation=tuple(v * MM for v in versatz_mm))
    if zweiter_tisch:
        b.add_instance(tisch, translation=(100.0, 0.0, 0.0))
    core.save_atomic(b, path)
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="skptool_paarung_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def platzierungen(self, a, b):
        code, out, err = run_cli("diff", str(a), str(b), "--json", "--nur", "platzierungen")
        self.assertIn(code, (0, 1), err)
        return code, json.loads(out)["abschnitte"]["platzierungen"]


class TestPaarung(Base):
    def test_moved_table_reports_only_the_table(self):
        a = tisch_modell(self.tmp / "a.skp")
        b = tisch_modell(self.tmp / "b.skp", versatz_mm=(0.0, 500.0, 0.0))
        code, ab = self.platzierungen(a, b)
        self.assertEqual(code, 1)
        self.assertEqual([(e["art"], e["name"], e["text"]) for e in ab["eintraege"]],
                         [("geaendert", "ROOT / Tisch", "verschoben um 500 mm")])
        self.assertAlmostEqual(ab["eintraege"][0]["verschoben_mm"], 500.0, places=3)
        self.assertEqual((ab["gleich"], ab["nur_in_a"], ab["nur_in_b"], ab["geaendert"]), (11, 0, 0, 1))
        code, out, _ = run_cli("diff", str(a), str(b))
        self.assertIn("Platzierungen: 11 gleich, 0 nur in A, 0 nur in B, 1 geaendert", out)
        self.assertIn("geaendert:  ROOT / Tisch  verschoben um 500 mm", out)
        self.assertNotIn("Bein", out.split("Platzierungen:")[1])

    def test_swapped_identical_legs_are_equal(self):
        a = tisch_modell(self.tmp / "a.skp")
        b = tisch_modell(self.tmp / "b.skp", beine=[BEINE[3], BEINE[1], BEINE[2], BEINE[0]])
        code, out, err = run_cli("diff", str(a), str(b))
        self.assertEqual(code, 0, out + err)
        self.assertIn("Platzierungen: 12 gleich, 0 nur in A, 0 nur in B, 0 geaendert", out)

    def test_one_of_four_legs_moved(self):
        a = tisch_modell(self.tmp / "a.skp")
        beine = list(BEINE)
        beine[2] = (beine[2][0] + 30 * MM, beine[2][1], beine[2][2])
        b = tisch_modell(self.tmp / "b.skp", beine=beine)
        code, ab = self.platzierungen(a, b)
        self.assertEqual(code, 1)
        # beide Tische nutzen dieselbe Definition, also ist das Bein in beiden verschoben, sonst nichts
        self.assertEqual([(e["art"], e["name"], e["text"]) for e in ab["eintraege"]],
                         [("geaendert", "ROOT / Tisch / Bein", "verschoben um 30 mm")] * 2)
        self.assertEqual(ab["gleich"], 10)
        # nur ein Tisch: genau ein Eintrag
        a1 = tisch_modell(self.tmp / "a1.skp", zweiter_tisch=False)
        b1 = tisch_modell(self.tmp / "b1.skp", beine=beine, zweiter_tisch=False)
        code, ab = self.platzierungen(a1, b1)
        self.assertEqual([(e["art"], e["name"], e["text"]) for e in ab["eintraege"]],
                         [("geaendert", "ROOT / Tisch / Bein", "verschoben um 30 mm")])
        self.assertAlmostEqual(ab["eintraege"][0]["b"]["matrix"][0][3] - ab["eintraege"][0]["a"]["matrix"][0][3],
                               30.0, places=3)  # Weltlage in der Ausgabe

    def test_table_and_leg_moved_are_separate(self):
        a = tisch_modell(self.tmp / "a.skp", zweiter_tisch=False)
        beine = list(BEINE)
        beine[0] = (beine[0][0], beine[0][1] + 10 * MM, beine[0][2])
        b = tisch_modell(self.tmp / "b.skp", versatz_mm=(200.0, 0.0, 0.0), beine=beine, zweiter_tisch=False)
        code, ab = self.platzierungen(a, b)
        self.assertEqual([(e["name"], e["text"]) for e in ab["eintraege"]],
                         [("ROOT / Tisch", "verschoben um 200 mm"), ("ROOT / Tisch / Bein", "verschoben um 10 mm")])

    def test_missing_table_is_one_entry_with_its_parts(self):
        a = tisch_modell(self.tmp / "a.skp")
        b = tisch_modell(self.tmp / "b.skp", zweiter_tisch=False)
        code, ab = self.platzierungen(a, b)
        self.assertEqual(code, 1)
        self.assertEqual([(e["art"], e["name"], e["text"]) for e in ab["eintraege"]],
                         [("nur_in_a", "ROOT / Tisch", "Tisch bei (2540, 0, 0) mm, mit 5 enthaltenen Platzierungen")])
        self.assertEqual(ab["gleich"], 6)

    @unittest.skipUnless(HAVE_BLENDER, "Blender nicht gefunden")
    def test_repro_stuhl_tisch_moved_with_edit(self):
        """Der Fall aus dem Doku-Audit: Tisch mit skptool edit um 500 mm verschieben."""
        b = self.tmp / "tisch_verschoben.skp"
        ops = json.dumps([{"op": "move", "select": {"name": "Table"}, "by": [0, 0.5, 0]}])
        code, out, err = run_cli("edit", str(S2017), "-o", str(b), "--ops", ops, "-q")
        self.assertEqual(code, 0, out + err)
        code, ab = self.platzierungen(S2017, b)
        self.assertEqual(code, 1)
        self.assertEqual([(e["art"], e["name"], e["text"]) for e in ab["eintraege"]],
                         [("geaendert", "ROOT / Table", "verschoben um 500 mm")])
        self.assertEqual(ab["gleich"], 14)


class TestZuordnung(unittest.TestCase):
    def test_optimal_against_brute_force(self):
        rng = np.random.default_rng(7)
        for runde in range(400):
            n, m = (int(v) for v in rng.integers(1, 6, 2))
            k = rng.random((n, m)) * 100
            if runde % 3 == 0:
                k = np.round(k / 25)  # viele Gleichstaende
            paare = vergleich.zuordnung(k)
            self.assertEqual(len(paare), min(n, m))
            self.assertEqual(len({x for x, _ in paare}), len(paare))
            self.assertEqual(len({y for _, y in paare}), len(paare))
            if n <= m:
                best = min(sum(k[i, p[i]] for i in range(n)) for p in itertools.permutations(range(m), n))
            else:
                best = min(sum(k[p[j], j] for j in range(m)) for p in itertools.permutations(range(n), m))
            self.assertAlmostEqual(sum(k[x, y] for x, y in paare), best, places=9)

    def test_row_shifted_along_itself_pairs_each_with_its_own(self):
        """500 gleiche Teile in einer Reihe, alle entlang der Reihe verschoben: jedes mit sich selbst
        gepaart (mit einfachen Abstaenden waere jede Zuordnung gleich gut) und schnell."""
        m = np.zeros((500, 12))
        m[:, 0] = m[:, 4] = m[:, 8] = 1
        m[:, 9] = np.arange(500) * 50.0
        mb = m.copy()
        mb[:, 9] += 25_400.0
        t = time.perf_counter()
        paare = vergleich._paare_nach_abstand(m, mb, list(range(500)), list(range(500)))
        self.assertLess(time.perf_counter() - t, 5.0)
        self.assertTrue(all(i == j for i, j in paare))

    def test_degenerate_ties_are_fast(self):
        t = time.perf_counter()
        paare = vergleich.zuordnung(np.ones((500, 500)))
        self.assertLess(time.perf_counter() - t, 5.0)
        self.assertEqual(len(paare), 500)


@unittest.skipUnless(S2026.exists(), "samples/extern fehlt (tools/beispiele_laden.py)")
class TestZeitGross(Base):
    def test_gross_2026_against_rewrite(self):
        """Gemessen vor der Aenderung: Vergleich 0,014 bis 0,023 s, ganzer diff etwa 20 s (Einlesen)."""
        b = self.tmp / "umgeschrieben.skp"
        core.rewrite_legacy(S2026, b)
        sa, sb = vergleich.schnappschuss(S2026), vergleich.schnappschuss(b)
        t = time.perf_counter()
        erg = vergleich.vergleiche(sa, sb)
        dauer = time.perf_counter() - t
        self.assertLess(dauer, 2.0)
        ab = erg["abschnitte"]["platzierungen"]
        self.assertEqual((ab["gleich"], ab["nur_in_a"], ab["nur_in_b"], ab["geaendert"]), (372, 0, 0, 0))


if __name__ == "__main__":
    unittest.main()
