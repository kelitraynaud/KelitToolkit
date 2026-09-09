"""Auto Clean: the main cleanup tools chained in the right order, with a
preview of what each step will touch and a single undo step."""

import bpy
import mathutils

from ..utils import normalize_name_for_unreal
from .export import collect_validation_issues
from .instances import OBJECT_OT_detect_and_replace_instances
from .materials import find_duplicate_materials
from .report import set_report, validation_lines
from .scene_cleanup import OBJECT_OT_delete_unused_empties, ORGANIZATION_ITEMS


ORIGIN_PRESETS = [
    ('BOTTOM_CENTER', "Bottom Center", "Origin at the bottom center (Unreal convention for props)"),
    ('CENTER', "Center", "Origin at the center of the bounds"),
    ('TOP_CENTER', "Top Center", "Origin at the top center (lights, ceiling elements)"),
    ('BOTTOM_CORNER', "Bottom Corner", "Origin at the bottom corner (modular architecture)"),
]


def origin_offset(obj, preset):
    """Local-space offset the origin preset would apply to the object's
    mesh (zero when the origin already sits there)."""
    corners = [mathutils.Vector(corner) for corner in obj.bound_box]
    low = mathutils.Vector((min(c.x for c in corners), min(c.y for c in corners),
                            min(c.z for c in corners)))
    high = mathutils.Vector((max(c.x for c in corners), max(c.y for c in corners),
                             max(c.z for c in corners)))
    center = (low + high) / 2
    if preset == 'CENTER':
        target = center
    elif preset == 'TOP_CENTER':
        target = mathutils.Vector((center.x, center.y, high.z))
    elif preset == 'BOTTOM_CORNER':
        target = low
    else:
        target = mathutils.Vector((center.x, center.y, low.z))
    return target


def has_unapplied_transform(obj, world=False):
    """Rotation or scale left on the object. `world` looks at the world
    matrix instead: what the object will carry once its parent empties are
    gone (the preview runs before that step)."""
    if world:
        _location, rotation, scale = obj.matrix_world.decompose()
        return (any(abs(value - 1.0) > 1e-6 for value in scale)
                or abs(rotation.angle) > 1e-6)
    return (any(abs(value - 1.0) > 1e-6 for value in obj.scale)
            or any(abs(value) > 1e-6 for value in obj.rotation_euler))


def transform_refused(obj, world=False):
    """What the instance-safe applies skip: mirrored or zero scale."""
    if world:
        scale = obj.matrix_world.to_scale()
        mirrored = obj.matrix_world.to_3x3().determinant() < 0
    else:
        scale = obj.scale
        mirrored = min(obj.scale) < 0
    return mirrored or min(abs(value) for value in scale) < 1e-6


class _Probe:
    """Stand-in operator instance for the dry-run helpers of other operators
    (their option values, plus the helper methods those helpers call)."""
    baked_positions = True
    compare_materials = 'CONTENT'
    preserve_camera_rig = True
    process_all = True
    _ancestors = OBJECT_OT_delete_unused_empties._ancestors
    _has_animation = OBJECT_OT_delete_unused_empties._has_animation
    _driver_targets = OBJECT_OT_delete_unused_empties._driver_targets


def compute_preview(context, pool, origin_preset, organization='COLLECTIONS'):
    """What each Auto Clean step would touch on `pool` (visible, editable
    objects in scope). Read-only."""
    meshes = [obj for obj in pool if obj.type == 'MESH' and obj.data]
    probe = _Probe()
    protected = OBJECT_OT_delete_unused_empties._protected_empties(probe, context)
    empties = [obj for obj in pool if obj.type == 'EMPTY' and obj.name not in protected]
    structural = 0
    if organization == 'PARENTS':
        structural = sum(1 for obj in empties if obj.children)
        empties = [obj for obj in empties if not obj.children]
    groups = OBJECT_OT_detect_and_replace_instances.find_duplicate_meshes(probe, meshes)
    seen = set()
    origins = 0
    for obj in meshes:
        if obj.data.name in seen:
            continue
        seen.add(obj.data.name)
        if origin_offset(obj, origin_preset).length > 1e-4:
            origins += 1
    renames = 0
    for obj in pool:
        kind = obj.type if obj.type in ('LIGHT', 'CAMERA', 'ARMATURE') else 'MESH'
        if normalize_name_for_unreal(obj.name, kind) != obj.name:
            renames += 1
        elif obj.type == 'MESH' and obj.data and obj.data.name != obj.name:
            renames += 1
    material_renames = sum(
        1 for material in bpy.data.materials
        if material.library is None
        and normalize_name_for_unreal(material.name, 'MATERIAL') != material.name)
    return {
        'objects': len(pool),
        'hidden': sum(1 for obj in context.scene.objects if not obj.visible_get()),
        'materials': sum(len(group) - 1 for group in find_duplicate_materials()),
        'empties': len(empties),
        'empties_kept': len(protected) + structural,
        'duplicates': sum(len(objs) - 1 for objs in groups.values()),
        'transforms': sum(1 for obj in meshes
                          if has_unapplied_transform(obj, world=True)
                          and not transform_refused(obj, world=True)),
        'transforms_refused': sum(1 for obj in meshes if transform_refused(obj, world=True)),
        'origins': origins,
        'renames': renames,
        'material_renames': material_renames,
    }


