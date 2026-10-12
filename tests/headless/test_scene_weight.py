"""Merge Duplicate Images and Scene Weight Report on a synthetic scene."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Harness  # noqa: E402

import bpy  # noqa: E402

t = Harness()
scene = t.fresh_scene()
materials_mod = t.submodule('operators.materials')
weight = t.submodule('operators.scene_weight')
folder = tempfile.mkdtemp(prefix='kelit_images_')

# ---- duplicate images: same file loaded three times, one as Non-Color ----
source = bpy.data.images.new('skin', 16, 16)
source.pixels = [0.5] * (16 * 16 * 4)
path = os.path.join(folder, 'skin.png')
source.filepath_raw = path
source.file_format = 'PNG'
source.save()
bpy.data.images.remove(source)
copy_a = bpy.data.images.load(path, check_existing=False)
copy_b = bpy.data.images.load(path, check_existing=False)
copy_c = bpy.data.images.load(path, check_existing=False)
copy_c.colorspace_settings.name = 'Non-Color'
# a byte-identical second file under another name also merges
other_path = os.path.join(folder, 'skin_copy.png')
with open(path, 'rb') as src, open(other_path, 'wb') as dst:
    dst.write(src.read())
copy_d = bpy.data.images.load(other_path, check_existing=False)
# packed duplicates
packed_a = bpy.data.images.load(path, check_existing=False)
packed_a.pack()
packed_b = bpy.data.images.load(path, check_existing=False)
packed_b.pack()
different = bpy.data.images.new('other', 8, 8)

material = bpy.data.materials.new('M_Skin')
node = material.node_tree.nodes.new('ShaderNodeTexImage')
node.image = copy_b
material.use_fake_user = True

groups = materials_mod.find_duplicate_images()
t.check('dup_groups', len(groups) == 2, [[i.name for i in g] for g in groups])
t.check('noncolor_kept_apart', all(copy_c not in g for g in groups))
t.check('generated_ignored', all(different not in g for g in groups))
names_before = len(bpy.data.images)
result = bpy.ops.kelit_toolkit.merge_duplicate_images('EXEC_DEFAULT')
t.check('merge_finished', list(result) == ['FINISHED'])
t.check('merge_removed', len(bpy.data.images) == names_before - 3, len(bpy.data.images))
t.check('material_remapped', node.image is not None and node.image.name in bpy.data.images
        and node.image.colorspace_settings.name != 'Non-Color')
t.check('noncolor_survives', 'Non-Color' in {i.colorspace_settings.name for i in bpy.data.images})

# ---- scene weight ----
scene = t.fresh_scene()
bpy.ops.mesh.primitive_cube_add()                       # 12 tris
cube = bpy.context.active_object
cube.name = 'Plain'
bpy.ops.mesh.primitive_cube_add(location=(3, 0, 0))
sub = bpy.context.active_object
sub.name = 'Subdivided'
modifier = sub.modifiers.new('Sub', 'SUBSURF')
modifier.levels, modifier.render_levels = 0, 2           # 12 x 16 = 192 at render
sub.hide_set(True)                                      # hidden on screen, rendered
bpy.ops.mesh.primitive_cube_add(location=(6, 0, 0))
skipped = bpy.context.active_object
skipped.name = 'NotRendered'
skipped.hide_render = True
shared = []
for index in range(3):                                  # 3 x 12 shown, 12 unique
    obj = bpy.data.objects.new(f'Parasol{index}', cube.data)
    bpy.context.collection.objects.link(obj)
    shared.append(obj)

library = bpy.data.collections.new('Props')             # excluded, instanced twice
scene.collection.children.link(library)
bpy.ops.mesh.primitive_cube_add(location=(0, 9, 0))
prop = bpy.context.active_object
prop.name = 'Prop'
for collection in list(prop.users_collection):
    collection.objects.unlink(prop)
library.objects.link(prop)
bpy.context.view_layer.layer_collection.children['Props'].exclude = True
for index in range(2):
    holder = bpy.data.objects.new(f'PropInstance{index}', None)
    holder.instance_type = 'COLLECTION'
    holder.instance_collection = library
    bpy.context.collection.objects.link(holder)

bpy.ops.mesh.primitive_plane_add(location=(0, -9, 0))   # 10 particles of the 12-tri prop
emitter = bpy.context.active_object
emitter.name = 'Emitter'
emitter.modifiers.new('Particles', 'PARTICLE_SYSTEM')
settings = emitter.particle_systems[0].settings
settings.type = 'HAIR'
settings.count = 10
settings.render_type = 'OBJECT'
settings.instance_object = prop

# a child collection whose PARENT is disabled in renders: not rendered,
# even though the child itself is enabled
parent_col = bpy.data.collections.new('botaniq')
scene.collection.children.link(parent_col)
child_col = bpy.data.collections.new('tropical')
parent_col.children.link(child_col)
parent_col.hide_render = True
bpy.ops.mesh.primitive_cube_add(location=(0, 20, 0))
tree = bpy.context.active_object
tree.name = 'Tree'
for collection in list(tree.users_collection):
    collection.objects.unlink(tree)
child_col.objects.link(tree)
tree_instancer = bpy.data.objects.new('TreeInstancer', None)   # instancer inside too
tree_instancer.instance_type = 'COLLECTION'
tree_instancer.instance_collection = library
child_col.objects.link(tree_instancer)

bpy.context.view_layer.update()
data = weight.compute_scene_weight(bpy.context)
by_name = {row['name']: row['triangles'] for row in data['rows']}
t.check('weight_plain', by_name.get('Plain') == 12, by_name.get('Plain'))
t.check('weight_hidden_but_rendered', by_name.get('Subdivided') == 192, by_name.get('Subdivided'))
t.check('weight_render_disabled_skipped', 'NotRendered' not in by_name)
t.check('weight_excluded_collection_skipped', 'Prop' not in by_name)
t.check('weight_collection_instances', by_name.get('PropInstance0') == 12)
t.check('weight_parent_hide_render_excludes', 'Tree' not in by_name and 'TreeInstancer' not in by_name)
t.check('weight_particles', by_name.get('Emitter') == 2 + 120, by_name.get('Emitter'))
expected_displayed = 12 + 192 + 36 + 24 + 122
t.check('weight_total', data['displayed'] == expected_displayed, data['displayed'])
t.check('weight_unique_lower', data['unique'] == expected_displayed - 36, data['unique'])
t.check('weight_shared_flagged', any(len(members) == 4 for _d, members in data['shared']))
t.check('weight_sorted', data['rows'][0]['name'] == 'Subdivided')

result = bpy.ops.kelit_toolkit.scene_weight_report('EXEC_DEFAULT')
lines = bpy.context.window_manager.kelit_report_lines
t.check('report_written', list(result) == ['FINISHED'] and len(lines) > 5
        and any(line.object_name == 'Subdivided' for line in lines))

t.finish('SCENEWEIGHT')
