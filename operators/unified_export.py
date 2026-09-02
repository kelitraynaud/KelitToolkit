"""Unified asset export - one entry point, three asset kinds.

The exporter classifies what is being sent and routes each asset through the
right pipeline:

- SKELETAL: an armature + every mesh it deforms (or that is parented to it),
  exported as ONE file per armature, animation included.
- STATIC: a mesh + its UCX_/UBX_/USP_ collisions and _LOD companions,
  one file per base mesh (same grouping as Batch FBX Export).
- SCENE: everything else (cameras, lights, empties, curves) - or, when the
  type is forced to SCENE, the whole selection in a single file.

USD is the primary format (future-proof, and UE's USD importer preserves far
more than FBX); FBX stays available with per-type settings that mirror
the BlenderTools defaults for skeletal meshes (FBX_SCALE_NONE, no leaf bones).
"""

import contextlib
import math
import os

import bpy

from ..utils import clean_name
from .export import is_companion_object, find_companion_objects


UE_NAME_PREFIXES = ('SM_', 'SK_', 'SKM_', 'A_', 'AM_', 'S_', 'BP_', 'UCX_', 'UBX_', 'USP_', 'UCP_')


# ============================================================================
# ASSET DETECTION / GROUPING
# ============================================================================

def get_deform_armature(obj):
    """The armature that deforms *obj* (Armature modifier first, then
    armature-deform parenting), or None."""
    if obj.type != 'MESH':
        return None
    for modifier in obj.modifiers:
        if modifier.type == 'ARMATURE' and modifier.object is not None:
            return modifier.object
    parent = obj.parent
    if parent is not None and parent.type == 'ARMATURE' and obj.parent_type in {'ARMATURE', 'BONE'}:
        return parent
    return None


def is_descendant_of(obj, ancestor):
    parent = obj.parent
    while parent is not None:
        if parent == ancestor:
            return True
        parent = parent.parent
    return False


def detect_asset_type(obj):
    """'SKELETAL' | 'STATIC' | 'SCENE' for a single object."""
    if obj.type == 'ARMATURE' or get_deform_armature(obj) is not None:
        return 'SKELETAL'
    if obj.type == 'MESH':
        return 'STATIC'
    return 'SCENE'


def collect_assets(context, objects, asset_type='AUTO', include_companions=True):
    """
    Group *objects* into exportable assets.

    :return list: [{'type', 'name', 'root', 'objects'}] - skeletal assets carry
        the armature + all meshes it deforms (scene-wide, so selecting just the
        armature is enough), static assets carry the mesh + its companions.
    """
    if asset_type == 'SCENE':
        # forced: ship the selection as-is, one file
        exportables = [o for o in objects]
        if not exportables:
            return []
        return [{'type': 'SCENE', 'name': get_export_name(context), 'root': None,
                 'objects': exportables}]

    scene_meshes = [o for o in context.scene.objects if o.type == 'MESH']
    assets = []
    used = set()

    # --- skeletal: one asset per armature involved in the selection ---
    armatures = []
    for obj in objects:
        armature = obj if obj.type == 'ARMATURE' else get_deform_armature(obj)
        if armature is not None and armature not in armatures:
            armatures.append(armature)

    for armature in armatures:
        members = [armature]
        for mesh in scene_meshes:
            if get_deform_armature(mesh) == armature or is_descendant_of(mesh, armature):
                members.append(mesh)
        assets.append({'type': 'SKELETAL', 'name': armature.name,
                       'root': armature, 'objects': members})
        used.update(member.name for member in members)

    # --- static: one asset per base mesh (+ collisions/LODs) ---
    for obj in objects:
        if obj.type != 'MESH' or obj.name in used:
            continue
        if include_companions and is_companion_object(obj):
            used.add(obj.name)
            continue
        companions = find_companion_objects(obj, scene_meshes) if include_companions else []
        assets.append({'type': 'STATIC', 'name': obj.name,
                       'root': obj, 'objects': [obj] + companions})
        used.add(obj.name)
        used.update(companion.name for companion in companions)

    # --- scene: whatever is left that USD/FBX can carry ---
    leftovers = [o for o in objects
                 if o.name not in used and o.type in {'CAMERA', 'LIGHT', 'EMPTY', 'CURVE', 'FONT'}]
    if leftovers:
        assets.append({'type': 'SCENE', 'name': get_export_name(context),
                       'root': None, 'objects': leftovers})

    if asset_type != 'AUTO':
        assets = [asset for asset in assets if asset['type'] == asset_type]
    return assets


