"""Zwei .skp-Dateien vergleichen: skptool diff a.skp b.skp.

Jede Datei wird einmal eingelesen und zu einem kleinen Schnappschuss aus reinen Daten verdichtet
(Ebenen, Materialien, Definitionen, Platzierungen, Baum). Danach wird das Modell freigegeben,
bevor die zweite Datei geladen wird: grosse Dateien brauchen beim Einlesen viel Speicher.

Zuordnung: Ebenen, Materialien und benannte Definitionen ueber den Namen. Gruppen-Definitionen
("Group#12") ueber ihren Inhalt (Flaechen und Namen der Kinder), weil sich ihre Nummern beim
Umschreiben aendern. Platzierungen ueber ihren Pfad ("ROOT / Tisch / Bein") als Multimenge, gepaart
ueber die Weltmatrix mit Toleranz.

Optional: platzierte Geometrie (--geometrie) und Texturlage (--texturen), beide ueber den
Szenenaufbau von OpenSKP. Die Funktionen dafuer nutzen auch tools/geometrievergleich.py und
tools/texturvergleich.py.

Rueckgabewerte wie bei GNU diff: 0 gleich, 1 Unterschiede, 2 Fehler.
"""
from __future__ import annotations

import array
import collections
import gc
import hashlib
import io
import json
import math
import sys
import warnings
from pathlib import Path

import numpy as np

from skptool import core

ABSCHNITTE = ("modell", "ebenen", "materialien", "definitionen", "platzierungen")
TITEL = {"modell": "Modell", "ebenen": "Ebenen", "materialien": "Materialien",
         "definitionen": "Definitionen", "platzierungen": "Platzierungen",
         "geometrie": "Geometrie", "texturen": "Texturen"}
LIMIT = 25  # Eintraege je Abschnitt ohne --all, wie bei info
_LIN_TOL = 1e-5  # Drehung/Skalierung: Rechenrauschen aus Blender (float32) gilt als gleich
_MAX_PAARE = 4_000_000  # groessere Restmengen je Pfad werden ueber die Sortierung gepaart


class VergleichsFehler(Exception):
    """Eine Datei konnte nicht gelesen werden (Rueckgabewert 2)."""

    def __init__(self, path: Path, exc: Exception):
        super().__init__(f"{path}: {exc}")
        self.path, self.exc = Path(path), exc


# ---------------------------------------------------------------- Geometrie (tools/geometrievergleich.py)

def _szenen_punkte(sc, tol_mm):
    pts, tris = [], 0
    for prim in sc.glb_primitives:
        p = np.frombuffer(prim.positions, np.float32).reshape(-1, 3).astype(np.float64)
        pts.append(np.rint(p * 1000.0 / tol_mm).astype(np.int64))
        tris += len(prim.indices) // 3
    return pts, tris


def _eindeutig(pts):
    return np.unique(np.concatenate(pts), axis=0) if pts else np.empty((0, 3), np.int64)


def geometrie_punkte(path, tol_mm):
    """Alle platzierten Eckpunkte (Weltkoordinaten, auf tol_mm gerastert, eindeutig) und Dreiecke."""
    sc = core.build_scene(core.open_skp(path))
    pts, tris = _szenen_punkte(sc, tol_mm)
    del sc
    gc.collect()
    return _eindeutig(pts), tris


def _als_void(a):
    a = np.ascontiguousarray(a)
    return a.view(np.dtype((np.void, a.dtype.itemsize * a.shape[1]))).ravel()


def _treffer_nah(src, dst):
    """Wie viele Rasterpunkte aus src in dst liegen, Nachbarzellen eingeschlossen (Rasterkanten)."""
    if not len(src) or not len(dst):
        return 0
    ziel = _als_void(dst)
    hit = np.zeros(len(src), bool)
    for i in (-1, 0, 1):
        for j in (-1, 0, 1):
            for k in (-1, 0, 1):
                rest = ~hit
                if not rest.any():
                    return len(src)
                hit[rest] = np.isin(_als_void(src[rest] + np.array([i, j, k], np.int64)), ziel)
    return int(hit.sum())


def punkte_vergleich(pa, ta, pb, tb):
    """Vergleich zweier Punktmengen aus geometrie_punkte (Schluessel wie tools/geometrievergleich)."""
    ha, hb = _treffer_nah(pa, pb), _treffer_nah(pb, pa)
    return {"punkte_a": len(pa), "punkte_b": len(pb), "dreiecke_a": ta, "dreiecke_b": tb,
            "a_in_b": ha / max(1, len(pa)), "b_in_a": hb / max(1, len(pb)),
            "treffer_a": ha, "treffer_b": hb}


def geometrie_vergleich(a, b, tol_mm=1.0):
    """Platzierte Geometrie zweier Dateien, eine nach der anderen eingelesen."""
    pa, ta = geometrie_punkte(a, tol_mm)
    pb, tb = geometrie_punkte(b, tol_mm)
    return punkte_vergleich(pa, ta, pb, tb)


