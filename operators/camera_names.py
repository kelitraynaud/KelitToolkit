"""Rename Cameras: the project convention Cam_<NN>_<Shot>_V<n>[A|B|C]_<lens>mm
(Cam_01_Large_V3B_50mm, Cam_02_CloseUp_V1_35mm). Object and camera data are
renamed together; the focal length is read from the camera. Timeline
markers and sequencer strips point at the object, not at its name: they
follow, and the report says how many are bound to each camera."""

import re

import bpy

from ..utils import iter_action_fcurves

CAMERA_PATTERN = re.compile(
    r'^Cam_(?P<number>\d+)_(?P<shot>[A-Za-z0-9]+)_V(?P<version>\d+)(?P<variant>[A-Z]?)_'
    r'(?P<lens>[\d.]+)mm$')

VARIANT_ITEMS = [
    ('NONE', "-", "No variant letter"),
    ('A', "A", "Variant A"),
    ('B', "B", "Variant B"),
    ('C', "C", "Variant C"),
]


def shot_label(text):
    """'close up' -> CloseUp, 'large' -> Large, 'CloseUp' unchanged: letters
    and digits only, one capital per word."""
    words = re.findall(r'[A-Za-z0-9]+', text)
    return ''.join(word[:1].upper() + word[1:] for word in words)


def lens_label(lens):
    """50.0 -> '50', 35.5 -> '35.5'."""
    rounded = round(lens, 1)
    return f"{rounded:g}"


def camera_name(number, shot, version, variant, lens):
    letter = '' if variant in ('', 'NONE') else variant
    return f"Cam_{number:02d}_{shot_label(shot) or 'Shot'}_V{version}{letter}_{lens_label(lens)}mm"


def lens_is_animated(camera_data):
    """True when the focal length changes over time (keys or a driver): the
    name then only gives the value at the current frame."""
    anim = getattr(camera_data, 'animation_data', None)
    if anim is None:
        return False
    for fcurve in iter_action_fcurves(anim.action):
        if fcurve.data_path == 'lens' and len(fcurve.keyframe_points) > 1:
            values = {round(k.co[1], 3) for k in fcurve.keyframe_points}
            if len(values) > 1:
                return True
    return any(driver.data_path == 'lens' for driver in anim.drivers)


def camera_bindings(scene, obj):
    """(timeline markers, sequencer strips) that point at the camera object."""
    markers = [m.name for m in scene.timeline_markers if m.camera == obj]
    strips = []
    editor = scene.sequence_editor
    if editor is not None:
        for strip in getattr(editor, 'strips_all', None) or getattr(editor, 'sequences_all', []):
            if getattr(strip, 'scene_camera', None) == obj:
                strips.append(strip.name)
    return markers, strips


def first_marker_frame(scene, obj):
    frames = [m.frame for m in scene.timeline_markers if m.camera == obj]
    return min(frames) if frames else None


class KelitCameraRenameItem(bpy.types.PropertyGroup):
    camera: bpy.props.StringProperty(name="Camera")
    number: bpy.props.IntProperty(name="NN", min=0, max=999, default=1)
    shot: bpy.props.StringProperty(name="Shot", default="Large",
                                   description="Shot type: Large, CloseUp, Medium...")
    version: bpy.props.IntProperty(name="V", min=0, max=999, default=1)
    variant: bpy.props.EnumProperty(name="Variant", items=VARIANT_ITEMS, default='NONE')


