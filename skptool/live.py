"""Live-Modus, Client-Seite: Befehle an ein offenes Blender-Fenster schicken.

Das offene Blender laeuft mit blender_scripts/live_server.py. Dieser Server schreibt Port und
Token in eine Statusdatei (Standard %LOCALAPPDATA%/skptool/live.json, anderer Ort ueber
SKPTOOL_LIVE_STATE). Dieses Modul liest die Datei, verbindet sich mit 127.0.0.1 und schickt
eine JSON-Zeile pro Anfrage. Das Token geht nie ueber die Leitung: Client und Server weisen sich
gegenseitig per HMAC ueber zwei Zufallswerte aus (Protokoll in live_server.py).

    from skptool import live
    live.launch("haus.blend", export_skp="haus_bearbeitet.skp")   # Blender mit Live-Server starten
    live.run_ops([{"op": "move", "select": {"name": "Palme*"}, "by": [0, 0, 1]}])
    live.screenshot("blick.png")
    live.save(wait_export=True)

Die Anbindung an die Kommandozeile (skptool live ..., skptool open --live) steht unten:
add_live_parser(), cmd_live() und open_live(), eingebunden in cli.py.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from skptool.blender import find_blender

SERVER_SCRIPT = Path(__file__).parent / "blender_scripts" / "live_server.py"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_ENV = "SKPTOOL_LIVE_STATE"
NOT_RUNNING = "Kein Live-Blender aktiv, starte mit skptool open <datei> --live"
MAX_RESPONSE = 64 * 1024 * 1024
PROTOCOL = 2


def mac(token: str, *parts: str) -> str:
    """HMAC-SHA256 als Hex, gleiche Funktion wie live_server.mac."""
    return hmac.new(token.encode("ascii"), "|".join(parts).encode("ascii"), hashlib.sha256).hexdigest()


def _check_private(path: Path, what: str) -> None:
    """POSIX: Datei bzw. Ordner muss dem eigenen Benutzer gehoeren, kein Symlink, fuer andere
    weder les- noch schreibbar. Sonst koennte ein anderer Benutzer Port und Token unterschieben."""
    if os.name == "nt":  # %LOCALAPPDATA% ist nur fuer den eigenen Benutzer zugaenglich
        return
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise LiveError(f"{what} {path} ist nicht privat (Eigentuemer oder Rechte falsch), "
                        "aus Sicherheitsgruenden abgelehnt")


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        st = os.lstat(path)
        if not stat.S_ISLNK(st.st_mode) and st.st_uid == os.getuid() and st.st_mode & 0o077:
            os.chmod(path, 0o700)
    _check_private(path, "Ordner")


def _open_private(path: Path):
    """Zum Schreiben oeffnen, ohne einem Symlink zu folgen, nur fuer den eigenen Benutzer."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    return os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8")


class LiveError(RuntimeError):
    pass


# ---------------------------------------------------------------- Statusdatei

def state_path(explicit: str | os.PathLike | None = None) -> Path:
    """Reihenfolge: Argument, SKPTOOL_LIVE_STATE, %LOCALAPPDATA%/skptool/live.json (bzw. ~/.cache)."""
    if explicit:
        return Path(explicit)
    if os.environ.get(STATE_ENV):
        return Path(os.environ[STATE_ENV])
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "skptool" / "live.json"


def read_state(path: str | os.PathLike | None = None) -> dict:
    p = state_path(path)
    if not p.exists() and not p.is_symlink():
        raise LiveError(NOT_RUNNING)
    _check_private(p.parent, "Ordner der Statusdatei")
    _check_private(p, "Statusdatei")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise LiveError(NOT_RUNNING) from None
    except (OSError, ValueError, RecursionError):
        raise LiveError(f"Statusdatei {p} ist unlesbar. {NOT_RUNNING}") from None
    token = data.get("token") if isinstance(data, dict) else None
    port = data.get("port") if isinstance(data, dict) else None
    if (not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536
            or not isinstance(token, str) or not token.isascii() or len(token) < 16):
        raise LiveError(f"Statusdatei {p} ist unvollstaendig. {NOT_RUNNING}")
    return data


def _drop_stale(path: Path, data: dict) -> None:
    """Statusdatei eines abgestuerzten Blenders entfernen (nur wenn sie unveraendert ist)."""
    try:
        if not path.is_symlink() and json.loads(path.read_text(encoding="utf-8")) == data:
            path.unlink()
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------- Anfragen

