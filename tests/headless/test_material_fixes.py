"""Texture name collisions, stale instance parameters, Alpha scalar maps,
geometry cache invalidation, per-actor materials, editor selection, real
PNG export and material UV plans. The Unreal-side scripts run against a
small fake 'unreal' module: nothing here talks to Unreal."""
import json
import os
import sys
import tempfile
import types
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Harness
import bpy

t = Harness()
sync = t.submodule('operators.usd_sync')
bridge = t.submodule('operators.ue_materials')
graph = t.submodule('operators.material_graph')
from KelitToolkit.operators import ue_remote as remote  # noqa: E402 (imported lazily by the add-on)
link = t.submodule('operators.unreal_link')
t.fresh_scene()
folder = tempfile.mkdtemp(prefix='kelit_matfix_')


def write_image_file(stem, color, file_format, extension):
    image = bpy.data.images.new('tmp_' + stem, 4, 4, alpha=True)
    image.pixels = list(color) * 16
    path = os.path.join(folder, stem + extension)
    image.filepath_raw = path
    image.file_format = file_format
    image.save()
    bpy.data.images.remove(image)
    return path


def load(path, name):
    image = bpy.data.images.load(path, check_existing=False)
    image.name = name
    return image


def textured_material(name, image, socket='Base Color', output='Color'):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    tree = material.node_tree
    bsdf = tree.nodes.get('Principled BSDF')
    node = tree.nodes.new('ShaderNodeTexImage')
    node.image = image
    tree.links.new(node.outputs[output], bsdf.inputs[socket])
    return material, node


def pixel(path):
    image = bpy.data.images.load(path, check_existing=False)
    try:
        return tuple(image.pixels[:4])
    finally:
        bpy.data.images.remove(image)


# ---------------------------------------------------------------------------
# 1. texture names: Wood, Wood.001, Wood.jpg, wood.png
# ---------------------------------------------------------------------------
red_file = write_image_file('wood_red', (1, 0, 0, 1), 'PNG', '.png')
green_file = write_image_file('wood_green', (0, 1, 0, 1), 'JPEG', '.jpg')
stone_file = write_image_file('stone', (.5, .5, .5, 1), 'PNG', '.png')
wood = load(red_file, 'Wood')
wood_copy = load(red_file, 'Wood.001')          # same file: same texture
wood_jpg = load(green_file, 'Wood.jpg')
wood_png = bpy.data.images.new('wood.png', 4, 4, alpha=True)
wood_png.pixels = [0, 0, 1, 1] * 16
wood_png.pack()
stone = load(stone_file, 'Stone')
materials = {}
for image in (wood, wood_copy, wood_jpg, wood_png, stone):
    materials[image.name], _node = textured_material('M_' + image.name.replace('.', '_'), image)
names = {image.name: sync.unreal_texture_name(image)
         for image in (wood, wood_copy, wood_jpg, wood_png, stone)}
t.check('collision_same_file_shares_name', names['Wood'] == names['Wood.001'], names)
t.check('collision_distinct_images_unique',
        len({names[n].casefold() for n in ('Wood', 'Wood.jpg', 'wood.png')}) == 3, names)
t.check('collision_hash_suffix', names['Wood'].startswith('T_Wood_H') and len(names['Wood']) == len('T_Wood_H') + 8, names)
t.check('no_collision_plain_name', names['Stone'] == 'T_Stone', names)
t.check('collision_name_stable', sync.unreal_texture_name(wood_jpg) == names['Wood.jpg'])

records = bridge.apply_material_policies_to_records(
    sync.extract_material_data(list(materials.values())), 'OFF', 'AUTO')
