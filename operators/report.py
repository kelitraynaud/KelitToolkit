"""Report window: the findings of Validate for Unreal and Auto Clean in a
scrollable list, with copy-to-clipboard and select-the-objects actions.
The lines live on the window manager, so the window can be reopened."""

import re

import bpy


ICONS = {'ISSUE': 'ERROR', 'WARNING': 'INFO', 'INFO': 'CHECKMARK'}
OBJECT_PATTERN = re.compile(r'^\[[!i]\] (.+?): ')


class KelitReportLine(bpy.types.PropertyGroup):
    text: bpy.props.StringProperty()
    kind: bpy.props.StringProperty(default='INFO')
    object_name: bpy.props.StringProperty()


def _select_active_line(_self, context):
    """Clicking a line selects the object it talks about."""
    wm = context.window_manager
    lines = wm.kelit_report_lines
    if not (0 <= wm.kelit_report_index < len(lines)) or context.mode != 'OBJECT':
        return
    obj = bpy.data.objects.get(lines[wm.kelit_report_index].object_name)
    if obj is None or not obj.visible_get():
        return
    try:
        for other in context.view_layer.objects:
            other.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
    except RuntimeError:
        pass


class KELIT_UL_report(bpy.types.UIList):
    bl_idname = "KELIT_UL_report"

    def draw_item(self, _context, layout, _data, item, _icon, _active_data,
                  _active_propname, _index):
        layout.label(text=item.text, icon=ICONS.get(item.kind, 'DOT'))


def set_report(context, title, lines):
    """Store a report. `lines` holds (kind, text) or (kind, text, object_name)
    tuples; kind is ISSUE, WARNING or INFO. Object names are read from the
    '[!] Name: ...' validation format when not given."""
    wm = context.window_manager
    wm.kelit_report_lines.clear()
    wm.kelit_report_title = title
    for entry in lines:
        kind, text = entry[0], entry[1]
        name = entry[2] if len(entry) > 2 else ''
        if not name:
            match = OBJECT_PATTERN.match(text)
            if match:
                name = match.group(1)
        line = wm.kelit_report_lines.add()
        line.text, line.kind, line.object_name = text, kind, name


def validation_lines(issues, warnings):
    return [('ISSUE', text) for text in issues] + [('WARNING', text) for text in warnings]


def open_report(context):
    """Open the report window (no-op without a window, i.e. in background)."""
    if bpy.app.background or context.window is None:
        return
    bpy.ops.kelit_toolkit.show_report('INVOKE_DEFAULT')


class KELIT_OT_show_report(bpy.types.Operator):
    """Show the last report (Validate for Unreal or Auto Clean) as a
    scrollable list. Click a line to select its object"""
    bl_idname = "kelit_toolkit.show_report"
    bl_label = "Show Last Report"
    bl_options = {'REGISTER'}

    def invoke(self, context, event):
        if not context.window_manager.kelit_report_lines:
            self.report({'INFO'}, "No report yet: run Validate for Unreal or Auto Clean first")
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=720)

    def draw(self, context):
        wm = context.window_manager
        layout = self.layout
        issues = sum(1 for line in wm.kelit_report_lines if line.kind == 'ISSUE')
        warnings = sum(1 for line in wm.kelit_report_lines if line.kind == 'WARNING')
        layout.label(text=f"{wm.kelit_report_title}: {issues} issue(s), {warnings} warning(s)",
                     icon='INFO')
        layout.template_list("KELIT_UL_report", "", wm, "kelit_report_lines",
                             wm, "kelit_report_index", rows=16)
        row = layout.row(align=True)
        row.operator(KELIT_OT_report_copy.bl_idname, icon='COPYDOWN')
        row.operator(KELIT_OT_report_select.bl_idname, icon='RESTRICT_SELECT_OFF')

    def execute(self, _context):
        return {'FINISHED'}


class KELIT_OT_report_copy(bpy.types.Operator):
    """Copy the whole report to the clipboard"""
    bl_idname = "kelit_toolkit.report_copy"
    bl_label = "Copy to Clipboard"
    bl_options = {'REGISTER'}

    def execute(self, context):
        wm = context.window_manager
        wm.clipboard = "\n".join(line.text for line in wm.kelit_report_lines)
        self.report({'INFO'}, f"{len(wm.kelit_report_lines)} line(s) copied")
        return {'FINISHED'}


class KELIT_OT_report_select(bpy.types.Operator):
    """Select every visible object the report points at"""
    bl_idname = "kelit_toolkit.report_select"
    bl_label = "Select Objects"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if context.mode != 'OBJECT':
            self.report({'WARNING'}, "Switch to Object Mode first")
            return {'CANCELLED'}
        names = {line.object_name for line in context.window_manager.kelit_report_lines
                 if line.object_name and line.kind in ('ISSUE', 'WARNING')}
        for obj in context.view_layer.objects:
            obj.select_set(False)
        selected = 0
        for name in sorted(names):
            obj = bpy.data.objects.get(name)
            if obj is not None and obj.visible_get():
                obj.select_set(True)
                if selected == 0:
                    context.view_layer.objects.active = obj
                selected += 1
        self.report({'INFO'}, f"{selected} object(s) selected")
        return {'FINISHED'}


classes = (
    KelitReportLine,
    KELIT_UL_report,
    KELIT_OT_show_report,
    KELIT_OT_report_copy,
    KELIT_OT_report_select,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.kelit_report_lines = bpy.props.CollectionProperty(type=KelitReportLine)
    bpy.types.WindowManager.kelit_report_index = bpy.props.IntProperty(
        default=0, update=_select_active_line)
    bpy.types.WindowManager.kelit_report_title = bpy.props.StringProperty(default="Report")


def unregister():
    del bpy.types.WindowManager.kelit_report_title
    del bpy.types.WindowManager.kelit_report_index
    del bpy.types.WindowManager.kelit_report_lines
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
