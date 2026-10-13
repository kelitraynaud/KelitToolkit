"""Optimize AI Meshes: dense triangulated meshes decimated once per shared
mesh, light quad meshes and rigged meshes left alone, revert restores all."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Harness  # noqa: E402

import bpy  # noqa: E402

t = Harness()
scene = t.fresh_scene()


def dense_sphere(name, location):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=256, ring_count=128, radius=0.2,
                                         location=location)
    obj = bpy.context.active_object
    obj.name = name
    obj.data.name = name
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.quads_convert_to_tris()
    bpy.ops.object.mode_set(mode='OBJECT')
    obj.data.materials.append(bpy.data.materials.new(name + '_mat'))
    return obj


dense = dense_sphere('Can', (0, 0, 0))                 # ~65k tris on 0.5 m2
shared_src = dense_sphere('Parasol', (2, 0, 0))
shared = [shared_src]
for index in range(2):
    copy = bpy.data.objects.new(f'Parasol.{index}', shared_src.data)
    copy.location = (2, 2 + index, 0)
    bpy.context.collection.objects.link(copy)
    shared.append(copy)
bpy.ops.mesh.primitive_cube_add(location=(5, 0, 0))   # light quads: ignored
light = bpy.context.active_object
light.name = 'LightQuads'
rigged = dense_sphere('Rigged', (8, 0, 0))            # dense but deformed: ignored
armature = bpy.data.objects.new('Rig', bpy.data.armatures.new('Rig'))
bpy.context.collection.objects.link(armature)
rigged.modifiers.new('Armature', 'ARMATURE').object = armature

faces_before = {o.name: len(o.data.polygons) for o in (dense, shared_src, light, rigged)}
uv_before = len(dense.data.uv_layers)
ai = t.submodule('operators.ai_optimize')
probe = {'min_tri_share': 0.9, 'density': 20000.0, 'max_faces_absolute': 100000,
         'budget': 5000.0, 'min_faces': 2000, 'max_faces': 60000, 'min_ratio': 0.01}
plans, skipped = ai.find_candidates(list(scene.objects), probe)
names = {p['mesh'].name: p for p in plans}
t.check('detects_dense_and_shared_once', sorted(names) == ['Can', 'Parasol'], sorted(names))
t.check('shared_users_counted', len(names['Parasol']['users']) == 3)
expected = max(2000, min(60000, names['Can']['area'] * 5000)) / names['Can']['faces']
t.check('ratio_from_budget', abs(names['Can']['ratio'] - expected) < 1e-6, names['Can']['ratio'])
t.check('rigged_skipped', skipped.get('Rigged') == 'armature', skipped)
t.check('light_ignored', 'LightQuads' not in names)

# non-destructive pass, then revert
bpy.ops.object.select_all(action='SELECT')
bpy.ops.kelit_toolkit.optimize_ai_meshes('EXEC_DEFAULT', scope='SCENE', min_ratio=0.01, create_reference=False)
with_mod = [o.name for o in scene.objects if o.modifiers.get('KT_Optimize')]
t.check('modifiers_on_all_users', sorted(with_mod) == sorted(['Can'] + [o.name for o in shared]),
        with_mod)
depsgraph = bpy.context.evaluated_depsgraph_get()
evaluated_faces = len(dense.evaluated_get(depsgraph).data.polygons)
t.check('modifier_reduces', evaluated_faces < faces_before['Can'] * 0.5, evaluated_faces)
t.check('mesh_data_untouched', len(dense.data.polygons) == faces_before['Can'])
bpy.ops.kelit_toolkit.remove_ai_optimize('EXEC_DEFAULT', scope='SCENE')
t.check('revert_clean', not any(o.modifiers.get('KT_Optimize') for o in scene.objects)
        and len(dense.data.polygons) == faces_before['Can'])

# destructive pass
bpy.ops.kelit_toolkit.optimize_ai_meshes('EXEC_DEFAULT', scope='SCENE', apply=True, min_ratio=0.01, create_reference=False,
                                         protect_uv_seams=True)
t.check('apply_reduces', len(dense.data.polygons) < faces_before['Can'] * 0.5,
        len(dense.data.polygons))
t.check('apply_keeps_uv', len(dense.data.uv_layers) == uv_before and dense.data.uv_layers.active)
t.check('apply_keeps_materials', dense.data.materials[0].name == 'Can_mat')
t.check('apply_shared_still_shared', len({o.data.name for o in shared}) == 1
        and shared[0].data.name == 'Parasol' and len(shared[0].data.polygons) < faces_before['Parasol'])
t.check('apply_no_leftovers', not any(o.modifiers.get('KT_Optimize') or o.vertex_groups.get('KT_UVSeams')
                                      for o in scene.objects)
        and 'KT_OptimizeTemp' not in bpy.data.objects)
t.check('apply_others_untouched', len(light.data.polygons) == faces_before['LightQuads']
        and len(rigged.data.polygons) == faces_before['Rigged'])

# ---- hero minimum ratio and heavy subdivision alert ----
scene = t.fresh_scene()
hero = dense_sphere('Lounger', (0, 0, 0))
mate = bpy.data.objects.new('Lounger.001', hero.data)
bpy.context.collection.objects.link(mate)
t.deselect_all()
hero.select_set(True)
bpy.context.view_layer.objects.active = hero
bpy.ops.kelit_toolkit.mark_ai_hero('EXEC_DEFAULT', min_ratio=0.2, include_instances=True)
t.check('hero_marked_on_all_users', abs(hero.get('kt_optimize_min_ratio', 0) - 0.2) < 1e-6
        and abs(mate.get('kt_optimize_min_ratio', 0) - 0.2) < 1e-6)
plans, _skipped = ai.find_candidates(list(scene.objects), probe)
plan = plans[0] if plans else {}
t.check('hero_ratio_respected', abs(plan.get('ratio', 0) - 0.2) < 1e-6
        and plan.get('auto_ratio', 1) < 0.2 and abs(plan.get('hero', 0) - 0.2) < 1e-6,
        plan.get('ratio'))
bpy.ops.kelit_toolkit.optimize_ai_meshes('EXEC_DEFAULT', scope='SCENE', min_ratio=0.01, create_reference=False)
lines = [line.text for line in bpy.context.window_manager.kelit_report_lines]
t.check('hero_in_report', any('hero minimum 0.20' in text for text in lines), lines[:4])
bpy.ops.kelit_toolkit.mark_ai_hero('EXEC_DEFAULT', min_ratio=0.0, include_instances=True)
t.check('hero_unmarked', 'kt_optimize_min_ratio' not in hero and 'kt_optimize_min_ratio' not in mate)

bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16, radius=0.1, location=(3, 0, 0))
ball = bpy.context.active_object
ball.name = 'Ball'
ball.modifiers.new('Sub', 'SUBSURF').render_levels = 3      # 512 faces x 64 = 32k: under the limit
bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32, radius=0.1, location=(4, 0, 0))
heavy = bpy.context.active_object
heavy.name = 'HeavyBall'
heavy.modifiers.new('Sub', 'SUBSURF').render_levels = 3     # 2048 x 64 = 131k: flagged
bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32, radius=2, location=(9, 0, 0))
big = bpy.context.active_object
big.name = 'BigSphere'
big.modifiers.new('Sub', 'SUBSURF').render_levels = 3       # same count but 4 m wide: not flagged
bpy.context.view_layer.update()
weight = t.submodule('operators.scene_weight')
flagged = [row[0] for row in weight.heavy_subdivisions(list(scene.objects))]
t.check('heavy_subdivision_flagged', flagged == ['HeavyBall'], flagged)
data = weight.compute_scene_weight(bpy.context)
t.check('heavy_subdivision_in_weight',
        [row[0] for row in data['heavy_subdivisions']] == ['HeavyBall'])

t.finish('AIOPTIMIZE')
