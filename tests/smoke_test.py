"""Kelit Toolkit headless smoke test.

Run with a Blender that has the addon deployed in its addons folder:

    blender -b --python tests/smoke_test.py -- report.json

Covers: registration, poll behavior, naming idempotency, collision creation,
empty-parent removal (world preservation), instance-safe apply, and the
key-reduction module. Exits non-zero on failure.
"""
import bpy
import addon_utils
import json
import sys

ADDON = 'KelitToolkit'

out_path = sys.argv[sys.argv.index('--') + 1] if '--' in sys.argv else 'smoke_report.json'
report = {}
failures = []


def check(name, condition):
    report[name] = bool(condition)
    if not condition:
        failures.append(name)


addon_utils.enable(ADDON, default_set=False, handle_error=None)
module = sys.modules.get(ADDON)
check('registered', module is not None)
check('has_version', bool(getattr(module, 'bl_info', {}).get('version')))

bpy.ops.wm.read_homefile(use_empty=True)

# poll: selection tools disabled with nothing selected
check('poll_disabled_empty',
      bpy.ops.object.normalize_names_quick.poll() is False
      and bpy.ops.object.validate_for_unreal.poll() is False
      and bpy.ops.object.set_origin_preset.poll() is False)

# naming: normalize is idempotent
bpy.ops.mesh.primitive_cube_add()
cube = bpy.context.active_object
cube.name = 'test cube.001.002'
bpy.ops.object.normalize_names_quick()
first = bpy.data.objects[0].name
bpy.ops.object.normalize_names_quick()
check('normalize_idempotent', bpy.data.objects[0].name == first == 'SM_TestCube')

# collision: UCX is created and named
bpy.ops.object.select_all(action='SELECT')
bpy.context.view_layer.objects.active = bpy.data.objects[0]
bpy.ops.object.create_collision_mesh(collision_type='UCX')
check('ucx_created', any(o.name.startswith('UCX_') for o in bpy.data.objects))

# remove_empty_parents preserves world position through nested chains
bpy.ops.wm.read_homefile(use_empty=True)
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
for o in bpy.data.objects:
    o.select_set(False)
mid.select_set(True)
bpy.context.view_layer.objects.active = mid
bpy.ops.object.remove_empty_parents(apply_transforms=True)
bpy.context.view_layer.update()
check('reparent_world_preserved',
      all(abs(a - b) < 1e-4 for a, b in zip(world_before, cube.matrix_world.translation)))

# instance-safe apply rotation: parented instance must NOT double-rotate
bpy.ops.wm.read_homefile(use_empty=True)
import math
parent = bpy.data.objects.new('parent_null', None)
parent.rotation_euler = (0, 0, math.radians(90))
bpy.context.collection.objects.link(parent)
bpy.ops.mesh.primitive_cube_add()
cube = bpy.context.active_object
cube.parent = parent
cube.rotation_euler = (0, 0, math.radians(45))
bpy.context.view_layer.update()
# compare world-space VERTEX positions: the matrix itself is expected to
# change (the rotation moves from the matrix into the geometry)
verts_before = sorted(tuple(round(c, 4) for c in (cube.matrix_world @ v.co))
                      for v in cube.data.vertices)
bpy.ops.object.apply_rotation_instances()
bpy.context.view_layer.update()
verts_after = sorted(tuple(round(c, 4) for c in (cube.matrix_world @ v.co))
                     for v in cube.data.vertices)
check('apply_rotation_world_stable',
      all(all(abs(a[i] - b[i]) < 1e-3 for i in range(3))
          for a, b in zip(verts_before, verts_after)))

# key reduction sanity (module-level, no UE needed)
usd_sync = sys.modules.get(f'{ADDON}.operators.usd_sync')
reduce = usd_sync.reduce_channel_keys
ease = [100.0 * (3 * t * t - 2 * t ** 3) for t in (i / 99 for i in range(100))]
check('reduction_smoothstep_2keys', len(reduce(ease, {0, 99}, 0.1)) == 2)
check('reduction_dense_seed_collapses', len(reduce(ease, set(range(100)), 0.1)) == 2)
check('reduction_const', len(reduce([5.0] * 100, set(), 0.05)) == 2)

report['failures'] = failures
report['ok'] = not failures
with open(out_path, 'w', encoding='utf-8') as handle:
    json.dump(report, handle, indent=1)
print('SMOKE', 'OK' if not failures else 'FAILED: ' + ', '.join(failures))
sys.exit(0 if not failures else 1)
