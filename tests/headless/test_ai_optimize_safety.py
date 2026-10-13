"""Regression coverage for optimizer scope, references, measured output and Hero."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Harness
import bpy
import bmesh

t = Harness()
ai = t.submodule('operators.ai_optimize')
probe = dict(min_tri_share=.9, density=20000, max_faces_absolute=100000,
             budget=5000, min_faces=2000, max_faces=60000, min_ratio=.01)

def sphere(name='Source'):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32, radius=.1)
    obj = bpy.context.object
    obj.name = name
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bmesh.ops.triangulate(bm, faces=list(bm.faces))
    bm.to_mesh(obj.data)
    bm.free()
    return obj

def select(obj):
    t.deselect_all()
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

def instance(obj,name):
    other = bpy.data.objects.new(name,obj.data)
    bpy.context.collection.objects.link(other)
    return other

def optimize(**kwargs):
    return bpy.ops.kelit_toolkit.optimize_ai_meshes('EXEC_DEFAULT', create_reference=False, **kwargs)

t.fresh_scene()
obj = sphere()
select(obj)
for ratio in (.95,.999,1.0):
    ai.add_modifier(obj,.12,False)
    obj[ai.HERO_PROPERTY]=ratio
    result=optimize()
    mod=obj.modifiers.get(ai.MODIFIER_NAME)
    t.check('hero_updates_' + str(ratio), result=={'FINISHED'} and
            ((mod is None) if ratio==1.0 else abs(mod.ratio-ratio)<1e-6))
del obj[ai.HERO_PROPERTY]
peer=instance(obj,'OutsideScope')
optimize(min_faces=10,max_faces=100)
t.check('default_ratio_floor',abs(obj.modifiers[ai.MODIFIER_NAME].ratio-.5)<1e-6)
t.check('selected_only_no_other_modifier',peer.modifiers.get(ai.MODIFIER_NAME) is None)
bpy.ops.kelit_toolkit.mark_ai_hero('EXEC_DEFAULT',min_ratio=.8)
t.check('hero_selected_only',peer.get(ai.HERO_PROPERTY) is None)
optimize(include_instances=True)
t.check('instances_explicit',abs(peer.modifiers[ai.MODIFIER_NAME].ratio-.8)<1e-6)

excluded=instance(obj,'ExcludedReference')
excluded[ai.EXCLUDE_PROPERTY]=True
optimize(include_instances=True)
t.check('excluded_user_does_not_block_others',excluded.modifiers.get(ai.MODIFIER_NAME) is None
        and obj.modifiers.get(ai.MODIFIER_NAME) is not None)

t.fresh_scene()
obj=sphere()
peer=instance(obj,'Peer')
base=peer.data
select(obj)
optimize(protect_uv_seams=True)
t.check('seam_weights_scoped',peer.data==base and obj.data!=base
        and peer.vertex_groups.get(ai.SEAM_GROUP) is None)
t.check('protected_positions_retained',
        len(ai.uv_seam_vertices(obj.data))>0)

t.fresh_scene()
obj=sphere()
peer=instance(obj,'Peer')
select(obj)
optimize(protect_uv_seams=True,include_instances=True)
ai.remove_optimization([obj])
t.check('remove_keeps_shared_protection',peer.modifiers[ai.MODIFIER_NAME].vertex_group==ai.SEAM_GROUP
        and peer.vertex_groups.get(ai.SEAM_GROUP) is not None)

t.fresh_scene()
obj=sphere()
select(obj)
obj.modifiers.new('Decimate_Test','DECIMATE').ratio=.1
result=optimize()
t.check('no_stacked_decimation',result=={'CANCELLED'} and obj.modifiers.get(ai.MODIFIER_NAME) is None)
obj.modifiers.clear()
obj.modifiers.new(ai.MODIFIER_NAME,'SUBSURF')
t.check('name_collision_safe',optimize()=={'CANCELLED'}
        and obj.modifiers[ai.MODIFIER_NAME].type=='SUBSURF')
ai.remove_optimization([obj])
t.check('remove_name_collision_safe',len(obj.modifiers)==1)
obj.modifiers.clear()
obj.modifiers.new('Subdivision','SUBSURF')
t.check('active_stack_skipped',optimize()=={'CANCELLED'})

t.fresh_scene()
obj=sphere()
peer=instance(obj,'OutsideScope')
original=peer.data
before=len(original.polygons)
select(obj)
bpy.ops.kelit_toolkit.optimize_ai_meshes('EXEC_DEFAULT',apply=True)
reference=bpy.data.objects[obj[ai.REFERENCE_PROPERTY]]
t.check('apply_scoped',peer.data==original and len(peer.data.polygons)==before
        and obj.data!=peer.data and len(obj.data.polygons)<before)
t.check('independent_reference',reference.data!=obj.data and reference.data!=peer.data
        and len(reference.data.polygons)==before and reference.get(ai.EXCLUDE_PROPERTY)
        and reference.hide_viewport and reference.hide_render and not reference.modifiers)
ref_name=reference.name
bpy.ops.kelit_toolkit.optimize_ai_meshes('EXEC_DEFAULT')
t.check('reference_not_overwritten',obj[ai.REFERENCE_PROPERTY]==ref_name
        and len(reference.data.polygons)==before)

t.fresh_scene()
obj=sphere()
select(obj)
optimize(min_ratio=.01,protect_uv_seams=True,min_faces=10,max_faces=100,budget=10)
actual=ai.evaluated_stats(bpy.context,obj)[0]
lines=[line.text for line in bpy.context.window_manager.kelit_report_lines]
t.check('report_measured_faces',any(f'to {actual:,} faces' in text for text in lines),actual)
obj.modifiers.clear()
while obj.data.uv_layers: obj.data.uv_layers.remove(obj.data.uv_layers[0])
optimize()
lines=[line.text for line in bpy.context.window_manager.kelit_report_lines]
t.check('missing_uv_reported',any('NO UV' in text for text in lines))

t.fresh_scene()
bpy.ops.mesh.primitive_plane_add(size=2)
plane=bpy.context.object
plane.scale=(10,1,.1)
bpy.context.view_layer.update()
t.check('nonuniform_surface_area',abs(ai.world_surface_area(plane.data,plane)-40)<1e-6)
t.check('scene_unit_conversion',abs(ai.world_surface_area(plane.data,plane,.01)-.004)<1e-6)

t.finish('AIOPTIMIZE_SAFETY')
