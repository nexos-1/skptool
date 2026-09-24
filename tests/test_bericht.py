"""Tests fuer skptool report (skptool/bericht.py). Start: .venv\\Scripts\\python -m unittest tests.test_bericht -v

Braucht weder Blender noch die externen Beispieldateien.
"""
import csv
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from xml.etree import ElementTree as ET

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
        # seit Namen je Flaeche mitgefuehrt werden, gibt es keine Verwechslung mehr und keine Warnung
        self.assertNotIn("verwechselt", text)
        self.assertNotIn("Rot Glas", text)  # andere Deckkraft, OpenSKP unterscheidet sie
        self.assertIn("Dynamische Komponenten: 1 Platzierung behaelt ihre Attribute als Daten", text)
        self.assertIn('das dynamische Verhalten geht verloren: "Tuer"', text)
        self.assertIn(f'"Riesig" ({seite} x 2 px)', text)
        self.assertNotIn("Klein", text)
        code, out, _ = run_cli("report", str(f), "-q")
        self.assertIn("  Warnungen:\n    - ", out)
        self.assertIn("Zusammenfassung: 1 Datei, 1 gelesen, 0 mit Fehler, 2 Warnungen", out)

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
        self.assertIn('1 getoente Textur (Colorize): beim Umschreiben bleibt die Toenung erhalten, die Art '
                      '(Farbton verschieben oder einfaerben) hat im 2017-Format kein bekanntes Feld: "Getoent"', text)
        self.assertIn("Szenen: 2 Szenen gehen verloren", text)
        self.assertIn("Schnittebenen: 1 Schnittebene geht verloren", text)
        # Texte ohne Ankerpunkt kann auch das Umschreiben nicht uebertragen
        self.assertIn("Texte: 2 von 2 gehen verloren (2 ohne freien Ankerpunkt)", text)
        self.assertIn("Nur beim Umschreiben nach .skp uebertragen, nicht nach Blender und in andere Formate: "
                      "2 Texte", text)
        self.assertEqual([z for z in w if z in a["verluste_umschreiben"]], a["verluste_umschreiben"])
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


    # ------------------------------------------------------------ CSV-Dialekte und Umdeuten

    def test_excel_deutet_um(self):
        """Faelle aus Excel 16 (deutsch): diese Texte kamen als Zahl, Datum, Uhrzeit, Prozent,
        Waehrung oder Wahrheitswert an."""
        for t in ["1-2", "1,5", "0012", "1E5", "5%", "1/2", "12:30", "3.4.", "WAHR", "TRUE", "1.5", "- 5",
                  "(5)", "5 \u20ac", "12.03.2024", "Jan 1", "Mai 2024", "12 Jan", "2024"]:
            self.assertTrue(bericht.excel_deutet_um(t), t)
        for t in ["Layer0", "2 St\u00fchle", "[Color_007]", "Walnut", "Material 1", "Glas 50%", "3D", "Mai",
                  "Tuer 1-2", "\u00c4pfel", "<auto>", "e", ""]:
            self.assertFalse(bericht.excel_deutet_um(t), t)

    def lies_csv(self, pfad, trenner):
        raw = pfad.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "UTF-8 mit BOM, sonst liest Excel ANSI")
        self.assertIn(b"\r\n", raw)
        return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""), delimiter=trenner))

    def test_csv_namen_bleiben_text(self):
        f = self.build("umdeuten.skp", materials=[("1-2", [1, 2, 3, 255], {})], layers=[("0012", True)])
        ziel = self.tmp / "b.csv"
        code, _, _ = run_cli("report", str(f), "--csv", "-o", str(ziel), "-q")
        self.assertEqual(code, 0)
        row = self.lies_csv(ziel, ";")[0]
        self.assertEqual(row["materialien_namen"], "'1-2")
        self.assertEqual(row["ausgeblendete_ebenen"], "'0012")
        self.assertEqual(row["ebenen_namen"], "Layer0, 0012")  # Liste: kein Datum, keine Zahl
        self.assertEqual(row["flaechen"], "1")  # Zahlenspalten bleiben Zahlen
        self.assertRegex(row["einlesezeit_s"], r"^\d+,\d\d$")

    def test_csv_international(self):
        f = self.build("formel.skp", materials=[("=1+1", [1, 2, 3, 255], {}), ("Holz, hell", [9, 9, 9, 255], {})])
        ziel = self.tmp / "b.csv"
        code, _, _ = run_cli("report", str(f), str(S2017), "--csv-international", "-o", str(ziel), "-q")
        self.assertEqual(code, 0)
        rows = self.lies_csv(ziel, ",")
        self.assertEqual(list(rows[0]), bericht.CSV_SPALTEN)
        self.assertTrue(rows[0]["materialien_namen"].startswith("'=1+1, Holz, hell"), rows[0])
        self.assertRegex(rows[1]["einlesezeit_s"], r"^\d+\.\d\d$")
        self.assertEqual(rows[1]["platzierte_flaechen"], "128")
        code, out, _ = run_cli("report", str(S2017), "--csv-international", "-q")  # stdout, mit BOM
        self.assertTrue(out.startswith("\ufeffdatei,status,"))
        code, _, err = run_cli("report", str(S2017), "--csv", "--csv-international", "-q")
        self.assertNotEqual(code, 0)


