"""Verweise fremder 3D-Dateien auf andere Dateien pruefen, bevor und nachdem Blender sie laedt.

Zwei Gefahren bei Dateien aus fremder Hand:
  1. Netzwerkpfade (\\\\server\\freigabe\\bild.png, //server/..., file://server/...). Schon der
     Versuch, sie zu oeffnen, schickt unter Windows die Anmeldung (NTLM-Hash) an den Server.
     Solche Dateien werden IMMER abgelehnt, bevor Blender sie anfasst.
  2. Lokale Dateien ausserhalb der Datei (../../Bilder/privat.png, C:/Users/.../x.bin). Blender
     wuerde sie einlesen und sie landen in der Ausgabe, die man weitergibt. Ohne
     --allow-external werden solche Verweise abgelehnt (wenn die Datei ohne sie nicht ladbar ist,
     z. B. glTF mit externer .bin) oder nach dem Laden entfernt, bevor irgendetwas ausgewertet wird.

precheck() ist reines Python (fuer .blend mit zstd und fuer USD nutzt es zstandard bzw. pxr, beides
liegt Blender bei). strip_external() laeuft in Blender nach dem Laden.
"""
import gzip
import json
import os
import re
import struct
import urllib.parse
import zipfile

MAX_TEXT = 512 * 1024 * 1024        # groesste Text-/JSON-Datei, die geprueft wird
MAX_GLB_JSON = 64 * 1024 * 1024
MAX_DEPTH = 8                        # verschachtelte .blend-Bibliotheken bzw. .mtl
MAX_FILES = 64
CHUNK = 16 * 1024 * 1024
OVERLAP = 4096
ZIP_MAX_BYTES = 8 * 1024 ** 3
ZIP_MAX_ENTRIES = 100_000
ZIP_MAX_RATIO = 100

# Netzwerkpfad als C-Zeichenkette (in .blend: Pfadfelder sind nullterminiert). "//" ist in
# Blender ein relativer Pfad und wird hier bewusst NICHT gesucht.
_UNC_CSTRING = re.compile(rb"(?:^|(?<=[^\x20-\x7e]))(\\\\[\x21-\x7e][\x20-\x7e]{2,1100}?)(?=\x00)")
# Netzwerkpfad irgendwo in Binaerdaten (FBX, Alembic, DAE): mit / oder \, nicht nach "http:" usw.
_UNC_LOOSE = re.compile(rb"(?<![A-Za-z0-9:])(?:\\\\|//)(?:[?.][\\/](?:UNC[\\/])?)?"
                        rb"[A-Za-z0-9_$][A-Za-z0-9_.$@%-]{0,254}[\\/][\x21-\x7e]")
_URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]+:")


class RefError(ValueError):
    """Datei wird aus Sicherheitsgruenden nicht geladen (Meldung auf Deutsch)."""


def classify(path):
    """'empty', 'data' (eingebettet), 'network' oder 'local'."""
    if not isinstance(path, str) or not path.strip():
        return "empty"
    p = path.strip()
    low = p.lower()
    if low.startswith("data:"):
        return "data"
    for cand in (p, urllib.parse.unquote(p)):
        c = cand.replace("/", "\\")
        low = cand.lower()
        if low.startswith("file:"):
            rest = cand[5:]
            if rest.startswith("//"):
                host = rest[2:].split("/", 1)[0].split("\\", 1)[0]
                if host and host.lower() != "localhost":
                    return "network"
            continue
        if _URI_SCHEME.match(cand) and not re.match(r"^[A-Za-z]:[\\/]", cand):
            return "network"  # http:, smb:, ftp: usw. (Laufwerksbuchstaben ausgenommen)
        if c.startswith("\\\\"):
            if c.lower().startswith("\\\\?\\unc\\"):
                return "network"
            if c.startswith("\\\\?\\") or c.startswith("\\\\.\\"):
                continue  # lokales Geraet bzw. langer lokaler Pfad
            return "network"
    return "local"


def _inside(base_dir, rel):
    """Liegt der (relative) Pfad innerhalb von base_dir? Absolute Pfade nie."""
    if os.path.isabs(rel) or re.match(r"^[A-Za-z]:", rel) or rel.startswith(("/", "\\")):
        return False
    full = os.path.realpath(os.path.join(base_dir, rel))
    base = os.path.realpath(base_dir)
    return os.path.commonpath([full, base]) == base


