import os
import re

import bpy
import mathutils
from ..utils import (
    clean_name,
    iter_action_fcurves,
    local_rotation_matrix,
    matrices_close,
    mesh_users,
    reset_local_rotation,
    transform_mesh_geometry,
)


COLLISION_PREFIXES = ('UCX_', 'UBX_', 'USP_', 'UCP_')
LOD_SUFFIX_PATTERN = re.compile(r'_LOD([1-9]\d*)$', re.IGNORECASE)


# ============================================================================
# VALIDATION HELPERS
# ============================================================================

def collect_validation_issues(objects):
    """
    Run all Unreal validation checks on mesh objects.

    :param list objects: mesh objects to validate.
    :return tuple: (issues, warnings) lists of formatted messages.
    """
    issues = []
    warnings = []

    for obj in objects:
        if obj.type != 'MESH' or not obj.data:
            continue

        # Check unapplied transformations
        if obj.scale != mathutils.Vector((1.0, 1.0, 1.0)):
            issues.append(f"[!] {obj.name}: Unapplied scale {obj.scale}")

        if obj.rotation_euler != mathutils.Euler((0.0, 0.0, 0.0)):
            warnings.append(f"[i] {obj.name}: Unapplied rotation")

        # Negative scale mirrors geometry and breaks collisions in Unreal
        if min(obj.scale) < 0:
            issues.append(f"[!] {obj.name}: Negative scale (mirrored) - breaks collision in Unreal")

        # Check materials
        if len(obj.data.materials) == 0:
            issues.append(f"[!] {obj.name}: No material")
        else:
            # Check if materials have textures
            for mat in obj.data.materials:
                if mat and mat.use_nodes:
                    has_textures = any(node.type == 'TEX_IMAGE' for node in mat.node_tree.nodes)
                    if not has_textures:
                        warnings.append(f"[i] {obj.name}: Material '{mat.name}' without textures")

        # Check UVs
        if not obj.data.uv_layers:
            issues.append(f"[!] {obj.name}: No UV map")

        # Check ngons - not critical: the FBX exporter and Unreal triangulate
        # on import. Only a concern for non-planar ngons (possible bad triangulation).
        ngons = [p for p in obj.data.polygons if len(p.vertices) > 4]
        if ngons:
            warnings.append(f"[i] {obj.name}: {len(ngons)} ngon(s) - will be triangulated on export")

        # Check polygon count (performance)
        poly_count = len(obj.data.polygons)
        if poly_count > 50000:
            warnings.append(f"[i] {obj.name}: {poly_count} polygons (consider LOD)")

    return issues, warnings


# ============================================================================
# OPERATORS - EXPORT/VALIDATION
# ============================================================================

class OBJECT_OT_validate_for_unreal(bpy.types.Operator):
    """Validate selected objects for export to Unreal"""
    bl_idname = "object.validate_for_unreal"
    bl_label = "Validate for Unreal"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    def execute(self, context):
        selected_objs = [obj for obj in context.selected_objects if obj.type == 'MESH']

        if not selected_objs:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}

        issues, warnings = collect_validation_issues(selected_objs)

        if issues or warnings:
            print("\n" + "="*50)
            print("UNREAL ENGINE VALIDATION REPORT")
            print("="*50)

            if issues:
                print("\n[!] CRITICAL ISSUES:")
                for issue in issues:
                    print(issue)

            if warnings:
                print("\n[i] WARNINGS:")
                for warning in warnings:
                    print(warning)

            print("="*50 + "\n")

            self.report({'WARNING'}, f"{len(issues)} issue(s), {len(warnings)} warning(s) - See console")
        else:
            self.report({'INFO'}, f"{len(selected_objs)} object(s) validated for Unreal")

        return {'FINISHED'}


