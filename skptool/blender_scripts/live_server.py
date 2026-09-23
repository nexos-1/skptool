"""Live-Modus: laeuft INNERHALB eines offenen Blender-Fensters.

Start (macht skptool open --live bzw. skptool.live.launch):
  blender -Y modell.blend --python live_server.py -- --state live.json
          [--export-skp ziel.skp] [--python-exe python.exe] [--project-root ordner]

Ablauf:
  - TCP-Server nur auf 127.0.0.1, zufaelliger freier Port, zufaelliges Token.
    Port, Token, PID und Datei stehen in der Statusdatei (--state), die beim Beenden geloescht wird.
  - Netzwerk in Hintergrund-Threads, jede Blender-Aenderung aber im Hauptthread ueber
    bpy.app.timers, damit die Oberflaeche fluessig bleibt.
  - Protokoll (Version 2), pro Verbindung genau ein Auftrag, jede Nachricht eine JSON-Zeile:
      Server:  {"hello": sn, "protocol": 2}                    sn = Zufallswert des Servers
      Client:  {"cmd": "ops", "cnonce": cn, "auth": HMAC(token, "client|sn|cn"), "ops": [...]}
      Server:  {"ok": ..., "proof": HMAC(token, "server|sn|cn"), ...}
    Das Token selbst geht nie ueber die Leitung. Ein falscher Server (z. B. ein anderer Benutzer
    auf einem frei gewordenen Port) erfaehrt nichts und kann keine gueltige Antwort faelschen.
    Befehle: ping, status, ops, undo, save, export_skp, screenshot, quit.
  - Jeder ops-Stapel wird ein eigener Rueckgaengig-Schritt ("skptool: move, scale").
  - Mit --export-skp wird nach jedem Speichern (Strg+S) im Hintergrund die .skp geschrieben.

Sicherheit: nur 127.0.0.1, gegenseitige Pruefung per HMAC in konstanter Zeit, Anfragen hoechstens
1 MB und 10 s insgesamt, keine Ausfuehrung von beliebigem Python, nur die festen Operationen aus
ops.py. Statusdatei und Ordner nur fuer den eigenen Benutzer (0600/0700, keine Symlinks).
Bilder schreibt der Server nur in seinen eigenen Temp-Ordner, die Exportdatei ist beim Start fest.
"""
import argparse
import atexit
import hashlib
import hmac
import json
import math
import os
import queue
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback

import bpy  # noqa: E402
from bpy.app.handlers import persistent  # noqa: E402
from mathutils import Vector  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)  # ops.py liegt daneben
try:
    import ops as skp_ops  # noqa: E402
finally:  # nicht dauerhaft im Suchpfad lassen, sonst bekaemen andere Add-ons unser "ops"
    if sys.path and sys.path[0] == _HERE:
        del sys.path[0]

PROTOCOL = 2
MAX_REQUEST = 1024 * 1024          # 1 MB pro Anfrage
MAX_HANDLERS = 8                   # gleichzeitige Verbindungen
READ_TIMEOUT = 10.0                # Sekunden fuer das Lesen einer ganzen Anfrage (nicht pro Paket)
DEFAULT_WAIT = 120.0               # so lange wartet eine Verbindung auf den Hauptthread
IDLE_INTERVAL = 0.1                # Timer-Takt ohne Auftraege
BUSY_INTERVAL = 0.02               # Timer-Takt kurz nach einem Auftrag
EXPORT_DEBOUNCE = 0.8              # Sekunden nach dem Speichern, bevor der Export startet
LABEL = "skptool live aktiv"


def _norm(path):
    return os.path.normcase(os.path.abspath(path)) if path else ""


def mac(token, *parts):
    """HMAC-SHA256 als Hex, gleiche Funktion wie skptool.live.mac."""
    msg = "|".join(parts).encode("ascii")
    return hmac.new(token.encode("ascii"), msg, hashlib.sha256).hexdigest()


