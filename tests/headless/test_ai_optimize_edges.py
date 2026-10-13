"""Optimize AI Meshes edge cases: linked objects, UV seam protection that
cannot be guaranteed, zero-scale objects, references of already decimated
meshes, viewports restored when Apply fails."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Harness  # noqa: E402

import bmesh  # noqa: E402
import bpy  # noqa: E402

t = Harness()
ai = t.submodule('operators.ai_optimize')
probe = dict(min_tri_share=.9, density=20000, max_faces_absolute=100000,
             budget=5000, min_faces=10, max_faces=100, min_ratio=.01)


def sphere(name='Source', location=(0, 0, 0)):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32, radius=.1, location=location)
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


# ---- linked objects are skipped by Remove KT_Optimize ----
t.fresh_scene()
source = sphere('LinkedSphere')
ai.add_modifier(source, .3, False)
library_path = os.path.join(tempfile.mkdtemp(prefix='kelit_ailib_'), 'props.blend')
bpy.data.libraries.write(library_path, {source}, fake_user=True)
t.fresh_scene()
with bpy.data.libraries.load(library_path, link=True) as (data_from, data_to):
    data_to.objects = ['LinkedSphere']
linked = data_to.objects[0]
bpy.context.scene.collection.objects.link(linked)
t.check('linked_has_modifier', linked.library is not None
        and linked.modifiers.get(ai.MODIFIER_NAME) is not None)
try:
    removed = ai.remove_optimization([linked])
    error = None
except Exception as exc:   # noqa: BLE001
    removed, error = -1, str(exc)
t.check('remove_skips_linked', removed == 0 and linked.modifiers.get(ai.MODIFIER_NAME) is not None,
        error)
try:
    result = list(bpy.ops.kelit_toolkit.remove_ai_optimize('EXEC_DEFAULT', scope='SCENE'))
except Exception as exc:   # noqa: BLE001
    result = [str(exc)]
t.check('remove_operator_linked_scene', result == ['FINISHED'], result)

# ---- protect_seams leaves no group behind when it gives up ----
t.fresh_scene()
first = sphere('First')
# vertex group names live on the mesh in Blender 5: users whose groups
# disagree only happen with different mesh data, so build that case
second = bpy.data.objects.new('Second', first.data.copy())
bpy.context.collection.objects.link(second)
second.vertex_groups.new(name='Other')       # SEAM group gets index 0 there, 1 here
plan = {'users': [first, second], 'mesh': first.data}
protected = ai.protect_seams(plan)
t.check('seams_unprotected', protected is False)
t.check('seams_groups_cleaned', first.vertex_groups.get(ai.SEAM_GROUP) is None
        and second.vertex_groups.get(ai.SEAM_GROUP) is None
        and second.vertex_groups.get('Other') is not None,
        [g.name for g in first.vertex_groups] + [g.name for g in second.vertex_groups])

# ---- zero-scale objects are skipped, not decimated to the minimum ----
t.fresh_scene()
flat = sphere('ZeroScale')
flat.scale = (0, 0, 0)
normal = sphere('Normal', location=(2, 0, 0))
bpy.context.view_layer.update()
plans, skipped = ai.find_candidates([flat, normal], probe)
planned = [p['mesh'].name for p in plans]
t.check('zero_scale_skipped', flat.data.name not in planned and flat.name_full in skipped,
        {'plans': planned, 'skipped': skipped})
t.check('normal_still_planned', normal.data.name in planned, planned)

# ---- a mesh already baked by Apply is not called "_KT_Original" ----
t.fresh_scene()
obj = sphere('Baked')
select(obj)
bpy.ops.kelit_toolkit.optimize_ai_meshes('EXEC_DEFAULT', apply=True, create_reference=False)
t.check('applied_mesh_marked', bool(obj.data.get(ai.APPLIED_PROPERTY, False))
        if hasattr(ai, 'APPLIED_PROPERTY') else False)
reference = ai.ensure_reference(bpy.context, {'users': [obj], 'mesh': obj.data})
t.check('decimated_reference_not_original', reference is None
        or not reference.name.endswith('_KT_Original'), getattr(reference, 'name', None))

t.fresh_scene()
fresh = sphere('Fresh')
reference = ai.ensure_reference(bpy.context, {'users': [fresh], 'mesh': fresh.data})
t.check('fresh_reference_original', reference.name == 'Fresh_KT_Original', reference.name)

# ---- Apply restores the viewports even when it fails ----
t.fresh_scene()
obj = sphere('Failing')
select(obj)
calls = {'switched': 0, 'restored': 0}


def fake_viewports_to_solid(_context):
    calls['switched'] += 1

    def restore():
        calls['restored'] += 1
    return restore


def boom(*_args, **_kwargs):
    raise RuntimeError('simulated failure')


original_switch, original_apply = ai.viewports_to_solid, ai.apply_plan
ai.viewports_to_solid, ai.apply_plan = fake_viewports_to_solid, boom
try:
    try:
        bpy.ops.kelit_toolkit.optimize_ai_meshes('EXEC_DEFAULT', apply=True, create_reference=False)
        raised = False
    except Exception:   # noqa: BLE001
        raised = True
finally:
    ai.viewports_to_solid, ai.apply_plan = original_switch, original_apply
t.check('apply_error_restores_viewports', raised and calls == {'switched': 1, 'restored': 1},
        dict(calls))

t.finish('AIOPTIMIZE_EDGES')
