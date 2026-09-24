"""Texte, Bemassungen und Attribute beim Umschreiben skp -> skp (2017-Format).

Frueher gingen Texte und Bemassungen beim Umschreiben ganz verloren, von den Attributen der
Platzierungen kam nur "dynamic_attributes" an, und das als Text. Jetzt werden freie Texte und
Bemassungen in ihrer Definition geschrieben (lokale Koordinaten, die Platzierung bleibt), alle
Attribut-Woerterbuecher mit ihren Typen, und was nicht geht, meldet je Art eine Zeile mit Anzahl,
wortgleich in rewrite_legacy (stats["verluste"]), convert (stderr) und report.

Die Beispieldateien enthalten keine Texte und Bemassungen, deshalb baut der Test eigene Modelle
mit core.create(). Attribute stehen in gondel_2020 und gross_2026 (SU_InstanceSet).

Start: .venv\\Scripts\\python -m unittest tests.test_anmerkungen -v
"""
import collections
import dataclasses
import io
import json
import math
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from openskp.create import Length, Point3d, Timestamp, Vector3d
from openskp.model import Dimension, Page, SectionPlane, TextEntity

from skptool import bericht, cli, core
from tests.test_openskp_vertrag import persistente_ids

ROOT = Path(__file__).resolve().parents[1]
S2020 = ROOT / "samples" / "extern" / "gondel_2020.skp"
EXTERN_HINT = "Beispieldatei fehlt, laden mit: python tools/beispiele_laden.py"
QUAD = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0)]
DREH = (0.0, -1.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)  # 90 Grad um Z

ATTRIBUTE = {
    "dynamic_attributes": {"_formatversion": 1.0, "lenx": 12.5, "_name": "Tisch", "anzahl": 3,
                           "ja": True, "punkt": Point3d(1, 2, 3), "richtung": Vector3d(0, 0, 1),
                           "laenge": Length(2.0), "zeit": Timestamp(3_000_000_000), "leer": None,
                           "feld": [1, "a", 2.5, [4, 5]], "umlaut": "Tuer " + "".join(map(chr, (0xE4, 0xF6, 0xFC, 0x20AC)))},
    "SU_InstanceSet": {"Owner": "", "Status": ""},
}


def run_cli(*args):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli.main(list(args)) or 0
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


def anmerkungen_je_definition(path):
    """{Definitionsname: (Texte, Bemassungen)} aus der Datei, Bemassungen aus dem Rohergebnis."""
    skp = core.open_skp(path)
    m = core.model_of(skp)
    out = {}
    for key, d in [("ROOT", m.root), *m.definitions.items()]:
        name = "ROOT" if key == "ROOT" else d.name
        raw = skp._parsed["defs_dict"][key]["builder"].dimensions
        out[name] = (sorted((t.text, t.point, t.label_point, t.hidden) for t in d.texts),
                     sorted((r.get("a"), r.get("b"), r.get("offset"), r.get("text"), r.get("hidden"))
                            for r in raw))
    return out


def anzahlen(path):
    """{Definitionsname: (Flaechen, Kanten, Platzierungen, Texte, Masse)} beim Wiedereinlesen."""
    skp = core.open_skp(path)
    m = core.model_of(skp)
    out = {}
    for key, d in [("ROOT", m.root), *m.definitions.items()]:
        name = "ROOT" if key == "ROOT" else d.name
        raw = skp._parsed["defs_dict"][key]["builder"].dimensions
        out[name] = (len(d.faces), len(d.edges), len(d.instances), len(d.texts), len(raw))
    return out


