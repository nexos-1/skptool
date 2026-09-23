"""Kantensichtbarkeit (hart, weich, glatt, verborgen) beim Neuschreiben skp -> skp (2017-Format).

Frueher setzte die Wiedergabe die Flags je Flaeche ("irgendeine Randkante weich" -> alle neuen
Kanten weich) und liess lose Kanten weg: in gondel_2020.skp fiel "(3D)shelf2B" von 966 auf 19
sichtbare Kanten. Jetzt muss jede Kante der Quelle mit denselben Flags ankommen. Einzige erlaubte
Abweichung: Diagonalen aus dem Zerlegen unebener Flaechen (gross_2026.skp: 963 Flaechen), die es
in der Quelle nicht gibt. Sie muessen weich und glatt sein, damit sie wie in SketchUp unsichtbar
bleiben.

Start: .venv\\Scripts\\python -m unittest tests.test_kanten -v
"""
import collections
import shutil
import tempfile
import unittest
from pathlib import Path

from skptool import core

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
S2017 = SAMPLES / "stuhl_tisch_2017.skp"
S2020 = SAMPLES / "extern" / "gondel_2020.skp"
S2026 = SAMPLES / "extern" / "gross_2026.skp"
EXTERN_HINT = "Beispieldatei fehlt, laden mit: python tools/beispiele_laden.py"


def kanten(path):
    """{Definitionsname: {Kante als Punktpaar: (weich, glatt, verborgen)}}, ROOT fuer das Modell."""
    m = core.model_of(core.open_skp(path))
    out = collections.defaultdict(dict)
    for d in [m.root, *m.definitions.values()]:
        name = "ROOT" if d is m.root else d.name
        for e in d.edges.values():
            a, b = d.vertices.get(e.v1_id), d.vertices.get(e.v2_id)
            if a is None or b is None or (a.x, a.y, a.z) == (b.x, b.y, b.z):
                continue
            out[name][frozenset(((a.x, a.y, a.z), (b.x, b.y, b.z)))] = (e.soft, e.smooth, e.hidden)
    return out


def sichtbar(flags):
    return sum(1 for soft, _, hidden in flags.values() if not soft and not hidden)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="skptool_kanten_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def assert_kanten_gleich(self, src, max_diagonalen=0):
        out = self.tmp / f"{src.stem}_2017.skp"
        stats = core.rewrite_legacy(src, out)
        a, b = kanten(src), kanten(out)
        self.assertEqual(set(a) - set(b), set(), "Definitionen mit Kanten fehlen")
        diagonalen = 0
        for name, ea in a.items():
            eb = b[name]
            with self.subTest(definition=name):
                self.assertEqual(sichtbar(eb), sichtbar(ea), "sichtbare Kanten")
                fehlend = [k for k in ea if k not in eb]
                self.assertEqual(len(fehlend), 0, "Kanten der Quelle fehlen")
                anders = [(ea[k], eb[k]) for k in ea if eb[k] != ea[k]]
                self.assertEqual(anders[:5], [], f"{len(anders)} Kanten mit anderen Flags")
                neu = [eb[k] for k in eb if k not in ea]
                self.assertEqual({f for f in neu if f != (True, True, False)}, set(),
                                 "neue Kanten muessen weich und glatt sein (Diagonalen)")
                diagonalen += len(neu)
        self.assertLessEqual(diagonalen, max_diagonalen)
        if not stats["triangulated"]:
            self.assertEqual(diagonalen, 0)
        return a, stats


class TestKantenflagsJeKante(Base):
    def test_gemischte_flags_an_einer_flaeche_bleiben_erhalten(self):
        """Flaeche B hat drei harte und eine weiche Kante (geteilt mit A). Frueher wurde B ganz weich."""
        b = core.create()
        with b.add_component_definition("Teil") as teil:
            teil.add_face([(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0)], soft_edges=True, smooth_edges=True)
            teil.add_face([(10, 0, 0), (20, 0, 0), (20, 10, 0), (10, 10, 0)])
            teil.add_face([(0, 0, 5), (5, 0, 5), (5, 5, 5)], hidden_edges=True)
            teil.add_polyline([(30, 0, 0), (40, 0, 0)])  # lose Kante ohne Flaeche
        b.add_instance(teil, translation=(0.0, 0.0, 0.0))
        src = self.tmp / "gemischt.skp"
        core.save_atomic(b, src)
        a, stats = self.assert_kanten_gleich(src)
        flags = collections.Counter(a["Teil"].values())
        self.assertEqual(flags, {(True, True, False): 4, (False, False, False): 4, (False, False, True): 3})
        self.assertEqual(stats["edges"], 11)

    def test_stuhl(self):
        self.assert_kanten_gleich(S2017)

    @unittest.skipUnless(S2020.exists(), EXTERN_HINT)
    def test_gondel_2020(self):
        a, _ = self.assert_kanten_gleich(S2020)
        self.assertEqual(sichtbar(a["(3D)shelf2B"]), 966)

    @unittest.skipUnless(S2026.exists(), EXTERN_HINT)
    def test_gross_2026(self):
        # 963 zerlegte Flaechen ergeben 2164 Diagonalen (gemessen 2026-09-23)
        self.assert_kanten_gleich(S2026, max_diagonalen=2500)


if __name__ == "__main__":
    unittest.main()
