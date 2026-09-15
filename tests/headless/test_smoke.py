"""Smoke: registration, polls, naming idempotency, collisions, reparenting,
instance-safe applies."""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Harness  # noqa: E402

import bpy  # noqa: E402
import mathutils  # noqa: E402

t = Harness()
t.check('has_version', bool(getattr(t.module, 'bl_info', {}).get('version')))

t.fresh_scene()

# selection tools are disabled with nothing selected
t.check('poll_disabled_empty',
        bpy.ops.kelit_toolkit.normalize_names_quick.poll() is False
        and bpy.ops.kelit_toolkit.validate_for_unreal.poll() is False
        and bpy.ops.kelit_toolkit.set_origin_preset.poll() is False)

# naming: normalize is idempotent and strips stacked suffixes
bpy.ops.mesh.primitive_cube_add()
cube = bpy.context.active_object
cube.name = 'test cube.001.002'
bpy.ops.kelit_toolkit.normalize_names_quick()
first = bpy.data.objects[0].name
bpy.ops.kelit_toolkit.normalize_names_quick()
t.check('normalize_idempotent', bpy.data.objects[0].name == first == 'SM_TestCube')

# collision: UCX is created, named, and convex-ish (a hull of a cube is a cube)
bpy.ops.object.select_all(action='SELECT')
bpy.context.view_layer.objects.active = bpy.data.objects[0]
bpy.ops.kelit_toolkit.create_collision_mesh(collision_type='UCX')
ucx = [o for o in bpy.data.objects if o.name.startswith('UCX_')]
t.check('ucx_created', len(ucx) == 1)
t.check('ucx_convex_hull_of_cube', ucx and len(ucx[0].data.vertices) == 8)

# remove_empty_parents preserves world position through nested chains
t.fresh_scene()
root = bpy.data.objects.new('root_null', None)
root.location = (10, 0, 0)
bpy.context.collection.objects.link(root)
mid = bpy.data.objects.new('mid_null', None)
mid.parent = root
mid.location = (0, 5, 0)
bpy.context.collection.objects.link(mid)
bpy.ops.mesh.primitive_cube_add(location=(10, 5, 2))
cube = bpy.context.active_object
world_before = cube.matrix_world.translation.copy()
cube.parent = mid
cube.matrix_parent_inverse = mid.matrix_world.inverted()
bpy.context.view_layer.update()
t.deselect_all()
mid.select_set(True)
bpy.context.view_layer.objects.active = mid
bpy.ops.kelit_toolkit.remove_empty_parents(apply_transforms=True)
bpy.context.view_layer.update()
t.check('reparent_world_preserved',
        all(abs(a - b) < 1e-4 for a, b in zip(world_before, cube.matrix_world.translation)))

# instance-safe apply rotation: parented instance keeps its world geometry
t.fresh_scene()
parent = bpy.data.objects.new('parent_null', None)
parent.rotation_euler = (0, 0, math.radians(90))
bpy.context.collection.objects.link(parent)
bpy.ops.mesh.primitive_cube_add()
cube = bpy.context.active_object
cube.parent = parent
cube.rotation_euler = (0, 0, math.radians(45))
bpy.context.view_layer.update()


def world_verts(obj):
    return sorted(tuple(round(c, 4) for c in (obj.matrix_world @ v.co))
                  for v in obj.data.vertices)


before = world_verts(cube)
bpy.ops.kelit_toolkit.apply_rotation_instances()
bpy.context.view_layer.update()
after = world_verts(cube)
t.check('apply_rotation_world_stable',
        all(all(abs(a[i] - b[i]) < 1e-3 for i in range(3)) for a, b in zip(before, after)))
t.check('apply_rotation_reset', all(abs(v) < 1e-6 for v in cube.rotation_euler))

# instance-safe apply scale: refuses mirrored scale, applies uniform one
t.fresh_scene()
bpy.ops.mesh.primitive_cube_add()
cube = bpy.context.active_object
cube.scale = (2.0, 2.0, 2.0)
bpy.ops.kelit_toolkit.apply_scale_instances()
t.check('apply_scale_applied', all(abs(v - 1.0) < 1e-6 for v in cube.scale)
        and abs(max(v.co.x for v in cube.data.vertices) - 2.0) < 1e-4)
cube.scale = (-1.0, 1.0, 1.0)
bpy.ops.kelit_toolkit.apply_scale_instances()
t.check('apply_scale_refuses_mirror', abs(cube.scale.x + 1.0) < 1e-6)
cube.scale = (0.0, 1.0, 1.0)
bpy.ops.kelit_toolkit.apply_scale_instances()
t.check('apply_scale_refuses_zero', abs(cube.scale.x) < 1e-6
        and abs(max(v.co.x for v in cube.data.vertices) - 2.0) < 1e-4)

# Apply All Transforms puts the selection and the active object back
t.fresh_scene()
bpy.ops.mesh.primitive_cube_add()
cube = bpy.context.active_object
cube.scale = (2.0, 2.0, 2.0)
lamp = bpy.data.objects.new('lamp', bpy.data.lights.new('lamp', 'POINT'))
bpy.context.collection.objects.link(lamp)
lamp.select_set(True)
bpy.context.view_layer.objects.active = lamp
bpy.ops.kelit_toolkit.apply_all_transforms()
t.check('apply_all_applied', all(abs(v - 1.0) < 1e-6 for v in cube.scale))
t.check('apply_all_selection_restored', lamp.select_get() and cube.select_get()
        and bpy.context.view_layer.objects.active == lamp)