class OBJECT_OT_apply_scale_instances(bpy.types.Operator):
    """Apply scale on shared mesh data and all their instances"""
    bl_idname = "object.apply_scale_instances"
    bl_label = "Apply Scale (Instances Safe)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    def execute(self, context):
        selected_objs = [obj for obj in context.selected_objects if obj.type == 'MESH']

        if not selected_objs:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}

        mesh_groups = {}
        for obj in selected_objs:
            mesh_groups.setdefault(obj.data, []).append(obj)

        applied_meshes = 0
        total_instances = 0

        for mesh, objs in mesh_groups.items():
            # the reset below touches EVERY user of the mesh, so the
            # same-scale check must cover every user too - not just the
            # selected ones
            all_instances = mesh_users(mesh)
            scale = objs[0].scale.copy()

            if any(min(obj.scale) < 0 for obj in all_instances):
                self.report({'WARNING'},
                            f"Mesh '{mesh.name}': negative (mirrored) scale - "
                            "baking it would flip the geometry inside-out. Skipped")
                continue

            all_same_scale = all(
                abs(obj.scale.x - scale.x) < 0.0001 and
                abs(obj.scale.y - scale.y) < 0.0001 and
                abs(obj.scale.z - scale.z) < 0.0001
                for obj in all_instances
            )
            if not all_same_scale:
                self.report({'WARNING'}, f"Mesh '{mesh.name}' has instances with "
                                         "different scales (selected or not) - skipped")
                continue

            transform_mesh_geometry(mesh, mathutils.Matrix.Diagonal(scale))

            for obj in all_instances:
                obj.scale = (1.0, 1.0, 1.0)

            applied_meshes += 1
            total_instances += len(all_instances)

        self.report({'INFO'}, f"Scale applied on {applied_meshes} mesh(es) - {total_instances} instance(s) affected")
        return {'FINISHED'}


class OBJECT_OT_apply_rotation_instances(bpy.types.Operator):
    """Apply rotation on shared mesh data and all their instances"""
    bl_idname = "object.apply_rotation_instances"
    bl_label = "Apply Rotation (Instances Safe)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    def execute(self, context):
        selected_objs = [obj for obj in context.selected_objects if obj.type == 'MESH']

        if not selected_objs:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}

        mesh_groups = {}
        for obj in selected_objs:
            mesh_groups.setdefault(obj.data, []).append(obj)

        applied_meshes = 0
        total_instances = 0

        for mesh, objs in mesh_groups.items():
            # bake the LOCAL rotation only: using the world matrix would fold
            # the parent's rotation into the mesh while only the local
            # rotation gets reset - parented objects ended up double-rotated
            all_instances = mesh_users(mesh)
            rotation_matrix = local_rotation_matrix(objs[0])

            all_same_rotation = all(
                matrices_close(local_rotation_matrix(obj), rotation_matrix)
                for obj in all_instances
            )
            if not all_same_rotation:
                self.report({'WARNING'}, f"Mesh '{mesh.name}' has instances with "
                                         "different rotations (selected or not) - skipped")
                continue

            transform_mesh_geometry(mesh, rotation_matrix)

            for obj in all_instances:
                reset_local_rotation(obj)

            applied_meshes += 1
            total_instances += len(all_instances)

        self.report({'INFO'}, f"Rotation applied on {applied_meshes} mesh(es) - {total_instances} instance(s) affected")
        return {'FINISHED'}


def iter_location_fcurves(action):
    """
    Yield every object-level 'location' / 'delta_location' F-Curve of an
    action, on both legacy (4.x) and slotted (5.x) actions - same approach as
    the slotted-actions API introduced in Blender 5.
    """
    if action is None:
        return
    fcurves = []
    if hasattr(action, 'layers'):
        try:
            for layer in action.layers:
                for strip in layer.strips:
                    for channelbag in strip.channelbags:
                        fcurves.extend(channelbag.fcurves)
        except AttributeError:
            fcurves = []
    if not fcurves and hasattr(action, 'fcurves'):
        fcurves = list(action.fcurves)
    for fcurve in fcurves:
        if fcurve.data_path in {'location', 'delta_location'}:
            yield fcurve


