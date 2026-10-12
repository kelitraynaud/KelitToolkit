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

import hashlib
import json
import os
import time

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
    unreal_texture_name,
    split_skeletal_assets,
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


_ALPHA_EXPORT_CACHE = {}
_TEXTURE_EXPORT_CACHE = {}


def file_stamp(path):
    try:
        stat = os.stat(path)
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return None


def export_texture_copy(image, path, extract_alpha=False):
    """Reuse clean image exports; hash actual output to detect edits in Unreal.

    File timestamps only validate this session's disk cache. The persistent
    Unreal identity is the file content hash, so rewriting identical pixels
    does not trigger reimports, including after restarting Blender.
    """
    path = os.path.normcase(os.path.abspath(path))
    source = bpy.path.abspath(image.filepath, library=image.library)
    packed = image.packed_file
    def signature():
        return (image.as_pointer(), tuple(image.size), image.channels,
                image.alpha_mode, image.file_format, image.colorspace_settings.name,
                source, file_stamp(source), packed.as_pointer() if packed else None,
                packed.size if packed else None, extract_alpha)
    previous = _TEXTURE_EXPORT_CACHE.get(path)
    output_stamp = file_stamp(path)
    if (image.source == 'FILE' and not image.is_dirty and output_stamp is not None
            and previous and previous[:2] == (signature(), output_stamp)):
        return previous[2]
    if extract_alpha in {'R', 'G', 'B'}:
        write_scalar_channel(image, path, extract_alpha)
    elif extract_alpha:
        write_alpha_mask(image, path)
    else:
        image.save(filepath=path, save_copy=True)
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    content_hash = digest.hexdigest()
    _TEXTURE_EXPORT_CACHE[path] = (signature(), file_stamp(path), content_hash)
    return content_hash


def write_scalar_channel(image, path, channel):
    """Unpack RGB scalar maps without altering the artist's master graph."""
    import numpy as np
    width, height = image.size
    if not width or not height:
        raise RuntimeError('Image has no pixels: ' + image.name)
    pixels = np.empty(width*height*4, dtype=np.float32)
    image.pixels.foreach_get(pixels)
    rgba = pixels.reshape(-1, 4)
    values = rgba[:, 'RGB'.index(channel)].copy()
    if image.colorspace_settings.name == 'sRGB' and not image.is_float:
        values = np.where(values <= .04045, values / 12.92, ((values + .055) / 1.055)**2.4)
    rgba[:, :3] = values[:, None]
    rgba[:, 3] = 1
    output = bpy.data.images.new('B2UE_ChannelExport', width, height, alpha=False)
    try:
        output.colorspace_settings.name = 'Non-Color'
        output.pixels.foreach_set(pixels)
        output.file_format = 'PNG'
        output.save(filepath=path)
    finally:
        bpy.data.images.remove(output)


