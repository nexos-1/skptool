"""Import- und Export-Operatoren. Die eigentliche Arbeit macht skptool in einem eigenen Prozess.

Ablauf Import:  skptool convert EIN.skp -o <temp>/model.blend  ->  Szene anhaengen, Collections und
                Objekte in die aktuelle Szene uebernehmen.
Ablauf Export:  Kopie der Datei speichern (save_as_mainfile copy=True, die offene Datei bleibt
                unveraendert)  ->  bei "Selection Only" oder ohne Modifikatoren: eigener Blender im
                Hintergrund filtert die Kopie (export_filter.py)  ->  skptool convert kopie.blend -o AUS.skp
Im Fenster laeuft das als Modal-Operator (Fortschritt in der Statusleiste, Esc bricht ab), im
Hintergrundmodus (blender -b) blockierend.
"""
import json
import os
import shutil
import tempfile
from pathlib import Path

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper
from mathutils import Matrix

from . import runner

ADDON = __package__
FILTER_SCRIPT = Path(__file__).with_name("export_filter.py")
FILTER_RESULT = "SKPTOOL_IO_FILTER "
_ACTIVE = set()  # laufende Auftraege, damit sie beim Laden einer Datei oder Beenden abgebrochen werden


class Failure(Exception):
    pass


def prefs(context=None):
    context = context or bpy.context
    addon = context.preferences.addons.get(ADDON)
    return addon.preferences if addon else None


def skptool_command(context):
    """Aufruf aus den Einstellungen, sonst der Starter von tools/aufruf_einrichten.py."""
    p = prefs(context)
    setting = (p.skptool_path if p else "") or runner.autodetect() or ""
    if setting.startswith("//"):
        # "//" hiesse: relativ zur offenen .blend. Ein Programm neben einer fremden Datei startet nie.
        raise runner.SkptoolNotFound("The skptool path must be absolute, not relative to the .blend file")
    return runner.resolve(setting)


def timeout_of(context):
    p = prefs(context)
    return int(p.timeout) if p else 3600


def _unit_scale(context, enabled):
    s = float(context.scene.unit_settings.scale_length)
    return s if enabled and s > 0 and abs(s - 1.0) > 1e-9 else None


class Stage:
    def __init__(self, label, start, done):
        self.label, self.start, self.done = label, start, done


