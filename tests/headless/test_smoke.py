"""Smoke: registration, polls, naming idempotency, collisions, reparenting,
instance-safe applies."""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Harness  # noqa: E402

import bpy  # noqa: E402

t = Harness()
t.check('has_version', bool(getattr(t.module, 'bl_info', {}).get('version')))

t.fresh_scene()

# selection tools are disabled with nothing selected
t.check('poll_disabled_empty',
        bpy.ops.object.normalize_names_quick.poll() is False
        and bpy.ops.object.validate_for_unreal.poll() is False
        and bpy.ops.object.set_origin_preset.poll() is False)

# naming: normalize is idempotent and strips stacked suffixes
bpy.ops.mesh.primitive_cube_add()
cube = bpy.context.active_object
cube.name = 'test cube.001.002'
bpy.ops.object.normalize_names_quick()
first = bpy.data.objects[0].name
bpy.ops.object.normalize_names_quick()
t.check('normalize_idempotent', bpy.data.objects[0].name == first == 'SM_TestCube')

# collision: UCX is created, named, and convex-ish (a hull of a cube is a cube)
bpy.ops.object.select_all(action='SELECT')
bpy.context.view_layer.objects.active = bpy.data.objects[0]
bpy.ops.object.create_collision_mesh(collision_type='UCX')
ucx = [o for o in bpy.data.objects if o.name.startswith('UCX_')]
t.check('ucx_created', len(ucx) == 1)
t.check('ucx_convex_hull_of_cube', ucx and len(ucx[0].data.vertices) == 8)

# remove_empty_parents preserves world position through nested chains
t.fresh_scene()
root = bpy.data.objects.new('root_null', None)
root.location = (10, 0, 0)
bpy.context.collection.objects.link(root)
mid = bpy.data.objects.new('mid_null', None)
mid.parent = root
mid.location = (0, 5, 0)
bpy.context.collection.objects.link(mid)
bpy.ops.mesh.primitive_cube_add(location=(10, 5, 2))
cube = bpy.context.active_object
world_before = cube.matrix_world.translation.copy()
cube.parent = mid
cube.matrix_parent_inverse = mid.matrix_world.inverted()
bpy.context.view_layer.update()
t.deselect_all()
mid.select_set(True)
bpy.context.view_layer.objects.active = mid
bpy.ops.object.remove_empty_parents(apply_transforms=True)
bpy.context.view_layer.update()
t.check('reparent_world_preserved',
        all(abs(a - b) < 1e-4 for a, b in zip(world_before, cube.matrix_world.translation)))

# instance-safe apply rotation: parented instance keeps its world geometry
t.fresh_scene()
parent = bpy.data.objects.new('parent_null', None)
parent.rotation_euler = (0, 0, math.radians(90))
bpy.context.collection.objects.link(parent)
bpy.ops.mesh.primitive_cube_add()
cube = bpy.context.active_object
cube.parent = parent
cube.rotation_euler = (0, 0, math.radians(45))
bpy.context.view_layer.update()


def world_verts(obj):
    return sorted(tuple(round(c, 4) for c in (obj.matrix_world @ v.co))
                  for v in obj.data.vertices)


before = world_verts(cube)
bpy.ops.object.apply_rotation_instances()
bpy.context.view_layer.update()
after = world_verts(cube)
t.check('apply_rotation_world_stable',
        all(all(abs(a[i] - b[i]) < 1e-3 for i in range(3)) for a, b in zip(before, after)))
t.check('apply_rotation_reset', all(abs(v) < 1e-6 for v in cube.rotation_euler))

# instance-safe apply scale: refuses mirrored scale, applies uniform one
t.fresh_scene()
bpy.ops.mesh.primitive_cube_add()
cube = bpy.context.active_object
cube.scale = (2.0, 2.0, 2.0)
bpy.ops.object.apply_scale_instances()
t.check('apply_scale_applied', all(abs(v - 1.0) < 1e-6 for v in cube.scale)
        and abs(max(v.co.x for v in cube.data.vertices) - 2.0) < 1e-4)
cube.scale = (-1.0, 1.0, 1.0)
bpy.ops.object.apply_scale_instances()
t.check('apply_scale_refuses_mirror', abs(cube.scale.x + 1.0) < 1e-6)

t.finish('SMOKE')