warnings = graph.record_warnings(records)
t.check('no_collision_warning_after_rename', not any('collide' in w for w in warnings), warnings)
entries = bridge.export_material_textures(records, os.path.join(folder, 'tex'))
by_name = {e['name']: e['file'] for e in entries}
t.check('every_distinct_image_exported', len(by_name) == 4, sorted(by_name))
expected = {'Wood': (1, 0, 0), 'Wood.jpg': (0, 1, 0), 'wood.png': (0, 0, 1), 'Stone': (.5, .5, .5)}
for image_name, color in expected.items():
    texture = records[materials[image_name].name]['base_color']['texture']
    value = pixel(by_name[texture])
    t.check('right_map_' + image_name, all(abs(a - b) < .08 for a, b in zip(value, color)),
            [texture, value])
fake_records = {
    'X': {'name': 'X', 'base_color': {'texture': 'T_Ghost', 'image': 'GhostA', 'channel': 'Color'}},
    'Y': {'name': 'Y', 'base_color': {'texture': 'T_ghost', 'image': 'GhostB', 'channel': 'Color'}},
}
t.check('remaining_collision_warned',
        any('Texture names collide' in w for w in graph.record_warnings(fake_records)))

# 7. JPEG source: real PNG bytes, the user's datablock untouched
with open(by_name[names['Wood.jpg']], 'rb') as handle:
    t.check('jpeg_written_as_png', handle.read(8) == b'\x89PNG\r\n\x1a\n')
t.check('jpeg_datablock_untouched', wood_jpg.file_format == 'JPEG'
        and wood_jpg.filepath == green_file and not wood_jpg.is_dirty)

# ---------------------------------------------------------------------------
# 3. Alpha output feeding scalar channels
# ---------------------------------------------------------------------------
orm = bpy.data.images.new('ORM_Alpha', 4, 4, alpha=True)
orm.colorspace_settings.name = 'Non-Color'
orm.pixels = [.1, .2, .3, .7] * 16
alpha_mat, alpha_node = textured_material('M_AlphaRough', orm, 'Roughness', 'Alpha')
alpha_mat.node_tree.links.new(alpha_node.outputs['Alpha'],
                              alpha_mat.node_tree.nodes['Principled BSDF'].inputs['Metallic'])
records = sync.extract_material_data([alpha_mat])
t.check('alpha_scalar_no_warning', not graph.record_warnings(records), graph.record_warnings(records))
entries = bridge.export_material_textures(records, os.path.join(folder, 'alpha'))
rough = records[alpha_mat.name]['roughness']['texture']
entry = next((e for e in entries if e['name'] == rough), None)
t.check('alpha_roughness_extracted', entry is not None and rough.endswith('_B2UE_AlphaMask')
        and entry['usage'] == 'linear' and abs(pixel(entry['file'])[0] - .7) < .01,
        [rough, entry and pixel(entry['file'])])
t.check('alpha_metallic_extracted',
        records[alpha_mat.name]['metallic']['texture'] == rough)
color_alpha, _node = textured_material('M_AlphaColor', orm, 'Base Color', 'Alpha')
t.check('alpha_color_warned', any('Alpha output' in w for w in
                                  graph.record_warnings(sync.extract_material_data([color_alpha]))))

# ---------------------------------------------------------------------------
# 7. UV plans: explicit active UV Map node, vectorized bake, linked objects
# ---------------------------------------------------------------------------
bpy.ops.mesh.primitive_grid_add(x_subdivisions=4, y_subdivisions=4)
grid = bpy.context.object
grid.name = 'UVGrid'
uv_name = grid.data.uv_layers.active.name
explicit, explicit_node = textured_material('M_ExplicitUV', stone)
uv_node = explicit.node_tree.nodes.new('ShaderNodeUVMap')
uv_node.uv_map = uv_name
explicit.node_tree.links.new(uv_node.outputs['UV'], explicit_node.inputs['Vector'])
second = explicit.node_tree.nodes.new('ShaderNodeTexImage')   # no vector: default UV
second.image = stone
explicit.node_tree.links.new(second.outputs['Color'], explicit.node_tree.nodes['Principled BSDF'].inputs['Roughness'])
mapped, mapped_node = textured_material('M_MappedUV', stone)
mapping = mapped.node_tree.nodes.new('ShaderNodeMapping')
coord = mapped.node_tree.nodes.new('ShaderNodeTexCoord')
mapped.node_tree.links.new(coord.outputs['UV'], mapping.inputs['Vector'])
mapped.node_tree.links.new(mapping.outputs['Vector'], mapped_node.inputs['Vector'])
mapping.inputs['Scale'].default_value = (2, 3, 1)
mapping.inputs['Location'].default_value = (.5, .25, 0)
grid.data.materials.append(explicit)
grid.data.materials.append(mapped)
for index, polygon in enumerate(grid.data.polygons):
    polygon.material_index = index % 2