class SkptoolTask:
    """Gemeinsamer Teil: Stufen nacheinander ausfuehren, Fortschritt, Abbruch, Aufraeumen."""

    _timer = None
    _job = None
    _tmp = None
    _stages = ()
    _index = 0

    # --- Aufbau

    def _prepare(self, context):
        self._tmp = tempfile.mkdtemp(prefix="skptool_io_")
        self._index = 0
        self._job = None

    def _cleanup(self, context=None):
        if self._job is not None and self._job.proc.poll() is None:
            self._job.cancel()
        if self._timer is not None and context is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        if context is not None and getattr(context, "workspace", None) is not None:
            context.workspace.status_text_set(None)
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None
        _ACTIVE.discard(self)

    def abort(self):
        """Von aussen (Datei laden, Blender beenden): Prozess beenden, Temp-Ordner loeschen."""
        if self._job is not None:
            self._job.cancel()
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None

    def _fail(self, context, msg, popup=True):
        if isinstance(msg, Exception) and not isinstance(msg, (Failure, runner.SkptoolNotFound)):
            msg = f"{type(msg).__name__}: {msg}"
        lines = [l for l in str(msg).splitlines() if l.strip()] or ["Unknown error"]
        self.report({"ERROR"} if popup else {"WARNING"}, f"{self.bl_label}: " + " | ".join(lines[:6]))
        if popup and not bpy.app.background and context.window_manager is not None:
            def draw(menu, _ctx):
                for line in lines[:20]:
                    menu.layout.label(text=line[:220])
            context.window_manager.popup_menu(draw, title=f"{self.bl_label} failed", icon="ERROR")
        self._cleanup(context)
        return {"CANCELLED"}

    def _start_stage(self):
        stage = self._stages[self._index]
        self._job = stage.start()

    # --- Ausfuehren

    def run(self, context):
        try:
            self._prepare(context)
            self._stages = self.build_stages(context)
        except Exception as exc:  # immer aufraeumen und melden
            return self._fail(context, exc)
        _ACTIVE.add(self)
        if bpy.app.background or context.window is None:
            return self._run_blocking(context)
        try:
            self._start_stage()
        except Exception as exc:  # immer aufraeumen und melden
            return self._fail(context, exc)
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.25, window=context.window)
        wm.modal_handler_add(self)
        self._status(context)
        return {"RUNNING_MODAL"}

    def _run_blocking(self, context):
        try:
            while self._index < len(self._stages):
                stage = self._stages[self._index]
                self._job = stage.start()
                self._job.wait()
                stage.done(self._job)
                self._index += 1
            result = self.finish(context)
        except Exception as exc:  # immer aufraeumen und melden
            return self._fail(context, exc)
        self._cleanup(context)
        return result

    def _status(self, context):
        stage = self._stages[self._index]
        step = self._job.progress() if self._job else ""
        text = f"skptool: {stage.label}" + (f", {step}" if step else "") + \
            f" ({self._job.elapsed:.0f} s, Esc to cancel)"
        context.workspace.status_text_set(text)

    def modal(self, context, event):
        if event.type == "ESC" and event.value == "PRESS":
            if self._job is not None:
                self._job.cancel()
            return self._fail(context, "Cancelled", popup=False)
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        rc = self._job.poll()
        if rc is None:
            self._status(context)
            return {"PASS_THROUGH"}
        try:
            self._stages[self._index].done(self._job)
            self._index += 1
            if self._index < len(self._stages):
                self._start_stage()
                self._status(context)
                return {"PASS_THROUGH"}
            result = self.finish(context)
        except Exception as exc:  # immer aufraeumen und melden
            return self._fail(context, exc)
        self._cleanup(context)
        return result

    def cancel(self, context):
        self._cleanup(context)

    # --- Hilfen fuer die Stufen

    def _skptool_stage(self, label, cmd, args):
        env = runner.build_env(cmd)
        timeout = self._timeout
        tmp = self._tmp

        def start():
            return runner.Job(cmd.argv + args, env, tmp, timeout)

        def done(job):
            if job.poll() != 0 or job.timed_out or job.cancelled:
                raise Failure(job.error_text())
            self._last_job = job
        return Stage(label, start, done)


def _check_output(job):
    ok = [l for l in job.stdout if l.startswith("OK ")]
    if not ok:
        raise Failure(job.error_text())
    return ok[-1]


def _summary(ok_line):
    """'OK   a -> b  (6.2s, ...)' -> Inhalt der Klammer."""
    i = ok_line.rfind("  (")
    return ok_line[i + 3:].rstrip(")") if i >= 0 else ok_line


