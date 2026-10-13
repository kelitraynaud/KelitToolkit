"""Auto Align: find the natural axes of a mesh (its flat faces, or its
principal axes for organic shapes) and line them up with the world axes.

- Rotate: the object turns, the mesh is untouched.
- Bake: the object ends aligned like Rotate, the rotation is written into
  the mesh and the object rotation is zero.
- Keep: nothing moves in the world, the mesh's local axes are realigned
  (mesh rotated one way, object rotated back).

Shared meshes are handled for every user: users left out of the selection
keep their world look, and a mesh whose selected users would need
different rotations is refused.
"""

import bpy
import mathutils

from ..utils import (cube_rotations, mesh_surface_arrays, mesh_users,
                     natural_axes, numpy_to_matrix3)

_IDENTITY_TOLERANCE = 1e-6


def mesh_natural_frame(mesh):
    """(frame, confidence, method) of a mesh, in mesh space."""
    points, normals, areas, centers = mesh_surface_arrays(mesh)
    return natural_axes(points, normals, areas, centers)


def closest_equivalent_frame(frame, reference):
    """Among the 24 frames equivalent to `frame` (columns = natural axes),
    the one G whose inverse G^T is closest to the rotation `reference`
    (maximum trace of G @ reference). Numpy arrays."""
    best = None
    best_score = None
    for cube in cube_rotations():
        candidate = frame @ cube.T
        score = float((candidate @ reference).trace())
        if best_score is None or score > best_score + 1e-9:
            best, best_score = candidate, score
    return best


def _uniform_scale(scale):
    values = [abs(component) for component in scale]
    return max(values) - min(values) <= 1e-5 * max(max(values), 1e-12)


def _world_rotation(obj):
    import numpy as np
    _location, rotation, _scale = obj.matrix_world.decompose()
    return np.array(rotation.to_matrix())


def _store_children(objs):
    return {child: child.matrix_world.copy() for obj in objs for child in obj.children}


def _restore_children(context, saved):
    if not saved:
        return
    context.view_layer.update()
    for child, matrix in saved.items():
        child.matrix_world = matrix


class OBJECT_OT_auto_align(bpy.types.Operator):
    """Line up the selected meshes with the world axes from their flat faces: turn the object, bake the turn into the mesh, or keep it in place and realign its local axes"""
    bl_idname = "kelit_toolkit.auto_align"
    bl_label = "Auto Align"
    bl_options = {'REGISTER', 'UNDO'}

    mode: bpy.props.EnumProperty(
        name="Mode",
        items=[
            ('ROTATE', "Rotate Object",
             "Turn the object so its natural axes follow the world axes (mesh untouched)"),
            ('BAKE', "Bake into Mesh",
             "Align like Rotate, then write the rotation into the mesh (object rotation zero)"),
            ('KEEP', "Keep in Place",
             "Nothing moves: the mesh's local axes are realigned on its natural axes"),
        ],
        default='ROTATE'
    )
    min_confidence: bpy.props.FloatProperty(
        name="Min Confidence",
        description="Objects whose axes are less clear than this are left alone "
                    "(round or organic shapes)",
        default=0.3, min=0.0, max=1.0
    )

    @classmethod
    def poll(cls, context):
        return (context.mode == 'OBJECT'
                and any(obj.type == 'MESH' for obj in context.selected_objects))

    def execute(self, context):
        import numpy as np
        objects = [obj for obj in context.selected_objects
                   if obj.type == 'MESH' and obj.data is not None]
        frames = {}
        skipped = []
        ready = []
        for obj in objects:
            mesh = obj.data
            if mesh not in frames:
                frames[mesh] = mesh_natural_frame(mesh) if len(mesh.polygons) or len(mesh.vertices) else None
            analysis = frames[mesh]
            if analysis is None:
                skipped.append(f"{obj.name}: empty mesh")
                continue
            frame, confidence, method = analysis
            if confidence < self.min_confidence:
                skipped.append(f"{obj.name}: no clear axes (confidence {confidence:.2f}), skipped")
                continue
            if not _uniform_scale(obj.matrix_world.to_scale()):
                skipped.append(f"{obj.name}: non-uniform scale, apply the scale first")
                continue
            ready.append((obj, frame))

        done = 0
        if self.mode == 'ROTATE':
            for obj, frame in ready:
                location, _rotation, scale = obj.matrix_world.decompose()
                aligned = closest_equivalent_frame(frame, _world_rotation(obj)).T
                obj.matrix_world = mathutils.Matrix.LocRotScale(
                    location, numpy_to_matrix3(aligned), scale)
                done += 1
        else:
            by_mesh = {}
            for obj, frame in ready:
                by_mesh.setdefault(obj.data, []).append((obj, frame))
            for mesh, members in by_mesh.items():
                count, reason = self._align_mesh(context, mesh, members, np)
                if reason:
                    skipped.append(reason)
                done += count

        for line in skipped:
            self.report({'WARNING'}, line)
        labels = {'ROTATE': "rotated", 'BAKE': "aligned, rotation baked into the mesh",
                  'KEEP': "local axes realigned, nothing moved"}
        message = f"Auto Align: {done} object(s) {labels[self.mode]}"
        if skipped:
            message += f", {len(skipped)} skipped"
        if done == 0:
            self.report({'WARNING'}, message)
            return {'CANCELLED'}
        self.report({'INFO'}, message)
        return {'FINISHED'}

    def _align_mesh(self, context, mesh, members, np):
        """Bake or Keep for every selected user of one mesh. Returns
        (aligned object count, refusal line or None)."""
        frame = members[0][1]
        if mesh.library is not None:
            return 0, f"{mesh.name}: linked mesh, cannot be edited"
        users = mesh_users(mesh)
        if any(not _uniform_scale(user.matrix_world.to_scale()) for user in users):
            return 0, f"{mesh.name}: a user of this shared mesh has a non-uniform scale"
        selected = [obj for obj, _frame in members]
        others = [user for user in users if user not in selected]
        if self.mode == 'KEEP':
            change = closest_equivalent_frame(frame, np.identity(3))
        else:
            choices = [closest_equivalent_frame(frame, _world_rotation(obj)) for obj in selected]
            change = choices[0]
            if any(not np.allclose(choice, change, atol=1e-5) for choice in choices[1:]):
                return 0, (f"{mesh.name}: its {len(selected)} selected users need different "
                           "rotations, select one or use Rotate Object")
        if np.allclose(change, np.identity(3), atol=_IDENTITY_TOLERANCE) and self.mode == 'KEEP':
            return len(selected), None
        change_matrix = numpy_to_matrix3(change)
        saved_children = _store_children(users)
        # mesh: p' = G^T p (handles shape keys and custom normals)
        mesh.transform(change_matrix.transposed().to_4x4(), shape_keys=True)
        mesh.update()
        compensated = users if self.mode == 'KEEP' else others
        for user in compensated:
            user.matrix_world = user.matrix_world @ change_matrix.to_4x4()
        if self.mode == 'BAKE':
            for obj in selected:
                location, _rotation, scale = obj.matrix_world.decompose()
                obj.matrix_world = mathutils.Matrix.LocRotScale(
                    location, mathutils.Matrix.Identity(3), scale)
        _restore_children(context, saved_children)
        return len(selected), None


classes = (
    OBJECT_OT_auto_align,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
