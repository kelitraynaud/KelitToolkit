"""Split Repeated Parts keeps UVs apart, stays fast on many elements and
keeps the children of a fully extracted object in place; Rejoin Material
Splits refuses groups whose members have different modifiers."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Harness  # noqa: E402

import bmesh  # noqa: E402
import bpy  # noqa: E402
import mathutils  # noqa: E402

t = Harness()
split_merge = t.submodule('operators.split_merge')


def add_box(bm, center, size, uv_layer=None):
    result = bmesh.ops.create_cube(bm, size=size, calc_uvs=uv_layer is not None)
    bmesh.ops.translate(bm, verts=result['verts'], vec=mathutils.Vector(center))
    return result['verts']


def mesh_object(name, bm):
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    return obj


def run_split(obj, min_vertices):
    t.deselect_all()
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    return bpy.ops.kelit_toolkit.split_repeated_parts('EXEC_DEFAULT', min_vertices=min_vertices,
                                                      min_repeats=2)


def uv_signature(mesh):
    layer = mesh.uv_layers.active
    return tuple(sorted((round(layer.data[loop.index].uv.x, 4), round(layer.data[loop.index].uv.y, 4))
                        for loop in mesh.loops))


# ---- same shape, different UVs: never the same instance ----
scene = t.fresh_scene()
bm = bmesh.new()
uv_layer = bm.loops.layers.uv.new('UVMap')
shifted = set()
for number, center in enumerate([(0, 0, 0), (4, 0, 0), (8, 0, 0), (12, 0, 0)]):
    verts = add_box(bm, center, 1.0, uv_layer)
    if number >= 2:
        shifted.update(verts)
for face in bm.faces:
    if all(v in shifted for v in face.verts):
        for loop in face.loops:
            loop[uv_layer].uv.x += 0.5
atlas = mesh_object('SM_Atlas', bm)
run_split(atlas, 8)
parts = [o for o in scene.objects if o.name.startswith('SM_Atlas_Part')]
t.check('uv_split_four_instances', len(parts) == 4, len(parts))
t.check('uv_two_shared_meshes', len({o.data.name for o in parts}) == 2,
        sorted({o.data.name for o in parts}))
t.check('uv_instances_keep_their_uvs',
        sorted(uv_signature(o.data) for o in parts if o.location.x > 6)
        != sorted(uv_signature(o.data) for o in parts if o.location.x < 6))

# ---- 400 elements: the analysis stays linear (it scanned every face of
# the mesh for every element: several seconds here) ----
scene = t.fresh_scene()
bm = bmesh.new()
for index in range(400):
    x, y = (index % 20) * 3.0, (index // 20) * 3.0
    ball = bmesh.ops.create_icosphere(bm, subdivisions=2, radius=0.5)   # 42 verts, 80 faces
    bmesh.ops.translate(bm, verts=ball['verts'], vec=mathutils.Vector((x, y, 0)))
    add_box(bm, (x + 0.5, y, 0), 0.1)                                    # touching bolt
crowd = mesh_object('SM_Crowd', bm)
start = time.perf_counter()
repeated, units, analysed = split_merge.analyse_repeats(crowd, 20, 2, 0.005)
elapsed = time.perf_counter() - start
if analysed is not None:
    analysed.free()
t.note('analysis_seconds_400', round(elapsed, 3))
t.check('analysis_400_found', units == 400 and len(repeated) == 1
        and len(repeated[0][1]) == 400, units)
t.check('analysis_400_fast', elapsed < 1.0, round(elapsed, 3))

# ---- fully extracted object: its children stay where they were ----
scene = t.fresh_scene()
bm = bmesh.new()
add_box(bm, (0, 0, 0), 1.0)
add_box(bm, (3, 0, 0), 1.0)
pair = mesh_object('SM_Pair', bm)
pair.location = (5, 0, 0)
child = bpy.data.objects.new('Lamp', None)
bpy.context.collection.objects.link(child)
child.parent = pair
child.location = (2, 1, 1)
bpy.context.view_layer.update()
child_world = child.matrix_world.copy()
run_split(pair, 8)
bpy.context.view_layer.update()
t.check('extracted_source_removed', 'SM_Pair' not in bpy.data.objects)
t.check('child_kept_in_place', (child.matrix_world.translation - child_world.translation).length < 1e-5,
        tuple(child.matrix_world.translation))

# ---- rejoin: members with different modifiers are refused ----
scene = t.fresh_scene()
materials = [bpy.data.materials.new(f'mat_{i}') for i in range(2)]


def part(name, x, material, bevel=None):
    bpy.ops.mesh.primitive_cube_add(location=(x, 0, 0))
    obj = bpy.context.active_object
    obj.name = name
    obj.data.materials.append(material)
    if bevel is not None:
        obj.modifiers.new('Bevel', 'BEVEL').width = bevel
    return obj


part('SM_PipeMaterial1', 0, materials[0], bevel=0.1)
part('SM_PipeMaterial2', 3, materials[1])
part('SM_RailMaterial1', 0, materials[0], bevel=0.05)
part('SM_RailMaterial2', 3, materials[1], bevel=0.05)
bpy.ops.object.select_all(action='SELECT')
bpy.ops.kelit_toolkit.rejoin_material_splits('EXEC_DEFAULT', search_scope='SELECTED')
t.check('rejoin_refused_different_modifiers',
        'SM_Pipe' not in bpy.data.objects and 'SM_PipeMaterial1' in bpy.data.objects
        and 'SM_PipeMaterial2' in bpy.data.objects
        and len(bpy.data.objects['SM_PipeMaterial1'].modifiers) == 1)
rail = bpy.data.objects.get('SM_Rail')
t.check('rejoin_same_modifiers_joined', rail is not None and len(rail.data.materials) == 2
        and len(rail.modifiers) == 1)

t.finish('SPLITSAFETY')