class OBJECT_OT_auto_clean(bpy.types.Operator):
    """Run the main cleanup tools in the right order: merge duplicate
    materials, delete unused empties, turn duplicate meshes into instances,
    apply rotation and scale, set the origins, normalize the names, then
    validate. Shows what each step will touch before running. One Ctrl+Z
    undoes everything"""
    bl_idname = "kelit_toolkit.auto_clean"
    bl_label = "Auto Clean"
    bl_options = {'REGISTER', 'UNDO'}

    scope: bpy.props.EnumProperty(
        name="Scope",
        description="Which objects to clean",
        items=[
            ('SCENE', "Entire Scene", "Every visible object of the current scene"),
            ('SELECTED', "Selected Only", "The selected objects (and the instances made from them)"),
        ],
        default='SCENE'
    )
    merge_materials: bpy.props.BoolProperty(
        name="Merge Duplicate Materials",
        description="Materials with the same textures, values and links become one "
                    "(whole file: materials are shared between scenes)",
        default=True
    )
    delete_empties: bpy.props.BoolProperty(
        name="Delete Unused Empties",
        description="Empties that nothing needs are removed, their children keep their "
                    "world transform. Camera rigs, constraint and driver targets and "
                    "animated parents are kept",
        default=True
    )
    organization: bpy.props.EnumProperty(
        name="Organization",
        description="What becomes of the structure the empties gave the scene",
        items=ORGANIZATION_ITEMS,
        default='COLLECTIONS'
    )
    instance_duplicates: bpy.props.BoolProperty(
        name="Duplicates to Instances",
        description="Objects with the same shape, UVs and materials share one mesh "
                    "(positions baked into the meshes are handled)",
        default=True
    )
    apply_transforms: bpy.props.BoolProperty(
        name="Apply Rotation and Scale",
        description="Instance-safe applies: every user of a mesh is updated together. "
                    "Mirrored or zero scales are skipped and listed",
        default=True
    )
    set_origins: bpy.props.BoolProperty(
        name="Set Origins",
        description="Move the origin of every mesh to the preset below (once per shared mesh, "
                    "world positions kept)",
        default=True
    )
    origin_preset: bpy.props.EnumProperty(
        name="Origin",
        description="Where the origin goes",
        items=ORIGIN_PRESETS,
        default='BOTTOM_CENTER'
    )
    normalize_names: bpy.props.BoolProperty(
        name="Normalize Names",
        description="SM_ / M_ prefixes, PascalCase, Blender suffixes removed, mesh data "
                    "named after its object",
        default=True
    )
    validate: bpy.props.BoolProperty(
        name="Validate at the End",
        description="List what still needs attention before sending to Unreal",
        default=True
    )

    REMEMBERED_OPTIONS = ('scope', 'merge_materials', 'delete_empties', 'organization',
                          'instance_duplicates', 'apply_transforms', 'set_origins',
                          'origin_preset', 'normalize_names', 'validate')

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    # ------------------------------------------------------------------
    # object pools
    def _pool(self, context):
        """Editable, visible objects in scope (hidden objects cannot be
        selected, so the selection-based tools would skip them anyway)."""
        if self.scope == 'SELECTED':
            names = getattr(self, '_working_names', None)
            if names is None:
                names = {obj.name for obj in context.selected_objects}
                self._working_names = names
            pool = [bpy.data.objects[name] for name in names if name in bpy.data.objects]
        else:
            pool = list(context.scene.objects)
        return [obj for obj in pool if obj.library is None and obj.visible_get()]

    def _meshes(self, context):
        return [obj for obj in self._pool(context) if obj.type == 'MESH' and obj.data]

    @staticmethod
    def _select(context, objects):
        for obj in context.view_layer.objects:
            obj.select_set(False)
        for obj in objects:
            obj.select_set(True)
        context.view_layer.objects.active = objects[0] if objects else None

    # ------------------------------------------------------------------
    # preview
    def _preview(self, context):
        key = (self.scope, self.origin_preset, self.organization)
        cached = getattr(self, '_preview_cache', None)
        if cached is not None and cached[0] == key:
            return cached[1]
        preview = compute_preview(context, self._pool(context), self.origin_preset,
                                  self.organization)
        self._preview_cache = (key, preview)
        return preview

    # ------------------------------------------------------------------
    def invoke(self, context, event):
        settings = context.scene.kelit_toolkit_settings
        if settings.auto_clean_options_saved:
            for name in self.REMEMBERED_OPTIONS:
                setattr(self, name, getattr(settings, 'auto_clean_' + name))
        self._working_names = None
        if not self._pool(context):
            self.report({'WARNING'}, "Nothing to clean: no visible object in scope")
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=460)

    def _remember_options(self, context):
        settings = context.scene.kelit_toolkit_settings
        for name in self.REMEMBERED_OPTIONS:
            setattr(settings, 'auto_clean_' + name, getattr(self, name))
        settings.auto_clean_options_saved = True

    def draw(self, context):
        layout = self.layout
        preview = self._preview(context)
        layout.prop(self, "scope")
        layout.label(text=f"{preview['objects']} object(s) in scope"
                          + (f", {preview['hidden']} hidden left untouched"
                             if preview['hidden'] else ''))
        layout.separator()

        def step(prop, count_text):
            row = layout.row()
            row.prop(self, prop)
            row.label(text=count_text)

        step("merge_materials", f"{preview['materials']} to merge")
        step("delete_empties", f"{preview['empties']} to delete, {preview['empties_kept']} kept")
        row = layout.row()
        row.enabled = self.delete_empties
        row.prop(self, "organization")
        step("instance_duplicates", f"{preview['duplicates']} to instance")
        refused = (f", {preview['transforms_refused']} skipped (mirror or zero scale)"
                   if preview['transforms_refused'] else '')
        step("apply_transforms", f"{preview['transforms']} to apply{refused}")
        step("set_origins", f"{preview['origins']} mesh(es) to move")
        row = layout.row()
        row.enabled = self.set_origins
        row.prop(self, "origin_preset")
        step("normalize_names",
             f"{preview['renames']} object(s), {preview['material_renames']} material(s)")
        step("validate", "report at the end")
        layout.separator()
        layout.label(text="Steps run in this order. Ctrl+Z undoes all of them.", icon='INFO')

    # ------------------------------------------------------------------
    def execute(self, context):
        self._preview_cache = None
        self._working_names = None
        self._remember_options(context)
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        initial_selection = [obj.name for obj in context.selected_objects]
        initial_active = context.view_layer.objects.active.name if context.view_layer.objects.active else None
        if self.scope == 'SELECTED':
            self._working_names = set(initial_selection)
        report = []
        try:
            if self.merge_materials:
                before = len(bpy.data.materials)
                bpy.ops.kelit_toolkit.merge_duplicate_materials('EXEC_DEFAULT')
                report.append(f"Materials merged: {before - len(bpy.data.materials)}")

            if self.delete_empties:
                empties = [obj for obj in self._pool(context) if obj.type == 'EMPTY']
                before = len(context.scene.objects)
                collections_before = len(bpy.data.collections)
                if empties:
                    self._select(context, empties)
                    bpy.ops.kelit_toolkit.delete_unused_empties(
                        'EXEC_DEFAULT', preserve_camera_rig=True,
                        process_all=(self.scope == 'SCENE'),
                        organization=self.organization)
                line = f"Unused empties deleted: {before - len(context.scene.objects)}"
                if self.organization == 'COLLECTIONS':
                    line += f", {len(bpy.data.collections) - collections_before} collection(s) created"
                elif self.organization == 'PARENTS':
                    line += ", parent empties kept"
                report.append(line)

            if self.instance_duplicates:
                meshes = self._meshes(context)
                before_names = {obj.name for obj in bpy.data.objects}
                unique_before = len({obj.data.name for obj in meshes})
                if meshes:
                    self._select(context, meshes)
                    bpy.ops.kelit_toolkit.detect_and_replace_instances(
                        'EXEC_DEFAULT', search_scope='SELECTED', baked_positions=True,
                        compare_materials='CONTENT', rename_to_mesh=True)
                if self._working_names is not None:
                    self._working_names |= {obj.name for obj in bpy.data.objects} - before_names
                unique_after = len({obj.data.name for obj in self._meshes(context)})
                report.append(f"Duplicates turned into instances: {unique_before - unique_after}")

            if self.apply_transforms:
                meshes = self._meshes(context)
                todo = [obj for obj in meshes if has_unapplied_transform(obj)]
                if meshes:
                    self._select(context, meshes)
                    bpy.ops.kelit_toolkit.apply_rotation_instances('EXEC_DEFAULT')
                    self._select(context, meshes)
                    bpy.ops.kelit_toolkit.apply_scale_instances('EXEC_DEFAULT')
                left = [obj for obj in self._meshes(context) if has_unapplied_transform(obj)]
                line = f"Transforms applied: {len(todo) - len(left)}"
                if left:
                    line += f", {len(left)} skipped: " + ", ".join(obj.name for obj in left[:3])
                report.append(line)

            if self.set_origins:
                meshes = self._meshes(context)
                moved = len({obj.data.name for obj in meshes
                             if origin_offset(obj, self.origin_preset).length > 1e-4})
                if meshes:
                    self._select(context, meshes)
                    bpy.ops.kelit_toolkit.set_origin_preset('EXEC_DEFAULT', preset=self.origin_preset)
                label = dict((item[0], item[1]) for item in ORIGIN_PRESETS)[self.origin_preset]
                report.append(f"Origins moved to {label}: {moved} mesh(es)")

            if self.normalize_names:
                pool = self._pool(context)
                before_names = {obj.name for obj in pool}
                material_names = {m.name for m in bpy.data.materials}
                if pool:
                    self._select(context, pool)
                    bpy.ops.kelit_toolkit.normalize_names_quick('EXEC_DEFAULT')
                meshes = self._meshes(context)
                if meshes:
                    self._select(context, meshes)
                    bpy.ops.kelit_toolkit.set_mesh_name_from_object('EXEC_DEFAULT')
                if self._working_names is not None:
                    self._working_names = {obj.name for obj in pool}
                renamed = len(before_names - {obj.name for obj in pool})
                renamed_materials = len(material_names - {m.name for m in bpy.data.materials})
                report.append(f"Renamed: {renamed} object(s), {renamed_materials} material(s)")

            issues, warnings = [], []
            if self.validate:
                issues, warnings = collect_validation_issues(self._meshes(context))
                report.append(f"Validation: {len(issues)} issue(s), {len(warnings)} warning(s)")
        finally:
            self._select(context, [bpy.data.objects[name] for name in initial_selection
                                   if name in bpy.data.objects and bpy.data.objects[name].visible_get()])
            if initial_active and initial_active in bpy.data.objects:
                context.view_layer.objects.active = bpy.data.objects[initial_active]

        print("\n=== Auto Clean ===")
        for line in report + issues + warnings:
            print(line)
        set_report(context, "Auto Clean",
                   [('INFO', line) for line in report] + validation_lines(issues, warnings))

        def draw_report(menu, _context):
            for line in report:
                menu.layout.label(text=line)
            if issues or warnings:
                menu.layout.separator()
                for line in (issues + warnings)[:4]:
                    menu.layout.label(text=line)
            menu.layout.separator()
            menu.layout.operator('kelit_toolkit.show_report', text="Show full report", icon='TEXT')

        # no popup without a window (background mode): the console report is enough
        if not bpy.app.background and context.window is not None:
            context.window_manager.popup_menu(draw_report, title="Auto Clean done", icon='CHECKMARK')
        self.report({'INFO'}, " | ".join(report))
        return {'FINISHED'}


classes = (
    OBJECT_OT_auto_clean,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