class IMPORT_SCENE_OT_skptool_skp(SkptoolTask, bpy.types.Operator, ImportHelper):
    """Import a SketchUp file (.skp) using skptool (components, tags, materials)"""
    bl_idname = "import_scene.skptool_skp"
    bl_label = "Import SketchUp"
    bl_options = {"REGISTER", "UNDO", "PRESET"}

    filename_ext = ".skp"
    filter_glob: StringProperty(default="*.skp", options={"HIDDEN"})
    textures: BoolProperty(name="Textures", default=True,
                           description="Import texture images (off: colors only, faster)")
    keep_triangles: BoolProperty(name="Keep Triangles", default=False,
                                 description="Do not merge triangles back into SketchUp-style faces")
    use_unit_scale: BoolProperty(name="Scene Unit Scale", default=True,
                                 description="Scale by the scene unit scale (SketchUp models are in meters)")

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "textures")
        col.prop(self, "keep_triangles")
        col.prop(self, "use_unit_scale")

    def invoke(self, context, event):
        # Drag & Drop (FileHandler): Pfad ist schon gesetzt, nur die Optionen zeigen
        if self.filepath and os.path.isfile(self.filepath) and hasattr(ImportHelper, "invoke_popup"):
            return self.invoke_popup(context)
        return ImportHelper.invoke(self, context, event)

    def execute(self, context):
        return self.run(context)

    def build_stages(self, context):
        src = Path(bpy.path.abspath(self.filepath))
        if src.suffix.lower() != ".skp" or not src.is_file():
            raise Failure(f"Not a SketchUp file: {src}")
        cmd = skptool_command(context)
        self._timeout = timeout_of(context)
        self._src = src
        self._blend = os.path.join(self._tmp, "model.blend")
        args = ["convert", str(src), "-o", self._blend, "--blender", bpy.app.binary_path]
        if not self.textures:
            args.append("--no-textures")
        if self.keep_triangles:
            args.append("--keep-triangles")
        return [self._skptool_stage(f"reading {src.name}", cmd, args)]

    def finish(self, context):
        _check_output(self._last_job)
        if not os.path.isfile(self._blend):
            raise Failure("skptool did not write the intermediate .blend file")
        stats = append_scene(context, self._blend, _unit_scale(context, self.use_unit_scale))
        self.report({"INFO"}, f"Imported {self._src.name}: {stats['objects']} objects, "
                              f"{stats['materials']} materials, tags: {', '.join(stats['collections']) or '-'}")
        return {"FINISHED"}


def append_scene(context, blend, unit_scale=None):
    """Szene aus der Zwischendatei anhaengen und ihren Inhalt in die aktuelle Szene haengen."""
    worlds_before = set(bpy.data.worlds)
    mats_before = set(bpy.data.materials)
    with bpy.data.libraries.load(blend, link=False) as (src, dst):
        if not src.scenes:
            raise Failure("The intermediate file contains no scene")
        dst.scenes = [src.scenes[0]]
    sc = dst.scenes[0]
    if sc is None:
        raise Failure("Could not append the imported scene")
    target = context.scene
    cols = list(sc.collection.children)
    objects = list(sc.objects)
    for c in cols:
        target.collection.children.link(c)
    for ob in sc.collection.objects:
        target.collection.objects.link(ob)
    bpy.data.scenes.remove(sc)
    for w in set(bpy.data.worlds) - worlds_before:
        if w.users == 0:
            bpy.data.worlds.remove(w)
    if unit_scale:
        s = Matrix.Scale(1.0 / unit_scale, 4)
        for ob in objects:
            if ob.parent is None:
                ob.matrix_world = s @ ob.matrix_world
    view = context.view_layer
    for ob in view.objects:
        ob.select_set(False)
    first = None
    for ob in objects:
        if ob.name in view.objects and ob.visible_get(view_layer=view):
            ob.select_set(True)
            if first is None and ob.parent is None:
                first = ob
    if first is not None:
        view.objects.active = first
    return {"objects": sum(1 for o in objects if o.type == "MESH"),
            "materials": len(set(bpy.data.materials) - mats_before),
            "collections": [c.name for c in cols]}


def _selection_tree(context):
    """Ausgewaehlte Objekte samt allen Kindern (wie eine SketchUp-Gruppe mit Inhalt)."""
    keep = set()
    for ob in context.selected_objects:
        keep.add(ob.name_full)
        keep.update(ch.name_full for ch in ob.children_recursive)
    return keep


