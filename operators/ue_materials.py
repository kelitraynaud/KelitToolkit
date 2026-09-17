"""Material bridge - rebuild Blender materials as clean Unreal instances.

Unreal's USD import produces materials parented to UsdPreviewSurface variants:
translucent as soon as an opacity input exists, hard to reason about, and
replaced on every re-import. This module instead:

1. reads each Blender material as a flat PBR record (see usd_sync),
2. makes sure a single, readable master material exists in the project,
3. creates one MaterialInstanceConstant per Blender material under it,
4. assigns those instances to the meshes, slot by slot.

The master is deliberately single-layer: one texture (or one constant) per
channel, plus static switches so unused samplers cost nothing. Blend mode and
two-sidedness are per-instance overrides, so one master serves opaque, cutout
and translucent materials. Adding displacement/tessellation later means
editing the master once - every instance inherits it.
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
    sanitize_prim_name,
)

# how Unreal must treat each channel's texture: colour maps stay sRGB, data
# maps are linear, normal maps get the normal-map compression and a flipped
# green channel (Blender reads OpenGL-style normals, Unreal DirectX-style)
CHANNEL_USAGE = (
    ('normal', 'normal'),
    ('roughness', 'linear'),
    ('metallic', 'linear'),
    ('opacity', 'linear'),
    ('base_color', 'color'),
    ('emissive', 'color'),
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
    for key, usage in CHANNEL_USAGE:   # normal first: the strictest usage wins
        for record in records.values():
            data = record.get(key)
            if data and 'texture' in data:
                wanted.setdefault(data['texture'], (data['image'], usage))

    exported = []
    for ue_name, (image_name, usage) in sorted(wanted.items()):
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
        exported.append({'name': ue_name, 'file': path.replace('\\', '/'), 'usage': usage})
    return exported


def apply_material_policies_to_records(records, two_sided='OFF', alpha_mode='AUTO'):
    """Turn the Send dialog's Two-Sided and Alpha choices into per-instance
    overrides: record['blend'] is None (opaque), 'MASKED' or 'TRANSLUCENT',
    record['two_sided'] a bool."""
    for record in records.values():
        opacity = record.get('opacity')
        has_alpha = bool(opacity) and ('texture' in opacity or opacity.get('value', 1.0) < 0.999)
        blend = None
        if has_alpha and alpha_mode != 'OPAQUE':
            if alpha_mode == 'TRANSLUCENT' or record.get('blended'):
                blend = 'TRANSLUCENT'
            else:
                blend = 'MASKED'
        if blend is None:
            record.pop('opacity', None)
        record['blend'] = blend
        record['two_sided'] = (two_sided == 'ON'
                               or (two_sided == 'BLENDER' and not record.get('backface_culling', False)))
    return records


# ============================================================================
# UNREAL-SIDE SCRIPT
# ============================================================================

MATERIAL_SCRIPT = '''
import json
import traceback
import unreal

PAYLOAD = json.loads(__PAYLOAD__)

MEL = unreal.MaterialEditingLibrary
EAL = unreal.EditorAssetLibrary
TOOLS = unreal.AssetToolsHelpers.get_asset_tools()

WHITE = "/Engine/EngineResources/WhiteSquareTexture"
FLAT_NORMAL = "/Engine/EngineMaterials/DefaultNormal"
LINEAR = unreal.MaterialSamplerType.SAMPLERTYPE_LINEAR_COLOR


def log(message):
    text = "[B2UE-MAT] " + str(message)
    unreal.log(text)
    print(text)


def helpers(material):
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

    return node, texture_param, scalar, vector, switch


def add_opacity(material, uv=None):
    """Opacity block: a map (red channel) or a value, wired to both Opacity
    and Opacity Mask. Which one counts is the instance's blend mode."""
    _node, texture_param, scalar, _vector, switch = helpers(material)
    opacity_map = texture_param("OpacityMap", -1000, 1900, WHITE, "06 - Opacity", LINEAR)
    if uv is not None:
        MEL.connect_material_expressions(uv, "", opacity_map, "UVs")
    opacity_value = scalar("Opacity", 1.0, -1000, 2120, "06 - Opacity")
    opacity_switch = switch("UseOpacityMap", False, -450, 1950, "06 - Opacity")
    MEL.connect_material_expressions(opacity_map, "R", opacity_switch, "True")
    MEL.connect_material_expressions(opacity_value, "", opacity_switch, "False")
    MEL.connect_material_property(opacity_switch, "", unreal.MaterialProperty.MP_OPACITY_MASK)
    MEL.connect_material_property(opacity_switch, "", unreal.MaterialProperty.MP_OPACITY)