# ---------------------------------------------------------------- Texturen (tools/texturvergleich.py)

def _szenen_texturzeilen(sc, with_names):
    keys, names = [], []
    for prim in sc.glb_primitives:
        pbr = sc.gltf_materials[prim.material_index]["pbrMetallicRoughness"]
        if "baseColorTexture" not in pbr or not len(prim.uvs):
            continue
        pos = np.rint(np.frombuffer(prim.positions, np.float32).reshape(-1, 3) * 1000).astype(np.int64)
        uv = np.asarray(prim.uvs, np.float64).reshape(-1, 2)
        fr = np.rint((uv % 1.0) * 100).astype(np.int64) % 100
        k = np.concatenate([pos, fr], axis=1)
        keys.append(k)
        if with_names:
            names += [sc.textures[pbr["baseColorTexture"]["index"]].filename.split("\\")[-1][:28]] * len(k)
    return keys, names


def _texturzeilen_fertig(keys, names):
    return (np.concatenate(keys) if keys else np.empty((0, 5), np.int64)), np.array(names)


def textur_zeilen(path, with_names=False):
    """Je texturierter Ecke: Position (mm) + UV modulo 1 (auf 1/100), dazu optional der Texturname."""
    sc = core.build_scene(core.open_skp(path))
    keys, names = _szenen_texturzeilen(sc, with_names)
    del sc
    gc.collect()
    return _texturzeilen_fertig(keys, names)


def textur_vergleich(ka, na, kb):
    """Anteil der Ecken aus ka, deren Position in kb vorkommt, und davon mit gleicher Texturlage."""
    posb = _als_void(np.ascontiguousarray(kb[:, :3]))
    hit_pos = np.isin(_als_void(np.ascontiguousarray(ka[:, :3])), posb)
    hit = np.isin(_als_void(ka), _als_void(kb))
    je = []
    if len(na):
        for name in np.unique(na[hit_pos]):
            sel = hit_pos & (na == name)
            je.append((str(name), float(hit[sel].mean()), int(sel.sum())))
    return {"ecken": len(ka), "hit_pos": hit_pos, "hit": hit, "je_textur": je}


# ---------------------------------------------------------------- Schnappschuss

def _ist_gruppe(name) -> bool:
    return not core._is_component_name(name)


def _bildgroesse(data):
    """Pixelgroesse nur aus dem Bildkopf (PIL dekodiert beim Oeffnen nichts)."""
    try:
        from PIL import Image
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with Image.open(io.BytesIO(data)) as im:
                return [int(im.size[0]), int(im.size[1])]
    except Exception:
        return None


def _material(mt) -> dict:
    tex = mt.texture
    data = tex.data if tex is not None else None
    # Verglichen wird nur RGB: das Alpha der Materialfarbe ist bei Texturen ein Formatkennzeichen
    # (OpenSKP schreibt 254), die echte Durchsichtigkeit steht in der Deckkraft
    return {"name": mt.name,
            "rgba": list(mt.color) if mt.color else None,
            "farbe": list(mt.color[:3]) if mt.color else None,
            "eingefaerbt": bool(mt.colorized),
            "deckkraft": round(float(mt.transparency), 3) if mt.transparency is not None else 1.0,
            "textur": tex is not None,
            "textur_sha256": hashlib.sha256(data).hexdigest() if data else None,
            "textur_pixel": _bildgroesse(data) if data else None}


def _kinder_text(namen, kurz=6):
    zahl = collections.Counter(namen)
    teile = [f"{n}x {k}" if n > 1 else k for k, n in sorted(zahl.items())]
    if len(teile) > kurz:
        teile = teile[:kurz] + [f"... {len(teile) - kurz} weitere"]
    return ", ".join(teile)


def baum_zeilen(model, max_depth=4, gruppen_neutral=False):
    """Verschachtelung als Textzeilen wie tools/baum.py. gruppen_neutral: Gruppen heissen "Gruppe"
    statt "Group#12", damit Baeume verschiedener Dateien vergleichbar sind."""
    lines = []

    def walk(defn, depth, label):
        lines.append(f"{'  ' * depth}{label}: {len(defn.faces)} Flaechen, {len(defn.instances)} Platzierungen")
        if depth >= max_depth:
            return
        groups = {}
        for inst in defn.instances:
            groups.setdefault(inst.ref_idx, []).append(inst)
        for ref, insts in groups.items():
            d = model.definitions.get(ref)
            if d is None:
                continue
            name = "Gruppe" if gruppen_neutral and _ist_gruppe(d.name) else d.name
            walk(d, depth + 1, f"{len(insts)}x {name!r}")

    walk(model.root, 0, "Modell")
    return lines


def _pfadteil(inst_name, def_name, ist_gruppe) -> str:
    """Name einer Platzierung im Pfad. Wie core._instance_info (eigener Name, sonst der der
    Definition), aber Gruppen heissen "Gruppe": ihre Nummern ("Group#12") aendern sich beim
    Umschreiben, und der Rueckweg aus Blender gibt unbenannten Gruppen den alten Definitionsnamen."""
    if inst_name and inst_name != def_name and not (ist_gruppe and _ist_gruppe(inst_name)):
        return inst_name
    return "Gruppe" if ist_gruppe else (def_name or "Gruppe")


