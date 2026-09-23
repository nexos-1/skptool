"""Bearbeitungsoperationen fuer SketchUp-Modelle in Blender (laeuft INNERHALB von Blender).

Eine feste Liste von Operationen, als JSON beschreibbar. Genutzt von:
  - bridge.py (Hintergrund: skptool edit / skptool list)
  - dem Live-Add-on im offenen Blender-Fenster

Bewusst gibt es keine Operation, die beliebigen Code ausfuehrt.

Jede Operation ist ein dict mit "op" und meist einer Auswahl:
  {"op": "move", "select": {"name": "Palme*"}, "by": [0, 0, 0.5]}

Auswahl ("select"), alle Angaben optional und kombinierbar (UND):
  name        Muster mit * und ?, ohne Gross-/Kleinschreibung, auf den Objektnamen
  layer       Name der Ebene (Collection)
  material    Objekt benutzt dieses Material
  definition  Name der SketchUp-Komponente (alle Platzierungen)
  type        "mesh" (Standard) oder "any"
Ohne "select" gilt die Operation fuer alle Mesh-Objekte (nur bei list, summary, measure und
hide/show sinnvoll; hide/show nehmen dann alle Objekte der Szene).

Einheiten: Meter, Grad. Achsen: x, y, z wie in SketchUp (z oben).

Operationen (Felder in eckigen Klammern sind optional):
  list          [select] [limit]              Objekte mit Ebene, Groesse, Materialien
  summary                                     Kennzahlen des ganzen Modells
  measure       [select] [to_object]          nur lesen: Groesse, Mitte, min/max der Auswahl;
                                              mit to_object auch Abstand der beiden Mitten
  move          select, by | to [anchor]      verschieben um [x, y, z] oder Mitte nach [x, y, z]
  rotate        select, deg [axis] [pivot]    drehen (axis x/y/z, Standard z)
  scale         select, factor [pivot]        skalieren, factor Zahl oder [x, y, z]
  mirror        select, axis [pivot]          spiegeln an der Ebene durch pivot senkrecht zu axis
  align         select, axis [to] [to_object] ausrichten: axis "x" oder ["x", "y"], to "min",
                                              "center" oder "max" (Standard "min") der gemeinsamen
                                              Box oder der Box von to_object (eine Auswahl)
  distribute    select, axis [gap]            gleichmaessig verteilen: ohne gap die Mitten
                                              zwischen dem ersten und letzten Objekt (mind. 3),
                                              mit gap feste Luecke in Metern ab dem ersten
  duplicate     select [offset] [count]       verknuepfte Kopien samt Inhalt in einer Reihe
  array         select, counts, spacing       verknuepfte Kopien im Raster: counts [nx, ny, nz]
                                              (je 1 bis 1000), spacing [dx, dy, dz] in Metern
  hide / show   [select]                      Objekte samt Inhalt aus- bzw. einblenden (nicht
                                              Ebenen), wird in der .skp als verborgen geschrieben
  set_material  select, material [color] [alpha] [replace]
  recolor       material, color [alpha]
  set_layer     select, layer
  hide_layer / show_layer  layer
  delete        select
  rename        select, to
  add_box       [name] [size] [at] [layer] [material] [color] [alpha]
Pivot ("pivot"): "self" (Standard, je Objekt seine Mitte), "group" (Mitte aller), "origin" oder
[x, y, z]; rotate und scale kennen zusaetzlich "bottom".
Die Box eines Objekts umfasst bei align, distribute, mirror, array und measure seinen ganzen
Inhalt (wie eine SketchUp-Gruppe). Die neueren Operationen (measure, mirror, align, distribute,
array, hide, show) lehnen unbekannte Felder ab.

Grenzen (gegen versehentliche oder boeswillige Riesenauftraege): hoechstens MAX_OPS Operationen
pro Aufruf, hoechstens MAX_OBJECTS Objekte in der Szene, Zahlen endlich und betragsmaessig bis
MAX_COORD, Namen bis 63 Zeichen ohne Steuerzeichen.
"""
import fnmatch
import math
import unicodedata

import bpy
from mathutils import Matrix, Vector

