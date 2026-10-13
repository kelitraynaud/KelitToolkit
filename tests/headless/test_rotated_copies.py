"""Rotated copies: Detect Duplicates finds copies whose position AND
rotation are baked into separate meshes, Split Repeated Parts finds turned
copies inside one mesh; every vertex stays where it was."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Harness  # noqa: E402
import _shapes as shapes  # noqa: E402

import bmesh  # noqa: E402
import bpy  # noqa: E402
import mathutils  # noqa: E402

t = Harness()
random = shapes.rng(23)


def baked_placement():
    return (mathutils.Matrix.Translation((random.uniform(-30, 30), random.uniform(-30, 30),
                                          random.uniform(-5, 5)))
            @ shapes.random_rotation(random))


# ---- Detect Duplicates: three copies, placement + rotation in the meshes ----
scene = t.fresh_scene()
material = bpy.data.materials.new('M_Vent')
copies = []
placements = [baked_placement(), baked_placement(),
              mathutils.Matrix.Translation((4, 0, 0)) @ mathutils.Matrix.Rotation(1.5707963267948966, 4, 'X')]
for number, placement in enumerate(placements):
    mesh = shapes.shape_mesh(f'vent_{number}', matrix=placement)
    mesh.materials.append(material)
    obj = shapes.link(f'Vent{number}', mesh,
                      mathutils.Matrix.Translation((number * 2.0, 1.0, 0.0)))
    copies.append(obj)
# a different shape must stay alone
odd = shapes.link('Odd', shapes.shape_mesh('odd', size=(2.0, 1.2, 0.6), matrix=baked_placement()))
odd.data.materials.append(material)
bpy.context.view_layer.update()
before = [shapes.world_points(obj) for obj in copies]
odd_before = shapes.world_points(odd)
bpy.ops.object.select_all(action='SELECT')
bpy.ops.kelit_toolkit.detect_and_replace_instances(
    'EXEC_DEFAULT', search_scope='SELECTED', baked_positions=True, rotated_copies=True,
    compare_materials='CONTENT', rename_to_mesh=False)
bpy.context.view_layer.update()
mesh_objects = [o for o in scene.objects if o.type == 'MESH' and o.name != 'Odd']
t.check('dupes_rotated_one_mesh', len(mesh_objects) == 3
        and len({o.data.name for o in mesh_objects}) == 1,
        sorted((o.name, o.data.name) for o in mesh_objects))
after = [shapes.world_points(obj) for obj in mesh_objects]
placed = all(any(shapes.same_points(points, other) for other in after) for points in before)
t.check('dupes_rotated_vertices_in_place', placed)
t.check('dupes_other_shape_alone', 'Odd' in bpy.data.objects
        and bpy.data.objects['Odd'].data.name == 'odd'
        and shapes.same_points(odd_before, shapes.world_points(bpy.data.objects['Odd'])))

# option off: the rotated copies are not matched
scene = t.fresh_scene()
for number, placement in enumerate([baked_placement(), baked_placement()]):
    shapes.link(f'Off{number}', shapes.shape_mesh(f'off_{number}', matrix=placement))
bpy.ops.object.select_all(action='SELECT')
bpy.ops.kelit_toolkit.detect_and_replace_instances(
    'EXEC_DEFAULT', search_scope='SELECTED', baked_positions=True, rotated_copies=False,
    rename_to_mesh=False)
t.check('dupes_option_off_untouched',
        len({o.data.name for o in scene.objects if o.type == 'MESH'}) == 2)

# ---- Split Repeated Parts: four turned copies inside one mesh ----
scene = t.fresh_scene()
material = bpy.data.materials.new('M_Set')
bm = bmesh.new()
turns = [mathutils.Matrix.Translation((0, 0, 0)),
         mathutils.Matrix.Translation((6, 0, 0)) @ shapes.random_rotation(random),
         mathutils.Matrix.Translation((0, 6, 0)) @ shapes.random_rotation(random),
         mathutils.Matrix.Translation((6, 6, 2)) @ mathutils.Matrix.Rotation(3.141592653589793, 4, 'Z')]
for turn in turns:
    shapes.add_beveled_box(bm, matrix=turn)
mesh = bpy.data.meshes.new('set')
bm.to_mesh(mesh)
bm.free()
mesh.materials.append(material)
holder = shapes.link('SM_Set', mesh)
holder.location = (50, 0, 0)
holder.scale = (0.5, 0.5, 0.5)
bpy.context.view_layer.update()
world_before = shapes.world_points(holder)
t.deselect_all()
holder.select_set(True)
bpy.context.view_layer.objects.active = holder
bpy.ops.kelit_toolkit.split_repeated_parts('EXEC_DEFAULT', min_vertices=8, min_repeats=2,
                                           rotated_copies=True)
bpy.context.view_layer.update()
parts = [o for o in scene.objects if o.name.startswith('SM_Set_Part')]
t.check('split_rotated_four_instances', len(parts) == 4
        and len({o.data.name for o in parts}) == 1, len(parts))
remaining = [o for o in scene.objects if o.type == 'MESH']
world_after = [co for o in remaining for co in shapes.world_points(o)]
t.check('split_rotated_world_unchanged', shapes.same_points(world_before, world_after))
t.check('split_rotated_material_kept', parts and parts[0].data.materials[0] == material)

t.finish('ROTATEDCOPIES')