def _read_line(conn, limit: int) -> bytes:
    buf = bytearray()
    while b"\n" not in buf:
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf += chunk
        if len(buf) > limit:
            raise LiveError("Antwort des Live-Blenders ist zu gross")
    return bytes(buf).split(b"\n", 1)[0]


def request(cmd: str, state: str | os.PathLike | None = None, timeout: float = 120.0, **payload) -> dict:
    """Eine Anfrage schicken und die Antwort (dict) liefern. Fehler des Servers als LiveError.

    Die Antwort wird nur angenommen, wenn der Server beweist, dass er das Token kennt."""
    path = state_path(state)
    data = read_state(path)
    token = data["token"]
    try:
        conn = socket.create_connection(("127.0.0.1", data["port"]), timeout=min(timeout, 5.0))
    except ConnectionRefusedError:
        _drop_stale(path, data)
        raise LiveError(f"{NOT_RUNNING} (alte Statusdatei entfernt, Blender wurde wohl geschlossen)") from None
    except OSError as exc:
        raise LiveError(f"Keine Verbindung zum Live-Blender auf Port {data['port']}: {exc}. {NOT_RUNNING}") from None
    try:
        with conn:
            conn.settimeout(10.0)
            try:
                hello = json.loads(_read_line(conn, 4096).decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError):
                raise LiveError("Auf dem Port antwortet kein skptool-Live-Blender") from None
            server_nonce = hello.get("hello") if isinstance(hello, dict) else None
            if (not isinstance(server_nonce, str) or not server_nonce.isascii() or not server_nonce.isalnum()
                    or not 16 <= len(server_nonce) <= 128):
                raise LiveError("Auf dem Port antwortet kein skptool-Live-Blender (altes Protokoll?)")
            cnonce = secrets.token_hex(16)
            msg = {**payload, "cmd": cmd, "timeout": timeout, "cnonce": cnonce,
                   "auth": mac(token, "client", server_nonce, cnonce)}
            conn.settimeout(timeout + 10.0)  # der Server meldet sich spaetestens nach timeout selbst
            conn.sendall(json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n")
            line = _read_line(conn, MAX_RESPONSE)
    except socket.timeout:
        raise LiveError(f"Blender hat nach {timeout:.0f} s nicht geantwortet (beschaeftigt oder ein Dialog "
                        "ist offen). Laenger warten mit --timeout.") from None
    except OSError as exc:
        raise LiveError(f"Verbindung zum Live-Blender abgebrochen: {exc}") from None
    if not line:
        raise LiveError("Live-Blender hat die Verbindung ohne Antwort beendet")
    try:
        resp = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise LiveError("Antwort des Live-Blenders ist kein JSON") from None
    if not isinstance(resp, dict):
        raise LiveError("Antwort des Live-Blenders ist ungueltig")
    proof = resp.pop("proof", None)
    if not isinstance(proof, str) or not hmac.compare_digest(
            proof.encode("utf-8", "replace"), mac(token, "server", server_nonce, cnonce).encode("ascii")):
        raise LiveError(str(resp.get("error") or "Antwort stammt nicht vom echten Live-Blender "
                                                  "(Pruefung fehlgeschlagen), nichts uebernommen"))
    if not resp.get("ok") and not (cmd == "ops" and "results" in resp):
        raise LiveError(resp.get("error") or "Unbekannter Fehler im Live-Blender")
    return resp


def ping(state=None, timeout: float = 10.0) -> dict:
    return request("ping", state, timeout)


def is_running(state=None) -> bool:
    try:
        ping(state, timeout=5.0)
        return True
    except LiveError:
        return False


def status(state=None, timeout: float = 30.0) -> dict:
    return request("status", state, timeout)


def run_ops(operations, state=None, timeout: float = 300.0, stop_on_error: bool = True) -> dict:
    """ops.run im offenen Blender; jeder Aufruf ist ein Rueckgaengig-Schritt. Rueckgabe mit "results"."""
    if isinstance(operations, dict):
        operations = [operations]
    return request("ops", state, timeout, ops=operations, stop_on_error=stop_on_error)


def undo(steps: int = 1, state=None, timeout: float = 60.0) -> dict:
    return request("undo", state, timeout, steps=steps)


def save(state=None, timeout: float = 300.0, wait_export: bool = False, export_timeout: float = 3600.0) -> dict:
    """.blend speichern; mit Exportziel startet danach automatisch der .skp-Export."""
    resp = request("save", state, timeout)
    if wait_export and (resp.get("export") or {}).get("target"):
        resp["export"] = wait_for_export(state, export_timeout)
    return resp


def export_skp(state=None, wait: bool = False, timeout: float = 3600.0) -> dict:
    resp = request("export_skp", state, 30.0)
    if wait and (resp.get("started") or (resp.get("export") or {}).get("state") == "laeuft"):
        resp["export"] = wait_for_export(state, timeout)
    return resp


def wait_for_export(state=None, timeout: float = 3600.0, poll: float = 0.5) -> dict:
    """Warten, bis kein Export mehr laeuft oder geplant ist; liefert den Exportstatus."""
    end = time.monotonic() + timeout
    while True:
        ex = status(state)["export"]
        if ex["state"] not in ("laeuft", "geplant") and not ex.get("pending"):
            return ex
        if time.monotonic() > end:
            raise LiveError(f"Export nach {timeout:.0f} s noch nicht fertig")
        time.sleep(poll)


def screenshot(out: str | os.PathLike, view: str = "model", width: int = 1600, height: int = 1000,
               state=None, timeout: float = 300.0) -> dict:
    """PNG des Modells (view=model: eigene Kamera, Workbench), der 3D-Ansicht (viewport) oder des
    ganzen Fensters (window). Der Server schreibt in seinen Temp-Ordner, hier wird verschoben."""
    resp = request("screenshot", state, timeout, view=view, width=width, height=height)
    src = _server_image(resp.get("path"))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, out)  # kopieren statt verschieben, dann die eigene Temp-Datei loeschen
    try:
        src.unlink()
    except OSError:
        pass
    resp["path"] = str(out)
    return resp