class OBJECT_OT_normalize_scene_scale(bpy.types.Operator):
    """Rescale the whole scene (objects AND cameras) by one factor around the
    world origin, so mis-scaled imports reach scale 1.0 at real-world size
    while every camera keeps exactly the same framing"""
    bl_idname = "object.normalize_scene_scale"
    bl_label = "Normalize Scene Scale"
    bl_options = {'REGISTER', 'UNDO'}

    mode: bpy.props.EnumProperty(
        name="Factor",
        items=[
            ('AUTO', "Auto (from object scale)",
             "Use 1 / the most common uniform scale of the selected roots "
             "(e.g. objects at 0.01 give a factor of 100)"),
            ('CUSTOM', "Custom", "Use the factor below"),
        ],
        default='AUTO'
    )

    custom_factor: bpy.props.FloatProperty(
        name="Custom Factor",
        description="Multiply every position and object scale by this value",
        default=100.0,
        min=0.0001,
        soft_max=1000.0
    )

    adjust_lights: bpy.props.BoolProperty(
        name="Adjust Lights",
        description="Scale light power by factor squared (inverse-square law) and "
                    "light sizes by the factor, so the lighting looks unchanged",
        default=True
    )

    adjust_cameras: bpy.props.BoolProperty(
        name="Adjust Camera Clip / Focus",
        description="Scale camera clip start/end and manual focus distance so "
                    "nothing gets clipped or defocused at the new size",
        default=True
    )

    # ------------------------------------------------------------------
    def _roots(self, context):
        objects = context.selected_objects or context.scene.objects
        roots = {}
        for obj in objects:
            root = obj
            while root.parent is not None:
                root = root.parent
            roots[root.name] = root
        return list(roots.values())

    def _auto_factor(self, roots):
        from collections import Counter
        scales = []
        for obj in roots:
            if obj.type in {'CAMERA', 'LIGHT'}:
                continue
            s = obj.scale
            if abs(s.x - s.y) < 1e-6 and abs(s.x - s.z) < 1e-6 and abs(s.x - 1.0) > 1e-6 and s.x > 0:
                scales.append(round(s.x, 6))
        if not scales:
            return None
        most_common = Counter(scales).most_common(1)[0][0]
        return 1.0 / most_common

    # ------------------------------------------------------------------
    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=380)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "mode")
        if self.mode == 'CUSTOM':
            layout.prop(self, "custom_factor")
        layout.prop(self, "adjust_lights")
        layout.prop(self, "adjust_cameras")

        roots = self._roots(context)
        box = layout.box()
        box.label(text=f"{len(roots)} root object(s), scaled around the world origin",
                  icon='OBJECT_ORIGIN')
        if self.mode == 'AUTO':
            factor = self._auto_factor(roots)
            if factor is None:
                box.label(text="No uniformly mis-scaled root found", icon='ERROR')
            else:
                box.label(text=f"Auto factor: x{factor:g}", icon='FULLSCREEN_ENTER')
        box.label(text="Camera framing is preserved", icon='CAMERA_DATA')

    def execute(self, context):
        roots = self._roots(context)
        if not roots:
            self.report({'WARNING'}, "Nothing to rescale")
            return {'CANCELLED'}

        if self.mode == 'AUTO':
            factor = self._auto_factor(roots)
            if factor is None:
                self.report({'WARNING'}, "No uniformly mis-scaled root found - use a Custom factor")
                return {'CANCELLED'}
        else:
            factor = self.custom_factor
        if abs(factor - 1.0) < 1e-9:
            self.report({'INFO'}, "Factor is 1.0 - nothing to do")
            return {'CANCELLED'}

        keyed = 0
        lights = 0
        cameras = 0
        seen_data = set()
        seen_actions = set()

        for obj in roots:
            # uniform scene scaling about the world origin: framing-invariant
            obj.location *= factor
            if obj.delta_location.length > 0:
                obj.delta_location *= factor
            if obj.type not in {'CAMERA', 'LIGHT'}:
                obj.scale *= factor
                # snap to exactly 1.0 when that is clearly the intent
                for axis in range(3):
                    if abs(obj.scale[axis] - 1.0) < 1e-4:
                        obj.scale[axis] = 1.0

            # animated positions must follow, or the first playback undoes it
            anim = obj.animation_data
            action = anim.action if anim else None
            if action is not None and action.name not in seen_actions:
                seen_actions.add(action.name)
                for fcurve in iter_location_fcurves(action):
                    for key in fcurve.keyframe_points:
                        key.co.y *= factor
                        key.handle_left.y *= factor
                        key.handle_right.y *= factor
                    keyed += 1

        # cameras and lights may live deeper than the roots
        pool = context.selected_objects or context.scene.objects
        for obj in pool:
            data = getattr(obj, 'data', None)
            if data is None or data.name_full in seen_data:
                continue
            seen_data.add(data.name_full)

            if obj.type == 'CAMERA' and self.adjust_cameras:
                data.clip_start *= factor
                data.clip_end *= factor
                if data.dof is not None and data.dof.focus_object is None:
                    data.dof.focus_distance *= factor
                cameras += 1

            elif obj.type == 'LIGHT' and self.adjust_lights:
                data.energy *= factor * factor
                if hasattr(data, 'shadow_soft_size'):
                    data.shadow_soft_size *= factor
                if data.type == 'AREA':
                    data.size *= factor
                    if data.shape in {'RECTANGLE', 'ELLIPSE'}:
                        data.size_y *= factor
                lights += 1

        message = f"Scene x{factor:g}: {len(roots)} root(s)"
        if keyed:
            message += f", {keyed} location curve(s)"
        if cameras:
            message += f", {cameras} camera(s)"
        if lights:
            message += f", {lights} light(s)"
        self.report({'INFO'}, message)
        return {'FINISHED'}


