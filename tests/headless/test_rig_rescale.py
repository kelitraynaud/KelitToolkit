"""Rescale Rig + Animation: a rig 100x too big under FBX-style empties ends
at scale 1.0, 100x smaller, with the same motion at every frame."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Harness  # noqa: E402

import bpy  # noqa: E402
import mathutils  # noqa: E402

t = Harness()
scene = t.fresh_scene(1, 20)

root = bpy.data.objects.new('model.fbx', None)
bpy.context.collection.objects.link(root)
node = bpy.data.objects.new('RootNode', None)
node.parent = root
bpy.context.collection.objects.link(node)

arm_data = bpy.data.armatures.new('Skeleton')
armature = bpy.data.objects.new('Skeleton', arm_data)
armature.parent = node
bpy.context.collection.objects.link(armature)
bpy.context.view_layer.objects.active = armature
bpy.ops.object.mode_set(mode='EDIT')
hips = arm_data.edit_bones.new('Hips')
hips.head, hips.tail = (0, 0, 90), (0, 0, 100)
spine = arm_data.edit_bones.new('Spine')
spine.head, spine.tail = (0, 0, 100), (0, 0, 150)
spine.parent = hips
bpy.ops.object.mode_set(mode='OBJECT')

bpy.ops.mesh.primitive_cube_add(size=40, location=(0, 0, 120))
body = bpy.context.active_object
body.name = 'Body'
group = body.vertex_groups.new(name='Spine')
group.add([v.index for v in body.data.vertices], 1.0, 'REPLACE')
body.parent = armature
body.modifiers.new('Armature', 'ARMATURE').object = armature
bpy.context.view_layer.update()

pose_hips = armature.pose.bones['Hips']
pose_hips.location = (0, 0, 0)
pose_hips.keyframe_insert('location', frame=1)
pose_hips.location = (0, 300, 0)        # 3 m of travel in centimetres
pose_hips.keyframe_insert('location', frame=20)
armature.pose.bones['Spine'].rotation_mode = 'XYZ'
armature.pose.bones['Spine'].rotation_euler = (0.4, 0, 0)
armature.pose.bones['Spine'].keyframe_insert('rotation_euler', frame=20)


def evaluated_verts(frame):
    scene.frame_set(frame)
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = body.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    points = sorted(tuple(evaluated.matrix_world @ v.co) for v in mesh.vertices)
    evaluated.to_mesh_clear()
    return points


before = {frame: evaluated_verts(frame) for frame in (1, 10, 20)}
t.deselect_all()
body.select_set(True)
bpy.context.view_layer.objects.active = body
result = bpy.ops.kelit_toolkit.rescale_rig_animation('EXEC_DEFAULT', factor=0.01,
                                                     remove_parent_empties=True)
t.check('finished', list(result) == ['FINISHED'])
after = {frame: evaluated_verts(frame) for frame in (1, 10, 20)}

worst = 0.0
for frame in before:
    for a, b in zip(before[frame], after[frame]):
        worst = max(worst, max(abs(a[i] * 0.01 - b[i]) for i in range(3)))
t.check('motion_scaled_every_frame', worst < 1e-4, round(worst, 7))
t.check('armature_scale_one', all(abs(v - 1) < 1e-6 for v in armature.scale))
t.check('mesh_scale_one', all(abs(v - 1) < 1e-6 for v in body.scale))
t.check('rest_scaled', abs(arm_data.bones['Spine'].length - 0.5) < 1e-4,
        arm_data.bones['Spine'].length)
t.check('empties_removed', armature.parent is None
        and 'RootNode' not in bpy.data.objects and 'model.fbx' not in bpy.data.objects)
t.check('body_kept_parent', body.parent == armature)

# ---- Fix Bone Lengths: a 3 m fingertip shrinks, nothing moves ----
bpy.context.view_layer.objects.active = armature
bpy.ops.object.mode_set(mode='EDIT')
finger = arm_data.edit_bones.new('Finger')
finger.head, finger.tail = (0, 0, 1.5), (0, 0, 4.5)   # 3 m, parent Spine is 0.5 m
finger.parent = arm_data.edit_bones['Spine']
tip = arm_data.edit_bones.new('FingerTip')       # stray end joint, 6 m away, no weights
tip.head, tip.tail = (6, 0, 1.5), (6, 0, 1.6)
tip.parent = finger
bpy.ops.object.mode_set(mode='OBJECT')
tip_pose = armature.pose.bones['FingerTip']
tip_pose.location = (0, 4, 0)                    # animation keeps it out there
tip_pose.keyframe_insert('location', frame=1)
finger_pose = armature.pose.bones['Finger']
finger_pose.rotation_mode = 'XYZ'
finger_pose.rotation_euler = (0.3, 0.2, 0)
finger_pose.keyframe_insert('rotation_euler', frame=10)
rest_before = arm_data.bones['Finger'].matrix_local.copy()
scene.frame_set(10)
pose_before = {b.name: b.matrix.copy() for b in armature.pose.bones}
verts_before = evaluated_verts(10)
t.deselect_all()
armature.select_set(True)
bpy.context.view_layer.objects.active = armature
bpy.ops.kelit_toolkit.fix_bone_lengths('EXEC_DEFAULT', stray_joints=True, drop_stray_keys=True)
scene.frame_set(10)
t.check('finger_shortened', arm_data.bones['Finger'].length < 0.51, arm_data.bones['Finger'].length)
t.check('finger_orientation_kept', all(
    abs(rest_before[i][j] - arm_data.bones['Finger'].matrix_local[i][j]) < 1e-5
    for i in range(4) for j in range(4)))
t.check('pose_unchanged', all(
    all(abs(pose_before[n][i][j] - armature.pose.bones[n].matrix[i][j]) < 1e-5
        for i in range(4) for j in range(4)) for n in pose_before if n != 'FingerTip'))
t.check('skin_unchanged', all(
    max(abs(a[i] - b[i]) for i in range(3)) < 1e-5
    for a, b in zip(verts_before, evaluated_verts(10))))
t.check('stray_tip_brought_back', (arm_data.bones['FingerTip'].head_local
                                  - arm_data.bones['Finger'].head_local).length < 1.0,
        tuple(arm_data.bones['FingerTip'].head_local))
tip_world = armature.matrix_world @ armature.pose.bones['FingerTip'].head
finger_world = armature.matrix_world @ armature.pose.bones['Finger'].head
t.check('stray_tip_stays_home_when_animated', (tip_world - finger_world).length < 1.0,
        round((tip_world - finger_world).length, 3))
t.check('normal_bones_untouched', abs(arm_data.bones['Spine'].length - 0.5) < 1e-4)

t.finish('RIGRESCALE')