# origin preset on a Ctrl+P-parented object (matrix_parent_inverse set):
# the geometry must not move in world space
t.fresh_scene()
holder = bpy.data.objects.new('holder', None)
holder.location = (10, 0, 0)
bpy.context.collection.objects.link(holder)
bpy.context.view_layer.update()
bpy.ops.mesh.primitive_cube_add(location=(10, 0, 1))
cube = bpy.context.active_object
cube.parent = holder
cube.matrix_parent_inverse = holder.matrix_world.inverted()
bpy.context.view_layer.update()
before = world_verts(cube)
t.deselect_all()
cube.select_set(True)
bpy.context.view_layer.objects.active = cube
bpy.ops.kelit_toolkit.set_origin_preset(preset='BOTTOM_CENTER')
bpy.context.view_layer.update()
after = world_verts(cube)
t.check('origin_parented_geometry_stays',
        all(all(abs(a[i] - b[i]) < 1e-3 for i in range(3)) for a, b in zip(before, after)))
t.check('origin_at_bottom', abs(min(v.co.z for v in cube.data.vertices)) < 1e-6)

# UCX of an object with modifiers: hull of the evaluated shape, and the copy
# carries no modifier (a Subsurf left on it re-shaped the hull at export)
t.fresh_scene()
bpy.ops.mesh.primitive_cube_add()
cube = bpy.context.active_object
cube.modifiers.new('Subdiv', 'SUBSURF').levels = 2
bpy.ops.kelit_toolkit.create_collision_mesh(collision_type='UCX')
ucx = next((o for o in bpy.data.objects if o.name.startswith('UCX_')), None)
t.check('ucx_no_modifiers', ucx is not None and len(ucx.modifiers) == 0)
if ucx is not None:
    evaluated = ucx.evaluated_get(bpy.context.evaluated_depsgraph_get())
    t.check('ucx_evaluated_is_raw', len(evaluated.data.vertices) == len(ucx.data.vertices))
    t.check('ucx_follows_evaluated_shape', len(ucx.data.vertices) > 8)

# Detect Duplicates with the placement baked into the meshes (Maya-style
# import) and duplicate materials with different names
t.fresh_scene()
image = bpy.data.images.new('T_rooftop', 8, 8)


def rooftop_material(name):
    material = bpy.data.materials.new(name)
    tree = material.node_tree
    tex = tree.nodes.new('ShaderNodeTexImage')
    tex.image = image
    principled = next(n for n in tree.nodes if n.type == 'BSDF_PRINCIPLED')
    tree.links.new(tex.outputs['Color'], principled.inputs['Base Color'])
    return material


mat_a, mat_b = rooftop_material('rooftop_01'), rooftop_material('rooftop_01.001')
boxes = []
for name, offset, material in (('vent_a', (0, 0, 0), mat_a), ('vent_b', (22, -25, 0), mat_b)):
    bpy.ops.mesh.primitive_cube_add()
    box = bpy.context.active_object
    box.name = name
    for v in box.data.vertices:
        v.co += mathutils.Vector(offset)   # placement written into the mesh
    box.data.materials.append(material)
    box.location = (5, 5, 0)               # both objects share one transform
    boxes.append(box)
bpy.context.view_layer.update()
world_before = {box.name: world_verts(box) for box in boxes}
bpy.ops.object.select_all(action='SELECT')
bpy.ops.kelit_toolkit.detect_and_replace_instances(
    'EXEC_DEFAULT', search_scope='SELECTED', baked_positions=True,
    compare_materials='CONTENT', rename_to_mesh=False)
bpy.context.view_layer.update()
mesh_objects = [o for o in bpy.data.objects if o.type == 'MESH']
t.check('dupes_baked_instanced', len(mesh_objects) == 2
        and len({o.data.name for o in mesh_objects}) == 1)
instance = next((o for o in mesh_objects if o.name != 'vent_a'), None)
t.check('dupes_baked_placement', instance is not None and all(
    all(abs(a[i] - b[i]) < 1e-3 for i in range(3))
    for a, b in zip(world_before['vent_b'], world_verts(instance))))

# Merge Duplicate Materials: one material left, every slot reassigned
count_before = len(bpy.data.materials)
bpy.ops.kelit_toolkit.merge_duplicate_materials('EXEC_DEFAULT')
t.check('materials_merged', bpy.data.materials.get('rooftop_01.001') is None
        and bpy.data.materials.get('rooftop_01') is not None
        and len(bpy.data.materials) == count_before - 1)
t.check('materials_reassigned', all(
    material is not None and material.name == 'rooftop_01'
    for mesh in bpy.data.meshes for material in mesh.materials))

# Send dialog, Collection mode without an 'Export' collection: the active
# collection is used (it used to send nothing, silently)
t.fresh_scene()
vehicles = bpy.data.collections.new('Vehicles')
bpy.context.scene.collection.children.link(vehicles)
bpy.ops.mesh.primitive_cube_add()
car = bpy.context.active_object
for collection in list(car.users_collection):
    collection.objects.unlink(car)
vehicles.objects.link(car)
bpy.context.view_layer.active_layer_collection = \
    bpy.context.view_layer.layer_collection.children['Vehicles']
t.deselect_all()
usd_sync = t.submodule('operators.usd_sync')
sync_op = usd_sync.UNREAL_OT_usd_scene_sync
probe = type('Probe', (), {'source': 'EXPORT_COLLECTION',
                           '_source_collection': staticmethod(sync_op._source_collection)})()
base = usd_sync.UNREAL_OT_usd_scene_sync._base_objects(probe, bpy.context)
t.check('send_collection_falls_back_to_active', [o.name for o in base] == [car.name])
export = bpy.data.collections.new('Export')
bpy.context.scene.collection.children.link(export)
base = usd_sync.UNREAL_OT_usd_scene_sync._base_objects(probe, bpy.context)
t.check('send_collection_prefers_export', list(base) == [])

t.finish('SMOKE')