class OBJECT_OT_bake_camera_animation(bpy.types.Operator):
    """Bake the camera's FINAL world animation onto the camera itself, then
    drop its parents and constraints. The camera moves identically but no
    longer needs any null/target - they can then be cleaned away"""
    bl_idname = "object.bake_camera_animation"
    bl_label = "Bake Camera Animation"
    bl_options = {'REGISTER', 'UNDO'}

    frame_step: bpy.props.IntProperty(
        name="Frame Step",
        description="Key every Nth frame (1 = every frame, exact)",
        default=1, min=1, max=10
    )

    bake_dof: bpy.props.BoolProperty(
        name="Bake Focus Distance",
        description="When the camera focuses on a target object, convert that to "
                    "keyed focus-distance values so the target is no longer needed",
        default=True
    )

    def _cameras(self, context):
        cameras = [o for o in context.selected_objects if o.type == 'CAMERA']
        if not cameras and context.scene.camera is not None:
            cameras = [context.scene.camera]
        return cameras

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=360)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "frame_step")
        layout.prop(self, "bake_dof")
        cameras = self._cameras(context)
        box = layout.box()
        if cameras:
            box.label(text=f"Camera(s): {', '.join(c.name for c in cameras[:3])}",
                      icon='CAMERA_DATA')
            box.label(text="Parents and constraints will be removed", icon='INFO')
        else:
            box.label(text="No camera selected and no scene camera", icon='ERROR')

    def execute(self, context):
        cameras = self._cameras(context)
        if not cameras:
            self.report({'WARNING'}, "No camera to bake")
            return {'CANCELLED'}

        scene = context.scene
        original_frame = scene.frame_current
        frames = list(range(scene.frame_start, scene.frame_end + 1, self.frame_step))
        if frames and frames[-1] != scene.frame_end:
            frames.append(scene.frame_end)

        # ---- pass 1: sample the FINAL world transforms while the rig lives --
        samples = {camera.name: [] for camera in cameras}
        for frame in frames:
            scene.frame_set(frame)
            depsgraph = context.evaluated_depsgraph_get()
            for camera in cameras:
                evaluated = camera.evaluated_get(depsgraph)
                matrix = evaluated.matrix_world.copy()
                focus = None
                dof = camera.data.dof
                if self.bake_dof and dof.use_dof and dof.focus_object is not None:
                    target = dof.focus_object.evaluated_get(depsgraph)
                    to_target = target.matrix_world.translation - matrix.translation
                    forward = -(matrix.to_3x3().col[2].normalized())
                    focus = abs(to_target.dot(forward))
                samples[camera.name].append((frame, matrix, focus))

        # ---- pass 2: strip the rig and key the sampled transforms ----------
        for camera in cameras:
            if camera.animation_data:
                camera.animation_data_clear()
            camera.constraints.clear()
            camera.parent = None
            camera.rotation_mode = 'XYZ'
            camera.delta_location = (0.0, 0.0, 0.0)
            camera.delta_rotation_euler = (0.0, 0.0, 0.0)
            camera.delta_scale = (1.0, 1.0, 1.0)

            had_focus = any(entry[2] is not None for entry in samples[camera.name])
            if had_focus:
                camera.data.dof.focus_object = None
                # clear only the focus-distance keys (they are re-keyed with
                # the baked values below): wiping the whole camera-data action
                # would also destroy an animated focal length (zoom)
                data_anim = camera.data.animation_data
                data_action = data_anim.action if data_anim else None
                if data_action is not None:
                    for fcurve in iter_action_fcurves(data_action):
                        if fcurve.data_path == 'dof.focus_distance':
                            while fcurve.keyframe_points:
                                fcurve.keyframe_points.remove(
                                    fcurve.keyframe_points[0], fast=True)

            previous_euler = None
            for frame, matrix, focus in samples[camera.name]:
                camera.location = matrix.to_translation()
                euler = (matrix.to_euler('XYZ', previous_euler)
                         if previous_euler else matrix.to_euler('XYZ'))
                previous_euler = euler
                camera.rotation_euler = euler
                camera.keyframe_insert('location', frame=frame)
                camera.keyframe_insert('rotation_euler', frame=frame)
                if focus is not None:
                    camera.data.dof.focus_distance = focus
                    camera.data.dof.keyframe_insert('focus_distance', frame=frame)

        scene.frame_set(original_frame)
        self.report({'INFO'}, f"{len(cameras)} camera(s) baked over {len(frames)} frame(s) - "
                              "rig-free, nulls can now be cleaned")
        return {'FINISHED'}


