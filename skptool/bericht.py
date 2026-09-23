"""Sammelbericht ueber viele SketchUp-Dateien: skptool report "projekte\\*.skp".

Je Datei die Daten aus core.info() plus eine Analyse (platzierte Flaechen, Materialien,
Texturen, was beim Umwandeln verloren geht) und Warnungen, die sich aus den Grenzen im README
ableiten. Eine Datei, die nicht lesbar ist, wird eine Fehlerzeile, der Lauf geht weiter.

Ausgabe als Text (Standard), JSON, CSV (Excel, deutsch) oder HTML (eine Datei ohne Skripte und
ohne externe Verweise). Namen stammen aus fremden Dateien und werden je Format entschaerft:
Steuerzeichen maskiert, CSV gegen Formeln, HTML komplett escaped.

Die Anbindung an die Kommandozeile ist add_report_parser(), eingebunden in cli.build_parser().
"""
from __future__ import annotations

import collections
import csv
import gc
import glob
import html
import io
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from skptool import __version__, cli, core
from skptool.gltf_writer import MAX_TEXTURE_SIDE

GROSSE_DATEI = 50 * 2**20  # ab hier braucht das Einlesen viel Arbeitsspeicher (README "Grenzen")
LISTE_MAX = 50  # Namen je Zelle bzw. Liste, der Rest wird gezaehlt
DYNAMISCH = "dynamic_attributes"


# ---------------------------------------------------------------- Dateien sammeln

def dateien(inputs: list[str], rekursiv: bool = False) -> list[Path]:
    """Muster aufloesen wie cli._expand, mit --rekursiv auch "**". Ordner fallen weg, doppelt
    genannte Dateien zaehlen einmal."""
    paths: list[Path] = []
    for item in inputs:
        if any(c in item for c in "*?["):
            hits = sorted(glob.glob(item, recursive=rekursiv))
            hits = [h for h in hits if not os.path.isdir(h)]
            if not hits:
                raise SystemExit(f"Keine Datei passt zu: {item}")
        else:
            if os.path.isdir(item):
                raise SystemExit(f"{item} ist ein Ordner. Muster angeben, z. B. \"{Path(item) / '*.skp'}\", "
                                 f"mit Unterordnern: \"{Path(item) / '**' / '*.skp'}\" --rekursiv")
            hits = [item]
        paths += [Path(h) for h in hits]
    seen, result = set(), []
    for p in paths:
        if not p.exists():
            raise SystemExit(f"Datei nicht gefunden: {p}")
        key = os.path.normcase(str(p.resolve()))
        if key not in seen:
            seen.add(key)
            result.append(p)
    return result


# ---------------------------------------------------------------- Analyse

def bildgroesse(data: bytes):
    """(Breite, Hoehe) nur aus dem Bildkopf, ohne zu dekodieren (wie gltf_writer._image_ok), oder None.

    Pillows Bombenpruefung ist dabei aus: sie wuerde bei riesigen Bildern abbrechen, und genau
    deren Groesse soll der Bericht nennen. Dekodiert wird nichts, also auch nichts entpackt."""
    from PIL import Image

    alt = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = None
    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
        return (int(w), int(h)) if w > 0 and h > 0 else None
    except Exception:
        return None
    finally:
        Image.MAX_IMAGE_PIXELS = alt


def _platzierungen(model) -> int:
    """Alle Platzierungen wie im Modell sichtbar, verschachtelte mitgezaehlt (wie placed_face_count)."""
    memo: dict = {}

    def count(defn, depth=0):
        key = id(defn)
        if key in memo:
            return memo[key]
        total = 0
        if depth < 64:
            for inst in defn.instances:
                ref = model.definitions.get(inst.ref_idx)
                if ref is not None and not ref.is_image:
                    total += 1 + count(ref, depth + 1)
        memo[key] = total
        return total

    return count(model.root)


