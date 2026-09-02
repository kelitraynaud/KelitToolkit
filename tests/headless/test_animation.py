"""Animation pipeline: key reduction, signed decomposition, merged camera
sampling, sensor fit, animated focus sampling (no Unreal needed)."""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Harness  # noqa: E402

import bpy  # noqa: E402
import mathutils  # noqa: E402

t = Harness()
usd_sync = t.submodule('operators.usd_sync')

# ---- key reduction ----
reduce = usd_sync.reduce_channel_keys
ease = [100.0 * (3 * x * x - 2 * x ** 3) for x in (i / 99 for i in range(100))]
t.check('reduce_smoothstep_2keys', len(reduce(ease, {0, 99}, 0.1)) == 2)
t.check('reduce_dense_seeds_collapse', len(reduce(ease, set(range(100)), 0.1)) == 2)
t.check('reduce_const', len(reduce([5.0] * 100, set(), 0.05)) == 2)
ramp = [10.0 * i for i in range(100)]
t.check('reduce_ramp_small', len(reduce(ramp, set(), 0.3)) <= 6)

# ---- signed decomposition round-trips a mirrored matrix ----
base = (mathutils.Matrix.Translation((1, 2, 3))
        @ mathutils.Euler((0.3, 0.5, 0.2)).to_matrix().to_4x4()
        @ mathutils.Matrix.Diagonal((-2.0, 1.5, 0.5)).to_4x4())
loc, euler, scale = usd_sync.decompose_signed(base)
rebuilt = (mathutils.Matrix.Translation(loc) @ euler.to_matrix().to_4x4()
           @ mathutils.Matrix.Diagonal(scale).to_4x4())
delta = max(abs(base[i][j] - rebuilt[i][j]) for i in range(4) for j in range(4))
t.check('decompose_signed_roundtrip', delta < 1e-5, round(delta, 7))
t.check('decompose_signed_mirror_kept', scale.x < 0)

# ---- scene: animated null + rigid camera child ----
scene = t.fresh_scene(1, 40)
null = bpy.data.objects.new('cam_null', None)
null.location = (0, -3, 1)
bpy.context.collection.objects.link(null)
null.keyframe_insert('location', frame=1)
null.keyframe_insert('rotation_euler', frame=1)
null.location = (2, -1, 1.5)
null.rotation_euler = (0, 0, 0.8)
null.keyframe_insert('location', frame=40)
null.keyframe_insert('rotation_euler', frame=40)

cam_data = bpy.data.cameras.new('ShotCam')
camera = bpy.data.objects.new('ShotCam', cam_data)
camera.parent = null
camera.location = (0, -4, 1.2)
camera.rotation_euler = (1.2, 0, 0)
bpy.context.collection.objects.link(camera)
scene.camera = camera
cam_data.dof.use_dof = True
cam_data.dof.focus_distance = 2.0
cam_data.dof.keyframe_insert('focus_distance', frame=1)
cam_data.dof.focus_distance = 8.0
cam_data.dof.keyframe_insert('focus_distance', frame=40)
bpy.context.view_layer.update()

staged = {'cam_null'}
# path A: standalone camera sampling
payload_a = usd_sync.build_camera_payload(bpy.context, camera, 1, 40, staged)
# path B: merged with the object bake
sampler = usd_sync.CameraSampler(camera, staged)
tracks = usd_sync.bake_world_animation(bpy.context, [null], 1, 40,
                                       relative_to_parent=True, key_mode='AUTHORED',
                                       camera_sampler=sampler)
payload_b = usd_sync.build_camera_payload(bpy.context, camera, 1, 40, staged,
                                          sampler=sampler)
identical = all(payload_a[f] == payload_b[f] for f in (
    'locations', 'forwards', 'ups', 'parent_locations', 'parent_rotations',
    'parent_scales', 'parent', 'key_frames', 'focal_length'))
t.check('camera_paths_identical', identical)

max_delta = 0.0
for index, frame in enumerate(range(1, 41)):
    scene.frame_set(frame)
    v = camera.evaluated_get(bpy.context.evaluated_depsgraph_get()).matrix_world.translation
    truth = [v.x * 100.0, -v.y * 100.0, v.z * 100.0]
    for a, b in zip(truth, payload_b['locations'][index]):
        max_delta = max(max_delta, abs(a - b))
t.check('camera_locations_exact', max_delta < 0.01, round(max_delta, 5))
t.check('null_track_reduced', 0 < max(len(p) for p in tracks['cam_null']['keys'].values()) <= 10)

# animated focus sampled per frame, first frame value used as static focus
distances = payload_b['focus'].get('distances_cm') or []
t.check('focus_curve_sampled', len(distances) == 40)
t.check('focus_curve_values', distances and abs(distances[0] - 200.0) < 1.0
        and abs(distances[-1] - 800.0) < 1.0)
t.check('focus_static_is_first_frame', abs(payload_b['focus']['distance_cm'] - 200.0) < 1.0)

