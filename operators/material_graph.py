"""Explicit PBR graph support and reversible, shared 2D material UV transforms."""
import contextlib
import hashlib
import json
import math
import bpy

IDENTITY = [1., 0., 0., 0., 1., 0.]


def active_uv_name(mesh):
    """The UV map a mesh renders with ('' when it has none)."""
    layers = getattr(mesh, 'uv_layers', None)
    if not layers:
        return ''
    active = next((u for u in layers if u.active_render), layers.active)
    return active.name if active else ''


def default_uv_names(tree):
    """Render UV names of every mesh using the material that owns `tree`
    (object-linked slots included). Empty set when nothing uses it."""
    material = next((m for m in bpy.data.materials if m.node_tree == tree), None)
    if material is None:
        return set()
    names = set()
    for obj in bpy.data.objects:
        if obj.type != 'MESH' or obj.data is None:
            continue
        if any(slot.material == material for slot in obj.material_slots):
            names.add(active_uv_name(obj.data))
    return names


def uv_source(socket, depth=0):
    if depth > 12:
        return {'unsupported': 'UV chain is cyclic or too deep'}
    if not socket.is_linked:
        return {'map': '', 'matrix': IDENTITY[:]}
    output = socket.links[0].from_socket
    node = output.node
    if node.type == 'REROUTE':
        return uv_source(node.inputs[0], depth + 1)
    if node.type == 'UVMAP':
        # naming the map every user mesh renders with is the same as naming
        # none: it must not count as a different mapping
        name = node.uv_map
        if name and default_uv_names(node.id_data) == {name}:
            name = ''
        return {'map': name, 'matrix': IDENTITY[:]}
    if node.type == 'TEX_COORD' and output.name == 'UV':
        return {'map': '', 'matrix': IDENTITY[:]}
    if node.type == 'MAPPING' and node.vector_type == 'POINT':
        if any(node.inputs[n].is_linked for n in ('Location', 'Rotation', 'Scale')):
            return {'unsupported': 'Animated/linked Mapping values require baking'}
        loc, rot, scale = [node.inputs[n].default_value for n in ('Location', 'Rotation', 'Scale')]
        if abs(rot[0]) > 1e-6 or abs(rot[1]) > 1e-6:
            return {'unsupported': 'Only 2D Mapping rotation is supported'}
        previous = uv_source(node.inputs['Vector'], depth + 1)
        if 'unsupported' in previous:
            return previous
        c, s = math.cos(rot[2]), math.sin(rot[2])
        a, b, x, d, e, y = previous['matrix']
        transform = [c*scale[0]*a-s*scale[1]*d, c*scale[0]*b-s*scale[1]*e,
                     c*scale[0]*x-s*scale[1]*y+loc[0],
                     s*scale[0]*a+c*scale[1]*d, s*scale[0]*b+c*scale[1]*e,
                     s*scale[0]*x+c*scale[1]*y+loc[1]]
        return {'map': previous['map'], 'matrix': [round(float(v), 8) for v in transform]}
    return {'unsupported': 'UV node not supported: ' + node.name + ' (' + node.type + ')'}


def inspect_input(socket, texture_name, depth=0):
    if socket is None:
        return None
    if depth > 12:
        return {'unsupported': 'Node chain is cyclic or too deep'}
    if not socket.is_linked:
        value = getattr(socket, 'default_value', None)
        if hasattr(value, '__len__') and not isinstance(value, str):
            return {'value': [round(float(v), 5) for v in value][:3]}
        return {'value': round(float(value), 5)} if value is not None else None
    source = socket.links[0].from_socket
    node = source.node
    if node.type == 'REROUTE':
        return inspect_input(node.inputs[0], texture_name, depth + 1)
    if node.type == 'TEX_IMAGE':
        if not node.image:
            return {'unsupported': 'Image is missing on ' + node.name}
        if node.projection != 'FLAT' or node.extension != 'REPEAT':
            return {'unsupported': 'Only flat, repeating image textures are supported: ' + node.name}
        uv = uv_source(node.inputs['Vector'])
        if 'unsupported' in uv:
            return uv
        return {'texture': texture_name(node.image), 'image': node.image.name,
                'colorspace': node.image.colorspace_settings.name,
                'source_node': node.name, 'channel': source.name, 'direct': True, 'uv': uv}
    if node.type in {'SEPRGB', 'SEPARATE_COLOR'}:
        if node.type == 'SEPARATE_COLOR' and node.mode != 'RGB':
            return {'unsupported': 'Separate Color currently supports RGB mode only'}
        data = inspect_input(node.inputs[0], texture_name, depth + 1)
        if data and 'texture' in data and data.get('channel') == 'Color':
            data['channel'] = {'Red': 'R', 'Green': 'G', 'Blue': 'B'}.get(source.name, source.name)
            data['direct'] = False
            return data
        return {'unsupported': 'Separate Color requires a direct image input'}
    if node.type == 'NORMAL_MAP':
        strength = node.inputs['Strength']
        if node.space != 'TANGENT' or strength.is_linked or abs(strength.default_value - 1) > 1e-6:
            return {'unsupported': 'Normal Map requires tangent space and strength 1'}
        data = inspect_input(node.inputs['Color'], texture_name, depth + 1)
        if data and 'texture' in data:
            data['normal_uv_map'] = node.uv_map
            data['direct'] = False
        return data
    return {'unsupported': 'Node operation not reproduced: ' + node.name + ' (' + node.type + ')'}


