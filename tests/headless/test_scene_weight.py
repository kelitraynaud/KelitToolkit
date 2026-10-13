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

# ---- unsaved painting and generated images are never merged ----
clean_a = bpy.data.images.load(path, check_existing=False)
clean_b = bpy.data.images.load(path, check_existing=False)
painted = bpy.data.images.load(path, check_existing=False)
painted.pixels = [0.9] * (16 * 16 * 4)                # painted, not saved
generated = bpy.data.images.new('generated_twin', 16, 16)
t.check('painted_is_dirty', painted.is_dirty)
skipped_images = []
groups = materials_mod.find_duplicate_images(skipped_images)
t.check('dirty_not_merged', all(painted not in g for g in groups)
        and any(clean_a in g and clean_b in g for g in groups),
        [[i.name for i in g] for g in groups])
t.check('skipped_listed', painted in skipped_images and generated in skipped_images,
        [i.name for i in skipped_images])
painted_name = painted.name
bpy.ops.kelit_toolkit.merge_duplicate_images('EXEC_DEFAULT')
t.check('dirty_survives_merge', bpy.data.images.get(painted_name) is not None
        and bpy.data.images.get('generated_twin') is not None)

# ---- texture sizes read from the file header, without loading ----
jpeg_source = bpy.data.images.new('jpeg_source', 48, 16)
jpeg_source.pixels = [0.4] * (48 * 16 * 4)
jpeg_path = os.path.join(folder, 'wide.jpg')
jpeg_source.filepath_raw = jpeg_path
jpeg_source.file_format = 'JPEG'
jpeg_source.save()
bpy.data.images.remove(jpeg_source)
png_source = bpy.data.images.new('png_source', 64, 32)
png_path = os.path.join(folder, 'header.png')
png_source.filepath_raw = png_path
png_source.file_format = 'PNG'
png_source.save()
bpy.data.images.remove(png_source)
t.fresh_scene()
header_png = bpy.data.images.load(png_path, check_existing=False)
header_jpg = bpy.data.images.load(jpeg_path, check_existing=False)
for image in (header_png, header_jpg):
    image.use_fake_user = True
t.check('header_not_loaded_yet', not header_png.has_data and not header_jpg.has_data)
rows = {row[0]: row for row in weight.texture_weights()}
t.check('header_png_size', rows.get(header_png.name, (0, 0, 0, 0))[2:4] == (64, 32),
        rows.get(header_png.name))
t.check('header_jpg_size', rows.get(header_jpg.name, (0, 0, 0, 0))[2:4] == (48, 16),
        rows.get(header_jpg.name))
t.check('header_memory', rows.get(header_png.name, (0, 0))[1] == 64 * 32 * 4)
t.check('header_did_not_load', not header_png.has_data and not header_jpg.has_data)

# ---- Catmull-Clark estimate follows the real polygon composition ----
t.fresh_scene()
triangle_mesh = bpy.data.meshes.new('Triangle')
triangle_mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
triangle = bpy.data.objects.new('Triangle', triangle_mesh)
bpy.context.collection.objects.link(triangle)
sub_one = triangle.modifiers.new('Sub', 'SUBSURF')
sub_one.levels, sub_one.render_levels = 0, 1
bpy.context.view_layer.update()
counter = weight.WeightCounter(bpy.context)
t.check('subsurf_triangle_level1', counter.object_triangles(triangle)[0] == 6,
        counter.object_triangles(triangle))
sub_one.render_levels = 2
counter = weight.WeightCounter(bpy.context)
t.check('subsurf_triangle_level2', counter.object_triangles(triangle)[0] == 24,
        counter.object_triangles(triangle))

bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32, radius=0.1, location=(5, 0, 0))
soup = bpy.context.active_object
soup.name = 'TriangleSoup'
import bmesh  # noqa: E402
bm = bmesh.new()
bm.from_mesh(soup.data)
bmesh.ops.triangulate(bm, faces=list(bm.faces))
bm.to_mesh(soup.data)
bm.free()
soup_faces = len(soup.data.polygons)                     # 3968 triangles
soup.modifiers.new('Sub', 'SUBSURF').render_levels = 2   # 3 x 3968 x 4 = 47616 quads
bpy.context.view_layer.update()
counter = weight.WeightCounter(bpy.context)
t.check('subsurf_soup_estimate', counter.object_triangles(soup)[0] == 2 * 3 * soup_faces * 4,
        counter.object_triangles(soup))
heavy = weight.heavy_subdivisions([soup], max_faces=40000)
t.check('heavy_uses_real_estimate', heavy and heavy[0][2] == 3 * soup_faces * 4, heavy)
t.check('heavy_not_flagged_under_limit', not weight.heavy_subdivisions([soup]))
soup.modifiers['Sub'].levels = 2                         # evaluate it for real
bpy.context.view_layer.update()
evaluated = soup.evaluated_get(bpy.context.evaluated_depsgraph_get())
t.check('subsurf_estimate_matches_blender', len(evaluated.data.polygons) == 3 * soup_faces * 4,
        len(evaluated.data.polygons))

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
