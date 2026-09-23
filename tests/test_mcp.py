"""Tests fuer den MCP-Server (skptool mcp). Start: .venv\\Scripts\\python -m unittest tests.test_mcp -v

Der Server laeuft als eigener Prozess (python -m skptool mcp), die Tests sprechen JSON-RPC ueber
Pipes wie ein echter Client. Tests mit Blender werden ohne Blender uebersprungen; der Live-Test mit
Blender-Fenster zusaetzlich mit SKPTOOL_SKIP_GUI_TESTS=1. Die uebrigen live_*-Werkzeuge laufen gegen
einen nachgebauten Live-Server mit dem echten Protokoll (HMAC), ohne Blender.
"""
import base64
import hashlib
import hmac
import json
import os
import queue
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zlib
from pathlib import Path

from skptool import mcp_server
from skptool.blender import BlenderError, find_blender

ROOT = Path(__file__).resolve().parents[1]
S2017 = ROOT / "samples" / "stuhl_tisch_2017.skp"
ALLE = ["skp_info", "skp_list", "skp_diff", "skp_report", "skp_convert", "skp_edit",
        "live_status", "live_ops", "live_screenshot", "live_undo"]
NUR_LESEN = ["skp_info", "skp_list", "skp_diff", "skp_report", "live_status", "live_screenshot"]
SCHEMA_WOERTER = {"type", "properties", "required", "additionalProperties", "items", "minItems", "maxItems",
                  "minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum", "enum", "default",
                  "description"}

try:
    BLENDER = find_blender()
except BlenderError:
    BLENDER = None
GUI_OK = BLENDER is not None and not os.environ.get("SKPTOOL_SKIP_GUI_TESTS")