def build_master(path):
    """Create the single-layer master material: one map or one value per channel."""
    folder, name = path.rsplit("/", 1)
    material = TOOLS.create_asset(name, folder, unreal.Material, unreal.MaterialFactoryNew())
    node, texture_param, scalar, vector, switch = helpers(material)

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
    rough_map = texture_param("RoughnessMap", -1000, -150, WHITE, "02 - Surface", LINEAR)
    wire_uv(rough_map)
    rough_value = scalar("Roughness", 0.5, -1000, 60, "02 - Surface")
    rough_switch = switch("UseRoughnessMap", False, -450, -120, "02 - Surface")
    MEL.connect_material_expressions(rough_map, "R", rough_switch, "True")
    MEL.connect_material_expressions(rough_value, "", rough_switch, "False")
    MEL.connect_material_property(rough_switch, "", unreal.MaterialProperty.MP_ROUGHNESS)

    metal_map = texture_param("MetallicMap", -1000, 220, WHITE, "02 - Surface", LINEAR)
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
    disp_map = texture_param("DisplacementMap", -1000, 1500, WHITE, "05 - Displacement", LINEAR)
    wire_uv(disp_map)
    disp_scale = scalar("DisplacementScale", 0.0, -1000, 1700, "05 - Displacement")
    disp = node(unreal.MaterialExpressionMultiply, -450, 1550)
    MEL.connect_material_expressions(disp_map, "R", disp, "A")
    MEL.connect_material_expressions(disp_scale, "", disp, "B")
    try:
        MEL.connect_material_property(disp, "", unreal.MaterialProperty.MP_DISPLACEMENT)
    except Exception as error:
        log("displacement output unavailable, nodes left ready: %s" % error)

    # --- opacity (cutout or translucent, decided per instance) ------------
    add_opacity(material, uv)

    MEL.recompile_material(material)
    EAL.save_asset(path, only_if_is_dirty=False)
    return material


def configure_texture(texture, usage):
    """Texture settings the master's samplers need. Returns True when changed."""
    wanted = {}
    if usage == "normal":
        wanted = {"compression_settings": unreal.TextureCompressionSettings.TC_NORMALMAP,
                  "srgb": False, "flip_green_channel": True}
    elif usage == "linear":
        wanted = {"srgb": False}
    changed = False
    for key, value in wanted.items():
        try:
            if texture.get_editor_property(key) != value:
                texture.set_editor_property(key, value)
                changed = True
        except Exception as error:
            log("texture %s: could not set %s (%s)" % (texture.get_name(), key, error))
    return changed