def platzierungen(path):
    """Multimenge (Besitzer, Definition, Name, Matrix, Attribute als JSON) aller Platzierungen."""
    m = core.model_of(core.open_skp(path))
    c = collections.Counter()
    for d in [m.root, *m.definitions.values()]:
        owner = "ROOT" if d is m.root else d.name
        for i in d.instances:
            ref = m.definitions.get(i.ref_idx)
            c[(owner, getattr(ref, "name", "?"), i.name, tuple(round(v, 9) for v in i.matrix[:12]),
               json.dumps(i.attribute_dictionaries, sort_keys=True, default=list))] += 1
    return c


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="skptool_anmerkungen_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def build(self, name="quelle.skp"):
        """Texte und Bemassungen im Modell, in einer Definition und in einer verschachtelten,
        gedreht und verschoben platziert, dazu Attribute an Platzierungen auf beiden Ebenen."""
        b = core.create()
        moebel = b.add_layer("Moebel")
        with b.add_component_definition("Schublade") as s:
            s.add_face(QUAD)
            core.write_text(s, b, "Schublade innen", (1, 2, 3), (4, 5, 6))
            core.write_dimension(s, b, (0, 0, 0), (0, 10, 0), 3.5)
        with b.add_component_definition("Tisch") as t:
            t.add_face(QUAD)
            t.add_instance(s, translation=(2, 0, 1), matrix3x3=DREH,
                           attribute_dicts=[("Schublade", {"nr": 1})])
            core.write_text(t, b, "Tischplatte", (5, 5, 0), (5, 5, 20), hidden=True)
            core.write_dimension(t, b, (0, 0, 0), (10, 0, 0), -4.0, text="1 m")
        with b.add_component_definition("Nur Text") as nt:  # Definition ohne Geometrie
            core.write_text(nt, b, "allein", (0, 0, 0), (1, 1, 1))
        b.add_instance(t, translation=(100, 50, 0), matrix3x3=DREH, layer=moebel,
                       attribute_dicts=list(ATTRIBUTE.items()))
        b.add_instance(t, translation=(0, 0, 0))
        b.add_instance(nt, translation=(7, 7, 7))
        b.add_face([(0, 0, 0), (1, 0, 0), (1, 1, 0)])
        b.add_text("Modell", (1, 1, 1), leader=(2, 2, 2))
        b.add_dimension((0, 0, 0), (0, 50, 0), offset=-3.0)
        out = self.tmp / name
        core.save_atomic(b, out)
        return out