class EXPORT_SCENE_OT_skptool_skp(SkptoolTask, bpy.types.Operator, ExportHelper):
    """Export the scene or the selection as a SketchUp 2017 file (.skp) using skptool"""
    bl_idname = "export_scene.skptool_skp"
    bl_label = "Export SketchUp"
    bl_options = {"REGISTER", "PRESET"}

    filename_ext = ".skp"
    filter_glob: StringProperty(default="*.skp", options={"HIDDEN"})
    use_selection: BoolProperty(name="Selection Only", default=False,
                                description="Export only selected objects and their children")
    apply_modifiers: BoolProperty(name="Apply Modifiers", default=True,
                                  description="Export the evaluated geometry (as skptool convert does). "
                                              "Off: modifiers are ignored")
    textures: BoolProperty(name="Textures", default=True, description="Write texture images")
    use_unit_scale: BoolProperty(name="Scene Unit Scale", default=True,
                                 description="Apply the scene unit scale (otherwise 1 unit = 1 meter)")
    allow_external: BoolProperty(name="Allow External Files", default=False,
                                 description="Also use local files the scene references (unpacked images, "
                                             "linked libraries). Only for scenes from a trusted source")

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "use_selection")
        col.prop(self, "apply_modifiers")
        col.prop(self, "textures")
        col.prop(self, "use_unit_scale")
        col.separator()
        col.prop(self, "allow_external")
        box = col.box().column(align=True)
        if self.allow_external:
            box.label(text="Referenced local files will be read.", icon="ERROR")
            box.label(text="Only use this for scenes from a trusted source.")
        else:
            box.label(text="Only packed data is exported.", icon="INFO")
            box.label(text="Unpacked images and libraries are skipped.")

    def execute(self, context):
        return self.run(context)

    def build_stages(self, context):
        out = Path(bpy.path.abspath(self.filepath))
        if out.suffix.lower() != ".skp":
            out = out.with_suffix(".skp")
        if out.is_dir():
            raise Failure(f"Not a file: {out}")
        cmd = skptool_command(context)  # vor dem Speichern pruefen
        self._timeout = timeout_of(context)
        self._out = out
        keep = None
        if self.use_selection:
            keep = _selection_tree(context)
            if not keep:
                raise Failure("Nothing selected")
        # Kopie der ganzen Datei; Speichern uebernimmt auch offene Aenderungen im Bearbeitungsmodus.
        # copy=True: Dateiname und "ungespeichert"-Zustand der offenen Datei bleiben, wie sie sind.
        copy =os.path.join(self._tmp, "scene.blend")
        bpy.ops.wm.save_as_mainfile(filepath=copy, copy=True, compress=False, relative_remap=True)
        blend = copy
        stages = []
        if keep is not None or not self.apply_modifiers:
            filtered = os.path.join(self._tmp, "export.blend")
            job_file = os.path.join(self._tmp, "filter.json")
            with open(job_file, "w", encoding="utf-8") as fh:
                json.dump({"keep": sorted(keep) if keep is not None else None,
                           "strip_modifiers": not self.apply_modifiers}, fh)
            stages.append(self._filter_stage(copy, filtered, job_file))
            blend = filtered
        args = ["convert", blend, "-o", str(out), "--blender", bpy.app.binary_path]
        if not self.textures:
            args.append("--no-textures")
        if self.allow_external:
            args.append("--allow-external")
        scale = _unit_scale(context, self.use_unit_scale)
        if scale:
            args += ["--unit-scale", repr(scale)]
        stages.append(self._skptool_stage(f"writing {out.name}", cmd, args))
        return stages

    def _filter_stage(self, src, dst, job_file):
        argv = [bpy.app.binary_path, "-b", "--factory-startup", "-Y", "--python-exit-code", "3",
                "--python", str(FILTER_SCRIPT), "--", src, dst, job_file]
        env = dict(os.environ)
        tmp, timeout = self._tmp, self._timeout

        def start():
            return runner.Job(argv, env, tmp, timeout)

        def done(job):
            line = next((l for l in reversed(job.stdout) if l.startswith(FILTER_RESULT)), None)
            res = json.loads(line[len(FILTER_RESULT):]) if line else {}
            if job.poll() != 0 or job.timed_out or job.cancelled or res.get("error") or not os.path.isfile(dst):
                raise Failure(res.get("error") or job.error_text())
            if res.get("objects", 0) == 0:
                raise Failure("Nothing to export")
        return Stage("preparing the scene copy", start, done)

    def finish(self, context):
        ok = _check_output(self._last_job)
        for line in runner.notices(self._last_job):
            self.report({"WARNING"}, line.strip())
        self.report({"INFO"}, f"Exported {self._out.name}: {_summary(ok)}")
        return {"FINISHED"}


def abort_all(*_args):
    for task in list(_ACTIVE):
        task.abort()
    _ACTIVE.clear()