class OBJECT_OT_rename_cameras(bpy.types.Operator):
    """Rename cameras to Cam_<NN>_<Shot>_V<n>[A|B|C]_<lens>mm, object and camera data together. The focal length is read from each camera"""
    bl_idname = "kelit_toolkit.rename_cameras"
    bl_label = "Rename Cameras"
    bl_options = {'REGISTER', 'UNDO'}

    items: bpy.props.CollectionProperty(type=KelitCameraRenameItem)

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and context.scene is not None

    def _cameras(self, context):
        selected = [o for o in context.selected_objects if o.type == 'CAMERA']
        cameras = selected or [o for o in context.scene.objects if o.type == 'CAMERA']
        scene = context.scene

        def order(obj):
            frame = first_marker_frame(scene, obj)
            return (frame is None, frame or 0, obj.name)
        return sorted(cameras, key=order)

    def fill(self, context):
        """One row per camera, prefilled from a name that already follows
        the convention, numbered in marker order otherwise."""
        self.items.clear()
        for index, obj in enumerate(self._cameras(context), start=1):
            item = self.items.add()
            item.camera = obj.name
            match = CAMERA_PATTERN.match(obj.name)
            if match:
                item.number = int(match['number'])
                item.shot = match['shot']
                item.version = int(match['version'])
                item.variant = match['variant'] or 'NONE'
            else:
                item.number = index
        return len(self.items)

    def invoke(self, context, event):
        if not self.fill(context):
            self.report({'WARNING'}, "No camera in the selection or the scene")
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=760)

    def plan(self, context):
        """[(object, new name, lens animated, markers, strips)]."""
        result = []
        for item in self.items:
            obj = bpy.data.objects.get(item.camera)
            if obj is None or obj.type != 'CAMERA':
                continue
            name = camera_name(item.number, item.shot, item.version, item.variant, obj.data.lens)
            markers, strips = camera_bindings(context.scene, obj)
            result.append((obj, name, lens_is_animated(obj.data), markers, strips))
        return result

    def draw(self, context):
        layout = self.layout
        plan = {obj.name: (name, animated, markers, strips)
                for obj, name, animated, markers, strips in self.plan(context)}
        names = [entry[0] for entry in plan.values()]
        header = layout.row()
        for text in ("Camera", "NN", "Shot", "V", "Var.", "New name"):
            header.label(text=text)
        for item in self.items:
            if item.camera not in plan:
                continue
            name, animated, markers, strips = plan[item.camera]
            row = layout.row(align=True)
            row.label(text=item.camera, icon='OUTLINER_OB_CAMERA')
            row.prop(item, "number", text="")
            row.prop(item, "shot", text="")
            row.prop(item, "version", text="")
            row.prop(item, "variant", text="")
            icon = 'ERROR' if animated or names.count(name) > 1 else 'FORWARD'
            row.label(text=name, icon=icon)
            if animated:
                layout.label(text=f"   {item.camera}: focal length animated, name uses the "
                                  "current frame", icon='KEYFRAME')
            if markers or strips:
                layout.label(text=f"   {len(markers)} marker(s), {len(strips)} strip(s) "
                                  "bound, they follow the rename", icon='MARKER')
        if len(set(names)) < len(names):
            layout.label(text="Two cameras get the same name: change NN, V or the variant",
                         icon='ERROR')

    def execute(self, context):
        from .report import set_report
        if not self.items:
            self.fill(context)
        plan = self.plan(context)
        if not plan:
            self.report({'WARNING'}, "No camera to rename")
            return {'CANCELLED'}
        names = [entry[1] for entry in plan]
        if len(set(names)) < len(names):
            self.report({'ERROR'}, "Two cameras would get the same name: nothing renamed")
            return {'CANCELLED'}
        cameras = {entry[0] for entry in plan}
        taken = [n for n in names if n in bpy.data.objects
                 and bpy.data.objects[n] not in cameras]
        if taken:
            self.report({'ERROR'}, f"Name already used by another object: {', '.join(taken[:3])}")
            return {'CANCELLED'}

        before = {obj: camera_bindings(context.scene, obj) for obj, *_rest in plan}
        lines = [('INFO', f"{obj.name} -> {name}", name) for obj, name, *_rest in plan]
        # two passes so cameras can swap names (Cam_01 <-> Cam_02)
        own_data = {obj: obj.data.users == 1 and obj.data.library is None
                    for obj, *_rest in plan}
        for index, (obj, *_rest) in enumerate(plan):
            obj.name = f"__kelit_cam_{index}"
            if own_data[obj]:
                obj.data.name = f"__kelit_camdata_{index}"
        shared_data = 0
        for obj, name, animated, _markers, _strips in plan:
            obj.name = name
            data = obj.data
            if not own_data[obj]:
                shared_data += 1
                lines.append(('WARNING', f"{name}: camera data shared or linked, "
                                         "data not renamed", name))
            else:
                data.name = name
                if data.name != name:
                    lines.append(('WARNING', f"{name}: camera data is called {data.name}, "
                                             "the name was taken by other camera data", name))
            after = camera_bindings(context.scene, obj)
            ok = after == before[obj]
            text = f"{name}: {len(after[0])} marker(s), {len(after[1])} strip(s) still bound"
            if animated:
                text += ", focal length animated (name uses the current frame)"
            lines.append(('INFO' if ok and not animated else 'WARNING', text, name))
        # the rows keep the old names: a redo (F9) undoes first, so they
        # still find their cameras
        set_report(context, "Rename Cameras", lines)
        message = f"{len(plan)} camera(s) renamed"
        if shared_data:
            message += f", {shared_data} with shared camera data left as is"
        self.report({'INFO'}, message)
        return {'FINISHED'}


classes = (
    KelitCameraRenameItem,
    OBJECT_OT_rename_cameras,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