def write_alpha_mask(image, path):
    """Extract Alpha to a linear grayscale texture for the existing master.

    No change to the source image or master graph. Clean images can reuse an
    extraction within this Blender session; painted/dirty images always run.
    Pixel transfer uses NumPy/foreach rather than Python loops.
    """
    import numpy as np

    path = os.path.normcase(os.path.abspath(path))

    def stamp(filename):
        try:
            stat = os.stat(filename)
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            return None

    source = bpy.path.abspath(image.filepath, library=image.library)
    packed = image.packed_file
    signature = (image.as_pointer(), tuple(image.size), image.alpha_mode,
                 image.colorspace_settings.name, source, stamp(source),
                 packed.as_pointer() if packed else None,
                 packed.size if packed else None)
    previous = _ALPHA_EXPORT_CACHE.get(path)
    output_stamp = stamp(path)
    if (image.source == 'FILE' and not image.is_dirty and output_stamp is not None
            and previous == (signature, output_stamp)):
        return False

    width, height = image.size
    if not width or not height or image.channels != 4:
        raise RuntimeError('Alpha source %s must be a loaded RGBA image' % image.name)
    pixels = np.empty(width * height * 4, dtype=np.float32)
    image.pixels.foreach_get(pixels)
    rgba = pixels.reshape((-1, 4))
    alpha = rgba[:, 3].copy()
    rgba[:, :3] = alpha[:, None]
    rgba[:, 3] = 1.0
    mask = bpy.data.images.new('B2UE_AlphaExport', width, height, alpha=False)
    try:
        mask.colorspace_settings.name = 'Non-Color'
        mask.pixels.foreach_set(pixels)
        mask.file_format = 'PNG'
        mask.save(filepath=path)
    finally:
        bpy.data.images.remove(mask)
    _ALPHA_EXPORT_CACHE[path] = (signature, stamp(path))
    return True


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
            if key == 'emissive' and not record.get('emissive_strength'):
                continue   # plugged in but switched off: nothing to import
            data = record.get(key)
            if data and 'texture' in data:
                channel = data.get('channel')
                extract_alpha = True if key == 'opacity' and channel == 'Alpha' else (channel if channel in {'R','G','B'} else False)
                if extract_alpha:
                    image = bpy.data.images.get(data['image'])
                    if image is None:
                        raise RuntimeError('Missing alpha source: ' + data['image'])
                    suffix = '_B2UE_AlphaMask' if extract_alpha is True else '_B2UE_' + extract_alpha
                    data['texture'] = unreal_texture_name(image) + suffix
                wanted.setdefault(data['texture'], (data['image'], usage, extract_alpha))

    exported = []
    for ue_name, (image_name, usage, extract_alpha) in sorted(wanted.items()):
        image = bpy.data.images.get(image_name)
        if image is None:
            continue
        path = os.path.join(out_dir, ue_name + '.png')
        try:
            # save_copy leaves the datablock's own filepath/format alone
            content_hash = export_texture_copy(image, path, extract_alpha)
        except (RuntimeError, OSError) as error:
            if extract_alpha:
                raise RuntimeError('Could not export opacity for %s: %s' % (image_name, error)) from error
            print(f"Build Material Instances - could not write {ue_name}: {error}")
            continue
        entry = {'name': ue_name, 'file': path.replace('\\', '/'), 'usage': usage,
                 'content_hash': content_hash}
        if extract_alpha:
            stat = os.stat(path)
            entry['alpha_stamp'] = '%s:%s' % (stat.st_mtime_ns, stat.st_size)
        exported.append(entry)
    return exported


def write_linear_white(out_dir):
    """4x4 white PNG, the default texture of the master's linear samplers
    (imported in Unreal with sRGB off; the engine's white square is sRGB and
    makes a Linear Color sampler fail to compile)."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, 'T_B2UE_LinearWhite.png')
    if not os.path.isfile(path):
        image = bpy.data.images.new('T_B2UE_LinearWhite', 4, 4, alpha=False)
        try:
            image.pixels = [1.0] * (4 * 4 * 4)
            image.file_format = 'PNG'
            image.save(filepath=path)
        finally:
            bpy.data.images.remove(image)
    return path.replace('\\', '/')


def apply_material_policies_to_records(records, two_sided='OFF', alpha_mode='AUTO'):
    """Turn the Send dialog's Two-Sided and Alpha choices into per-instance
    overrides: record['blend'] is None (opaque), 'MASKED' or 'TRANSLUCENT',
    record['two_sided'] a bool."""
    for record in records.values():
        opacity = record.get('opacity')
        has_alpha = bool(opacity) and ('texture' in opacity or opacity.get('value', 1.0) < 0.999)
        blend = None
        if has_alpha and alpha_mode != 'OPAQUE':
            if alpha_mode == 'TRANSLUCENT':
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
import math
import traceback
import unreal

PAYLOAD = json.loads(__PAYLOAD__)

MEL = unreal.MaterialEditingLibrary
EAL = unreal.EditorAssetLibrary
TOOLS = unreal.AssetToolsHelpers.get_asset_tools()

WHITE = "/Engine/EngineResources/WhiteSquareTexture"
FLAT_NORMAL = "/Engine/EngineMaterials/DefaultNormal"
LINEAR = unreal.MaterialSamplerType.SAMPLERTYPE_LINEAR_COLOR
# default of the LINEAR samplers: the engine's white square is sRGB, and a
# Linear Color sampler pointing at an sRGB texture does not compile (the whole
# material then falls back to the grey default). The add-on ships its own.
LINEAR_WHITE = PAYLOAD["linear_white"]["path"]
MASTER_VERSION = 2.0


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
    opacity_map = texture_param("OpacityMap", -1000, 1900, LINEAR_WHITE, "06 - Opacity", LINEAR)
    if uv is not None:
        MEL.connect_material_expressions(uv, "", opacity_map, "UVs")
    opacity_value = scalar("Opacity", 1.0, -1000, 2120, "06 - Opacity")
    opacity_switch = switch("UseOpacityMap", False, -450, 1950, "06 - Opacity")
    MEL.connect_material_expressions(opacity_map, "R", opacity_switch, "True")
    MEL.connect_material_expressions(opacity_value, "", opacity_switch, "False")
    MEL.connect_material_property(opacity_switch, "", unreal.MaterialProperty.MP_OPACITY_MASK)
    MEL.connect_material_property(opacity_switch, "", unreal.MaterialProperty.MP_OPACITY)


def ensure_linear_white():
    """Import the add-on's linear white texture next to the master (once)."""
    info = PAYLOAD["linear_white"]
    if not EAL.does_asset_exist(info["path"]):
        folder, name = info["path"].rsplit("/", 1)
        task = unreal.AssetImportTask()
        task.filename = info["file"]
        task.destination_path = folder
        task.destination_name = name
        task.automated = True
        task.replace_existing = True
        task.save = True
        TOOLS.import_asset_tasks([task])
    texture = EAL.load_asset(info["path"])
    if texture is not None and texture.get_editor_property("srgb"):
        texture.set_editor_property("srgb", False)
        EAL.save_asset(info["path"], only_if_is_dirty=False)
    return texture