class Report:
    def __init__(self):
        self.network = []     # immer verboten
        self.external = []    # lokale Dateien ausserhalb der Datei
        self.blocking = []    # externe Verweise, ohne die die Datei nicht ladbar ist
        self.files = 0

    def as_dict(self):
        return {"network": self.network, "external": self.external, "blocking": self.blocking}


# ---------------------------------------------------------------- Datenstroeme

def _open_stream(path):
    """Datei als (entpackten) Byte-Strom. .blend kann gzip- oder zstd-gepackt sein."""
    fh = open(path, "rb")
    magic = fh.read(4)
    fh.seek(0)
    if magic[:2] == b"\x1f\x8b":
        return gzip.GzipFile(fileobj=fh)
    if magic == b"\x28\xb5\x2f\xfd":
        try:
            import zstandard
        except ImportError:
            fh.close()
            raise RefError(f"{os.path.basename(path)} ist zstd-gepackt und kann hier nicht geprueft werden")
        return zstandard.ZstdDecompressor().stream_reader(fh, read_across_frames=True, closefd=True)
    return fh


def _scan_stream(stream, pattern, limit=None):
    """Alle Treffer (dekodiert) im Strom, blockweise mit Ueberlappung."""
    found, tail, total = [], b"", 0
    while True:
        chunk = stream.read(CHUNK)
        if not chunk:
            break
        total += len(chunk)
        buf = tail + chunk
        for m in pattern.finditer(buf):
            if m.end() > len(tail):  # nicht doppelt aus der Ueberlappung
                hit = (m.group(1) if m.groups() else m.group(0)).decode("latin-1")
                if hit not in found:
                    found.append(hit)
        tail = buf[-OVERLAP:]
        if limit and total > limit:
            break
    return found


def check_zip(path):
    """Grenzen fuer ZIP-Container (.usdz), wie core.check_container fuer .skp."""
    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
    except zipfile.BadZipFile:
        raise RefError(f"{os.path.basename(path)} ist kein gueltiges ZIP-Archiv") from None
    if len(infos) > ZIP_MAX_ENTRIES:
        raise RefError(f"{os.path.basename(path)}: zu viele Eintraege ({len(infos)})")
    total = sum(i.file_size for i in infos)
    if total > ZIP_MAX_BYTES:
        raise RefError(f"{os.path.basename(path)}: entpackt zu gross ({total / 1024 ** 3:.1f} GB)")
    for i in infos:
        if i.compress_size and i.file_size / max(i.compress_size, 1) > ZIP_MAX_RATIO and i.file_size > 1 << 20:
            raise RefError(f"{os.path.basename(path)}: Eintrag {i.filename!r} ist verdaechtig stark komprimiert")


# ---------------------------------------------------------------- .blend

def _blend_library_paths(path):
    """Pfade verlinkter .blend-Bibliotheken (LI-Bloecke), direkt aus der Datei gelesen."""
    paths = []
    with _open_stream(path) as fh:
        head = fh.read(7)
        if head != b"BLENDER":
            raise RefError(f"{os.path.basename(path)} ist keine .blend-Datei")
        b7 = fh.read(1)
        if b7 in (b"_", b"-"):
            ptr = 4 if b7 == b"_" else 8
            little = fh.read(1) == b"v"
            fh.read(3)
            e = "<" if little else ">"
            st = struct.Struct(e + ("4siIii" if ptr == 4 else "4siQii"))
            order = ("code", "len")
        else:
            size = int(b7 + fh.read(1))
            if size != 17:
                raise RefError(f"{os.path.basename(path)}: unbekannter .blend-Kopf")
            fh.read(size - 9)
            st = struct.Struct("<4siQqq")
            order = ("code", "sdna")
        while True:
            raw = fh.read(st.size)
            if len(raw) < st.size:
                break
            vals = st.unpack(raw)
            code = vals[0]
            length = vals[1] if order[1] == "len" else vals[3]
            if code == b"ENDB" or length < 0:
                break
            if code[:2] == b"LI":
                if length > 1 << 20:
                    raise RefError(f"{os.path.basename(path)}: ungueltiger Bibliotheksblock")
                data = fh.read(length)
                for s in re.findall(rb"[\x20-\x7e]{3,1100}(?=\x00)", data):
                    if not s.lower().endswith(b".blend"):
                        continue
                    if s.startswith(b"LI") and not re.search(rb"[\\/]", s):
                        continue  # Name der Bibliothek (ID-Name), kein Pfad
                    paths.append(s.decode("latin-1"))
            else:
                left = length
                while left > 0:  # ueberspringen (Strom kann nicht immer seek)
                    got = fh.read(min(left, CHUNK))
                    if not got:
                        break
                    left -= len(got)
    return paths


