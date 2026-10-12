"""Geometry invalidation, packed channels, shared Mapping, destination safety."""
import os, sys, tempfile, json
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
from _harness import Harness
import bpy
from pxr import Usd, UsdGeom
t=Harness()
sync=t.submodule('operators.usd_sync')
bridge=t.submodule('operators.ue_materials')
from KelitToolkit.operators import ue_remote, sync_cache, material_graph
t.fresh_scene()
folder=tempfile.mkdtemp(prefix='kelit095_')
nodes=[{'node_id':'1','project_root':'C:/One/','project_name':'One'},
       {'node_id':'2','project_root':'C:/Two/','project_name':'Two'}]
t.check('target_explicit',ue_remote.choose_editor(nodes,'c:/two')['node_id']=='2')
for label,items,path in [('ambiguous',nodes,''),('missing',nodes,'C:/Missing')]:
 try:ue_remote.choose_editor(items,path);safe=False
 except ue_remote.TargetSelectionError:safe=True
 t.check('target_'+label+'_refused',safe)
with ue_remote.pin_editor(nodes[0]):
 t.check('target_pinned',ue_remote.resolve_target()['node_id']=='1')
t.check('pin_restored',ue_remote._PINNED_EDITOR is None)

bpy.ops.mesh.primitive_plane_add()
obj=bpy.context.object
obj.name='SM_ChannelMapping'
mat=bpy.data.materials.new('M_ChannelMapping')
mat.use_nodes=True;obj.data.materials.append(mat)
tree=mat.node_tree;bsdf=tree.nodes.get('Principled BSDF')
image=bpy.data.images.new('ORMFixture',2,2,alpha=True)
image.colorspace_settings.name='Non-Color';image.pixels=[.2,.4,.8,1]*4
tex=tree.nodes.new('ShaderNodeTexImage');tex.image=image
separate=tree.nodes.new('ShaderNodeSeparateColor');separate.mode='RGB'
tree.links.new(tex.outputs['Color'],separate.inputs[0])
tree.links.new(separate.outputs['Green'],bsdf.inputs['Roughness'])
tree.links.new(separate.outputs['Blue'],bsdf.inputs['Metallic'])
mapping=tree.nodes.new('ShaderNodeMapping')
uv=tree.nodes.new('ShaderNodeTexCoord')
tree.links.new(uv.outputs['UV'],mapping.inputs['Vector'])
tree.links.new(mapping.outputs['Vector'],tex.inputs['Vector'])
mapping.inputs['Scale'].default_value=(2,3,1)
mapping.inputs['Location'].default_value=(.25,-.5,0)
records=sync.extract_material_data([mat])
t.check('green_blue_semantics',records[mat.name]['roughness']['channel']=='G' and records[mat.name]['metallic']['channel']=='B')
entries=bridge.export_material_textures(records,folder)
for channel,value in [('G',.4),('B',.8)]:
 path=next(e['file'] for e in entries if e['name'].endswith('_B2UE_'+channel))
 loaded=bpy.data.images.load(path,check_existing=False)
 t.check('unpacked_'+channel,abs(loaded.pixels[0]-value)<.005,float(loaded.pixels[0]))
original=obj.data
original_uv=[tuple(v.uv) for v in original.uv_layers.active.data]
file=os.path.join(folder,'geometry.usda')
sync.export_usd_hierarchy(file,[obj],True,bake_material_uvs=True)
t.check('original_mesh_restored',obj.data==original and [tuple(v.uv) for v in original.uv_layers.active.data]==original_uv)
stage=Usd.Stage.Open(file)
mesh=next(p for p in stage.Traverse() if p.GetTypeName()=='Mesh')
uvs=UsdGeom.PrimvarsAPI(mesh).GetPrimvar('st').ComputeFlattened()
expected={(u*2+.25,v*3-.5) for u,v in original_uv}
t.check('mapping_in_exported_uv',set(tuple(v) for v in uvs)==expected,[tuple(v) for v in uvs])
mesh = None
stage = None
h1=sync_cache.static_fingerprint(file)
bsdf.inputs['Base Color'].default_value=(.7,.2,.1,1)
sync.export_usd_hierarchy(file,[obj],True,bake_material_uvs=True)
t.check('shader_change_reuses_geometry',sync_cache.static_fingerprint(file)==h1)
obj.data.vertices[0].co.x+=.1
sync.export_usd_hierarchy(file,[obj],True,bake_material_uvs=True)
h2=sync_cache.static_fingerprint(file)
t.check('vertex_edit_invalidates',h2!=h1)
mapping.inputs['Location'].default_value.x+=.25
sync.export_usd_hierarchy(file,[obj],True,bake_material_uvs=True)
t.check('mapping_edit_invalidates',sync_cache.static_fingerprint(file)!=h2)
# Unknown arithmetic must not silently turn into a raw image.
math=tree.nodes.new('ShaderNodeMath');math.operation='MULTIPLY'
tree.links.new(separate.outputs['Green'],math.inputs[0]);tree.links.new(math.outputs[0],bsdf.inputs['Roughness'])
t.check('unsupported_math_reported',bool(material_graph.record_warnings(sync.extract_material_data([mat]))))
other=tree.nodes.new('ShaderNodeBsdfPrincipled')
output=tree.get_output_node('ALL')
tree.links.new(other.outputs['BSDF'],output.inputs['Surface'])
t.check('connected_shader_selected',sync.get_principled(mat)==other)
mix=tree.nodes.new('ShaderNodeMixShader')
tree.links.new(other.outputs['BSDF'],mix.inputs[1])
tree.links.new(mix.outputs[0],output.inputs['Surface'])
t.check('shader_mix_reported',bool(material_graph.record_warnings(sync.extract_material_data([mat]))))
t.finish('sync optimizations')