def get_export_name(context):
    if bpy.data.filepath:
        return os.path.splitext(os.path.basename(bpy.data.filepath))[0]
    return context.scene.name or 'BlenderScene'


def asset_filename(asset, auto_prefix=True):
    """File stem for an asset, optionally forcing the UE naming prefix."""
    base = clean_name(asset['name'])
    if auto_prefix and not base.startswith(UE_NAME_PREFIXES):
        if asset['type'] == 'SKELETAL':
            base = 'SK_' + base
        elif asset['type'] == 'STATIC':
            base = 'SM_' + base
    return base


# ============================================================================
# EXPORT STATE HELPERS
# ============================================================================

@contextlib.contextmanager
def isolated_selection(objects):
    """Select exactly *objects*, restore the user's selection afterwards."""
    original_selection = [o for o in bpy.context.selected_objects]
    original_active = bpy.context.view_layer.objects.active
    try:
        for obj in bpy.data.objects:
            obj.select_set(False)
        for obj in objects:
            try:
                obj.select_set(True)
            except RuntimeError:
                pass  # not in this view layer
        if objects:
            bpy.context.view_layer.objects.active = objects[0]
        yield
    finally:
        for obj in bpy.data.objects:
            obj.select_set(False)
        for obj in original_selection:
            try:
                obj.select_set(True)
            except RuntimeError:
                pass
        bpy.context.view_layer.objects.active = original_active


@contextlib.contextmanager
def action_frame_range(context, asset, include_animation):
    """
    Match the scene frame range to the asset's active action(s) for the
    duration of the export, so animation is never cut at the scene's end
    frame, then restore the user's range.
    """
    scene = context.scene
    ranges = []
    if include_animation:
        for obj in asset['objects']:
            animation_data = obj.animation_data
            if animation_data and animation_data.action:
                start, end = animation_data.action.frame_range
                ranges.append((start, end))
    if not ranges:
        yield
        return

    original = (scene.frame_start, scene.frame_end)
    scene.frame_start = int(math.floor(min(r[0] for r in ranges)))
    scene.frame_end = int(math.ceil(max(r[1] for r in ranges)))
    try:
        yield
    finally:
        scene.frame_start, scene.frame_end = original


def _hangs_below(member, root):
    parent = member.parent
    while parent is not None:
        if parent == root:
            return True
        parent = parent.parent
    return False


def _hierarchy_depth(obj):
    depth = 0
    while obj.parent is not None:
        depth += 1
        obj = obj.parent
    return depth


def _members_to_move(asset):
    """Members whose world matrix has to be set by hand: the root itself and
    the members that do not hang below it (children of the root follow it).
    Parents first: a member parented to another moved member is then re-based
    against its parent's NEW matrix. Plain list order once re-based Hair
    (child) before Head (parent), and Hair ended up moved twice, for good."""
    root = asset['root']
    members = [member for member in asset['objects']
               if member == root or not _hangs_below(member, root)]
    return sorted(members, key=_hierarchy_depth)


@contextlib.contextmanager
def _moved_members(asset, transform):
    """Apply `transform(matrix_world)` to the members that need it, restore
    every touched matrix on the way out (also when an assignment fails)."""
    moved = []
    try:
        for member in _members_to_move(asset):
            original_matrix = member.matrix_world.copy()
            moved.append((member, original_matrix))
            member.matrix_world = transform(original_matrix)
        bpy.context.view_layer.update()
        yield
    finally:
        for member, original_matrix in moved:
            member.matrix_world = original_matrix
        bpy.context.view_layer.update()


@contextlib.contextmanager
def at_world_origin(asset):
    """Temporarily move a static asset group to the world origin (keeps the
    relative offsets of its collisions/LODs), then restore."""
    root = asset['root']
    if root is None:
        yield
        return
    delta = root.matrix_world.translation.copy()

    def shifted(matrix):
        new_matrix = matrix.copy()
        new_matrix.translation = matrix.translation - delta
        return new_matrix

    with _moved_members(asset, shifted):
        yield