class OBJECT_OT_apply_all_transforms(bpy.types.Operator):
    """Apply all transformations on selected objects"""
    bl_idname = "object.apply_all_transforms"
    bl_label = "Apply All Transforms"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    def execute(self, context):
        selected_objs = [obj for obj in context.selected_objects if obj.type == 'MESH']

        if not selected_objs:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}

        # transform_apply raises on multi-user mesh data: route those to the
        # instance-safe operators instead of showing a raw traceback
        single_user = [obj for obj in selected_objs
                       if obj.data is None or obj.data.users <= 1]
        shared = [obj for obj in selected_objs if obj not in single_user]

        applied = 0
        if single_user:
            bpy.ops.object.select_all(action='DESELECT')
            for obj in single_user:
                obj.select_set(True)
            context.view_layer.objects.active = single_user[0]
            try:
                bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
                applied = len(single_user)
            except RuntimeError as error:
                self.report({'WARNING'}, f"Apply failed: {str(error)[:120]}")
            for obj in selected_objs:
                obj.select_set(True)

        message = f"Transforms applied on {applied} object(s)"
        if shared:
            message += (f" - {len(shared)} skipped (shared mesh data: use "
                        "Apply Scale/Rotation (Instances Safe))")
        self.report({'INFO' if applied else 'WARNING'}, message)
        return {'FINISHED'}


def is_companion_object(obj):
    """True for objects that belong to a base mesh (UCX_/UBX_/USP_ collisions, _LOD1+)"""
    name = clean_name(obj.name)
    if name.startswith(COLLISION_PREFIXES):
        return True
    return LOD_SUFFIX_PATTERN.search(name) is not None


def find_companion_objects(obj, candidates):
    """
    Find the collision meshes (UCX_/UBX_/USP_) and LOD meshes (_LOD1, _LOD2...)
    that belong to a base object, so they can be exported in the same FBX.

    Matches Unreal conventions: 'UCX_<BaseName>', 'UCX_<BaseName>_01', '<BaseName>_LOD1'.
    """
    base = clean_name(obj.name)
    # Collisions are sometimes named after the prefix-less name (Chair vs SM_Chair)
    base_names = {base}
    if base.startswith('SM_'):
        base_names.add(base[3:])

    companions = []
    for other in candidates:
        if other is obj or other.type != 'MESH':
            continue

        other_name = clean_name(other.name)

        matched = False
        for prefix in COLLISION_PREFIXES:
            if other_name.startswith(prefix):
                rest = other_name[len(prefix):]
                if rest in base_names or any(rest.startswith(b + '_') for b in base_names):
                    matched = True
                break

        if not matched:
            lod_match = LOD_SUFFIX_PATTERN.search(other_name)
            if lod_match and other_name[:lod_match.start()] == base:
                matched = True

        if matched:
            companions.append(other)

    return companions