def _struktur(m) -> dict:
    defs = m.definitions
    gruppe = {i: _ist_gruppe(d.name) for i, d in defs.items()}

    def kurzname(i):
        return "Gruppe" if gruppe[i] else defs[i].name

    # Flaechen wie platziert, rekursiv (wie core.placed_face_count, aber je Definition)
    memo: dict = {}

    def flaechen_von(d, depth=0):
        if id(d) in memo:
            return memo[id(d)]
        total = len(d.faces)
        if depth < 64:
            for inst in d.instances:
                ref = defs.get(inst.ref_idx)
                if ref is not None:
                    total += flaechen_von(ref, depth + 1)
        memo[id(d)] = total
        return total

    referenziert: collections.Counter = collections.Counter()
    for d in [m.root, *defs.values()]:
        for inst in d.instances:
            referenziert[inst.ref_idx] += 1

    definitionen: dict = {}
    for i, d in defs.items():
        kinder = [kurzname(inst.ref_idx) for inst in d.instances if inst.ref_idx in defs]
        if gruppe[i]:  # Signatur: Flaechen und Namen der Kinder, Schluessel ungekuerzt
            kopf = f"Gruppe ({len(d.faces)} Flaechen"
            schluessel = kopf + (f"; {_kinder_text(kinder, 10**9)}" if kinder else "") + ")"
            anzeige = kopf + (f"; {_kinder_text(kinder)}" if kinder else "") + ")"
        else:
            schluessel = anzeige = d.name
        edges = d.edges.values()
        definitionen.setdefault(schluessel, []).append({
            "name": anzeige, "original": d.name, "gruppe": gruppe[i],
            "flaechen": len(d.faces),
            "kanten_sichtbar": sum(1 for e in edges if not e.soft and not e.hidden),
            "kanten_weich": sum(1 for e in edges if e.soft),
            "rueckseiten": sum(1 for f in d.faces.values() if f.back_material_id is not None),
            "kinder": len(d.instances),
            "platziert": referenziert.get(i, 0),
            "flaechen_platziert": flaechen_von(d)})

    # Platzierungen, Pfad und Ebene wie core._instance_info, Gruppen heissen "Gruppe"
    from openskp import _core

    pfade: list = []
    attrs: list = []
    matrizen = array.array("d")  # 12 Werte je Platzierung, kompakt auch bei Millionen
    intern: dict = {}

    def walk(defn, matrix, path, layer, depth=0):
        if depth > 64:
            return
        for inst in defn.instances:
            ref = defs.get(inst.ref_idx)
            if ref is None:
                continue
            mm = _core.multiply_matrices(matrix, inst.matrix)
            teil = _pfadteil(inst.name, ref.name, gruppe[inst.ref_idx])
            p = f"{path} / {teil}"
            p = intern.setdefault(p, p)
            lay = inst.layer if inst.layer not in (None, "", "Layer0") else layer
            pfade.append(p)
            eig = (kurzname(inst.ref_idx), lay, bool(inst.hidden))
            attrs.append(intern.setdefault(eig, eig))  # gleiche Tupel teilen, spart Speicher
            w = mm[12] if len(mm) > 12 and mm[12] not in (0, None) else 1.0
            matrizen.extend([mm[0] / w, mm[1] / w, mm[2] / w, mm[3] / w, mm[4] / w, mm[5] / w,
                             mm[6] / w, mm[7] / w, mm[8] / w,
                             mm[9] * 25.4 / w, mm[10] * 25.4 / w, mm[11] * 25.4 / w])
            if len(pfade) > core.MAX_PLACEMENTS:
                raise core.UnsafeFileError(f"mehr als {core.MAX_PLACEMENTS} Platzierungen (verschachtelte "
                                           "Komponenten vervielfachen sich), anhebbar mit SKPTOOL_MAX_PLACEMENTS")
            walk(ref, mm, p, lay, depth + 1)

    walk(m.root, [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1.0], "ROOT", "Layer0")

    alle = [m.root, *defs.values()]
    bemassungen = len(m.dimensions or []) or sum(len(d.dimensions or []) for d in alle)
    return {
        "ebenen": [{"name": l.name, "ausgeblendet": bool(l.hidden)} for l in m.layers],
        # Materialien ohne ID kann keine Flaeche nutzen (z. B. "Layer_Layer0" von Render-Plugins),
        # das Umschreiben laesst sie deshalb weg
        "materialien": [_material(mt) for mt in m.materials if mt.id is not None],
        "definitionen": definitionen,
        "platzierungen": {"pfade": pfade, "attrs": attrs,
                          "matrizen": np.frombuffer(matrizen, np.float64).reshape(-1, 12).copy()},
        "modell": {"szenen": len(m.pages or []), "bemassungen": bemassungen,
                   "texte": sum(len(d.texts or []) for d in alle),
                   "schnittebenen": sum(len(d.section_planes or []) for d in alle),
                   "lose_flaechen": len(m.root.faces),
                   "flaechen_platziert": flaechen_von(m.root)},
        "baum": baum_zeilen(m, 4, gruppen_neutral=True),
    }


