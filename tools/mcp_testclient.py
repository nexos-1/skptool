"""Kleiner MCP-Testclient: spricht mit "skptool mcp" so, wie es Claude Code und Claude Desktop tun.

Ablauf wie beim offiziellen TypeScript-SDK (Client.connect): initialize mit der neuesten
Protokollversion, Pruefung der Antwort, notifications/initialized, dann tools/list und ein
Werkzeugaufruf. Jede Antwort wird gegen die Formen geprueft, die ein Client erwartet
(Tool: name, inputSchema vom Typ object; CallToolResult: content mit text/image). Aendert keine
Einstellungen, weder von Claude Code noch von Claude Desktop.

Aufruf (aus dem Projektordner):
  .venv\\Scripts\\python tools\\mcp_testclient.py
  .venv\\Scripts\\python tools\\mcp_testclient.py --datei samples\\stuhl_tisch_2017.skp
  .venv\\Scripts\\python tools\\mcp_testclient.py -- C:\\Pfad\\skptool.cmd mcp --nur-lesen

Rueckgabewert 0: alles in Ordnung, 1: Abweichung (mit Meldung auf stderr).
"""
import argparse
import base64
import json
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Versionen, die das TypeScript-SDK kennt; die erste schickt es beim initialize
CLIENT_VERSIONEN = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05", "2024-10-07"]
NAME_MUSTER = re.compile(r"^[A-Za-z0-9_-]{1,64}$")  # Claude Code: mcp__<server>__<werkzeug>


class Abweichung(Exception):
    pass


def _pruefe(bedingung, text):
    if not bedingung:
        raise Abweichung(text)