def _blend_abspath(p, blend_dir):
    if p.startswith("//"):
        return os.path.normpath(os.path.join(blend_dir, p[2:].lstrip("/\\")))
    return p


def _check_blend(path, rep, depth, seen):
    with _open_stream(path) as fh:
        for hit in _scan_stream(fh, _UNC_CSTRING):
            if classify(hit) == "network":
                rep.network.append(f"{os.path.basename(path)}: {hit}")
    for lib in _blend_library_paths(path):
        if classify(lib if not lib.startswith("//") else "x") == "network":
            rep.network.append(f"{os.path.basename(path)}: Bibliothek {lib}")
            continue
        full = _blend_abspath(lib, os.path.dirname(path))
        rep.external.append(full)
        # Bibliotheken laedt Blender schon beim Oeffnen: auch sie vorher pruefen
        if depth < MAX_DEPTH and os.path.isfile(full) and os.path.realpath(full) not in seen:
            seen.add(os.path.realpath(full))
            if len(seen) > MAX_FILES:
                raise RefError("zu viele verlinkte .blend-Bibliotheken")
            _check_blend(full, rep, depth + 1, seen)


# ---------------------------------------------------------------- glTF

def _gltf_json(path):
    with open(path, "rb") as fh:
        if path.lower().endswith(".glb"):
            head = fh.read(20)
            if len(head) < 20 or head[:4] != b"glTF":
                raise RefError(f"{os.path.basename(path)} ist keine GLB-Datei")
            length, ctype = struct.unpack("<II", head[12:20])
            if ctype != 0x4E4F534A or length > MAX_GLB_JSON:
                raise RefError(f"{os.path.basename(path)}: ungueltiger GLB-JSON-Teil")
            raw = fh.read(length)
        else:
            raw = fh.read(MAX_TEXT + 1)
            if len(raw) > MAX_TEXT:
                raise RefError(f"{os.path.basename(path)} ist zu gross")
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise RefError(f"{os.path.basename(path)}: glTF-JSON ist ungueltig") from None