class OBJECT_OT_batch_fbx_export(bpy.types.Operator):
    """Export each selected object as individual FBX (with its collisions and LODs)"""
    bl_idname = "object.batch_fbx_export"
    bl_label = "Batch FBX Export"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    directory: bpy.props.StringProperty(
        name="Export Directory",
        description="Destination folder for FBX files",
        subtype='DIR_PATH'
    )

    apply_modifiers: bpy.props.BoolProperty(
        name="Apply Modifiers",
        description="Apply modifiers during export",
        default=True
    )

    use_selection: bpy.props.BoolProperty(
        name="Export Selected Only",
        description="Export only selected objects",
        default=True
    )

    use_mesh_edges: bpy.props.BoolProperty(
        name="Export Loose Edges",
        description="Export loose edges",
        default=False
    )

    use_tangent_space: bpy.props.BoolProperty(
        name="Export Tangent Space",
        description="Export tangents (important for Normal Maps)",
        default=True
    )

    export_at_origin: bpy.props.BoolProperty(
        name="Export at Origin",
        description="Temporarily move each object to the world origin so the "
                    "asset pivot is correct in Unreal, then restore its position",
        default=True
    )

    include_companions: bpy.props.BoolProperty(
        name="Include Collisions & LODs",
        description="Export UCX_/UBX_/USP_ collision meshes and _LOD meshes "
                    "inside the same FBX as their base object (Unreal convention)",
        default=True
    )

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        if not self.directory:
            self.report({'WARNING'}, "No folder selected")
            return {'CANCELLED'}

        objects_to_export = context.selected_objects if self.use_selection else [obj for obj in context.scene.objects if obj.type == 'MESH']

        if not objects_to_export:
            self.report({'WARNING'}, "No objects to export")
            return {'CANCELLED'}

        # Save selection state
        original_selection = context.selected_objects[:]
        original_active = context.view_layer.objects.active

        # Deselect all
        bpy.ops.object.select_all(action='DESELECT')

        scene_meshes = [o for o in context.scene.objects if o.type == 'MESH']

        exported_count = 0
        companions_count = 0
        used_filenames = set()
        for obj in objects_to_export:
            if obj.type != 'MESH':
                continue

            # Collisions and LODs are exported alongside their base object,
            # not as separate files
            if self.include_companions and is_companion_object(obj):
                continue

            companions = find_companion_objects(obj, scene_meshes) if self.include_companions else []
            export_group = [obj] + companions

            # Select the whole group
            for member in export_group:
                member.select_set(True)
            context.view_layer.objects.active = obj

            # File name - deduplicated: 'Chair' and 'Chair.001' clean to the
            # same stem and would silently overwrite each other
            stem = clean_name(obj.name)
            candidate = stem
            suffix = 1
            while candidate.lower() in used_filenames:
                suffix += 1
                candidate = f"{stem}_{suffix:02d}"
            used_filenames.add(candidate.lower())
            if candidate != stem:
                self.report({'WARNING'},
                            f"'{obj.name}' exported as {candidate}.fbx (name clash)")
            filename = candidate + ".fbx"
            filepath = os.path.join(self.directory, filename)

            # Temporarily move the group to the world origin (keeps the
            # relative offsets of collisions/LODs) so the pivot is correct
            moved = []
            if self.export_at_origin:
                delta = obj.matrix_world.translation.copy()
                for member in export_group:
                    original_matrix = member.matrix_world.copy()
                    moved.append((member, original_matrix))
                    new_matrix = original_matrix.copy()
                    new_matrix.translation = original_matrix.translation - delta
                    member.matrix_world = new_matrix
                context.view_layer.update()

            try:
                bpy.ops.export_scene.fbx(
                    filepath=filepath,
                    use_selection=True,
                    object_types={'MESH'},
                    use_mesh_modifiers=self.apply_modifiers,
                    mesh_smooth_type='FACE',
                    use_tspace=self.use_tangent_space,
                    use_mesh_edges=self.use_mesh_edges,
                    bake_anim=False,
                    # Optimized settings for Unreal Engine
                    axis_forward='-Z',
                    axis_up='Y',
                    apply_unit_scale=True,
                    apply_scale_options='FBX_SCALE_ALL',
                    use_custom_props=False,
                )
                exported_count += 1
                companions_count += len(companions)
            finally:
                # Always restore positions and selection, even if export fails
                for member, original_matrix in moved:
                    member.matrix_world = original_matrix
                for member in export_group:
                    member.select_set(False)

        # Restore original selection
        bpy.ops.object.select_all(action='DESELECT')
        for obj in original_selection:
            obj.select_set(True)
        context.view_layer.objects.active = original_active

        if companions_count:
            self.report({'INFO'}, f"{exported_count} FBX exported (+{companions_count} collision/LOD meshes embedded)")
        else:
            self.report({'INFO'}, f"{exported_count} object(s) exported as FBX")
        return {'FINISHED'}


classes = (
    OBJECT_OT_validate_for_unreal,
    OBJECT_OT_apply_scale_instances,
    OBJECT_OT_apply_rotation_instances,
    OBJECT_OT_normalize_scene_scale,
    OBJECT_OT_bake_camera_animation,
    OBJECT_OT_apply_all_transforms,
    OBJECT_OT_batch_fbx_export,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