try:
    result = {"master": None, "master_created": False, "master_upgraded": False,
              "instances": 0, "assigned": 0, "textures_imported": 0,
              "missing_textures": [], "errors": []}

    master_path = PAYLOAD["master_path"]
    if EAL.does_asset_exist(master_path):
        master = EAL.load_asset(master_path)
        names = [str(name) for name in MEL.get_texture_parameter_names(master)]
        if "OpacityMap" not in names:
            # master built by an older version: give it the opacity block
            add_opacity(master)
            MEL.recompile_material(master)
            EAL.save_asset(master_path, only_if_is_dirty=False)
            result["master_upgraded"] = True
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
    usages = {}
    to_import = []
    for entry in PAYLOAD["textures"]:
        usages[entry["name"]] = entry.get("usage", "color")
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

    for name, texture in textures.items():
        if configure_texture(texture, usages.get(name, "color")):
            EAL.save_asset(texture_folder + "/" + name, only_if_is_dirty=False)

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

    BLEND_MODES = {"MASKED": unreal.BlendMode.BLEND_MASKED,
                   "TRANSLUCENT": unreal.BlendMode.BLEND_TRANSLUCENT}

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
        apply_channel(
            "opacity", "OpacityMap", "UseOpacityMap",
            lambda v: MEL.set_material_instance_scalar_parameter_value(
                instance, "Opacity", float(v if not isinstance(v, list) else v[0])))

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

        # blend mode and two-sidedness are per-instance overrides
        try:
            overrides = instance.get_editor_property("base_property_overrides")
            blend = BLEND_MODES.get(record.get("blend"))
            overrides.set_editor_property("override_blend_mode", blend is not None)
            if blend is not None:
                overrides.set_editor_property("blend_mode", blend)
            two_sided = bool(record.get("two_sided"))
            overrides.set_editor_property("override_two_sided", two_sided)
            overrides.set_editor_property("two_sided", two_sided)
            instance.set_editor_property("base_property_overrides", overrides)
            MEL.update_material_instance(instance)
        except Exception as error:
            result["errors"].append("%s overrides: %s" % (inst_name, error))

        EAL.save_asset(inst_path, only_if_is_dirty=False)
        instances[blender_name] = instance
        result["instances"] += 1

    # --- assign them to the sent meshes, slot by slot --------------------
    # The mesh of an object is found three ways, most reliable first: the
    # actor tagged by the sync, an actor with the object's name (actors
    # placed by hand carry no tag), then the Static Mesh asset by name in
    # the scene's folder (nothing placed in the level at all).
    subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    by_tag, by_label = {}, {}
    for actor in subsystem.get_all_level_actors():
        component = actor.get_component_by_class(unreal.StaticMeshComponent)
        if component is None:
            continue
        by_label.setdefault(actor.get_actor_label(), []).append(component)
        for tag in actor.tags:
            tag = str(tag)
            if tag.startswith("B2UE:obj:"):
                by_tag.setdefault(tag[len("B2UE:obj:"):], []).append(component)

    assets_by_name = {}
    if EAL.does_directory_exist(PAYLOAD["scene_folder"]):
        for path in EAL.list_assets(PAYLOAD["scene_folder"], recursive=True):
            name = path.split("/")[-1].split(".")[0]
            assets_by_name.setdefault(name.lower(), path.split(".")[0])

    result["unmatched_objects"] = []
    for object_name, slot_names in PAYLOAD["slots"].items():
        components = by_tag.get(object_name) or by_label.get(object_name) or []
        mesh = components[0].static_mesh if components else None
        if mesh is None:
            for candidate in PAYLOAD["mesh_names"].get(object_name, []):
                asset_path = assets_by_name.get(candidate.lower())
                asset = EAL.load_asset(asset_path) if asset_path else None
                if isinstance(asset, unreal.StaticMesh):
                    mesh = asset
                    break
        if mesh is None:
            result["unmatched_objects"].append(object_name)
            continue
        for index, blender_material in enumerate(slot_names):
            instance = instances.get(blender_material)
            if instance is None:
                continue
            try:
                # on the ASSET, so every actor using it and every later
                # placement gets it; component overrides are cleared
                mesh.set_material(index, instance)
                for component in components:
                    component.set_material(index, None)
                result["assigned"] += 1
            except Exception as error:
                result["errors"].append("%s slot %d: %s" % (object_name, index, error))
        EAL.save_loaded_asset(mesh, only_if_is_dirty=True)

    log("B2UE_MAT_RESULT " + json.dumps(result))
except Exception:
    log("B2UE_MAT_ERROR " + traceback.format_exc().replace("\\n", " | "))