class TestTexteUndBemassungen(Base):
    def test_bleiben_in_ihrer_definition_erhalten(self):
        src = self.build()
        out = self.tmp / "ziel.skp"
        st = core.rewrite_legacy(src, out)
        self.assertEqual((st["texts"], st["dimensions"]), (4, 3))
        vorher, nachher = anmerkungen_je_definition(src), anmerkungen_je_definition(out)
        self.assertEqual(nachher, vorher)
        self.assertEqual(vorher["Tisch"][0], [("Tischplatte", (5.0, 5.0, 0.0), (5.0, 5.0, 20.0), True)])
        self.assertEqual(vorher["Tisch"][1], [((0.0, 0.0, 0.0), (10.0, 0.0, 0.0), -4.0, "1 m", False)])
        self.assertEqual(vorher["ROOT"][1], [((0.0, 0.0, 0.0), (0.0, 50.0, 0.0), -3.0, "", False)])
        self.assertEqual(st["verluste"], ["Dynamische Komponenten: 1 Platzierung behaelt ihre Attribute als "
                                          "Daten, die Formeln an der Definition liest OpenSKP nicht, das "
                                          "dynamische Verhalten geht verloren: \"Tisch\""])

    def test_platzierungen_und_lage_bleiben_gleich(self):
        """Die Anmerkungen stehen lokal, also muessen Matrix und Verschachtelung unveraendert sein."""
        src = self.build()
        out = self.tmp / "ziel.skp"
        core.rewrite_legacy(src, out)
        self.assertEqual(platzierungen(out), platzierungen(src))
        # Gleiche lokale Punkte in derselben Definition und gleiche Matrizen auf dem ganzen Pfad
        # (Tisch gedreht und verschoben, darin die Schublade gedreht) = gleiche Lage in der Welt.
        m = core.model_of(core.open_skp(out))
        defs = {d.name: d for d in m.definitions.values()}
        (text,) = defs["Schublade"].texts
        self.assertEqual(text.point, (1.0, 2.0, 3.0))
        (inner,) = defs["Tisch"].instances
        self.assertEqual(m.definitions[inner.ref_idx].name, "Schublade")
        self.assertEqual([round(v, 9) for v in inner.matrix[:12]],
                         [*DREH, 2.0, 0.0, 1.0])

    def test_zweites_umschreiben_ist_stabil(self):
        src = self.build()
        a, b = self.tmp / "a.skp", self.tmp / "b.skp"
        core.rewrite_legacy(src, a)
        core.rewrite_legacy(a, b)
        self.assertEqual(anmerkungen_je_definition(b), anmerkungen_je_definition(src))
        self.assertEqual(platzierungen(b), platzierungen(src))

    def test_persistente_ids_eindeutig_und_anzahlen_gleich(self):
        """OpenSKP 1.3.0 vergibt persistente IDs aus einem Zaehler fuer die ganze Datei. Texte und
        Masse aus write_text/write_dimension (in Definitionen und im Modell) muessen ihn mitbenutzen:
        nach dem Umschreiben jede ID genau einmal, Zaehler im Kopf nicht kleiner als die groesste ID,
        und beim Wiedereinlesen je Definition gleich viele Flaechen, Kanten, Platzierungen, Texte
        und Masse wie in der Quelle."""
        src = self.build()
        a, b = self.tmp / "a.skp", self.tmp / "b.skp"
        core.rewrite_legacy(src, a)
        core.rewrite_legacy(a, b)
        for path in (src, a, b):
            with self.subTest(datei=path.name):
                found, counter = persistente_ids(path)
                kinds = collections.Counter(kind for kind, _ in found)
                self.assertEqual((kinds["_read_text"], kinds["_read_dimlinear"]), (4, 3))
                ids = [pid for _, pid in found]
                self.assertEqual({p: n for p, n in collections.Counter(ids).items() if n > 1}, {})
                self.assertGreaterEqual(counter, max(ids))
                self.assertEqual(anzahlen(path), anzahlen(src))

    @unittest.skipUnless(S2020.exists(), EXTERN_HINT)
    def test_persistente_ids_eindeutig_gondel(self):
        """Echte Datei mit Attributen (SU_InstanceSet) und vielen Definitionen."""
        out = self.tmp / "gondel.skp"
        core.rewrite_legacy(S2020, out)
        found, counter = persistente_ids(out)
        ids = [pid for _, pid in found]
        self.assertGreater(len(ids), 10_000)
        self.assertEqual({p: n for p, n in collections.Counter(ids).items() if n > 1}, {})
        self.assertGreaterEqual(counter, max(ids))

    def test_bemassungen_ab_2021_kommen_aus_der_modellliste(self):
        """Dateien ab 2021 fuehren Bemassungen nur in model.dimensions, in Weltkoordinaten."""
        src = self.build()
        echt = core.model_of

        def wie_2021(skp):
            m = echt(skp)
            for d in [m.root, *m.definitions.values()]:
                d.dimensions = []
            m.dimensions = [Dimension(a=(1.0, 2.0, 3.0), b=(4.0, 2.0, 3.0), offset=6.0)]
            return m

        out = self.tmp / "ziel.skp"
        with mock.patch.object(core, "model_of", wie_2021):
            st = core.rewrite_legacy(src, out)
        self.assertEqual(st["dimensions"], 1)
        nachher = anmerkungen_je_definition(out)
        self.assertEqual(nachher["ROOT"][1], [((1.0, 2.0, 3.0), (4.0, 2.0, 3.0), 6.0, "", False)])
        self.assertEqual(nachher["Tisch"][1], [])