@contextlib.contextmanager
def at_neutral_root(asset):
    """Temporarily neutralize the root's FULL transform (identity), members
    following in relative space, then restore. Used by the skeletal sync
    export: the spawn re-applies location, rotation and scale from the
    payload, so any rotation or scale left in the file would be applied
    twice."""
    root = asset['root']
    if root is None:
        yield
        return
    root_inverse = root.matrix_world.inverted_safe()
    with _moved_members(asset, lambda matrix: root_inverse @ matrix):
        yield


# ============================================================================
# FORMAT BACKENDS
# ============================================================================

def export_asset_usd(filepath, asset, include_animation):
    """USD backend - primary format. Skeletal assets ride UsdSkel (armatures +
    blend shapes), ready for UE's USD/Interchange importer."""
    kwargs = dict(
        filepath=filepath,
        selected_objects_only=True,
        export_materials=True,
        convert_world_material=False,
        use_instancing=False,
        export_lights=False,
        export_cameras=False,
        export_animation=False,
        export_armatures=False,
        export_shapekeys=False,
    )
    if asset['type'] == 'SKELETAL':
        kwargs.update(
            export_armatures=True,
            export_shapekeys=True,
            export_animation=include_animation,
            only_deform_bones=False,
        )
    elif asset['type'] == 'STATIC':
        # parity with USD Scene Sync: UE triangulates anyway, do it here so
        # what you see in Blender is exactly what UE builds
        kwargs.update(triangulate_meshes=True)
    else:  # SCENE
        kwargs.update(
            export_lights=True,
            export_cameras=True,
            export_armatures=True,
            export_shapekeys=True,
            export_animation=include_animation,
        )
    bpy.ops.wm.usd_export(**kwargs)


def export_asset_fbx(filepath, asset, include_animation):
    """FBX backend - fallback. Skeletal settings follow the BlenderTools defaults."""
    kwargs = dict(
        filepath=filepath,
        use_selection=True,
        axis_forward='-Z',
        axis_up='Y',
        apply_unit_scale=True,
        use_custom_props=False,
        mesh_smooth_type='FACE',
        use_tspace=True,
        use_mesh_modifiers=True,
    )
    if asset['type'] == 'SKELETAL':
        kwargs.update(
            object_types={'ARMATURE', 'MESH'},
            # FBX_SCALE_NONE + apply_unit_scale is the proven UE recipe: the
            # skeleton lands in UE at scale 1.0 with no unit-conversion warning
            apply_scale_options='FBX_SCALE_NONE',
            add_leaf_bones=False,
            use_armature_deform_only=False,
            armature_nodetype='NULL',
            bake_anim=include_animation,
            bake_anim_use_all_actions=False,
            bake_anim_use_nla_strips=False,
            bake_anim_use_all_bones=True,
            bake_anim_step=1.0,
            bake_anim_simplify_factor=0.0,
            bake_anim_force_startend_keying=True,
        )
    elif asset['type'] == 'STATIC':
        # parity with the legacy Batch FBX Export settings
        kwargs.update(
            object_types={'MESH'},
            apply_scale_options='FBX_SCALE_ALL',
            bake_anim=False,
        )
    else:  # SCENE
        kwargs.update(
            object_types={'MESH', 'EMPTY', 'CAMERA', 'LIGHT'},
            apply_scale_options='FBX_SCALE_NONE',
            bake_anim=include_animation,
        )
    bpy.ops.export_scene.fbx(**kwargs)


# ============================================================================
# OPERATOR
# ============================================================================