records = sync.extract_material_data([explicit, mapped])
t.check('explicit_active_uv_is_default',
        graph.material_uv_plan(records[explicit.name]) == {'map': '', 'matrix': graph.IDENTITY}
        and not graph.record_warnings({explicit.name: records[explicit.name]}),
        graph.record_warnings({explicit.name: records[explicit.name]}))
source_uv = [tuple(v.uv) for v in grid.data.uv_layers.active.data]
expected_uv = list(source_uv)
for polygon in grid.data.polygons:
    for loop in polygon.loop_indices:
        if polygon.material_index == 1:
            u, v = source_uv[loop]
            expected_uv[loop] = (u * 2 + .5, v * 3 + .25)
with graph.temporary_material_uvs([grid]):
    baked = [tuple(v.uv) for v in grid.data.uv_layers['B2UE_MaterialUV'].data]
t.check('vectorized_uv_bake', all(abs(a[0] - b[0]) < 1e-5 and abs(a[1] - b[1]) < 1e-5
                                  for a, b in zip(baked, expected_uv)) and len(baked) == len(expected_uv))

library_path = os.path.join(folder, 'library.blend')
bpy.ops.mesh.primitive_plane_add()
lib_obj = bpy.context.object
lib_obj.name = 'LibObject'
lib_image = bpy.data.images.new('LibImage', 4, 4)
lib_mat, lib_node = textured_material('M_LibMapped', lib_image)
lib_mapping = lib_mat.node_tree.nodes.new('ShaderNodeMapping')
lib_mapping.inputs['Scale'].default_value = (2, 2, 1)
lib_mat.node_tree.links.new(lib_mapping.outputs['Vector'], lib_node.inputs['Vector'])
lib_obj.data.materials.append(lib_mat)
bpy.data.libraries.write(library_path, {lib_obj}, fake_user=True)
lib_mesh = lib_obj.data
bpy.data.objects.remove(lib_obj)
bpy.data.meshes.remove(lib_mesh)
bpy.data.materials.remove(lib_mat)
bpy.data.images.remove(lib_image)
with bpy.data.libraries.load(library_path, link=True) as (data_from, data_to):
    data_to.objects = ['LibObject']
linked = data_to.objects[0]
try:
    with graph.temporary_material_uvs([linked]):
        pass
    linked_ok = linked.data.library is not None
except Exception as error:
    linked_ok = False
    t.note('linked_error', str(error))
t.check('linked_object_skipped', linked_ok)

# ---------------------------------------------------------------------------
# Fake Unreal for the Unreal-side material script (fixes 2 and 5)
# ---------------------------------------------------------------------------
class FakeObject:
    def __init__(self, name, path=None):
        self.name, self.path = name, path or name
        self.props, self.meta = {}, {}

    def get_name(self):
        return self.name

    def get_path_name(self):
        return self.path

    def get_editor_property(self, key):
        return self.props.get(key)

    def set_editor_property(self, key, value):
        self.props[key] = value


class LinearColor:
    def __init__(self, r=0.0, g=0.0, b=0.0, a=1.0):
        self.r, self.g, self.b, self.a = float(r), float(g), float(b), float(a)

    def __eq__(self, other):
        return isinstance(other, LinearColor) and (self.r, self.g, self.b, self.a) == (other.r, other.g, other.b, other.a)

    def __repr__(self):
        return 'LinearColor(%g, %g, %g, %g)' % (self.r, self.g, self.b, self.a)