class TestAttribute(Base):
    def test_alle_woerterbuecher_mit_typen(self):
        src = self.build()
        out = self.tmp / "ziel.skp"
        st = core.rewrite_legacy(src, out)
        self.assertEqual(st["attribute_dicts"], 3)
        self.assertEqual(st["attribute_lost"], 0)
        m = core.model_of(core.open_skp(out))
        tisch = [i for i in m.root.instances if i.attribute_dictionaries]
        self.assertEqual(len(tisch), 1)
        d = tisch[0].attribute_dictionaries
        self.assertEqual(set(d), {"dynamic_attributes", "SU_InstanceSet"})
        dyn = d["dynamic_attributes"]
        self.assertEqual(dyn["lenx"], 12.5)
        self.assertIsInstance(dyn["anzahl"], int)
        self.assertEqual(dyn["punkt"], (1.0, 2.0, 3.0))  # Punkt bleibt Punkt (Tupel), kein Feld
        self.assertEqual(dyn["feld"], [1, "a", 2.5, [4, 5]])
        self.assertEqual(dyn["zeit"], 3_000_000_000)
        self.assertIsNone(dyn["leer"])
        self.assertEqual(dyn["umlaut"], ATTRIBUTE["dynamic_attributes"]["umlaut"])
        self.assertEqual(tisch[0].properties["_name"], "Tisch")

    def test_nicht_schreibbare_werte_fallen_einzeln_weg(self):
        src = self.build()
        echt = core.model_of

        def mit_langem_wert(skp):
            m = echt(skp)
            inst = next(i for i in m.root.instances if i.attribute_dictionaries)
            inst.attribute_dictionaries["dynamic_attributes"]["lang"] = "x" * 300
            inst.attribute_dictionaries["dynamic_attributes"]["objekt"] = object()
            return m

        out = self.tmp / "ziel.skp"
        with mock.patch.object(core, "model_of", mit_langem_wert):
            st = core.rewrite_legacy(src, out)
        self.assertEqual(st["attribute_lost"], 2)
        self.assertIn("Attribute: 2 Werte gehen verloren (Text ueber 254 Zeichen oder unbekannter Typ)",
                      st["verluste"])
        dyn = next(i for i in core.model_of(core.open_skp(out)).root.instances
                   if i.attribute_dictionaries).attribute_dictionaries["dynamic_attributes"]
        self.assertNotIn("lang", dyn)
        self.assertEqual(dyn["lenx"], 12.5)  # der Rest des Woerterbuchs bleibt

    @unittest.skipUnless(S2020.exists(), EXTERN_HINT)
    def test_beispiel_gondel_behaelt_su_instanceset(self):
        out = self.tmp / "gondel.skp"
        st = core.rewrite_legacy(S2020, out)
        self.assertEqual(st["attribute_dicts"], 10)
        vorher = {k: n for k, n in platzierungen(S2020).items() if k[4] != "{}"}
        nachher = {k: n for k, n in platzierungen(out).items() if k[4] != "{}"}
        self.assertEqual(sum(vorher.values()), 10)
        self.assertEqual({(k[0], k[1], k[4]): n for k, n in nachher.items()},
                         {(k[0], k[1], k[4]): n for k, n in vorher.items()})
        self.assertEqual(st["verluste"], [])


class TestVerlustmeldungen(Base):
    def patched(self):
        """Modell wie aus einer Datei mit Szenen, Schnittebenen und nicht uebertragbaren Anmerkungen."""
        echt = core.model_of

        def mit_verlusten(skp):
            m = echt(skp)
            if getattr(m, "_skptool_test", False):
                return m
            m._skptool_test = True
            m.pages = [Page(name="Vorne"), Page(name="Oben"), Page(name="Schnitt")]
            m.root.section_planes = [SectionPlane(name="S1")]
            d = next(d for d in m.definitions.values() if d.name == "Tisch")
            d.section_planes = [SectionPlane(name="S2")]
            d.texts.append(TextEntity(text="an Kante", point=None))
            d.texts.append(TextEntity(text="y" * 300, point=(0.0, 0.0, 0.0)))
            m.root.dimensions.append(Dimension(text="ohne Punkte"))
            return m

        return mock.patch.object(core, "model_of", mit_verlusten)

    def test_eine_zeile_je_art_mit_anzahl(self):
        src = self.build()
        out = self.tmp / "ziel.skp"
        with self.patched():
            st = core.rewrite_legacy(src, out)
        v = st["verluste"]
        self.assertEqual([z.split(":")[0] for z in v],
                         ["Szenen", "Schnittebenen", "Texte", "Bemassungen", "Dynamische Komponenten"])
        self.assertEqual(v[0], "Szenen: 3 Szenen gehen verloren (der 2017-Writer kann keine Szenen schreiben)")
        self.assertEqual(v[1], "Schnittebenen: 2 Schnittebenen gehen verloren "
                               "(der 2017-Writer kann keine Schnittebenen schreiben)")
        self.assertEqual(v[2], "Texte: 2 von 6 gehen verloren (1 ohne freien Ankerpunkt, "
                               "1 laenger als 254 Zeichen)")
        self.assertEqual(v[3], "Bemassungen: 1 von 4 geht verloren (1 ohne lesbare Endpunkte)")
        self.assertEqual(st["texts"], 4)
        self.assertEqual(st["dimensions"], 3)
        for z in v:
            self.assertIn(z, st["warnings"])
            self.assertTrue(z.isascii(), z)

    def test_report_meldet_dieselben_zeilen(self):
        src = self.build()
        with self.patched():
            st = core.rewrite_legacy(src, self.tmp / "ziel.skp")
            code, out, _ = run_cli("report", str(src), "--json", "-q")
        self.assertEqual(code, 0)
        e = json.loads(out)["dateien"][0]
        self.assertEqual(e["analyse"]["verluste_umschreiben"], st["verluste"])
        for z in st["verluste"]:
            self.assertIn(z, e["warnungen"])
        self.assertEqual((e["analyse"]["texte"], e["analyse"]["bemassungen"], e["analyse"]["szenen"],
                          e["analyse"]["schnittebenen"]), (6, 4, 3, 2))
        self.assertIn("Nur beim Umschreiben nach .skp uebertragen, nicht nach Blender und in andere Formate: "
                      "6 Texte, 4 Bemassungen", e["warnungen"])

    def test_convert_zeigt_die_zeilen(self):
        src = self.build()
        out = self.tmp / "ziel.skp"
        with self.patched():
            code, stdout, stderr = run_cli("convert", str(src), "-o", str(out), "-q")
        self.assertEqual(code, 0, stderr)
        self.assertIn("OK   quelle.skp", stdout)
        zeilen = [z for z in stderr.splitlines() if z.startswith("Hinweis quelle.skp: ")]
        self.assertEqual(len(zeilen), 5, stderr)
        self.assertIn("Hinweis quelle.skp: Szenen: 3 Szenen gehen verloren", stderr)

    def test_ohne_verluste_keine_zeilen(self):
        b = core.create()
        b.add_face(QUAD)
        core.write_text(b, b, "nur Text", (0, 0, 0), (1, 1, 1))
        src = self.tmp / "klein.skp"
        core.save_atomic(b, src)
        st = core.rewrite_legacy(src, self.tmp / "ziel.skp")
        self.assertEqual((st["verluste"], st["texts"]), ([], 1))
        code, stdout, stderr = run_cli("convert", str(src), "-o", str(self.tmp / "z2.skp"), "-q")
        self.assertEqual(code, 0)
        self.assertNotIn("Hinweis", stderr)


