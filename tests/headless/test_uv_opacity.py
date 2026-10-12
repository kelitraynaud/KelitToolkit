"""Regression tests for active UV ordering and one-image RGBA masks."""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Harness
import bpy
import numpy as np
from pxr import Usd, UsdGeom, UsdShade, Sdf, Gf

t = Harness()
sync = t.submodule('operators.usd_sync')
bridge = t.submodule('operators.ue_materials')
t.fresh_scene()
folder = tempfile.mkdtemp(prefix='kelit_uv_alpha_')

# Model the Sketchfab/C4D layer layout that lost the active UV on UE import.
bpy.ops.mesh.primitive_plane_add()
obj = bpy.context.object
obj.name = 'UVRegression'
obj.data.uv_layers[0].name = 'UVSet0'
for i in range(1, 5):
    layer = obj.data.uv_layers.new(name='UVSet' + str(i), do_init=False)
    for uv in layer.data:
        uv.uv = (0, 0)
obj.data.uv_layers[0].active_render = True
original_uvs = [(u.name, u.active_render, [tuple(v.uv) for v in u.data]) for u in obj.data.uv_layers]
path = os.path.join(folder, 'uv.usda')
counts, _, _ = sync.export_usd_hierarchy(path, [obj], False)
stage = Usd.Stage.Open(path)
mesh = next(p for p in stage.Traverse() if p.GetTypeName() == 'Mesh')
variables = [p for p in UsdGeom.PrimvarsAPI(mesh).GetPrimvars() if str(p.GetTypeName()) == 'texCoord2f[]']
names = sorted(str(p.GetPrimvarName()) for p in variables)
t.check('active_uv_sorts_first', names[0] == 'st', names)
t.check('all_source_sets_preserved', len(variables) == 5)
t.check('uv_limit_reported', counts.get('uv_over_limit') == 1, counts)
active = UsdGeom.PrimvarsAPI(mesh).GetPrimvar('st')
t.check('active_uv_not_constant', len(set(tuple(v) for v in active.ComputeFlattened())) == 4)
t.check('blender_uvs_unchanged', original_uvs == [(u.name, u.active_render, [tuple(v.uv) for v in u.data]) for u in obj.data.uv_layers])
t.check('uv_pass_idempotent', sync.normalize_uv_primvars(stage)['uv_renamed'] == 0)

# Indexed primvars and explicit shader UV readers must keep their meaning.
stage2 = Usd.Stage.CreateInMemory()
mesh2 = UsdGeom.Mesh.Define(stage2, '/Mesh')
api = UsdGeom.PrimvarsAPI(mesh2)
api.CreatePrimvar('st', Sdf.ValueTypeNames.TexCoord2fArray).Set([Gf.Vec2f(0, 0)])
pv = api.CreatePrimvar('UVSet1', Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.faceVarying)
pv.Set([Gf.Vec2f(.25, .75), Gf.Vec2f(.75, .25)])
pv.SetIndices([1, 0, 1])
reader = UsdShade.Shader.Define(stage2, '/Reader')
reader.CreateIdAttr('UsdPrimvarReader_float2')
reader.CreateInput('varname', Sdf.ValueTypeNames.Token).Set('UVSet1')
interface = UsdShade.Material.Define(stage2, '/Material')
interface_uv = interface.CreateInput('UVName', Sdf.ValueTypeNames.Token)
interface_uv.Set('UVSet1')
connected = UsdShade.Shader.Define(stage2, '/ConnectedReader')
connected.CreateIdAttr('UsdPrimvarReader_float2')
connected.CreateInput('varname', Sdf.ValueTypeNames.Token).ConnectToSource(interface_uv)
sync.normalize_uv_primvars(stage2)
new = api.GetPrimvar('st_UVSet1')
t.check('indexed_uv_preserved', list(new.GetIndices()) == [1, 0, 1]
        and [tuple(v) for v in new.Get()] == [(.25, .75), (.75, .25)])
t.check('shader_uv_reader_updated', str(reader.GetInput('varname').Get()) == 'st_UVSet1')
t.check('connected_uv_reader_updated', str(interface_uv.Get()) == 'st_UVSet1')

# Non-default render layer, with names that already have the USD prefix.
obj.data.uv_layers[0].name = 'st_Custom'
obj.data.uv_layers[1].active_render = True
for uv in obj.data.uv_layers[1].data:
    uv.uv = (.3, .7)
path2 = os.path.join(folder, 'nondefault.usda')
sync.export_usd_hierarchy(path2, [obj], False)
stage3 = Usd.Stage.Open(path2)
mesh3 = next(p for p in stage3.Traverse() if p.GetTypeName() == 'Mesh')
vals = UsdGeom.PrimvarsAPI(mesh3).GetPrimvar('st').ComputeFlattened()
t.check('active_render_not_active_editor_layer', all(abs(v[0] - .3) < 1e-5 for v in vals))

