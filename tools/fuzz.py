"""Deterministischer Fuzzer fuer skptool: feindliche Eingaben an jede Schnittstelle.

Nur Standardbibliothek und numpy, keine neuen Abhaengigkeiten. Alles ist ueber einen Startwert
(--seed) wiederholbar. Jeder Fall wird gegen ein Orakel geprueft:

  - Er endet innerhalb eines Zeitbudgets (Standard 60 s, Blender 180 s).
  - Bei kaputter Eingabe endet das Programm mit einem Rueckgabewert ungleich 0 und genau einer
    lesbaren deutschen Fehlerzeile, nie mit einem Python-Traceback auf dem Terminal.
  - Die Speicherspitze bleibt unter einer Grenze (Windows: Job-Objekt, sonst ru_maxrss).
  - Es wird keine Datei ausserhalb des Zielordners geschrieben und keine unvollstaendige
    Ausgabedatei zurueckgelassen.

Aufruf (Beispiele):

  python tools/fuzz.py --cases 2000                 # schnelle Ziele, alle Muster
  python tools/fuzz.py --target rewrite --cases 500
  python tools/fuzz.py --target mcp --cases 300
  python tools/fuzz.py --target blender --cases 12  # langsam, braucht Blender
  python tools/fuzz.py --list                        # verfuegbare Ziele

Findet der Lauf etwas, schreibt er den kleinsten Ausloeser nach --outdir und meldet ihn.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SAMPLES_DIR = ROOT / "samples"
EXTERN_DIR = SAMPLES_DIR / "extern"
PY = sys.executable
TRACEBACK_MARKER = "Traceback (most recent call last)"

# Grenzen des Orakels
ZEIT_BUDGET = 60.0
ZEIT_BUDGET_BLENDER = 180.0
RSS_GRENZE_MB = 4096          # eine Umwandlung darf nicht mehr Speicher ziehen
RSS_GRENZE_BLENDER_MB = 8192


# ---------------------------------------------------------------- Mutator

class Mutator:
    """Deterministische Byte-Mutationen ueber einen seed. Alle Strategien aus der Aufgabe."""

    def __init__(self, seed: int):
        self.rng = np.random.default_rng(seed)

    def _pos(self, data) -> int:
        return int(self.rng.integers(0, len(data))) if data else 0

    def bit_flip(self, s: bytearray) -> None:
        if s:
            p = self._pos(s)
            s[p] ^= 1 << int(self.rng.integers(0, 8))

    def byte_insert(self, s: bytearray) -> None:
        p = self._pos(s)
        n = int(self.rng.integers(1, 32))
        s[p:p] = bytes(self.rng.integers(0, 256, n, dtype=np.uint8).tolist())

    def byte_delete(self, s: bytearray) -> None:
        if s:
            p = self._pos(s)
            del s[p:p + int(self.rng.integers(1, 16))]

    def chunk_dup(self, s: bytearray) -> None:
        if s:
            q = self._pos(s)
            p = self._pos(s)
            s[p:p] = bytes(s[q:q + int(self.rng.integers(1, 128))])

    def corrupt_length(self, s: bytearray) -> None:
        """Ein kleines Feld (meist ein Laengen- oder Zahlwert) mit Zufallsbytes ueberschreiben."""
        if len(s) < 5:
            return
        p = int(self.rng.integers(0, len(s) - 4))
        n = int(self.rng.integers(1, 5))
        for k in range(n):
            s[p + k] = int(self.rng.integers(0, 256))

    def truncate(self, s: bytearray) -> None:
        if s:
            del s[self._pos(s):]

    STRATEGIEN = (bit_flip, byte_insert, byte_delete, chunk_dup, corrupt_length, truncate)

    def mutate(self, data: bytes, runden: int | None = None) -> bytes:
        s = bytearray(data)
        runden = runden if runden is not None else int(self.rng.integers(1, 12))
        for _ in range(runden):
            self.STRATEGIEN[int(self.rng.integers(0, len(self.STRATEGIEN)))](self, s)
        return bytes(s)


# ---------------------------------------------------------------- Generatoren feindlicher Dateien

def hostile_gltf(rng: np.random.Generator) -> bytes:
    """Feindliches glTF (JSON): tiefe Verschachtelung, riesige Arrays, boese Verweise, NaN-Text."""
    tief = {"a": 1}
    for _ in range(int(rng.integers(0, 200))):
        tief = {"n": tief}
    doc = {
        "asset": {"version": "2.0"},
        "images": [{"uri": rng.choice(["\\\\server\\share\\x.png", "//host/x.png", "../../../etc/passwd",
                                        "file://host/x", "data:image/png;base64,AAAA", "x" * 5000])}],
        "buffers": [{"uri": rng.choice(["b.bin", "../../secret.bin", "\\\\srv\\b.bin"]),
                     "byteLength": int(rng.integers(-5, 2**31))}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": int(rng.integers(-2, 99))}}]}],
        "nodes": [{"mesh": 0, "children": list(range(int(rng.integers(0, 50))))}],
        "extra": tief,
    }
    return json.dumps(doc).encode("utf-8", "surrogatepass")


def hostile_glb(rng: np.random.Generator) -> bytes:
    js = hostile_gltf(rng)
    js += b" " * (-len(js) % 4)
    laenge = rng.choice([len(js), 2**31, 0xFFFFFFFF, 8])
    return struct.pack("<III", 0x46546C67, 2, int(rng.integers(0, 2**31))) + \
        struct.pack("<II", int(laenge), 0x4E4F534A) + js


def hostile_obj(rng: np.random.Generator) -> bytes:
    zeilen = [b"mtllib " + rng.choice([b"\\\\srv\\a.mtl", b"../a.mtl", b"a.mtl", b"x" * 3000])]
    for _ in range(int(rng.integers(0, 60))):
        zeilen.append(b"v " + b" ".join(rng.choice([b"0", b"nan", b"inf", b"1e400", b"x"]) for _ in range(3)))
        zeilen.append(b"f " + b" ".join(str(int(rng.integers(-9, 999))).encode() for _ in range(3)))
    return b"\n".join(zeilen)


def hostile_mtl(rng: np.random.Generator) -> bytes:
    return b"\n".join(b"map_Kd " + rng.choice([b"\\\\srv\\t.png", b"../../t.png", b"t.png", b"C:\\x\\t.png"])
                      for _ in range(int(rng.integers(1, 20))))


def hostile_ply(rng: np.random.Generator) -> bytes:
    kopf = (b"ply\nformat ascii 1.0\nelement vertex %d\nproperty float x\nproperty float y\n"
            b"property float z\nelement face %d\nproperty list uchar int vertex_indices\nend_header\n"
            % (int(rng.integers(-5, 10**9)), int(rng.integers(-5, 10**9))))
    return kopf + bytes(rng.integers(0, 256, int(rng.integers(0, 200)), dtype=np.uint8).tolist())


def hostile_stl(rng: np.random.Generator) -> bytes:
    dreiecke = int(rng.integers(0, 2**31))  # riesige angekuendigte Anzahl (Laengenfeld)
    return b"\x00" * 80 + struct.pack("<I", dreiecke) + \
        bytes(rng.integers(0, 256, int(rng.integers(0, 200)), dtype=np.uint8).tolist())


def hostile_dxf(rng: np.random.Generator) -> bytes:
    teile = [b"0\nSECTION\n2\nENTITIES\n"]
    for _ in range(int(rng.integers(0, 40))):
        teile.append(b"0\n3DFACE\n10\n" + rng.choice([b"nan", b"1e400", b"0", b"x"]) + b"\n")
    return b"".join(teile) + b"0\nENDSEC\n0\nEOF\n"


def hostile_zip(rng: np.random.Generator) -> bytes:
    """ZIP-Container-Angriffe: Zip-Bombe, verschachtelt, riesige angekuendigte Groessen, Pfad-Traversal,
    viele Eintraege. Als .skp verpackt (SketchUp ab 2021 ist ein ZIP)."""
    buf = io.BytesIO()
    art = int(rng.integers(0, 6))
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if art == 0:  # Bombe: viele Nullen, hoch komprimiert (Verhaeltnis weit ueber der Grenze)
            z.writestr("model.dat", b"\x00" * int(rng.integers(1_200_000, 4_000_000)))
        elif art == 1:  # viele Eintraege
            for k in range(int(rng.integers(100, 3000))):
                z.writestr(f"materials/e{k}/material.xml", b"<x/>")
        elif art == 2:  # Pfad-Traversal
            z.writestr("../../evil.dat", b"x")
            z.writestr("model.dat", b"x")
        elif art == 3:  # verschachteltes ZIP
            inner = io.BytesIO()
            with zipfile.ZipFile(inner, "w", zipfile.ZIP_DEFLATED) as iz:
                iz.writestr("model.dat", b"\x00" * 2_000_000)
            z.writestr("nested.skp", inner.getvalue())
            z.writestr("model.dat", b"x")
        elif art == 4:  # riesige Namen und Kommentare
            z.writestr("m" * 40000 + "/model.dat", b"x")
        else:  # normal aussehende, aber leere Struktur
            z.writestr("model.dat", bytes(rng.integers(0, 256, 200, dtype=np.uint8).tolist()))
    data = bytearray(buf.getvalue())
    if int(rng.integers(0, 2)):  # angekuendigte Groessen im lokalen Kopf verdrehen
        Mutator(int(rng.integers(0, 2**31))).corrupt_length(data)
    # SketchUp-Kopf davor, damit header_version etwas findet
    return b"\xff\xfe\xff\x0e" + "SketchUp Model{21.0.0}".encode("utf-16-le") + bytes(data)


HOSTILE = {
    ".gltf": hostile_gltf, ".glb": hostile_glb, ".obj": hostile_obj, ".mtl": hostile_mtl,
    ".ply": hostile_ply, ".stl": hostile_stl, ".dxf": hostile_dxf, ".skp": hostile_zip,
}


def hostile_ops(rng: np.random.Generator) -> bytes:
    """Feindliches Operations-JSON: tiefe Verschachtelung, riesige Arrays, NaN/Infinity, Unicode-Steuer-
    und Bidi-Zeichen, ueberlange Namen."""
    art = int(rng.integers(0, 7))
    if art == 0:
        return b"[" * int(rng.integers(1, 6000))
    if art == 1:
        return b'[' + b'0,' * int(rng.integers(1, 200000)) + b'0]'
    if art == 2:
        return json.dumps([{"op": "move", "by": [float("nan")]}]).encode()  # wird zu NaN im Text
    if art == 3:
        boese = "\u202e\u200b\u0000\u2028name" + "x" * int(rng.integers(0, 5000))
        return json.dumps([{"op": "rename", "select": {"name": boese}, "to": boese}]).encode("utf-8", "surrogatepass")
    if art == 4:
        return ('[{"op": "scale", "factor": 1e400}]').encode()
    if art == 5:
        return bytes(rng.integers(0, 256, int(rng.integers(0, 400)), dtype=np.uint8).tolist())
    return json.dumps([{"op": "x"}] * int(rng.integers(1, 3000))).encode()


def hostile_jsonrpc(rng: np.random.Generator) -> bytes:
    """Eine JSON-RPC-Zeile fuer den MCP-Server: fehlerhaft, uebergross, falsche Typen, unbekannte
    Methoden, id-Spiele."""
    art = int(rng.integers(0, 10))
    if art == 0:
        return b'{"jsonrpc":"2.0"}'  # ohne method/id
    if art == 1:
        return json.dumps({"jsonrpc": "2.0", "id": {"boese": 1}, "method": "tools/call"}).encode()
    if art == 2:
        return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "x" * 5000}).encode()
    if art == 3:
        return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "skp_info", "arguments": {"path": 123}}}).encode()
    if art == 4:
        return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": "kein objekt"}).encode()
    if art == 5:
        return json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"}] * 3).encode()  # Batch
    if art == 6:
        return b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":' + \
            str(int(rng.integers(0, 2**40))).encode() + b'}}'
    if art == 7:
        return bytes(rng.integers(0, 256, int(rng.integers(0, 200)), dtype=np.uint8).tolist())
    if art == 8:
        return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "skp_edit", "arguments": {
                               "input": "a.skp", "output": "b.skp",
                               "ops": [{"op": "scale", "factor": float("inf")}]}}}).encode()
    return json.dumps({"jsonrpc": "2.0", "id": 1.5, "method": "ping"}).encode()  # id als float


# ---------------------------------------------------------------- Live-Server in-Prozess (ohne Blender)

class _Stub:
    """Ersatz fuer alles aus bpy/mathutils: jedes Attribut und jeder Aufruf liefert wieder einen Stub."""

    def __getattr__(self, name):
        return _Stub()

    def __call__(self, *a, **kw):
        return _Stub()

    def __iter__(self):
        return iter(())

    def __bool__(self):
        return False


class LiveServerImProzess:
    """blender_scripts/live_server.py mit Ersatz-bpy laden und seinen echten TCP-Teil starten
    (serve, _read_request, HMAC-Pruefung, Warteschlange). Blender-Befehle sind durch ping ersetzt.
    Die Datei ruft am Ende main() auf; geladen wird deshalb eine Kopie ohne diese letzte Zeile."""

    def __init__(self, lesefrist: float = 2.0):
        import argparse as _ap
        import importlib.util
        import shutil
        import threading
        import types

        quelle_dir = ROOT / "skptool" / "blender_scripts"
        text = (quelle_dir / "live_server.py").read_text(encoding="utf-8").rstrip()
        if not text.endswith("main()"):
            raise RuntimeError("live_server.py endet nicht mehr mit main(), Fuzz-Harness anpassen")
        self.tmp = Path(tempfile.mkdtemp(prefix="fuzz_live_"))
        (self.tmp / "live_server_fuzz.py").write_text(text[: -len("main()")] + "\n", encoding="utf-8")
        shutil.copy(quelle_dir / "ops.py", self.tmp / "ops.py")

        def modul(name, **attrs):
            m = types.ModuleType(name)
            m.__getattr__ = lambda n: _Stub()  # PEP 562
            for k, v in attrs.items():
                setattr(m, k, v)
            return m

        handlers = modul("bpy.app.handlers", persistent=lambda f: f)
        app = modul("bpy.app", handlers=handlers, background=True)
        typen = modul("bpy.types", Operator=object)
        bpy = modul("bpy", app=app, types=typen)
        mathutils = modul("mathutils", Vector=_Stub(), Matrix=_Stub())
        ersatz = {"bpy": bpy, "bpy.app": app, "bpy.app.handlers": handlers, "bpy.types": typen,
                  "mathutils": mathutils}
        alt = {k: sys.modules.get(k) for k in [*ersatz, "ops"]}
        sys.modules.update(ersatz)
        try:
            spec = importlib.util.spec_from_file_location("skptool_live_server_fuzz",
                                                          self.tmp / "live_server_fuzz.py")
            self.mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.mod)
        finally:
            for k, v in alt.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
        self.mod.READ_TIMEOUT = lesefrist
        self.mod.COMMANDS = {"ping": lambda live, req: {"pong": True}}
        self.lesefrist = lesefrist
        args = _ap.Namespace(state=str(self.tmp / "live.json"), export_skp=None, python_exe=None,
                             project_root=None)
        self.live = self.mod.Live(args)
        self.token, self.port = self.live.token, self.live.port
        threading.Thread(target=self.live.serve, daemon=True).start()
        threading.Thread(target=self._arbeiter, daemon=True).start()

    def _arbeiter(self):
        while not self.live.stopping:
            try:
                job = self.live.jobs.get(timeout=0.2)
            except Exception:  # noqa: BLE001 - queue.Empty
                continue
            self.live._run_job(job)

    def mac(self, *teile):
        return self.mod.mac(self.token, *teile)

    def verbinde(self):
        import socket
        c = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        return c, json.loads(_zeile(c))["hello"]

    def anfrage(self, cmd="ping", **extra):
        """Gueltige, angemeldete Anfrage; Rueckgabe (Antwort, Server-Nonce, gesendete Zeile)."""
        c, sn = self.verbinde()
        with c:
            cn = "a" * 32
            msg = {"cmd": cmd, "cnonce": cn, "auth": self.mac("client", sn, cn), **extra}
            roh = json.dumps(msg).encode() + b"\n"
            c.sendall(roh)
            return json.loads(_zeile(c)), sn, roh

    def schliessen(self):
        import shutil
        self.live.stopping = True
        try:
            self.live.sock.close()
        except OSError:
            pass
        shutil.rmtree(self.live.tmp, ignore_errors=True)
        shutil.rmtree(self.tmp, ignore_errors=True)


def client_gegen_tropfserver(live_modul, frist: float = 25.0, takt: float = 0.5):
    """skptool.live.request gegen einen Server, der alle takt Sekunden ein Byte ohne Zeilenende
    schickt. Rueckgabe (Sekunden, Name der Ausnahme oder 'haengt')."""
    import socket
    import threading

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    stop = threading.Event()

    def tropfen():
        try:
            c, _ = srv.accept()
        except OSError:
            return
        with c:
            while not stop.is_set():
                try:
                    c.sendall(b"x")
                except OSError:
                    return
                stop.wait(takt)

    threading.Thread(target=tropfen, daemon=True).start()
    tmp = Path(tempfile.mkdtemp(prefix="fuzz_drip_"))
    zustand = tmp / "live.json"
    zustand.write_text(json.dumps({"protocol": 2, "port": srv.getsockname()[1], "token": "t" * 43,
                                   "pid": os.getpid()}), encoding="utf-8")
    if os.name != "nt":
        os.chmod(zustand, 0o600)
    erg = {}

    def rufe():
        t = time.time()
        try:
            live_modul.request("ping", state=zustand, timeout=5.0)
            erg["e"] = "ok"
        except Exception as exc:  # noqa: BLE001
            erg["e"] = type(exc).__name__
        erg["t"] = time.time() - t

    th = threading.Thread(target=rufe, daemon=True)
    th.start()
    th.join(frist)
    stop.set()
    srv.close()
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    if th.is_alive():
        return frist, "haengt"
    return erg["t"], erg["e"]


def _zeile(conn, grenze=2**21) -> bytes:
    buf = bytearray()
    while b"\n" not in buf:
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf += chunk
        if len(buf) > grenze:
            raise ValueError("Antwort zu gross")
    return bytes(buf).split(b"\n", 1)[0]


def live_rahmen(rng: np.random.Generator) -> bytes:
    """Feindliche Rahmen fuer den Live-Server (vor der Anmeldung)."""
    art = int(rng.integers(0, 8))
    if art == 0:
        return bytes(rng.integers(0, 256, int(rng.integers(0, 3000)), dtype=np.uint8).tolist()) + b"\n"
    if art == 1:
        return b"[" * int(rng.integers(1000, 200000)) + b"\n"
    if art == 2:
        return b'{"cmd":"ping","cnonce":"' + b"a" * int(rng.integers(0, 300)) + b'","auth":"' + \
            b"0" * int(rng.integers(0, 100)) + b'"}\n'
    if art == 3:
        return json.dumps({"cmd": ["ping"], "cnonce": {"x": 1}, "auth": None, "timeout": "NaN"}).encode() + b"\n"
    if art == 4:
        return b'{"cmd":"ping","timeout":NaN,"cnonce":"' + b"b" * 32 + b'","auth":"' + b"f" * 64 + b'"}\n'
    if art == 5:
        return b"\xff\xfe" + "{\"cmd\": \"ping\"}".encode("utf-16-le") + b"\n"
    if art == 6:
        return b"x" * int(rng.integers(1, 5000))  # ohne Zeilenende, dann schliessen
    return b"\n"


# ---------------------------------------------------------------- Ausfuehren und Orakel

def _peak_rss(job) -> int:
    """Windows: Speicherspitze des Job-Objekts, sonst 0 (dann ru_maxrss der Kinder)."""
    try:
        from skptool import stapel
        if hasattr(job, "peak"):
            return job.peak()
    except Exception:
        pass
    return 0


def lauf_prozess(args: list[str], budget: float, rss_grenze_mb: int, cwd: Path) -> dict:
    """Ein CLI-Aufruf als eigener Prozess mit Zeit- und Speicherwacht. Rueckgabe: Orakel-Befund."""
    from skptool import stapel

    env = dict(os.environ)
    rest = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p and os.path.isabs(p)]
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), *[p for p in rest if Path(p) != ROOT]])
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [PY, "-P", "-m", "skptool", *args]
    logpfad = cwd / "_log.txt"
    peak = 0
    with open(logpfad, "wb") as log:
        proc = stapel.Prozess(cmd, env, log, log)
        ende = time.time() + budget
        rc = None
        while True:
            rc = proc.poll()
            if rc is not None:
                break
            p = _peak_rss(proc)
            if p:
                peak = max(peak, p)
            if time.time() > ende:
                proc.beenden()
                proc.schliessen()
                return {"symptom": "Zeitueberschreitung", "rc": None, "peak_mb": peak // 2**20, "log": ""}
            time.sleep(0.02)
        p = _peak_rss(proc)
        if p:
            peak = max(peak, p)
        proc.beenden()
        proc.schliessen()
    log_text = logpfad.read_text("utf-8", "replace") if logpfad.exists() else ""
    peak_mb = peak // 2**20
    if TRACEBACK_MARKER in log_text:
        return {"symptom": "Traceback", "rc": rc, "peak_mb": peak_mb, "log": log_text[-600:]}
    if peak_mb > rss_grenze_mb:
        return {"symptom": f"Speicher {peak_mb} MB ueber Grenze {rss_grenze_mb} MB", "rc": rc,
                "peak_mb": peak_mb, "log": ""}
    return {"symptom": None, "rc": rc, "peak_mb": peak_mb, "log": log_text[-600:]}


_ROHE_AUSNAHME = re.compile(r"\b(?:[A-Z]\w*(?:Error|Exception)|BadZipFile|error): ")


def pruefe_meldung(rc, log: str, mehrzeilig_ok: bool = False):
    """Orakel fuer die Fehlermeldung: bei Rueckgabewert ungleich 0 genau eine Zeile (mit -q) und kein
    roher Python-Ausnahmename wie "KeyError: ..." oder "BlenderError: ...". Rueckgabe Symptom oder None."""
    if rc in (0, None):
        return None
    if _ROHE_AUSNAHME.search(log):
        return f"rohe Python-Fehlermeldung: {_ROHE_AUSNAHME.search(log).group(0).strip()}"
    zeilen = [z for z in log.splitlines() if z.strip()]
    if not zeilen:
        return "Fehler ohne jede Meldung"
    if not mehrzeilig_ok and len(zeilen) != 1:
        return f"Fehlermeldung ueber {len(zeilen)} Zeilen statt einer"
    return None


def _dateien_ausserhalb(cwd: Path, vorher: set) -> list:
    aktuell = set()
    for p in cwd.rglob("*"):
        aktuell.add(p)
    neu = [p for p in aktuell - vorher if p.is_file()]
    return neu


# ---------------------------------------------------------------- Ziele

class Kampagne:
    def __init__(self, a):
        self.a = a
        self.outdir = Path(a.outdir)
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.funde = []
        self.gezaehlt = 0
        self.samples = [p for p in [SAMPLES_DIR / "stuhl_tisch_2017.skp", SAMPLES_DIR / "leer_2025.skp",
                                    EXTERN_DIR / "gondel_2020.skp", EXTERN_DIR / "gross_2026.skp"] if p.is_file()]

    def melde(self, ziel: str, symptom: str, data: bytes, args, extra=""):
        name = f"fund_{ziel}_{len(self.funde):03d}.bin"
        (self.outdir / name).write_bytes(data)
        self.funde.append({"ziel": ziel, "symptom": symptom, "datei": name, "args": args, "extra": extra})
        print(f"FUND [{ziel}] {symptom}  -> {name}  {extra}", flush=True)

    # --- CLI-basierte Ziele (mutierte .skp bzw. feindliche Dateien) ---
    def _cli_datei(self, ziel: str, ext: str, gen, cli_args, dst_ext=None, budget=ZEIT_BUDGET,
                   rss=RSS_GRENZE_MB, n=None):
        rng = np.random.default_rng(self.a.seed + hash(ziel) % 10000)
        mut = Mutator(self.a.seed + 1)
        n = n if n is not None else self.a.cases
        for i in range(n):
            with tempfile.TemporaryDirectory(prefix="fuzz_") as tmp:
                tmp = Path(tmp)
                vorher = set(tmp.rglob("*"))
                inp = tmp / f"in{ext}"
                if gen == "mutate" and self.samples:
                    data = mut.mutate(self.samples[i % len(self.samples)].read_bytes())
                else:
                    data = HOSTILE[ext](rng) if ext in HOSTILE else gen(rng)
                inp.write_bytes(data)
                dst = tmp / f"out{dst_ext}" if dst_ext else None
                args = [a.replace("<IN>", str(inp)).replace("<OUT>", str(dst) if dst else "")
                        for a in cli_args]
                befund = lauf_prozess(args, budget, rss, tmp)
                self.gezaehlt += 1
                sym = befund["symptom"]
                if sym is None and dst is not None and befund["rc"] not in (0, None) and dst.exists():
                    sym = "Teil-Ausgabe nach Fehler geblieben"
                ist_fehler = not (cli_args[0] == "diff" and befund["rc"] == 1)  # diff: 1 heisst "verschieden"
                if sym is None and ist_fehler:
                    sym = pruefe_meldung(befund["rc"], befund["log"], mehrzeilig_ok=(cli_args[0] == "report"))
                if sym is None:
                    # Kein Schritt darf Dateien anlegen, die nicht Eingabe, Ziel (auch Geschwister mit
                    # gleichem Stamm wie out.mtl zu out.obj) oder das Log sind. So faellt ein Schreiben
                    # ausserhalb des Ziels (etwa durch Pfad-Traversal) auf.
                    erlaubt = {"_log.txt", inp.name}
                    for extra in _dateien_ausserhalb(tmp, vorher):
                        if extra.name in erlaubt or extra == inp:
                            continue
                        if dst is not None and (extra == dst or extra.stem == dst.stem):
                            continue
                        sym = f"unerwartete Datei geschrieben: {extra.relative_to(tmp)}"
                        break
                if sym is not None:
                    self.melde(ziel, sym, data, args, f"rc={befund['rc']} peak={befund['peak_mb']}MB")
                    if befund["log"]:
                        print("   ", befund["log"].replace("\n", "\n    ")[-500:], flush=True)

    def ziel_info(self):
        self._cli_datei("info", ".skp", "mutate", ["info", "<IN>", "--fast"])
        self._cli_datei("info-bounds", ".skp", "mutate", ["info", "<IN>", "--bounds"])

    def ziel_glb(self):
        self._cli_datei("convert-glb", ".skp", "mutate", ["convert", "<IN>", "-o", "<OUT>", "-q"], ".glb")

    def ziel_3mf(self):
        self._cli_datei("convert-3mf", ".skp", "mutate", ["convert", "<IN>", "-o", "<OUT>", "-q"], ".3mf")

    def ziel_json(self):
        self._cli_datei("convert-json", ".skp", "mutate", ["convert", "<IN>", "-o", "<OUT>", "-q"], ".json")

    def ziel_native(self):
        for ext in (".obj", ".stl", ".ply", ".dxf", ".ifc"):
            self._cli_datei(f"convert{ext}", ".skp", "mutate", ["convert", "<IN>", "-o", "<OUT>", "-q"], ext,
                            n=max(1, self.a.cases // 5))

    def ziel_rewrite(self):
        self._cli_datei("rewrite", ".skp", "mutate", ["convert", "<IN>", "-o", "<OUT>", "-q"], ".skp")

    def ziel_diff(self):
        ref = self.samples[0] if self.samples else None
        if ref:
            self._cli_datei("diff", ".skp", "mutate", ["diff", "<IN>", str(ref), "-q"])

    def ziel_report(self):
        self._cli_datei("report", ".skp", "mutate", ["report", "<IN>"])

    def ziel_zip(self):
        self._cli_datei("zip-info", ".skp", "hostile", ["info", "<IN>", "--bounds"])
        self._cli_datei("zip-glb", ".skp", "hostile", ["convert", "<IN>", "-o", "<OUT>", "-q"], ".glb")

    # --- In-Prozess-Ziele (schnell) ---
    def ziel_ops(self):
        from skptool.opsjson import load_ops
        rng = np.random.default_rng(self.a.seed + 5)
        for i in range(self.a.cases * 4):
            data = hostile_ops(rng)
            self.gezaehlt += 1
            raw = data.decode("latin-1")
            try:
                load_ops(raw)
            except SystemExit:
                pass
            except Exception as exc:
                self.melde("opsjson", f"unerwartete Ausnahme {type(exc).__name__}", data, ["load_ops"], str(exc)[:120])

    def ziel_refcheck(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("refcheck", ROOT / "skptool" / "blender_scripts" / "refcheck.py")
        refcheck = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(refcheck)
        rng = np.random.default_rng(self.a.seed + 6)
        exts = list(HOSTILE)
        for i in range(self.a.cases * 2):
            ext = exts[int(rng.integers(0, len(exts)))]
            data = HOSTILE[ext](rng)
            with tempfile.TemporaryDirectory(prefix="fuzz_ref_") as tmp:
                f = Path(tmp) / f"m{ext}"
                f.write_bytes(data)
                for allow in (False, True):
                    self.gezaehlt += 1
                    try:
                        refcheck.precheck(str(f), allow)
                    except refcheck.RefError:
                        pass
                    except Exception as exc:
                        self.melde("refcheck", f"unerwartete Ausnahme {type(exc).__name__}", data,
                                   ["precheck", ext], str(exc)[:120])

    def ziel_live(self):
        """Live-Protokoll in-Prozess: Rahmen ueberpruefen ohne Blender (nur die Client-Seite und die
        Rahmen-Logik des Servers). Oversized frames, kaputte JSON, falsche HMAC, Wiederholung."""
        from skptool import live
        rng = np.random.default_rng(self.a.seed + 8)
        for i in range(self.a.cases):
            self.gezaehlt += 1
            # _read_line mit einem Strom, der tropfenweise oder uebergross liefert
            data = bytes(rng.integers(0, 256, int(rng.integers(0, 5000)), dtype=np.uint8).tolist())

            class Fake:
                def __init__(self, d):
                    self.d = d
                    self.i = 0

                def recv(self, n):
                    if self.i >= len(self.d):
                        return b""
                    chunk = self.d[self.i:self.i + int(rng.integers(1, 200))]
                    self.i += len(chunk)
                    return chunk

            try:
                live._read_line(Fake(data), 4096)
            except live.LiveError:
                pass
            except Exception as exc:
                self.melde("live-read", f"unerwartete Ausnahme {type(exc).__name__}", data, ["_read_line"],
                           str(exc)[:120])
        # feindlicher Server auf dem Port: tropft Bytes ohne Zeilenende. Der Client muss nach seiner
        # Gesamtfrist (Begruessung 10 s) mit LiveError aufgeben, nicht erst nach 4096 Tropfen.
        self.gezaehlt += 1
        dauer, fehler = client_gegen_tropfserver(live, frist=25.0)
        if fehler != "LiveError" or dauer > 20.0:
            self.melde("live-client", f"Client haengt an tropfendem Server ({fehler}, {dauer:.1f} s)", b"",
                       ["request"])

    def ziel_live_server(self):
        """Server-Seite des Live-Protokolls in-Prozess: feindliche Rahmen vor der Anmeldung, falsche
        HMAC, Wiederholung, uebergrosse Rahmen, Slowloris, zu viele Verbindungen. Orakel: keine
        Antwort ist je ok ohne gueltige Anmeldung, jede Verbindung endet in der Lesefrist, und danach
        beantwortet der Server ein gueltiges ping mit gueltigem Beweis."""
        import socket
        import threading

        rng = np.random.default_rng(self.a.seed + 10)
        srv = LiveServerImProzess(lesefrist=2.0)
        frist = srv.lesefrist + 5.0

        def pruefe(name, antwort_roh, t0, data):
            dauer = time.time() - t0
            if dauer > frist:
                self.melde("live-server", f"{name}: Verbindung hing {dauer:.1f} s", data, [name])
                return
            if not antwort_roh:
                return
            try:
                antwort = json.loads(antwort_roh)
            except ValueError:
                self.melde("live-server", f"{name}: Antwort ist kein JSON", data, [name], repr(antwort_roh[:100]))
                return
            if not isinstance(antwort, dict) or antwort.get("ok") is not False:
                self.melde("live-server", f"{name}: Antwort ohne Anmeldung nicht abgelehnt", data, [name],
                           repr(antwort)[:150])

        def roh_senden(data, nachher_schliessen=True):
            t0 = time.time()
            c, _sn = srv.verbinde()
            c.settimeout(frist)
            antwort = b""
            try:
                with c:
                    c.sendall(data)
                    if nachher_schliessen and not data.endswith(b"\n"):
                        c.shutdown(socket.SHUT_WR)
                    antwort = _zeile(c)
            except (OSError, ValueError):
                pass
            return antwort, t0

        try:
            for _ in range(self.a.cases):
                data = live_rahmen(rng)
                self.gezaehlt += 1
                antwort, t0 = roh_senden(data)
                pruefe("rahmen", antwort, t0, data)
            # falsche HMAC
            c, sn = srv.verbinde()
            with c:
                c.sendall(json.dumps({"cmd": "ping", "cnonce": "c" * 32, "auth": "0" * 64}).encode() + b"\n")
                pruefe("falsche-hmac", _zeile(c), time.time(), b"")
            # Wiederholung einer gueltigen Anfrage auf neuer Verbindung (anderer Server-Nonce)
            antwort, _sn, roh = srv.anfrage("ping")
            if antwort.get("ok") is not True:
                self.melde("live-server", "gueltiges ping abgelehnt", roh, ["ping"], repr(antwort)[:150])
            antwort2, t0 = roh_senden(roh)
            pruefe("wiederholung", antwort2, t0, roh)
            # uebergrosser Rahmen
            gross = b"{\"cmd\":\"" + b"x" * (1024 * 1024 + 10) + b"\"}\n"
            antwort, t0 = roh_senden(gross)
            pruefe("uebergross", antwort, t0, b"")
            # Slowloris: ein Byte je 0,2 s, nie ein Zeilenende
            t0 = time.time()
            c, _sn = srv.verbinde()
            c.settimeout(0.2)
            with c:
                geschlossen = False
                while time.time() - t0 < frist and not geschlossen:
                    try:
                        c.sendall(b"{")
                        antwort = c.recv(4096)
                        if antwort:
                            geschlossen = True
                            pruefe("slowloris", antwort.split(b"\n")[0], t0, b"")
                    except socket.timeout:
                        pass
                    except OSError:
                        geschlossen = True
                if not geschlossen:
                    self.melde("live-server", "Slowloris haelt die Verbindung offen", b"", ["slowloris"])
            self.gezaehlt += 4
            # zu viele Verbindungen: 12 offene, die nichts schicken; der Server muss begrenzen und danach
            # wieder antworten
            offene, abgewiesen = [], 0
            for _ in range(12):
                c = socket.create_connection(("127.0.0.1", srv.port), timeout=10)
                offene.append(c)
                try:
                    gruss = json.loads(_zeile(c))
                except (OSError, ValueError):
                    continue
                if "hello" not in gruss:
                    abgewiesen += 1
                    if gruss.get("ok") is not False:
                        self.melde("live-server", "Ueberzaehlige Verbindung nicht sauber abgewiesen", b"",
                                   ["verbindungen"], repr(gruss)[:150])
            for c in offene:
                c.close()
            if abgewiesen == 0:
                self.melde("live-server", "12 gleichzeitige Verbindungen ohne Begrenzung angenommen", b"",
                           ["verbindungen"])
            self.gezaehlt += 1
            # authentisch, aber mit feindlichen Zusatzfeldern
            for _ in range(max(5, self.a.cases // 10)):
                werte = [-1, 0, 1e308, "x", None, [1], {"a": 1}, 2**70]
                extra = {"timeout": werte[int(rng.integers(0, len(werte)))],
                         "ops": [[[]]], "z" * int(rng.integers(1, 50)): "\u202e\x00"}
                self.gezaehlt += 1
                try:
                    antwort, _sn, roh = srv.anfrage("ping", **extra)
                except (OSError, ValueError) as exc:
                    self.melde("live-server", f"angemeldete Anfrage brach ab: {type(exc).__name__}", b"",
                               ["ping"], str(extra)[:150])
                    continue
                if antwort.get("ok") is not True:
                    self.melde("live-server", "angemeldetes ping mit Zusatzfeldern abgelehnt", roh, ["ping"],
                               repr(antwort)[:150])
            # am Ende: lebt der Server noch und beweist er sich?
            ende = time.time() + srv.lesefrist + 5
            while True:
                try:
                    antwort, sn, _roh = srv.anfrage("ping")
                    break
                except (OSError, ValueError):
                    if time.time() > ende:
                        self.melde("live-server", "Server antwortet nach dem Angriff nicht mehr", b"", ["ping"])
                        return
                    time.sleep(0.2)
            if antwort.get("proof") != srv.mac("server", sn, "a" * 32) or antwort.get("ok") is not True:
                self.melde("live-server", "Server-Beweis nach dem Angriff falsch", b"", ["ping"], repr(antwort)[:150])
            if threading.active_count() > 200:
                self.melde("live-server", f"{threading.active_count()} Threads uebrig", b"", ["threads"])
        finally:
            srv.schliessen()

    def ziel_mcp(self):
        """MCP-Server als Subprozess, JSON-RPC ueber stdio: feindliche Zeilen, Antwort muss sauberes
        JSON-RPC sein, stdout nie ein Traceback, Prozess bleibt am Leben und antwortet danach auf ping."""
        rng = np.random.default_rng(self.a.seed + 9)
        n = self.a.cases
        # Zeilen buendeln, jeder Serverstart deckt viele Faelle ab (Prozessstart ist teuer)
        pro_start = 40
        for start in range(0, n, pro_start):
            zeilen = []
            for _ in range(min(pro_start, n - start)):
                zeilen.append(hostile_jsonrpc(rng))
            # am Ende ein gueltiges ping, um zu sehen, dass der Server noch lebt
            gueltige_id = 999999
            zeilen.append(json.dumps({"jsonrpc": "2.0", "id": gueltige_id, "method": "ping"}).encode())
            eingabe = b"\n".join(zeilen) + b"\n"
            env = dict(os.environ)
            env["PYTHONPATH"] = str(ROOT)
            env["PYTHONIOENCODING"] = "utf-8"
            cmd = [PY, "-P", "-m", "skptool", "mcp", "--nur-lesen"]
            try:
                r = subprocess.run(cmd, input=eingabe, cwd=str(ROOT), env=env,
                                   capture_output=True, timeout=ZEIT_BUDGET)
            except subprocess.TimeoutExpired:
                self.melde("mcp", "Zeitueberschreitung", eingabe, ["mcp"], "")
                continue
            out = r.stdout.decode("utf-8", "replace")
            self.gezaehlt += len(zeilen)
            if TRACEBACK_MARKER in out:
                self.melde("mcp", "Traceback auf stdout", eingabe, ["mcp"], out[-300:])
                continue
            # jede stdout-Zeile muss gueltiges JSON-RPC sein
            lebt = False
            for zeile in out.splitlines():
                zeile = zeile.strip()
                if not zeile:
                    continue
                try:
                    msg = json.loads(zeile)
                except ValueError:
                    self.melde("mcp", "Nicht-JSON auf stdout", eingabe, ["mcp"], zeile[:200])
                    break
                if isinstance(msg, dict) and msg.get("id") == gueltige_id and msg.get("result") == {}:
                    lebt = True
            else:
                if not lebt:
                    self.melde("mcp", "Server antwortet nach Angriff nicht mehr auf ping", eingabe, ["mcp"], out[-200:])

    def ziel_blender(self):
        """Langsamer Blender-Weg: feindliche .glb nach .skp, wenige Faelle."""
        from skptool import blender
        try:
            blender.find_blender()
        except Exception as exc:
            print(f"Blender nicht gefunden, Ziel uebersprungen: {exc}", flush=True)
            return
        exts = (".glb", ".gltf", ".obj", ".ply", ".stl")
        gesamt = min(self.a.cases, self.a.blender_cases)
        for k, ext in enumerate(exts):
            n = gesamt // len(exts) + (1 if k < gesamt % len(exts) else 0)
            if n:
                self._cli_datei(f"blender{ext}2skp", ext, "hostile", ["convert", "<IN>", "-o", "<OUT>", "-q"],
                                ".skp", budget=ZEIT_BUDGET_BLENDER, rss=RSS_GRENZE_BLENDER_MB, n=n)
        # .dxf ist keine Blender-Eingabe: muss sofort sauber abgelehnt werden
        self._cli_datei("dxf-eingabe", ".dxf", "hostile", ["convert", "<IN>", "-o", "<OUT>", "-q"], ".skp",
                        n=max(1, gesamt // 10))

    ZIELE = {
        "info": ziel_info, "glb": ziel_glb, "3mf": ziel_3mf, "json": ziel_json, "native": ziel_native,
        "rewrite": ziel_rewrite, "diff": ziel_diff, "report": ziel_report, "zip": ziel_zip,
        "ops": ziel_ops, "refcheck": ziel_refcheck, "live": ziel_live, "live-server": ziel_live_server,
        "mcp": ziel_mcp, "blender": ziel_blender,
    }
    SCHNELL = ("ops", "refcheck", "live", "live-server", "info", "glb", "3mf", "json", "rewrite", "diff", "report", "zip", "mcp")

    def fahre(self, ziele):
        for name in ziele:
            fn = self.ZIELE[name]
            print(f"== Ziel {name}", flush=True)
            t = time.time()
            fn(self)
            print(f"   {name}: {round(time.time() - t, 1)} s", flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--cases", type=int, default=200, help="Faelle je Ziel (In-Prozess-Ziele ein Vielfaches)")
    ap.add_argument("--blender-cases", type=int, default=12)
    ap.add_argument("--target", default="schnell",
                    help="ein Ziel, 'schnell' (ohne Blender), 'alle' oder Komma-Liste")
    ap.add_argument("--outdir", default=str(Path(tempfile.gettempdir()) / "skptool_fuzz_funde"),
                    help="Ordner fuer die Ausloeser gefundener Faelle (Standard im Temp-Ordner, nie im Repo)")
    ap.add_argument("--list", action="store_true", help="Ziele auflisten")
    a = ap.parse_args(argv)
    if a.list:
        print("Ziele:", ", ".join(Kampagne.ZIELE))
        print("schnell:", ", ".join(Kampagne.SCHNELL))
        return 0
    if a.target == "schnell":
        ziele = list(Kampagne.SCHNELL)
    elif a.target == "alle":
        ziele = list(Kampagne.ZIELE)
    else:
        ziele = [z.strip() for z in a.target.split(",") if z.strip()]
    for z in ziele:
        if z not in Kampagne.ZIELE:
            raise SystemExit(f"Unbekanntes Ziel: {z}. Bekannt: {', '.join(Kampagne.ZIELE)}")
    k = Kampagne(a)
    t = time.time()
    k.fahre(ziele)
    print(f"\n=== {k.gezaehlt} Faelle in {round(time.time() - t, 1)} s, {len(k.funde)} Fund(e)", flush=True)
    for f in k.funde:
        print(f)
    return 1 if k.funde else 0


if __name__ == "__main__":
    sys.exit(main())