def schnappschuss(path, geometrie=False, texturen=False, toleranz_mm=0.1) -> dict:
    """Datei einlesen, zu reinen Daten verdichten und das Modell wieder freigeben.

    Mit geometrie/texturen wird zusaetzlich die Szene aufgebaut (braucht deutlich mehr Speicher).
    Fehler beim Lesen kommen als VergleichsFehler."""
    path = Path(path)
    try:
        if not path.is_file():
            raise FileNotFoundError("Datei nicht gefunden")
        version = core.header_version(path)
        skp = core.open_skp(path)
        snap = _struktur(core.model_of(skp))
    except Exception as exc:  # noqa: BLE001 - jede Lesefehler-Art wird Rueckgabewert 2
        raise VergleichsFehler(path, exc) from exc
    snap.update(datei=str(path), version=version, groesse_bytes=path.stat().st_size)
    skp._skptool_model = None  # Modell freigeben, die Szene braucht nur das Rohergebnis
    gc.collect()
    if geometrie or texturen:
        try:
            sc = core.build_scene(skp)
        except Exception as exc:  # noqa: BLE001
            raise VergleichsFehler(path, exc) from exc
        del skp
        gc.collect()
        if geometrie:
            pts, tris = _szenen_punkte(sc, toleranz_mm)
            snap["geometrie"] = {"punkte": pts, "dreiecke": tris}
        if texturen:
            snap["texturen"] = _szenen_texturzeilen(sc, True)
        del sc
        gc.collect()
        if geometrie:
            snap["geometrie"]["punkte"] = _eindeutig(snap["geometrie"]["punkte"])
        if texturen:
            snap["texturen"] = _texturzeilen_fertig(*snap["texturen"])
    else:
        del skp
        gc.collect()
    return snap


def zusammenfassung(snap: dict) -> dict:
    """Kleine Beschreibung eines Schnappschusses fuer die Ausgabe (JSON "a" und "b")."""
    out = {"datei": snap["datei"], "version": snap["version"], "groesse_bytes": snap["groesse_bytes"],
           "ebenen": len(snap["ebenen"]), "materialien": len(snap["materialien"]),
           "definitionen": sum(len(v) for v in snap["definitionen"].values()),
           "platzierungen": len(snap["platzierungen"]["pfade"]), **snap["modell"]}
    if "geometrie" in snap:
        out["punkte"] = len(snap["geometrie"]["punkte"])
        out["dreiecke"] = snap["geometrie"]["dreiecke"]
    if "texturen" in snap:
        out["texturierte_ecken"] = len(snap["texturen"][0])
    out["baum"] = snap["baum"]
    return out


# ---------------------------------------------------------------- Vergleich

def _abschnitt():
    return {"gleich": 0, "nur_in_a": 0, "nur_in_b": 0, "geaendert": 0, "eintraege": []}


def _eintrag(ab, art, name, text, **extra):
    ab[art] += 1
    ab["eintraege"].append({"art": art, "name": name, "text": text, **extra})


def _janein(v):
    return "ja" if v else "nein"


def _farbe(rgb):
    return "#" + "".join(f"{int(c):02x}" for c in rgb[:3]) if rgb else "-"


def _mm(v):
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


_FELDER = {
    "ebenen": [("ausgeblendet", "ausgeblendet", _janein)],
    "materialien": [("farbe", "Farbe", _farbe), ("deckkraft", "Deckkraft", str),
                    ("textur", "Textur", _janein), ("eingefaerbt", "Textur eingefaerbt", _janein),
                    ("textur_sha256", "Texturbild", lambda v: v[:12] if v else "-"),
                    ("textur_pixel", "Texturgroesse", lambda v: f"{v[0]} x {v[1]} px" if v else "-")],
    "definitionen": [("flaechen", "Flaechen", str), ("kanten_sichtbar", "sichtbare Kanten", str),
                     ("kanten_weich", "weiche Kanten", str), ("rueckseiten", "Flaechen mit Rueckseitenmaterial", str),
                     ("kinder", "enthaltene Platzierungen", str), ("platziert", "Platzierungen", str),
                     ("flaechen_platziert", "platzierte Flaechen", str)],
}


def _beschreibe(abschnitt, rec):
    if abschnitt == "ebenen":
        return "ausgeblendet" if rec["ausgeblendet"] else ""
    if abschnitt == "materialien":
        return _farbe(rec["farbe"]) + (f", Textur {rec['textur_pixel'][0]} x {rec['textur_pixel'][1]} px"
                                      if rec["textur_pixel"] else (", Textur" if rec["textur"] else ""))
    if abschnitt == "definitionen":
        return (f"{rec['flaechen']} Flaechen, {rec['kinder']} enthaltene Platzierungen, "
                f"{rec['platziert']}x platziert")
    return ""


