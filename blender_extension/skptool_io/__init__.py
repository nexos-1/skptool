"""SketchUp (.skp) Import und Export ueber skptool.

Die Erweiterung enthaelt kein OpenSKP. Sie ruft ein installiertes skptool als eigenen Prozess auf,
dessen Pfad in den Add-on-Einstellungen steht (oder am Standardort von tools/aufruf_einrichten.py).
"""
import atexit

import bpy
from bpy.props import IntProperty, StringProperty

from . import operators, runner


class SKPTOOL_IO_OT_detect(bpy.types.Operator):
    """Look for the skptool launcher created by tools/aufruf_einrichten.py"""
    bl_idname = "skptool_io.detect"
    bl_label = "Detect skptool"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        found = runner.autodetect()
        if not found:
            self.report({"WARNING"}, f"No skptool launcher found at {runner.default_starter()}")
            return {"CANCELLED"}
        operators.prefs(context).skptool_path = found
        self.report({"INFO"}, f"skptool found: {found}")
        return {"FINISHED"}


class SKPTOOL_IO_OT_check(bpy.types.Operator):
    """Run 'skptool --version' with the configured path"""
    bl_idname = "skptool_io.check"
    bl_label = "Test skptool"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        try:
            cmd = operators.skptool_command(context)
            version = runner.version_of(cmd)
        except (runner.SkptoolNotFound, OSError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except Exception as exc:  # z. B. Zeitlimit
            self.report({"ERROR"}, f"{type(exc).__name__}: {exc}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"{version} ({cmd.description})")
        return {"FINISHED"}


class SkptoolPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    skptool_path: StringProperty(
        name="skptool Path", subtype="FILE_PATH",
        description="skptool project folder, its skptool.cmd / skptool launcher, the launcher from "
                    "tools/aufruf_einrichten.py, or the venv Python. Empty: use the launcher in "
                    "~/.local/bin if present")
    timeout: IntProperty(name="Timeout (s)", default=3600, min=10, max=86400,
                         description="Stop skptool if an import or export takes longer than this")

    def draw(self, context):
        col = self.layout.column()
        row = col.row(align=True)
        row.prop(self, "skptool_path")
        row.operator(SKPTOOL_IO_OT_detect.bl_idname, text="", icon="VIEWZOOM")
        col.prop(self, "timeout")
        row = col.row()
        row.operator(SKPTOOL_IO_OT_check.bl_idname, icon="CHECKMARK")
        try:
            cmd = operators.skptool_command(context)
            col.label(text=f"Runs: {cmd.description}", icon="CONSOLE")
        except runner.SkptoolNotFound as exc:
            col.label(text=str(exc), icon="ERROR")


class SKPTOOL_IO_FH_skp(bpy.types.FileHandler):
    bl_idname = "SKPTOOL_IO_FH_skp"
    bl_label = "SketchUp (.skp)"
    bl_import_operator = operators.IMPORT_SCENE_OT_skptool_skp.bl_idname
    bl_file_extensions = ".skp"

    @classmethod
    def poll_drop(cls, context):
        return context.area is not None and context.area.type in {"VIEW_3D", "OUTLINER"}


def menu_import(self, context):
    self.layout.operator(operators.IMPORT_SCENE_OT_skptool_skp.bl_idname, text="SketchUp (.skp)")


def menu_export(self, context):
    self.layout.operator(operators.EXPORT_SCENE_OT_skptool_skp.bl_idname, text="SketchUp (.skp)")


classes = (
    SKPTOOL_IO_OT_detect,
    SKPTOOL_IO_OT_check,
    SkptoolPreferences,
    operators.IMPORT_SCENE_OT_skptool_skp,
    operators.EXPORT_SCENE_OT_skptool_skp,
    SKPTOOL_IO_FH_skp,
)


@bpy.app.handlers.persistent
def _on_load_pre(*_args):
    operators.abort_all()


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_export)
    bpy.app.handlers.load_pre.append(_on_load_pre)
    atexit.register(operators.abort_all)  # Blender beenden: laufende skptool-Prozesse mit beenden


def unregister():
    operators.abort_all()
    atexit.unregister(operators.abort_all)
    if _on_load_pre in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_on_load_pre)
    bpy.types.TOPBAR_MT_file_export.remove(menu_export)
    bpy.types.TOPBAR_MT_file_import.remove(menu_import)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
