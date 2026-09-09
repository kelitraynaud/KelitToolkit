"""Auto Clean on a Maya-style import: root empty at 0.01 with a 90 degree
rotation, duplicate boxes with their placement baked into the meshes,
copies of one material, a camera rig that must survive."""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Harness  # noqa: E402

import bpy  # noqa: E402
import mathutils  # noqa: E402

t = Harness()
scene = t.fresh_scene()

root = bpy.data.objects.new('asia_building', None)
root.scale = (0.01, 0.01, 0.01)
root.rotation_euler = (math.radians(90), 0, 0)
bpy.context.collection.objects.link(root)

image = bpy.data.images.new('T_rooftop', 8, 8)


def rooftop_material(name):
    material = bpy.data.materials.new(name)
    tree = material.node_tree
    tex = tree.nodes.new('ShaderNodeTexImage')
    tex.image = image
    principled = next(n for n in tree.nodes if n.type == 'BSDF_PRINCIPLED')
    tree.links.new(tex.outputs['Color'], principled.inputs['Base Color'])
    return material


boxes = []
for index, offset in enumerate(((0, 0, 0), (22, -25, 0), (44, -50, 0))):
    bpy.ops.mesh.primitive_cube_add()
    box = bpy.context.active_object
    box.name = f'pCube{index + 1}'
    for v in box.data.vertices:
        v.co += mathutils.Vector(offset)   # placement written into the mesh
    box.data.materials.append(rooftop_material('rooftop_01' if index == 0 else f'rooftop_01.00{index}'))
    box.parent = root
    boxes.append(box)

cam_rig = bpy.data.objects.new('cam_rig', None)
bpy.context.collection.objects.link(cam_rig)
camera = bpy.data.objects.new('ShotCam', bpy.data.cameras.new('ShotCam'))
camera.parent = cam_rig
bpy.context.collection.objects.link(camera)
unused = bpy.data.objects.new('unused_null', None)
bpy.context.collection.objects.link(unused)
bpy.context.view_layer.update()


def world_verts(obj):
    return sorted(tuple(round(c, 4) for c in (obj.matrix_world @ v.co))
                  for v in obj.data.vertices)


before = {box.name: world_verts(box) for box in boxes}
t.check('poll_object_mode', bpy.ops.kelit_toolkit.auto_clean.poll())
result = bpy.ops.kelit_toolkit.auto_clean('EXEC_DEFAULT', scope='SCENE', origin_preset='BOTTOM_CENTER')
bpy.context.view_layer.update()
t.check('auto_clean_finished', list(result) == ['FINISHED'])

meshes = [o for o in scene.objects if o.type == 'MESH']
t.check('boxes_kept', len(meshes) == 3)
t.check('boxes_instanced', len({o.data.name for o in meshes}) == 1)
t.check('root_empty_removed', bpy.data.objects.get('asia_building') is None)
t.check('unused_empty_removed', bpy.data.objects.get('unused_null') is None)
# the rig empty survives (renamed by the naming step, hence the references)
t.check('camera_rig_kept', cam_rig.name in bpy.data.objects and camera.parent == cam_rig)
t.check('transforms_applied', all(
    all(abs(v - 1.0) < 1e-6 for v in o.scale) and all(abs(v) < 1e-6 for v in o.rotation_euler)
    for o in meshes))

# the geometry has not moved in world space, whatever happened to the data
after = sorted(world_verts(o) for o in meshes)
expected = sorted(before.values())
t.check('world_geometry_unchanged', all(
    all(abs(a[i] - b[i]) < 1e-4 for i in range(3))
    for verts_a, verts_b in zip(after, expected) for a, b in zip(verts_a, verts_b)))

mesh = meshes[0].data
xs = [v.co.x for v in mesh.vertices]
ys = [v.co.y for v in mesh.vertices]
t.check('origin_bottom_center', abs(min(v.co.z for v in mesh.vertices)) < 1e-6
        and abs(min(xs) + max(xs)) < 1e-6 and abs(min(ys) + max(ys)) < 1e-6)

textured = [m for m in bpy.data.materials if m.node_tree and any(
    n.type == 'TEX_IMAGE' for n in m.node_tree.nodes)]
t.check('materials_merged', len(textured) == 1 and textured[0].name.startswith('M_'))
t.check('names_normalized', all(o.name.startswith('SM_') for o in meshes)
        and mesh.name.startswith('SM_'))
t.check('options_remembered', scene.kelit_toolkit_settings.auto_clean_options_saved
        and scene.kelit_toolkit_settings.auto_clean_origin_preset == 'BOTTOM_CENTER')

t.finish('AUTOCLEAN')