def build_master(path, existing=None):
    """Create the single-layer master material: one map or one value per
    channel. `existing` rebuilds an older master in place (same asset, so
    every instance keeps its parent)."""
    ensure_linear_white()
    if existing is not None:
        material = existing
        MEL.delete_all_material_expressions(material)
    else:
        folder, name = path.rsplit("/", 1)
        material = TOOLS.create_asset(name, folder, unreal.Material, unreal.MaterialFactoryNew())
    node, texture_param, scalar, vector, switch = helpers(material)
    scalar("B2UE_MasterVersion", MASTER_VERSION, -1500, -300, "99 - Internal")

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
    rough_map = texture_param("RoughnessMap", -1000, -150, LINEAR_WHITE, "02 - Surface", LINEAR)
    wire_uv(rough_map)
    rough_value = scalar("Roughness", 0.5, -1000, 60, "02 - Surface")
    rough_switch = switch("UseRoughnessMap", False, -450, -120, "02 - Surface")
    MEL.connect_material_expressions(rough_map, "R", rough_switch, "True")
    MEL.connect_material_expressions(rough_value, "", rough_switch, "False")
    MEL.connect_material_property(rough_switch, "", unreal.MaterialProperty.MP_ROUGHNESS)

    metal_map = texture_param("MetallicMap", -1000, 220, LINEAR_WHITE, "02 - Surface", LINEAR)
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
    disp_map = texture_param("DisplacementMap", -1000, 1500, LINEAR_WHITE, "05 - Displacement", LINEAR)
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
              "instances": 0, "instances_updated": 0, "assigned": 0, "textures_imported": 0,
              "missing_textures": [], "errors": []}

    master_path = PAYLOAD["master_path"]
    if EAL.does_asset_exist(master_path):
        master = EAL.load_asset(master_path)
        # A project master belongs to the artist. Never rebuild its graph
        # during Send, including when its version marker is old or absent.
        if not isinstance(master, unreal.Material):
            raise RuntimeError("Configured master is not a Material: " + master_path)
        if any(record.get("opacity") for record in PAYLOAD["materials"].values()):
            textures = {str(n) for n in MEL.get_texture_parameter_names(master)}
            switches = {str(n) for n in MEL.get_static_switch_parameter_names(master)}
            if "OpacityMap" not in textures or "UseOpacityMap" not in switches:
                raise RuntimeError("Master needs OpacityMap and UseOpacityMap. "
                                   "The existing graph was preserved: " + master_path)
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
    content_hashes = {}
    to_import = []
    for entry in PAYLOAD["textures"]:
        usages[entry["name"]] = entry.get("usage", "color")
        content_hash = entry["content_hash"]
        content_hashes[entry["name"]] = content_hash
        asset_path = texture_folder + "/" + entry["name"]
        if EAL.does_asset_exist(asset_path):
            existing = EAL.load_asset(asset_path)
            if EAL.get_metadata_tag(existing, "B2UE_ContentHash") == content_hash:
                textures[entry["name"]] = existing
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
            if _task.imported_object_paths and EAL.does_asset_exist(asset_path):
                textures[name] = EAL.load_asset(asset_path)
            else:
                result["errors"].append("Texture import failed: " + name)
    result["textures_imported"] = sum(name in textures for name, _task in to_import)

    for name, texture in textures.items():
        changed = configure_texture(texture, usages.get(name, "color"))
        if name in content_hashes and EAL.get_metadata_tag(texture, "B2UE_ContentHash") != content_hashes[name]:
            EAL.set_metadata_tag(texture, "B2UE_ContentHash", content_hashes[name])
            changed = True
        if changed:
            EAL.save_asset(texture_folder + "/" + name, only_if_is_dirty=False)

    # Do not recursively load the scene/root when every requested map was
    # already found in the explicit export. This is the normal warm-send path.
    needed = {data["texture"].lower() for record in PAYLOAD["materials"].values()
              for data in record.values() if isinstance(data, dict) and "texture" in data}
    for folder in PAYLOAD["fallback_texture_folders"]:
        if needed.issubset({name.lower() for name in textures}):
            break
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
        changed = [False]
        if EAL.does_asset_exist(inst_path):
            instance = EAL.load_asset(inst_path)
        else:
            instance = TOOLS.create_asset(
                inst_name, instance_folder, unreal.MaterialInstanceConstant,
                unreal.MaterialInstanceConstantFactoryNew())
            changed[0] = True
        if instance.get_editor_property("parent") != master:
            instance.set_editor_property("parent", master)
            changed[0] = True

        def parameter_matches(kind, previous, value):
            if kind == "scalar":
                return math.isclose(previous, value, rel_tol=1e-6, abs_tol=1e-6)
            if kind == "vector":
                return all(math.isclose(getattr(previous, k), getattr(value, k),
                                       rel_tol=1e-6, abs_tol=1e-6) for k in ("r", "g", "b", "a"))
            return previous == value

        def set_parameter(kind, name, value):
            getter = getattr(MEL, "get_material_instance_" + kind + "_parameter_value")
            if not parameter_matches(kind, getter(instance, name), value):
                setter = getattr(MEL, "set_material_instance_" + kind + "_parameter_value")
                setter(instance, name, value)
                # UE 5.8's parameter setters return False even on success
                # (their C++ bResult is never assigned). Verify by reading.
                if not parameter_matches(kind, getter(instance, name), value):
                    raise RuntimeError("Could not set %s on %s" % (name, inst_name))
                changed[0] = True

        def apply_channel(key, map_param, switch_param, value_setter):
            data = record.get(key)
            # Reset only this add-on's switch, and only if the effective value
            # changes. Removing a mask must not leave yesterday's mask active.
            use_map = bool(data and "texture" in data and find_texture(data["texture"]))
            set_parameter("static_switch", switch_param, use_map)
            if not data:
                if key == "opacity":
                    value_setter(1.0)
                return
            if "texture" in data:
                tex = find_texture(data["texture"])
                if tex is None:
                    result["missing_textures"].append(data["texture"])
                    return
                set_parameter("texture", map_param, tex)
            elif "value" in data:
                value_setter(data["value"])

        apply_channel(
            "base_color", "BaseColorMap", "UseBaseColorMap",
            lambda v: set_parameter(
                "vector", "BaseColorTint",
                unreal.LinearColor(v[0], v[1], v[2], 1.0) if isinstance(v, list) else
                unreal.LinearColor(v, v, v, 1.0)))
        apply_channel(
            "roughness", "RoughnessMap", "UseRoughnessMap",
            lambda v: set_parameter(
                "scalar", "Roughness", float(v if not isinstance(v, list) else v[0])))
        apply_channel(
            "metallic", "MetallicMap", "UseMetallicMap",
            lambda v: set_parameter(
                "scalar", "Metallic", float(v if not isinstance(v, list) else v[0])))
        apply_channel("normal", "NormalMap", "UseNormalMap", lambda v: None)
        apply_channel(
            "opacity", "OpacityMap", "UseOpacityMap",
            lambda v: set_parameter(
                "scalar", "Opacity", float(v if not isinstance(v, list) else v[0])))

        emissive = record.get("emissive")
        strength = record.get("emissive_strength", 0.0)
        if emissive and strength:
            if "texture" in emissive:
                tex = find_texture(emissive["texture"])
                if tex is not None:
                    set_parameter("texture", "EmissiveMap", tex)
                    set_parameter("static_switch", "UseEmissiveMap", True)
            elif isinstance(emissive.get("value"), list):
                v = emissive["value"]
                set_parameter("vector", "EmissiveTint", unreal.LinearColor(v[0], v[1], v[2], 1.0))
            set_parameter("scalar", "EmissiveStrength", float(strength))

        # blend mode and two-sidedness are per-instance overrides
        try:
            overrides = instance.get_editor_property("base_property_overrides")
            blend = BLEND_MODES.get(record.get("blend"))
            wanted = {"override_blend_mode": blend is not None}
            if blend is not None:
                wanted["blend_mode"] = blend
            two_sided = bool(record.get("two_sided"))
            wanted.update(override_two_sided=two_sided, two_sided=two_sided)
            overrides_changed = False
            for key, value in wanted.items():
                if overrides.get_editor_property(key) != value:
                    overrides.set_editor_property(key, value)
                    overrides_changed = True
            if overrides_changed:
                instance.set_editor_property("base_property_overrides", overrides)
                changed[0] = True
            if changed[0]:
                MEL.update_material_instance(instance)
        except Exception as error:
            result["errors"].append("%s overrides: %s" % (inst_name, error))

        if changed[0]:
            EAL.save_asset(inst_path, only_if_is_dirty=False)
            result["instances_updated"] += 1
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
        if component is None or component.static_mesh is None:
            continue
        if not component.static_mesh.get_path_name().startswith(PAYLOAD['scene_folder'] + '/'):
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
        if EAL.get_metadata_tag(mesh, 'B2UE_UVPlan') != PAYLOAD.get('uv_signatures', {}).get(object_name, ''):
            result['errors'].append(object_name + ': UV mapping changed. Use Send to Unreal to update geometry first.')
            continue
        for index, blender_material in enumerate(slot_names):
            instance = instances.get(blender_material)
            if instance is None:
                continue
            try:
                # on the ASSET, so every actor using it and every later
                # placement gets it; component overrides are cleared
                if mesh.get_material(index) != instance:
                    mesh.set_material(index, instance)
                for component in components:
                    overrides = component.get_editor_property("override_materials")
                    if index < len(overrides) and overrides[index] is not None:
                        component.set_material(index, None)
                result["assigned"] += 1
            except Exception as error:
                result["errors"].append("%s slot %d: %s" % (object_name, index, error))
        EAL.save_loaded_asset(mesh, only_if_is_dirty=True)

    # USD merges a rig's skinning targets into one SkeletalMesh and numbers
    # the slots. The sync records their material binding order as metadata.
    for rig_name in PAYLOAD.get('skeletal_rigs', []):
        path = PAYLOAD.get('skeletal_paths', {}).get(rig_name)
        mesh = EAL.load_asset(path) if path else None
        if mesh is None:
            for path in assets_by_name.values():
                candidate = EAL.load_asset(path)
                if isinstance(candidate, unreal.SkeletalMesh) and EAL.get_metadata_tag(candidate, 'B2UE_RigName') == rig_name:
                    mesh = candidate
                    break
        if not isinstance(mesh, unreal.SkeletalMesh):
            result['unmatched_objects'].append(rig_name)
            continue
        try:
            if (EAL.get_metadata_tag(mesh, 'B2UE_RigUVPlan') or '{}') != PAYLOAD.get('rig_uv_signatures', {}).get(rig_name, '{}'):
                raise RuntimeError('UV mapping changed. Use Send to Unreal to update geometry first.')
            slot_names = json.loads(EAL.get_metadata_tag(mesh, 'B2UE_MaterialSlots'))
            slots = list(mesh.get_editor_property('materials'))
            if len(slot_names) != len(slots):
                raise RuntimeError('USD material slot count differs: %d expected / %d imported; assignments preserved' % (len(slot_names), len(slots)))
            changed = False
            for index, blender_material in enumerate(slot_names):
                if blender_material is None:
                    continue
                instance = instances.get(blender_material)
                if instance is None:
                    raise RuntimeError('Missing material instance: ' + blender_material)
                if slots[index].material_interface != instance:
                    slots[index].material_interface = instance
                    changed = True
                result['assigned'] += 1
            if changed:
                mesh.set_editor_property('materials', slots)
                EAL.save_loaded_asset(mesh, only_if_is_dirty=True)
            for actor in subsystem.get_all_level_actors():
                component = actor.get_component_by_class(unreal.SkeletalMeshComponent)
                if component and component.get_editor_property('skeletal_mesh_asset') == mesh:
                    for index, material in enumerate(component.get_editor_property('override_materials')):
                        if material is not None and index < len(slots):
                            component.set_material(index, None)
        except Exception as error:
            result['errors'].append('%s: %s' % (rig_name, error))

    log("B2UE_MAT_RESULT " + json.dumps(result))