def _vergleiche_liste(abschnitt, a: dict, b: dict):
    """a, b: Schluessel -> Liste von Datensaetzen. Gleiche Datensaetze zaehlen als gleich, der Rest
    wird der Reihe nach gepaart (geaendert), Ueberzaehlige sind nur in A oder nur in B."""
    felder = _FELDER[abschnitt]
    ab = _abschnitt()

    def werte(rec):
        return tuple(json.dumps(rec[f], sort_keys=True) for f, _, _ in felder)

    for key in sorted(set(a) | set(b)):
        la = sorted(((werte(r), r) for r in a.get(key, [])), key=lambda wr: wr[0])
        lb = sorted(((werte(r), r) for r in b.get(key, [])), key=lambda wr: wr[0])
        offen = collections.Counter(w for w, _ in lb)
        rest_a = []
        for w, rec in la:
            if offen[w] > 0:
                offen[w] -= 1
                ab["gleich"] += 1
            else:
                rest_a.append(rec)
        rest_b = []
        for w, rec in lb:
            if offen[w] > 0:
                offen[w] -= 1
                rest_b.append(rec)
        paare = _paare_aehnlichste(rest_a, rest_b, [f for f, _, _ in felder])
        for x, y in paare:
            ra, rb = rest_a[x], rest_b[y]
            teile = [f"{label} {fmt(ra[f])} -> {fmt(rb[f])}" for f, label, fmt in felder if ra[f] != rb[f]]
            _eintrag(ab, "geaendert", ra["name"], ", ".join(teile),
                     a={f: ra[f] for f, _, _ in felder}, b={f: rb[f] for f, _, _ in felder})
        gepaart_a, gepaart_b = {x for x, _ in paare}, {y for _, y in paare}
        for x, rec in enumerate(rest_a):
            if x not in gepaart_a:
                _eintrag(ab, "nur_in_a", rec["name"], _beschreibe(abschnitt, rec))
        for y, rec in enumerate(rest_b):
            if y not in gepaart_b:
                _eintrag(ab, "nur_in_b", rec["name"], _beschreibe(abschnitt, rec))
    return ab


def _paare_aehnlichste(ra, rb, felder):
    """Gleich benannte Reste paaren, die mit den wenigsten abweichenden Feldern zuerst (wichtig bei
    Gruppen mit gleicher Signatur). Sehr grosse Mengen einfach der Reihe nach."""
    n = min(len(ra), len(rb))
    if not n:
        return []
    if len(ra) * len(rb) > 250_000:
        return [(i, i) for i in range(n)]
    kosten = sorted((sum(a[f] != b[f] for f in felder), x, y)
                    for x, a in enumerate(ra) for y, b in enumerate(rb))
    paare, frei_a, frei_b = [], set(range(len(ra))), set(range(len(rb)))
    for _, x, y in kosten:
        if x in frei_a and y in frei_b:
            paare.append((x, y))
            frei_a.discard(x)
            frei_b.discard(y)
            if len(paare) == n:
                break
    return paare


def _nach_name(recs):
    out: dict = {}
    for r in recs:
        out.setdefault(r["name"], []).append(r)
    return out


def _vergleiche_modell(a, b):
    ab = _abschnitt()
    labels = {"szenen": "Szenen", "bemassungen": "Bemassungen", "texte": "Texte",
              "schnittebenen": "Schnittebenen", "lose_flaechen": "lose Flaechen (ohne Gruppe)",
              "flaechen_platziert": "platzierte Flaechen"}
    for k, label in labels.items():
        if a[k] == b[k]:
            ab["gleich"] += 1
        else:
            _eintrag(ab, "geaendert", label, f"{a[k]} -> {b[k]}", a=a[k], b=b[k])
    return ab


def _paare_nach_abstand(ma, mb, ia, ib):
    """Restliche Platzierungen eines Pfads paaren, naechste Position zuerst."""
    if not ia or not ib:
        return []
    if len(ia) * len(ib) > _MAX_PAARE:
        oa = sorted(ia, key=lambda i: tuple(ma[i, 9:12]))
        ob = sorted(ib, key=lambda i: tuple(mb[i, 9:12]))
        return list(zip(oa, ob))
    sa, sb = ma[ia], mb[ib]
    # spaltenweise, damit kein (n, m, 12)-Zwischenfeld entsteht
    quad = np.zeros((len(ia), len(ib)))
    lin = np.zeros((len(ia), len(ib)))
    for k in range(12):
        d = sa[:, k, None] - sb[None, :, k]
        if k >= 9:
            quad += d * d
        else:
            np.maximum(lin, np.abs(d), out=lin)
    kosten = np.sqrt(quad) + lin * 1e-3
    paare, frei_a, frei_b = [], set(range(len(ia))), set(range(len(ib)))
    for flat in np.argsort(kosten, axis=None, kind="stable"):
        x, y = divmod(int(flat), len(ib))
        if x in frei_a and y in frei_b:
            paare.append((ia[x], ib[y]))
            frei_a.discard(x)
            frei_b.discard(y)
            if not frei_a or not frei_b:
                break
    return paare