class Verbindung:
    def __init__(self, befehl, cwd=None):
        self.proc = subprocess.Popen(befehl, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        self.zeilen: "queue.Queue[bytes | None]" = queue.Queue()
        self.alle: list[bytes] = []
        self.log: list[bytes] = []
        self.leser = [threading.Thread(target=self._lesen, daemon=True),
                      threading.Thread(target=lambda: self.log.extend(iter(self.proc.stderr.readline, b"")),
                                       daemon=True)]
        for th in self.leser:
            th.start()
        self.naechste_id = 0

    def _lesen(self):
        for zeile in iter(self.proc.stdout.readline, b""):
            self.alle.append(zeile)
            self.zeilen.put(zeile)
        self.zeilen.put(None)

    def senden(self, msg):
        self.proc.stdin.write(json.dumps(msg).encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    def anfrage(self, methode, params=None, timeout=300):
        self.naechste_id += 1
        rid = self.naechste_id
        msg = {"jsonrpc": "2.0", "id": rid, "method": methode}
        if params is not None:
            msg["params"] = params
        self.senden(msg)
        while True:
            try:
                zeile = self.zeilen.get(timeout=timeout)
            except queue.Empty:
                raise Abweichung(f"keine Antwort auf {methode} nach {timeout} s") from None
            if zeile is None:
                raise Abweichung(f"Server hat sich waehrend {methode} beendet: "
                                 + b"".join(self.log[-10:]).decode("utf-8", "replace"))
            antwort = json.loads(zeile)
            if antwort.get("id") == rid:
                return antwort

    def schliessen(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise Abweichung("Server hat sich nach dem Schliessen von stdin nicht beendet") from None
        finally:
            for th in self.leser:
                th.join(10)
            self.proc.stdout.close()
            self.proc.stderr.close()


def pruefe_werkzeug(t):
    _pruefe(isinstance(t, dict), "Werkzeug ist kein Objekt")
    _pruefe(isinstance(t.get("name"), str) and NAME_MUSTER.match(t["name"]), f"ungueltiger Name {t.get('name')!r}")
    _pruefe(isinstance(t.get("description"), str) and t["description"], f"{t['name']}: Beschreibung fehlt")
    s = t.get("inputSchema")
    _pruefe(isinstance(s, dict) and s.get("type") == "object", f"{t['name']}: inputSchema.type muss object sein")
    props = s.get("properties", {})
    _pruefe(isinstance(props, dict), f"{t['name']}: properties muss ein Objekt sein")
    for k in s.get("required", []):
        _pruefe(k in props, f"{t['name']}: Pflichtfeld {k} fehlt in properties")
    for k, p in props.items():
        _pruefe(isinstance(p, dict) and p.get("type") in ("string", "integer", "number", "boolean", "array", "object"),
                f"{t['name']}.{k}: type fehlt oder ist unbekannt")
    for k, v in (t.get("annotations") or {}).items():
        if k.endswith("Hint"):
            _pruefe(isinstance(v, bool), f"{t['name']}: annotations.{k} muss boolean sein")


def pruefe_ergebnis(r):
    _pruefe(isinstance(r.get("content"), list) and r["content"], "content fehlt")
    for c in r["content"]:
        if c.get("type") == "text":
            _pruefe(isinstance(c.get("text"), str), "text-Inhalt ohne text")
        elif c.get("type") == "image":
            base64.b64decode(c["data"], validate=True)
            _pruefe(c.get("mimeType") == "image/png", "Bild ohne mimeType image/png")
        else:
            raise Abweichung(f"unerwarteter Inhaltstyp {c.get('type')!r}")
    _pruefe(isinstance(r.get("isError", False), bool), "isError muss boolean sein")
    if "structuredContent" in r:
        _pruefe(isinstance(r["structuredContent"], dict), "structuredContent muss ein Objekt sein")


def lauf(befehl, datei, cwd=None, ausgabe=print) -> None:
    v = Verbindung(befehl, cwd)
    try:
        init = v.anfrage("initialize", {
            "protocolVersion": CLIENT_VERSIONEN[0],
            "capabilities": {"roots": {}, "elicitation": {}},
            "clientInfo": {"name": "skptool-testclient", "version": "1.0.0"}})
        _pruefe("result" in init, f"initialize fehlgeschlagen: {init}")
        res = init["result"]
        _pruefe(res.get("protocolVersion") in CLIENT_VERSIONEN,
                f"Server antwortet mit unbekannter Protokollversion {res.get('protocolVersion')!r}")
        _pruefe("tools" in (res.get("capabilities") or {}), "Server meldet keine tools-Faehigkeit")
        _pruefe(isinstance((res.get("serverInfo") or {}).get("name"), str), "serverInfo.name fehlt")
        ausgabe(f"initialize: Protokoll {res['protocolVersion']}, Server {res['serverInfo']['name']} "
                f"{res['serverInfo'].get('version')}")
        v.senden({"jsonrpc": "2.0", "method": "notifications/initialized"})
        _pruefe(v.anfrage("ping").get("result") == {}, "ping ohne leeres Ergebnis")
        tools = v.anfrage("tools/list")
        _pruefe("result" in tools, f"tools/list fehlgeschlagen: {tools}")
        liste = tools["result"]["tools"]
        for t in liste:
            pruefe_werkzeug(t)
        ausgabe(f"tools/list: {len(liste)} Werkzeuge: {', '.join(t['name'] for t in liste)}")
        if datei:
            r = v.anfrage("tools/call", {"name": "skp_info", "arguments": {"path": str(datei)}})
            _pruefe("result" in r, f"tools/call fehlgeschlagen: {r}")
            pruefe_ergebnis(r["result"])
            _pruefe(not r["result"]["isError"], "skp_info meldet einen Fehler: " + r["result"]["content"][0]["text"])
            info = r["result"].get("structuredContent", {})
            ausgabe(f"skp_info: Version {info.get('version')}, {len(info.get('layers', []))} Ebenen, "
                    f"{len(info.get('components', []))} Komponenten")
    finally:
        v.schliessen()
    for zeile in v.alle:  # stdout darf nur JSON-RPC enthalten
        msg = json.loads(zeile)
        _pruefe(isinstance(msg, dict) and msg.get("jsonrpc") == "2.0", f"keine JSON-RPC-Zeile auf stdout: {zeile!r}")
    ausgabe(f"stdout: {len(v.alle)} Zeilen, alle JSON-RPC; Server beendet mit Code {v.proc.returncode}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--datei", default=str(ROOT / "samples" / "stuhl_tisch_2017.skp"),
                    help="Datei fuer den Probeaufruf von skp_info (leer: keiner)")
    ap.add_argument("befehl", nargs="*", help="Serverbefehl nach --, Standard: dieses Python mit -m skptool mcp")
    a = ap.parse_args(argv)
    befehl = a.befehl or [sys.executable, "-m", "skptool", "mcp"]
    try:
        lauf(befehl, a.datei, cwd=str(ROOT))
    except (Abweichung, ValueError, KeyError) as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
