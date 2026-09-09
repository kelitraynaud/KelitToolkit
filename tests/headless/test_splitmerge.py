"""Rejoin Material Splits and Split Repeated Parts on synthetic meshes."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Harness  # noqa: E402

import bmesh  # noqa: E402
import bpy  # noqa: E402
import mathutils  # noqa: E402

t = Harness()
scene = t.fresh_scene()


def world_verts(obj):
    return sorted(tuple(round(c, 4) for c in (obj.matrix_world @ v.co))
                  for v in obj.data.vertices)


# ---- rejoin: Box074 cut into three material parts ----
mats = [bpy.data.materials.new(f'mat_{i}') for i in range(3)]
parts = []
for index in range(3):
    bpy.ops.mesh.primitive_cube_add(location=(index * 3, 0, 0))
    part = bpy.context.active_object
    part.name = f'SM_Box074Material{260 + index * 50}'
    part.data.materials.append(mats[index])
    parts.append(part)
bpy.ops.mesh.primitive_cube_add(location=(0, 10, 0))
other = bpy.context.active_object
other.name = 'SM_Box075Material260'   # alone: not a group
world_before = sorted(sum((world_verts(p) for p in parts), []))
bpy.ops.object.select_all(action='SELECT')
bpy.ops.kelit_toolkit.rejoin_material_splits('EXEC_DEFAULT', search_scope='SELECTED')
joined = bpy.data.objects.get('SM_Box074')
t.check('rejoin_joined', joined is not None and len(joined.data.materials) == 3
        and len(joined.data.vertices) == 24)
t.check('rejoin_geometry_kept', joined is not None and world_verts(joined) == world_before)
t.check('rejoin_single_left_alone', bpy.data.objects.get('SM_Box075Material260') is not None)

# ---- split repeated parts: 4 AC units (box + grille touching), 1 unique blob ----
scene = t.fresh_scene()
bm = bmesh.new()
material = bpy.data.materials.new('ac_mat')


def add_box(bm, center, size):
    result = bmesh.ops.create_cube(bm, size=size)
    bmesh.ops.translate(bm, verts=result['verts'], vec=mathutils.Vector(center))
    return result['verts']


unit_centers = [(0, 0, 0), (5, 0, 0), (0, 6, 0), (5, 6, 2)]
for center in unit_centers:
    add_box(bm, center, 1.0)                                          # the box (8 verts)
    add_box(bm, (center[0] + 0.55, center[1], center[2]), 0.1)        # grille touching it
    add_box(bm, (center[0] + 0.55, center[1] + 0.2, center[2]), 0.1)  # second grille piece
add_box(bm, (20, 20, 20), 3.0)                                        # unique element
mesh = bpy.data.meshes.new('ac_set')
bm.to_mesh(mesh)
bm.free()
mesh.materials.append(material)
holder = bpy.data.objects.new('SM_AcSet', mesh)
holder.location = (100, 0, 0)
holder.scale = (0.5, 0.5, 0.5)
bpy.context.collection.objects.link(holder)
bpy.context.view_layer.update()
world_before = world_verts(holder)
t.deselect_all()
holder.select_set(True)
bpy.context.view_layer.objects.active = holder
bpy.ops.kelit_toolkit.split_repeated_parts('EXEC_DEFAULT', min_vertices=20, min_repeats=2)
bpy.context.view_layer.update()
instances = [o for o in scene.objects if o.name.startswith('SM_AcSet_Part')]
t.check('split_four_instances', len(instances) == 4
        and len({o.data.name for o in instances}) == 1)
t.check('split_unit_is_box_plus_grilles', instances and len(instances[0].data.vertices) == 24)
t.check('split_unique_stays', holder.name in bpy.data.objects
        and len(holder.data.vertices) == 8)
after = sorted(sum((world_verts(o) for o in instances + [holder]), []))
t.check('split_world_unchanged', after == world_before)
t.check('split_material_kept', instances and instances[0].data.materials[0] == material)

t.finish('SPLITMERGE')