def _platzierung(p, i) -> dict:
    """Eine Platzierung fuer die JSON-Ausgabe: Weltmatrix 4x4 zeilenweise, Verschiebung in mm."""
    m = p["matrizen"][i].tolist()
    definition, ebene, ausgeblendet = p["attrs"][i]
    return {"definition": definition, "ebene": ebene, "ausgeblendet": ausgeblendet,
            "matrix": [m[0:3] + [m[9]], m[3:6] + [m[10]], m[6:9] + [m[11]], [0.0, 0.0, 0.0, 1.0]]}


def _vergleiche_platzierungen(pa, pb, tol_mm):
    ab = _abschnitt()
    ma, mb = pa["matrizen"], pb["matrizen"]
    ja: dict = {}
    jb: dict = {}
    for i, p in enumerate(pa["pfade"]):
        ja.setdefault(p, []).append(i)
    for i, p in enumerate(pb["pfade"]):
        jb.setdefault(p, []).append(i)

    def raster(m):
        zellen = np.floor(m[:, 9:12] / tol_mm).astype(np.int64).tolist()
        lin = (np.round(m[:, :9], 6) + 0.0).tolist()
        return [(tuple(z), tuple(l)) for z, l in zip(zellen, lin)]

    ra, rb = raster(ma), raster(mb)

    def gleich(i, j):
        return (pa["attrs"][i] == pb["attrs"][j]
                and float(np.linalg.norm(ma[i, 9:12] - mb[j, 9:12])) <= tol_mm
                and float(np.abs(ma[i, :9] - mb[j, :9]).max()) <= _LIN_TOL)

    def ort(m, i):
        return "(" + ", ".join(_mm(v) for v in m[i, 9:12]) + ") mm"

    for pfad in sorted(set(ja) | set(jb)):
        ia, ib = ja.get(pfad, []), jb.get(pfad, [])
        # 1. schnell: gleiche Rasterzelle und gleiche Eigenschaften
        eimer: dict = {}
        for j in ib:
            eimer.setdefault((pb["attrs"][j], rb[j]), []).append(j)
        rest_a = []
        for i in ia:
            kand = eimer.get((pa["attrs"][i], ra[i]))
            j = next((j for j in kand or [] if gleich(i, j)), None)
            if j is None:
                rest_a.append(i)
            else:
                kand.remove(j)
                ab["gleich"] += 1
        rest_b = [j for js in eimer.values() for j in js]
        # 2. innerhalb der Toleranz, aber ueber eine Rasterkante hinweg
        if rest_a and rest_b:
            gepaart_a, gepaart_b = set(), set()
            for i, j in _paare_nach_abstand(ma, mb, rest_a, rest_b):
                if gleich(i, j):
                    gepaart_a.add(i)
                    gepaart_b.add(j)
                    ab["gleich"] += 1
            rest_a = [i for i in rest_a if i not in gepaart_a]
            rest_b = [j for j in rest_b if j not in gepaart_b]
        # 3. geaendert: naechste Position zuerst
        gepaart_a, gepaart_b = set(), set()
        for i, j in _paare_nach_abstand(ma, mb, rest_a, rest_b):
            gepaart_a.add(i)
            gepaart_b.add(j)
            teile = []
            weg = float(np.linalg.norm(ma[i, 9:12] - mb[j, 9:12]))
            if weg > tol_mm:
                teile.append(f"verschoben um {_mm(weg)} mm")
            if float(np.abs(ma[i, :9] - mb[j, :9]).max()) > _LIN_TOL:
                teile.append("gedreht oder skaliert")
            (da, la, ha), (db, lb, hb) = pa["attrs"][i], pb["attrs"][j]
            if da != db:
                teile.append(f"Definition {da} -> {db}")
            if la != lb:
                teile.append(f"Ebene {la} -> {lb}")
            if ha != hb:
                teile.append(f"ausgeblendet {_janein(ha)} -> {_janein(hb)}")
            _eintrag(ab, "geaendert", pfad, ", ".join(teile), verschoben_mm=round(weg, 4),
                     a=_platzierung(pa, i), b=_platzierung(pb, j))
        for i in rest_a:
            if i not in gepaart_a:
                _eintrag(ab, "nur_in_a", pfad, f"{pa['attrs'][i][0]} bei {ort(ma, i)}")
        for j in rest_b:
            if j not in gepaart_b:
                _eintrag(ab, "nur_in_b", pfad, f"{pb['attrs'][j][0]} bei {ort(mb, j)}")
    return ab