# ---- sensor fit: AUTO + portrait drives the vertical side ----
scene.render.resolution_x, scene.render.resolution_y = 1080, 1920
cam_data.sensor_fit = 'AUTO'
cam_data.sensor_width = 36.0
sampler2 = usd_sync.CameraSampler(camera, staged)
scene.frame_set(1)
sampler2.sample(bpy.context.evaluated_depsgraph_get())
payload_c = usd_sync.build_camera_payload(bpy.context, camera, 1, 1, staged, sampler=sampler2)
t.check('sensor_fit_auto_portrait',
        abs(payload_c['sensor_height'] - 36.0) < 1e-3
        and payload_c['sensor_width'] < payload_c['sensor_height'])
cam_data.sensor_fit = 'VERTICAL'
cam_data.sensor_height = 24.0
payload_d = usd_sync.build_camera_payload(bpy.context, camera, 1, 1, staged, sampler=sampler2)
t.check('sensor_fit_vertical', abs(payload_d['sensor_height'] - 24.0) < 1e-3)

# ---- mirrored object flows into the graph ----
bpy.ops.mesh.primitive_cube_add()
cube = bpy.context.active_object
cube.name = 'MirroredCube'
cube.scale = (-1.0, 1.0, 1.0)
bpy.context.view_layer.update()
graph = usd_sync.build_scene_graph([cube])
t.check('graph_keeps_mirror', graph[0]['scale'][0] < 0)

# ---- focus object off-axis: distance along the view axis, like Blender ----
scene = t.fresh_scene()
cam_data = bpy.data.cameras.new('FocusCam')
camera = bpy.data.objects.new('FocusCam', cam_data)
bpy.context.collection.objects.link(camera)
camera.rotation_euler = (math.radians(90), 0, 0)   # looks down +Y
target = bpy.data.objects.new('FocusTarget', None)
bpy.context.collection.objects.link(target)
target.location = (10 * math.sin(math.radians(30)), 10 * math.cos(math.radians(30)), 0)
cam_data.dof.use_dof = True
cam_data.dof.focus_object = target
bpy.context.view_layer.update()
sampler = usd_sync.CameraSampler(camera, set())
sampler.sample(bpy.context.evaluated_depsgraph_get())
t.check('focus_projected_on_view_axis',
        bool(sampler.focus_distances) and abs(sampler.focus_distances[0] - 866.025) < 0.5,
        sampler.focus_distances[:1])
payload = usd_sync.build_camera_payload(bpy.context, camera, 1, 1, set())
t.check('focus_static_projected', abs(payload['focus']['distance_cm'] - 866.025) < 0.5)

# ---- export context managers on a mesh-under-mesh skeletal hierarchy ----
scene = t.fresh_scene()
unified_export = t.submodule('operators.unified_export')
armature = bpy.data.objects.new('Rig', bpy.data.armatures.new('Rig'))
armature.location = (5, 0, 0)
armature.rotation_euler = (0, 0, 1.0)
bpy.context.collection.objects.link(armature)
bpy.context.view_layer.update()
bpy.ops.mesh.primitive_cube_add(location=(5, 2, 0))
head = bpy.context.active_object
head.name = 'Head'
head.parent = armature
head.matrix_parent_inverse = armature.matrix_world.inverted()
bpy.context.view_layer.update()
bpy.ops.mesh.primitive_cube_add(location=(5, 2, 1))
hair = bpy.context.active_object
hair.name = 'Hair'
hair.parent = head
hair.matrix_parent_inverse = head.matrix_world.inverted()
bpy.context.view_layer.update()
bpy.ops.mesh.primitive_cube_add(location=(0, 0, 3))
loose = bpy.context.active_object
loose.name = 'Loose'   # deformed but not parented: re-based by hand
bpy.context.view_layer.update()
members = (armature, head, hair, loose)
originals = {o.name: o.matrix_world.copy() for o in members}
# Hair listed before Head: the order that used to move Hair twice
asset = {'type': 'SKELETAL', 'name': 'Rig', 'root': armature,
         'objects': [armature, hair, head, loose]}


def close(a, b):
    return all(abs(a[i][j] - b[i][j]) < 1e-5 for i in range(4) for j in range(4))


root_inverse = armature.matrix_world.inverted()
with unified_export.at_neutral_root(asset):
    bpy.context.view_layer.update()
    inside = {o.name: o.matrix_world.copy() for o in members}
bpy.context.view_layer.update()
t.check('neutral_root_identity', close(inside['Rig'], mathutils.Matrix.Identity(4)))
t.check('neutral_root_members_follow', all(
    close(inside[name], root_inverse @ originals[name]) for name in ('Head', 'Hair', 'Loose')))
t.check('neutral_root_restored', all(close(o.matrix_world, originals[o.name]) for o in members))

delta = armature.matrix_world.translation.copy()
with unified_export.at_world_origin(asset):
    bpy.context.view_layer.update()
    inside = {o.name: o.matrix_world.copy() for o in members}
bpy.context.view_layer.update()
t.check('world_origin_shift', all(
    (inside[name].translation - (originals[name].translation - delta)).length < 1e-5
    for name in ('Rig', 'Head', 'Hair', 'Loose')))
t.check('world_origin_restored', all(close(o.matrix_world, originals[o.name]) for o in members))

t.finish('ANIMATION')