def analysiere(model) -> dict:
    """Kennzahlen eines geparsten Modells, die core.info() nicht liefert."""
    alle = [model.root, *model.definitions.values()]
    instanzen = [inst for d in alle for inst in d.instances]
    bilder = sum(1 for inst in instanzen
                 if (ref := model.definitions.get(inst.ref_idx)) is not None and ref.is_image)
    dynamisch = [inst.name or getattr(model.definitions.get(inst.ref_idx), "name", "") or "?"
                 for inst in instanzen if DYNAMISCH in (inst.attribute_dictionaries or {})]

    mats = list(model.materials)
    getoent = [mt.name for mt in mats if mt.texture is not None and mt.colorized]
    transparent = [mt.name for mt in mats if mt.transparency is not None and mt.transparency < 0.999]

    texturen = {"anzahl": 0, "bytes": 0, "groesste_kante_px": None, "unlesbar": [], "zu_gross": []}
    for mt in mats:
        data = mt.texture.data if mt.texture is not None else None
        if not data:
            continue
        texturen["anzahl"] += 1
        texturen["bytes"] += len(data)
        size = bildgroesse(data)
        if size is None:
            texturen["unlesbar"].append(mt.name)
            continue
        kante = max(size)
        if texturen["groesste_kante_px"] is None or kante > texturen["groesste_kante_px"]:
            texturen["groesste_kante_px"] = kante
        if kante > MAX_TEXTURE_SIDE:
            texturen["zu_gross"].append(f"\"{mt.name}\" ({size[0]} x {size[1]} px)")

    # Die Szene von OpenSKP fasst Materialien mit gleicher Farbe, gleicher Deckkraft und ohne
    # Textur zusammen (Schluessel in openskp/scene.py). Nur Materialien mit ID sind benutzbar.
    gruppen: dict = collections.defaultdict(list)
    for mt in mats:
        if mt.id is None or mt.texture is not None or not mt.color:
            continue
        deck = round(mt.transparency if mt.transparency is not None else 1.0, 3)
        gruppen[(tuple(mt.color[:3]), deck)].append(mt.name)
    gleiche_farben = [{"farbe": "#%02x%02x%02x" % rgb, "materialien": names}
                      for (rgb, _), names in gruppen.items() if len(names) > 1]

    # Dateien vor 2021 fuehren Bemassungen der obersten Ebene doppelt (Modell und Wurzel)
    bemassungen = max(len(model.dimensions or []), len(model.root.dimensions)) + \
        sum(len(d.dimensions) for d in model.definitions.values())
    return {
        "platzierte_flaechen": core.placed_face_count(model),
        "definitionen": sum(1 for d in model.definitions.values() if not d.is_image),
        "platzierungen": _platzierungen(model),
        "bilder": bilder,
        "ausgeblendete_ebenen": [l.name for l in model.layers if l.hidden],
        "materialien": {"gesamt": len(mats), "texturiert": sum(1 for mt in mats if mt.texture is not None),
                        "getoent": len(getoent), "transparent": len(transparent)},
        "getoente_materialien": getoent,
        "texturen": texturen,
        "szenen": len(model.pages or []),
        "bemassungen": bemassungen,
        "texte": sum(len(d.texts) for d in alle),
        "schnittebenen": sum(len(d.section_planes) for d in alle),
        "dynamische_komponenten": dynamisch,
        "gleiche_farben": gleiche_farben,
    }


def _hauptversion(version) -> int | None:
    text = str(version or "").strip("{}")
    head = text.split(".")[0]
    return int(head) if head.isdigit() else None


def _namen(items, n=5, zitat=True) -> str:
    """Die ersten Namen fuer eine Warnung, in Anfuehrungszeichen, damit Leerzeichen sichtbar sind."""
    items = list(items)
    text = ", ".join(f'"{x}"' if zitat else str(x) for x in items[:n])
    return text + (f" und {len(items) - n} weitere" if len(items) > n else "")


def _mehrzahl(n: int, eins: str, viele: str) -> str:
    return f"{n} {eins if n == 1 else viele}"