def _walk_uris(node, out, depth=0):
    if depth > 64:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "uri" and isinstance(v, str):
                out.append(v)
            else:
                _walk_uris(v, out, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _walk_uris(v, out, depth + 1)


def _check_gltf(path, rep):
    uris = []
    _walk_uris(_gltf_json(path), uris)
    for u in uris:
        kind = classify(u)
        if kind == "network":
            rep.network.append(u[:200])
        elif kind == "local":
            rep.blocking.append(urllib.parse.unquote(u)[:200])


# ---------------------------------------------------------------- OBJ / MTL

_MTL_MAPS = ("map_", "bump", "disp", "decal", "refl", "norm")


def _text_lines(path):
    with open(path, "rb") as fh:
        raw = fh.read(MAX_TEXT + 1)
    if len(raw) > MAX_TEXT:
        raise RefError(f"{os.path.basename(path)} ist zu gross")
    return raw.decode("utf-8", "replace").splitlines()


def _check_obj(path, rep):
    folder = os.path.dirname(os.path.abspath(path))
    with open(path, "rb") as fh:  # mtllib steht praktisch immer oben, aber ganze Datei pruefen
        hits = _scan_stream(fh, re.compile(rb"(?m)^[ \t]*mtllib[ \t]+([^\r\n]{1,2048})"))
    for hit in hits:
        cands = [hit.strip()] + hit.split()
        net = [c for c in cands if classify(c) == "network"]
        if net:
            rep.network.append(f"mtllib {net[0]}")
            continue
        for name in cands:
            if classify(name) != "local":
                continue
            if not _inside(folder, name):
                rep.blocking.append(f"mtllib {name}")
                break
            full = os.path.join(folder, name)
            if os.path.isfile(full):
                _check_mtl(full, rep)
                break


def _check_mtl(path, rep):
    for line in _text_lines(path):
        s = line.strip()
        if not s or not s.lower().startswith(_MTL_MAPS):
            continue
        parts = s.split()[1:]
        cands = [" ".join(parts)] + parts
        for c in cands:
            if classify(c) == "network":
                rep.network.append(f"{os.path.basename(path)}: {c}")
                break
        else:
            rep.external.append(parts[-1] if parts else "")


# ---------------------------------------------------------------- USD

def _usd_asset_paths(layer):
    from pxr import Sdf
    found = list(layer.subLayerPaths)
    try:
        found += list(layer.GetCompositionAssetDependencies())
    except AttributeError:
        found += list(layer.GetExternalReferences())

    def values(v):
        if isinstance(v, Sdf.AssetPath):
            yield v.path
        elif isinstance(v, (Sdf.AssetPathArray, list, tuple)):
            for x in v:
                if isinstance(x, Sdf.AssetPath):
                    yield x.path

    def visit(p):
        spec = layer.GetObjectAtPath(p)
        if isinstance(spec, Sdf.AttributeSpec):
            if spec.HasDefaultValue():
                found.extend(values(spec.default))
            for t in layer.ListTimeSamplesForPath(p):
                found.extend(values(layer.QueryTimeSample(p, t)))
        elif isinstance(spec, Sdf.PrimSpec):
            for lst in (spec.referenceList, spec.payloadList):
                for item in list(lst.explicitItems) + list(lst.prependedItems) + list(lst.appendedItems):
                    found.append(item.assetPath)

    layer.Traverse(Sdf.Path.absoluteRootPath, visit)
    return [f for f in found if f]


def _check_usd(path, rep):
    if path.lower().endswith(".usdz"):
        check_zip(path)
    try:
        from pxr import Sdf
    except ImportError:
        raise RefError("USD-Dateien lassen sich nur in Blender pruefen") from None
    layer = Sdf.Layer.FindOrOpen(path)
    if layer is None:
        raise RefError(f"{os.path.basename(path)} ist keine lesbare USD-Datei")
    packaged = path.lower().endswith(".usdz")
    for a in _usd_asset_paths(layer):
        kind = classify(a)
        if kind == "network":
            rep.network.append(a[:200])
        elif kind == "local":
            if packaged and not os.path.isabs(a) and ".." not in re.split(r"[\\/]", a) \
                    and not re.match(r"^[A-Za-z]:", a):
                continue  # liegt im .usdz selbst
            rep.blocking.append(a[:200])


# ---------------------------------------------------------------- Einstieg

def precheck(path, allow_external=False):
    """Vor dem Laden: Netzwerkpfade immer ablehnen, externe Pflicht-Verweise ohne Erlaubnis
    ablehnen. Rueckgabe: Report. Wirft RefError mit deutscher Meldung."""
    path = os.path.abspath(path)
    try:
        return _precheck(path, allow_external)
    except RefError:
        raise
    except Exception as exc:  # was sich nicht pruefen laesst, wird nicht geoeffnet
        raise RefError(f"{os.path.basename(path)} laesst sich nicht auf Verweise pruefen "
                       f"({type(exc).__name__}: {str(exc)[:120]}) und wird deshalb nicht geoeffnet") from None


def _precheck(path, allow_external):
    ext = os.path.splitext(path)[1].lower()
    rep = Report()
    if ext == ".blend":
        _check_blend(path, rep, 0, {os.path.realpath(path)})
    elif ext in (".gltf", ".glb"):
        _check_gltf(path, rep)
    elif ext == ".obj":
        _check_obj(path, rep)
    elif ext in (".usd", ".usda", ".usdc", ".usdz"):
        _check_usd(path, rep)
    elif ext in (".fbx", ".abc", ".dae"):
        with open(path, "rb") as fh:
            for hit in _scan_stream(fh, _UNC_LOOSE):
                if classify(hit) == "network":
                    rep.network.append(hit[:200])
    # .stl, .ply: keine Verweise
    if rep.network:
        raise RefError("Die Datei verweist auf Netzwerkpfade und wird aus Sicherheitsgruenden nicht "
                       "geoeffnet (schon der Zugriff wuerde Anmeldedaten an fremde Server senden): "
                       + ", ".join(rep.network[:5]))
    if rep.blocking and not allow_external:
        raise RefError("Die Datei braucht Dateien ausserhalb von sich selbst ("
                       + ", ".join(rep.blocking[:5]) + "). Das koennten beliebige Dateien dieses "
                       "Rechners sein, die dann in der Ausgabe landen. Nur wenn die Datei aus sicherer "
                       "Quelle stammt: --allow-external")
    return rep


# ---------------------------------------------------------------- nach dem Laden (in Blender)

_PATH_SUBTYPES = {"FILE_PATH", "DIR_PATH", "FILE_NAME", "DIRPATH", "FILEPATH"}


def _path_props(rna_obj, depth=0, seen=None):
    """(Name, Wert) aller nicht leeren Pfad-Eigenschaften, auch in verschachtelten Einstellungen."""
    import bpy
    if rna_obj is None or depth > 3:
        return []
    seen = seen if seen is not None else set()
    try:
        key = rna_obj.as_pointer()
    except Exception:
        key = id(rna_obj)
    if key in seen:
        return []
    seen.add(key)
    out = []
    for prop in rna_obj.bl_rna.properties:
        ident = prop.identifier
        if ident in ("rna_type",):
            continue
        try:
            if prop.type == "STRING" and prop.subtype in _PATH_SUBTYPES:
                val = getattr(rna_obj, ident)
                if val:
                    out.append((ident, val))
            elif prop.type == "POINTER":
                sub = getattr(rna_obj, ident)
                if sub is not None and not isinstance(sub, bpy.types.ID):
                    out += _path_props(sub, depth + 1, seen)
        except Exception:
            continue
    return out


def strip_external(allow_external=False):
    """Nach dem Laden, vor jeder Auswertung: externe Dateien entfernen (ohne --allow-external).

    Rueckgabe: Liste der gefundenen Verweise (Text). Mit allow_external wird nur berichtet."""
    import bpy
    found, drop = [], not allow_external

    for lib in list(bpy.data.libraries):
        found.append(f"Bibliothek {bpy.path.abspath(lib.filepath)}")
        if drop:
            bpy.data.libraries.remove(lib)
    for coll_name, keep in (("images", lambda i: i.packed_file is not None or i.source not in
                             {"FILE", "SEQUENCE", "MOVIE", "TILED"}),
                            ("sounds", lambda s: s.packed_file is not None),
                            ("movieclips", lambda m: False),
                            ("fonts", lambda f: f.packed_file is not None or f.filepath == "<builtin>"),
                            ("cache_files", lambda c: False),
                            ("volumes", lambda v: v.packed_file is not None if hasattr(v, "packed_file") else False)):
        coll = getattr(bpy.data, coll_name, None)
        if coll is None:
            continue
        for item in list(coll):
            fp = getattr(item, "filepath", "")
            if not fp or keep(item):
                continue
            found.append(f"{coll_name}: {bpy.path.abspath(fp)}")
            if drop:
                coll.remove(item)
    for txt in list(bpy.data.texts):
        if getattr(txt, "filepath", "") and not txt.is_in_memory:
            found.append(f"Text {bpy.path.abspath(txt.filepath)}")
            if drop:
                bpy.data.texts.remove(txt)

    # Modifikatoren mit Dateipfaden (Caches, Bakes, Ozean usw.) lesen beim Auswerten von der Platte
    for ob in list(bpy.data.objects):
        for mod in list(getattr(ob, "modifiers", [])):
            paths = _path_props(mod)
            if paths:
                found.append(f"{ob.name}/{mod.name}: {paths[0][1]}")
                if drop:
                    ob.modifiers.remove(mod)
    # Knoten, die Dateien lesen (Geometry-Nodes-Import, externe Skripte)
    trees = list(bpy.data.node_groups)
    for idc in (bpy.data.materials, bpy.data.worlds, bpy.data.lights, bpy.data.scenes):
        for idb in idc:
            nt = getattr(idb, "node_tree", None)
            if nt is not None:
                trees.append(nt)
    for nt in trees:
        for node in list(nt.nodes):
            reads = node.bl_idname.startswith("GeometryNodeImport") or bool(_path_props(node))
            if not reads:
                for s in getattr(node, "inputs", []):
                    if s.type == "STRING" and getattr(s, "subtype", "") in _PATH_SUBTYPES \
                            and getattr(s, "default_value", ""):
                        reads = True
                        break
            if reads:
                found.append(f"Knoten {nt.name}/{node.name}")
                if drop:
                    nt.nodes.remove(node)
    return found
