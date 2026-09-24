"""Nativer 3MF-Export fuer 3D-Druck-Slicer (PrusaSlicer, Bambu Studio, Cura), ohne Blender.

3MF (Core-Spezifikation 1.3) ist ein OPC-ZIP mit drei Teilen: [Content_Types].xml, _rels/.rels
und 3D/3dmodel.model. Einheit Millimeter, Z oben wie in SketchUp.

Aufbau des Modells:
  basematerials  ein Eintrag je Material (Name wie in SketchUp, displaycolor als sRGB-Hex)
  Netz-Objekte   jede eindeutige Geometrie (OpenSKP "mesh resource") genau einmal
  Baugruppe      ein Objekt mit <components>: jede Platzierung verweist mit Weltmatrix auf ihr
                 Netz-Objekt. Genau ein <build><item> auf die Baugruppe, der Slicer sieht also
                 ein Objekt aus mehreren Teilen, relative Lagen bleiben erhalten.

Matrizen: 3MF rechnet mit Zeilenvektoren, p' = [x y z 1] * M. Das Attribut transform listet
"m00 m01 m02 m10 m11 m12 m20 m21 m22 m30 m31 m32", also die Bilder der x-, y- und z-Achse
(= Spalten der ueblichen Spaltenvektor-Matrix L) und danach die Verschiebung.

Gespiegelte Platzierungen (Determinante < 0): die Spezifikation verlangt, dass der Leser dann die
Umlaufrichtung selbst korrigiert. Darauf verlassen wir uns nicht. Solche Platzierungen verweisen
auf eine gespiegelte Variante des Netzes (z gespiegelt, Umlauf umgedreht) mit einer Matrix
positiver Determinante. Das Ergebnis ist bei jedem Leser gleich.

Netz-Objekte liegen um die Mitte ihres Huellquaders, die Verschiebung steckt in der Matrix der
Platzierung (siehe centered). Das umgeht einen Fehler in OrcaSlicer/Bambu Studio bei gedrehten
Platzierungen mehrfach benutzter Netze.

Geprueft mit PrusaSlicer 2.9.6, OrcaSlicer 2.4.2, lib3mf 2.5.0 (strenger Modus) und den XSD der
3MF-Core-Spezifikation. PrusaSlicer macht aus jeder Platzierung ein eigenes Objekt; beruehren sich
Teile, fragt es beim Oeffnen, ob es ein Objekt aus mehreren Teilen sein soll ("Multi-part object
detected"). Nur mit "Ja" bleiben die Teile in ihrer Lage zueinander, sonst setzt es jedes Teil
einzeln auf die Druckplatte. Flache Teile ohne Volumen entfernt es mit einem Hinweis.
OrcaSlicer laedt die Baugruppe als ein Objekt.

Rueckseiten: OpenSKP gibt Flaechen mit verschiedenen Farben vorne und hinten zweimal aus (die
Rueckseite umgekehrt gewunden). Fuer den Druck ergaebe das zwei deckungsgleiche Huellen ohne
Volumen, deshalb bleibt je Flaeche nur die Vorderseite (siehe drop_back_sides). Die 3MF hat daher
weniger Dreiecke als die GLB: Dreiecke 3MF + entfernte Rueckseiten = Dreiecke GLB.

Texturen werden nicht geschrieben (nur die Grundfarbe des Materials).
Netze werden nicht repariert. Offene oder uneinheitlich orientierte Netze meldet write_3mf als
Hinweis, damit klar ist, warum ein Slicer sich beschwert.
"""
from __future__ import annotations

import math
import os
import re
import sys
import zipfile
from pathlib import Path

import numpy as np

from skptool import __version__
from skptool.gltf_writer import MAX_NODES, _material_names

MODEL_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
MODEL_PATH = "3D/3dmodel.model"
CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
    '</Types>\n')
RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    f'<Relationship Target="/{MODEL_PATH}" Id="rel0" '
    'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
    '</Relationships>\n')
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)  # feste Zeitstempel: gleiche Eingabe, gleiche Bytes
MAX_NAME = 256
MAX_WARN_LINES = 20

# Achsen: die instanzierte Szene ist glTF (Meter, Y oben). SketchUp und 3MF: Z oben.
# (x, y, z)_glTF -> (x, -z, y)_SketchUp
_P = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]])
_MIRROR = np.diag([1.0, 1.0, -1.0])  # Spiegelung fuer Varianten gespiegelter Platzierungen
_M_TO_MM = 1000.0

# In XML 1.0 erlaubte Zeichen; alles andere (Steuerzeichen, einzelne Surrogate) fliegt raus
_XML_ILLEGAL = re.compile("[^\t\n\r\u0020-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")
_ATTR_ESCAPE = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;",
                "\t": "&#9;", "\n": "&#10;", "\r": "&#13;"}


def xml_attr(text) -> str:
    """Text sicher als XML-Attributwert (ohne Anfuehrungszeichen). Namen stammen aus fremden
    Dateien: verbotene Zeichen entfernen, Sonderzeichen maskieren, Laenge begrenzen."""
    s = _XML_ILLEGAL.sub("", str(text if text is not None else ""))[:MAX_NAME]
    return "".join(_ATTR_ESCAPE.get(c, c) for c in s)


def _num(v: float) -> str:
    """Zahl im Format ST_Number: kein nan/inf, keine Gebietsschema-Abhaengigkeit, kurz."""
    v = float(v)
    if not math.isfinite(v):
        raise ValueError("ungueltige Zahl (NaN oder unendlich) im Modell, nichts geschrieben")
    s = repr(v + 0.0)  # + 0.0 macht aus -0.0 eine 0.0
    if s.endswith(".0"):
        s = s[:-2]
    return s


def _transform(lin: np.ndarray, t: np.ndarray) -> str:
    """3MF-transform aus Spaltenvektor-Form p' = lin @ p + t (Millimeter)."""
    vals = [round(float(x), 12) for x in lin.T.reshape(-1)] + [round(float(x), 6) for x in t]
    return " ".join(_num(x) for x in vals)


def _displaycolor(rgba) -> str:
    out = []
    for c in list(rgba)[:4]:
        c = float(c)
        out.append(max(0, min(255, round(c * 255))) if math.isfinite(c) else 255)
    while len(out) < 3:
        out.append(255)
    text = "#%02X%02X%02X" % tuple(out[:3])
    if len(out) > 3 and out[3] < 255:
        text += "%02X" % out[3]
    return text


def _welded_mesh(res):
    """Punkte (float64, mm, Z oben) und Dreiecke eines mesh resource, exakt gleiche Punkte
    verschmolzen (keine Reparatur: nur gleiche Koordinaten werden ein Punkt, sonst waere jedes
    Netz fuer den Slicer offen). Rueckgabe (verts, tris (n,3), materialindex je Dreieck, entfernt)."""
    pos_parts, tri_parts, mat_parts, off = [], [], [], 0
    for prim in res.primitives:
        if not len(prim.indices):
            continue
        pos = np.asarray(prim.positions, np.float32).reshape(-1, 3)
        idx = np.asarray(prim.indices, np.int64).reshape(-1, 3)
        if len(idx) and (idx.min() < 0 or idx.max() >= len(pos)):
            raise ValueError(f"Netz {res.id}: Dreiecksindex ausserhalb der Punktliste")
        pos_parts.append(pos)
        tri_parts.append(idx + off)
        mat_parts.append(np.full(len(idx), int(prim.material_index), np.int64))
        off += len(pos)
    if not tri_parts:
        return None
    pos = np.concatenate(pos_parts)
    if not np.isfinite(pos).all():
        raise ValueError(f"Netz {res.id}: ungueltige Koordinaten (NaN oder unendlich), nichts geschrieben")
    _, first, inv = np.unique(pos, axis=0, return_index=True, return_inverse=True)
    inv = inv.reshape(-1)
    order = np.argsort(first, kind="stable")  # Punkte in Reihenfolge des ersten Auftretens
    rank = np.empty_like(order)
    rank[order] = np.arange(len(order))
    pos32 = pos[first[order]]
    verts = (pos32.astype(np.float64) * _M_TO_MM) @ _P.T
    tris = rank[inv[np.concatenate(tri_parts)]]
    mats = np.concatenate(mat_parts)
    ok = (tris[:, 0] != tris[:, 1]) & (tris[:, 1] != tris[:, 2]) & (tris[:, 0] != tris[:, 2])
    return verts, pos32, tris[ok], mats[ok], int((~ok).sum())