def warnungen_version(version) -> list[str]:
    major = _hauptversion(version)
    if major == 19:
        return ["Datei aus SketchUp 2019: OpenSKP scheitert laut Projekt an manchen Dateien dieser Version, "
                "Ergebnis genau pruefen"]
    if major is not None and major < 13:
        return [f"Sehr alte Version (SketchUp {major}): nicht jede Datei ist lesbar, "
                "Version 7 und aelter liest erst OpenSKP 1.3.0"]
    return []


def warnungen(version, groesse_bytes: int, a: dict) -> list[str]:
    """Deutsche Hinweise, jeder aus einer Grenze im README abgeleitet."""
    out = warnungen_version(version)
    if groesse_bytes > GROSSE_DATEI:
        out.append(f"Datei ist {groesse_bytes / 2**20:.0f} MB gross: das Einlesen braucht viel Arbeitsspeicher "
                   "(bei 200 MB gut 12 GB)")
    if a["getoente_materialien"]:
        n = len(a["getoente_materialien"])
        out.append(f"{_mehrzahl(n, 'getoente Textur', 'getoente Texturen')} (Colorize) "
                   f"{'verliert' if n == 1 else 'verlieren'} beim Umschreiben die Toenung: "
                   f"{_namen(a['getoente_materialien'])}")
    fehlt = [text for n, text in ((a["szenen"], _mehrzahl(a["szenen"], "Szene", "Szenen")),
                                  (a["bemassungen"], _mehrzahl(a["bemassungen"], "Bemassung", "Bemassungen")),
                                  (a["texte"], _mehrzahl(a["texte"], "Text", "Texte")),
                                  (a["schnittebenen"], _mehrzahl(a["schnittebenen"], "Schnittebene",
                                                                 "Schnittebenen"))) if n]
    if fehlt:
        out.append(f"Werden nicht uebertragen: {', '.join(fehlt)}")
    if a["dynamische_komponenten"]:
        n = len(a["dynamische_komponenten"])
        out.append(f"{_mehrzahl(n, 'dynamische Komponente', 'dynamische Komponenten')} ({DYNAMISCH}): "
                   f"das dynamische Verhalten wird nicht uebertragen: {_namen(a['dynamische_komponenten'])}")
    for g in a["gleiche_farben"]:
        out.append(f"Materialien gleicher Farbe {g['farbe']} koennen verwechselt werden: {_namen(g['materialien'])}")
    if a["texturen"]["zu_gross"]:
        n = len(a["texturen"]["zu_gross"])
        out.append(f"{_mehrzahl(n, 'Textur', 'Texturen')} mit mehr als {MAX_TEXTURE_SIDE} px Kantenlaenge "
                   f"{'wird' if n == 1 else 'werden'} beim Export durch einen Platzhalter ersetzt: "
                   f"{_namen(a['texturen']['zu_gross'], zitat=False)}")
    return out


def pruefe_datei(pfad: Path, bounds: bool = False, verbose: bool = False) -> dict:
    """Ein Eintrag des Berichts. Wirft nie, ein Fehler steht dann in "fehler"."""
    t0 = time.perf_counter()
    eintrag: dict = {"datei": str(pfad), "name": pfad.name, "ok": False, "fehler": None}
    try:
        eintrag["groesse_bytes"] = pfad.stat().st_size
    except OSError:
        eintrag["groesse_bytes"] = None
    skp = model = None
    try:
        t = time.perf_counter()
        skp = core.open_skp(pfad)
        model = core.model_of(skp)  # einmal einlesen, info() und die Analyse nutzen dasselbe Objekt
        sekunden = time.perf_counter() - t
        daten = core.info(pfad, with_bounds=bounds, skp=skp)
        analyse = analysiere(model)
        eintrag.update(ok=True, version=daten["version"], format=daten["format"],
                       einlesezeit_s=round(sekunden, 2), info=daten, analyse=analyse,
                       warnungen=warnungen(daten["version"], daten["size_bytes"], analyse))
    except Exception as exc:  # noqa: BLE001 - jede kaputte Datei wird eine Zeile, der Lauf geht weiter
        eintrag["fehler"] = cli._explain(pfad, exc, verbose)
        try:
            eintrag["version"] = core.header_version(pfad)
        except (OSError, ValueError):
            eintrag["version"] = None
        eintrag["warnungen"] = warnungen_version(eintrag["version"])
    finally:
        skp = model = None  # Modell freigeben, bevor die naechste Datei kommt
        gc.collect()
    eintrag["zeit_s"] = round(time.perf_counter() - t0, 2)
    return eintrag