def fake_unreal(assets, actors, logs, import_error=None):
    module = types.ModuleType('unreal')
    module.log = lambda text: logs.append(str(text))
    module.LinearColor = LinearColor

    class Material(FakeObject):
        pass

    class Texture(FakeObject):
        pass

    class MaterialInstanceConstant(FakeObject):
        def __init__(self, *args):
            super().__init__(*args)
            self.params = {}
            self.props['base_property_overrides'] = FakeObject('overrides')

    class StaticMesh(FakeObject):
        def __init__(self, *args):
            super().__init__(*args)
            self.materials = {}

        def get_material(self, index):
            return self.materials.get(index)

        def set_material(self, index, material):
            self.materials[index] = material

    class StaticMeshComponent:
        def __init__(self, mesh):
            self.static_mesh = mesh
            self.overrides = [None]

        def get_editor_property(self, key):
            return list(self.overrides)

        def set_material(self, index, material):
            self.overrides[index] = material

    class Actor:
        def __init__(self, label, tags, component):
            self.label, self.tags, self.component = label, tags, component

        def get_component_by_class(self, cls):
            return self.component if cls is StaticMeshComponent else None

        def get_actor_label(self):
            return self.label

    for cls in (Material, Texture, MaterialInstanceConstant, StaticMesh, StaticMeshComponent):
        setattr(module, cls.__name__, cls)
    module.SkeletalMesh = type('SkeletalMesh', (FakeObject,), {})
    module.SkeletalMeshComponent = type('SkeletalMeshComponent', (), {})
    module.Actor = Actor
    module.EditorActorSubsystem = type('EditorActorSubsystem', (), {})
    module.get_editor_subsystem = lambda cls: types.SimpleNamespace(get_all_level_actors=lambda: actors)
    module.BlendMode = types.SimpleNamespace(BLEND_MASKED='MASKED', BLEND_TRANSLUCENT='TRANSLUCENT')
    module.MaterialSamplerType = types.SimpleNamespace(SAMPLERTYPE_LINEAR_COLOR=1, SAMPLERTYPE_NORMAL=2)
    module.MaterialInstanceConstantFactoryNew = lambda: None
    module.UsdStageImportOptions = type('UsdStageImportOptions', (FakeObject,), {'__init__': lambda self: FakeObject.__init__(self, 'options')})
    module.AssetImportTask = type('AssetImportTask', (), {})

    def create_asset(name, folder_path, cls, factory):
        asset = cls(name, folder_path + '/' + name)
        assets[asset.path] = asset
        return asset

    def import_asset_tasks(tasks):
        if import_error:
            raise RuntimeError(import_error)

    tools = types.SimpleNamespace(create_asset=create_asset, import_asset_tasks=import_asset_tasks)
    module.AssetToolsHelpers = types.SimpleNamespace(get_asset_tools=lambda: tools)

    def load_asset(path):
        return assets.get(path.split('.')[0])

    def remove_tag(asset, tag):
        asset.meta.pop(tag, None)

    def set_tag(asset, tag, value):
        asset.meta[tag] = value

    module.EditorAssetLibrary = types.SimpleNamespace(
        does_asset_exist=lambda path: path.split('.')[0] in assets,
        load_asset=load_asset,
        get_metadata_tag=lambda asset, tag: asset.meta.get(tag, ''),
        set_metadata_tag=set_tag,
        remove_metadata_tag=remove_tag,
        save_asset=lambda *a, **k: True,
        save_loaded_asset=lambda *a, **k: True,
        save_directory=lambda *a, **k: True,
        does_directory_exist=lambda path: False,
        list_assets=lambda *a, **k: [])

    def names(kind):
        return lambda material: [n for (k, n) in material.defaults if k == kind]

    def default(kind):
        return lambda material, name: material.defaults.get((kind, name))

    def instance_get(kind):
        def get(instance, name):
            if (kind, name) in instance.params:
                return instance.params[(kind, name)]
            return instance.get_editor_property('parent').defaults.get((kind, name))
        return get

    def instance_set(kind):
        def set_(instance, name, value):
            instance.params[(kind, name)] = value
            return False   # UE 5.8 returns False even on success
        return set_

    mel = types.SimpleNamespace(update_material_instance=lambda instance: None)
    for kind in ('scalar', 'vector', 'texture', 'static_switch'):
        setattr(mel, 'get_%s_parameter_names' % kind, names(kind))
        setattr(mel, 'get_material_default_%s_parameter_value' % kind, default(kind))
        setattr(mel, 'get_material_instance_%s_parameter_value' % kind, instance_get(kind))
        setattr(mel, 'set_material_instance_%s_parameter_value' % kind, instance_set(kind))
    module.MaterialEditingLibrary = mel
    return module


