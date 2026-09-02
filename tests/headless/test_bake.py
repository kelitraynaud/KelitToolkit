"""High to Low Poly (Bake): slow test (Cycles bakes), skipped by --quick."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Harness  # noqa: E402

import bpy  # noqa: E402

t = Harness()
t.fresh_scene()

bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32, radius=1.0)
high = bpy.context.active_object
high.name = 'Rock'
subsurf = high.modifiers.new('Subdiv', 'SUBSURF')
subsurf.levels = 2
texture = bpy.data.textures.new('RockNoise', 'CLOUDS')
texture.noise_scale = 0.4
displace = high.modifiers.new('Displace', 'DISPLACE')
displace.texture = texture
displace.strength = 0.25
material = bpy.data.materials.new('RockMat')
material.use_nodes = True
principled = next(n for n in material.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
principled.inputs['Base Color'].default_value = (0.8, 0.25, 0.1, 1.0)
high.data.materials.append(material)

bpy.ops.mesh.primitive_cube_add(size=4, location=(0, 0, -2.6))
floor = bpy.context.active_object
floor.name = 'Floor'

t.deselect_all()
high.select_set(True)
bpy.context.view_layer.objects.active = high

result = bpy.ops.kelit_toolkit.convert_to_low_poly(
    'EXEC_DEFAULT', target_faces=1500, bake_resolution='512',
    bake_normal=True, bake_ao=True, bake_basecolor=True,
    bake_samples=8, keep_original=True)
t.check('bake_finished', list(result) == ['FINISHED'])

low = bpy.data.objects.get('Rock_LP')
t.check('low_exists', low is not None)
if low:
    t.check('low_budget', 0 < len(low.data.polygons) <= 1700, len(low.data.polygons))
    t.check('low_has_uv', bool(low.data.uv_layers))
    t.check('low_material', bool(low.data.materials) and low.data.materials[0].name == 'M_Rock_LP')
for suffix, colorspace in (('BaseColor', 'sRGB'), ('Normal', 'Non-Color'), ('AO', 'Non-Color')):
    image = bpy.data.images.get(f'T_Rock_{suffix}')
    varied = 0
    if image is not None:
        pixels = list(image.pixels[0:4000])
        varied = sum(1 for v in pixels if 0.001 < v < 0.999)
    t.check(f'map_{suffix}', image is not None and image.packed_file is not None
            and varied > 100 and image.colorspace_settings.name == colorspace)
t.check('original_hidden', high.hide_get() and high.hide_render)
t.check('floor_render_restored', floor.hide_render is False)

t.finish('BAKE')