def erstelle(pfade: list[Path], bounds: bool = False, verbose: bool = False, fortschritt=None) -> list[dict]:
    eintraege = []
    for i, p in enumerate(pfade, 1):
        if fortschritt:
            fortschritt(f"[{i}/{len(pfade)}] {p.name}")
        eintraege.append(pruefe_datei(p, bounds=bounds, verbose=verbose))
    return eintraege


# ---------------------------------------------------------------- Ausgabeformate

def _sicher(value) -> str:
    """Steuer- und Bidi-Zeichen aus fremden Namen sichtbar machen (wie die Terminalausgabe)."""
    return str(value).translate(cli._UNSAFE)


def _kb(n) -> str:
    if n is None:
        return "?"
    if n >= 2**20:
        return f"{n / 2**20:.1f} MB"
    return f"{n / 1024:.0f} KB" if n >= 1024 else f"{n} Byte"


def _abmessungen(info: dict) -> str | None:
    s = info.get("size_m")
    return f"{s['width']} x {s['depth']} x {s['height']} m" if s else None


def _komponenten(a: dict) -> str:
    text = (f"{_mehrzahl(a['definitionen'], 'Definition', 'Definitionen')}, "
            f"{_mehrzahl(a['platzierungen'], 'Platzierung', 'Platzierungen')}")
    return text + (f", {_mehrzahl(a['bilder'], 'Bild', 'Bilder')}" if a["bilder"] else "")


def _materialien(a: dict) -> str:
    m = a["materialien"]
    return (f"{m['gesamt']}, davon {m['texturiert']} mit Textur, {m['getoent']} getoent, "
            f"{m['transparent']} transparent")


def _texturen(a: dict) -> str:
    t = a["texturen"]
    text = f"{t['anzahl']}"
    if t["anzahl"]:
        text += f" ({_kb(t['bytes'])})"
        if t["groesste_kante_px"]:
            text += f", groesste Kante {t['groesste_kante_px']} px"
        if t["unlesbar"]:
            text += f", {len(t['unlesbar'])} mit unlesbarem Bildkopf"
    return text


def _weiteres(a: dict) -> str:
    return (f"{_mehrzahl(a['szenen'], 'Szene', 'Szenen')}, {_mehrzahl(a['bemassungen'], 'Bemassung', 'Bemassungen')}, "
            f"{_mehrzahl(a['texte'], 'Text', 'Texte')}, "
            f"{_mehrzahl(a['schnittebenen'], 'Schnittebene', 'Schnittebenen')}")


def _zusammenfassung(eintraege: list[dict], sekunden: float) -> str:
    ok = sum(1 for e in eintraege if e["ok"])
    warn = sum(len(e.get("warnungen") or []) for e in eintraege)
    return (f"Zusammenfassung: {_mehrzahl(len(eintraege), 'Datei', 'Dateien')}, {ok} gelesen, "
            f"{len(eintraege) - ok} mit Fehler, {_mehrzahl(warn, 'Warnung', 'Warnungen')}, {sekunden:.1f} s")