def run_fake(script, module):
    previous = sys.modules.get('unreal')
    sys.modules['unreal'] = module
    try:
        exec(compile(script, 'b2ue_fake', 'exec'), {'__name__': 'b2ue_fake'})
    finally:
        if previous is None:
            sys.modules.pop('unreal', None)
        else:
            sys.modules['unreal'] = previous


assets, logs, actors = {}, [], []
module = fake_unreal(assets, actors, logs)
master = module.Material('M_B2UE_Master', '/Game/BlenderSync/M_B2UE_Master')
default_white = module.Texture('WhiteSquare', '/Engine/White')
default_linear = module.Texture('LinearWhite', '/Game/BlenderSync/T_B2UE_LinearWhite')
default_normal = module.Texture('DefaultNormal', '/Engine/DefaultNormal')
master.defaults = {
    ('scalar', 'UVTiling'): 1.0, ('scalar', 'Roughness'): .5, ('scalar', 'Metallic'): 0.0,
    ('scalar', 'EmissiveStrength'): 0.0, ('scalar', 'DisplacementScale'): 0.0, ('scalar', 'Opacity'): 1.0,
    ('scalar', 'B2UE_MasterVersion'): 2.0,
    ('vector', 'BaseColorTint'): LinearColor(1, 1, 1, 1), ('vector', 'EmissiveTint'): LinearColor(0, 0, 0, 1),
    ('texture', 'BaseColorMap'): default_white, ('texture', 'EmissiveMap'): default_white,
    ('texture', 'RoughnessMap'): default_linear, ('texture', 'MetallicMap'): default_linear,
    ('texture', 'DisplacementMap'): default_linear, ('texture', 'OpacityMap'): default_linear,
    ('texture', 'NormalMap'): default_normal,
}
for switch in ('UseBaseColorMap', 'UseRoughnessMap', 'UseMetallicMap', 'UseNormalMap',
               'UseEmissiveMap', 'UseOpacityMap'):
    master.defaults[('static_switch', switch)] = False
assets[master.path] = master
assets[default_linear.path] = default_linear
texture_folder = '/Game/BlenderSync/Scene/Textures'
texture_assets = {}
for name in ('T_Wood', 'T_Rough'):
    texture = module.Texture(name, texture_folder + '/' + name)
    texture.meta['B2UE_ContentHash'] = 'hash_' + name
    assets[texture.path] = texture_assets[name] = texture
instance_folder = '/Game/BlenderSync/Scene/MaterialInstances'
stale = module.MaterialInstanceConstant('MI_MatMap', instance_folder + '/MI_MatMap')
stale.props['parent'] = master
stale.params.update({
    ('vector', 'BaseColorTint'): LinearColor(1, 0, 0, 1),
    ('static_switch', 'UseEmissiveMap'): True, ('scalar', 'EmissiveStrength'): 5.0,
    ('vector', 'EmissiveTint'): LinearColor(0, 1, 0, 1), ('texture', 'EmissiveMap'): texture_assets['T_Wood'],
    ('static_switch', 'UseRoughnessMap'): True, ('texture', 'RoughnessMap'): texture_assets['T_Rough'],
    ('scalar', 'UVTiling'): 4.0,
})
assets[stale.path] = stale
shared_mesh = module.StaticMesh('SM_Shared', '/Game/BlenderSync/Scene/SM_Shared')
assets[shared_mesh.path] = shared_mesh
components = {}
for object_name in ('A', 'B', 'C'):
    components[object_name] = module.StaticMeshComponent(shared_mesh)
    actors.append(module.Actor(object_name, ['B2UE:obj:' + object_name], components[object_name]))