OPS = {}
MAX_OPS = 1000
MAX_OBJECTS = 100_000
MAX_COORD = 1e6        # Meter bzw. Faktor
MAX_PATTERN = 256
READ_ONLY = frozenset({"list", "summary", "measure"})  # aendern nichts (kein Rueckgaengig-Schritt)
# Markiert Objekte, die hide nur zusammen mit ihrem Elternobjekt verborgen hat. Beim Schreiben der
# .skp traegt dann nur die aeussere Gruppe das Verborgen-Merkmal, wie nach "Ausblenden" in SketchUp.
HIDDEN_WITH_PARENT = "skp_hidden_with_parent"


def op(fn=None, *, name=None):
    """Operation registrieren; der JSON-Name ist der Funktionsname oder name=..."""
    def register(f):
        OPS[name or f.__name__] = f
        return f
    return register(fn) if fn is not None else register


class OpError(ValueError):
    pass


# ---------------------------------------------------------------- Auswahl

def _collections_of(ob):
    return [c.name for c in ob.users_collection]


def _materials_of(ob):
    return sorted({_base(s.material.name) for s in ob.material_slots if s.material})


def _suffix(name):
    return len(name) > 4 and name[-4] == "." and name[-3:].isdigit()


def _base(name):
    return name[:-4] if _suffix(name) else name


def _text(v, what, max_len=MAX_PATTERN):
    if not isinstance(v, str):
        raise OpError(f"{what} muss ein Text sein")
    if len(v) > max_len:
        raise OpError(f"{what} ist zu lang (hoechstens {max_len} Zeichen)")
    if any(unicodedata.category(ch) == "Cc" or 0x80 <= ord(ch) <= 0x9F for ch in v):
        raise OpError(f"{what} enthaelt Steuerzeichen")
    return v


def _name(v, what):
    """Name fuer Objekt, Ebene oder Material: 1 bis 63 Zeichen (Blender-Grenze), ohne Steuerzeichen."""
    v = _text(v, what, 63).strip()
    if not v:
        raise OpError(f"{what} darf nicht leer sein")
    return v


def select(spec=None):
    if spec is None:
        spec = {}
    if not isinstance(spec, dict):
        raise OpError('select muss ein Objekt sein, z. B. {"name": "Palme*"}')
    unknown = set(spec) - {"name", "layer", "material", "definition", "type"}
    if unknown:
        raise OpError(f"Unbekannte Auswahlfelder: {', '.join(sorted(map(str, unknown)))}")
    for key in ("name", "layer", "material", "definition"):
        if key in spec:
            _text(spec[key], f"select.{key}")
    kind = spec.get("type", "mesh")
    if kind not in ("mesh", "any"):
        raise OpError('select.type muss "mesh" oder "any" sein')
    obs = [o for o in bpy.context.scene.objects if kind == "any" or o.type == "MESH"]
    if "name" in spec:
        pat = spec["name"].lower()
        obs = [o for o in obs if fnmatch.fnmatchcase(o.name.lower(), pat)
               or fnmatch.fnmatchcase(_base(o.name).lower(), pat)]
    if "layer" in spec:
        obs = [o for o in obs if spec["layer"] in _collections_of(o)]
    if "material" in spec:
        want = spec["material"]
        obs = [o for o in obs if any(s.material and _base(s.material.name) == want for s in o.material_slots)]
    if "definition" in spec:
        want = spec["definition"]
        obs = [o for o in obs if str(o.get("skp_definition", "")) == want
               or (o.type == "MESH" and _base(o.data.name) == want)]
    return obs


def _require(obs, spec):
    if not obs:
        raise OpError(f"Keine Objekte passen zur Auswahl {spec}")
    return obs


# ---------------------------------------------------------------- Hilfen

def _world_bbox(obs):
    mn = Vector((math.inf,) * 3)
    mx = Vector((-math.inf,) * 3)
    for o in obs:
        for c in o.bound_box:
            w = o.matrix_world @ Vector(c)
            mn = Vector(map(min, mn, w))
            mx = Vector(map(max, mx, w))
    return mn, mx


def _pivot(obs, pivot):
    if pivot in (None, "self", "each", "bottom"):
        return None  # je Objekt eigene Mitte (bei "bottom" Mitte der Unterkante)
    if pivot == "group":
        mn, mx = _world_bbox(obs)
        return (mn + mx) / 2
    if pivot == "origin":
        return Vector((0, 0, 0))
    if isinstance(pivot, (list, tuple)) and len(pivot) == 3:
        return _vec(pivot, "pivot")
    raise OpError(f"Unbekannter Drehpunkt {pivot!r} (self, group, origin oder [x, y, z])")