def _vergleiche_geometrie(ga, gb, tol_mm):
    r = punkte_vergleich(ga["punkte"], ga["dreiecke"], gb["punkte"], gb["dreiecke"])
    ab = _abschnitt()
    ab["gleich"] = r["treffer_a"]
    fehlt_a, fehlt_b = r["punkte_a"] - r["treffer_a"], r["punkte_b"] - r["treffer_b"]
    if fehlt_a:
        ab["nur_in_a"] = fehlt_a
        ab["eintraege"].append({"art": "nur_in_a", "name": "Punkte",
                                "text": f"{fehlt_a} von {r['punkte_a']} platzierten Punkten ohne Gegenstueck "
                                        f"in B ({1 - r['a_in_b']:.4%}, +-{_mm(tol_mm)} mm)"})
    if fehlt_b:
        ab["nur_in_b"] = fehlt_b
        ab["eintraege"].append({"art": "nur_in_b", "name": "Punkte",
                                "text": f"{fehlt_b} von {r['punkte_b']} platzierten Punkten ohne Gegenstueck "
                                        f"in A ({1 - r['b_in_a']:.4%}, +-{_mm(tol_mm)} mm)"})
    if r["dreiecke_a"] != r["dreiecke_b"]:
        # andere Zerlegung derselben Flaechen, z. B. nach dem Umschreiben: Hinweis, kein Unterschied
        ab["hinweise"] = [f"Dreiecke {r['dreiecke_a']} -> {r['dreiecke_b']} (andere Zerlegung, "
                          "die Punkte entscheiden)"]
    ab["werte"] = {k: v for k, v in r.items()}
    return ab


def _vergleiche_texturen(ta, tb):
    (ka, na), (kb, nb) = ta, tb
    ab = _abschnitt()
    r = textur_vergleich(ka, na, kb)
    hit_pos, hit = r["hit_pos"], r["hit"]
    ab["gleich"] = int(hit.sum())
    ab["geaendert"] = int((hit_pos & ~hit).sum())
    ab["nur_in_a"] = int((~hit_pos).sum())
    ab["nur_in_b"] = int((~np.isin(_als_void(np.ascontiguousarray(kb[:, :3])),
                                   _als_void(np.ascontiguousarray(ka[:, :3])))).sum()) if len(kb) else 0
    if ab["nur_in_a"]:
        ab["eintraege"].append({"art": "nur_in_a", "name": "Ecken",
                                "text": f"{ab['nur_in_a']} von {len(ka)} texturierten Ecken ohne Gegenstueck in B"})
    if ab["nur_in_b"]:
        ab["eintraege"].append({"art": "nur_in_b", "name": "Ecken",
                                "text": f"{ab['nur_in_b']} von {len(kb)} texturierten Ecken ohne Gegenstueck in A"})
    for name, anteil, anzahl in r["je_textur"]:
        if anteil < 1.0:
            ab["eintraege"].append({"art": "geaendert", "name": name,
                                    "text": f"gleiche Texturlage bei {anteil:.1%} von {anzahl} Ecken"})
    ab["werte"] = {"ecken_a": len(ka), "ecken_b": len(kb),
                   "position_vorhanden": float(hit_pos.mean()) if len(ka) else None,
                   "gleiche_texturlage": float(hit[hit_pos].mean()) if hit_pos.any() else None}
    return ab


def vergleiche(sa: dict, sb: dict, abschnitte=ABSCHNITTE, toleranz_mm=0.1) -> dict:
    """Zwei Schnappschuesse vergleichen. Rueckgabe: {"gleich", "a", "b", "abschnitte"}."""
    out: dict = {}
    for name in abschnitte:
        if name == "modell":
            out[name] = _vergleiche_modell(sa["modell"], sb["modell"])
        elif name == "platzierungen":
            out[name] = _vergleiche_platzierungen(sa["platzierungen"], sb["platzierungen"], toleranz_mm)
        elif name == "definitionen":
            out[name] = _vergleiche_liste(name, sa[name], sb[name])
        else:
            out[name] = _vergleiche_liste(name, _nach_name(sa[name]), _nach_name(sb[name]))
    if "geometrie" in sa and "geometrie" in sb:
        out["geometrie"] = _vergleiche_geometrie(sa["geometrie"], sb["geometrie"], toleranz_mm)
    if "texturen" in sa and "texturen" in sb:
        out["texturen"] = _vergleiche_texturen(sa["texturen"], sb["texturen"])
    gleich = all(ab["nur_in_a"] + ab["nur_in_b"] + ab["geaendert"] == 0 for ab in out.values())
    return {"gleich": gleich, "a": zusammenfassung(sa), "b": zusammenfassung(sb), "abschnitte": out}


# ---------------------------------------------------------------- Ausgabe und Befehl

_ART = {"nur_in_a": "nur in A", "nur_in_b": "nur in B", "geaendert": "geaendert"}


def _zeige_datei(tag, z):
    kb = z["groesse_bytes"] / 1024
    print(f"{tag}: {z['datei']}  (Version {z['version']}, {kb:.0f} KB, {z['definitionen']} Definitionen, "
          f"{z['platzierungen']} Platzierungen, {z['flaechen_platziert']} platzierte Flaechen)")


