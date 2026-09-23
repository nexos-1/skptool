"""Tests fuer skptool report (skptool/bericht.py). Start: .venv\\Scripts\\python -m unittest tests.test_bericht -v

Braucht weder Blender noch die externen Beispieldateien.
"""
import csv
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from openskp.model import Definition, Material, Page, SkpModel, Texture
from PIL import Image

from skptool import bericht, cli, core

ROOT = Path(__file__).resolve().parents[1]
S2017 = ROOT / "samples" / "stuhl_tisch_2017.skp"
S2025 = ROOT / "samples" / "leer_2025.skp"
QUADRAT = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0)]


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


def png(path: Path, w: int, h: int) -> Path:
    Image.new("RGB", (w, h), (30, 160, 60)).save(path)
    return path


class BerichtTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="skptool_bericht_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, name, materials=(), layers=(), textures=(), dynamic=False):
        """Kleines Modell mit core.create(): Texturen, Materialien, Ebenen, dann Geometrie."""
        b = core.create()
        slots = [b.add_texture_material(n, str(p)) for n, p in textures]
        slots += [b.add_material(n, rgba, **kw) for n, rgba, kw in materials]
        for n, hidden in layers:
            b.add_layer(n, hidden=hidden)
        if dynamic:
            with b.add_component_definition("Tuer") as cd:
                cd.add_face([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)])
            b.add_instance(cd, attribute_dicts=[("dynamic_attributes", {"_formatversion": 1.0, "lenx": 3.0})])
            b.add_instance(cd, translation=(5, 0, 0))
        for i, slot in enumerate(slots or [None]):
            b.add_face([(x, y, 5 * i) for x, y, _ in QUADRAT], material=slot)
        out = self.tmp / name
        core.save_atomic(b, out)
        return out

    def json_report(self, *paths):
        code, out, err = run_cli("report", *map(str, paths), "--json", "-q")
        return code, json.loads(out), err

    # ------------------------------------------------------------ Beispiele und Formate

    def test_beispiele_zwei_eintraege_mit_version(self):
        code, doc, _ = self.json_report(S2017, S2025)
        self.assertEqual(code, 0)
        self.assertEqual(doc["skptool"], bericht.__version__)
        self.assertEqual([e["name"] for e in doc["dateien"]], [S2017.name, S2025.name])
        self.assertEqual([e["version"] for e in doc["dateien"]], ["17.0.1", "25.0.575"])
        stuhl = doc["dateien"][0]
        self.assertTrue(stuhl["ok"])
        self.assertIsNone(stuhl["fehler"])
        self.assertEqual(stuhl["info"]["faces_total"], 86)  # Daten aus core.info()
        self.assertEqual(stuhl["analyse"]["platzierte_flaechen"], 128)  # 2 x 4 Beine mit je 7 Flaechen
        self.assertEqual(stuhl["analyse"]["definitionen"], 9)
        self.assertEqual(stuhl["analyse"]["platzierungen"], 15)
        self.assertEqual(stuhl["analyse"]["materialien"]["gesamt"], 4)
        self.assertEqual(stuhl["warnungen"], [])
        self.assertGreaterEqual(stuhl["einlesezeit_s"], 0)

    def test_text_ausgabe_je_datei_ein_block_und_zusammenfassung(self):
        code, out, _ = run_cli("report", str(S2017), str(S2025), "-q")
        self.assertEqual(code, 0)
        self.assertIn("stuhl_tisch_2017.skp\n", out)
        self.assertIn("Version:        17.0.1 -> SketchUp 2017", out)
        self.assertIn("Version:        25.0.575 -> SketchUp 2025", out)
        self.assertIn("Flaechen:       86 gespeichert, 128 platziert", out)
        self.assertIn("Zusammenfassung: 2 Dateien, 2 gelesen, 0 mit Fehler", out)

    def test_json_mehrere_dateien_ist_ein_gueltiges_dokument(self):
        a = self.build("a.skp", materials=[("Grau", [90, 90, 90, 255], {})])
        code, out, _ = run_cli("report", str(S2017), str(S2025), str(a), "--json", "-q")
        self.assertEqual(code, 0)
        doc = json.loads(out)  # genau ein Dokument, nicht eines je Datei
        self.assertEqual(set(doc), {"skptool", "dateien"})
        self.assertEqual(len(doc["dateien"]), 3)
        out.encode("ascii")  # rein ASCII, die Terminal-Maskierung kann es nicht verfaelschen
        ziel = self.tmp / "bericht.json"
        code, _, _ = run_cli("report", str(S2017), str(a), "-o", str(ziel), "-q")  # Format aus der Endung
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(ziel.read_text(encoding="utf-8"))["dateien"]), 2)

    def test_kaputte_datei_wird_fehlerzeile_und_lauf_geht_weiter(self):
        kaputt = self.tmp / "kaputt.skp"
        kaputt.write_bytes(S2017.read_bytes()[:4000])  # abgeschnitten, Kopf noch lesbar
        fremd = self.tmp / "fremd.skp"
        fremd.write_bytes(b"kein SketchUp " * 50)
        code, doc, _ = self.json_report(S2017, kaputt, fremd, S2025)
        self.assertEqual(code, 1)
        ok = {e["name"]: e for e in doc["dateien"]}
        self.assertEqual(len(ok), 4)
        self.assertTrue(ok[S2017.name]["ok"] and ok[S2025.name]["ok"])
        self.assertEqual(ok[S2025.name]["version"], "25.0.575")  # nach dem Fehler komplett gelesen
        self.assertFalse(ok["kaputt.skp"]["ok"])
        self.assertEqual(ok["kaputt.skp"]["version"], "17.0.1")
        self.assertIn("(Version 17.0.1)", ok["kaputt.skp"]["fehler"])  # Text aus cli._explain
        self.assertEqual(ok["fremd.skp"]["fehler"], "keine SketchUp-Datei (Dateikopf fehlt)")
        code, out, _ = run_cli("report", str(kaputt), str(S2017), "-q")
        self.assertEqual(code, 1)
        self.assertIn("FEHLER:", out)
        self.assertIn("1 gelesen, 1 mit Fehler", out)

    def test_datei_wird_einmal_gelesen_und_danach_freigegeben(self):
        import weakref
        from unittest import mock

        refs = []
        original = core.open_skp

        def zaehlen(path):
            skp = original(path)
            refs.append((weakref.ref(skp), weakref.ref(core.model_of(skp))))
            return skp

        with mock.patch.object(core, "open_skp", zaehlen):
            e = bericht.pruefe_datei(S2017)
        self.assertTrue(e["ok"])
        self.assertEqual(len(refs), 1, "info() und Analyse teilen sich ein Einlesen")
        self.assertIsNone(refs[0][0]())
        self.assertIsNone(refs[0][1](), "Modell nach der Datei freigegeben")

    def test_open_skp_bleibt_nach_fehler_unveraendert(self):
        original = core.open_skp
        bericht.pruefe_datei(self.tmp / "gibtsnicht.skp")
        self.assertIs(core.open_skp, original)

    def test_csv_entschaerft_formeln(self):
        f = self.build("formel.skp", materials=[('=HYPERLINK("x")', [1, 2, 3, 255], {}),
                                                ("Normal", [9, 9, 9, 255], {})],
                       layers=[("@SUMME(1)", False), ("-2+3", True)])
        ziel = self.tmp / "bericht.csv"
        code, _, _ = run_cli("report", str(f), str(S2017), "--csv", "-o", str(ziel), "-q")
        self.assertEqual(code, 0)
        raw = ziel.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "UTF-8 mit BOM fuer Excel")
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""), delimiter=";"))
        self.assertEqual(len(rows), 2)
        row = rows[0]
        self.assertTrue(row["materialien_namen"].startswith("'=HYPERLINK(\"x\")"), row["materialien_namen"])
        self.assertEqual(row["ausgeblendete_ebenen"], "'-2+3")
        self.assertTrue(row["ebenen_namen"].startswith("Layer0"))
        for r in rows:
            for value in r.values():
                self.assertFalse(value[:1] in ("=", "+", "-", "@", "\t", "\r"), value)
        self.assertEqual(rows[1]["platzierte_flaechen"], "128")  # Zahlen bleiben Zahlen
        # auch auf stdout mit BOM
        code, out, _ = run_cli("report", str(f), "--csv", "-q")
        self.assertTrue(out.startswith("\ufeffdatei;status;"))
        self.assertIn("'=HYPERLINK(\"\"x\"\")", out)
        self.assertNotIn("\\r", out)  # Zeilenende nicht von der Terminal-Maskierung verunstaltet
        self.assertEqual(len(list(csv.reader(io.StringIO(out.lstrip("﻿")), delimiter=";"))), 2)
        self.assertIn(b"\r\n", raw)  # in der Datei Excel-uebliches CRLF

    def test_html_escaped_alles_und_hat_kein_skript(self):
        f = self.build("xss.skp", materials=[("<img src=x onerror=alert(2)>", [5, 5, 5, 255], {})],
                       layers=[("<script>alert(1)</script>", True)])
        ziel = self.tmp / "bericht.html"
        code, _, _ = run_cli("report", str(f), str(S2017), "-o", str(ziel), "-q")  # Format aus der Endung
        self.assertEqual(code, 0)
        page = ziel.read_text(encoding="utf-8")
        self.assertNotIn("<script", page.lower())
        self.assertNotIn("<img", page.lower())
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertIn("&lt;img src=x onerror=alert(2)&gt;", page)
        self.assertIn("<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; "
                      "style-src 'unsafe-inline'\">", page)
        for extern in ("http://", "https://", "src=\"", "href=", "@import", "url("):
            self.assertNotIn(extern, page)

    # ------------------------------------------------------------ Warnungen

    def test_warnungen_gleiche_farbe_dynamisch_und_riesige_textur(self):
        seite = bericht.MAX_TEXTURE_SIDE + 1
        gross = png(self.tmp / "gross.png", seite, 2)
        klein = png(self.tmp / "klein.png", 64, 32)
        f = self.build("warn.skp", textures=[("Riesig", gross), ("Klein", klein)],
                       materials=[("Rot A", [200, 0, 0, 255], {}), ("Rot B", [200, 0, 0, 255], {}),
                                  ("Rot Glas", [200, 0, 0, 255], {"opacity": 0.4})],
                       dynamic=True)
        code, doc, _ = self.json_report(f)
        self.assertEqual(code, 0)
        e = doc["dateien"][0]
        a = e["analyse"]
        self.assertEqual(a["gleiche_farben"], [{"farbe": "#c80000", "materialien": ["Rot A", "Rot B"]}])
        self.assertEqual(a["materialien"], {"gesamt": 5, "texturiert": 2, "getoent": 0, "transparent": 1})
        self.assertEqual(a["texturen"]["anzahl"], 2)
        self.assertEqual(a["texturen"]["groesste_kante_px"], seite)
        self.assertGreater(a["texturen"]["bytes"], 0)
        self.assertEqual(a["dynamische_komponenten"], ["Tuer"])
        text = "\n".join(e["warnungen"])
        self.assertIn('Materialien gleicher Farbe #c80000 koennen verwechselt werden: "Rot A", "Rot B"', text)
        self.assertNotIn("Rot Glas", text)  # andere Deckkraft, OpenSKP unterscheidet sie
        self.assertIn("1 dynamische Komponente (dynamic_attributes)", text)
        self.assertIn(f'"Riesig" ({seite} x 2 px)', text)
        self.assertNotIn("Klein", text)
        code, out, _ = run_cli("report", str(f), "-q")
        self.assertIn("  Warnungen:\n    - ", out)
        self.assertIn("Zusammenfassung: 1 Datei, 1 gelesen, 0 mit Fehler, 3 Warnungen", out)

    def test_warnungen_aus_readme_grenzen(self):
        tex = Texture(filename="holz.png", data=png(self.tmp / "t.png", 8, 8).read_bytes())
        root = Definition(id=0, name="ROOT")
        root.texts = [object(), object()]
        root.section_planes = [object()]
        model = SkpModel(version="{19.0.685}", root=root,
                         materials=[Material(name="Getoent", id=3, texture=tex, colorized=True)],
                         pages=[Page(name="Szene 1"), Page(name="Szene 2")])
        a = bericht.analysiere(model)
        self.assertEqual((a["szenen"], a["texte"], a["schnittebenen"]), (2, 2, 1))
        self.assertEqual(a["materialien"]["getoent"], 1)
        w = bericht.warnungen("19.0.685", 60 * 2**20, a)
        text = "\n".join(w)
        self.assertIn("SketchUp 2019", text)
        self.assertIn("60 MB gross", text)
        self.assertIn('1 getoente Textur (Colorize) verliert beim Umschreiben die Toenung: "Getoent"', text)
        self.assertIn("Werden nicht uebertragen: 2 Szenen, 2 Texte, 1 Schnittebene", text)
        self.assertIn("Sehr alte Version", "\n".join(bericht.warnungen_version("8.0.1")))
        self.assertEqual(bericht.warnungen_version("17.0.1"), [])
        self.assertEqual(bericht.warnungen_version(None), [])

    def test_bildgroesse_nur_aus_dem_kopf(self):
        # Kopf eines 40000 x 40000 PNG ohne Bilddaten: Pillow wuerde es als Bombe ablehnen
        head = png(self.tmp / "k.png", 1, 1).read_bytes()
        import struct
        import zlib
        ihdr = struct.pack(">IIBBBBB", 40000, 40000, 8, 2, 0, 0, 0)
        chunk = b"IHDR" + ihdr
        riese = head[:8] + struct.pack(">I", 13) + chunk + struct.pack(">I", zlib.crc32(chunk)) + head[33:]
        self.assertEqual(bericht.bildgroesse(riese), (40000, 40000))
        self.assertIsNone(bericht.bildgroesse(b"kein bild"))
        from PIL import Image as _Image
        self.assertIsNotNone(_Image.MAX_IMAGE_PIXELS)  # Grenze danach wieder aktiv

    # ------------------------------------------------------------ Ziel und Muster

    def test_ziel_darf_keine_eingabe_sein(self):
        a = self.tmp / "a.skp"
        shutil.copy(S2017, a)
        before = a.read_bytes()
        for args in ([str(a), "-o", str(a)], [str(self.tmp / "*.skp"), "-o", str(a)],
                     [str(a), "--json", "-o", str(self.tmp / "." / "a.skp")]):
            code, out, err = run_cli("report", *args, "-q")
            self.assertNotEqual(code, 0, args)
            self.assertIn("ist selbst eine Eingabe", err)
            self.assertEqual(a.read_bytes(), before)

    def test_ziel_wird_atomar_ersetzt(self):
        ziel = self.tmp / "bericht.txt"
        ziel.write_text("alt", encoding="utf-8")
        code, out, _ = run_cli("report", str(S2025), "-o", str(ziel), "-q")
        self.assertEqual(code, 0)
        self.assertIn("Bericht geschrieben", out)
        self.assertIn("leer_2025.skp", ziel.read_text(encoding="utf-8"))
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["bericht.txt"])  # keine Reste

    def test_rekursiv_findet_unterordner(self):
        tief = self.tmp / "projekte" / "a" / "b"
        tief.mkdir(parents=True)
        shutil.copy(S2017, tief / "tief.skp")
        shutil.copy(S2025, self.tmp / "projekte" / "oben.skp")
        muster = str(self.tmp / "projekte" / "**" / "*.skp")
        code, doc, _ = self.json_report(muster, "--rekursiv")
        self.assertEqual(code, 0)
        self.assertEqual(sorted(e["name"] for e in doc["dateien"]), ["oben.skp", "tief.skp"])
        code, _, err = run_cli("report", muster, "-q")  # ohne --rekursiv ist ** nur eine Ordnerebene
        self.assertNotEqual(code, 0)
        self.assertIn("Keine Datei passt", err)
        code, _, err = run_cli("report", str(self.tmp / "projekte"), "-q")
        self.assertNotEqual(code, 0)
        self.assertIn("ist ein Ordner", err)

    def test_doppelt_genannte_datei_zaehlt_einmal(self):
        code, doc, _ = self.json_report(S2017, S2017, ROOT / "samples" / "*.skp")
        self.assertEqual(code, 0)
        self.assertEqual(sorted(e["name"] for e in doc["dateien"]), sorted([S2017.name, S2025.name]))


if __name__ == "__main__":
    unittest.main()
