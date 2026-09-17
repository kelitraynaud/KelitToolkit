"""USD export post-processing: kinds, prim names, and the material policies
(single-sided by default, alpha as cutout unless the material is Blended)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Harness  # noqa: E402

import bpy  # noqa: E402

t = Harness()
usd_sync = t.submodule('operators.usd_sync')
try:
    from pxr import Usd, UsdGeom, UsdShade  # noqa: E402
except ImportError:
    t.note('pxr', 'unavailable, USD checks skipped')
    t.finish('USD')

scene = t.fresh_scene()
folder = tempfile.mkdtemp(prefix='kelit_usd_')


def saved_image(name):
    image = bpy.data.images.new(name, 8, 8)
    image.filepath_raw = os.path.join(folder, name + '.png')
    image.file_format = 'PNG'
    image.save()
    return image


def material_with_alpha(name, method):
    material = bpy.data.materials.new(name)
    material.surface_render_method = method
    tree = material.node_tree
    principled = next(n for n in tree.nodes if n.type == 'BSDF_PRINCIPLED')
    color = tree.nodes.new('ShaderNodeTexImage')
    color.image = saved_image(name + '_BaseColor')
    opacity = tree.nodes.new('ShaderNodeTexImage')
    opacity.image = saved_image(name + '_Opacity')
    tree.links.new(color.outputs['Color'], principled.inputs['Base Color'])
    tree.links.new(opacity.outputs['Color'], principled.inputs['Alpha'])
    return material


cutout = material_with_alpha('M_Cutout', 'DITHERED')
glass = material_with_alpha('M_Glass', 'BLENDED')
objects = []
for name, material in (('Body', cutout), ('Window', glass)):
    bpy.ops.mesh.primitive_cube_add()
    obj = bpy.context.active_object
    obj.name = name
    obj.data.materials.append(material)
    objects.append(obj)


def export(**options):
    path = os.path.join(folder, f"scene_{len(os.listdir(folder))}.usda")
    tagged, _hints, _data = usd_sync.export_usd_hierarchy(path, objects, True, **options)
    stage = Usd.Stage.Open(path)
    meshes = {p.GetParent().GetName(): UsdGeom.Mesh(p) for p in stage.Traverse()
              if p.GetTypeName() == 'Mesh'}
    shaders = {}
    for prim in stage.Traverse():
        if prim.GetTypeName() == 'Shader':
            shader = UsdShade.Shader(prim)
            if shader.GetIdAttr().Get() == 'UsdPreviewSurface':
                shaders[prim.GetParent().GetName()] = shader
    keep_alive.append(stage)   # prims expire when their stage is collected
    return tagged, meshes, shaders


keep_alive = []


def opacity_state(shader):
    opacity = shader.GetInput('opacity')
    threshold = shader.GetInput('opacityThreshold')
    return (opacity is not None and opacity.HasConnectedSource(),
            threshold.Get() if threshold is not None else None)


# defaults: single-sided everywhere, cutout for the dithered material,
# translucent kept for the blended one
tagged, meshes, shaders = export()
t.check('default_single_sided', all(not m.GetDoubleSidedAttr().Get() for m in meshes.values())
        and tagged.get('single_sided', 0) == 2, tagged)
t.check('default_cutout', opacity_state(shaders['M_Cutout']) == (True, 0.5), opacity_state(shaders['M_Cutout']))
t.check('default_blended_translucent', opacity_state(shaders['M_Glass']) == (True, None),
        opacity_state(shaders['M_Glass']))
t.check('kinds_tagged', tagged.get('component') == 2)

# two-sided everywhere, alpha ignored
tagged, meshes, shaders = export(two_sided='ON', alpha_mode='OPAQUE')
t.check('forced_double_sided', all(m.GetDoubleSidedAttr().Get() for m in meshes.values()))
t.check('alpha_ignored', all(opacity_state(s)[0] is False for s in shaders.values())
        and tagged.get('opaque') == 2)

# as in Blender: doubleSided left as exported, alpha translucent as exported
tagged, meshes, shaders = export(two_sided='BLENDER', alpha_mode='TRANSLUCENT')
t.check('blender_policy_untouched', tagged.get('single_sided', 0) == 0
        and tagged.get('masked', 0) == 0 and tagged.get('opaque', 0) == 0)

# ---- master material bridge: records, policies, textures, script ----
import json  # noqa: E402
ue_materials = t.submodule('operators.ue_materials')
records = usd_sync.extract_material_data([cutout, glass])
t.check('records_have_opacity', 'texture' in records['M_Cutout'].get('opacity', {})
        and records['M_Glass']['blended'] is True and records['M_Cutout']['blended'] is False
        and records['M_Cutout']['backface_culling'] is False)

policies = ue_materials.apply_material_policies_to_records(
    usd_sync.extract_material_data([cutout, glass]), 'OFF', 'AUTO')
t.check('policy_auto', policies['M_Cutout']['blend'] == 'MASKED'
        and policies['M_Glass']['blend'] == 'TRANSLUCENT'
        and policies['M_Cutout']['two_sided'] is False)
policies = ue_materials.apply_material_policies_to_records(
    usd_sync.extract_material_data([cutout, glass]), 'BLENDER', 'OPAQUE')
t.check('policy_opaque_blender_sides', policies['M_Cutout']['blend'] is None
        and 'opacity' not in policies['M_Cutout'] and policies['M_Cutout']['two_sided'] is True)

textures = ue_materials.export_material_textures(
    ue_materials.apply_material_policies_to_records(
        usd_sync.extract_material_data([cutout]), 'OFF', 'AUTO'),
    os.path.join(folder, 'textures'))
usages = {entry['name']: entry['usage'] for entry in textures}
t.check('textures_exported_with_usage', len(textures) == 2
        and sorted(usages.values()) == ['color', 'linear']
        and all(os.path.isfile(entry['file']) for entry in textures), usages)

# texture names mirror Unreal's: no extra underscore for digit-leading files
digit_image = bpy.data.images.new('25736b_R_Tuk_Tuk_Roughness.png', 4, 4)
t.check('texture_name_digit_stem',
        usd_sync.unreal_texture_name(digit_image) == 'T_25736b_R_Tuk_Tuk_Roughness',
        usd_sync.unreal_texture_name(digit_image))
white = ue_materials.write_linear_white(os.path.join(folder, 'textures'))
t.check('linear_white_written', os.path.isfile(white)
        and bpy.data.images.get('T_B2UE_LinearWhite') is None)

script = ue_materials.MATERIAL_SCRIPT.replace('__PAYLOAD__', json.dumps(json.dumps({'x': "it's"})))
try:
    compile(script, 'b2ue_materials', 'exec')
    t.check('material_script_compiles', True)
except SyntaxError as error:
    t.check('material_script_compiles', False, str(error))

t.finish('USD')
