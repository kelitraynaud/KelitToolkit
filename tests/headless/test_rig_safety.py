"""Fix Bone Lengths and Rescale Rig + Animation leave rig machinery alone:
IK / socket / targeted bones are never moved nor stripped of their keys,
meshes skinned only through a modifier follow the rescale, and a refused rig
is left exactly as it was."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Harness  # noqa: E402

import bpy  # noqa: E402

t = Harness()
export = t.submodule('operators.export')


def new_armature(name, bones, location=(0, 0, 0)):
    """bones: list of (name, head, tail, parent name or None)."""
    data = bpy.data.armatures.new(name)
    obj = bpy.data.objects.new(name, data)
    obj.location = location
    bpy.context.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    for bone_name, head, tail, parent in bones:
        bone = data.edit_bones.new(bone_name)
        bone.head, bone.tail = head, tail
        if parent:
            bone.parent = data.edit_bones[parent]
    bpy.ops.object.mode_set(mode='OBJECT')
    return obj


def location_curves(armature, bone_name):
    path = f'pose.bones["{bone_name}"].location'
    return [fc for action in export.rig_actions(armature)
            for fc in export.iter_action_fcurves(action) if fc.data_path == path]


def skinned_cube(name, armature, location, bone, parent=False):
    bpy.ops.mesh.primitive_cube_add(size=0.4, location=location)
    mesh = bpy.context.active_object
    mesh.name = name
    group = mesh.vertex_groups.new(name=bone)
    group.add([v.index for v in mesh.data.vertices], 1.0, 'REPLACE')
    mesh.modifiers.new('Armature', 'ARMATURE').object = armature
    if parent:
        mesh.parent = armature
    return mesh


def world_points(obj, frame):
    bpy.context.scene.frame_set(frame)
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    points = sorted(tuple(evaluated.matrix_world @ v.co) for v in mesh.vertices)
    evaluated.to_mesh_clear()
    return points


# ---- 1) IK, socket, targeted and bone-parented bones are never "fixed" ----
scene = t.fresh_scene(1, 20)
rig = new_armature('Mannequin', [
    ('root', (0, 0, 0), (0, 0.1, 0), None),
    ('pelvis', (0, 0, 1.0), (0, 0, 1.1), 'root'),
    ('spine', (0, 0, 1.1), (0, 0, 1.4), 'pelvis'),
    ('ik_hand_root', (0, 0, 0), (0, 0.1, 0), 'root'),
    ('ik_hand_gun', (0.6, 0.4, 1.3), (0.6, 0.5, 1.3), 'ik_hand_root'),
    ('weapon_socket', (0.9, 0, 1.4), (0.9, 0.1, 1.4), 'spine'),
    ('Grip', (-0.9, 0, 1.4), (-0.9, 0.1, 1.4), 'spine'),
    ('Mount', (0, 0.9, 1.4), (0, 1.0, 1.4), 'spine'),
    ('Tip', (0, -0.9, 2.4), (0, -0.8, 2.4), 'spine'),
])
body = skinned_cube('Body', rig, (0, 0, 1.2), 'spine', parent=True)
ik = rig.pose.bones['spine'].constraints.new('IK')
ik.target, ik.subtarget = rig, 'Grip'
holder = bpy.data.objects.new('Holster', None)
bpy.context.collection.objects.link(holder)
holder.parent = rig
holder.parent_type = 'BONE'
holder.parent_bone = 'Mount'
gun = rig.pose.bones['ik_hand_gun']
for frame, value in ((1, (0, 3, 0)), (10, (0, 3.5, 0))):   # large, keyed offsets
    gun.location = value
    gun.keyframe_insert('location', frame=frame)
protected = ('ik_hand_gun', 'weapon_socket', 'Grip', 'Mount')
heads_before = {name: rig.data.bones[name].head_local.copy() for name in protected}
lengths_before = {name: rig.data.bones[name].length for name in protected}
t.deselect_all()
rig.select_set(True)
bpy.context.view_layer.objects.active = rig
bpy.ops.kelit_toolkit.fix_bone_lengths('EXEC_DEFAULT')
for name in protected:
    t.check(f'protected_{name}_not_moved',
            (rig.data.bones[name].head_local - heads_before[name]).length < 1e-6,
            tuple(rig.data.bones[name].head_local))
t.check('ik_bone_keys_kept', len(location_curves(rig, 'ik_hand_gun')) == 3)
t.check('ik_target_length_kept', abs(rig.data.bones['Grip'].length - lengths_before['Grip']) < 1e-6)
t.check('plain_stray_tip_still_fixed',
        (rig.data.bones['Tip'].head_local - rig.data.bones['spine'].head_local).length < 1.0,
        tuple(rig.data.bones['Tip'].head_local))
t.check('rescale_fix_lengths_off_by_default',
        bpy.ops.kelit_toolkit.rescale_rig_animation.get_rna_type()
        .properties['fix_lengths'].default is False)

# a stray joint with keys stays put when key deletion is off
bpy.context.view_layer.objects.active = rig
bpy.ops.object.mode_set(mode='EDIT')
loose = rig.data.edit_bones.new('Loose')
loose.head, loose.tail = (0, -3, 1.4), (0, -2.9, 1.4)
loose.parent = rig.data.edit_bones['spine']
bpy.ops.object.mode_set(mode='OBJECT')
rig.pose.bones['Loose'].location = (0, 4, 0)
rig.pose.bones['Loose'].keyframe_insert('location', frame=1)
loose_head = rig.data.bones['Loose'].head_local.copy()
try:
    bpy.ops.kelit_toolkit.fix_bone_lengths('EXEC_DEFAULT', drop_stray_keys=False)
    t.check('keyed_stray_kept_without_drop',
            (rig.data.bones['Loose'].head_local - loose_head).length < 1e-6
            and len(location_curves(rig, 'Loose')) == 3)
except TypeError as error:
    t.check('keyed_stray_kept_without_drop', False, str(error))

# hidden armature: skipped with a message, no exception
rig.hide_set(True)
notes = []
try:
    count = export.fix_bone_lengths(bpy.context, rig, notes=notes)
    t.check('hidden_rig_skipped', count == 0 and notes and 'hidden' in notes[0], notes)
except Exception as error:   # noqa: BLE001
    t.check('hidden_rig_skipped', False, str(error))
    if bpy.context.object is not None and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
t.check('hidden_rig_object_mode', bpy.context.mode == 'OBJECT')
rig.hide_set(False)

# ---- 2) a mesh skinned only through its Armature modifier is rescaled ----
scene = t.fresh_scene(1, 20)
rig = new_armature('Rig', [('Hips', (0, 0, 90), (0, 0, 100), None),
                           ('Spine', (0, 0, 100), (0, 0, 150), 'Hips')], location=(200, 0, 0))
loose_mesh = skinned_cube('Outfit', rig, (200, 0, 120), 'Spine', parent=False)
loose_mesh.scale = (40, 40, 40)
hips = rig.pose.bones['Hips']
hips.location = (0, 0, 0)
hips.keyframe_insert('location', frame=1)
hips.location = (0, 300, 0)
hips.keyframe_insert('location', frame=20)
bpy.context.view_layer.update()
before = {frame: world_points(loose_mesh, frame) for frame in (1, 20)}
t.deselect_all()
loose_mesh.select_set(True)
rig.select_set(True)
bpy.context.view_layer.objects.active = rig
result = bpy.ops.kelit_toolkit.rescale_rig_animation('EXEC_DEFAULT', factor=0.01)
t.check('modifier_only_finished', list(result) == ['FINISHED'])
worst = 0.0
for frame in before:
    for a, b in zip(before[frame], world_points(loose_mesh, frame)):
        worst = max(worst, max(abs(a[i] * 0.01 - b[i]) for i in range(3)))
t.check('modifier_only_mesh_rescaled', worst < 1e-4, round(worst, 6))
t.check('modifier_only_mesh_scale_one', all(abs(v - 1) < 1e-6 for v in loose_mesh.scale))
t.check('active_object_restored', bpy.context.view_layer.objects.active == rig)

# ---- 3) a refused rig is left exactly as it was ----
scene = t.fresh_scene(1, 20)
node = bpy.data.objects.new('RootNode', None)
bpy.context.collection.objects.link(node)
bad = new_armature('Stretched', [('Hips', (0, 0, 90), (0, 0, 100), None)])
bad.parent = node
bad.scale = (1, 2, 1)
bad_mesh = skinned_cube('BadBody', bad, (0, 0, 95), 'Hips', parent=True)
good = new_armature('Fine', [('Hips', (0, 0, 90), (0, 0, 100), None)], location=(5, 0, 0))
shared_action = bpy.data.actions.new('SharedWalk')
for armature in (bad, good):
    armature.animation_data_create()
bad.animation_data.action = shared_action
bad.pose.bones['Hips'].location = (0, 50, 0)
bad.pose.bones['Hips'].keyframe_insert('location', frame=1)
bad.location = (1, 2, 3)
bad.keyframe_insert('location', frame=1)
good.animation_data.action = shared_action
if hasattr(good.animation_data, 'action_slot'):
    good.animation_data.action_slot = bad.animation_data.action_slot
bpy.context.view_layer.update()
keys_before = sorted((fc.data_path, fc.array_index, tuple(tuple(k.co) for k in fc.keyframe_points))
                     for fc in export.iter_action_fcurves(shared_action))
state_before = (tuple(bad.scale), tuple(bad.location), bad.data.bones['Hips'].length,
                [tuple(v.co) for v in bad_mesh.data.vertices], tuple(good.location))
t.deselect_all()
bad.select_set(True)
good.select_set(True)
bpy.context.view_layer.objects.active = good
result = bpy.ops.kelit_toolkit.rescale_rig_animation('EXEC_DEFAULT', factor=0.01,
                                                     remove_parent_empties=True)
keys_after = sorted((fc.data_path, fc.array_index, tuple(tuple(k.co) for k in fc.keyframe_points))
                    for fc in export.iter_action_fcurves(shared_action))
state_after = (tuple(bad.scale), tuple(bad.location), bad.data.bones['Hips'].length,
               [tuple(v.co) for v in bad_mesh.data.vertices], tuple(good.location))
t.check('refused_cancelled', list(result) == ['CANCELLED'])
t.check('refused_rig_untouched', state_after == state_before)
t.check('refused_parent_empty_kept', 'RootNode' in bpy.data.objects and bad.parent == node)
t.check('shared_action_untouched', keys_after == keys_before)

# shared armature data: refused before any change
scene = t.fresh_scene(1, 20)
twin = new_armature('Twin', [('Hips', (0, 0, 90), (0, 0, 100), None)])
copy = bpy.data.objects.new('TwinCopy', twin.data)
bpy.context.collection.objects.link(copy)
t.deselect_all()
twin.select_set(True)
bpy.context.view_layer.objects.active = twin
try:
    result = bpy.ops.kelit_toolkit.rescale_rig_animation('EXEC_DEFAULT', factor=0.01)
except RuntimeError as error:   # it used to fail half-way, after scaling the rig
    result = {'ERROR'}
t.check('shared_armature_data_refused', list(result) == ['CANCELLED']
        and abs(twin.data.bones['Hips'].length - 10) < 1e-6 and tuple(twin.scale) == (1, 1, 1))

t.finish('RIGSAFETY')