# ---------------------------------------------------------------- Gegenproben mit echten Programmen
# Laufen nur, wenn das Programm installiert ist (sonst uebersprungen). SKPTOOL_SKIP_OFFICE_TESTS=1
# schaltet Excel und LibreOffice ab. Jeder Test beendet nur die Prozesse, die er selbst startet.

EXCEL = r"C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE"
SOFFICE = next((p for p in [os.environ.get("SKPTOOL_SOFFICE", ""), shutil.which("soffice") or "",
                            r"C:\Program Files\LibreOffice\program\soffice.exe"] if p and Path(p).is_file()), None)
EDGE = next((p for p in [os.environ.get("SKPTOOL_EDGE", ""),
                         r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                         r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"] if p and Path(p).is_file()), None)
OFFICE_AUS = bool(os.environ.get("SKPTOOL_SKIP_OFFICE_TESTS"))
BOESE = ["=HYPERLINK(\"http://example.invalid\";\"x\")", "+SUMME(1;2)", "-2+3", "@SUMME(1;2)", "\tA1",
         "\uff1dSUMME(1)", "1-2", "0012", "12:30", "WAHR", "\u00c4pfel \u00d6l \u00df \u20ac \u6f22\u5b57 \U0001f600"]

# Oeffnet eine CSV wie ein Doppelklick (Local = Spracheinstellung) und schreibt alle Zellen als JSON.
EXCEL_PS1 = r"""
param([string]$Csv, [string]$Ziel)
$ErrorActionPreference = "Stop"
$vorher = @(Get-Process EXCEL -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
$xl = New-Object -ComObject Excel.Application
$meine = @(Get-Process EXCEL -ErrorAction SilentlyContinue | Where-Object { $vorher -notcontains $_.Id } | ForEach-Object { $_.Id })
try {
    $xl.Visible = $false; $xl.DisplayAlerts = $false; $xl.AutomationSecurity = 3
    $m = [Type]::Missing
    $wb = $xl.Workbooks.Open($Csv, 0, $true, $m, $m, $m, $true, $m, $m, $false, $false, $m, $false, $true)
    $ws = $wb.Worksheets.Item(1)
    $ur = $ws.UsedRange
    $werte = $ur.Value2
    $zeilen = @()
    for ($r = 1; $r -le $ur.Rows.Count; $r++) {
        $z = @()
        for ($c = 1; $c -le $ur.Columns.Count; $c++) {
            $v = $werte[$r, $c]
            $t = if ($null -eq $v) { "leer" } elseif ($v -is [double]) { "zahl" } elseif ($v -is [bool]) { "bool" } else { "text" }
            $z += ,@([string]$v, $t)
        }
        $zeilen += ,@($z)
    }
    $formeln = $ur.HasFormula
    $wb.Close($false)
    @{ formeln = [string]$formeln; zellen = $zeilen } | ConvertTo-Json -Depth 5 -Compress | Out-File -FilePath $Ziel -Encoding utf8
} finally {
    $xl.Quit()
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($xl)
    [GC]::Collect(); [GC]::WaitForPendingFinalizers()
    Start-Sleep -Milliseconds 500
    foreach ($id in $meine) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
}
"""


def lies_xlsx(pfad):
    """Erstes Blatt einer xlsx: Liste von Zeilen mit (Text, Typ), Formeln gezaehlt. Nur Standardbibliothek."""
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(pfad) as zf:
        shared = [("".join(t.text or "" for t in si.iter(ns + "t")))
                  for si in ET.fromstring(zf.read("xl/sharedStrings.xml")).iter(ns + "si")] \
            if "xl/sharedStrings.xml" in zf.namelist() else []
        sheet = ET.fromstring(zf.read("xl/worksheets/sheet1.xml"))
    grid, formeln = {}, 0
    for c in sheet.iter(ns + "c"):
        ref = c.get("r")
        col = 0
        for ch in re.match(r"[A-Z]+", ref).group(0):
            col = col * 26 + ord(ch) - 64
        row = int(re.search(r"\d+", ref).group(0)) - 1
        v, t = c.find(ns + "v"), c.get("t", "n")
        formeln += c.find(ns + "f") is not None
        if t == "s":
            grid[(row, col - 1)] = (shared[int(v.text)], "text")
        elif t in ("str", "inlineStr"):
            grid[(row, col - 1)] = ("".join(x.text or "" for x in c.iter(ns + "t")) if t == "inlineStr"
                                    else v.text or "", "text")
        elif v is not None:
            grid[(row, col - 1)] = (v.text, "zahl")
    n_r = max(r for r, _ in grid) + 1
    n_c = max(c for _, c in grid) + 1
    return [[grid.get((r, c), ("", "leer")) for c in range(n_c)] for r in range(n_r)], formeln


class OfficeTest(unittest.TestCase):
    """Die CSV so, wie Excel und LibreOffice sie oeffnen: keine Formel, Umlaute richtig, Spalten
    getrennt, Zahlen als Zahl, fremde Namen als Text und ungekuerzt."""
    setUp = BerichtTest.setUp
    tearDown = BerichtTest.tearDown
    build = BerichtTest.build

    def boeser_bericht(self, schalter="--csv"):
        pfade = [str(self.build(f"boese_{i}.skp", materials=[(name, [i, 20, 30, 255], {})],
                                layers=[(name, True)])) for i, name in enumerate(BOESE)]
        lang = [(f"Lang {i:02d} " + "abcdefghij" * 24, [i, 1, 1, 255], {}) for i in range(55)]
        pfade.append(str(self.build("umlaute_\u00c4\u00f6\u00fc_\u00df.skp", materials=lang)))
        ziel = self.tmp / "bericht.csv"
        code, _, err = run_cli("report", *pfade, str(S2017), schalter, "-o", str(ziel), "-q")
        self.assertEqual(code, 0, err)
        trenner = ";" if schalter == "--csv" else ","
        rows = list(csv.reader(io.StringIO(ziel.read_bytes().decode("utf-8-sig"), newline=""), delimiter=trenner))
        return ziel, rows

    def vergleiche(self, soll, ist, programm):
        self.assertEqual(len(ist), len(soll), programm)
        for r, (srow, irow) in enumerate(zip(soll, ist)):
            self.assertGreaterEqual(len(irow), len(srow), (programm, r))
            for c, want in enumerate(srow):
                got, typ = irow[c]
                spalte = soll[0][c]
                if r and re.fullmatch(r"\d+([.,]\d+)?", want):  # von skptool als Zahl geschrieben
                    self.assertEqual(typ, "zahl", (programm, spalte, want))
                    self.assertAlmostEqual(float(got), float(want.replace(",", ".")), places=6)
                elif want:
                    self.assertEqual(typ, "text", (programm, spalte, want))
                    self.assertEqual(got, want, (programm, r, spalte))  # auch 12.000 Zeichen ungekuerzt
        namen = [row[0][0] for row in ist]
        self.assertTrue(any("umlaute_\u00c4\u00f6\u00fc_\u00df.skp" in n for n in namen), namen)

    @unittest.skipUnless(os.name == "nt" and Path(EXCEL).is_file() and not OFFICE_AUS, "Excel nicht installiert")
    def test_excel(self):
        ziel, soll = self.boeser_bericht()
        ps1 = self.tmp / "excel.ps1"
        ps1.write_text(EXCEL_PS1, encoding="utf-8-sig")
        js = self.tmp / "excel.json"
        vorher = self.excel_pids()
        p = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1),
                            str(ziel), str(js)], capture_output=True, text=True, timeout=300)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.excel_pids() - vorher, set(), "Excel-Prozess des Tests laeuft noch")
        doc = json.loads(js.read_text(encoding="utf-8-sig"))
        self.assertEqual(doc["formeln"], "False")
        self.vergleiche(soll, [[tuple(z) for z in row] for row in doc["zellen"]], "Excel")

    @staticmethod
    def excel_pids():
        # Bytes statt text=True: ohne laufendes Excel meldet tasklist deutschen Text in der OEM-Codepage
        # ("ausgefuehrt" mit u-Umlaut), den cp1252 nicht dekodiert; stdout waere dann None
        tasklist = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tasklist.exe"
        p = subprocess.run([str(tasklist), "/FI", "IMAGENAME eq EXCEL.EXE", "/FO", "CSV", "/NH"],
                           capture_output=True)
        text = p.stdout.decode("ascii", "replace")
        return {line.split('","')[1] for line in text.splitlines() if line.startswith('"EXCEL')}

    @unittest.skipUnless(SOFFICE and not OFFICE_AUS, "LibreOffice nicht installiert")
    def test_libreoffice(self):
        # Filteroptionen: Trenner, Textbegrenzer ", UTF-8 (76), ab Zeile 1, Sprache 1031 (de) bzw. 1033 (en)
        for schalter, optionen in (("--csv", "59,34,76,1,,1031"), ("--csv-international", "44,34,76,1,,1033")):
            ziel, soll = self.boeser_bericht(schalter)
            aus = self.tmp / f"lo{len(optionen)}"
            profil = (self.tmp / "lo-profil").as_uri()
            p = subprocess.Popen([SOFFICE, f"-env:UserInstallation={profil}", "--headless", "--norestore",
                                  f"--infilter=Text - txt - csv (StarCalc):{optionen}", "--convert-to", "xlsx",
                                  "--outdir", str(aus), str(ziel)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                p.communicate(timeout=300)
            finally:
                if p.poll() is None:
                    p.kill()
            ist, formeln = lies_xlsx(aus / "bericht.xlsx")
            self.assertEqual(formeln, 0)
            self.vergleiche(soll, ist, f"LibreOffice {schalter}")

    @unittest.skipUnless(EDGE, "Edge nicht installiert")
    def test_html_in_edge(self):
        """Der Browser baut aus fremden Namen kein Element: kein Skript, kein Bild, keine Ressource."""
        f = self.build("xss.skp", materials=[('<img src=x onerror="alert(2)">', [5, 5, 5, 255], {})],
                       layers=[("<script>alert(1)</script>", True)])
        ziel = self.tmp / "bericht.html"
        self.assertEqual(run_cli("report", str(f), "-o", str(ziel), "-q")[0], 0)
        # Ausgabe in eine Datei statt in eine Pipe: Edge-Kindprozesse halten eine Pipe sonst offen
        dump = self.tmp / "dom.html"
        with open(dump, "wb") as fh:
            p = subprocess.Popen([EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
                                  f"--user-data-dir={self.tmp / 'edge'}", "--dump-dom", ziel.as_uri()],
                                 stdout=fh, stderr=subprocess.DEVNULL)
            try:
                p.wait(timeout=120)
            finally:
                if p.poll() is None:
                    subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
        dom = dump.read_text(encoding="utf-8", errors="replace")
        self.assertIn("skptool Bericht", dom)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", dom)
        self.assertNotRegex(dom.lower(), r"<(script|img|iframe|object|embed|link|base)\b")


if __name__ == "__main__":
    unittest.main()