# Red is deliberately unlike Alpha: testing only the blend mode misses this bug.
image = bpy.data.images.new('RGBARegression', 1024, 1024, alpha=True)
pixels = np.ones((1024 * 1024, 4), dtype=np.float32)
pixels[:, 0] = .9
pixels[:, 3] = np.tile([0., 1.], 1024 * 512)
image.pixels.foreach_set(pixels.ravel())
image.file_format = 'PNG'
image.filepath_raw = os.path.join(folder, 'rgba.png')
image.save()
image = bpy.data.images.load(image.filepath_raw, check_existing=False)
material = bpy.data.materials.new('M_RGBA')
bsdf = next(n for n in material.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
tex = material.node_tree.nodes.new('ShaderNodeTexImage')
tex.image = image
tree = material.node_tree
tree.links.new(tex.outputs['Color'], bsdf.inputs['Base Color'])
tree.links.new(tex.outputs['Alpha'], bsdf.inputs['Alpha'])
material.surface_render_method = 'BLENDED'
original_image = (image.filepath, image.colorspace_settings.name, image.alpha_mode)
t.check('real_alpha_not_parasitic', not sync.has_parasitic_alpha(material))
records = bridge.apply_material_policies_to_records(sync.extract_material_data([material]))
t.check('rgba_masked_by_default', records['M_RGBA']['blend'] == 'MASKED')
t.check('source_channel_retained', records['M_RGBA']['opacity']['channel'] == 'Alpha')
start = time.perf_counter()
exports = bridge.export_material_textures(records, folder)
t.note('first_rgba_export_seconds', time.perf_counter() - start)
mask_entry = next(e for e in exports if e.get('alpha_stamp'))
t.check('rgba_separate_linear_mask', mask_entry['usage'] == 'linear'
        and records['M_RGBA']['opacity']['texture'] != records['M_RGBA']['base_color']['texture'])
mask = bpy.data.images.load(mask_entry['file'], check_existing=False)
mask.colorspace_settings.name = 'Non-Color'
readback = np.empty(1024 * 1024 * 4, dtype=np.float32)
mask.pixels.foreach_get(readback)
t.check('mask_contains_alpha_not_red', np.allclose(readback[0::4], pixels[:, 3], atol=.005))
stamp = os.stat(mask_entry['file']).st_mtime_ns
start = time.perf_counter()
rewritten = bridge.write_alpha_mask(image, mask_entry['file'])
t.note('cached_mask_seconds', time.perf_counter() - start)
t.check('clean_alpha_export_cached', not rewritten and os.stat(mask_entry['file']).st_mtime_ns == stamp)
t.check('source_image_unchanged', original_image == (image.filepath, image.colorspace_settings.name, image.alpha_mode))
pixels[:, 3] = 1. - pixels[:, 3]
image.pixels.foreach_set(pixels.ravel())
t.check('dirty_alpha_invalidates_cache', bridge.write_alpha_mask(image, mask_entry['file']))
bpy.data.images.remove(mask)
mask = bpy.data.images.load(mask_entry['file'], check_existing=False)
mask.colorspace_settings.name = 'Non-Color'
mask.pixels.foreach_get(readback)
t.check('updated_mask_pixels', np.allclose(readback[0::4], pixels[:, 3], atol=.005))
t.check('explicit_translucent', bridge.apply_material_policies_to_records(
    sync.extract_material_data([material]), alpha_mode='TRANSLUCENT')['M_RGBA']['blend'] == 'TRANSLUCENT')

# Reroutes retain output identity. An actual duplicated Color path remains filtered.
reroute = tree.nodes.new('NodeReroute')
tree.links.new(tex.outputs['Alpha'], reroute.inputs[0])
tree.links.new(reroute.outputs[0], bsdf.inputs['Alpha'])
t.check('rerouted_alpha_retained', not sync.has_parasitic_alpha(material)
        and sync.extract_material_data([material])['M_RGBA']['opacity']['channel'] == 'Alpha')
tree.links.new(tex.outputs['Color'], bsdf.inputs['Alpha'])
bsdf.inputs['Alpha'].default_value = .37
t.check('duplicated_color_still_filtered', sync.has_parasitic_alpha(material))
try:
    with sync.temporarily_opaque([material]):
        raise RuntimeError('test rollback')
except RuntimeError:
    pass
t.check('temporary_alpha_restored_on_error', bsdf.inputs['Alpha'].is_linked
        and abs(bsdf.inputs['Alpha'].default_value - .37) < 1e-5
        and material.surface_render_method == 'BLENDED')

t.finish('UV_OPACITY')