def _apply_world(ob, m_world):
    """Weltmatrix setzen, auch fuer Kinder in einer Hierarchie."""
    ob.matrix_world = m_world @ ob.matrix_world


def _around(center, m):
    return Matrix.Translation(center) @ m @ Matrix.Translation(-center)


def _num(v, name, lo=-MAX_COORD, hi=MAX_COORD):
    """Endliche Zahl in [lo, hi]; keine Wahrheitswerte, keine Texte, kein NaN/Unendlich."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise OpError(f"{name} muss eine Zahl sein")
    v = float(v)
    if not math.isfinite(v) or not lo <= v <= hi:
        raise OpError(f"{name} muss zwischen {lo:g} und {hi:g} liegen")
    return v


def _int(v, name, lo, hi):
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise OpError(f"{name} muss eine ganze Zahl zwischen {lo} und {hi} sein")
    return v


def _vec(v, name, lo=-MAX_COORD, hi=MAX_COORD):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        x = _num(v, name, lo, hi)
        return Vector((x, x, x))
    if isinstance(v, (list, tuple)) and len(v) == 3:
        return Vector([_num(x, name, lo, hi) for x in v])
    raise OpError(f"{name} braucht eine Zahl oder [x, y, z]")


def _color(c):
    """[r, g, b] als 0 bis 255 oder 0 bis 1 (alle Werte hoechstens 1)."""
    if not isinstance(c, (list, tuple)) or len(c) not in (3, 4):
        raise OpError("color braucht [r, g, b], Werte 0 bis 255 (oder 0 bis 1)")
    vals = [_num(x, "color", 0, 255) for x in c[:3]]
    return [x / 255 for x in vals] if max(vals) > 1 else vals


def _budget(extra):
    now = len(bpy.data.objects)
    if now + extra > MAX_OBJECTS:
        raise OpError(f"Das ergaebe {now + extra} Objekte, hoechstens {MAX_OBJECTS} sind erlaubt")


def _fields(p, name, allowed):
    """Unbekannte Felder ablehnen (Tippfehler wie "axsi" sollen nicht still ignoriert werden)."""
    unknown = set(p) - {"op", *allowed}
    if unknown:
        shown = ", ".join(sorted(repr(str(k)[:40]) for k in unknown)[:10])
        raise OpError(f"Unbekannte Felder fuer {name}: {shown}. Moeglich: {', '.join(allowed)}")


def _need(p, key, hint):
    if key not in p:
        raise OpError(f"{p.get('op')} braucht {hint}")
    return p[key]


def _axis(v, what="axis"):
    """Achse als Index 0, 1, 2 aus "x", "y", "z" (ohne Gross-/Kleinschreibung)."""
    if not isinstance(v, str) or v.strip().lower() not in ("x", "y", "z"):
        raise OpError(f"{what} muss x, y oder z sein")
    return "xyz".index(v.strip().lower())


def _axes(v, what="axis"):
    """Eine Achse ("x") oder eine Liste verschiedener Achsen (["x", "y"])."""
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not 1 <= len(v) <= 3:
        raise OpError(f'{what} muss x, y oder z sein oder eine Liste wie ["x", "y"]')
    out = []
    for a in v:
        i = _axis(a, what)
        if i in out:
            raise OpError(f"{what} nennt eine Achse doppelt")
        out.append(i)
    return out


def _tree_meshes(roots):
    """Alle Mesh-Objekte in den Teilbaeumen (Gruppe samt Inhalt)."""
    return [o for r in roots for o in _subtree(r) if o.type == "MESH"]


def _tree_bbox(roots):
    """Weltbox der Objekte samt Inhalt; ohne Geometrie die Lage der Objekte selbst."""
    meshes = _tree_meshes(roots)
    if meshes:
        return _world_bbox(meshes)
    pts = [r.matrix_world.translation for r in roots]
    mn, mx = pts[0].copy(), pts[0].copy()
    for q in pts[1:]:
        mn = Vector(map(min, mn, q))
        mx = Vector(map(max, mx, q))
    return mn, mx


def _within(mn, mx):
    """Ergebnis muss im erlaubten Bereich bleiben (sonst scheitert erst das Schreiben der .skp)."""
    if max(max(abs(v) for v in mn), max(abs(v) for v in mx)) > MAX_COORD:
        raise OpError(f"Das Ergebnis laege weiter als {MAX_COORD:g} m vom Ursprung entfernt")


def _selection(p, key="select"):
    spec = p.get(key)
    return _top_level(_require(select(spec), spec))


def _top_level(obs):
    """Objekte, deren Eltern nicht mit ausgewaehlt sind (sonst doppelte Transformation)."""
    chosen = set(obs)
    out = []
    for o in obs:
        p = o.parent
        while p is not None and p not in chosen:
            p = p.parent
        if p is None:
            out.append(o)
    return out


def _info(o):
    mn, mx = _world_bbox([o])
    size = mx - mn
    return {
        "name": o.name,
        "layer": ", ".join(_collections_of(o)),
        "definition": str(o.get("skp_definition", "")) or (_base(o.data.name) if o.type == "MESH" else ""),
        "placements": o.data.users if o.type == "MESH" else 0,
        "materials": _materials_of(o) if o.type == "MESH" else [],
        "center": [round(v, 3) for v in (mn + mx) / 2],
        "size": [round(v, 3) for v in size],
        "faces": len(o.data.polygons) if o.type == "MESH" else 0,
        "hidden": not o.visible_get(),
    }


def _material(name, color=None, alpha=None, create=True):
    name = _name(name, "material")
    rgb_in = _color(color) if color is not None else None
    alpha = _num(alpha, "alpha", 0, 1) if alpha is not None else None
    mat = bpy.data.materials.get(name)
    if mat is None:
        if not create:
            raise OpError(f"Material {name!r} gibt es nicht")
        if color is None:
            raise OpError(f"Neues Material {name!r} braucht eine Farbe, z. B. \"color\": [60, 60, 60]")
        mat = bpy.data.materials.new(name)
        mat.use_nodes = True
    if rgb_in is not None:
        rgb = rgb_in
        a = 1.0 if alpha is None else alpha
        mat.diffuse_color = (*rgb, a)
        node = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None) if mat.use_nodes else None
        if node is not None:
            base = node.inputs["Base Color"]
            for link in list(base.links):  # Farbe statt Textur
                mat.node_tree.links.remove(link)
            base.default_value = (*rgb, 1.0)
            node.inputs["Alpha"].default_value = a
    elif alpha is not None:
        mat.diffuse_color = (*mat.diffuse_color[:3], float(alpha))
    return mat


# ---------------------------------------------------------------- Operationen

@op(name="list")
def list_objects(p):
    obs = select(p.get("select"))
    limit = _int(p.get("limit", 200), "limit", 1, 100_000)
    return {"count": len(obs), "objects": [_info(o) for o in obs[:limit]],
            "truncated": len(obs) > limit}


@op
def summary(p):
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    mn, mx = _world_bbox(meshes) if meshes else (Vector(), Vector())
    return {
        "objects": len(meshes),
        "unique_meshes": len({o.data for o in meshes}),
        "size": [round(v, 3) for v in (mx - mn)],
        "layers": [{"name": c.name, "objects": len(c.objects), "hidden": c.hide_viewport}
                   for c in bpy.data.collections],
        "materials": sorted({_base(m.name) for m in bpy.data.materials if m.users}),
    }


@op
def move(p):
    obs = _top_level(_require(select(p.get("select")), p.get("select")))
    if "by" in p:
        d = _vec(p["by"], "by")
        for o in obs:
            _apply_world(o, Matrix.Translation(d))
    elif "to" in p:
        mn, mx = _world_bbox(obs)
        center = (mn + mx) / 2
        target = _vec(p["to"], "to")
        if p.get("anchor") == "bottom":
            center.z = mn.z
        for o in obs:
            _apply_world(o, Matrix.Translation(target - center))
    else:
        raise OpError("move braucht \"by\": [x, y, z] oder \"to\": [x, y, z]")
    return {"moved": len(obs)}


@op
def rotate(p):
    obs = _top_level(_require(select(p.get("select")), p.get("select")))
    axis = p.get("axis", "z")
    axis = axis.upper() if isinstance(axis, str) else ""
    if axis not in ("X", "Y", "Z"):
        raise OpError("axis muss x, y oder z sein")
    m = Matrix.Rotation(math.radians(_num(p["deg"], "deg", -36000, 36000)), 4, axis)
    pivot = p.get("pivot", "self")
    center = _pivot(obs, pivot)
    for o in obs:
        c = center
        if c is None:
            mn, mx = _world_bbox([o])
            c = (mn + mx) / 2
            if pivot == "bottom":
                c.z = mn.z
        _apply_world(o, _around(c, m))
    return {"rotated": len(obs)}


@op
def scale(p):
    obs = _top_level(_require(select(p.get("select")), p.get("select")))
    f = _vec(p["factor"], "factor", -1e4, 1e4)
    if min(abs(v) for v in f) < 1e-6:
        raise OpError("factor muss betragsmaessig mindestens 0.000001 sein")
    m = Matrix.Diagonal((*f, 1.0))
    pivot = p.get("pivot", "self")
    center = _pivot(obs, pivot)
    for o in obs:
        c = center
        if c is None:
            mn, mx = _world_bbox([o])
            c = (mn + mx) / 2
            if pivot == "bottom" or p.get("anchor") == "bottom":
                c.z = mn.z
        _apply_world(o, _around(c, m))
    return {"scaled": len(obs)}


@op
def set_material(p):
    obs = _require(select(p.get("select")), p.get("select"))
    mat = _material(p["material"], p.get("color"), p.get("alpha"))
    only = p.get("replace")  # nur dieses Material ersetzen, sonst alle Slots
    if only is not None:
        only = _name(only, "replace")
    n = 0
    for o in obs:
        if not o.material_slots:
            if o.data.users > 1:
                o.data = o.data.copy()  # eigene Geometrie, sonst aenderten sich alle Platzierungen
            o.data.materials.append(mat)
            n += 1
            continue
        for s in o.material_slots:
            if only is None or (s.material and _base(s.material.name) == only):
                s.link = "OBJECT"  # nur dieses Objekt, geteilte Geometrie bleibt unberuehrt
                s.material = mat
                n += 1
    return {"slots_changed": n, "objects": len(obs)}


@op
def recolor(p):
    name = _name(p["material"], "material")
    mats = [m for m in bpy.data.materials if _base(m.name) == name]
    if not mats:
        raise OpError(f"Material {name!r} gibt es nicht")
    for m in mats:
        _material(m.name, p.get("color"), p.get("alpha"), create=False)
    return {"materials_changed": len(mats)}


@op
def set_layer(p):
    obs = _require(select(p.get("select")), p.get("select"))
    name = _name(p["layer"], "layer")
    col = bpy.data.collections.get(name)
    if col is None:
        col = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(col)
    for o in obs:
        for c in list(o.users_collection):
            c.objects.unlink(o)
        col.objects.link(o)
    return {"moved_to_layer": len(obs), "layer": name}


def _layer_visibility(name, hidden):
    name = _name(name, "layer")
    col = bpy.data.collections.get(name)
    if col is None:
        raise OpError(f"Ebene {name!r} gibt es nicht")
    col.hide_viewport = hidden
    col.hide_render = hidden
    return {"layer": name, "hidden": hidden}


@op
def hide_layer(p):
    return _layer_visibility(p["layer"], True)


@op
def show_layer(p):
    return _layer_visibility(p["layer"], False)


@op
def delete(p):
    obs = _top_level(_require(select(p.get("select")), p.get("select")))
    doomed = [o for root in obs for o in _subtree(root)]  # Gruppe samt Inhalt, wie in SketchUp
    names = [o.name for o in obs]
    for o in reversed(doomed):
        bpy.data.objects.remove(o, do_unlink=True)
    return {"deleted": len(doomed), "names": names[:50]}


@op
def rename(p):
    obs = _require(select(p.get("select")), p.get("select"))
    if len(obs) != 1:
        raise OpError(f"rename braucht genau ein Objekt, gefunden: {len(obs)}")
    old = obs[0].name
    obs[0].name = _name(p["to"], "to")
    return {"from": old, "to": obs[0].name}


def _subtree(ob):
    """Objekt mit allen Unterobjekten (wie eine SketchUp-Gruppe mit ihrem Inhalt)."""
    out = [ob]
    for child in ob.children:
        out += _subtree(child)
    return out


@op
def duplicate(p):
    """Verknuepfte Kopien samt Inhalt (SketchUp: weitere Platzierungen derselben Komponente)."""
    obs = _top_level(_require(select(p.get("select")), p.get("select")))
    step = _vec(p.get("offset", [1, 0, 0]), "offset")
    count = _int(p.get("count", 1), "count", 1, 1000)
    _budget(sum(len(_subtree(r)) for r in obs) * count)
    made = []
    for root in obs:
        for i in range(1, count + 1):
            made.append(_copy_tree(root, Matrix.Translation(step * i)).name)
    return {"created": len(made), "names": made[:50]}


def _copy_tree(root, m_world):
    """Verknuepfte Kopie von root samt Inhalt, um m_world (Weltmatrix) versetzt. Rueckgabe: neue Wurzel."""
    mapping = {}
    for o in _subtree(root):
        c = o.copy()  # teilt die Geometrie, behaelt lokale Transformation
        for col in o.users_collection:
            col.objects.link(c)
        mapping[o] = c
    for o, c in mapping.items():
        if o is root:
            c.parent = o.parent
            c.matrix_world = m_world @ o.matrix_world
        else:
            c.parent = mapping[o.parent]
            c.matrix_parent_inverse = o.matrix_parent_inverse.copy()
    return mapping[root]


@op
def array(p):
    """Verknuepfte Kopien im Raster (1D, 2D oder 3D); das Original bleibt Zelle [0, 0, 0]."""
    _fields(p, "array", ("select", "counts", "spacing"))
    obs = _selection(p)
    counts = _need(p, "counts", '"counts": [nx, ny, nz], z. B. [3, 2, 1]')
    if not isinstance(counts, list) or len(counts) != 3:
        raise OpError('counts braucht [nx, ny, nz], z. B. [3, 2, 1]')
    n = [_int(c, "counts", 1, 1000) for c in counts]
    total = n[0] * n[1] * n[2]
    if total < 2:
        raise OpError("counts ergibt keine Kopie, mindestens ein Wert muss groesser als 1 sein")
    step = _vec(_need(p, "spacing", '"spacing": [dx, dy, dz] in Metern'), "spacing")
    # Grenzen pruefen, BEVOR irgendetwas angelegt wird
    _budget(sum(len(_subtree(r)) for r in obs) * (total - 1))
    mn, mx = _tree_bbox(obs)
    reach = Vector([step[k] * (n[k] - 1) for k in range(3)])
    _within(mn + Vector([min(v, 0.0) for v in reach]), mx + Vector([max(v, 0.0) for v in reach]))
    made = []
    for root in obs:
        for iz in range(n[2]):
            for iy in range(n[1]):
                for ix in range(n[0]):
                    if ix == iy == iz == 0:
                        continue
                    off = Vector((ix * step.x, iy * step.y, iz * step.z))
                    made.append(_copy_tree(root, Matrix.Translation(off)).name)
    return {"created": len(made), "grid": n, "names": made[:50]}


def _mirror_pivot(obs, pivot):
    if isinstance(pivot, str) and pivot in ("self", "each"):
        return None
    if pivot == "group":
        mn, mx = _tree_bbox(obs)
        return (mn + mx) / 2
    if pivot == "origin":
        return Vector((0, 0, 0))
    if isinstance(pivot, list) and len(pivot) == 3:
        return _vec(pivot, "pivot")
    raise OpError("pivot muss self, group, origin oder [x, y, z] sein")


@op
def mirror(p):
    """Spiegeln an der Ebene durch pivot senkrecht zu axis (wie "Spiegeln entlang" in SketchUp).

    Die Geometrie bleibt geteilt, nur die Platzierung bekommt eine Spiegelung (Determinante -1).
    SketchUp und Blender zeigen die Vorderseiten dabei weiter nach aussen."""
    _fields(p, "mirror", ("select", "axis", "pivot"))
    obs = _selection(p)
    i = _axis(_need(p, "axis", '"axis": "x", "y" oder "z"'))
    f = [1.0, 1.0, 1.0]
    f[i] = -1.0
    m = Matrix.Diagonal((*f, 1.0))
    center = _mirror_pivot(obs, p.get("pivot", "self"))
    for o in obs:
        c = center
        if c is None:
            mn, mx = _tree_bbox([o])
            c = (mn + mx) / 2
        _apply_world(o, _around(c, m))
    return {"mirrored": len(obs), "axis": "xyz"[i]}


def _edge_value(mn, mx, i, where):
    if where == "min":
        return mn[i]
    if where == "max":
        return mx[i]
    return (mn[i] + mx[i]) / 2


def _inside(obj, roots):
    """Liegt obj in einem der Teilbaeume (oder ist selbst eine der Wurzeln)?"""
    chosen = set(roots)
    while obj is not None:
        if obj in chosen:
            return True
        obj = obj.parent
    return False


@op
def align(p):
    """Ausrichten der Boxen an min, Mitte oder max der gemeinsamen Box oder eines Bezugsobjekts."""
    _fields(p, "align", ("select", "axis", "to", "to_object"))
    obs = _selection(p)
    axes = _axes(_need(p, "axis", '"axis": "x", "y", "z" oder eine Liste wie ["x", "y"]'))
    where = p.get("to", "min")
    if not isinstance(where, str) or where not in ("min", "center", "max"):
        raise OpError('to muss "min", "center" oder "max" sein')
    if "to_object" in p:
        refs = _selection(p, "to_object")
        ref_set = set(refs)
        movers = [o for o in obs if o not in ref_set]
        if not movers:
            raise OpError("Nichts auszurichten: alle ausgewaehlten Objekte sind selbst der Bezug")
        if any(_inside(r, movers) for r in refs):
            raise OpError("Das Bezugsobjekt liegt in einem der auszurichtenden Objekte")
        rmn, rmx = _tree_bbox(refs)
    else:
        movers = obs
        rmn, rmx = _tree_bbox(obs)
    target = [_edge_value(rmn, rmx, i, where) for i in axes]
    for o in movers:
        mn, mx = _tree_bbox([o])
        d = Vector((0.0, 0.0, 0.0))
        for i, t in zip(axes, target):
            d[i] = t - _edge_value(mn, mx, i, where)
        _apply_world(o, Matrix.Translation(d))
    return {"aligned": len(movers), "axis": "".join("xyz"[i] for i in axes), "to": where,
            "value": [round(t, 6) for t in target]}


@op
def distribute(p):
    """Gleichmaessig verteilen entlang einer Achse, nach Mitten oder mit fester Luecke."""
    _fields(p, "distribute", ("select", "axis", "gap"))
    obs = _selection(p)
    i = _axis(_need(p, "axis", '"axis": "x", "y" oder "z"'))
    boxes = {o: _tree_bbox([o]) for o in obs}
    moves = {}
    if "gap" in p:
        if len(obs) < 2:
            raise OpError(f"distribute mit gap braucht mindestens 2 Objekte, gefunden: {len(obs)}")
        gap = _num(p["gap"], "gap")
        order = sorted(obs, key=lambda o: (boxes[o][0][i], o.name))
        pos = boxes[order[0]][1][i] + gap
        for o in order[1:]:
            mn, mx = boxes[o]
            moves[o] = pos - mn[i]
            pos = mx[i] + moves[o] + gap
        result = {"gap": round(gap, 6)}
    else:
        if len(obs) < 3:
            raise OpError(f"distribute nach Mitten braucht mindestens 3 Objekte, gefunden: {len(obs)} "
                          "(oder \"gap\" angeben)")
        mid = {o: (boxes[o][0][i] + boxes[o][1][i]) / 2 for o in obs}
        order = sorted(obs, key=lambda o: (mid[o], o.name))
        first, last = mid[order[0]], mid[order[-1]]
        step = (last - first) / (len(order) - 1)
        for k, o in enumerate(order[1:-1], 1):
            moves[o] = first + k * step - mid[o]
        result = {"step": round(step, 6)}
    for o, d in moves.items():
        mn, mx = boxes[o]
        shift = Vector((0.0, 0.0, 0.0))
        shift[i] = d
        _within(mn + shift, mx + shift)
    for o, d in moves.items():
        shift = Vector((0.0, 0.0, 0.0))
        shift[i] = d
        _apply_world(o, Matrix.Translation(shift))
    return {"distributed": len(obs), "axis": "xyz"[i], **result, "order": [o.name for o in order][:50]}


def _hidden_itself(o):
    try:
        return o.hide_get() or o.hide_viewport
    except RuntimeError:  # nicht in der aktiven Ansichtsebene
        return o.hide_viewport


def _set_hidden(o, hidden):
    try:
        o.hide_set(hidden)  # wie H / Alt+H in Blender (Auge im Outliner)
    except RuntimeError:  # nicht in der aktiven Ansichtsebene
        o.hide_viewport = hidden
    if not hidden:
        o.hide_viewport = False
    o.hide_render = hidden


def _visibility(p, hidden):
    name = "hide" if hidden else "show"
    _fields(p, name, ("select",))
    spec = p.get("select")
    if spec is None:
        roots = _top_level(list(bpy.context.scene.objects))
    else:
        roots = _selection(p)
    n = 0
    for root in roots:
        for o in _subtree(root):
            if hidden and o is not root:
                # schon vorher selbst verborgene Teile behalten in der .skp ihr eigenes Merkmal
                if HIDDEN_WITH_PARENT in o or not _hidden_itself(o):
                    o[HIDDEN_WITH_PARENT] = True
            elif HIDDEN_WITH_PARENT in o:
                del o[HIDDEN_WITH_PARENT]
            _set_hidden(o, hidden)
            n += 1
    return {"hidden" if hidden else "shown": n, "names": [r.name for r in roots][:50]}


@op
def hide(p):
    """Objekte samt Inhalt ausblenden (einzelne Objekte, nicht Ebenen)."""
    return _visibility(p, True)


@op
def show(p):
    """Objekte samt Inhalt wieder einblenden."""
    return _visibility(p, False)


def _rounded(v):
    return [round(x, 6) for x in v]


def _box(roots):
    """(Mitte, Beschreibung) der Box samt Inhalt, gerundet auf Mikrometer."""
    mn, mx = _tree_bbox(roots)
    c = (mn + mx) / 2
    return c, {"count": len(roots), "size": _rounded(mx - mn), "center": _rounded(c),
               "min": _rounded(mn), "max": _rounded(mx)}


@op
def measure(p):
    """Nur lesen: Box der Auswahl samt Inhalt; mit to_object auch Abstand der Mitten."""
    _fields(p, "measure", ("select", "to_object"))
    c1, out = _box(_selection(p))
    if "to_object" in p:
        c2, other = _box(_selection(p, "to_object"))
        delta = c2 - c1
        out.update(other=other, delta=_rounded(delta), distance=round(delta.length, 6))
    return out


@op
def add_box(p):
    size = _vec(p.get("size", [1, 1, 1]), "size", 1e-6, MAX_COORD)
    at = _vec(p.get("at", [0, 0, 0]), "at")  # Mitte der Unterseite
    name = _name(p.get("name", "Quader"), "name")
    _budget(1)
    me = bpy.data.meshes.new(name)
    x, y, z = size / 2
    v = [(-x, -y, 0), (x, -y, 0), (x, y, 0), (-x, y, 0),
         (-x, -y, size.z), (x, -y, size.z), (x, y, size.z), (-x, y, size.z)]
    f = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    me.from_pydata(v, [], f)
    ob = bpy.data.objects.new(name, me)
    ob.location = at
    col = bpy.context.scene.collection
    if p.get("layer"):
        layer = _name(p["layer"], "layer")
        col = bpy.data.collections.get(layer) or bpy.data.collections.new(layer)
        if col.name not in bpy.context.scene.collection.children:
            bpy.context.scene.collection.children.link(col)
    col.objects.link(ob)
    if p.get("material"):
        me.materials.append(_material(p["material"], p.get("color"), p.get("alpha")))
    return {"created": ob.name}


def run(operations, stop_on_error=True):
    """Liste von Operationen ausfuehren. Rueckgabe: Ergebnis je Operation."""
    if isinstance(operations, dict):
        operations = [operations]
    if not isinstance(operations, list):
        return [{"op": None, "ok": False, "index": 0,
                 "error": 'Operationen muessen eine Liste sein, z. B. [{"op": "summary"}]'}]
    if len(operations) > MAX_OPS:
        return [{"op": None, "ok": False, "index": 0,
                 "error": f"Hoechstens {MAX_OPS} Operationen pro Aufruf ({len(operations)} angegeben)"}]
    results = []
    for i, p in enumerate(operations):
        name = p.get("op") if isinstance(p, dict) else None
        name = name if isinstance(name, str) and len(name) <= 40 else None
        try:
            if not isinstance(p, dict):
                raise OpError("Jede Operation muss ein Objekt sein, z. B. {\"op\": \"summary\"}")
            fn = OPS.get(name)
            if fn is None:
                raise OpError(f"Unbekannte Operation {name!r}. Moeglich: {', '.join(sorted(OPS))}")
            results.append({"op": name, "ok": True, **fn(p)})
            bpy.context.view_layer.update()  # neue Positionen fuer die naechste Operation
        except Exception as exc:  # jeder Fehler endet hier, mit Meldung statt Absturz
            if isinstance(exc, OpError):
                msg = str(exc)
            elif isinstance(exc, KeyError):
                msg = f"fehlender Wert {exc}"
            else:
                msg = f"ungueltige Angabe ({type(exc).__name__}: {str(exc)[:200]})"
            results.append({"op": name, "ok": False, "error": msg, "index": i})
            if stop_on_error:
                break
    return results