def _server_image(path) -> Path:
    """Nur ein PNG aus dem Temp-Ordner eines Live-Servers annehmen (Schutz, falls der Server
    doch nicht der echte waere): regulaere Datei, kein Symlink, direkt in <temp>/skptool_live_*."""
    if not isinstance(path, str):
        raise LiveError("Live-Blender hat keinen Bildpfad geliefert")
    p = Path(path)
    tmp = Path(os.path.realpath(tempfile.gettempdir()))
    real = Path(os.path.realpath(p))
    ok = (p.suffix.lower() == ".png" and not p.is_symlink() and real.is_file()
          and real.parent.parent == tmp and real.parent.name.startswith("skptool_live_"))
    if not ok:
        raise LiveError(f"Unerwarteter Bildpfad vom Live-Blender abgelehnt: {path}")
    return real


def quit_blender(state=None, force: bool = False, timeout: float = 30.0) -> dict:
    return request("quit", state, timeout, force=force)


# ---------------------------------------------------------------- Blender starten

def ensure_free(state=None) -> Path:
    """LiveError, wenn schon ein Live-Blender antwortet; veraltete Statusdatei wegraeumen."""
    path = state_path(state)
    if not path.exists() and not path.is_symlink():
        return path
    old = read_state(path)  # fremde oder unsichere Statusdatei: Fehler statt still loeschen
    try:
        socket.create_connection(("127.0.0.1", old["port"]), timeout=5.0).close()
    except ConnectionRefusedError:
        _drop_stale(path, old)  # niemand hoert zu: Blender wurde beendet
        return path
    except OSError:
        pass
    try:
        info = ping(path, timeout=5.0)
    except LiveError as exc:
        raise LiveError(f"Auf Port {old['port']} laeuft noch etwas, aber es antwortet nicht wie erwartet "
                        f"({exc}). Ist Blender beschaeftigt? Sonst {path} loeschen.") from None
    raise LiveError(f"Es laeuft schon ein Live-Blender (PID {info.get('pid')}, Datei {old.get('blend')}). "
                    "Erst dort speichern und schliessen oder skptool live --quit.")


def launch(blend: str | os.PathLike, export_skp: str | os.PathLike | None = None, blender: str | None = None,
           state=None, wait: bool = True, timeout: float = 300.0, extra_args=(), log: str | None = None,
           allow_external: bool = False):
    """Blender mit Fenster und Live-Server starten, ohne zu blockieren.

    Mit wait=True wird gewartet, bis der Server antwortet (grosse Dateien laden etwas).
    Rueckgabe: (Popen, Statusdaten oder None)."""
    blend = Path(blend).resolve()
    if not blend.exists():
        raise LiveError(f"Datei nicht gefunden: {blend}")
    path = ensure_free(state)
    _private_dir(path.parent)
    exe = find_blender(blender)
    cmd = [exe, *extra_args, "-Y", str(blend), "--python", str(SERVER_SCRIPT), "--",
           "--state", str(path), "--python-exe", sys.executable, "--project-root", str(PROJECT_ROOT)]
    if export_skp:
        cmd += ["--export-skp", str(Path(export_skp).resolve())]
    if allow_external:
        cmd.append("--allow-external")
    log_path = Path(log) if log else path.with_suffix(".log")
    flags = 0
    if os.name == "nt":  # eigene, unsichtbare Konsole: Blender lebt weiter, wenn das Terminal zugeht
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    with _open_private(log_path) as fh:
        proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                creationflags=flags, close_fds=True)
    if not wait:
        return proc, None
    return proc, wait_ready(path, proc, timeout, log_path)