def private_dir(path):
    """Ordner nur fuer den eigenen Benutzer anlegen bzw. pruefen (POSIX: Eigentuemer, 0700)."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    if os.name != "nt":
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode) or st.st_uid != os.getuid():
            raise RuntimeError(f"Ordner {path} gehoert nicht diesem Benutzer, Live-Modus abgebrochen")
        if st.st_mode & 0o077:
            os.chmod(path, 0o700)


def write_private(path, text):
    """Datei atomar und nur fuer den eigenen Benutzer schreiben (zufaelliger Temp-Name, 0600)."""
    folder = os.path.dirname(path)
    private_dir(folder)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".live-", suffix=".tmp")  # O_EXCL, 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class Job:
    def __init__(self, req):
        self.req = req
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.state = "wartet"   # wartet, laeuft, fertig, verworfen
        self.response = None


class Live:
    """Der gesamte Zustand des Live-Servers (eine Instanz pro Blender)."""

    def __init__(self, args):
        self.args = args
        self.state_path = os.path.abspath(args.state)
        self.export_target = os.path.abspath(args.export_skp) if args.export_skp else None
        self.python_exe = args.python_exe
        self.project_root = args.project_root
        self.token = secrets.token_urlsafe(32)
        self.jobs = queue.Queue(maxsize=32)
        self.slots = threading.BoundedSemaphore(MAX_HANDLERS)
        self.tmp = tempfile.mkdtemp(prefix="skptool_live_")
        self.shots = 0
        self.last_activity = 0.0
        self.ready = False
        self.stopping = False
        self.blend_at_start = ""
        # Export-Zustand (nur im Hauptthread veraendert)
        self.export = {"target": self.export_target, "state": "aus" if not self.export_target else "bereit",
                       "message": "", "started": None, "finished": None, "seconds": None, "runs": 0}
        self.export_proc = None
        self.export_log = None
        self.export_due = None
        self.export_pending = False
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: niemand darf den Port mitbenutzen
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]

    # ------------------------------------------------------------ Netzwerk (Hintergrund)

    def serve(self):
        while not self.stopping:
            try:
                conn, _addr = self.sock.accept()
            except OSError:
                break
            if not self.slots.acquire(blocking=False):
                self._send(conn, {"ok": False, "error": "Zu viele gleichzeitige Anfragen, bitte erneut versuchen"})
                conn.close()
                continue
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()

    @staticmethod
    def _send(conn, obj):
        try:
            conn.sendall(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
        except OSError:
            pass

    def _read_request(self, conn):
        deadline = time.monotonic() + READ_TIMEOUT  # Gesamtfrist: tropfenweises Senden hilft nicht
        buf = bytearray()
        while b"\n" not in buf:
            left = deadline - time.monotonic()
            if left <= 0:
                raise ValueError("Zeitueberschreitung beim Lesen der Anfrage")
            conn.settimeout(left)
            try:
                chunk = conn.recv(65536)
            except (socket.timeout, TimeoutError):
                raise ValueError("Zeitueberschreitung beim Lesen der Anfrage") from None
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_REQUEST:
                raise ValueError(f"Anfrage zu gross (hoechstens {MAX_REQUEST // 1024} KB)")
        line = bytes(buf).split(b"\n", 1)[0]
        if len(line) > MAX_REQUEST:
            raise ValueError(f"Anfrage zu gross (hoechstens {MAX_REQUEST // 1024} KB)")
        try:
            req = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise ValueError("Anfrage ist kein gueltiges JSON") from None
        if not isinstance(req, dict):
            raise ValueError("Anfrage muss ein JSON-Objekt sein")
        return req

    def _handle_conn(self, conn):
        try:
            self._serve_one(conn)
        except Exception:  # nie mit Traceback sterben, egal was geschickt wird
            self._send(conn, {"ok": False, "error": "Ungueltige Anfrage"})
        finally:
            try:
                conn.close()
            finally:
                self.slots.release()

    def _serve_one(self, conn):
        server_nonce = secrets.token_hex(16)
        self._send(conn, {"hello": server_nonce, "protocol": PROTOCOL})
        try:
            req = self._read_request(conn)
        except ValueError as exc:
            self._send(conn, {"ok": False, "error": str(exc)})
            return
        cnonce, auth = req.pop("cnonce", None), req.pop("auth", None)
        req.pop("token", None)  # Protokoll 1: Token im Klartext wird nicht mehr angenommen
        ok_shape = (isinstance(cnonce, str) and isinstance(auth, str) and 16 <= len(cnonce) <= 128
                    and cnonce.isascii() and cnonce.isalnum() and auth.isascii() and len(auth) == 64)
        if not ok_shape or not hmac.compare_digest(
                auth.encode("ascii"), mac(self.token, "client", server_nonce, cnonce).encode("ascii")):
            self._send(conn, {"ok": False, "error": "Zugriff verweigert: Anmeldung ungueltig "
                                                    "(anderes skptool oder falsche Statusdatei)"})
            return
        proof = mac(self.token, "server", server_nonce, cnonce)

        def reply(obj):
            self._send(conn, {**obj, "proof": proof})

        cmd = req.get("cmd")
        if not isinstance(cmd, str) or cmd not in COMMANDS:
            reply({"ok": False, "error": f"Unbekannter Befehl {str(cmd)[:40]!r}. Moeglich: "
                                         f"{', '.join(sorted(COMMANDS))}"})
            return
        try:
            wait = float(req.get("timeout", DEFAULT_WAIT))
        except (TypeError, ValueError, OverflowError):
            wait = DEFAULT_WAIT
        wait = min(max(wait, 1.0), 3600.0) if wait == wait else DEFAULT_WAIT  # NaN -> Standard
        job = Job(req)
        try:
            self.jobs.put_nowait(job)
        except queue.Full:
            reply({"ok": False, "error": "Warteschlange voll, Blender ist beschaeftigt"})
            return
        if not job.done.wait(wait):
            with job.lock:
                if job.state == "wartet":
                    job.state = "verworfen"
            if job.state == "verworfen":
                reply({"ok": False, "error": "Blender hat nicht rechtzeitig reagiert (beschaeftigt "
                                             "oder ein Dialog ist offen). Auftrag verworfen."})
                return
            job.done.wait()  # laeuft schon, Ergebnis abwarten
        reply(job.response)

    # ------------------------------------------------------------ Hauptthread (Timer)

    def tick(self):
        try:
            if not self.ready:
                self._become_ready()
            self._poll_export()
            try:
                job = self.jobs.get_nowait()
            except queue.Empty:
                job = None
            if job is not None:
                self._run_job(job)
                self.last_activity = time.monotonic()
                if not self.jobs.empty():
                    return 0.001
        except Exception:  # der Timer darf nie sterben
            traceback.print_exc()
        if self.stopping:
            return None
        busy = time.monotonic() - self.last_activity < 2.0 or self.export_proc is not None
        return BUSY_INTERVAL if busy else IDLE_INTERVAL

    def _become_ready(self):
        self.blend_at_start = bpy.data.filepath
        self.write_state()
        self.ready = True
        _redraw()
        _report(f"{LABEL} (Port {self.port})")
        print(f"skptool live: bereit auf 127.0.0.1:{self.port}, Statusdatei {self.state_path}", flush=True)

    def _run_job(self, job):
        with job.lock:
            if job.state == "verworfen":
                return
            job.state = "laeuft"
        req = job.req
        t0 = time.perf_counter()
        try:
            resp = COMMANDS[req["cmd"]](self, req)
            resp = {"ok": True, **resp} if "ok" not in resp else resp
        except UserError as exc:
            resp = {"ok": False, "error": str(exc)}
        except Exception as exc:
            traceback.print_exc()
            resp = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        resp["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        job.response = resp
        job.state = "fertig"
        job.done.set()
        _redraw()

    # ------------------------------------------------------------ Statusdatei

    def write_state(self):
        data = {"protocol": PROTOCOL, "port": self.port, "token": self.token, "pid": os.getpid(),
                "blend": bpy.data.filepath, "export_skp": self.export_target, "started": time.time()}
        write_private(self.state_path, json.dumps(data))

    def remove_state(self):
        """Nur die eigene Statusdatei loeschen, nie die einer neueren Sitzung."""
        try:
            if os.path.islink(self.state_path):
                return
            with open(self.state_path, encoding="utf-8") as fh:
                data = json.load(fh)
            if data.get("pid") == os.getpid() and data.get("token") == self.token:
                os.remove(self.state_path)
        except (OSError, ValueError):
            pass

    def shutdown(self):
        self.stopping = True
        self.remove_state()
        try:
            self.sock.close()
        except OSError:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ Export nach .skp

    def export_allowed(self):
        if not self.export_target:
            return "Kein Exportziel gesetzt (starten mit --export-skp ziel.skp)"
        if not bpy.data.filepath:
            return "Die Datei ist noch nicht gespeichert"
        if self.blend_at_start and _norm(bpy.data.filepath) != _norm(self.blend_at_start):
            return (f"Export pausiert: geoeffnet ist {os.path.basename(bpy.data.filepath)}, "
                    f"nicht {os.path.basename(self.blend_at_start)}")
        return None

    def schedule_export(self):
        why = self.export_allowed()
        if why:
            self.export.update(state="pausiert" if self.export_target else "aus", message=why)
            _redraw()
            return False
        self.export_due = time.monotonic() + EXPORT_DEBOUNCE
        if self.export_proc is None:
            self.export.update(state="geplant", message="Export startet gleich")
        _redraw()
        return True

    def _poll_export(self):
        proc = self.export_proc
        if proc is not None:
            rc = proc.poll()
            if rc is None:
                return
            secs = round(time.monotonic() - self.export["started"], 1)
            tail = self._export_log_tail()
            self.export_proc = None
            name = os.path.basename(self.export_target)
            if rc == 0 and os.path.exists(self.export_target):
                self.export.update(state="ok", finished=time.time(), seconds=secs,
                                   message=f"{name} geschrieben ({secs} s)")
                _report(f"skptool: {name} geschrieben ({secs} s)")
            else:
                self.export.update(state="fehler", finished=time.time(), seconds=secs,
                                   message=f"Export fehlgeschlagen (Code {rc}): {tail}")
                _report(f"skptool: Export nach {name} fehlgeschlagen, Details mit skptool live --status",
                        "ERROR")
            _redraw()
            if self.export_pending:
                self.export_pending = False
                self.export_due = time.monotonic() + EXPORT_DEBOUNCE
        if self.export_due is not None and time.monotonic() >= self.export_due:
            self.export_due = None
            self.start_export()

    def _export_log_tail(self):
        try:
            with open(self.export_log, encoding="utf-8", errors="replace") as fh:
                lines = [ln.strip() for ln in fh.read().splitlines() if ln.strip()]
            return " | ".join(lines[-6:])[-1500:]
        except OSError:
            return ""

    def start_export(self):
        """Startet den Export als eigenen Prozess; liefert (gestartet, Meldung)."""
        why = self.export_allowed()
        if why:
            self.export.update(state="pausiert" if self.export_target else "aus", message=why)
            return False, why
        if self.export_proc is not None:
            self.export_pending = True
            return False, "Export laeuft bereits, danach folgt ein weiterer"
        if bpy.data.is_dirty:
            note = " (ungespeicherte Aenderungen fehlen im Export, erst speichern)"
        else:
            note = ""
        py = self.python_exe or _default_python(self.project_root)
        cmd = [py, "-P", "-m", "skptool", "convert", bpy.data.filepath, "-o", self.export_target, "-q"]
        if self.args.allow_external:  # Sitzung aus einer .skp: externe Bilder hat der Nutzer selbst gewaehlt
            cmd.append("--allow-external")
        env = dict(os.environ)
        env.pop("PYTHONHOME", None)
        if self.project_root:
            env["PYTHONPATH"] = self.project_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        self.export["runs"] += 1
        self.export_log = os.path.join(self.tmp, f"export_{self.export['runs']}.log")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with open(self.export_log, "w", encoding="utf-8") as log:
            self.export_proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env,
                                                cwd=self.project_root or None, creationflags=flags,
                                                stdin=subprocess.DEVNULL)
        self.export.update(state="laeuft", started=time.monotonic(), finished=None, seconds=None,
                           message=f"Schreibe {os.path.basename(self.export_target)}" + note)
        _report(f"skptool: schreibe {os.path.basename(self.export_target)} ...")
        _redraw()
        return True, self.export["message"]

    def export_status(self):
        out = dict(self.export)
        out.pop("started", None)
        out["pending"] = self.export_pending or self.export_due is not None
        return out


class UserError(Exception):
    pass


LIVE = None  # die Instanz, gesetzt in main()


# ---------------------------------------------------------------- Kontext und Anzeige

def _window():
    wm = bpy.context.window_manager
    return wm.windows[0] if wm and wm.windows else None


def _override(need_view3d=False):
    """Kontext fuer bpy.ops aus einem Timer heraus (dort gibt es sonst kein Fenster)."""
    wm = bpy.context.window_manager
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                region = next((r for r in area.regions if r.type == "WINDOW"), None)
                return {"window": win, "screen": win.screen, "area": area, "region": region}
    if need_view3d:
        raise UserError("Keine 3D-Ansicht offen")
    win = _window()
    return {"window": win, "screen": win.screen} if win else {}


def _redraw():
    try:
        for win in bpy.context.window_manager.windows:
            for area in win.screen.areas:
                if area.type in ("STATUSBAR", "VIEW_3D", "OUTLINER", "PROPERTIES"):
                    area.tag_redraw()
    except Exception:
        pass


def _report(message, level="INFO"):
    """Meldung in der Statusleiste und im Info-Editor (ueber einen kleinen Operator)."""
    try:
        with bpy.context.temp_override(**_override()):
            bpy.ops.skptool.live_report(message=message, level=level)
    except Exception:
        print(f"skptool live: {message}", flush=True)


def _indicator_text():
    live = LIVE
    if live is None:
        return LABEL
    ex = live.export
    if ex["state"] == "laeuft":
        return f"{LABEL} | Export laeuft ..."
    if ex["state"] == "ok":
        return f"{LABEL} | {ex['message']}"
    if ex["state"] == "fehler":
        return f"{LABEL} | Export fehlgeschlagen"
    if ex["state"] == "pausiert":
        return f"{LABEL} | Export pausiert"
    if ex["state"] in ("bereit", "geplant"):
        return f"{LABEL} | Strg+S schreibt {os.path.basename(ex['target'])}"
    return LABEL


def _draw_statusbar(self, context):
    icon = "ERROR" if LIVE and LIVE.export["state"] == "fehler" else "LINKED"
    self.layout.label(text=_indicator_text(), icon=icon)


def _draw_view3d_header(self, context):
    self.layout.label(text="skptool live", icon="LINKED")


class SKPTOOL_OT_live_report(bpy.types.Operator):
    """Meldung von skptool live anzeigen"""
    bl_idname = "skptool.live_report"
    bl_label = "skptool live Meldung"
    bl_options = {"INTERNAL"}

    message: bpy.props.StringProperty()
    level: bpy.props.EnumProperty(items=[("INFO", "Info", ""), ("WARNING", "Warnung", ""),
                                         ("ERROR", "Fehler", "")], default="INFO")

    def execute(self, context):
        self.report({self.level}, self.message)
        return {"FINISHED"}


# ---------------------------------------------------------------- Befehle (Hauptthread)

def cmd_ping(live, req):
    return {"pong": True, "pid": os.getpid(), "protocol": PROTOCOL}


def cmd_status(live, req):
    scene = bpy.context.scene
    meshes = sum(1 for o in scene.objects if o.type == "MESH")
    return {"blend": bpy.data.filepath, "dirty": bpy.data.is_dirty, "mode": bpy.context.mode,
            "objects": meshes, "all_objects": len(scene.objects), "engine": scene.render.engine,
            "camera": scene.camera.name if scene.camera else None, "blender": bpy.app.version_string, "pid": os.getpid(), "port": live.port,
            "export": live.export_status(), "queue": live.jobs.qsize(), "ops": sorted(skp_ops.OPS)}


def _ensure_object_mode():
    if bpy.context.mode != "OBJECT" and bpy.ops.object.mode_set.poll():
        bpy.ops.object.mode_set(mode="OBJECT")
        return True
    return False


def cmd_ops(live, req):
    operations = req.get("ops")
    if isinstance(operations, dict):
        operations = [operations]
    if not isinstance(operations, list) or not operations or not all(isinstance(o, dict) for o in operations):
        raise UserError('ops braucht eine Liste wie [{"op": "move", "select": {"name": "Palme*"}, "by": [0, 0, 1]}]')
    if len(operations) > 1000:
        raise UserError("Hoechstens 1000 Operationen pro Anfrage")
    with bpy.context.temp_override(**_override()):
        switched = _ensure_object_mode()
        results = skp_ops.run(operations, stop_on_error=bool(req.get("stop_on_error", True)))
        changing = [r["op"] for r in results if r.get("ok") and r["op"] not in ("list", "summary")]
        undo = None
        if changing:
            names = list(dict.fromkeys(changing))
            undo = ("skptool: " + ", ".join(names))[:60]
            bpy.ops.ed.undo_push(message=undo)
    _redraw()
    out = {"ok": all(r.get("ok") for r in results), "results": results, "undo_step": undo}
    if switched:
        out["note"] = "Blender war nicht im Objektmodus, auf Objektmodus umgeschaltet"
    if not out["ok"]:
        bad = next(r for r in results if not r.get("ok"))
        out["error"] = f"Operation {bad.get('index')} ({bad.get('op')}): {bad.get('error')}"
    return out


def cmd_undo(live, req):
    steps = int(req.get("steps", 1))
    if not 1 <= steps <= 50:
        raise UserError("steps muss zwischen 1 und 50 liegen")
    done = 0
    with bpy.context.temp_override(**_override()):
        for _ in range(steps):
            if not bpy.ops.ed.undo.poll():
                break
            bpy.ops.ed.undo()
            done += 1
    _redraw()
    return {"undone": done}


def cmd_save(live, req):
    if not bpy.data.filepath:
        raise UserError("Die Datei hat noch keinen Namen. Bitte einmal in Blender mit Datei > Speichern unter sichern.")
    with bpy.context.temp_override(**_override()):
        bpy.ops.wm.save_mainfile()
    # save_post hat den Export bereits eingeplant (falls ein Ziel gesetzt ist)
    return {"saved": bpy.data.filepath, "export": live.export_status()}


def cmd_export(live, req):
    started, msg = live.start_export()
    return {"started": started, "message": msg, "export": live.export_status()}


def cmd_screenshot(live, req):
    view = req.get("view", "model")
    try:
        width = min(max(int(req.get("width", 1600)), 64), 4096)
        height = min(max(int(req.get("height", 1000)), 64), 4096)
    except (TypeError, ValueError):
        raise UserError("width und height muessen Zahlen sein") from None
    live.shots += 1
    path = os.path.join(live.tmp, f"bild_{live.shots:04d}.png")
    if view == "model":
        _render_model(path, width, height)
    elif view == "viewport":
        with bpy.context.temp_override(**_override(need_view3d=True)):
            bpy.ops.screen.screenshot_area(filepath=path)
    elif view == "window":
        with bpy.context.temp_override(**_override()):
            bpy.ops.screen.screenshot(filepath=path)
    else:
        raise UserError("view muss model, viewport oder window sein")
    if not os.path.exists(path):
        raise UserError("Bild wurde nicht geschrieben")
    return {"path": path, "bytes": os.path.getsize(path), "view": view}


def cmd_quit(live, req):
    if bpy.data.is_dirty and not req.get("force"):
        raise UserError("Es gibt ungespeicherte Aenderungen. Erst speichern oder mit force beenden.")
    bpy.app.timers.register(_quit_later, first_interval=0.2)
    return {"quitting": True, "export_running": live.export_proc is not None}


def _quit_later():
    LIVE.shutdown()
    with bpy.context.temp_override(**_override()):
        bpy.ops.wm.quit_blender()
    return None


COMMANDS = {"ping": cmd_ping, "status": cmd_status, "ops": cmd_ops, "undo": cmd_undo, "save": cmd_save,
            "export_skp": cmd_export, "screenshot": cmd_screenshot, "quit": cmd_quit}


# ---------------------------------------------------------------- Vorschaubild ohne Spuren

def _stamp_flags(render):
    return [p.identifier for p in render.bl_rna.properties
            if p.identifier.startswith("use_stamp") and p.type == "BOOLEAN" and not p.is_readonly]


def _render_model(path, width, height):
    """Wie bridge.render_preview, aber ohne bleibende Kamera und mit wiederhergestellten Einstellungen."""
    with bpy.context.temp_override(**_override()):
        scene = bpy.context.scene
        meshes = [o for o in scene.objects if o.type == "MESH" and o.visible_get()]
        if not meshes:
            raise UserError("Keine sichtbare Geometrie zum Rendern")
        mn, mx = skp_ops._world_bbox(meshes)
        center = (mn + mx) / 2
        radius = max((mx - mn).length / 2, 1e-3)
        r, sh = scene.render, scene.display.shading
        saved = {
            "camera": scene.camera, "world": scene.world,
            "world_color": tuple(scene.world.color) if scene.world else None,
            "engine": r.engine, "rx": r.resolution_x, "ry": r.resolution_y, "rp": r.resolution_percentage,
            "fmt": r.image_settings.file_format, "filepath": r.filepath,
            "view_transform": scene.view_settings.view_transform,
            "stamp": {k: getattr(r, k) for k in _stamp_flags(r)},
            "shading": {k: getattr(sh, k) for k in ("light", "color_type", "show_object_outline",
                                                    "show_cavity", "background_type")},
        }
        cam_data = bpy.data.cameras.new("skptool_live_cam")
        cam = bpy.data.objects.new("skptool_live_cam", cam_data)
        world = None
        scene.collection.objects.link(cam)
        try:
            direction = Vector((1.0, -1.3, 0.8)).normalized()
            cam_data.lens = 50
            cam_data.sensor_fit = "AUTO"
            fov = 2 * math.atan(cam_data.sensor_width / (2 * cam_data.lens))
            if width >= height:
                fov = fov * height / width
            dist = radius / math.sin(fov / 2) * 1.02
            cam.location = center + direction * dist
            cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()
            cam_data.clip_start = dist / 1000
            cam_data.clip_end = dist * 10
            scene.camera = cam
            if scene.world is None:
                world = bpy.data.worlds.new("skptool_live_world")
                scene.world = world
            scene.world.color = (0.92, 0.93, 0.95)
            r.engine = "BLENDER_WORKBENCH"
            sh.light = "STUDIO"
            sh.color_type = "TEXTURE"
            sh.show_object_outline = True
            sh.show_cavity = True
            sh.background_type = "WORLD"
            scene.view_settings.view_transform = "Standard"
            r.resolution_x, r.resolution_y, r.resolution_percentage = width, height, 100
            r.image_settings.file_format = "PNG"
            r.filepath = path
            for k in saved["stamp"]:  # keine Metadaten (Dateipfad, Datum, Rechner) im PNG
                setattr(r, k, False)
            bpy.ops.render.render(write_still=True)
        finally:
            scene.camera = saved["camera"]
            bpy.data.objects.remove(cam, do_unlink=True)
            bpy.data.cameras.remove(cam_data, do_unlink=True)
            scene.world = saved["world"]
            if world is not None:
                bpy.data.worlds.remove(world, do_unlink=True)
            elif saved["world_color"] is not None:
                scene.world.color = saved["world_color"]
            r.engine = saved["engine"]
            r.resolution_x, r.resolution_y, r.resolution_percentage = saved["rx"], saved["ry"], saved["rp"]
            r.image_settings.file_format = saved["fmt"]
            r.filepath = saved["filepath"]
            scene.view_settings.view_transform = saved["view_transform"]
            for k, v in saved["stamp"].items():
                setattr(r, k, v)
            for k, v in saved["shading"].items():
                setattr(sh, k, v)


# ---------------------------------------------------------------- Handler

@persistent
def _on_save_post(*args):
    live = LIVE
    if live is None or not live.export_target:
        return
    saved = next((a for a in args if isinstance(a, str) and a), bpy.data.filepath)
    if live.blend_at_start and _norm(saved) != _norm(live.blend_at_start):
        live.export.update(state="pausiert", message=f"Gespeichert als {os.path.basename(saved)}, "
                                                     f"Export nur fuer {os.path.basename(live.blend_at_start)}")
        _redraw()
        return
    if live.export_proc is not None:
        live.export_pending = True
        live.export.update(message="Export laeuft, danach folgt ein weiterer")
        return
    live.schedule_export()


@persistent
def _on_load_post(*args):
    live = LIVE
    if live is None:
        return
    try:
        live.write_state()  # neue Datei in der Statusdatei vermerken
    except OSError:
        pass
    if live.export_target and live.export_allowed():
        live.export.update(state="pausiert", message=live.export_allowed())
    elif live.export_target and live.export["state"] == "pausiert":
        live.export.update(state="bereit", message="")
    _redraw()


def _parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    ap = argparse.ArgumentParser(prog="live_server.py")
    ap.add_argument("--state", required=True, help="Pfad der Statusdatei (Port, Token)")
    ap.add_argument("--export-skp", help="Nach jedem Speichern diese .skp schreiben")
    ap.add_argument("--python-exe", help="Python fuer den Export (Standard: .venv des Projekts)")
    ap.add_argument("--project-root", help="Projektordner von skptool (PYTHONPATH fuer den Export)")
    ap.add_argument("--allow-external", action="store_true",
                    help="Export: lokale Bilder uebernehmen (nur fuer Sitzungen aus einer .skp)")
    return ap.parse_args(argv)


def _default_python(root):
    root = root or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for cand in (os.path.join(root, ".venv", "Scripts", "python.exe"), os.path.join(root, ".venv", "bin", "python")):
        if os.path.exists(cand):
            return cand
    return shutil.which("python") or shutil.which("python3") or "python"


def main():
    global LIVE
    args = _parse_args()
    if bpy.app.background:
        print("skptool live braucht ein Blender-Fenster (nicht mit -b starten)", file=sys.stderr)
    if not args.project_root:
        args.project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    LIVE = Live(args)
    bpy.utils.register_class(SKPTOOL_OT_live_report)
    bpy.types.STATUSBAR_HT_header.append(_draw_statusbar)
    bpy.types.VIEW3D_HT_header.append(_draw_view3d_header)
    bpy.app.handlers.save_post.append(_on_save_post)
    bpy.app.handlers.load_post.append(_on_load_post)
    atexit.register(LIVE.shutdown)
    threading.Thread(target=LIVE.serve, name="skptool-live", daemon=True).start()
    bpy.app.timers.register(LIVE.tick, first_interval=0.1, persistent=True)


main()