except Exception:
    log("B2UE_MAT_ERROR " + traceback.format_exc().replace("\\n", " | "))
'''


def build_material_instances(context, objects, two_sided='OFF', alpha_mode='AUTO', skeletal_paths=None):
    """Rebuild the materials of `objects` as instances of the master material
    in the open Unreal project and assign them to the sent meshes.

    :return tuple: (success, message, result dict or None)
    """
    started = time.perf_counter()
    skeletal_assets, static_objects = split_skeletal_assets(objects)
    objects = list(dict.fromkeys(list(objects) + [o for a in skeletal_assets for o in a['objects']]))
    materials = collect_materials(objects)
    if not materials:
        return False, "No materials found on those objects", None

    settings = context.scene.kelit_toolkit_settings
    content_root = (settings.usd_content_folder or '/Game/BlenderSync').rstrip('/')
    scene_name = get_scene_name()
    scene_folder = f'{content_root}/{scene_name}'

    records = apply_material_policies_to_records(
        extract_material_data(materials), two_sided, alpha_mode)
    from .material_graph import record_warnings, uv_signatures
    warnings = record_warnings(records)
    if warnings:
        return False, 'Unsupported materials: ' + '; '.join(warnings[:3]), {'errors': warnings}
    texture_dir = os.path.join(get_staging_dir(), 'b2ue_textures', scene_name)
    master_path = (settings.ue_master_material or '/Game/BlenderSync/M_B2UE_Master').rstrip('/')
    payload = {
        'master_path': master_path,
        'linear_white': {
            'file': write_linear_white(texture_dir),
            'path': master_path.rsplit('/', 1)[0] + '/T_B2UE_LinearWhite',
        },
        'instance_folder': f'{scene_folder}/MaterialInstances',
        'texture_folder': f'{scene_folder}/Textures',
        'fallback_texture_folders': [scene_folder, content_root],
        'textures': export_material_textures(records, texture_dir),
        'materials': records,
        'slots': build_material_slots(static_objects),
        'skeletal_rigs': [a['name'] for a in skeletal_assets],
        'skeletal_paths': skeletal_paths or {},
        'rig_uv_signatures': {a['name']: json.dumps({name: sig for name, sig in uv_signatures(a['objects'], records).items() if sig}, sort_keys=True) for a in skeletal_assets},
        'scene_folder': scene_folder,
        'uv_signatures': uv_signatures(objects, records),
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

    export_seconds = time.perf_counter() - started
    started = time.perf_counter()
    success, output = run_unreal_python([
        f'exec(compile(open("{script_path.replace(chr(92), "/")}", encoding="utf-8").read(),'
        f' "b2ue_materials", "exec"))'
    ])
    print('B2UE_MATERIAL_TIMINGS ' + json.dumps({
        'prepare_and_export_seconds': round(export_seconds, 3),
        'unreal_rpc_seconds': round(time.perf_counter() - started, 3),
    }))
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
        return False, "No material result received; check Unreal's Output Log", None

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
    return not any(data.get(k) for k in ('errors', 'missing_textures', 'unmatched_objects')), message, data


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