def wait_ready(state=None, proc=None, timeout: float = 300.0, log_path=None) -> dict:
    path = state_path(state)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if proc is not None and proc.poll() is not None:
            tail = ""
            if log_path and Path(log_path).exists():
                tail = "\n".join(Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()[-15:])
            raise LiveError(f"Blender wurde beendet, bevor der Live-Modus bereit war (Code {proc.returncode}).\n{tail}")
        if path.exists():
            try:
                data = read_state(path)
                if proc is None or data.get("pid") == proc.pid:
                    ping(path, timeout=5.0)
                    return data
            except LiveError:
                pass
        time.sleep(0.2)
    raise LiveError(f"Live-Blender war nach {timeout:.0f} s nicht bereit")


# ---------------------------------------------------------------- Kommandozeile

def default_export_target(src: Path) -> Path | None:
    """skptool open haus.skp --live schreibt beim Speichern haus_bearbeitet.skp (nie das Original)."""
    if src.suffix.lower() == ".skp":
        return src.with_name(src.stem + "_bearbeitet.skp")
    return None


def open_live(src: Path, blend: Path, a) -> int:
    """Teil von skptool open ... --live, nachdem die .blend-Datei existiert."""
    explicit = bool(getattr(a, "export_skp", None))
    target = Path(a.export_skp) if explicit else default_export_target(src)
    if target is not None and target.resolve() == src.resolve():
        raise SystemExit(f"--export-skp darf nicht die Originaldatei sein ({src}). Bitte anderen Namen waehlen.")
    if target is not None and not explicit and target.exists():
        raise SystemExit(f"{target} gibt es schon und wuerde beim Speichern ueberschrieben. Zum Ueberschreiben "
                         f'ausdruecklich angeben: --export-skp "{target}", sonst einen anderen Namen.')
    try:
        # Aus einer .skp erzeugt: die .blend enthaelt nur eingebettete Daten, externe Bilder kommen
        # also vom Nutzer selbst. Bei fremden .blend gilt der sichere Standard (nur eingebettet).
        proc, data = launch(blend, export_skp=target, blender=a.blender,
                            timeout=float(getattr(a, "timeout", None) or 300),
                            allow_external=src.suffix.lower() == ".skp")
    except LiveError as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 1
    print(f"Live-Blender bereit mit {blend} (PID {proc.pid}, Port {data['port']}).")
    if target:
        print(f"Jedes Speichern in Blender (Strg+S) schreibt automatisch {target}")
    print("Befehle: skptool live --ops '<json>' | --screenshot bild.png | --save | --status | --undo | --quit")
    return 0


def _load_ops(raw: str):
    from skptool.opsjson import load_ops
    return load_ops(raw)


def _print_results(results) -> None:
    for r in results or []:
        if not r.get("ok"):
            print(f"  FEHLER {r.get('op')}: {r.get('error')}")
        elif r["op"] in ("list", "summary"):
            detail = {k: v for k, v in r.items() if k not in ("op", "ok")}
            print(f"  {r['op']}:\n" + json.dumps(detail, indent=2, ensure_ascii=False))
        else:
            detail = {k: v for k, v in r.items() if k not in ("op", "ok", "objects", "names")}
            print(f"  {r['op']:12} " + ", ".join(f"{k}={v}" for k, v in detail.items()))