components['C'].overrides[0] = FakeObject('stale override')
identity_uv = {'map': '', 'matrix': graph.IDENTITY}
payload = {
    'master_path': master.path,
    'linear_white': {'file': 'unused', 'path': default_linear.path},
    'instance_folder': instance_folder,
    'texture_folder': texture_folder,
    'fallback_texture_folders': [],
    'textures': [{'name': n, 'file': 'unused', 'usage': 'color', 'content_hash': 'hash_' + n}
                 for n in ('T_Wood', 'T_Rough')],
    'materials': {
        'MatMap': {'name': 'MatMap', 'blend': None, 'two_sided': False, 'emissive_strength': 0.0,
                   'base_color': {'texture': 'T_Wood', 'image': 'Wood', 'channel': 'Color', 'uv': identity_uv},
                   'roughness': {'value': .3}},
        'MatValue': {'name': 'MatValue', 'blend': None, 'two_sided': False, 'emissive_strength': 2.0,
                     'base_color': {'value': [.2, .4, .6]},
                     'roughness': {'texture': 'T_Rough', 'image': 'Rough', 'channel': 'Color', 'uv': identity_uv},
                     'emissive': {'value': [1, .5, 0]}},
    },
    'slots': {'A': ['MatMap'], 'B': ['MatValue'], 'C': ['MatMap']},
    'skeletal_rigs': [], 'skeletal_paths': {}, 'rig_uv_signatures': {},
    'scene_folder': '/Game/BlenderSync/Scene', 'uv_signatures': {}, 'mesh_names': {},
}
run_fake(bridge.MATERIAL_SCRIPT.replace('__PAYLOAD__', json.dumps(json.dumps(payload))), module)
result_line = next((line for line in logs if 'B2UE_MAT_RESULT' in line), None)
error_line = next((line for line in logs if 'B2UE_MAT_ERROR' in line), None)
t.check('fake_material_script_ran', result_line is not None and error_line is None, error_line)
result = json.loads(result_line.split('B2UE_MAT_RESULT', 1)[1]) if result_line else {}
mi_map = assets.get(instance_folder + '/MI_MatMap')
mi_value = assets.get(instance_folder + '/MI_MatValue')


def param(instance, kind, name):
    """Effective value: the instance override, else the master default."""
    if instance is None:
        return None
    if (kind, name) in instance.params:
        return instance.params[(kind, name)]
    return master.defaults.get((kind, name))


t.check('map_resets_tint_white', param(mi_map, 'vector', 'BaseColorTint') == LinearColor(1, 1, 1, 1)
        and param(mi_map, 'static_switch', 'UseBaseColorMap') is True
        and param(mi_map, 'texture', 'BaseColorMap') is texture_assets['T_Wood'])
t.check('emission_off_resets', param(mi_map, 'static_switch', 'UseEmissiveMap') is False
        and param(mi_map, 'scalar', 'EmissiveStrength') == 0.0
        and param(mi_map, 'vector', 'EmissiveTint') == LinearColor(0, 0, 0, 1)
        and param(mi_map, 'texture', 'EmissiveMap') is default_white,
        {k[1]: repr(v) for k, v in (mi_map.params.items() if mi_map else [])})
t.check('map_to_value_resets_map', param(mi_map, 'static_switch', 'UseRoughnessMap') is False
        and param(mi_map, 'texture', 'RoughnessMap') is default_linear
        and abs(param(mi_map, 'scalar', 'Roughness') - .3) < 1e-6)