class UNREAL_OT_export_assets(bpy.types.Operator):
    """Export the selection as separate Unreal-ready assets - skeletal meshes
    (armature + skinned meshes + animation), static meshes (with collisions
    and LODs) and scene objects - as USD (recommended) or FBX"""
    bl_idname = "unreal_toolkit.export_assets"
    bl_label = "Export Assets"
    bl_options = {'REGISTER'}

    directory: bpy.props.StringProperty(
        name="Export Directory",
        description="Destination folder for the exported files",
        subtype='DIR_PATH'
    )

    file_format: bpy.props.EnumProperty(
        name="Format",
        description="File format sent to Unreal",
        items=[
            ('USD', "USD (recommended)", "Future-proof pipeline: skeletal via UsdSkel, "
             "better material fidelity through UE's USD importer"),
            ('FBX', "FBX", "Classic pipeline with per-type settings proven against Unreal"),
        ],
        default='USD'
    )

    asset_type: bpy.props.EnumProperty(
        name="Asset Type",
        description="What to make of the selection",
        items=[
            ('AUTO', "Auto Detect", "Classify each selected object: armatures and skinned "
             "meshes become skeletal assets, plain meshes static assets, the rest scene objects"),
            ('SKELETAL', "Skeletal Mesh", "Only the skeletal assets (armature + skinned meshes)"),
            ('STATIC', "Static Mesh", "Only the static meshes (with their collisions/LODs)"),
            ('SCENE', "Scene / Other", "The whole selection in a single file, as-is"),
        ],
        default='AUTO'
    )

    include_animation: bpy.props.BoolProperty(
        name="Include Animation",
        description="Export the active action of each armature (the scene frame "
                    "range is temporarily stretched to the action's own range)",
        default=True
    )

    include_companions: bpy.props.BoolProperty(
        name="Include Collisions & LODs",
        description="Export UCX_/UBX_/USP_ collision meshes and _LOD meshes "
                    "inside the same file as their base object",
        default=True
    )

    export_at_origin: bpy.props.BoolProperty(
        name="Static Meshes at Origin",
        description="Temporarily move each static asset to the world origin so "
                    "the pivot is correct in Unreal, then restore its position",
        default=True
    )

    auto_prefix: bpy.props.BoolProperty(
        name="Auto Prefix (SK_/SM_)",
        description="Prefix the file name with SK_ (skeletal) or SM_ (static) "
                    "when the object name does not already carry a UE prefix",
        default=True
    )

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "file_format")
        layout.prop(self, "asset_type")
        layout.separator()
        layout.prop(self, "include_animation")
        layout.prop(self, "include_companions")
        layout.prop(self, "export_at_origin")
        layout.prop(self, "auto_prefix")

    def execute(self, context):
        if not self.directory:
            self.report({'WARNING'}, "No folder selected")
            return {'CANCELLED'}

        objects = list(context.selected_objects)
        if not objects:
            export_collection = bpy.data.collections.get('Export')
            if export_collection:
                objects = list(export_collection.all_objects)
        if not objects:
            self.report({'WARNING'}, "Nothing selected (and no 'Export' collection)")
            return {'CANCELLED'}

        if context.object and context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        assets = collect_assets(context, objects, self.asset_type, self.include_companions)
        if not assets:
            self.report({'WARNING'}, f"No {self.asset_type.lower()} asset found in the selection")
            return {'CANCELLED'}

        extension = '.usd' if self.file_format == 'USD' else '.fbx'
        backend = export_asset_usd if self.file_format == 'USD' else export_asset_fbx

        exported = []
        failures = []
        counts = {'SKELETAL': 0, 'STATIC': 0, 'SCENE': 0}
        used_stems = set()

        for asset in assets:
            # dedup: 'Rig' and 'Rig.001' clean to the same stem and would
            # silently overwrite each other in the same batch
            stem = asset_filename(asset, self.auto_prefix)
            candidate = stem
            suffix = 1
            while candidate.lower() in used_stems:
                suffix += 1
                candidate = f"{stem}_{suffix:02d}"
            used_stems.add(candidate.lower())
            filepath = os.path.join(self.directory, candidate + extension)
            origin_ctx = at_world_origin(asset) if (
                asset['type'] == 'STATIC' and self.export_at_origin) else contextlib.nullcontext()
            try:
                with isolated_selection(asset['objects']), \
                        action_frame_range(context, asset, self.include_animation), \
                        origin_ctx:
                    backend(filepath, asset, self.include_animation)
                exported.append(filepath)
                counts[asset['type']] += 1
            except RuntimeError as error:
                failures.append(f"{asset['name']}: {error}")

        if exported:
            print("\nKelitToolkit - exported assets:")
            for filepath in exported:
                print(f"  {filepath}")
        if failures:
            for failure in failures:
                print(f"Export failed - {failure}")
            self.report({'ERROR'}, f"{len(failures)} export(s) failed - see console")
            return {'CANCELLED'} if not exported else {'FINISHED'}

        summary = ", ".join(f"{count} {kind.lower()}" for kind, count in counts.items() if count)
        self.report({'INFO'}, f"{len(exported)} {self.file_format} file(s) exported ({summary})")
        return {'FINISHED'}


classes = (
    UNREAL_OT_export_assets,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