def material_uv_plan(record):
    channels = [v for v in record.values() if isinstance(v, dict) and 'texture' in v]
    plans = [v.get('uv', {'map': '', 'matrix': IDENTITY[:]}) for v in channels]
    if not plans:
        return {'map': '', 'matrix': IDENTITY[:]}
    if any(p != plans[0] for p in plans):
        raise ValueError('Different UV mappings per texture require a dedicated material or baking')
    plan = plans[0]
    if 'normal' in record and 'texture' in record['normal']:
        a, b, _, d, e, _ = plan['matrix']
        if abs(b) > 1e-6 or abs(d) > 1e-6 or a <= 0 or e <= 0:
            raise ValueError('Rotated/mirrored Mapping with a normal map requires tangent-space conversion')
    return plan


# channels the master reads as one value (the red channel of their map)
SCALAR_CHANNELS = {'roughness', 'metallic', 'opacity'}


def texture_extraction(key, data):
    """How the texture of channel `key` must be written for the master:
    True extracts the image's Alpha into a linear grayscale map, 'R'/'G'/'B'
    unpacks one colour channel, False keeps the image as it is. The master
    reads scalar maps through their red channel, so an Alpha output feeding
    Roughness or Metallic exported as RGB would read the colour instead."""
    channel = (data or {}).get('channel')
    if key in SCALAR_CHANNELS and channel == 'Alpha':
        return True
    if channel in {'R', 'G', 'B'}:
        return channel
    return False


def extraction_suffix(mode):
    if mode is True:
        return '_B2UE_AlphaMask'
    return '_B2UE_' + mode if mode else ''


def texture_collisions(records):
    """Exported texture names (compared without case, as Unreal does) that
    still point at more than one distinct image."""
    from .usd_sync import image_identity, unreal_texture_name
    owners = {}
    for record in records.values():
        for key, data in record.items():
            if not isinstance(data, dict) or 'texture' not in data:
                continue
            image = bpy.data.images.get(data.get('image', ''))
            base = unreal_texture_name(image) if image is not None else data['texture']
            name = base + extraction_suffix(texture_extraction(key, data))
            entry = owners.setdefault(name.casefold(), (name, set(), set()))
            entry[1].add(image_identity(image) if image is not None else data.get('image', ''))
            entry[2].add(data.get('image', ''))
    return {name: sorted(images) for name, identities, images in owners.values()
            if len(identities) > 1}


def record_warnings(records):
    warnings = []
    names = {}
    for name, record in records.items():
        key = record.get('name', name).casefold()
        if key in names and names[key] != name:
            warnings.append('Material names collide in Unreal: ' + names[key] + ' / ' + name)
        names[key] = name
        if record.get('unsupported'):
            warnings.append(name + ': no supported Principled BSDF')
        for channel, value in record.items():
            if isinstance(value, dict) and value.get('unsupported'):
                warnings.append(name + ' / ' + channel + ': ' + value['unsupported'])
            if isinstance(value, dict) and value.get('channel') in {'R', 'G', 'B'} and channel in {'base_color', 'emissive', 'normal'}:
                warnings.append(name + ' / ' + channel + ': separated channels supported for scalar maps only')
            if isinstance(value, dict) and value.get('channel') == 'Alpha' and channel in {'base_color', 'emissive', 'normal'}:
                warnings.append(name + ' / ' + channel + ': an image Alpha output is supported for scalar maps only')
        try:
            material_uv_plan(record)
        except ValueError as error:
            warnings.append(name + ': ' + str(error))
    for texture, images in sorted(texture_collisions(records).items()):
        warnings.append('Texture names collide in Unreal: ' + texture + ' (' + ', '.join(images) + ')')
    return warnings