def add_live_parser(sub) -> None:
    """Unterbefehl "live" an den argparse-Subparser von skptool haengen."""
    p = sub.add_parser("live", help="Befehle an das offene Live-Blender schicken",
                       description="Steuert ein mit 'skptool open <datei> --live' gestartetes Blender. "
                                   "Mehrere Schalter gehen zusammen, Reihenfolge: ops, undo, save, export, "
                                   "screenshot, status, quit.")
    p.add_argument("--ops", help="Operationen als JSON-Text oder .json-Datei (wie bei edit)")
    p.add_argument("--keep-going", action="store_true", help="Bei fehlerhafter Operation weitermachen")
    p.add_argument("--undo", type=int, nargs="?", const=1, help="Letzte(n) Schritt(e) rueckgaengig machen")
    p.add_argument("--save", action="store_true", help=".blend speichern (loest den .skp-Export aus)")
    p.add_argument("--wait", action="store_true", help="Nach --save/--export warten, bis die .skp geschrieben ist")
    p.add_argument("--export", action="store_true", help=".skp-Export jetzt starten (Stand der gespeicherten .blend)")
    p.add_argument("--screenshot", metavar="PNG", help="Bild des Modells speichern")
    p.add_argument("--view", choices=["model", "viewport", "window"], default="model",
                   help="model: ganzes Modell (Standard), viewport: 3D-Ansicht wie gerade zu sehen, "
                        "window: ganzes Fenster")
    p.add_argument("--width", type=int, default=1600)
    p.add_argument("--height", type=int, default=1000)
    p.add_argument("--status", action="store_true", help="Zustand anzeigen (Datei, Aenderungen, Export)")
    p.add_argument("--quit", action="store_true", help="Blender beenden (nur ohne ungespeicherte Aenderungen)")
    p.add_argument("--force", action="store_true", help="Mit --quit: auch mit ungespeicherten Aenderungen")
    p.add_argument("--timeout", type=float, default=300.0, help="Sekunden, die auf Blender gewartet wird")
    p.add_argument("--json", action="store_true", help="Antworten als JSON ausgeben")
    p.set_defaults(func=cmd_live)


def cmd_live(a) -> int:
    ops = _load_ops(a.ops) if a.ops else None
    if not any([ops, a.undo, a.save, a.export, a.screenshot, a.status, a.quit]):
        a.status = True
    out: dict = {}
    rc = 0
    try:
        if ops:
            resp = run_ops(ops, timeout=a.timeout, stop_on_error=not a.keep_going)
            out["ops"] = resp
            if not a.json:
                print(f"Bearbeitung ({resp.get('ms')} ms" + (f", Rueckgaengig: {resp['undo_step']}"
                                                              if resp.get("undo_step") else "") + "):")
                _print_results(resp["results"])
                if resp.get("note"):
                    print(f"Hinweis: {resp['note']}")
            if not resp.get("ok"):
                rc = 1
        if a.undo:
            resp = undo(a.undo, timeout=a.timeout)
            out["undo"] = resp
            if not a.json:
                print(f"Rueckgaengig: {resp['undone']} Schritt(e)")
        if a.save:
            resp = save(timeout=a.timeout, wait_export=a.wait)
            out["save"] = resp
            if not a.json:
                print(f"Gespeichert: {resp['saved']}")
                _print_export(resp.get("export"))
        if a.export:
            resp = export_skp(wait=a.wait)
            out["export"] = resp
            if not a.json:
                if not a.wait:
                    print(resp["message"])
                _print_export(resp.get("export"))
        if a.screenshot:
            resp = screenshot(a.screenshot, view=a.view, width=a.width, height=a.height, timeout=a.timeout)
            out["screenshot"] = resp
            if not a.json:
                print(f"Bild: {resp['path']} ({resp['ms']} ms)")
        if a.status:
            resp = status(timeout=a.timeout)
            out["status"] = resp
            if not a.json:
                print(f"Live-Blender {resp['blender']}, PID {resp['pid']}, Port {resp['port']}")
                print(f"Datei: {resp['blend'] or '(ungespeichert)'}"
                      + ("  [ungespeicherte Aenderungen]" if resp["dirty"] else ""))
                print(f"Objekte: {resp['objects']}, Modus: {resp['mode']}")
                _print_export(resp["export"])
        if a.quit:
            resp = quit_blender(force=a.force)
            out["quit"] = resp
            if not a.json:
                print("Blender wird beendet" + (" (laufender Export wird noch fertig)"
                                                if resp.get("export_running") else ""))
    except LiveError as exc:
        if a.json:
            out["error"] = str(exc)
            print(json.dumps(out, indent=2, ensure_ascii=False))
        else:
            print(f"FEHLER: {exc}", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
    return rc


def _print_export(ex) -> None:
    if not ex or not ex.get("target"):
        print("Export: kein Ziel (starten mit --export-skp ziel.skp)")
        return
    state = ex["state"] + (", weiterer geplant" if ex.get("pending") and ex["state"] == "laeuft" else "")
    print(f"Export nach {ex['target']}: {state}" + (f" - {ex['message']}" if ex.get("message") else ""))