'''


def build_material_instances(context, objects, two_sided='OFF', alpha_mode='AUTO'):
    """Rebuild the materials of `objects` as instances of the master material
    in the open Unreal project and assign them to the sent meshes.

    :return tuple: (success, message, result dict or None)
    """
    materials = collect_materials(objects)
    if not materials:
        return False, "No materials found on those objects", None

    settings = context.scene.kelit_toolkit_settings
    content_root = (settings.usd_content_folder or '/Game/BlenderSync').rstrip('/')
    scene_name = get_scene_name()
    scene_folder = f'{content_root}/{scene_name}'

    records = apply_material_policies_to_records(
        extract_material_data(materials), two_sided, alpha_mode)
    texture_dir = os.path.join(get_staging_dir(), 'b2ue_textures', scene_name)
    payload = {
        'master_path': (settings.ue_master_material or '/Game/BlenderSync/M_B2UE_Master').rstrip('/'),
        'instance_folder': f'{scene_folder}/MaterialInstances',
        'texture_folder': f'{scene_folder}/Textures',
        'fallback_texture_folders': [scene_folder, content_root],
        'textures': export_material_textures(records, texture_dir),
        'materials': records,
        'slots': build_material_slots(objects),
        'scene_folder': scene_folder,
        # asset names the sync gives a mesh: the object's name, with '_Mesh'
        # appended when it ends in digits (Unreal would strip them otherwise)
        'mesh_names': {obj.name: [sanitize_prim_name(obj.name),
                                  sanitize_prim_name(obj.name) + '_Mesh']
                       for obj in objects if obj.type == 'MESH'},
    }
    # double dumps: the payload becomes a python string literal in the script
    script = MATERIAL_SCRIPT.replace('__PAYLOAD__', json.dumps(json.dumps(payload)))

    script_path = os.path.join(get_staging_dir(), f'{scene_name}_ue_materials.py')
    with open(script_path, 'w', encoding='utf-8') as handle:
        handle.write(script)

    success, output = run_unreal_python([
        f'exec(compile(open("{script_path.replace(chr(92), "/")}", encoding="utf-8").read(),'
        f' "b2ue_materials", "exec"))'
    ])
    if not success:
        return False, f"Unreal connection failed: {output}", None

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
        return False, ("Unreal reported an error while building the materials. Details: "
                       "System Console (Window menu) or Unreal's Output Log, lines [B2UE-MAT]"), None
    if not data:
        return True, "material build sent, check Unreal's Output Log", None

    message = (f"{data['instances']} material instance(s), "
               f"{data.get('textures_imported', 0)} texture(s) imported, "
               f"{data['assigned']} slot(s) assigned")
    if data.get('master_created'):
        message += ", master created"
    elif data.get('master_upgraded'):
        message += ", master upgraded (opacity)"
    if data.get('missing_textures'):
        unique = sorted(set(data['missing_textures']))
        message += f", {len(unique)} texture(s) not found"
        print(f"Build Material Instances - missing textures: {unique}")
    if data.get('unmatched_objects'):
        message += f", {len(data['unmatched_objects'])} object(s) without a mesh in Unreal"
        print(f"Build Material Instances - no mesh found for: {data['unmatched_objects']}")
    if data.get('errors'):
        message += f", {len(data['errors'])} error(s) (see console)"
        print(f"Build Material Instances - errors: {data['errors']}")
    return True, message, data


# ============================================================================
# OPERATOR
# ============================================================================

class UNREAL_OT_build_material_instances(bpy.types.Operator):
    """Create one Unreal material instance per Blender material, children of
    the master material below, import their textures and assign them to the
    actors sent from this .blend. 'Send to Unreal' does this by itself when
    its Materials option is set to the master material; run it by hand to
    refresh the materials without sending the meshes again"""
    bl_idname = "kelit_toolkit.build_material_instances"
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
        settings = context.scene.kelit_toolkit_settings
        layout.prop(settings, "ue_master_material")

        materials = collect_materials(self._resolve_objects(context))
        box = layout.box()
        box.label(text=f"{len(materials)} material(s) will be rebuilt", icon='MATERIAL')
        box.label(text="Textures are exported from Blender into the scene's Textures folder",
                  icon='INFO')
        box.label(text=f"Two-Sided: {settings.sync_two_sided}, Alpha: {settings.sync_alpha_mode} "
                       "(from the Send dialog)", icon='INFO')

    def execute(self, context):
        settings = context.scene.kelit_toolkit_settings
        success, message, _data = build_material_instances(
            context, self._resolve_objects(context),
            two_sided=settings.sync_two_sided or 'OFF',
            alpha_mode=settings.sync_alpha_mode or 'AUTO')
        self.report({'INFO'} if success else {'ERROR'}, message)
        return {'FINISHED'} if success else {'CANCELLED'}


classes = (
    UNREAL_OT_build_material_instances,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
