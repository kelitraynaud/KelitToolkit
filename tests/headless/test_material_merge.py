"""Merge Duplicate Materials must only merge materials that render the same:
node settings (Math operation, Mix blend type, RGB colour, ramp stops,
Separate Color mode, UV Map, node group content) and image files are part
of the fingerprint. Viewports switched to Solid come back even on errors."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Harness  # noqa: E402

import bpy  # noqa: E402

t = Harness()
t.fresh_scene()
materials_mod = t.submodule('operators.materials')
utils = t.submodule('utils')
fingerprint = utils.material_fingerprint


def principled(material):
    return next(n for n in material.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')


def new_material(name):
    material = bpy.data.materials.new(name)
    material.use_fake_user = True
    return material


def math_material(name, operation):
    material = new_material(name)
    tree = material.node_tree
    node = tree.nodes.new('ShaderNodeMath')
    node.operation = operation
    node.inputs[0].default_value = 0.3
    node.inputs[1].default_value = 0.5
    tree.links.new(node.outputs[0], principled(material).inputs['Roughness'])
    return material


def rgb_material(name, colour):
    material = new_material(name)
    tree = material.node_tree
    node = tree.nodes.new('ShaderNodeRGB')
    node.outputs[0].default_value = colour
    tree.links.new(node.outputs[0], principled(material).inputs['Base Color'])
    return material


def ramp_material(name, position, colour=(1, 1, 1, 1)):
    material = new_material(name)
    tree = material.node_tree
    node = tree.nodes.new('ShaderNodeValToRGB')
    node.color_ramp.elements[1].position = position
    node.color_ramp.elements[1].color = colour
    tree.links.new(node.outputs['Color'], principled(material).inputs['Base Color'])
    return material


def mix_material(name, blend):
    material = new_material(name)
    tree = material.node_tree
    node = tree.nodes.new('ShaderNodeMix')
    node.data_type = 'RGBA'
    node.blend_type = blend
    tree.links.new(node.outputs['Result'], principled(material).inputs['Base Color'])
    return material


def separate_material(name, mode):
    material = new_material(name)
    tree = material.node_tree
    node = tree.nodes.new('ShaderNodeSeparateColor')
    node.mode = mode
    tree.links.new(node.outputs[0], principled(material).inputs['Roughness'])
    return material


def uvmap_material(name, uv_map):
    material = new_material(name)
    tree = material.node_tree
    node = tree.nodes.new('ShaderNodeUVMap')
    node.uv_map = uv_map
    tex = tree.nodes.new('ShaderNodeTexNoise')
    tree.links.new(node.outputs['UV'], tex.inputs['Vector'])
    tree.links.new(tex.outputs['Fac'], principled(material).inputs['Roughness'])
    return material


def group_material(name, operation):
    group = bpy.data.node_groups.new(name + '_Group', 'ShaderNodeTree')
    group.interface.new_socket('Value', in_out='OUTPUT', socket_type='NodeSocketFloat')
    output = group.nodes.new('NodeGroupOutput')
    math = group.nodes.new('ShaderNodeMath')
    math.operation = operation
    group.links.new(math.outputs[0], output.inputs[0])
    material = new_material(name)
    tree = material.node_tree
    node = tree.nodes.new('ShaderNodeGroup')
    node.node_tree = group
    tree.links.new(node.outputs[0], principled(material).inputs['Roughness'])
    return material


def curve_material(name, y):
    material = new_material(name)
    tree = material.node_tree
    node = tree.nodes.new('ShaderNodeRGBCurve')
    node.mapping.curves[3].points[0].location = (0.0, y)
    node.mapping.update()
    tree.links.new(node.outputs['Color'], principled(material).inputs['Base Color'])
    return material


def image_material(name, image):
    material = new_material(name)
    tree = material.node_tree
    node = tree.nodes.new('ShaderNodeTexImage')
    node.image = image
    tree.links.new(node.outputs['Color'], principled(material).inputs['Base Color'])
    return material


def save_png(path, value):
    image = bpy.data.images.new('tmp_save', 8, 8)
    image.pixels = [value] * (8 * 8 * 4)
    image.filepath_raw = path
    image.file_format = 'PNG'
    image.save()
    bpy.data.images.remove(image)


root = tempfile.mkdtemp(prefix='kelit_matmerge_')
folder_a = os.path.join(root, 'a')
folder_b = os.path.join(root, 'b')
os.makedirs(folder_a)
os.makedirs(folder_b)
save_png(os.path.join(folder_a, 'rooftop_01.png'), 0.2)
save_png(os.path.join(folder_a, 'tile.png'), 0.6)
save_png(os.path.join(folder_b, 'tile.png'), 0.6)   # same name, same bytes, other folder

# ---- the fingerprint itself ----
pairs = {
    'math': (math_material('MathAdd', 'ADD'), math_material('MathMul', 'MULTIPLY')),
    'rgb': (rgb_material('RgbRed', (1, 0, 0, 1)), rgb_material('RgbBlue', (0, 0, 1, 1))),
    'ramp': (ramp_material('RampA', 1.0), ramp_material('RampB', 0.4)),
    'mix': (mix_material('MixMix', 'MIX'), mix_material('MixMul', 'MULTIPLY')),
    'separate': (separate_material('SepRGB', 'RGB'), separate_material('SepHSV', 'HSV')),
    'uvmap': (uvmap_material('UvFirst', 'UVMap'), uvmap_material('UvSecond', 'Lightmap')),
    'group': (group_material('GroupAdd', 'ADD'), group_material('GroupMul', 'MULTIPLY')),
    'curve': (curve_material('CurveLow', 0.0), curve_material('CurveHigh', 0.3)),
    'ramp_colour': (ramp_material('RampWhite', 0.7), ramp_material('RampGreen', 0.7, (0, 1, 0, 1))),
}
image_a = bpy.data.images.load(os.path.join(folder_a, 'tile.png'), check_existing=False)
image_b = bpy.data.images.load(os.path.join(folder_b, 'tile.png'), check_existing=False)
pairs['folder'] = (image_material('FolderA', image_a), image_material('FolderB', image_b))
for label, (first, second) in pairs.items():
    t.check(f'fingerprint_differs_{label}', fingerprint(first) != fingerprint(second))

# same file loaded twice (rooftop_01.png and rooftop_01.png.001): one texture
twin_a = bpy.data.images.load(os.path.join(folder_a, 'rooftop_01.png'), check_existing=False)
twin_b = bpy.data.images.load(os.path.join(folder_a, 'rooftop_01.png'), check_existing=False)
roof = image_material('Roof', twin_a)
roof_copy = image_material('Roof.001', twin_b)
t.check('fingerprint_same_file_equal', fingerprint(roof) == fingerprint(roof_copy))
# cosmetic changes do not matter
for node in roof_copy.node_tree.nodes:
    node.location.x += 120
    node.label = 'moved'
t.check('fingerprint_ignores_layout', fingerprint(roof) == fingerprint(roof_copy))
# copies of every material compare equal to their source
math_copy = pairs['math'][0].copy()
group_copy = pairs['group'][1].copy()
t.check('fingerprint_copy_equal', fingerprint(math_copy) == fingerprint(pairs['math'][0])
        and fingerprint(group_copy) == fingerprint(pairs['group'][1]))
bpy.data.materials.remove(math_copy)
bpy.data.materials.remove(group_copy)
# the colour space of the image is part of the key
roof_linear = image_material('RoofLinear', bpy.data.images.load(
    os.path.join(folder_a, 'rooftop_01.png'), check_existing=False))
roof_linear.node_tree.nodes['Image Texture'].image.colorspace_settings.name = 'Non-Color'
t.check('fingerprint_colorspace', fingerprint(roof_linear) != fingerprint(roof))
# a group using itself through another group must not recurse forever
loop_a = bpy.data.node_groups.new('LoopA', 'ShaderNodeTree')
loop_b = bpy.data.node_groups.new('LoopB', 'ShaderNodeTree')
try:
    loop_a.nodes.new('ShaderNodeGroup').node_tree = loop_b
    loop_b.nodes.new('ShaderNodeGroup').node_tree = loop_a
except Exception:
    pass   # Blender may refuse the cycle itself: fine either way
loop_material = new_material('Loop')
loop_material.node_tree.nodes.new('ShaderNodeGroup').node_tree = loop_a
try:
    hash(fingerprint(loop_material))
    t.check('fingerprint_group_cycle', True)
except RecursionError:
    t.check('fingerprint_group_cycle', False)
bpy.data.materials.remove(loop_material)

# ---- the operator ----
names_apart = [m.name for pair in pairs.values() for m in pair]
result = bpy.ops.kelit_toolkit.merge_duplicate_materials('EXEC_DEFAULT')
t.check('merge_finished', list(result) == ['FINISHED'], list(result))
missing = [name for name in names_apart if bpy.data.materials.get(name) is None]
t.check('merge_keeps_different_apart', not missing, missing)
t.check('merge_same_file_merged', bpy.data.materials.get('Roof') is not None
        and bpy.data.materials.get('Roof.001') is None)

# ---- viewports come back even when the operation fails ----
calls = {'switched': 0, 'restored': 0}


def fake_viewports_to_solid(_context):
    calls['switched'] += 1

    def restore():
        calls['restored'] += 1
    return restore


def boom(*_args, **_kwargs):
    raise RuntimeError('simulated failure')


original_switch = materials_mod.viewports_to_solid
original_replace = materials_mod.replace_materials
original_pick_image = materials_mod.pick_image_to_keep
original_remove = getattr(materials_mod, 'remove_datablocks', None)
materials_mod.viewports_to_solid = fake_viewports_to_solid
try:
    # merge materials
    image_material('Again', twin_a)
    image_material('Again.001', twin_b)
    materials_mod.replace_materials = boom
    try:
        bpy.ops.kelit_toolkit.merge_duplicate_materials('EXEC_DEFAULT')
        raised = False
    except Exception:
        raised = True
    materials_mod.replace_materials = original_replace
    t.check('restore_after_material_error', raised and calls['switched'] == 1
            and calls['restored'] == 1, dict(calls))

    # merge images: fails once the viewports are switched
    def pick_then_fail(group):
        if calls['switched'] > calls['restored']:
            raise RuntimeError('simulated failure')
        return original_pick_image(group)
    materials_mod.pick_image_to_keep = pick_then_fail
    try:
        bpy.ops.kelit_toolkit.merge_duplicate_images('EXEC_DEFAULT')
        raised = False
    except Exception:
        raised = True
    materials_mod.pick_image_to_keep = original_pick_image
    t.check('restore_after_image_error', raised and calls['switched'] == 2
            and calls['restored'] == 2, dict(calls))

    # purge unused materials
    orphan = bpy.data.materials.new('Orphan')
    t.check('orphan_unused', orphan.users == 0)
    materials_mod.remove_datablocks = boom
    try:
        bpy.ops.kelit_toolkit.purge_unused_materials('EXEC_DEFAULT')
        raised = False
    except Exception:
        raised = True
    t.check('restore_after_purge_error', raised and calls['switched'] == 3
            and calls['restored'] == 3, dict(calls))
finally:
    materials_mod.viewports_to_solid = original_switch
    materials_mod.replace_materials = original_replace
    materials_mod.pick_image_to_keep = original_pick_image
    if original_remove is not None:
        materials_mod.remove_datablocks = original_remove

t.finish('MATERIALMERGE')