def uv_signatures(objects, records):
    result = {}
    for obj in objects:
        if obj.type != 'MESH':
            continue
        plans = []
        for slot in obj.material_slots:
            if slot.material and slot.material.name in records:
                plan = material_uv_plan(records[slot.material.name])
                plans.append([slot.material.name, plan])
        if any(plan != {'map': '', 'matrix': IDENTITY} for _, plan in plans):
            result[obj.name] = hashlib.sha256(json.dumps(plans, sort_keys=True).encode()).hexdigest()
        else:
            result[obj.name] = ''
    return result


@contextlib.contextmanager
def temporary_material_uvs(objects):
    """Bake shared 2D mapping into copies used only by the master-material export."""
    from .usd_sync import extract_material_data, collect_materials
    import numpy as np
    records = extract_material_data(collect_materials(objects))
    warnings = record_warnings(records)
    if warnings:
        raise RuntimeError('Unsupported material graph: ' + '; '.join(warnings[:4]))
    plans = {name: material_uv_plan(record) for name, record in records.items()}
    copies = []
    try:
        for obj in objects:
            if obj.type != 'MESH' or obj.data is None or not obj.data.uv_layers:
                continue
            if obj.library is not None:
                # a linked object cannot take a temporary mesh
                print('[KelitToolkit] ' + obj.name + ': linked object, material UV mapping not baked')
                continue
            material_plans = {i: plans[s.material.name] for i, s in enumerate(obj.material_slots)
                              if s.material and s.material.name in plans}
            if all(p == {'map': '', 'matrix': IDENTITY} for p in material_plans.values()):
                continue
            original = obj.data
            active = next((u for u in original.uv_layers if u.active_render), original.uv_layers.active)
            for index, plan in material_plans.items():
                record = records[obj.material_slots[index].material.name]
                normal = record.get('normal', {})
                if 'texture' in normal and plan['map'] and plan['map'] != active.name:
                    raise RuntimeError(obj.name + ': alternate UV with normal map needs an explicit tangent conversion')
            mesh = original.copy()
            copies.append((obj, original, mesh))
            obj.data = mesh
            target = mesh.uv_layers.new(name='B2UE_MaterialUV')
            target.active_render = True
            coords = np.empty(len(mesh.loops)*2, dtype=np.float32)
            active.data.foreach_get('uv', coords)
            coords = coords.reshape(-1, 2)
            # material index of every loop, without a per-polygon Python loop
            polygon_count = len(mesh.polygons)
            material_index = np.empty(polygon_count, dtype=np.int32)
            mesh.polygons.foreach_get('material_index', material_index)
            loop_start = np.empty(polygon_count, dtype=np.int64)
            mesh.polygons.foreach_get('loop_start', loop_start)
            loop_total = np.empty(polygon_count, dtype=np.int64)
            mesh.polygons.foreach_get('loop_total', loop_total)
            loop_material = np.full(len(mesh.loops), -1, dtype=np.int32)
            if polygon_count:
                order = np.repeat(loop_start - np.cumsum(loop_total) + loop_total, loop_total) + np.arange(int(loop_total.sum()))
                loop_material[order] = np.repeat(material_index, loop_total)
            for index, plan in material_plans.items():
                source = original.uv_layers.get(plan['map']) if plan['map'] else active
                if source is None:
                    raise RuntimeError(obj.name + ': UV map not found: ' + plan['map'])
                values = np.empty(len(mesh.loops)*2, dtype=np.float32)
                source.data.foreach_get('uv', values)
                values = values.reshape(-1, 2)
                a,b,x,d,e,y = plan['matrix']
                mask = loop_material == index
                v = values[mask]
                coords[mask, 0] = a*v[:,0]+b*v[:,1]+x
                coords[mask, 1] = d*v[:,0]+e*v[:,1]+y
            target.data.foreach_set('uv', coords.ravel())
        yield
    finally:
        for obj, original, mesh in reversed(copies):
            obj.data = original
            bpy.data.meshes.remove(mesh)