class Client:
    """Startet python -m skptool mcp und spricht JSON-RPC ueber stdin/stdout."""

    def __init__(self, *server_args, env=None, cwd=ROOT):
        self.stderr = tempfile.TemporaryFile()
        self.proc = subprocess.Popen([sys.executable, "-m", "skptool", "mcp", *server_args], cwd=cwd,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
                                     env={**os.environ, **(env or {})})
        self.zeilen = queue.Queue()
        self.roh: list[bytes] = []
        self.rid = 0
        self.leser = threading.Thread(target=self._lesen, daemon=True)
        self.leser.start()

    def _lesen(self):
        for zeile in iter(self.proc.stdout.readline, b""):
            self.roh.append(zeile)
            self.zeilen.put(json.loads(zeile))
        self.zeilen.put(None)

    def roh_senden(self, data: bytes):
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def senden(self, msg):
        self.roh_senden(json.dumps(msg).encode("utf-8") + b"\n")

    def empfangen(self, timeout=60):
        msg = self.zeilen.get(timeout=timeout)
        if msg is None:
            raise AssertionError("Server beendet: " + self.log())
        return msg

    def anfrage(self, methode, params=None, timeout=600):
        self.rid += 1
        msg = {"jsonrpc": "2.0", "id": self.rid, "method": methode}
        if params is not None:
            msg["params"] = params
        self.senden(msg)
        antwort = self.empfangen(timeout)
        assert antwort.get("id") == self.rid, antwort
        return antwort

    def init(self, version="2025-06-18"):
        r = self.anfrage("initialize", {"protocolVersion": version, "capabilities": {},
                                        "clientInfo": {"name": "test_mcp", "version": "1"}})
        self.senden({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return r

    def call(self, name, args=None, timeout=600):
        r = self.anfrage("tools/call", {"name": name, "arguments": args or {}}, timeout)
        return r["result"] if "result" in r else r

    def log(self):
        self.stderr.seek(0)
        return self.stderr.read().decode("utf-8", "replace")

    def schliessen(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=30)
        finally:
            if self.proc.poll() is None:
                self.proc.kill()
                self.proc.wait(10)
            self.leser.join(10)
            self.proc.stdout.close()
            self.stderr.close()
        return self.proc.returncode


def text_of(result):
    return "".join(c.get("text", "") for c in result["content"] if c["type"] == "text")


class TestProtokoll(unittest.TestCase):
    """Ein Server fuer alle Protokolltests; am Ende: stdout enthaelt nur JSON-RPC."""

    @classmethod
    def setUpClass(cls):
        cls.c = Client()
        cls.tmp = Path(tempfile.mkdtemp(prefix="skptool_mcp_test_"))

    @classmethod
    def tearDownClass(cls):
        rc = cls.c.schliessen()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        assert rc == 0, f"Server endete mit {rc}"
        for zeile in cls.c.roh:
            msg = json.loads(zeile)
            assert isinstance(msg, dict) and msg.get("jsonrpc") == "2.0", zeile
            assert zeile.endswith(b"\n") and zeile.count(b"\n") == 1, zeile
            assert zeile.isascii(), zeile

    def test_01_initialize(self):
        r = self.c.init("2025-06-18")["result"]
        self.assertEqual(r["protocolVersion"], "2025-06-18")
        self.assertEqual(r["capabilities"], {"tools": {}})
        self.assertEqual(r["serverInfo"]["name"], "skptool")
        self.assertIn("serverInfo", r)
        # bekannte aeltere Version wird uebernommen, unbekannte bekommt die Standardversion
        self.assertEqual(self.c.anfrage("initialize", {"protocolVersion": "2024-11-05"})["result"]["protocolVersion"],
                         "2024-11-05")
        self.assertEqual(self.c.anfrage("initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"],
                         "2025-06-18")

    def test_02_ping(self):
        self.assertEqual(self.c.anfrage("ping")["result"], {})

    def test_03_tools_list_schemas(self):
        tools = self.c.anfrage("tools/list")["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ALLE)

        def woerter(schema, wo):
            self.assertLessEqual(set(schema) - SCHEMA_WOERTER, set(), wo)
            for k, s in schema.get("properties", {}).items():
                self.assertIn(s.get("type"), ("string", "integer", "number", "boolean", "array", "object"), wo + k)
                woerter(s, f"{wo}.{k}")
            if "items" in schema:
                woerter(schema["items"], wo + "[]")

        for t in tools:
            s = t["inputSchema"]
            self.assertEqual(s["type"], "object", t["name"])
            self.assertIs(s["additionalProperties"], False, t["name"])
            self.assertLessEqual(set(s.get("required", [])), set(s["properties"]), t["name"])
            woerter(s, t["name"])
            self.assertTrue(t["description"].isascii(), t["name"])
            self.assertNotIn(chr(0x2014), json.dumps(t))
            self.assertIn(t["annotations"]["readOnlyHint"], (True, False))
            self.assertFalse(any(k.startswith("_") for k in t), t["name"])
            # keine Moeglichkeit, externe Dateien freizuschalten
            self.assertNotIn("allow_external", json.dumps(s))
        ops = next(t for t in tools if t["name"] == "skp_edit")["inputSchema"]["properties"]["ops"]
        self.assertEqual(set(ops["items"]["properties"]["op"]["enum"]), set(mcp_server.OP_NAMEN))
        self.assertTrue({"move", "duplicate", "add_box", "array", "measure"} <= set(mcp_server.OP_NAMEN))

    def test_04_skp_info(self):
        r = self.c.call("skp_info", {"path": str(S2017)})
        self.assertFalse(r["isError"], text_of(r))
        info = r["structuredContent"]
        self.assertEqual(info["version"], "17.0.1")
        self.assertIn("Chair", [l["name"] for l in info["layers"]])
        self.assertEqual(json.loads(text_of(r)), info)  # Text und Struktur stimmen ueberein
        # relativer Pfad gilt ab dem Arbeitsordner des Servers
        r = self.c.call("skp_info", {"path": "samples/stuhl_tisch_2017.skp"})
        self.assertFalse(r["isError"], text_of(r))

    def test_05_skp_diff_same_file(self):
        r = self.c.call("skp_diff", {"a": str(S2017), "b": str(S2017)})
        self.assertFalse(r["isError"], text_of(r))
        self.assertIs(r["structuredContent"]["gleich"], True)
        self.assertEqual(set(r["structuredContent"]["abschnitte"]),
                         {"modell", "ebenen", "materialien", "definitionen", "platzierungen"})

    def test_06_skp_report(self):
        r = self.c.call("skp_report", {"paths": [str(ROOT / "samples" / "*.skp")]})
        self.assertFalse(r["isError"], text_of(r))
        self.assertGreaterEqual(r["structuredContent"]["anzahl"], 2)
        r = self.c.call("skp_report", {"paths": [str(self.tmp / "gibtsnicht_*.skp")]})
        self.assertTrue(r["isError"])
        self.assertIn("Keine Datei passt", text_of(r))

    def test_07_unknown_method_and_tool(self):
        r = self.c.anfrage("resources/list")
        self.assertEqual(r["error"]["code"], -32601)
        r = self.c.anfrage("tools/call", {"name": "format_c", "arguments": {}})
        self.assertEqual(r["error"]["code"], -32602)
        r = self.c.anfrage("tools/call", {"arguments": {}})
        self.assertEqual(r["error"]["code"], -32602)
        r = self.c.anfrage("tools/call", {"name": "skp_info", "arguments": "path=x"})
        self.assertEqual(r["error"]["code"], -32602)
        r = self.c.anfrage("tools/list", ["kein", "objekt"])
        self.assertEqual(r["error"]["code"], -32602)

    def test_08_malformed_lines_do_not_crash(self):
        self.c.roh_senden(b"{kein json\n")
        r = self.c.empfangen()
        self.assertEqual(r["error"]["code"], -32700)
        self.assertIsNone(r["id"])
        self.c.roh_senden(b"\xff\xfe\x00kaputt\n")
        self.assertEqual(self.c.empfangen()["error"]["code"], -32700)
        self.c.roh_senden(b'{"jsonrpc": "2.0", "id": 5, "method": "ping", "params": {"x": NaN}}\n')
        self.assertEqual(self.c.empfangen()["error"]["code"], -32700)
        self.c.roh_senden(b"[" * 100000 + b"\n")
        self.assertEqual(self.c.empfangen()["error"]["code"], -32700)
        self.c.senden([{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
        self.assertEqual(self.c.empfangen()["error"]["code"], -32600)
        self.c.senden({"jsonrpc": "1.0", "id": 1, "method": "ping"})
        self.assertEqual(self.c.empfangen()["error"]["code"], -32600)
        self.c.senden({"jsonrpc": "2.0", "id": None, "method": "ping"})
        self.assertEqual(self.c.empfangen()["error"]["code"], -32600)
        self.c.roh_senden(b"\n\r\n")  # Leerzeilen werden still uebergangen
        # unbekannte Benachrichtigungen und Antworten bekommen keine Antwort
        self.c.senden({"jsonrpc": "2.0", "method": "notifications/irgendwas", "params": {}})
        self.c.senden({"jsonrpc": "2.0", "id": 77, "result": {}})
        self.assertEqual(self.c.anfrage("ping")["result"], {})

    def test_09_bad_arguments_are_tool_errors(self):
        faelle = [
            ("skp_info", {}, "'path' fehlt"),
            ("skp_info", {"path": 5}, "muss ein Text sein"),
            ("skp_info", {"path": str(S2017), "allow_external": True}, "unbekannt"),
            ("skp_list", {"path": str(S2017), "limit": 0}, "mindestens 1"),
            ("skp_list", {"path": str(S2017), "limit": True}, "ganze Zahl"),
            ("skp_diff", {"a": str(S2017), "b": str(S2017), "toleranz": 0}, "groesser als 0"),
            ("live_screenshot", {"width": 100000}, "hoechstens 1920"),
            ("live_screenshot", {"view": "desktop"}, "nicht erlaubt"),
        ]
        for name, args, erwartet in faelle:
            r = self.c.call(name, args)
            self.assertTrue(r["isError"], (name, args))
            self.assertIn(erwartet, text_of(r), (name, args))

    def test_10_invalid_ops_rejected(self):
        ziel = self.tmp / "nie.skp"
        faelle = [
            ("kein array", "muss eine Liste sein"),
            ([], "mindestens 1"),
            ([{"op": "format_c"}], "nicht erlaubt"),
            ([{"select": {}}], "'op' fehlt"),
            (["move"], "muss ein Objekt sein"),
            ([{"op": "summary"}] * 1001, "hoechstens 1000"),
        ]
        for ops, erwartet in faelle:
            r = self.c.call("skp_edit", {"input": str(S2017), "output": str(ziel), "ops": ops})
            self.assertTrue(r["isError"], ops)
            self.assertIn(erwartet, text_of(r), str(ops)[:80])
        # 1e999 wird beim Einlesen unendlich: dieselbe Pruefung wie --ops lehnt es ab
        self.c.roh_senden(json.dumps({"jsonrpc": "2.0", "id": "inf", "method": "tools/call", "params": {
            "name": "live_ops", "arguments": {"ops": [{"op": "move", "by": [0, 0, 0]}]}}})
                          .replace("[0, 0, 0]", "[1e999, 0, 0]").encode() + b"\n")
        r = self.c.empfangen()
        self.assertEqual(r["id"], "inf")
        self.assertTrue(r["result"]["isError"])
        self.assertIn("keine gueltige Zahl", text_of(r["result"]))
        self.assertFalse(ziel.exists())

    def test_11_paths_are_checked(self):
        vorhanden = self.tmp / "vorhanden.skp"
        vorhanden.write_bytes(b"alt")
        faelle = [
            ({"input": r"\\server\freigabe\a.skp", "output": str(self.tmp / "a.skp")}, "Netzwerk"),
            ({"input": "//server/freigabe/a.skp", "output": str(self.tmp / "a.skp")}, "Netzwerk"),
            ({"input": "file://server/a.skp", "output": str(self.tmp / "a.skp")}, "URL"),
            ({"input": str(S2017), "output": str(self.tmp / "a.py")}, "Endung .py"),
            ({"input": str(S2017), "output": str(self.tmp / "a.json")}, "Endung .json"),
            ({"input": str(S2017), "output": str(self.tmp / "a.obj")}, "Endung .obj"),
            ({"input": str(self.tmp / "fehlt.skp"), "output": str(self.tmp / "a.skp")}, "nicht gefunden"),
            ({"input": str(S2017), "output": str(self.tmp / "kein_ordner" / "a.skp")}, "Zielordner"),
            ({"input": str(S2017), "output": str(vorhanden)}, "gibt es schon"),
            ({"input": str(S2017), "output": str(S2017), "ueberschreiben": True}, "selbst eine Eingabe"),
            ({"input": str(S2017), "output": str(self.tmp / "a.skp\x1b[31m")}, "Steuerzeichen"),
        ]
        if os.name == "nt":
            faelle += [
                ({"input": str(S2017), "output": str(S2017).upper(), "ueberschreiben": True}, "selbst eine Eingabe"),
                ({"input": str(S2017), "output": str(self.tmp / "a.skp:versteckt")}, "Doppelpunkt"),
                ({"input": str(S2017), "output": str(self.tmp) + "\\x:y.skp"}, "Doppelpunkt"),
                ({"input": str(S2017), "output": str(self.tmp / "NUL.skp")}, "Geraetename"),
                ({"input": r"\\?\C:\a.skp", "output": str(self.tmp / "a.skp")}, "Netzwerk"),
            ]
        for args, erwartet in faelle:
            r = self.c.call("skp_convert", args)
            self.assertTrue(r["isError"], args)
            self.assertIn(erwartet, text_of(r), args)
        self.assertEqual(vorhanden.read_bytes(), b"alt")
        self.assertFalse((self.tmp / "a.skp").exists())
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["vorhanden.skp"])

    def test_12_skp_convert_native_writes_new_file(self):
        ziel = self.tmp / "stuhl.glb"
        r = self.c.call("skp_convert", {"input": str(S2017), "output": str(ziel)})
        self.assertFalse(r["isError"], text_of(r))
        self.assertTrue(ziel.read_bytes().startswith(b"glTF"))
        self.assertEqual(r["structuredContent"]["output"], str(ziel.resolve()))
        vorher = ziel.read_bytes()
        r = self.c.call("skp_convert", {"input": str(S2017), "output": str(ziel)})
        self.assertTrue(r["isError"])
        self.assertIn("gibt es schon", text_of(r))
        self.assertEqual(ziel.read_bytes(), vorher)
        self.assertEqual([p.name for p in self.tmp.iterdir() if "skptool-tmp" in p.name], [])
        # skp -> skp (2017-Format) ohne Blender
        ziel2 = self.tmp / "stuhl_neu.skp"
        r = self.c.call("skp_convert", {"input": str(S2017), "output": str(ziel2)})
        self.assertFalse(r["isError"], text_of(r))
        r = self.c.call("skp_diff", {"a": str(S2017), "b": str(ziel2)})
        self.assertFalse(r["isError"], text_of(r))

    def test_13_cancelled_request_gets_no_answer(self):
        self.c.senden({"jsonrpc": "2.0", "id": "abbruch", "method": "tools/call",
                       "params": {"name": "skp_diff", "arguments": {"a": str(S2017), "b": str(S2017),
                                                                     "geometrie": True}}})
        self.c.senden({"jsonrpc": "2.0", "method": "notifications/cancelled",
                       "params": {"requestId": "abbruch", "reason": "Test"}})
        self.assertEqual(self.c.anfrage("ping")["result"], {})
        time.sleep(6)  # ein nicht abgebrochener Aufruf waere jetzt fertig
        self.assertEqual(self.c.anfrage("ping")["result"], {})  # dazwischen kam keine andere Antwort


class TestServerSchalter(unittest.TestCase):
    def test_nur_lesen_hides_write_tools(self):
        c = Client("--nur-lesen")
        try:
            c.init()
            names = [t["name"] for t in c.anfrage("tools/list")["result"]["tools"]]
            self.assertEqual(names, NUR_LESEN)
            for t in c.anfrage("tools/list")["result"]["tools"]:
                self.assertIs(t["annotations"]["readOnlyHint"], True)
            for name in ("skp_convert", "skp_edit", "live_ops", "live_undo"):
                r = c.anfrage("tools/call", {"name": name, "arguments": {}})
                self.assertEqual(r["error"]["code"], -32602, name)
                self.assertIn("--nur-lesen", r["error"]["message"])
            self.assertFalse(c.call("skp_info", {"path": str(S2017)})["isError"])
        finally:
            self.assertEqual(c.schliessen(), 0)

    def test_timeout_ends_call_with_error(self):
        c = Client("--timeout", "0.3")
        try:
            c.init()
            t = time.time()
            r = c.call("skp_diff", {"a": str(S2017), "b": str(S2017), "geometrie": True}, timeout=60)
            self.assertLess(time.time() - t, 30)
            self.assertTrue(r["isError"])
            self.assertIn("Zeitlimit", text_of(r))
            self.assertEqual(c.anfrage("ping")["result"], {})  # der Server laeuft weiter
        finally:
            c.schliessen()

    def test_ordner_restricts_paths(self):
        tmp = Path(tempfile.mkdtemp(prefix="skptool_mcp_test_"))
        try:
            kopie = tmp / "kopie.skp"
            shutil.copyfile(S2017, kopie)
            c = Client("--ordner", str(tmp))
            try:
                c.init()
                self.assertFalse(c.call("skp_info", {"path": str(kopie)})["isError"])
                r = c.call("skp_info", {"path": str(S2017)})
                self.assertTrue(r["isError"])
                self.assertIn("ausserhalb der freigegebenen Ordner", text_of(r))
                r = c.call("skp_info", {"path": str(tmp / ".." / S2017.name)})
                self.assertTrue(r["isError"])
            finally:
                c.schliessen()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_stdin_eof_ends_server(self):
        c = Client()
        c.init()
        self.assertEqual(c.schliessen(), 0)


class TestHilfsfunktionen(unittest.TestCase):
    def test_output_is_capped(self):
        gross = {"liste": [{"name": "x" * 100, "i": i} for i in range(20000)], "wert": 1}
        r = mcp_server.ergebnis(gross)
        self.assertLessEqual(len(r["content"][0]["text"].encode()), mcp_server.MAX_TEXT)
        self.assertIn("_gekuerzt", r["structuredContent"])
        self.assertEqual(json.loads(r["content"][0]["text"])["wert"], 1)
        # ein einzelner riesiger Text laesst sich nicht kuerzen: harte Grenze mit Hinweis, keine Struktur
        r = mcp_server.ergebnis({"t": "y" * (mcp_server.MAX_TEXT * 2)})
        self.assertIn("[gekuerzt:", r["content"][0]["text"])
        self.assertLess(len(r["content"][0]["text"]), mcp_server.MAX_TEXT + 200)
        self.assertNotIn("structuredContent", r)

    def test_control_and_bidi_characters_are_masked(self):
        r = mcp_server.ergebnis({"name": "a\x1b]52;c;x\x07\u202eb\x85"})
        text = r["content"][0]["text"]
        self.assertTrue(all(ord(ch) >= 32 and ch not in "\u202e\x85" for ch in text))
        self.assertEqual(json.loads(text)["name"], "a\x1b]52;c;x\x07\u202eb\x85")  # gueltiges JSON

    def test_op_names_from_ops_py(self):
        self.assertIn("move", mcp_server.OP_NAMEN)
        self.assertIn("list", mcp_server.OP_NAMEN)  # @op(name="list")
        self.assertNotIn("list_objects", mcp_server.OP_NAMEN)
        # neue Operationen aus ops.py erscheinen ohne Aenderung am Server
        for neu in ("align", "distribute", "array", "mirror", "hide", "show", "measure"):
            self.assertIn(neu, mcp_server.OP_NAMEN)

    def test_no_overwrite_move(self):
        tmp = Path(tempfile.mkdtemp(prefix="skptool_mcp_test_"))
        try:
            quelle, ziel = tmp / "q.bin", tmp / "z.bin"
            quelle.write_bytes(b"neu")
            ziel.write_bytes(b"alt")
            with self.assertRaises(mcp_server.Abgelehnt):
                mcp_server.uebernehmen(quelle, ziel, False)
            self.assertEqual(ziel.read_bytes(), b"alt")
            mcp_server.uebernehmen(quelle, ziel, True)
            self.assertEqual(ziel.read_bytes(), b"neu")
            self.assertEqual(sorted(p.name for p in tmp.iterdir()), ["q.bin", "z.bin"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _png(w=4, h=3) -> bytes:
    def chunk(typ, data):
        return struct.pack(">I", len(data)) + typ + data + struct.pack(">I", zlib.crc32(typ + data))
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class FakeLive:
    """Nachgebauter Live-Server mit dem echten Protokoll (HMAC in beide Richtungen)."""

    def __init__(self, token):
        self.token = token
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        self.anfragen = []
        self.bilder = Path(tempfile.mkdtemp(prefix="skptool_live_", dir=tempfile.gettempdir()))
        threading.Thread(target=self._loop, daemon=True).start()

    def mac(self, *parts):
        return hmac.new(self.token.encode(), "|".join(parts).encode(), hashlib.sha256).hexdigest()

    def _loop(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            with conn:
                nonce = "a1" * 16
                conn.sendall(json.dumps({"hello": nonce}).encode() + b"\n")
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                req = json.loads(buf)
                self.anfragen.append(req)
                if req["auth"] != self.mac("client", nonce, req["cnonce"]):
                    continue
                cmd = req["cmd"]
                if cmd == "status":
                    resp = {"ok": True, "blender": "5.2", "pid": 1, "port": self.port, "blend": "x.blend",
                            "dirty": False, "objects": 2, "mode": "OBJECT", "export": {"state": "keiner"}}
                elif cmd == "screenshot":
                    bild = self.bilder / "shot.png"
                    bild.write_bytes(_png())
                    resp = {"ok": True, "path": str(bild), "ms": 5, "width": req["width"], "height": req["height"]}
                elif cmd == "ops":
                    resp = {"ok": True, "results": [{"op": o["op"], "ok": True} for o in req["ops"]],
                            "undo_step": "skptool", "ms": 1}
                elif cmd == "undo":
                    resp = {"ok": True, "undone": req["steps"]}
                else:
                    resp = {"ok": False, "error": "unbekannt"}
                resp["proof"] = self.mac("server", nonce, req["cnonce"])
                conn.sendall(json.dumps(resp).encode() + b"\n")

    def close(self):
        self.srv.close()
        shutil.rmtree(self.bilder, ignore_errors=True)


class TestLiveOhneBlender(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="skptool_mcp_live_"))
        self.state = self.tmp / "live.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_live_blender_is_a_tool_error(self):
        c = Client(env={"SKPTOOL_LIVE_STATE": str(self.state)})
        try:
            c.init()
            r = c.call("live_status")
            self.assertTrue(r["isError"])
            self.assertIn("Kein Live-Blender aktiv", text_of(r))
        finally:
            c.schliessen()

    def test_live_tools_against_protocol_server(self):
        token = "test-token-" + "z" * 32
        fake = FakeLive(token)
        self.state.write_text(json.dumps({"port": fake.port, "token": token, "pid": 1}), encoding="utf-8")
        os.chmod(self.state, 0o600)
        c = Client(env={"SKPTOOL_LIVE_STATE": str(self.state)})
        try:
            c.init()
            r = c.call("live_status")
            self.assertFalse(r["isError"], text_of(r))
            self.assertEqual(r["structuredContent"]["objects"], 2)
            r = c.call("live_screenshot", {"width": 640, "height": 400})
            self.assertFalse(r["isError"], text_of(r))
            bild = [x for x in r["content"] if x["type"] == "image"]
            self.assertEqual(len(bild), 1)
            self.assertEqual(bild[0]["mimeType"], "image/png")
            self.assertEqual(base64.b64decode(bild[0]["data"]), _png())
            self.assertNotIn("path", r["structuredContent"])
            self.assertEqual([p for p in fake.bilder.iterdir()], [])  # Server-Datei weggeraeumt
            r = c.call("live_ops", {"ops": [{"op": "move", "select": {"name": "Wuerfel"}, "by": [0, 0, 1]}]})
            self.assertFalse(r["isError"], text_of(r))
            self.assertEqual(fake.anfragen[-1]["ops"][0]["by"], [0, 0, 1])
            self.assertEqual(c.call("live_undo", {"steps": 2})["structuredContent"]["undone"], 2)
            anzahl = len(fake.anfragen)
            r = c.call("live_ops", {"ops": [{"op": "shell", "cmd": "calc"}]})
            self.assertTrue(r["isError"])
            self.assertEqual(len(fake.anfragen), anzahl)  # abgelehnt, bevor etwas gesendet wurde
            for req in fake.anfragen:
                self.assertNotIn(token, json.dumps(req))
        finally:
            c.schliessen()
            fake.close()


@unittest.skipUnless(BLENDER, "Blender nicht installiert")
class TestMitBlender(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="skptool_mcp_blender_"))
        cls.c = Client()
        cls.c.init()

    @classmethod
    def tearDownClass(cls):
        cls.c.schliessen()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_skp_edit_writes_new_file_and_refuses_overwrite(self):
        ziel = self.tmp / "bearbeitet.skp"
        vorher = S2017.read_bytes()
        ops = [{"op": "move", "select": {"name": "Leg_Chair*"}, "by": [0, 0, 1]},
               {"op": "add_box", "size": [1, 1, 1], "at": [5, 0, 0], "name": "NeueKiste"}]
        r = self.c.call("skp_edit", {"input": str(S2017), "output": str(ziel), "ops": ops})
        self.assertFalse(r["isError"], text_of(r))
        self.assertTrue(ziel.is_file())
        self.assertEqual([o["op"] for o in r["structuredContent"]["operationen"]], ["move", "add_box"])
        self.assertEqual(S2017.read_bytes(), vorher)
        r = self.c.call("skp_diff", {"a": str(S2017), "b": str(ziel)})
        self.assertFalse(r["isError"], text_of(r))
        self.assertIs(r["structuredContent"]["gleich"], False)
        erster = ziel.read_bytes()
        r = self.c.call("skp_edit", {"input": str(S2017), "output": str(ziel), "ops": ops})
        self.assertTrue(r["isError"])
        self.assertIn("gibt es schon", text_of(r))
        self.assertEqual(ziel.read_bytes(), erster)
        # eine scheiternde Operation schreibt nichts
        ziel2 = self.tmp / "nie.skp"
        r = self.c.call("skp_edit", {"input": str(S2017), "output": str(ziel2),
                                     "ops": [{"op": "move", "select": {"name": "*"}, "by": [1e7, 0, 0]}]})
        self.assertTrue(r["isError"])
        self.assertFalse(ziel2.exists())
        # mit ueberschreiben: true wird ersetzt
        r = self.c.call("skp_edit", {"input": str(S2017), "output": str(ziel), "ueberschreiben": True,
                                     "ops": [{"op": "summary"}]})
        self.assertFalse(r["isError"], text_of(r))
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["bearbeitet.skp"])

    def test_skp_list_and_convert_to_blend(self):
        r = self.c.call("skp_list", {"path": str(S2017), "name": "Leg_*", "limit": 3})
        self.assertFalse(r["isError"], text_of(r))
        objekte = r["structuredContent"]["list"]["objects"]
        self.assertEqual(len(objekte), 3)
        self.assertTrue(all(o["name"].startswith("Leg_") for o in objekte))
        self.assertGreaterEqual(r["structuredContent"]["list"]["count"], 8)  # 4 Stuhl- und 4 Tischbeine
        blend = self.tmp / "stuhl.blend"
        r = self.c.call("skp_convert", {"input": str(S2017), "output": str(blend)})
        self.assertFalse(r["isError"], text_of(r))
        self.assertTrue(blend.is_file())
        r = self.c.call("skp_list", {"path": str(blend)})
        self.assertFalse(r["isError"], text_of(r))
        blend.unlink()


@unittest.skipUnless(GUI_OK, "Blender nicht installiert oder GUI-Tests abgeschaltet")
class TestLiveBlender(unittest.TestCase):
    """Echtes Blender-Fenster mit Live-Server, gesteuert ueber den MCP-Server."""

    def test_live_tools_with_real_blender(self):
        from skptool import live

        tmp = Path(tempfile.mkdtemp(prefix="skptool_mcp_gui_"))
        state = tmp / "live.json"
        c = Client()  # Blender-Datei ueber das MCP-Werkzeug erzeugen
        try:
            c.init()
            blend = tmp / "stuhl.blend"
            self.assertFalse(c.call("skp_convert", {"input": str(S2017), "output": str(blend)})["isError"])
        finally:
            c.schliessen()
        proc, _ = live.launch(blend, state=state, timeout=300)
        c = Client(env={"SKPTOOL_LIVE_STATE": str(state)})
        try:
            c.init()
            r = c.call("live_status")
            self.assertFalse(r["isError"], text_of(r))
            n = r["structuredContent"]["objects"]
            r = c.call("live_ops", {"ops": [{"op": "add_box", "size": [1, 1, 1], "at": [3, 0, 0]}]})
            self.assertFalse(r["isError"], text_of(r))
            self.assertEqual(c.call("live_status")["structuredContent"]["objects"], n + 1)
            r = c.call("live_screenshot", {"width": 320, "height": 200})
            self.assertFalse(r["isError"], text_of(r))
            png = base64.b64decode(next(x for x in r["content"] if x["type"] == "image")["data"])
            self.assertTrue(png.startswith(b"\x89PNG"))
            self.assertEqual(struct.unpack(">II", png[16:24]), (320, 200))
            self.assertFalse(c.call("live_undo", {"steps": 1})["isError"])
            self.assertEqual(c.call("live_status")["structuredContent"]["objects"], n)
            r = c.call("live_ops", {"ops": [{"op": "move", "select": {"name": "gibtsnicht*"}, "by": [0, 0, 1]}]})
            self.assertTrue(r["isError"])
        finally:
            c.schliessen()
            try:
                live.quit_blender(state, force=True)
            except live.LiveError:
                pass
            try:
                proc.wait(60)
            except subprocess.TimeoutExpired:
                proc.kill()
            shutil.rmtree(tmp, ignore_errors=True)


class TestTestclient(unittest.TestCase):
    def test_client_script_mimicking_claude_code(self):
        out = subprocess.run([sys.executable, str(ROOT / "tools" / "mcp_testclient.py")], cwd=ROOT,
                             capture_output=True, timeout=300)
        text = out.stdout.decode("utf-8", "replace")
        self.assertEqual(out.returncode, 0, text + out.stderr.decode("utf-8", "replace"))
        self.assertIn("Protokoll 2025-11-25", text)
        self.assertIn("10 Werkzeuge", text)
        self.assertIn("OK", text)


if __name__ == "__main__":
    unittest.main()
