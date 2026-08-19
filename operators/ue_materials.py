"""Material bridge - rebuild Blender materials as clean Unreal instances.

Unreal's USD import produces materials parented to UsdPreviewSurface variants:
translucent as soon as an opacity input exists, hard to reason about, and
replaced on every re-import. This module instead:

1. reads each Blender material as a flat PBR record (see usd_sync),
2. makes sure a single, readable master material exists in the project,
3. creates one MaterialInstanceConstant per Blender material under it,
4. assigns those instances to the meshes, slot by slot.

The master is deliberately single-layer: one texture (or one constant) per
channel, plus static switches so unused samplers cost nothing. Adding
displacement/tessellation later means editing the master once - every
instance inherits it.
"""

import json
import os

import bpy

from .unreal_link import run_unreal_python
from .usd_sync import (
    collect_materials,
    collect_scene_objects,
    extract_material_data,
    build_material_slots,
    get_scene_name,
    get_staging_dir,
    is_exportable,
)


def export_material_textures(records, out_dir):
    """
    Write every texture the materials need to *out_dir*, one file per Unreal
    asset name.

    We do this instead of relying on the USD round-trip: Blender silently
    skips some packed images, and Unreal deduplicates byte-identical files
    into a single asset - so two materials that share a flat colour map end
    up with one of them pointing at nothing. Saving a copy per asset name
    keeps the mapping one-to-one and leaves the .blend untouched.
    """
    os.makedirs(out_dir, exist_ok=True)
    wanted = {}
    for record in records.values():
        for key in ('base_color', 'roughness', 'metallic', 'normal', 'emissive'):
            data = record.get(key)
            if data and 'texture' in data:
                wanted[data['texture']] = data['image']

    exported = []
    for ue_name, image_name in sorted(wanted.items()):
        image = bpy.data.images.get(image_name)
        if image is None:
            continue
        path = os.path.join(out_dir, ue_name + '.png')
        try:
            # save_copy leaves the datablock's own filepath/format alone
            image.save(filepath=path, save_copy=True)
        except (RuntimeError, OSError) as error:
            print(f"Build Material Instances - could not write {ue_name}: {error}")
            continue
        exported.append({'name': ue_name, 'file': path.replace('\\', '/')})
    return exported


# ============================================================================
# UNREAL-SIDE SCRIPT
# ============================================================================

