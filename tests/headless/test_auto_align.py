"""Natural axes / canonical signature helper and the Auto Align operator
(Rotate, Bake, Keep, shared meshes)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Harness  # noqa: E402
import _shapes as shapes  # noqa: E402

import bmesh  # noqa: E402
import bpy  # noqa: E402
import mathutils  # noqa: E402

t = Harness()
utils = t.submodule('utils')
random = shapes.rng(11)
expected = sorted(shapes.BOX_SIZE)


def signature_of(mesh):
    return utils.canonical_signature(*utils.mesh_surface_arrays(mesh))


# ---- helper: same shape under random rotations + translations ----
t.fresh_scene()
reference = signature_of(shapes.shape_mesh('ref'))[0]
same = []
for number in range(6):
    matrix = (mathutils.Matrix.Translation((random.uniform(-50, 50), random.uniform(-50, 50),
                                            random.uniform(-50, 50)))
              @ shapes.random_rotation(random))
    same.append(signature_of(shapes.shape_mesh(f'turned_{number}', matrix=matrix))[0] == reference)
quarter = mathutils.Matrix.Rotation(1.5707963267948966, 4, 'Z')
same.append(signature_of(shapes.shape_mesh('quarter', matrix=quarter))[0] == reference)
t.check('signature_rotation_invariant', all(same), same)
other = signature_of(shapes.shape_mesh('other', size=(2.0, 1.2, 0.7)))[0]
t.check('signature_differs_for_other_shape', other != reference)
frame, confidence, method = utils.natural_axes(*utils.mesh_surface_arrays(shapes.shape_mesh('conf')))
t.check('box_confidence_high', confidence > 0.8 and method == 'NORMALS', round(confidence, 3))

# canonical frame is consistent: p - c = R u
points, normals, areas, centers = utils.mesh_surface_arrays(shapes.shape_mesh('rot', matrix=quarter))
signature, rotation, centroid = utils.canonical_signature(points, normals, areas, centers)
import numpy as np  # noqa: E402
rebuilt = sorted(map(tuple, np.rint(((points - centroid) @ rotation) * 1e4).astype(int).tolist()))
t.check('canonical_rotation_consistent', tuple(rebuilt) == signature)
t.check('rotation_proper', abs(np.linalg.det(rotation) - 1.0) < 1e-6)

# PCA fallback on an organic blob (no flat faces): no crash, a rotation
bm = bmesh.new()
bmesh.ops.create_icosphere(bm, subdivisions=3, radius=1.0)
for vertex in bm.verts:
    vertex.co = mathutils.Vector((vertex.co.x * 2.0, vertex.co.y * 1.3, vertex.co.z * 0.7)) \
        * (1.0 + 0.05 * random.random())
blob = bpy.data.meshes.new('blob')
bm.to_mesh(blob)
bm.free()
frame, confidence, method = utils.natural_axes(*utils.mesh_surface_arrays(blob))
t.check('blob_pca_fallback', method == 'PCA' and abs(np.linalg.det(frame) - 1.0) < 1e-6
        and 0.0 <= confidence <= 1.0, (method, round(confidence, 3)))
t.check('blob_signature_no_crash', len(signature_of(blob)[0]) == len(blob.vertices))


# ---- operator ----
def turned_object(name, mesh=None):
    if mesh is None:
        mesh = shapes.shape_mesh(name + '_mesh', matrix=shapes.random_rotation(random))
    matrix = mathutils.Matrix.LocRotScale(
        mathutils.Vector((3.0, -4.0, 5.0)),
        shapes.random_rotation(random).to_3x3(), mathutils.Vector((1.5, 1.5, 1.5)))
    return shapes.link(name, mesh, matrix)


def run(mode, *objects, active=None):
    t.deselect_all()
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = active or objects[0]
    result = bpy.ops.kelit_toolkit.auto_align('EXEC_DEFAULT', mode=mode)
    bpy.context.view_layer.update()
    return result


def axis_aligned(obj, scale=1.5):
    return all(abs(a - b * scale) < 1e-3 for a, b in zip(shapes.extents(shapes.world_points(obj)),
                                                          expected))


t.fresh_scene()
obj = turned_object('Rot')
location = obj.matrix_world.translation.copy()
mesh_before = [v.co.copy() for v in obj.data.vertices]
t.check('rotate_starts_unaligned', not axis_aligned(obj))
run('ROTATE', obj)
t.check('rotate_aligned', axis_aligned(obj), shapes.extents(shapes.world_points(obj)))
t.check('rotate_mesh_untouched', all((a - v.co).length < 1e-6
                                     for a, v in zip(mesh_before, obj.data.vertices)))
t.check('rotate_location_kept', (obj.matrix_world.translation - location).length < 1e-5)

obj = turned_object('Bake')
run('BAKE', obj)
rotation = obj.matrix_world.to_quaternion().to_matrix()
t.check('bake_aligned', axis_aligned(obj))
t.check('bake_rotation_zero', utils.matrices_close(rotation, mathutils.Matrix.Identity(3), 1e-5))

obj = turned_object('Keep')
world_before = shapes.world_points(obj)
run('KEEP', obj)
t.check('keep_world_unchanged', shapes.same_points(world_before, shapes.world_points(obj)))
local = [v.co for v in obj.data.vertices]
t.check('keep_local_axes_realigned', all(abs(a - b) < 1e-3
                                         for a, b in zip(shapes.extents(local), expected)))

# shared mesh, two users, only one selected
shared = shapes.shape_mesh('shared', matrix=shapes.random_rotation(random))
first, second = turned_object('UserA', shared), turned_object('UserB', shared)
second.location.x += 10
bpy.context.view_layer.update()
before_second = shapes.world_points(second)
before_first = shapes.world_points(first)
run('KEEP', first)
t.check('keep_shared_both_unchanged',
        shapes.same_points(before_first, shapes.world_points(first))
        and shapes.same_points(before_second, shapes.world_points(second))
        and first.data == second.data)
run('BAKE', first)
t.check('bake_shared_selected_aligned', axis_aligned(first))
t.check('bake_shared_other_unchanged', shapes.same_points(before_second, shapes.world_points(second))
        and first.data == second.data)

# two selected users needing different rotations: refused, nothing moves
shared = shapes.shape_mesh('shared2', matrix=shapes.random_rotation(random))
third, fourth = turned_object('UserC', shared), turned_object('UserD', shared)
bpy.context.view_layer.update()
third_before, fourth_before = shapes.world_points(third), shapes.world_points(fourth)
result = run('BAKE', third, fourth)
t.check('bake_shared_disagree_refused', result == {'CANCELLED'}
        and shapes.same_points(third_before, shapes.world_points(third))
        and shapes.same_points(fourth_before, shapes.world_points(fourth)))

# a sphere is skipped (no clear axes), nothing crashes
bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=24)
sphere = bpy.context.active_object
sphere_matrix = sphere.matrix_world.copy()
bpy.ops.kelit_toolkit.auto_align('EXEC_DEFAULT', mode='ROTATE', min_confidence=0.99)
t.check('low_confidence_skipped', utils.matrices_close(sphere.matrix_world.to_3x3(),
                                                       sphere_matrix.to_3x3()))

t.finish('AUTOALIGN')