def drucke(ergebnis: dict, alle: bool) -> None:
    _zeige_datei("A", ergebnis["a"])
    _zeige_datei("B", ergebnis["b"])
    summe = 0
    for name, ab in ergebnis["abschnitte"].items():
        diffs = ab["nur_in_a"] + ab["nur_in_b"] + ab["geaendert"]
        summe += diffs
        einheit = {"geometrie": " Punkte", "texturen": " Ecken"}.get(name, "")
        print(f"{TITEL[name]}: {ab['gleich']}{einheit} gleich, {ab['nur_in_a']} nur in A, "
              f"{ab['nur_in_b']} nur in B, {ab['geaendert']} geaendert")
        eintraege = ab["eintraege"] if alle else ab["eintraege"][:LIMIT]
        for e in eintraege:
            print(f"  {_ART[e['art']] + ':':11} {e['name']}" + (f"  {e['text']}" if e["text"] else ""))
        rest = len(ab["eintraege"]) - len(eintraege)
        if rest > 0:
            print(f"  ... {rest} weitere Eintraege, alle anzeigen mit --all")
        for h in ab.get("hinweise", []):
            print(f"  Hinweis: {h}")
    print("Ergebnis: gleich" if ergebnis["gleich"]
          else f"Ergebnis: verschieden ({summe} Unterschied{'' if summe == 1 else 'e'})")


def _toleranz(text):
    import argparse

    try:
        v = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"keine Zahl: {text}") from None
    if not math.isfinite(v) or v <= 0:
        raise argparse.ArgumentTypeError("die Toleranz muss eine positive Zahl in mm sein")
    return v


def _abschnitte(text):
    import argparse

    namen = [t.strip().lower() for t in text.split(",") if t.strip()]
    falsch = [n for n in namen if n not in ABSCHNITTE]
    if falsch or not namen:
        raise argparse.ArgumentTypeError(f"unbekannt: {', '.join(falsch) or '(leer)'}; "
                                         f"moeglich: {', '.join(ABSCHNITTE)}")
    return [n for n in ABSCHNITTE if n in namen]


def add_diff_parser(sub) -> None:
    """Unterbefehl "diff" an den argparse-Subparser von skptool haengen."""
    p = sub.add_parser("diff", help="Zwei .skp-Dateien vergleichen (Rueckgabe 0 gleich, 1 verschieden, 2 Fehler)",
                       description="Vergleicht Ebenen, Materialien, Definitionen und Platzierungen zweier "
                                   ".skp-Dateien, optional auch platzierte Geometrie und Texturlage. "
                                   "Rueckgabewert wie bei diff: 0 gleich, 1 verschieden, 2 Fehler.")
    p.add_argument("a", help="erste .skp-Datei")
    p.add_argument("b", help="zweite .skp-Datei")
    p.add_argument("--geometrie", action="store_true",
                   help="Platzierte Eckpunkte vergleichen (baut die Szene auf, braucht viel Arbeitsspeicher)")
    p.add_argument("--texturen", action="store_true",
                   help="Texturlage je Ecke vergleichen (baut die Szene auf, braucht viel Arbeitsspeicher)")
    p.add_argument("--toleranz", type=_toleranz, default=0.1, metavar="MM",
                   help="Toleranz fuer Positionen und Punkte in mm (Standard 0.1)")
    p.add_argument("--nur", type=_abschnitte, default=list(ABSCHNITTE), metavar="LISTE",
                   help="nur diese Abschnitte, mit Komma getrennt: " + ",".join(ABSCHNITTE))
    p.add_argument("--all", action="store_true", help="Alle Eintraege zeigen (sonst hoechstens 25 je Abschnitt)")
    p.add_argument("--json", action="store_true", help="Ergebnis als ein JSON-Dokument")
    p.add_argument("-q", "--quiet", action="store_true", help="Keine Ausgabe, nur Rueckgabewert")
    p.add_argument("-v", "--verbose", action="store_true", help="Technische Details bei Lesefehlern")
    p.set_defaults(func=cmd_diff)


def cmd_diff(a) -> int:
    from skptool import cli

    step = cli._Progress(a.quiet)
    snaps = []
    for path in (Path(a.a), Path(a.b)):
        try:
            if path.is_file() and path.stat().st_size >= 5 * 2**20:
                step(cli._read_hint(path))
            snaps.append(schnappschuss(path, a.geometrie, a.texturen, a.toleranz))
        except VergleichsFehler as err:
            exc = err.exc
            text = ("Datei nicht gefunden" if isinstance(exc, FileNotFoundError)
                    else cli._explain(path, exc, a.verbose))
            print(f"FEHLER {path}: {text}", file=sys.stderr)
            return 2
    ergebnis = vergleiche(snaps[0], snaps[1], a.nur, a.toleranz)
    if a.quiet:
        pass
    elif a.json:
        print(json.dumps(ergebnis, indent=2, ensure_ascii=False))
    else:
        drucke(ergebnis, a.all)
    return 0 if ergebnis["gleich"] else 1