MATERIAL_SCRIPT = '''
import json
import traceback
import unreal

PAYLOAD = json.loads(r\'\'\'__PAYLOAD__\'\'\')

MEL = unreal.MaterialEditingLibrary
EAL = unreal.EditorAssetLibrary
TOOLS = unreal.AssetToolsHelpers.get_asset_tools()

WHITE = "/Engine/EngineResources/WhiteSquareTexture"
FLAT_NORMAL = "/Engine/EngineMaterials/DefaultNormal"


def log(message):
    text = "[B2UE-MAT] " + str(message)
    unreal.log(text)
    print(text)


def build_master(path):
    """Create the single-layer master material: one map or one value per channel."""
    folder, name = path.rsplit("/", 1)
    material = TOOLS.create_asset(name, folder, unreal.Material, unreal.MaterialFactoryNew())

    def node(cls, x, y):
        return MEL.create_material_expression(material, cls, x, y)

    def texture_param(param, x, y, default, group, sampler=None):
        node_ = node(unreal.MaterialExpressionTextureSampleParameter2D, x, y)
        node_.set_editor_property("parameter_name", param)
        node_.set_editor_property("group", group)
        tex = EAL.load_asset(default)
        if tex:
            node_.set_editor_property("texture", tex)
        if sampler is not None:
            node_.set_editor_property("sampler_type", sampler)
        return node_

    def scalar(param, value, x, y, group):
        node_ = node(unreal.MaterialExpressionScalarParameter, x, y)
        node_.set_editor_property("parameter_name", param)
        node_.set_editor_property("default_value", value)
        node_.set_editor_property("group", group)
        return node_

    def vector(param, rgba, x, y, group):
        node_ = node(unreal.MaterialExpressionVectorParameter, x, y)
        node_.set_editor_property("parameter_name", param)
        node_.set_editor_property("default_value", unreal.LinearColor(*rgba))
        node_.set_editor_property("group", group)
        return node_

    def switch(param, default, x, y, group):
        node_ = node(unreal.MaterialExpressionStaticSwitchParameter, x, y)
        node_.set_editor_property("parameter_name", param)
        node_.set_editor_property("default_value", default)
        node_.set_editor_property("group", group)
        return node_

    # --- shared UVs -------------------------------------------------------
    tex_coord = node(unreal.MaterialExpressionTextureCoordinate, -1500, 0)
    tiling = scalar("UVTiling", 1.0, -1500, 150, "00 - UV")
    uv = node(unreal.MaterialExpressionMultiply, -1300, 60)
    MEL.connect_material_expressions(tex_coord, "", uv, "A")
    MEL.connect_material_expressions(tiling, "", uv, "B")

    def wire_uv(sampler_node):
        MEL.connect_material_expressions(uv, "", sampler_node, "UVs")

    # --- base colour ------------------------------------------------------
    base_map = texture_param("BaseColorMap", -1000, -600, WHITE, "01 - Base Color")
    wire_uv(base_map)
    tint = vector("BaseColorTint", (1.0, 1.0, 1.0, 1.0), -1000, -350, "01 - Base Color")
    tinted = node(unreal.MaterialExpressionMultiply, -700, -520)
    MEL.connect_material_expressions(base_map, "RGB", tinted, "A")
    MEL.connect_material_expressions(tint, "", tinted, "B")
    base_switch = switch("UseBaseColorMap", False, -450, -500, "01 - Base Color")
    MEL.connect_material_expressions(tinted, "", base_switch, "True")
    MEL.connect_material_expressions(tint, "", base_switch, "False")
    MEL.connect_material_property(base_switch, "", unreal.MaterialProperty.MP_BASE_COLOR)

    # --- roughness / metallic --------------------------------------------
    rough_map = texture_param("RoughnessMap", -1000, -150, WHITE, "02 - Surface",
                              unreal.MaterialSamplerType.SAMPLERTYPE_LINEAR_COLOR)
    wire_uv(rough_map)
    rough_value = scalar("Roughness", 0.5, -1000, 60, "02 - Surface")
    rough_switch = switch("UseRoughnessMap", False, -450, -120, "02 - Surface")
    MEL.connect_material_expressions(rough_map, "R", rough_switch, "True")
    MEL.connect_material_expressions(rough_value, "", rough_switch, "False")
    MEL.connect_material_property(rough_switch, "", unreal.MaterialProperty.MP_ROUGHNESS)

    metal_map = texture_param("MetallicMap", -1000, 220, WHITE, "02 - Surface",
                              unreal.MaterialSamplerType.SAMPLERTYPE_LINEAR_COLOR)
    wire_uv(metal_map)
    metal_value = scalar("Metallic", 0.0, -1000, 430, "02 - Surface")
    metal_switch = switch("UseMetallicMap", False, -450, 250, "02 - Surface")
    MEL.connect_material_expressions(metal_map, "R", metal_switch, "True")
    MEL.connect_material_expressions(metal_value, "", metal_switch, "False")
    MEL.connect_material_property(metal_switch, "", unreal.MaterialProperty.MP_METALLIC)

    # --- normal -----------------------------------------------------------
    normal_map = texture_param("NormalMap", -1000, 620, FLAT_NORMAL, "03 - Normal",
                               unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL)
    wire_uv(normal_map)
    flat = node(unreal.MaterialExpressionConstant3Vector, -1000, 850)
    flat.set_editor_property("constant", unreal.LinearColor(0.0, 0.0, 1.0, 1.0))
    normal_switch = switch("UseNormalMap", False, -450, 650, "03 - Normal")
    MEL.connect_material_expressions(normal_map, "RGB", normal_switch, "True")
    MEL.connect_material_expressions(flat, "", normal_switch, "False")
    MEL.connect_material_property(normal_switch, "", unreal.MaterialProperty.MP_NORMAL)

    # --- emissive ---------------------------------------------------------
    emissive_map = texture_param("EmissiveMap", -1000, 1050, WHITE, "04 - Emissive")
    wire_uv(emissive_map)
    emissive_tint = vector("EmissiveTint", (0.0, 0.0, 0.0, 1.0), -1000, 1280, "04 - Emissive")
    emissive_switch = switch("UseEmissiveMap", False, -700, 1080, "04 - Emissive")
    MEL.connect_material_expressions(emissive_map, "RGB", emissive_switch, "True")
    MEL.connect_material_expressions(emissive_tint, "", emissive_switch, "False")
    emissive_strength = scalar("EmissiveStrength", 0.0, -700, 1300, "04 - Emissive")
    emissive = node(unreal.MaterialExpressionMultiply, -450, 1150)
    MEL.connect_material_expressions(emissive_switch, "", emissive, "A")
    MEL.connect_material_expressions(emissive_strength, "", emissive, "B")
    MEL.connect_material_property(emissive, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)

    # --- displacement, ready for Nanite tessellation later ----------------
    disp_map = texture_param("DisplacementMap", -1000, 1500, WHITE, "05 - Displacement",
                             unreal.MaterialSamplerType.SAMPLERTYPE_LINEAR_COLOR)
    wire_uv(disp_map)
    disp_scale = scalar("DisplacementScale", 0.0, -1000, 1700, "05 - Displacement")
    disp = node(unreal.MaterialExpressionMultiply, -450, 1550)
    MEL.connect_material_expressions(disp_map, "R", disp, "A")
    MEL.connect_material_expressions(disp_scale, "", disp, "B")
    try:
        MEL.connect_material_property(disp, "", unreal.MaterialProperty.MP_DISPLACEMENT)
    except Exception as error:
        log("displacement output unavailable, nodes left ready: %s" % error)

    MEL.recompile_material(material)
    EAL.save_asset(path, only_if_is_dirty=False)
    return material


try:
    result = {"master": None, "master_created": False, "instances": 0,
              "assigned": 0, "textures_imported": 0, "missing_textures": [],
              "errors": []}

    master_path = PAYLOAD["master_path"]
    if EAL.does_asset_exist(master_path):
        master = EAL.load_asset(master_path)
        log("reusing master %s" % master_path)
    else:
        master = build_master(master_path)
        result["master_created"] = True
        log("created master %s" % master_path)
    result["master"] = master_path

    # --- import the textures written by Blender, one asset per name ------
    # Each file is imported under the exact name the material records expect,
    # so nothing depends on how the USD import happened to name (or dedupe)
    # its own textures.
    texture_folder = PAYLOAD["texture_folder"]
    textures = {}
    to_import = []
    for entry in PAYLOAD["textures"]:
        asset_path = texture_folder + "/" + entry["name"]
        if EAL.does_asset_exist(asset_path):
            textures[entry["name"]] = EAL.load_asset(asset_path)
            continue
        task = unreal.AssetImportTask()
        task.filename = entry["file"]
        task.destination_path = texture_folder
        task.destination_name = entry["name"]
        task.automated = True
        task.replace_existing = True
        task.save = True
        to_import.append((entry["name"], task))

    if to_import:
        TOOLS.import_asset_tasks([task for _, task in to_import])
        for name, _task in to_import:
            asset_path = texture_folder + "/" + name
            if EAL.does_asset_exist(asset_path):
                textures[name] = EAL.load_asset(asset_path)
    result["textures_imported"] = len(to_import)

    # fall back to whatever the USD import produced, for anything we missed
    for folder in PAYLOAD["fallback_texture_folders"]:
        if not EAL.does_directory_exist(folder):
            continue
        for path in EAL.list_assets(folder, recursive=True):
            asset = EAL.load_asset(path.split(".")[0])
            if isinstance(asset, unreal.Texture):
                textures.setdefault(asset.get_name(), asset)

    def find_texture(name):
        if name in textures:
            return textures[name]
        for key in textures:
            if key.lower() == name.lower():
                return textures[key]
        return None

    # --- one instance per Blender material -------------------------------
    instance_folder = PAYLOAD["instance_folder"]
    instances = {}
    for blender_name, record in PAYLOAD["materials"].items():
        inst_name = "MI_" + record["name"]
        inst_path = instance_folder + "/" + inst_name
        if EAL.does_asset_exist(inst_path):
            instance = EAL.load_asset(inst_path)
        else:
            instance = TOOLS.create_asset(
                inst_name, instance_folder, unreal.MaterialInstanceConstant,
                unreal.MaterialInstanceConstantFactoryNew())
        instance.set_editor_property("parent", master)

        def apply_channel(key, map_param, switch_param, value_setter):
            data = record.get(key)
            if not data:
                return
            if "texture" in data:
                tex = find_texture(data["texture"])
                if tex is None:
                    result["missing_textures"].append(data["texture"])
                    return
                MEL.set_material_instance_texture_parameter_value(instance, map_param, tex)
                MEL.set_material_instance_static_switch_parameter_value(instance, switch_param, True)
            elif "value" in data:
                value_setter(data["value"])

        apply_channel(
            "base_color", "BaseColorMap", "UseBaseColorMap",
            lambda v: MEL.set_material_instance_vector_parameter_value(
                instance, "BaseColorTint",
                unreal.LinearColor(v[0], v[1], v[2], 1.0) if isinstance(v, list) else
                unreal.LinearColor(v, v, v, 1.0)))
        apply_channel(
            "roughness", "RoughnessMap", "UseRoughnessMap",
            lambda v: MEL.set_material_instance_scalar_parameter_value(
                instance, "Roughness", float(v if not isinstance(v, list) else v[0])))
        apply_channel(
            "metallic", "MetallicMap", "UseMetallicMap",
            lambda v: MEL.set_material_instance_scalar_parameter_value(
                instance, "Metallic", float(v if not isinstance(v, list) else v[0])))
        apply_channel("normal", "NormalMap", "UseNormalMap", lambda v: None)

        emissive = record.get("emissive")
        strength = record.get("emissive_strength", 0.0)
        if emissive and strength:
            if "texture" in emissive:
                tex = find_texture(emissive["texture"])
                if tex is not None:
                    MEL.set_material_instance_texture_parameter_value(instance, "EmissiveMap", tex)
                    MEL.set_material_instance_static_switch_parameter_value(instance, "UseEmissiveMap", True)
            elif isinstance(emissive.get("value"), list):
                v = emissive["value"]
                MEL.set_material_instance_vector_parameter_value(
                    instance, "EmissiveTint", unreal.LinearColor(v[0], v[1], v[2], 1.0))
            MEL.set_material_instance_scalar_parameter_value(instance, "EmissiveStrength", float(strength))

        EAL.save_asset(inst_path, only_if_is_dirty=False)
        instances[blender_name] = instance
        result["instances"] += 1

    # --- assign them to the synced meshes, slot by slot ------------------
    subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actors = {}
    for actor in subsystem.get_all_level_actors():
        for tag in actor.tags:
            tag = str(tag)
            if tag.startswith("B2UE:obj:"):
                actors[tag[len("B2UE:obj:"):]] = actor

    for object_name, slot_names in PAYLOAD["slots"].items():
        actor = actors.get(object_name)
        if actor is None:
            continue
        component = actor.get_component_by_class(unreal.StaticMeshComponent)
        if component is None:
            continue
        for index, blender_material in enumerate(slot_names):
            instance = instances.get(blender_material)
            if instance is None:
                continue
            try:
                component.set_material(index, instance)
                result["assigned"] += 1
            except Exception as error:
                result["errors"].append("%s slot %d: %s" % (object_name, index, error))

    log("B2UE_MAT_RESULT " + json.dumps(result))
except Exception:
    log("B2UE_MAT_ERROR " + traceback.format_exc().replace("\\n", " | "))
'''