t.check('uv_tiling_reset', param(mi_map, 'scalar', 'UVTiling') == 1.0)
t.check('value_material_written', param(mi_value, 'vector', 'BaseColorTint') == LinearColor(.2, .4, .6, 1)
        and param(mi_value, 'static_switch', 'UseRoughnessMap') is True
        and param(mi_value, 'scalar', 'Roughness') == .5
        and param(mi_value, 'scalar', 'EmissiveStrength') == 2.0
        and param(mi_value, 'vector', 'EmissiveTint') == LinearColor(1, .5, 0, 1)
        and param(mi_value, 'static_switch', 'UseEmissiveMap') is False)
t.check('shared_asset_keeps_common', shared_mesh.materials.get(0) is mi_map)
t.check('different_actor_overridden', components['B'].overrides[0] is mi_value
        and components['A'].overrides[0] is None and components['C'].overrides[0] is None,
        [repr(components[n].overrides) for n in 'ABC'])
t.check('override_counted', result.get('actor_overrides') == 1 and not result.get('errors'), result)

# ---------------------------------------------------------------------------
# 4. geometry cache invalidated before a re-import that then fails
# ---------------------------------------------------------------------------
cache_file = os.path.join(folder, 'cache.json')
with open(cache_file, 'w', encoding='utf-8') as handle:
    json.dump({'version': 1, 'fingerprint': 'old', 'assets': ['/Game/S/SM_Old.SM_Old']}, handle)
assets, logs = {}, []
module = fake_unreal(assets, [], logs, import_error='import exploded')
old_mesh = module.StaticMesh('SM_Old', '/Game/S/SM_Old')
old_mesh.meta['B2UE_StaticImportHash'] = 'old'
assets[old_mesh.path] = old_mesh
sync_payload = {'usd_file': 'C:/nothing.usd', 'static_fingerprint': 'new', 'cache_file': cache_file,
                'force_reimport': False, 'skeletal': [], 'content_folder': '/Game/S', 'scene_name': 'S',
                'import_materials': False, 'objects': [], 'place_in_level': False}
run_fake(sync.UNREAL_SCRIPT_TEMPLATE.replace('__PAYLOAD__', json.dumps(json.dumps(sync_payload))), module)
t.check('failed_import_reported', any('import exploded' in line for line in logs), logs[-3:])
t.check('cache_file_invalidated', not os.path.exists(cache_file))
t.check('import_hash_cleared', 'B2UE_StaticImportHash' not in old_mesh.meta)

# ---------------------------------------------------------------------------
# 6. editor selection with a fake discovery list
# ---------------------------------------------------------------------------
ONE = {'node_id': '1', 'project_root': 'C:/One/', 'project_name': 'One'}
TWO = {'node_id': '2', 'project_root': 'C:/Two/', 'project_name': 'Two'}


def discovery(timeline):
    clock = [0.0]

    def poll():
        current = []
        for start, nodes in timeline:
            if clock[0] >= start - 1e-9:
                current = nodes
        return current

    def sleep(seconds):
        clock[0] += seconds
    return dict(poll=poll, clock=lambda: clock[0], sleep=sleep), clock


def outcome(timeline, **kwargs):
    fake, clock = discovery(timeline)
    try:
        node = remote.wait_for_target(fake['poll'], clock=fake['clock'], sleep=fake['sleep'], **kwargs)
        return node['node_id'], clock[0]
    except Exception as error:
        return type(error).__name__ + ': ' + str(error), clock[0]


late_second = outcome([(0.1, [ONE]), (0.9, [ONE, TWO])])
t.check('auto_listens_full_window', late_second[0].startswith('TargetSelectionError')
        and 'Choose Project' in late_second[0], late_second)
single = outcome([(0.1, [ONE])])
t.check('auto_single_editor', single[0] == '1' and single[1] >= remote.DISCOVERY_WINDOW - 1e-6, single)
busy = outcome([(0.1, [ONE]), (6.0, [ONE, TWO])], project='c:/two')
t.check('chosen_busy_editor_waited', busy[0] == '2' and busy[1] >= 5.9, busy)
never = outcome([(0.1, [ONE])], project='C:/Two', patience=3.0)
t.check('chosen_missing_refused', never[0].startswith('TargetSelectionError'), never)
nothing = outcome([], patience=2.0)
t.check('nobody_is_connection_error', nothing[0].startswith('ConnectionError'), nothing)
pinned = outcome([(0.1, [ONE, TWO])], pinned=TWO)
t.check('pinned_editor', pinned[0] == '2', pinned)

