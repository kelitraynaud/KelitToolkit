"""Fixes from the audit of a 2 362-object production scene: instanced vs
unique geometry, scans that froze on large files and network drives,
empties a rig needs, Rename Cameras, Normalize Names leaving rigs alone."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Harness  # noqa: E402

import bpy  # noqa: E402

t = Harness()
weight = t.submodule('operators.scene_weight')
materials_mod = t.submodule('operators.materials')
utils = t.submodule('utils')
cleanup = t.submodule('operators.scene_cleanup')
naming = t.submodule('operators.naming')
cameras = t.submodule('operators.camera_names')

# ---- 1. grass in particles: the source counts once in unique ----
scene = t.fresh_scene()
grass_col = bpy.data.collections.new('Grass')
scene.collection.children.link(grass_col)
bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16)   # 960 tris
tuft = bpy.context.active_object
tuft.name = 'Tuft'
for collection in list(tuft.users_collection):
    collection.objects.unlink(tuft)
grass_col.objects.link(tuft)
bpy.context.view_layer.layer_collection.children['Grass'].exclude = True
tuft_tris = 960
grounds = []
for index in range(3):
    bpy.ops.mesh.primitive_plane_add(location=(index * 3, 0, 0))   # 2 tris
    ground = bpy.context.active_object
    ground.name = f'Ground{index}'
    ground.modifiers.new('Grass', 'PARTICLE_SYSTEM')
    settings = ground.particle_systems[0].settings
    settings.type = 'HAIR'
    settings.count = 100
    settings.render_type = 'COLLECTION'
    settings.instance_collection = grass_col
    grounds.append(ground)
bpy.context.view_layer.update()
data = weight.compute_scene_weight(bpy.context)
t.check('grass_displayed', data['displayed'] == 3 * (2 + 100 * tuft_tris), data['displayed'])
t.check('grass_instanced', data['instanced'] == 3 * 100 * tuft_tris, data['instanced'])
# each ground is its own object (particle modifier); the tuft counts once
t.check('grass_unique_source_once', data['unique'] == 3 * 2 + tuft_tris, data['unique'])
calls = []
weight.compute_scene_weight(bpy.context, progress=lambda done, total: calls.append(total))
t.check('weight_progress_called', calls and calls[0] == 3, calls)

# ---- 2. folder index, missing files, scans reused by the dialogs ----
folder = tempfile.mkdtemp(prefix='kelit_audit_')
path = os.path.join(folder, 'Wall.png')
with open(path, 'wb') as handle:
    handle.write(b'x' * 1234)
index = utils.FolderIndex()
t.check('folder_size', index.size(path) == 1234, index.size(path))
t.check('folder_case_insensitive', index.size(os.path.join(folder, 'wall.PNG')) == 1234)
t.check('folder_missing_file', index.size(os.path.join(folder, 'nope.png')) is None)
t.check('folder_missing_folder', index.size(os.path.join(folder, 'gone', 'a.png')) is None)
t.check('folder_listed_once', len(index.folders) == 2, list(index.folders))

t.fresh_scene()
missing = bpy.data.images.load(path, check_existing=False)
missing.filepath = os.path.join(folder, 'missing_on_network.png')
missing.use_fake_user = True
rows = weight.texture_weights()
t.check('missing_image_skipped', all(row[0] != missing.name for row in rows))
t.check('missing_image_not_loaded', not missing.has_data)

for name in ('M_Paint', 'M_Paint.001'):
    material = bpy.data.materials.new(name)
    material.use_fake_user = True
groups = materials_mod.find_duplicate_materials(bpy.context)
t.check('materials_scan_with_progress', len(groups) == 1 and len(groups[0]) == 2,
        [[m.name for m in g] for g in groups])
materials_mod.remember_scan('materials', bpy.data.materials, [['M_Paint', 'M_Paint.001']])
t.check('scan_recalled', materials_mod.recall_scan('materials', bpy.data.materials)
        == [['M_Paint', 'M_Paint.001']])
bpy.data.materials.new('M_Other').use_fake_user = True
t.check('scan_invalidated_by_change',
        materials_mod.recall_scan('materials', bpy.data.materials) is None)
result = bpy.ops.kelit_toolkit.merge_duplicate_materials('EXEC_DEFAULT')
t.check('merge_still_works', list(result) == ['FINISHED']
        and 'M_Paint.001' not in bpy.data.materials)

# ---- 3. Delete Unused Empties keeps what a rig or a modifier needs ----
scene = t.fresh_scene()


def empty(name, parent=None):
    obj = bpy.data.objects.new(name, None)
    scene.collection.objects.link(obj)
    obj.parent = parent
    return obj


rig_data = bpy.data.armatures.new('RigData')
rig = bpy.data.objects.new('Rig', rig_data)
scene.collection.objects.link(rig)
bpy.context.view_layer.objects.active = rig
bpy.ops.object.mode_set(mode='EDIT')
bone = rig_data.edit_bones.new('hand')
bone.tail = (0, 0, 1)
bpy.ops.object.mode_set(mode='OBJECT')
ik_target = empty('yvick_constraint_IK')
widget = empty('WGT_hand')
pose_bone = rig.pose.bones['hand']
pose_bone.custom_shape = widget
pose_bone.constraints.new('IK').target = ik_target
on_bone = empty('hand_attach')
on_bone.parent = rig
on_bone.parent_type = 'BONE'
on_bone.parent_bone = 'hand'

bpy.ops.mesh.primitive_cube_add()
cube = bpy.context.active_object
cube.name = 'Box'
hook_target = empty('HookTarget')
cube.modifiers.new('Hook', 'HOOK').object = hook_target
offset = empty('ArrayOffset')
array = cube.modifiers.new('Array', 'ARRAY')
array.use_object_offset = True
array.offset_object = offset
driver_target = empty('DriverTarget')
cube['amount'] = 0.0
fcurve = cube.driver_add('["amount"]')
variable = fcurve.driver.variables.new()
variable.targets[0].id = driver_target
fcurve.driver.expression = variable.name
material = bpy.data.materials.new('M_Projected')
cube.data.materials.append(material)
coords = material.node_tree.nodes.new('ShaderNodeTexCoord')
projector = empty('Projector')
coords.object = projector

library = bpy.data.collections.new('PropLibrary')
scene.collection.children.link(library)
member = bpy.data.objects.new('LibraryAnchor', None)
library.objects.link(member)
instancer = empty('PropInstancer')
instancer.instance_type = 'COLLECTION'
instancer.instance_collection = library

free_a = empty('Null_free_a')
free_b = empty('Null_free_b')
needed = ['yvick_constraint_IK', 'WGT_hand', 'hand_attach', 'HookTarget', 'ArrayOffset',
          'DriverTarget', 'Projector', 'PropInstancer', 'LibraryAnchor']
bpy.context.view_layer.update()
references = cleanup.empty_references(scene)
t.check('references_found', all(name in references for name in needed),
        {name: references.get(name) for name in needed})
t.check('free_not_referenced', 'Null_free_a' not in references and 'Null_free_b' not in references,
        references)
count = len(bpy.data.objects)
bpy.ops.kelit_toolkit.delete_unused_empties('EXEC_DEFAULT', dry_run=True, organization='FLAT')
t.check('dry_run_changes_nothing', len(bpy.data.objects) == count)
lines = [line.text for line in bpy.context.window_manager.kelit_report_lines]
t.check('dry_run_lists', any('Null_free_a' in text for text in lines)
        and any('yvick_constraint_IK' in text and 'bone' in text for text in lines), lines[:12])
bpy.ops.kelit_toolkit.delete_unused_empties('EXEC_DEFAULT', organization='FLAT')
t.check('free_empties_deleted', 'Null_free_a' not in bpy.data.objects
        and 'Null_free_b' not in bpy.data.objects)
t.check('needed_empties_kept', all(name in bpy.data.objects for name in needed),
        [name for name in needed if name not in bpy.data.objects])
t.check('rig_still_targets', pose_bone.constraints[0].target is not None
        and pose_bone.custom_shape is not None)

# ---- 4. Rename Cameras ----
scene = t.fresh_scene()


def camera(name, lens):
    data = bpy.data.cameras.new(name + '_data')
    data.lens = lens
    obj = bpy.data.objects.new(name, data)
    scene.collection.objects.link(obj)
    return obj


wide = camera('CAM_wide_old', 50.0)
close = camera('Camera.003', 35.0)
marker = scene.timeline_markers.new('shot_A', frame=10)
marker.camera = wide
close.data.lens = 35.0
close.data.keyframe_insert('lens', frame=1)
close.data.lens = 85.0
close.data.keyframe_insert('lens', frame=20)
scene.frame_set(1)
t.check('lens_animated', cameras.lens_is_animated(close.data)
        and not cameras.lens_is_animated(wide.data))
t.check('name_format', cameras.camera_name(1, 'large', 3, 'B', 50.0) == 'Cam_01_Large_V3B_50mm'
        and cameras.camera_name(2, 'close up', 1, 'NONE', 35.0) == 'Cam_02_CloseUp_V1_35mm')
items = [{'name': '', 'camera': 'CAM_wide_old', 'number': 1, 'shot': 'Large', 'version': 3, 'variant': 'B'},
         {'name': '', 'camera': 'Camera.003', 'number': 2, 'shot': 'CloseUp', 'version': 1,
          'variant': 'NONE'}]
result = bpy.ops.kelit_toolkit.rename_cameras('EXEC_DEFAULT', items=items)
t.check('cameras_renamed', list(result) == ['FINISHED']
        and wide.name == 'Cam_01_Large_V3B_50mm' and close.name == 'Cam_02_CloseUp_V1_35mm',
        (wide.name, close.name))
t.check('camera_data_renamed', wide.data.name == wide.name and close.data.name == close.name)
t.check('marker_still_bound', scene.timeline_markers['shot_A'].camera == wide)
lines = [line.text for line in bpy.context.window_manager.kelit_report_lines]
t.check('report_marker_and_animated', any('1 marker' in text for text in lines)
        and any('animated' in text for text in lines), lines)
result = bpy.ops.kelit_toolkit.rename_cameras(
    'EXEC_DEFAULT',
    items=[{'name': '', 'camera': wide.name, 'number': 2, 'shot': 'CloseUp', 'version': 1, 'variant': 'NONE'},
           {'name': '', 'camera': close.name, 'number': 1, 'shot': 'Large', 'version': 3, 'variant': 'B'}])
t.check('cameras_can_swap', wide.name.startswith('Cam_02_CloseUp_V1_')
        and close.name.startswith('Cam_01_Large_V3B_'), (wide.name, close.name))
wide.data.lens = 35.0   # same lens, same fields: same name
refused = False
try:
    bpy.ops.kelit_toolkit.rename_cameras(
        'EXEC_DEFAULT',
        items=[{'name': '', 'camera': wide.name, 'number': 5, 'shot': 'Large', 'version': 1,
                'variant': 'NONE'},
               {'name': '', 'camera': close.name, 'number': 5, 'shot': 'Large', 'version': 1,
                'variant': 'NONE'}])
except RuntimeError as error:      # an ERROR report raises in scripts
    refused = 'same name' in str(error)
t.check('duplicate_names_refused', refused and wide.name.startswith('Cam_02')
        and close.name.startswith('Cam_01'), (wide.name, close.name))

# swapping two cameras with the same lens: the data names follow too
scene = t.fresh_scene()
swap_a = camera('Cam_01_Large_V1_50mm', 50.0)
swap_b = camera('Cam_02_Large_V1_50mm', 50.0)
swap_a.data.name, swap_b.data.name = swap_a.name, swap_b.name
bpy.ops.kelit_toolkit.rename_cameras(
    'EXEC_DEFAULT',
    items=[{'name': '', 'camera': swap_a.name, 'number': 2, 'shot': 'Large', 'version': 1,
            'variant': 'NONE'},
           {'name': '', 'camera': swap_b.name, 'number': 1, 'shot': 'Large', 'version': 1,
            'variant': 'NONE'}])
t.check('swap_data_names_follow', swap_a.name == 'Cam_02_Large_V1_50mm'
        and swap_a.data.name == swap_a.name and swap_b.data.name == swap_b.name == 'Cam_01_Large_V1_50mm',
        (swap_a.name, swap_a.data.name, swap_b.name, swap_b.data.name))

# ---- 5. Normalize Names leaves rigs alone ----
scene = t.fresh_scene()
rig_data = bpy.data.armatures.new('body_rig_data')
rig = bpy.data.objects.new('body_rig', rig_data)
scene.collection.objects.link(rig)
bpy.context.view_layer.objects.active = rig
bpy.ops.object.mode_set(mode='EDIT')
rig_data.edit_bones.new('spine').tail = (0, 0, 1)
bpy.ops.object.mode_set(mode='OBJECT')
widget = bpy.data.objects.new('WGT-spine', None)
scene.collection.objects.link(widget)
target = bpy.data.objects.new('ctrl target', None)
scene.collection.objects.link(target)
rig.pose.bones['spine'].custom_shape = widget
rig.pose.bones['spine'].constraints.new('COPY_LOCATION').target = target
bpy.ops.mesh.primitive_cube_add()
chair = bpy.context.active_object
chair.name = 'my chair'
for obj in scene.objects:
    obj.select_set(True)
bpy.context.view_layer.objects.active = chair
bpy.ops.kelit_toolkit.normalize_names_quick('EXEC_DEFAULT')
t.check('rig_names_kept', {rig.name, widget.name, target.name} == {'body_rig', 'WGT-spine', 'ctrl target'},
        (rig.name, widget.name, target.name))
t.check('mesh_still_normalized', chair.name != 'my chair', chair.name)
bpy.ops.kelit_toolkit.normalize_names_quick('EXEC_DEFAULT', skip_rigs=False)
t.check('rig_renamed_when_asked', widget.name != 'WGT-spine' or target.name != 'ctrl target',
        (widget.name, target.name))

# ---- 6. Merge Duplicate Materials / Images never rewire a rig ----
scene = t.fresh_scene()


def head_shader(name, rig_empty):
    """Same nodes for every copy, a driver reading the copy's own rig."""
    material = bpy.data.materials.new(name)
    material.use_fake_user = True
    principled = next(n for n in material.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
    fcurve = principled.inputs['Roughness'].driver_add('default_value')
    variable = fcurve.driver.variables.new()
    variable.targets[0].id = rig_empty
    variable.targets[0].data_path = 'location.x'
    fcurve.driver.expression = variable.name
    return material


native_a = bpy.data.objects.new('head_native', None)
native_b = bpy.data.objects.new('head_native.001', None)
for obj in (native_a, native_b):
    scene.collection.objects.link(obj)
shader_a = head_shader('head_shader', native_a)
shader_b = head_shader('head_shader.001', native_b)
t.check('driver_targets_in_fingerprint',
        utils.material_fingerprint(shader_a) != utils.material_fingerprint(shader_b))
groups, animated, blocked = materials_mod.safe_material_groups()
t.check('driven_left_out_by_default', not groups and {shader_a, shader_b} <= set(animated),
        ([[m.name for m in g] for g in groups], [m.name for m in animated]))
groups, animated, blocked = materials_mod.safe_material_groups(include_animated=True)
t.check('different_rigs_never_merged', not any(shader_a in g and shader_b in g for g in groups),
        [[m.name for m in g] for g in groups])
shader_c = head_shader('head_shader.002', native_a)          # same rig as shader_a
groups, _animated, _blocked = materials_mod.safe_material_groups(include_animated=True)
t.check('same_rig_merges_when_asked', any(shader_a in g and shader_c in g for g in groups),
        [[m.name for m in g] for g in groups])
result = bpy.ops.kelit_toolkit.merge_duplicate_materials('EXEC_DEFAULT')
t.check('default_merge_touches_no_driven', all(name in bpy.data.materials for name in
                                               ('head_shader', 'head_shader.001', 'head_shader.002')))

plain_a = bpy.data.materials.new('M_Plain')
plain_b = bpy.data.materials.new('M_Plain.001')
for material in (plain_a, plain_b):
    material.use_fake_user = True
bpy.ops.mesh.primitive_cube_add()
cube = bpy.context.active_object
cube.data.materials.append(plain_b)
dna = bpy.data.objects.new('CharacterDNA', None)
scene.collection.objects.link(dna)
dna['targets'] = [{'owner': plain_b, 'path': 'nodes["logic"]'}, {'owner': None, 'path': ''}]
t.check('idprop_holds_material', materials_mod._custom_property_holds(dna, plain_b))
groups, _animated, blocked = materials_mod.safe_material_groups()
t.check('idprop_reference_blocks_merge', 'M_Plain.001' in blocked
        and not any(plain_b in g for g in groups), blocked)
bpy.ops.kelit_toolkit.merge_duplicate_materials('EXEC_DEFAULT')
t.check('idprop_copy_survives', 'M_Plain.001' in bpy.data.materials
        and dna['targets'][0]['owner'] == plain_b)
lines = [line.text for line in bpy.context.window_manager.kelit_report_lines]
t.check('blocked_reported', any('M_Plain.001' in text and 'custom property' in text
                                for text in lines), lines)
dna['targets'] = []                                          # reference gone: merges
bpy.ops.kelit_toolkit.merge_duplicate_materials('EXEC_DEFAULT')
t.check('unreferenced_copy_merges', 'M_Plain.001' not in bpy.data.materials
        and cube.data.materials[0] == plain_a)

t.fresh_scene()
image_path = os.path.join(folder, 'skin.png')
source = bpy.data.images.new('skin_src', 8, 8)
source.filepath_raw = image_path
source.file_format = 'PNG'
source.save()
bpy.data.images.remove(source)
image_a = bpy.data.images.load(image_path, check_existing=False)
image_b = bpy.data.images.load(image_path, check_existing=False)
for image in (image_a, image_b):
    image.use_fake_user = True
holder = bpy.data.objects.new('ImageHolder', None)
bpy.context.scene.collection.objects.link(holder)
holder['texture'] = image_b
groups, _skipped, blocked = materials_mod.safe_image_groups()
t.check('image_idprop_blocks_merge', image_b.name in blocked and not groups,
        (blocked, [[i.name for i in g] for g in groups]))
del holder['texture']
groups, _skipped, blocked = materials_mod.safe_image_groups()
t.check('image_merges_without_reference', groups and not blocked,
        (blocked, [[i.name for i in g] for g in groups]))

t.finish('SCENEAUDIT')