# ============================================================================
# OPERATOR
# ============================================================================

class UNREAL_OT_build_material_instances(bpy.types.Operator):
    """Rebuild the Blender materials as Unreal material instances under a
    single readable master, and assign them to the synced meshes.
    Run this after 'Send Scene via USD'"""
    bl_idname = "unreal_toolkit.build_material_instances"
    bl_label = "Build Material Instances"
    bl_options = {'REGISTER'}

    source: bpy.props.EnumProperty(
        name="Source",
        description="Which objects' materials to rebuild",
        items=[
            ('SELECTED', "Selection (+ parents/children)", "Materials used by the selected hierarchy"),
            ('EXPORT_COLLECTION', "Export Collection", "Materials used by the 'Export' collection"),
        ],
        default='SELECTED'
    )

    def _resolve_objects(self, context):
        if self.source == 'EXPORT_COLLECTION':
            collection = bpy.data.collections.get('Export')
            base = list(collection.all_objects) if collection else []
        else:
            base = list(context.selected_objects)
            if not base:
                collection = bpy.data.collections.get('Export')
                if collection:
                    base = list(collection.all_objects)
        return [obj for obj in collect_scene_objects(base) if is_exportable(obj)]

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=400)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "source")
        settings = context.scene.unreal_toolkit_settings
        layout.prop(settings, "ue_master_material")

        materials = collect_materials(self._resolve_objects(context))
        box = layout.box()
        box.label(text=f"{len(materials)} material(s) will be rebuilt", icon='MATERIAL')
        box.label(text="Textures come from the last USD sync", icon='INFO')

    def execute(self, context):

        objects = self._resolve_objects(context)
        materials = collect_materials(objects)
        if not materials:
            self.report({'WARNING'}, "No materials found on those objects")
            return {'CANCELLED'}

        settings = context.scene.unreal_toolkit_settings
        content_root = (settings.usd_content_folder or '/Game/BlenderSync').rstrip('/')
        scene_name = get_scene_name()
        scene_folder = f'{content_root}/{scene_name}'

        records = extract_material_data(materials)
        texture_dir = os.path.join(get_staging_dir(), 'b2ue_textures', scene_name)
        payload = {
            'master_path': (settings.ue_master_material or '/Game/BlenderSync/M_B2UE_Master').rstrip('/'),
            'instance_folder': f'{scene_folder}/MaterialInstances',
            'texture_folder': f'{scene_folder}/Textures',
            'fallback_texture_folders': [scene_folder, content_root],
            'textures': export_material_textures(records, texture_dir),
            'materials': records,
            'slots': build_material_slots(objects),
        }
        script = MATERIAL_SCRIPT.replace('__PAYLOAD__', json.dumps(payload))

        script_path = os.path.join(get_staging_dir(), f'{scene_name}_ue_materials.py')
        with open(script_path, 'w', encoding='utf-8') as handle:
            handle.write(script)

        success, output = run_unreal_python([
            f'exec(compile(open("{script_path.replace(chr(92), "/")}", encoding="utf-8").read(),'
            f' "b2ue_materials", "exec"))'
        ])
        if not success:
            self.report({'ERROR'}, f"Unreal connection failed: {output}")
            return {'CANCELLED'}

        data, error = None, None
        for line in str(output).splitlines():
            if 'B2UE_MAT_ERROR' in line:
                error = line.split('B2UE_MAT_ERROR', 1)[1].strip()
            elif 'B2UE_MAT_RESULT' in line:
                try:
                    data = json.loads(line.split('B2UE_MAT_RESULT', 1)[1].strip())
                except json.JSONDecodeError:
                    pass

        if error:
            print(f"Build Material Instances - Unreal error:\n{error}")
            self.report({'ERROR'}, "Unreal-side error - see console / UE Output Log")
            return {'CANCELLED'}

        if data:
            message = (f"{data['instances']} instance(s), {data.get('textures_imported', 0)} texture(s), "
                       f"{data['assigned']} slot(s) assigned"
                       + (" - master created" if data.get('master_created') else ""))
            if data.get('missing_textures'):
                unique = sorted(set(data['missing_textures']))
                message += f" - {len(unique)} texture(s) not found"
                print(f"Build Material Instances - missing textures: {unique}")
            self.report({'INFO'}, message)
        else:
            self.report({'INFO'}, "Material build sent - check the UE Output Log")
        return {'FINISHED'}


classes = (
    UNREAL_OT_build_material_instances,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