def _face_lookup(defn, pos32: np.ndarray):
    """Je verschmolzenem Punkt die SketchUp-Flaechen, an denen er liegt, und deren Normalen.

    Die Punkte werden genau so gerechnet wie in OpenSKP (Zoll * 0.0254 als float32, Achsen
    wie glTF), damit der Vergleich bitgenau ist."""
    index = {tuple(p): i for i, p in enumerate(pos32.tolist())}
    vids = list(defn.vertices)
    raw = np.array([(v.x, v.z, -v.y) for v in defn.vertices.values()], np.float64).reshape(-1, 3)
    vkey = {}
    for vid, p in zip(vids, (raw * 0.0254).astype(np.float32).tolist()):
        k = index.get(tuple(p))
        if k is not None:
            vkey[vid] = k
    faces_at: dict = {}
    normals = {}
    for fid, face in defn.faces.items():
        fn = tuple(float(x) for x in (face.normal or (0.0, 0.0, 0.0)))
        fl = math.sqrt(sum(x * x for x in fn)) if len(fn) == 3 else 0.0
        if fl > 0 and math.isfinite(fl):
            normals[fid] = (fn[0] / fl, fn[1] / fl, fn[2] / fl)
        for loop in face.loops or []:
            for edge_id, _sense in loop:
                e = defn.edges.get(edge_id)
                if e is None:
                    continue
                for vid in (e.v1_id, e.v2_id):
                    k = vkey.get(vid)
                    if k is not None:
                        faces_at.setdefault(k, set()).add(fid)
    return faces_at, normals


_COPLANAR = 0.99  # |cos| zwischen Dreiecks- und Flaechennormale: Dreieck liegt in dieser Flaeche


def _front_parity(tri, unit_n, faces_at, normals):
    """True/False: das Dreieck tri (Einheitsnormale unit_n aus seinem Umlauf) ist die Vorderseite
    bzw. Rueckseite seiner SketchUp-Flaeche. None: nicht eindeutig (keine Flaeche gefunden oder
    zwei deckungsgleiche Flaechen mit entgegengesetzter Normale, dann sind beide Seiten echt)."""
    a, b, c = tri
    cands = faces_at.get(a, set()) & faces_at.get(b, set()) & faces_at.get(c, set())
    if not cands or unit_n is None:
        return None
    cos = [unit_n[0] * fn[0] + unit_n[1] * fn[1] + unit_n[2] * fn[2]
           for fn in (normals.get(f) for f in sorted(cands)) if fn is not None]
    if not cos:
        return None
    coplanar = {x > 0 for x in cos if abs(x) >= _COPLANAR}
    if len(coplanar) == 1:
        return coplanar.pop()
    if coplanar:  # zwei deckungsgleiche Flaechen, eine nach vorn, eine nach hinten
        return None
    best = max(cos, key=abs)  # schmales Dreieck an einer Kante: am besten passende Flaeche
    return best > 0 if abs(best) > 1e-9 else None