# no editor at all: the Send opens the Connection Doctor again
opened, reports = [], []
saved = (remote.resolve_target, link.open_connection_doctor)


def no_editor(*_args, **_kwargs):
    raise ConnectionError('No Unreal editor answered discovery')


remote.resolve_target = no_editor
link.open_connection_doctor = lambda: opened.append(True) or True
try:
    fake_operator = types.SimpleNamespace(report=lambda kind, message: reports.append(message))
    target = sync.UNREAL_OT_usd_scene_sync._resolve_target_or_report(fake_operator)
finally:
    remote.resolve_target, link.open_connection_doctor = saved
t.check('no_editor_opens_doctor', target is None and opened
        and any(link.NO_EDITOR_MESSAGE in r for r in reports), reports)

# Choose Project: stable enum list, readable discovery errors
items = link._PROJECT_CHOICES
link.update_project_choices([ONE, TWO])
first = list(link._PROJECT_CHOICES)
link.update_project_choices([TWO])
t.check('enum_list_stable', link._PROJECT_CHOICES is items
        and [i[0] for i in first] == ['AUTO', 'C:/One/', 'C:/Two/']
        and [i[0] for i in link._PROJECT_CHOICES] == ['AUTO', 'C:/Two/']
        and link._PROJECT_CHOICES[1] is first[2])
saved_find = remote.find_editors


def broken_find(*_args, **_kwargs):
    raise OSError('port 6766 already in use')


remote.find_editors = broken_find
choose_reports = []
try:
    fake_operator = types.SimpleNamespace(report=lambda kind, text: choose_reports.append(text))
    state = link.UNREAL_OT_choose_project.invoke(fake_operator, bpy.context, None)
except Exception as error:
    state = 'raised ' + repr(error)
finally:
    remote.find_editors = saved_find
t.check('choose_project_readable_error', state == {'CANCELLED'} and any(
    'Could not look for running Unreal editors' in r and 'port 6766' in r for r in choose_reports),
    [str(state)] + choose_reports)

# ---------------------------------------------------------------------------
# 7. a material export error during the Send is a warning, not a traceback
# ---------------------------------------------------------------------------
for obj in list(bpy.data.objects):
    obj.select_set(False)
bpy.ops.mesh.primitive_plane_add()
plane = bpy.context.object
plane.name = 'SM_BrokenAlpha'
broken = bpy.data.images.new('BrokenAlpha', 4, 4, alpha=True)
broken.source = 'FILE'
broken.filepath = os.path.join(folder, 'does_not_exist.png')
broken_mat, _node = textured_material('M_BrokenAlpha', broken, 'Alpha', 'Alpha')
plane.data.materials.append(broken_mat)
plane.select_set(True)
bpy.context.view_layer.objects.active = plane
saved = (remote.resolve_target, sync.run_unreal_python)
remote.resolve_target = lambda *a, **k: dict(ONE, project_root=folder)
sync.run_unreal_python = lambda commands: (True, 'B2UE_RESULT ' + json.dumps(
    {'assets': 1, 'spawned': 0, 'attached': 0, 'missing': []}))
try:
    state = bpy.ops.kelit_toolkit.usd_scene_sync(
        'EXEC_DEFAULT', source='SELECTED', import_materials=True, material_source='MASTER',
        place_in_level=False, include_animation=False)
    sync_error = ''
except RuntimeError as error:
    state, sync_error = None, str(error)
finally:
    remote.resolve_target, sync.run_unreal_python = saved
t.check('material_error_is_warning', state == {'FINISHED'}, sync_error[-400:])

t.finish('material fixes')