class TestHilfsfunktionen(unittest.TestCase):
    def test_attributwerte(self):
        v, ok = core._attr_value((1, 2, 3))
        self.assertTrue(ok)
        self.assertIsInstance(v, Point3d)
        self.assertEqual(core._attr_value([1, 2, 3]), ([1, 2, 3], True))  # Liste bleibt Feld
        v, ok = core._attr_value(2**31 + 5)
        self.assertTrue(ok)
        self.assertIsInstance(v, Timestamp)
        self.assertEqual(core._attr_value(-2**40), (float(-2**40), True))
        self.assertEqual(core._attr_value("x" * 254), ("x" * 254, True))
        self.assertFalse(core._attr_value("x" * 255)[1])
        self.assertFalse(core._attr_value(["ok", "x" * 255])[1])
        self.assertFalse(core._attr_value(b"bytes")[1])
        self.assertEqual(core._attr_value(True), (True, True))
        # feindliche Datei: tief verschachtelte Felder fallen weg statt den Writer zu ueberlasten
        tief = []
        for _ in range(500):
            tief = [tief]
        self.assertFalse(core._attr_value(tief)[1])
        flach = [[[1]]]
        self.assertEqual(core._attr_value(flach), (flach, True))

    def test_anmerkungen_ohne_rohergebnis(self):
        """Ohne parsed (report ueber ein nachgebautes Modell) gelten Bemassungen je Definition als
        nicht uebertragbar, das Modell selbst kennt ihre Endpunkte nicht."""
        from openskp.model import Definition, SkpModel
        root = Definition(id=0, name="ROOT")
        root.dimensions = [Dimension(text="a")]
        root.texts = [TextEntity(text="t", point=(1.0, 1.0, 1.0), label_point=None)]
        a = core.anmerkungen(SkpModel(root=root))
        self.assertEqual((a.masse_gesamt, a.masse_ohne_endpunkte), (1, 1))
        self.assertEqual(a.texte["ROOT"], [("t", (1.0, 1.0, 1.0), (1.0, 1.0, 1.0), False)])
        a = core.anmerkungen(SkpModel(root=Definition(), dimensions=[
            Dimension(a=(0.0, 0.0, 0.0), b=(0.0, 0.0, 0.0)), Dimension(a=(0.0, 0.0, 0.0), b=(1.0, 0.0, math.nan))]))
        self.assertEqual((a.masse_gesamt, a.masse_ohne_endpunkte, a.masse), (2, 2, {}))


if __name__ == "__main__":
    unittest.main()