def format_text(eintraege: list[dict], sekunden: float) -> str:
    z: list[str] = []
    for e in eintraege:
        z.append(e["name"])
        z.append(f"  Datei:          {e['datei']} ({_kb(e.get('groesse_bytes'))})")
        if not e["ok"]:
            if e.get("version"):
                z.append(f"  Version:        {e['version']}")
            z.append(f"  FEHLER:         {e['fehler']}")
        else:
            info, a = e["info"], e["analyse"]
            z.append(f"  Version:        {e['version']} -> {e['format']}, eingelesen in {e['einlesezeit_s']:.2f} s")
            z.append(f"  Flaechen:       {info['faces_total']} gespeichert, {a['platzierte_flaechen']} platziert")
            z.append(f"  Komponenten:    {_komponenten(a)}")
            hidden = ", ".join(a["ausgeblendete_ebenen"]) or "-"
            z.append(f"  Ebenen:         {len(info['layers'])}, ausgeblendet: {hidden}")
            z.append(f"  Materialien:    {_materialien(a)}")
            z.append(f"  Texturen:       {_texturen(a)}")
            z.append(f"  Weiteres:       {_weiteres(a)}")
            if _abmessungen(info):
                z.append(f"  Abmessungen:    {_abmessungen(info)} (B x T x H), {info.get('triangles', 0)} Dreiecke")
        if e.get("warnungen"):
            z.append("  Warnungen:")
            z += [f"    - {w}" for w in e["warnungen"]]
        elif e["ok"]:
            z.append("  Warnungen:      keine")
        z.append("")
    z.append(_zusammenfassung(eintraege, sekunden))
    return _sicher("\n".join(z) + "\n")


def format_json(eintraege: list[dict]) -> str:
    # ensure_ascii: reine ASCII-Ausgabe, damit die Maskierung der Terminalausgabe das JSON nie
    # veraendert (sie wuerde rohe Steuerzeichen durch "\x.." ersetzen, das ist kein gueltiges JSON)
    return json.dumps({"skptool": __version__, "dateien": eintraege}, indent=2, ensure_ascii=True) + "\n"


# Zellen mit diesem Anfang wertet Excel/LibreOffice als Formel (auch als Vollbreiten-Zeichen)
_FORMEL_START = ("=", "+", "-", "@", "\t", "\r", "\uff1d", "\uff0b", "\uff0d", "\uff20")

CSV_SPALTEN = ["datei", "status", "fehler", "version", "format", "groesse_bytes", "einlesezeit_s", "flaechen",
               "platzierte_flaechen", "definitionen", "platzierungen", "bilder", "ebenen", "ausgeblendete_ebenen",
               "materialien", "texturiert", "getoent", "transparent", "texturen", "textur_bytes",
               "groesste_textur_px", "szenen", "bemassungen", "texte", "schnittebenen", "dynamische_komponenten",
               "abmessungen_m", "warnungen", "ebenen_namen", "materialien_namen"]


