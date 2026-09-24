"""Pruefung von "skptool mcp" mit unabhaengigen Clients: dem offiziellen MCP-Python-SDK und den
offiziellen JSON-Schemas der MCP-Spezifikation (je Protokollversion).

Einrichtung, einmalig und nie in der Projekt-venv (Pakete mindestens 14 Tage alt):
    uv venv .venv-mcp --python 3.12
    uv pip install --python .venv-mcp\\Scripts\\python.exe --exclude-newer 2026-09-09T21:00:00Z ^
        "mcp==2.2.0" "jsonschema==4.26.0"

Aufruf aus dem Projektordner mit dem Python, das skptool ausfuehren kann:
    .venv\\Scripts\\python tools\\mcp_sdk_pruefung.py          alles ausser Blender-Fenster
    .venv\\Scripts\\python tools\\mcp_sdk_pruefung.py --live   zusaetzlich live_* mit echtem Blender-Fenster
    .venv\\Scripts\\python tools\\mcp_sdk_pruefung.py --schnell  ohne Blender und ohne grosse Ausgaben

Das Skript startet sich mit dem Python aus .venv-mcp neu (dort liegt das SDK); der Server laeuft mit
dem Python, das das Skript aufgerufen hat. Gibt es .venv-mcp nicht, endet es mit einem Hinweis und
Rueckgabe 0. Die Schemas der Spezifikation kommen von einem festen Commit und werden per SHA-256
geprueft (Ablage: .venv-mcp/mcp-schema/).

Der Server bekommt, wie bei jedem SDK-Client, nur die Standard-Umgebung des SDK (unter Windows
ohne ProgramFiles). Genau so starten ihn Claude Desktop und andere Clients ohne eigenes "env".

Rueckgabe 0: alles gruen, 1: mindestens eine Abweichung.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SDK_VENV = ROOT / ".venv-mcp"
STUHL = ROOT / "samples" / "stuhl_tisch_2017.skp"
SCHEMA_COMMIT = "271ecc9accafdd9b83a3c869fa67c22953b2af80"
SCHEMA_URL = "https://raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol/{c}/schema/{v}/schema.json"
SCHEMA_SHA256 = {
    "2024-11-05": "61cea2392d4f284092d09bc84b9ac488c0d5618ac2b38a56942fc5b99fd960ce",
    "2025-03-26": "e720669548c8100a4282c49e580efd6ddf7f28899ea786fc8db251dbdb356131",
    "2025-06-18": "af845e7e5b9d27107d1690f0936022546177a1403e63ffb11470135b296a2e01",
    "2025-11-25": "268a5f82ba70fd7e4b6dc4aa1e64f116f74b4d0edcb69dc046829c79dd4e97e7",
}
ALLE = ["skp_info", "skp_list", "skp_diff", "skp_report", "skp_convert", "skp_edit",
        "live_status", "live_ops", "live_screenshot", "live_undo"]
NUR_LESEN = ["skp_info", "skp_list", "skp_diff", "skp_report", "live_status", "live_screenshot"]


def _sdk_python() -> Path | None:
    for p in (SDK_VENV / "Scripts" / "python.exe", SDK_VENV / "bin" / "python"):
        if p.is_file():
            return p
    return None


def _neu_starten(argv) -> int:
    """Mit dem Python aus .venv-mcp neu starten; der Server nutzt dieses (aufrufende) Python."""
    py = _sdk_python()
    if py is None:
        print(f"uebersprungen: {SDK_VENV} fehlt (Einrichtung siehe Kopf dieses Skripts)")
        return 0
    return subprocess.call([str(py), str(Path(__file__).resolve()), *argv, "--server-python", sys.executable],
                           cwd=str(ROOT))


# ---------------------------------------------------------------- Ergebnisse sammeln

class Bericht:
    def __init__(self):
        self.ergebnisse: list[tuple[str, bool, str]] = []

    def ok(self, name, detail=""):
        self.ergebnisse.append((name, True, detail))
        print(f"  OK      {name}" + (f": {detail}" if detail else ""), flush=True)

    def fehler(self, name, detail):
        self.ergebnisse.append((name, False, detail))
        print(f"  FEHLER  {name}: {detail}", flush=True)

    def pruefe(self, name, bedingung, detail="", fehltext=None):
        if bedingung:
            self.ok(name, detail)
        else:
            self.fehler(name, fehltext or detail or "Bedingung nicht erfuellt")
        return bool(bedingung)

    def zusammenfassung(self) -> int:
        schlecht = [e for e in self.ergebnisse if not e[1]]
        print(f"\n{len(self.ergebnisse) - len(schlecht)} von {len(self.ergebnisse)} Pruefungen gruen")
        for name, _, detail in schlecht:
            print(f"  FEHLER {name}: {detail}")
        return 1 if schlecht else 0


# ---------------------------------------------------------------- Offizielle Schemas der Spezifikation

def lade_schemas() -> dict:
    ablage = SDK_VENV / "mcp-schema"
    ablage.mkdir(parents=True, exist_ok=True)
    schemas = {}
    for v, soll in SCHEMA_SHA256.items():
        datei = ablage / f"{v}.json"
        if not datei.is_file():
            with urllib.request.urlopen(SCHEMA_URL.format(c=SCHEMA_COMMIT, v=v), timeout=60) as r:
                datei.write_bytes(r.read())
        ist = hashlib.sha256(datei.read_bytes()).hexdigest()
        if ist != soll:
            datei.unlink()
            raise RuntimeError(f"Schema {v}: SHA-256 {ist} statt {soll}")
        schemas[v] = json.loads(datei.read_text(encoding="utf-8"))
    return schemas


def schema_pruefer(root: dict, name: str):
    """Validator fuer die Definition `name` aus einem Spezifikations-Schema (Draft 7 oder 2020-12)."""
    from jsonschema.validators import validator_for
    from referencing import Registry, Resource

    abschnitt = "$defs" if "$defs" in root else "definitions"
    if name not in root[abschnitt]:
        return None
    uri = "urn:mcp-schema"
    registry = Registry().with_resource(uri, Resource.from_contents(root))
    cls = validator_for(root)
    return cls({"$ref": f"{uri}#/{abschnitt}/{name}"}, registry=registry)


def schema_fehler(root, name, daten) -> str | None:
    pruefer = schema_pruefer(root, name)
    if pruefer is None:
        return f"Definition {name} fehlt im Schema"
    fehler = sorted(pruefer.iter_errors(daten), key=lambda e: list(e.absolute_path))
    if not fehler:
        return None
    e = fehler[0]
    return f"{name}: {e.message[:200]} bei {'/'.join(map(str, e.absolute_path))}"


class RohClient:
    """Minimaler JSON-RPC-Client ueber stdio, um die rohen Nachrichten gegen die Schemas zu pruefen."""

    def __init__(self, server_py, *extra, env=None):
        self.proc = subprocess.Popen([server_py, "-m", "skptool", "mcp", *extra], cwd=str(ROOT),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     env=env)
        self.q: queue.Queue = queue.Queue()
        self.zeilen: list[bytes] = []
        threading.Thread(target=self._lesen, daemon=True).start()
        self.rid = 0

    def _lesen(self):
        for z in iter(self.proc.stdout.readline, b""):
            self.zeilen.append(z)
            self.q.put(json.loads(z))
        self.q.put(None)

    def roh(self, msg):
        self.proc.stdin.write(json.dumps(msg).encode() + b"\n")
        self.proc.stdin.flush()

    def anfrage(self, methode, params=None, timeout=300):
        self.rid += 1
        msg = {"jsonrpc": "2.0", "id": self.rid, "method": methode}
        if params is not None:
            msg["params"] = params
        self.roh(msg)
        while True:
            a = self.q.get(timeout=timeout)
            if a is None:
                raise RuntimeError("Server beendet")
            if a.get("id") == self.rid:
                return a

    def schliessen(self):
        self.proc.stdin.close()
        rc = self.proc.wait(30)
        self.proc.stdout.close()
        return rc


def pruefe_schemas(b: Bericht, server_py: str, env: dict):
    print("\n[1] Rohe Nachrichten gegen die offiziellen Schemas der Spezifikation")
    try:
        schemas = lade_schemas()
    except Exception as exc:  # noqa: BLE001
        b.fehler("Schemas laden", f"{type(exc).__name__}: {exc}")
        return
    b.ok("Schemas geladen", f"Commit {SCHEMA_COMMIT[:12]}, {', '.join(schemas)} (SHA-256 geprueft)")
    for v, root in schemas.items():
        antwort_def = "JSONRPCResultResponse" if "JSONRPCResultResponse" in root.get("$defs", {}) else "JSONRPCResponse"
        fehler_def = "JSONRPCErrorResponse" if "JSONRPCErrorResponse" in root.get("$defs", {}) else "JSONRPCError"
        c = RohClient(server_py, env=env)
        probleme = []
        batch_text = ""
        try:
            init = c.anfrage("initialize", {"protocolVersion": v, "capabilities": {},
                                            "clientInfo": {"name": "schema-pruefung", "version": "1"}})
            probleme += [schema_fehler(root, antwort_def, init), schema_fehler(root, "InitializeResult", init["result"])]
            if init["result"]["protocolVersion"] != v:
                probleme.append(f"Server antwortet mit {init['result']['protocolVersion']} statt {v}")
            c.roh({"jsonrpc": "2.0", "method": "notifications/initialized"})
            ping = c.anfrage("ping")
            probleme.append(schema_fehler(root, antwort_def, ping))
            tl = c.anfrage("tools/list")
            probleme += [schema_fehler(root, antwort_def, tl), schema_fehler(root, "ListToolsResult", tl["result"])]
            for t in tl["result"]["tools"]:
                probleme.append(schema_fehler(root, "Tool", t))
            for args in ({"path": str(STUHL)}, {"path": str(ROOT / "gibtsnicht.skp")}, {}):
                r = c.anfrage("tools/call", {"name": "skp_info", "arguments": args})
                probleme += [schema_fehler(root, antwort_def, r), schema_fehler(root, "CallToolResult", r["result"])]
            for methode, params in (("resources/list", {}), ("tools/call", {"name": "gibtsnicht", "arguments": {}})):
                r = c.anfrage(methode, params)
                probleme.append(schema_fehler(root, fehler_def, r))
            # JSON-RPC-Batch: 2025-03-26 verlangt die Unterstuetzung, die anderen Versionen kennen keine
            c.roh([{"jsonrpc": "2.0", "id": "b1", "method": "ping"},
                   {"jsonrpc": "2.0", "method": "notifications/irgendwas"},
                   {"jsonrpc": "2.0", "id": "b2", "method": "tools/call",
                    "params": {"name": "skp_info", "arguments": {"path": str(STUHL)}}},
                   {"jsonrpc": "2.0", "id": "b3", "method": "gibtsnicht"}])
            batch = c.q.get(timeout=300)
            if v == "2025-03-26":
                probleme.append(schema_fehler(root, "JSONRPCBatchResponse", batch))
                if not isinstance(batch, list) or sorted(m.get("id") for m in batch) != ["b1", "b2", "b3"]:
                    probleme.append(f"Batch-Antwort unvollstaendig: {str(batch)[:200]}")
                else:
                    batch_text = f", Batch mit {len(batch)} Antworten"
            elif not (isinstance(batch, dict) and batch.get("error", {}).get("code") == -32600):
                probleme.append(f"Batch unter {v} nicht mit -32600 abgelehnt: {str(batch)[:200]}")
            else:
                batch_text = ", Batch abgelehnt (-32600)"
            rc = c.schliessen()
            if rc != 0:
                probleme.append(f"Server endete mit Code {rc}")
        except Exception as exc:  # noqa: BLE001
            probleme.append(f"{type(exc).__name__}: {exc}")
            c.proc.kill()
        probleme = [p for p in probleme if p]
        b.pruefe(f"Schema {v}: initialize, ping, tools/list, tools/call, Fehlerantworten", not probleme,
                 f"{len(c.zeilen)} Nachrichten gueltig{batch_text}", "; ".join(probleme[:5]))


# ---------------------------------------------------------------- SDK

def main_sdk(a) -> int:
    import anyio

    return anyio.run(_main_sdk, a)


async def _main_sdk(a) -> int:  # noqa: C901 - eine lange Liste von Pruefungen
    import anyio
    import mcp_types as types
    from jsonschema import Draft7Validator, Draft202012Validator
    from mcp import StdioServerParameters
    from mcp.client import Client, ClientSession
    from mcp.client.stdio import stdio_client
    from mcp.shared.exceptions import MCPError
    from mcp.shared.tool_name_validation import validate_tool_name
    from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

    b = Bericht()
    tmp = Path(tempfile.mkdtemp(prefix="skptool_sdk_"))
    log_pfad = tmp / "server.log"
    log = open(log_pfad, "w", encoding="utf-8")  # noqa: SIM115 - bleibt bis zum Ende offen
    info = types.Implementation(name="skptool-sdk-pruefung", version="1.0")
    sdk_version = importlib.metadata.version("mcp")
    print(f"MCP-Python-SDK {sdk_version}, Server-Python {a.server_python}")

    def params(*extra, env=None):
        return StdioServerParameters(command=a.server_python, args=["-m", "skptool", "mcp", *extra],
                                     cwd=str(ROOT), env=env)

    @asynccontextmanager
    async def sitzung(*extra, env=None, version=None):
        async with stdio_client(params(*extra, env=env), errlog=log) as (r, w):
            async with ClientSession(r, w, client_info=info, read_timeout_seconds=900) as s:
                if version is None:
                    s.init_ergebnis = await s.initialize()
                else:
                    res = await s.send_request(types.InitializeRequest(params=types.InitializeRequestParams(
                        protocol_version=version, capabilities=types.ClientCapabilities(), client_info=info)),
                        types.InitializeResult)
                    s.init_ergebnis = res
                    if res.protocol_version in HANDSHAKE_PROTOCOL_VERSIONS:
                        s.adopt(res)
                        await s.send_notification(types.InitializedNotification())
                yield s

    def text(r) -> str:
        return "".join(c.text for c in r.content if getattr(c, "type", "") == "text")

    async def fall(name, coro):
        try:
            return await coro
        except Exception as exc:  # noqa: BLE001
            b.fehler(name, f"{type(exc).__name__}: {exc}"[:500])
            if a.verbose:
                traceback.print_exc()
            return None

    # -- 1: offizielle Schemas (roh, ohne SDK)
    pruefe_schemas(b, a.server_python, None)

    # -- 2: Verbindungsaufbau wie ein aktueller SDK-Client (server/discover, dann initialize)
    print("\n[2] Verbindungsaufbau mit dem SDK")

    async def auto_modus():
        async with Client(stdio_client(params(), errlog=log), mode="auto", client_info=info) as c:
            b.pruefe("Client(mode='auto'): server/discover abgelehnt, Rueckfall auf initialize",
                     c.protocol_version == "2025-11-25" and c.server_info.name == "skptool",
                     f"Protokoll {c.protocol_version}, Server {c.server_info.name} {c.server_info.version}",
                     f"Protokoll {c.protocol_version}")
            tools = (await c.list_tools()).tools
            b.pruefe("Client: tools/list", [t.name for t in tools] == ALLE, f"{len(tools)} Werkzeuge")
    await fall("Client(mode='auto')", auto_modus())

    for v in HANDSHAKE_PROTOCOL_VERSIONS:
        async def je_version(v=v):
            async with sitzung(version=v) as s:
                ist = s.init_ergebnis.protocol_version
                tools = (await s.list_tools()).tools
                r = await s.call_tool("skp_info", {"path": str(STUHL)})
                await s.send_ping()
                b.pruefe(f"Protokoll {v}: initialize, tools/list, tools/call, ping",
                         ist == v and len(tools) == 10 and not r.is_error,
                         f"ausgehandelt {ist}", f"ausgehandelt {ist}, {len(tools)} Werkzeuge, isError {r.is_error}")
        await fall(f"Protokoll {v}", je_version())

    for v in ("2099-12-31", "2024-10-07", "2026-07-28"):
        async def unbekannt(v=v):
            async with sitzung(version=v) as s:
                ist = s.init_ergebnis.protocol_version
                b.pruefe(f"unbekannte Version {v}: Server bietet seine neueste an", ist == "2025-11-25",
                         f"Antwort {ist}", f"Antwort {ist} statt 2025-11-25")
        await fall(f"unbekannte Version {v}", unbekannt())

    # -- 3: Werkzeuge und Schemas
    print("\n[3] tools/list")

    async def werkzeugliste():
        async with sitzung() as s:
            caps = s.init_ergebnis.capabilities
            b.pruefe("capabilities.tools vorhanden, nichts Unerfuelltes angekuendigt",
                     caps.tools is not None and caps.resources is None and caps.prompts is None,
                     caps.model_dump(exclude_none=True).__repr__())
            b.pruefe("serverInfo und instructions", s.init_ergebnis.server_info.name == "skptool"
                     and bool(s.init_ergebnis.instructions), s.init_ergebnis.server_info.version)
            tools = (await s.list_tools()).tools
            b.pruefe("10 Werkzeuge in fester Reihenfolge", [t.name for t in tools] == ALLE)
            for t in tools:
                probleme = []
                erg = validate_tool_name(t.name)
                if not erg.is_valid or erg.warnings:
                    probleme.append(f"Name: {erg.warnings}")
                for cls in (Draft202012Validator, Draft7Validator):
                    try:
                        cls.check_schema(t.input_schema)
                    except Exception as exc:  # noqa: BLE001
                        probleme.append(f"inputSchema ({cls.__name__}): {exc}"[:200])
                    if t.output_schema is not None:
                        try:
                            cls.check_schema(t.output_schema)
                        except Exception as exc:  # noqa: BLE001
                            probleme.append(f"outputSchema ({cls.__name__}): {exc}"[:200])
                if t.input_schema.get("type") != "object":
                    probleme.append("inputSchema.type ist nicht object")
                if t.output_schema is not None and t.output_schema.get("type") != "object":
                    probleme.append("outputSchema.type ist nicht object")
                ann = t.annotations
                if ann is None or ann.read_only_hint is None:
                    probleme.append("annotations.readOnlyHint fehlt")
                b.pruefe(f"Werkzeug {t.name}: Name, Schemas (Draft 2020-12 und 7), Hinweise", not probleme,
                         f"readOnly={ann.read_only_hint}, outputSchema={'ja' if t.output_schema else 'nein'}",
                         "; ".join(probleme))
            return {t.name: t for t in tools}
    werkzeug = await fall("tools/list", werkzeugliste()) or {}

    # -- 4: Werkzeuge mit gueltigen Argumenten
    print("\n[4] Werkzeuge aufrufen")
    blender = not a.schnell

    async def werkzeuge_aufrufen():
        async with sitzung() as s:
            r = await s.call_tool("skp_info", {"path": str(STUHL)})
            sc = r.structured_content or {}
            b.pruefe("skp_info", not r.is_error and sc.get("version") == "17.0.1" and json.loads(text(r)) == sc,
                     f"Version {sc.get('version')}, {len(sc.get('layers', []))} Ebenen", text(r)[:300])
            r = await s.call_tool("skp_diff", {"a": str(STUHL), "b": str(STUHL)})
            b.pruefe("skp_diff (gleiche Datei)", not r.is_error and (r.structured_content or {}).get("gleich") is True,
                     "gleich: true", text(r)[:300])
            r = await s.call_tool("skp_report", {"paths": [str(ROOT / "samples" / "*.skp")]})
            b.pruefe("skp_report", not r.is_error and (r.structured_content or {}).get("anzahl", 0) >= 2,
                     f"{(r.structured_content or {}).get('anzahl')} Dateien", text(r)[:300])
            for endung, magie in ((".glb", b"glTF"), (".3mf", b"PK\x03\x04"), (".skp", None)):
                ziel = tmp / f"stuhl{endung}"
                r = await s.call_tool("skp_convert", {"input": str(STUHL), "output": str(ziel)})
                ok = not r.is_error and ziel.is_file() and (magie is None or ziel.read_bytes()[:4] == magie)
                b.pruefe(f"skp_convert nach {endung}", ok, f"{ziel.stat().st_size if ziel.is_file() else 0} Bytes, "
                         f"{(r.structured_content or {}).get('ergebnis', '')}"[:160], text(r)[:300])
            r = await s.call_tool("skp_diff", {"a": str(STUHL), "b": str(tmp / "stuhl.skp")})
            b.pruefe("skp_diff Original gegen umgeschriebene .skp", not r.is_error,
                     f"gleich: {(r.structured_content or {}).get('gleich')}", text(r)[:300])
            if not blender:
                return
            r = await s.call_tool("skp_list", {"path": str(STUHL), "name": "Leg_*", "limit": 3})
            objekte = ((r.structured_content or {}).get("list") or {}).get("objects", [])
            b.pruefe("skp_list (Blender)", not r.is_error and len(objekte) == 3,
                     f"{len(objekte)} Objekte: {', '.join(o['name'] for o in objekte)}", text(r)[:400])
            ziel = tmp / "bewegt.skp"
            ops = [{"op": "move", "select": {"name": "Leg_Chair*"}, "by": [0, 0, 0.5]}]
            r = await s.call_tool("skp_edit", {"input": str(STUHL), "output": str(ziel), "ops": ops})
            b.pruefe("skp_edit mit move (Blender)", not r.is_error and ziel.is_file(),
                     f"{(r.structured_content or {}).get('operationen')}"[:200], text(r)[:400])
            r = await s.call_tool("skp_diff", {"a": str(STUHL), "b": str(ziel)})
            b.pruefe("skp_diff zeigt die Verschiebung", not r.is_error
                     and (r.structured_content or {}).get("gleich") is False, "gleich: false", text(r)[:300])
            ziel = tmp / "stuhl.blend"
            r = await s.call_tool("skp_convert", {"input": str(STUHL), "output": str(ziel)})
            b.pruefe("skp_convert nach .blend (Blender)", not r.is_error and ziel.is_file(), "", text(r)[:400])
    await fall("Werkzeuge aufrufen", werkzeuge_aufrufen())

    # -- 5: Fehlerwege
    print("\n[5] Fehlerwege")

    async def fehlerwege():
        async with sitzung() as s:
            faelle = [
                ("skp_info", {"path": str(tmp / "gibtsnicht.skp")}, "nicht gefunden"),
                ("skp_info", {"path": r"\\server\freigabe\a.skp"}, "Netzwerk"),
                ("skp_convert", {"input": str(STUHL), "output": str(tmp / "stuhl.glb")}, "gibt es schon"),
                ("skp_convert", {"input": str(STUHL), "output": str(STUHL), "ueberschreiben": True},
                 "selbst eine Eingabe"),
                ("skp_convert", {"input": str(STUHL), "output": str(tmp / "a.json")}, "Endung .json"),
                ("skp_info", {}, "'path' fehlt"),
                ("skp_info", {"path": str(STUHL), "zusatz": 1}, "unbekannt"),
                ("live_screenshot", {"view": "desktop"}, "nicht erlaubt"),
            ]
            vorher = (tmp / "stuhl.glb").read_bytes() if (tmp / "stuhl.glb").exists() else None
            for name, args, erwartet in faelle:
                r = await s.call_tool(name, args)
                b.pruefe(f"{name} {json.dumps(args)[:70]}: isError mit Meldung",
                         r.is_error and erwartet in text(r) and r.structured_content is None,
                         text(r)[:110], f"isError={r.is_error}: {text(r)[:300]}")
            if vorher is not None:
                b.pruefe("vorhandene Datei unveraendert", (tmp / "stuhl.glb").read_bytes() == vorher)
            # Schema-Abgleich: was jsonschema ablehnt, lehnt auch der Server ab (und umgekehrt)
            abweichungen = []
            proben = {
                "skp_diff": [{"a": "x.skp", "b": "y.skp", "toleranz": 0}, {"a": "x.skp", "b": "y.skp", "toleranz": 1e-9},
                             {"a": "x.skp"}, {"a": "x.skp", "b": "y.skp", "geometrie": "ja"}],
                "skp_list": [{"path": "x.skp", "limit": 0}, {"path": "x.skp", "limit": 1000}, {"path": "x.skp", "limit": 1.5},
                             {"path": "x.skp", "limit": True}, {"path": ""}, {"path": "x" * 4097}],
                "live_screenshot": [{"width": 63}, {"width": 64}, {"height": 1201}, {"view": "window"}],
                "skp_report": [{"paths": []}, {"paths": ["a"] * 101}, {"paths": [5]}, {"paths": ["a"], "rekursiv": 1}],
                "live_undo": [{"steps": 0}, {"steps": 50}, {"steps": 51}],
                "skp_edit": [{"input": "a.skp", "output": "b.skp", "ops": []},
                             {"input": "a.skp", "output": "b.skp", "ops": [{"op": "gibtsnicht"}]},
                             {"input": "a.skp", "output": "b.skp", "ops": [{"select": {}}]}],
            }
            for name, liste in proben.items():
                schema = werkzeug[name].input_schema if name in werkzeug else None
                if schema is None:
                    continue
                for args in liste:
                    js_ok = Draft202012Validator(schema).is_valid(args)
                    r = await s.call_tool(name, args)
                    server_lehnt_ab = r.is_error and text(r).startswith("FEHLER: Ungueltige Argumente")
                    if js_ok == server_lehnt_ab:
                        abweichungen.append(f"{name} {json.dumps(args)[:60]}: jsonschema "
                                            f"{'gueltig' if js_ok else 'ungueltig'}, Server: {text(r)[:80]}")
            b.pruefe("Argumentpruefung des Servers stimmt mit jsonschema ueberein", not abweichungen,
                     f"{sum(map(len, proben.values()))} Proben", "; ".join(abweichungen[:4]))
            try:
                await s.call_tool("gibtsnicht", {})
                b.fehler("unbekanntes Werkzeug", "kein Protokollfehler")
            except MCPError as exc:
                b.pruefe("unbekanntes Werkzeug: JSON-RPC-Fehler -32602", exc.error.code == -32602,
                         exc.error.message, f"Code {exc.error.code}")
            try:
                await s.send_request(types.ListResourcesRequest(), types.ListResourcesResult)
                b.fehler("resources/list", "keine Fehlerantwort")
            except MCPError as exc:
                b.pruefe("nicht angebotene Methode: -32601", exc.error.code == -32601, exc.error.message)
            await s.send_ping()
            b.ok("Server laeuft nach allen Fehlern weiter (ping)")
    await fall("Fehlerwege", fehlerwege())

    # -- 6: Schalter
    print("\n[6] --nur-lesen und --ordner")

    async def nur_lesen():
        async with sitzung("--nur-lesen") as s:
            tools = (await s.list_tools()).tools
            b.pruefe("--nur-lesen: nur lesende Werkzeuge", [t.name for t in tools] == NUR_LESEN
                     and all(t.annotations.read_only_hint for t in tools), ", ".join(t.name for t in tools))
            try:
                await s.call_tool("skp_convert", {"input": str(STUHL), "output": str(tmp / "nie.glb")})
                b.fehler("--nur-lesen: skp_convert", "wurde ausgefuehrt")
            except MCPError as exc:
                b.pruefe("--nur-lesen: skp_convert abgelehnt (-32602)", exc.error.code == -32602
                         and not (tmp / "nie.glb").exists(), exc.error.message)
    await fall("--nur-lesen", nur_lesen())

    async def ordner():
        frei = tmp / "frei"
        frei.mkdir()
        shutil.copyfile(STUHL, frei / "kopie.skp")
        async with sitzung("--ordner", str(frei)) as s:
            r1 = await s.call_tool("skp_info", {"path": str(frei / "kopie.skp")})
            r2 = await s.call_tool("skp_info", {"path": str(STUHL)})
            r3 = await s.call_tool("skp_convert", {"input": str(frei / "kopie.skp"), "output": str(tmp / "draussen.glb")})
            r4 = await s.call_tool("skp_info", {"path": str(frei / ".." / "frei" / ".." / STUHL.name)})
            b.pruefe("--ordner: innen erlaubt, aussen abgelehnt (Eingabe, Ausgabe, ..)",
                     not r1.is_error and r2.is_error and r3.is_error and r4.is_error
                     and "ausserhalb" in text(r2) and not (tmp / "draussen.glb").exists(),
                     text(r2)[:100], f"{r1.is_error} {r2.is_error} {r3.is_error} {r4.is_error}")
    await fall("--ordner", ordner())

    # -- 7: Abbruch, ping, Gleichzeitigkeit
    print("\n[7] Abbruch, ping, gleichzeitige Anfragen")

    async def abbruch():
        async with sitzung() as s:
            t = time.monotonic()
            with anyio.move_on_after(0.4) as scope:
                await s.call_tool("skp_diff", {"a": str(STUHL), "b": str(STUHL), "geometrie": True})
            b.pruefe("Abbruch durch den Client (anyio-Cancel, SDK sendet notifications/cancelled)",
                     scope.cancelled_caught, f"nach {time.monotonic() - t:.1f} s")
            await s.send_ping()
            await anyio.sleep(8)  # ein nicht abgebrochener Aufruf waere jetzt fertig
            await s.send_ping()
            r = await s.call_tool("skp_info", {"path": str(STUHL)})
            b.pruefe("nach dem Abbruch: ping und Werkzeuge gehen weiter", not r.is_error)
        log.flush()
        b.pruefe("Server hat den Arbeitsprozess abgebrochen (Log)", "abgebrochen" in log_pfad.read_text("utf-8"),
                 "Eintrag 'Aufruf ... abgebrochen' im stderr-Log")
    await fall("Abbruch", abbruch())

    async def gleichzeitig():
        async with sitzung() as s:
            ergebnisse: list = []

            async def eins(i):
                if i % 3 == 0:
                    await s.send_ping()
                    ergebnisse.append("ping")
                else:
                    r = await s.call_tool("skp_info", {"path": str(STUHL)})
                    ergebnisse.append("ok" if not r.is_error else text(r))
            t = time.monotonic()
            async with anyio.create_task_group() as tg:
                for i in range(9):
                    tg.start_soon(eins, i)
            b.pruefe("9 gleichzeitige Anfragen (6 skp_info, 3 ping)", ergebnisse.count("ok") == 6
                     and ergebnisse.count("ping") == 3, f"{time.monotonic() - t:.1f} s", str(ergebnisse)[:300])
            ergebnisse.clear()

            async def flut(i):
                r = await s.call_tool("skp_info", {"path": str(STUHL)})
                ergebnisse.append("ok" if not r.is_error else ("voll" if "Zu viele" in text(r) else text(r)))
            async with anyio.create_task_group() as tg:
                for i in range(14):
                    tg.start_soon(flut, i)
            b.pruefe("14 gleichzeitige Aufrufe: jede Anfrage bekommt eine Antwort, Ueberlauf als isError",
                     len(ergebnisse) == 14 and set(ergebnisse) <= {"ok", "voll"} and ergebnisse.count("ok") >= 10,
                     f"{ergebnisse.count('ok')} ausgefuehrt, {ergebnisse.count('voll')} abgewiesen", str(ergebnisse)[:300])
    await fall("gleichzeitig", gleichzeitig())

    # -- 8: grosse Ausgaben
    if not a.schnell:
        print("\n[8] Grosse Ausgaben")

        async def gross():
            viele = tmp / "viele"
            viele.mkdir()
            for i in range(201):
                shutil.copyfile(STUHL, viele / f"stuhl_{i:03d}.skp")
            async with sitzung() as s:
                r = await s.call_tool("skp_report", {"paths": [str(viele / "*.skp")]})
                b.pruefe("skp_report mit 201 Dateien: Grenze 200", r.is_error and "hoechstens 200" in text(r),
                         text(r)[:100])
                (viele / "stuhl_200.skp").unlink()
                t = time.monotonic()
                r = await s.call_tool("skp_report", {"paths": [str(viele / "*.skp")]})
                groesse = len(text(r).encode("utf-8"))
                sc = r.structured_content or {}
                b.pruefe("skp_report mit 200 Dateien: Text hoechstens 200 KB, gueltiges JSON",
                         not r.is_error and groesse <= 200 * 1024 and json.loads(text(r)) == sc,
                         f"{groesse} Bytes Text, anzahl {sc.get('anzahl')}, gekuerzt: {bool(sc.get('_gekuerzt'))}, "
                         f"{time.monotonic() - t:.0f} s", f"isError {r.is_error}, {groesse} Bytes: {text(r)[:200]}")
        await fall("grosse Ausgaben", gross())

    # -- 9: Live-Werkzeuge gegen ein echtes Blender-Fenster
    if a.live:
        print("\n[9] Live-Werkzeuge mit echtem Blender-Fenster")
        await fall("Live", live_pruefung(b, a, tmp, sitzung, text))

    log.close()
    code = b.zusammenfassung()
    if code == 0:
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print(f"Arbeitsordner und Server-Log bleiben: {tmp}")
    return code


async def live_pruefung(b, a, tmp, sitzung, text):
    """skptool open <kopie> --live starten, die live_*-Werkzeuge pruefen, Blender wieder beenden.
    Eigene Statusdatei, damit ein anderes Live-Blender auf dem Rechner nicht gestoert wird.
    Beendet wird nur das hier gestartete Blender."""
    import anyio

    ordner = tmp / "live"
    ordner.mkdir()
    kopie = ordner / "stuhl.skp"
    shutil.copyfile(STUHL, kopie)
    state = ordner / "live.json"
    env = dict(os.environ, SKPTOOL_LIVE_STATE=str(state))
    start = await anyio.to_thread.run_sync(lambda: subprocess.run(
        [a.server_python, "-m", "skptool", "open", str(kopie), "--live"], cwd=str(ROOT), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600))
    m = re.search(r"PID (\d+)", start.stdout)
    if start.returncode != 0 or not m:
        b.fehler("skptool open --live", (start.stdout + start.stderr)[-600:])
        return
    pid = int(m.group(1))
    b.ok("skptool open --live gestartet", f"Blender PID {pid}")
    try:
        async with sitzung(env={"SKPTOOL_LIVE_STATE": str(state)}) as s:
            r = await s.call_tool("live_status")
            n = (r.structured_content or {}).get("objects")
            b.pruefe("live_status", not r.is_error and isinstance(n, int), f"{n} Objekte", text(r)[:300])
            r = await s.call_tool("live_ops", {"ops": [{"op": "add_box", "size": [1, 1, 1], "at": [3, 0, 0],
                                                         "name": "SdkKiste"}]})
            b.pruefe("live_ops add_box", not r.is_error, text(r)[:120], text(r)[:300])
            r = await s.call_tool("live_status")
            b.pruefe("live_status danach ein Objekt mehr", (r.structured_content or {}).get("objects") == n + 1)
            r = await s.call_tool("live_screenshot", {"width": 320, "height": 200})
            bilder = [c for c in r.content if c.type == "image"]
            png = base64.b64decode(bilder[0].data) if bilder else b""
            b.pruefe("live_screenshot: image/png 320x200", not r.is_error and len(bilder) == 1
                     and bilder[0].mime_type == "image/png" and png[:8] == b"\x89PNG\r\n\x1a\n"
                     and struct.unpack(">II", png[16:24]) == (320, 200), f"{len(png)} Bytes PNG", text(r)[:300])
            r = await s.call_tool("live_screenshot", {"view": "window", "width": 1920, "height": 1200})
            png = b"".join(base64.b64decode(c.data) for c in r.content if c.type == "image")
            b.pruefe("live_screenshot window 1920x1200 (grosses Bild)", not r.is_error and png[:4] == b"\x89PNG",
                     f"{len(png)} Bytes", text(r)[:300])
            r = await s.call_tool("live_undo", {"steps": 1})
            r2 = await s.call_tool("live_status")
            b.pruefe("live_undo", not r.is_error and (r2.structured_content or {}).get("objects") == n,
                     f"wieder {n} Objekte", text(r)[:300])
            r = await s.call_tool("live_ops", {"ops": [{"op": "move", "select": {"name": "gibtsnicht*"},
                                                         "by": [0, 0, 1]}]})
            b.pruefe("live_ops ohne Treffer: isError", r.is_error, text(r)[:120])
    finally:
        ende = await anyio.to_thread.run_sync(lambda: subprocess.run(
            [a.server_python, "-m", "skptool", "live", "--quit", "--force"], cwd=str(ROOT), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120))
        weg = await anyio.to_thread.run_sync(_warte_auf_ende, pid, 60)
        if not weg:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"] if os.name == "nt" else ["kill", "-9", str(pid)],
                           capture_output=True)
        b.pruefe("Blender mit skptool live --quit beendet", weg, f"PID {pid} beendet",
                 f"musste beendet werden: {ende.stdout} {ende.stderr}"[:300])


def _laeuft(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True,
                             text=True, errors="replace").stdout
        return f'"{pid}"' in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _warte_auf_ende(pid: int, sekunden: float) -> bool:
    ende = time.monotonic() + sekunden
    while time.monotonic() < ende:
        if not _laeuft(pid):
            return True
        time.sleep(0.5)
    return False


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--live", action="store_true", help="live_* mit echtem Blender-Fenster pruefen")
    ap.add_argument("--schnell", action="store_true", help="ohne Blender-Werkzeuge und ohne grosse Ausgaben")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--server-python", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if importlib.util.find_spec("mcp") is None or importlib.util.find_spec("jsonschema") is None:
        return _neu_starten(argv)
    if not a.server_python:
        kandidat = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not kandidat.is_file():
            print("--server-python fehlt: mit dem Python aufrufen, das skptool ausfuehren kann")
            return 1
        a.server_python = str(kandidat)
    return main_sdk(a)


if __name__ == "__main__":
    sys.exit(main())