def drop_back_sides(verts, pos32, tris, mats, defn):
    """Rueckseiten entfernen: OpenSKP gibt Flaechen mit unterschiedlicher Vorder- und
    Rueckseitenfarbe doppelt aus (Vorderseite plus umgekehrt gewundene Rueckseite). Fuer den Druck
    waeren das zwei deckungsgleiche Huellen mit Volumen null.

    Gesucht werden Dreiecke mit denselben drei Punkten in beiden Umlaufrichtungen. Behalten wird
    die Richtung, die zur Normale der SketchUp-Flaeche passt (Vorderseite). Ist das nicht
    eindeutig, bleiben beide Seiten (es wird nie eine moeglicherweise echte Flaeche geloescht).
    Rueckgabe (tris, mats, entfernt, unklar)."""
    if not len(tris):
        return tris, mats, 0, 0
    srt = np.sort(tris, axis=1)
    start = np.argmin(tris, axis=1)
    rows = np.arange(len(tris))
    even = tris[rows, (start + 1) % 3] < tris[rows, (start + 2) % 3]  # gleicher Umlauf wie srt
    _, first_in_group, group = np.unique(srt, axis=0, return_index=True, return_inverse=True)
    group = group.reshape(-1)
    n_even = np.bincount(group, weights=even.astype(np.float64))
    n_all = np.bincount(group)
    mixed = (n_even > 0) & (n_even < n_all)
    if not mixed.any() or defn is None:
        return tris, mats, 0, int(mixed.sum())
    faces_at, normals = _face_lookup(defn, pos32)
    keep_all = np.zeros(len(n_all), bool)
    front_even = np.zeros(len(n_all), bool)
    unclear = 0
    todo = np.flatnonzero(mixed)
    reps = srt[first_in_group[todo]]  # Umlauf "even"
    tri_n = np.cross(verts[reps[:, 1]] - verts[reps[:, 0]], verts[reps[:, 2]] - verts[reps[:, 0]])
    length = np.linalg.norm(tri_n, axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        unit = tri_n / length[:, None]
    unit_list = [u if ln > 0 else None for u, ln in zip(unit.tolist(), length.tolist())]
    for g, tri, unit_n in zip(todo.tolist(), reps.tolist(), unit_list):
        parity = _front_parity(tri, unit_n, faces_at, normals)
        if parity is None:
            unclear += 1
            keep_all[g] = True
        else:
            front_even[g] = parity  # srt hat Umlauf "even"
    keep = ~mixed[group] | keep_all[group] | (even == front_even[group])
    return tris[keep], mats[keep], int((~keep).sum()), unclear


def check_closed(verts: np.ndarray, tris: np.ndarray) -> dict:
    """Kantenpruefung: geschlossen (jede Kante genau zwei Dreiecke), einheitlicher Umlauf und
    Normalen nach aussen (positives Volumen)."""
    n = max(len(verts), 1)
    a, b = tris.reshape(-1), np.roll(tris, -1, axis=1).reshape(-1)
    directed = a * n + b
    undirected = np.minimum(a, b) * n + np.maximum(a, b)
    _, counts = np.unique(undirected, return_counts=True)
    open_edges = int((counts == 1).sum())
    nonmanifold = int((counts > 2).sum())
    _, dcounts = np.unique(directed, return_counts=True)
    flipped = int((dcounts > 1).sum())
    volume = 0.0
    if len(tris):
        v = verts[tris]
        volume = float(np.einsum("ij,ij->i", v[:, 0], np.cross(v[:, 1], v[:, 2])).sum() / 6.0)
    closed = open_edges == 0 and nonmanifold == 0 and flipped == 0
    return {"closed": closed, "open_edges": open_edges, "nonmanifold_edges": nonmanifold,
            "flipped_edges": flipped, "volume_mm3": volume, "inside_out": closed and volume < 0}


def _problem_text(name: str, chk: dict) -> str | None:
    parts = []
    if chk["open_edges"]:
        parts.append(f"{chk['open_edges']} offene Kanten")
    if chk["nonmanifold_edges"]:
        parts.append(f"{chk['nonmanifold_edges']} Kanten an mehr als zwei Flaechen")
    if chk["flipped_edges"]:
        parts.append(f"{chk['flipped_edges']} Kanten mit uneinheitlicher Flaechenrichtung")
    if chk["inside_out"]:
        parts.append("Flaechen zeigen nach innen")
    if not parts:
        return None
    return f"3MF-Objekt {name!r} ist nicht druckfertig geschlossen: " + ", ".join(parts)


VERT_DIGITS = 4  # Nachkommastellen der Punkte (mm): 0,1 Mikrometer


def centered(verts: np.ndarray):
    """Punkte so verschieben, dass die Mitte ihres Huellquaders im Ursprung liegt.
    Rueckgabe (Punkte gerundet wie in der Datei, Mitte c). Die Platzierung bekommt c als
    zusaetzliche Verschiebung (t + L c), die Weltlage bleibt gleich.

    Warum: OrcaSlicer 2.4 (und Bambu Studio, gleicher Leser) verschiebt jede weitere Platzierung
    eines mehrfach benutzten Netzes nach dem Anwenden der Matrix noch einmal um die unrotierte
    Mitte des Netzes. Gedrehte oder skalierte Platzierungen landen dort sonst neben der richtigen
    Stelle. Liegt die Mitte schon im Ursprung, ist diese Verschiebung null. Nebenbei bleiben die
    Koordinaten klein, Slicer rechnen intern mit float32 (bei 46 m Abstand zum Ursprung nur noch
    auf etwa 4 Mikrometer genau)."""
    rounded = np.round(verts, VERT_DIGITS)
    if not len(rounded):
        return rounded, np.zeros(3)
    c = np.round((rounded.min(axis=0) + rounded.max(axis=0)) / 2.0, VERT_DIGITS)
    return np.round(rounded - c, VERT_DIGITS), c


def _mesh_xml(obj_id: int, name: str, verts, tris, mats, pid: int) -> list[str]:
    default = int(mats[0]) if len(mats) else 0
    head = (f'<object id="{obj_id}" type="model" name="{xml_attr(name)}" pid="{pid}" pindex="{default}">'
            "<mesh><vertices>")
    out = [head]
    rounded = np.round(verts, VERT_DIGITS).tolist()
    out += [f'<vertex x="{_num(x)}" y="{_num(y)}" z="{_num(z)}"/>' for x, y, z in rounded]
    out.append("</vertices><triangles>")
    for (v1, v2, v3), m in zip(tris.tolist(), mats.tolist()):
        if m == default:
            out.append(f'<triangle v1="{v1}" v2="{v2}" v3="{v3}"/>')
        else:
            out.append(f'<triangle v1="{v1}" v2="{v2}" v3="{v3}" pid="{pid}" p1="{m}"/>')
    out.append("</triangles></mesh></object>")
    return out


def _placements(isc, max_components: int):
    """(mesh_resource_id, Weltmatrix 4x4 glTF Spaltenvektor) je Platzierung, ohne Rekursion."""
    out, stack, seen = [], [(isc.scene_hierarchy, np.eye(4))], 0
    while stack:
        node, parent = stack.pop()
        seen += 1
        if seen > MAX_NODES:
            raise ValueError(f"mehr als {MAX_NODES} Knoten, Datei vervielfacht sich beim Aufbau")
        m = np.asarray(node.matrix, np.float64)
        if m.shape != (16,) or not np.isfinite(m).all():
            raise ValueError("ungueltige Platzierungsmatrix (NaN oder unendlich), nichts geschrieben")
        world = parent @ m.reshape(4, 4).T
        if node.mesh_resource_id is not None:
            out.append((node.mesh_resource_id, world))
            if len(out) > max_components:
                raise ValueError(f"mehr als {max_components} Platzierungen fuer 3MF (Grenze "
                                 "SKPTOOL_MAX_PLACEMENTS), nichts geschrieben")
        stack.extend((c, world) for c in reversed(node.children))
    return out


def _write_zip_atomic(out: Path, parts: list[tuple[str, bytes]]) -> None:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.skptool-tmp")
    try:
        with zipfile.ZipFile(tmp, "w") as zf:
            for name, data in parts:
                info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 0  # sonst je Betriebssystem andere Bytes
                info.external_attr = 0o644 << 16
                zf.writestr(info, data, compresslevel=6)
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            tmp.unlink()


def write_3mf(model, isc, out: Path, max_components: int = 5_000_000, warn=None) -> dict:
    """Instanzierte OpenSKP-Szene als 3MF schreiben. warn(text) bekommt Hinweise (Standard: stderr)."""
    if warn is None:
        def warn(text):
            print(f"Hinweis: {text}", file=sys.stderr, flush=True)

    materials = _material_names(model, isc, textures=False, fallback_names=True)
    placements = _placements(isc, max_components)
    if not placements:
        raise ValueError("Keine Flaechen im Modell, 3MF braucht mindestens ein Netz. Nichts geschrieben.")

    # source_triangles: wie in der GLB; kept_triangles: davon behalten (je Netz, ohne Varianten);
    # triangles: tatsaechlich geschrieben (mit gespiegelten Varianten)
    stats = {"mesh_objects": 0, "components": 0, "triangles": 0, "placed_triangles": 0,
             "source_triangles": 0, "kept_triangles": 0,
             "mirrored_variants": 0, "degenerate_dropped": 0, "skipped_placements": 0,
             "back_sides_dropped": 0, "back_sides_unclear": 0, "placed_back_sides_dropped": 0,
             "materials": len(materials), "open_objects": 0, "checked_objects": 0, "problems": []}
    res_by_id = {r.id: r for r in isc.mesh_resources}
    base_pid = 1
    chunks = [
        '<?xml version="1.0" encoding="UTF-8"?>\n',
        f'<model unit="millimeter" xml:lang="en-US" xmlns="{MODEL_NS}">',
        f'<metadata name="Application">{xml_attr("skptool " + __version__)}</metadata>',
        f'<metadata name="Title">{xml_attr(Path(out).stem)}</metadata>',
        f'<resources><basematerials id="{base_pid}">',
    ]
    chunks += [f'<base name="{xml_attr(m["name"])}" displaycolor="'
               f'{_displaycolor(m["pbrMetallicRoughness"].get("baseColorFactor", (1, 1, 1, 1)))}"/>'
               for m in materials]
    if not materials:
        chunks.append('<base name="SketchUp_Standard" displaycolor="#DBDBD6"/>')
    chunks.append("</basematerials>")

    next_id = base_pid + 1
    objects: dict = {}  # (resource_id, gespiegelt) -> (objekt-id, Dreiecke)
    meshes: dict = {}   # resource_id -> (verts, tris, mats) oder None
    back_count: dict = {}  # resource_id -> entfernte Rueckseiten-Dreiecke
    components = []
    for res_id, world in placements:
        if res_id not in meshes:
            res = res_by_id.get(res_id)
            welded = _welded_mesh(res) if res is not None else None
            if welded is not None:
                verts, pos32, tris, mats, dropped = welded
                stats["source_triangles"] += len(tris) + dropped
                stats["degenerate_dropped"] += dropped
                defn = model.root if res.definition_id == "ROOT" else model.definitions.get(res.definition_id)
                tris, mats, backs, unclear = drop_back_sides(verts, pos32, tris, mats, defn)
                stats["back_sides_dropped"] += backs
                stats["back_sides_unclear"] += unclear
                back_count[res_id] = backs
                stats["kept_triangles"] += len(tris)
                welded = (verts, tris, mats) if len(tris) else None
                if welded is not None:
                    name = res.definition_name or res.id
                    chk = check_closed(verts, tris)
                    stats["checked_objects"] += 1
                    text = _problem_text(name, chk)
                    if text:
                        stats["open_objects"] += 1
                        stats["problems"].append(text)
            meshes[res_id] = welded
        mesh = meshes[res_id]
        if mesh is None:
            continue
        lin = _P @ world[:3, :3] @ _P.T
        t = _P @ world[:3, 3] * _M_TO_MM
        det = float(np.linalg.det(lin))
        if abs(det) < 1e-12:
            stats["skipped_placements"] += 1  # auf null skaliert: keine druckbare Geometrie
            continue
        mirrored = det < 0
        key = (res_id, mirrored)
        if key not in objects:
            verts, tris, mats = mesh
            name = res_by_id[res_id].definition_name or res_id
            if mirrored:
                verts, tris = verts @ _MIRROR, tris[:, [0, 2, 1]]
                name += " (gespiegelt)"
                stats["mirrored_variants"] += 1
            if len(mats) and (mats.min() < 0 or mats.max() >= max(len(materials), 1)):
                raise ValueError(f"Netz {res_id}: Materialindex ausserhalb der Materialliste")
            verts, center = centered(verts)
            chunks += _mesh_xml(next_id, name, verts, tris, mats, base_pid)
            objects[key] = (next_id, len(tris), center)
            stats["mesh_objects"] += 1
            stats["triangles"] += len(tris)
            next_id += 1
        obj_id, ntris, center = objects[key]
        if mirrored:
            lin = lin @ _MIRROR  # Netz ist schon gespiegelt, Matrix bekommt positive Determinante
        t = t + lin @ center  # Netz liegt um seine Mitte, siehe centered()
        ident = np.allclose(lin, np.eye(3), rtol=0, atol=1e-12) and np.allclose(t, 0, rtol=0, atol=1e-9)
        tr = "" if ident else f' transform="{_transform(lin, t)}"'
        components.append(f'<component objectid="{obj_id}"{tr}/>')
        stats["placed_triangles"] += ntris
        stats["placed_back_sides_dropped"] += back_count.get(res_id, 0)
    if not components:
        raise ValueError("Keine druckbare Geometrie im Modell, nichts geschrieben.")
    stats["components"] = len(components)
    assembly = next_id
    chunks.append(f'<object id="{assembly}" type="model" name="{xml_attr(Path(out).stem)}"><components>')
    chunks += components
    chunks.append(f'</components></object></resources><build><item objectid="{assembly}"/></build></model>\n')

    _write_zip_atomic(out, [("[Content_Types].xml", CONTENT_TYPES.encode("utf-8")),
                            ("_rels/.rels", RELS.encode("utf-8")),
                            (MODEL_PATH, "".join(chunks).encode("utf-8"))])

    for text in stats["problems"][:MAX_WARN_LINES]:
        warn(text)
    if len(stats["problems"]) > MAX_WARN_LINES:
        warn(f"... und {len(stats['problems']) - MAX_WARN_LINES} weitere nicht geschlossene 3MF-Objekte")
    if stats["problems"]:
        warn(f"3MF: {stats['open_objects']} von {stats['checked_objects']} Objekten nicht geschlossen. "
             "Slicer reparieren das meist selbst oder melden Fehler; skptool repariert keine Netze.")
    if stats["degenerate_dropped"]:
        warn(f"3MF: {stats['degenerate_dropped']} Dreiecke ohne Flaeche (doppelte Eckpunkte) weggelassen")
    if stats["back_sides_unclear"]:
        warn(f"3MF: bei {stats['back_sides_unclear']} doppelseitigen Dreiecken war die Vorderseite nicht "
             "eindeutig, beide Seiten wurden behalten")
    if stats["skipped_placements"]:
        warn(f"3MF: {stats['skipped_placements']} auf Groesse null skalierte Platzierungen weggelassen")
    return stats