def _zelle(value):
    """Wert fuer eine CSV-Zelle: Zahlen bleiben Zahlen, Text wird gegen Formeln entschaerft."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "ja" if value else "nein"
    if isinstance(value, float):
        return f"{value:.2f}".replace(".", ",")  # deutsches Excel
    if isinstance(value, int):
        return value
    text = _sicher(value)
    if text.lstrip(" ").startswith(_FORMEL_START):
        text = "'" + text
    return text


def _liste(names) -> str:
    names = list(names)
    return ", ".join(names[:LISTE_MAX]) + (f" ... ({len(names) - LISTE_MAX} weitere)" if len(names) > LISTE_MAX else "")


def _csv_zeile(e: dict) -> dict:
    row = {"datei": e["datei"], "status": "ok" if e["ok"] else "Fehler", "fehler": e["fehler"],
           "version": e.get("version"), "groesse_bytes": e.get("groesse_bytes"),
           "warnungen": " | ".join(e.get("warnungen") or [])}
    if e["ok"]:
        info, a = e["info"], e["analyse"]
        m, t = a["materialien"], a["texturen"]
        row.update(format=e["format"], einlesezeit_s=float(e["einlesezeit_s"]), flaechen=info["faces_total"],
                   platzierte_flaechen=a["platzierte_flaechen"], definitionen=a["definitionen"],
                   platzierungen=a["platzierungen"], bilder=a["bilder"], ebenen=len(info["layers"]),
                   ausgeblendete_ebenen=_liste(a["ausgeblendete_ebenen"]), materialien=m["gesamt"],
                   texturiert=m["texturiert"], getoent=m["getoent"], transparent=m["transparent"],
                   texturen=t["anzahl"], textur_bytes=t["bytes"], groesste_textur_px=t["groesste_kante_px"],
                   szenen=a["szenen"], bemassungen=a["bemassungen"], texte=a["texte"],
                   schnittebenen=a["schnittebenen"], dynamische_komponenten=len(a["dynamische_komponenten"]),
                   abmessungen_m=_abmessungen(info), ebenen_namen=_liste(l["name"] for l in info["layers"]),
                   materialien_namen=_liste(mt["name"] for mt in info["materials"]))
    return {k: _zelle(row.get(k)) for k in CSV_SPALTEN}


def format_csv(eintraege: list[dict], zeilenende: str = "\r\n") -> str:
    """CSV fuer ein deutsches Excel: Semikolon, Dezimalkomma. Das BOM setzt die Ausgabe davor.
    Fuer die Konsole zeilenende="\\n": die Maskierung dort wuerde ein CR sichtbar machen."""
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=CSV_SPALTEN, delimiter=";", lineterminator=zeilenende)
    w.writeheader()
    for e in eintraege:
        w.writerow(_csv_zeile(e))
    return buf.getvalue()


_CSS = """
:root{--bg:#fbfbfa;--fg:#1d1d1b;--muted:#6b6b66;--line:#e2e1dc;--card:#ffffff;--ok:#1f7a45;--warn:#9a5b00;
--err:#b3261e;--warnbg:#fff4e0;--errbg:#fdecea}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe6;--muted:#a3a29b;--line:#34332f;--card:#1f1f1d;
--ok:#6fcf97;--warn:#f2b35b;--err:#ff8a80;--warnbg:#2d2413;--errbg:#361b19}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:1.5rem;margin:0 0 4px}
h2{font-size:1.1rem;margin:0 0 8px;overflow-wrap:anywhere}
.meta{color:var(--muted);margin:0 0 24px}
.scroll{overflow-x:auto;margin-bottom:32px;border:1px solid var(--line);border-radius:8px;background:var(--card)}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-weight:600;color:var(--muted);font-size:.85rem;white-space:nowrap}
td.n,th.n{text-align:right}
tr:last-child td{border-bottom:0}
section{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:16px;margin-bottom:16px}
dl{display:grid;grid-template-columns:max-content 1fr;gap:2px 16px;margin:0 0 8px}
dt{color:var(--muted)}
dd{margin:0;overflow-wrap:anywhere}
.ok{color:var(--ok);font-weight:600}.err{color:var(--err);font-weight:600}
.fehler{background:var(--errbg);color:var(--err);padding:8px 12px;border-radius:6px}
ul.warn{background:var(--warnbg);color:var(--warn);padding:8px 12px 8px 28px;border-radius:6px;margin:8px 0 0}
details{margin-top:8px}summary{cursor:pointer;color:var(--muted)}
@media (max-width:600px){dl{grid-template-columns:1fr}dt{margin-top:6px}}
"""


def _h(value) -> str:
    """Jeder Wert im HTML geht hier durch."""
    return html.escape(_sicher("" if value is None else value), quote=True)


def _html_liste(names, leer="-") -> str:
    names = list(names)
    if not names:
        return _h(leer)
    text = ", ".join(_h(n) for n in names[:200])
    return text + (_h(f" ... ({len(names) - 200} weitere)") if len(names) > 200 else "")


def format_html(eintraege: list[dict], sekunden: float) -> str:
    o: list[str] = []
    o.append("<!DOCTYPE html>\n<html lang=\"de\">\n<head>\n<meta charset=\"utf-8\">\n"
             "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">\n"
             "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
             "<meta name=\"referrer\" content=\"no-referrer\">\n"
             f"<title>skptool Bericht</title>\n<style>{_CSS}</style>\n</head>\n<body>\n<main>\n")
    o.append("<h1>skptool Bericht</h1>\n")
    o.append(f"<p class=\"meta\">{_h(_zusammenfassung(eintraege, sekunden))}<br>skptool {_h(__version__)}, "
             f"erstellt {_h(datetime.now().strftime('%d.%m.%Y %H:%M'))}</p>\n")
    o.append("<div class=\"scroll\"><table>\n<thead><tr><th>Datei</th><th>Status</th><th>Version</th>"
             "<th class=\"n\">Flaechen platziert</th><th class=\"n\">Definitionen</th><th class=\"n\">Materialien</th>"
             "<th class=\"n\">Texturen</th><th class=\"n\">Warnungen</th><th class=\"n\">Zeit</th></tr></thead>\n<tbody>\n")
    for e in eintraege:
        a = e.get("analyse") or {}
        status = "<span class=\"ok\">ok</span>" if e["ok"] else "<span class=\"err\">Fehler</span>"
        o.append(f"<tr><td>{_h(e['name'])}</td><td>{status}</td><td>{_h(e.get('version') or '-')}</td>"
                 f"<td class=\"n\">{_h(a.get('platzierte_flaechen', '-'))}</td>"
                 f"<td class=\"n\">{_h(a.get('definitionen', '-'))}</td>"
                 f"<td class=\"n\">{_h(a['materialien']['gesamt'] if a else '-')}</td>"
                 f"<td class=\"n\">{_h(a['texturen']['anzahl'] if a else '-')}</td>"
                 f"<td class=\"n\">{_h(len(e.get('warnungen') or []))}</td>"
                 f"<td class=\"n\">{_h(format(e.get('zeit_s', 0), '.1f') + ' s')}</td></tr>\n")
    o.append("</tbody></table></div>\n")
    for e in eintraege:
        o.append(f"<section>\n<h2>{_h(e['name'])}</h2>\n<dl>\n")
        rows = [("Datei", _h(e["datei"])), ("Groesse", _h(_kb(e.get("groesse_bytes"))))]
        if e.get("version"):
            rows.append(("Version", _h(e["version"] + (f" ({e['format']})" if e.get("format") else ""))))
        if e["ok"]:
            info, a = e["info"], e["analyse"]
            rows += [
                ("Eingelesen in", _h(f"{e['einlesezeit_s']:.2f} s")),
                ("Flaechen", _h(f"{info['faces_total']} gespeichert, {a['platzierte_flaechen']} platziert")),
                ("Komponenten", _h(_komponenten(a))),
                ("Ebenen", _html_liste(f"{l['name']}" + (" (ausgeblendet)" if l["hidden"] else "")
                                       for l in info["layers"])),
                ("Materialien", _h(_materialien(a))),
                ("Texturen", _h(_texturen(a))),
                ("Weiteres", _h(_weiteres(a))),
            ]
            if _abmessungen(info):
                rows.append(("Abmessungen", _h(f"{_abmessungen(info)} (B x T x H)")))
        o += [f"<dt>{k}</dt><dd>{v}</dd>\n" for k, v in rows]
        o.append("</dl>\n")
        if not e["ok"]:
            o.append(f"<p class=\"fehler\">Fehler: {_h(e['fehler'])}</p>\n")
        if e.get("warnungen"):
            o.append("<ul class=\"warn\">\n" + "".join(f"<li>{_h(w)}</li>\n" for w in e["warnungen"]) + "</ul>\n")
        if e["ok"] and e["info"]["materials"]:
            o.append("<details><summary>Alle Materialien</summary>\n<p>"
                     + _html_liste(mt["name"] for mt in e["info"]["materials"]) + "</p></details>\n")
        o.append("</section>\n")
    o.append("</main>\n</body>\n</html>\n")
    return "".join(o)


# ---------------------------------------------------------------- Kommandozeile

_ENDUNGEN = {".json": "json", ".csv": "csv", ".html": "html", ".htm": "html"}


def schreibe_atomar(ziel: Path, text: str, encoding: str = "utf-8") -> None:
    """Erst eine Nachbardatei schreiben, dann per Umbenennen ersetzen: ein abgebrochener Lauf
    hinterlaesst nie einen halben Bericht."""
    ziel.parent.mkdir(parents=True, exist_ok=True)
    tmp = ziel.with_name(f".{ziel.name}.skptool-tmp")
    try:
        with open(tmp, "w", encoding=encoding, newline="") as fh:
            fh.write(text)
        os.replace(tmp, ziel)
    finally:
        if tmp.exists():
            tmp.unlink()


def cmd_report(a) -> int:
    pfade = dateien(a.inputs, rekursiv=a.rekursiv)
    ziel = Path(a.output) if a.output else None
    if ziel is not None:
        if ziel.is_dir():
            raise SystemExit(f"Ziel {ziel} ist ein Ordner, bitte eine Datei angeben. Nichts geschrieben.")
        clash = next((p for p in pfade if cli._same_file(ziel, p)), None)
        if clash is not None:
            raise SystemExit(f"Ziel {ziel} ist selbst eine Eingabe ({clash.name}). Nichts geschrieben.")
    fmt = "json" if a.json else "csv" if a.csv else "html" if a.html else \
        _ENDUNGEN.get(ziel.suffix.lower(), "text") if ziel is not None else "text"

    def fortschritt(msg):
        print(f"  {msg}", file=sys.stderr, flush=True)

    t0 = time.perf_counter()
    eintraege = erstelle(pfade, bounds=a.bounds, verbose=a.verbose, fortschritt=None if a.quiet else fortschritt)
    sekunden = time.perf_counter() - t0
    if fmt == "json":
        text = format_json(eintraege)
    elif fmt == "csv":
        text = format_csv(eintraege, zeilenende="\r\n" if ziel is not None else "\n")
    elif fmt == "html":
        text = format_html(eintraege, sekunden)
    else:
        text = format_text(eintraege, sekunden)
    if ziel is not None:
        schreibe_atomar(ziel, text, encoding="utf-8-sig" if fmt == "csv" else "utf-8")
        print(f"Bericht geschrieben: {ziel} ({_zusammenfassung(eintraege, sekunden)})")
    else:
        sys.stdout.write(("\ufeff" if fmt == "csv" else "") + text)
    return 0 if all(e["ok"] for e in eintraege) else 1


def add_report_parser(sub) -> None:
    """Unterbefehl "report" an den argparse-Subparser von skptool haengen."""
    p = sub.add_parser("report", help="Sammelbericht ueber viele .skp-Dateien",
                       description="Liest jede Datei ein und fasst zusammen: Version, Flaechen, Komponenten, "
                                   "Materialien, Texturen und Warnungen, was beim Umwandeln verloren geht. "
                                   "Nicht lesbare Dateien werden eine Fehlerzeile, der Lauf geht weiter "
                                   "(Rueckgabewert dann 1).")
    p.add_argument("inputs", nargs="+", help="Dateien oder Muster, z. B. \"projekte\\*.skp\"")
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="Ein JSON-Dokument fuer alle Dateien")
    fmt.add_argument("--csv", action="store_true", help="CSV fuer Excel (UTF-8 mit BOM, Semikolon)")
    fmt.add_argument("--html", action="store_true", help="Eine HTML-Datei ohne Skripte und externe Verweise")
    p.add_argument("-o", "--output", help="Bericht in diese Datei schreiben (ohne Schalter bestimmt die "
                                          "Endung .json/.csv/.html das Format)")
    p.add_argument("--bounds", action="store_true",
                   help="Abmessungen berechnen (dauert laenger, braucht bei grossen Dateien viel Speicher)")
    p.add_argument("--rekursiv", action="store_true", help="\"**\" im Muster durchsucht auch Unterordner")
    p.add_argument("-q", "--quiet", action="store_true", help="Keine Fortschrittsanzeige")
    p.add_argument("-v", "--verbose", action="store_true", help="Technische Details bei Fehlern")
    p.set_defaults(func=cmd_report)
