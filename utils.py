"""Utility functions for Kelit Toolkit"""

import re

import bpy
import mathutils


# ---------------------------------------------------------------------------
# Shared instance/geometry helpers (used by export.py, origin.py, ...)
# ---------------------------------------------------------------------------

def mesh_users(mesh):
    """Every object in the file using this mesh data."""
    return [obj for obj in bpy.data.objects
            if obj.type == 'MESH' and obj.data == mesh]


def transform_mesh_geometry(mesh, matrix3):
    """Apply a 3x3 matrix to the mesh vertices AND its shape keys, so
    shape-keyed meshes stay in sync instead of silently desyncing."""
    if mesh.shape_keys:
        for key_block in mesh.shape_keys.key_blocks:
            for point in key_block.data:
                point.co = matrix3 @ point.co
    for vertex in mesh.vertices:
        vertex.co = matrix3 @ vertex.co
    mesh.update()


def offset_mesh_geometry(mesh, offset):
    """Translate the mesh vertices AND its shape keys by a vector."""
    if mesh.shape_keys:
        for key_block in mesh.shape_keys.key_blocks:
            for point in key_block.data:
                point.co += offset
    for vertex in mesh.vertices:
        vertex.co += offset
    mesh.update()


def local_rotation_matrix(obj):
    """The object's LOCAL rotation as a 3x3 matrix, whatever its mode."""
    mode = obj.rotation_mode
    if mode == 'QUATERNION':
        return obj.rotation_quaternion.to_matrix()
    if mode == 'AXIS_ANGLE':
        angle, axis_x, axis_y, axis_z = obj.rotation_axis_angle
        axis = mathutils.Vector((axis_x, axis_y, axis_z))
        if axis.length < 1e-8:
            axis = mathutils.Vector((0.0, 0.0, 1.0))
        return mathutils.Matrix.Rotation(angle, 3, axis)
    return obj.rotation_euler.to_matrix()


def reset_local_rotation(obj):
    """Zero the local rotation in every representation."""
    obj.rotation_euler = (0.0, 0.0, 0.0)
    obj.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
    obj.rotation_axis_angle = (0.0, 0.0, 1.0, 0.0)


def matrices_close(a, b, tolerance=1e-4):
    return all(abs(a[i][j] - b[i][j]) < tolerance
               for i in range(3) for j in range(3))


def is_editable(datablock):
    """False for library-linked datablocks, whose names cannot be written."""
    return datablock is not None and datablock.library is None


def iter_action_fcurves(action):
    """Yield every F-Curve of an action, on both legacy (4.x) and slotted
    (5.x) actions. Collected per strip so one exotic strip type cannot wipe
    what the other strips already contributed."""
    if action is None:
        return
    fcurves = []
    if hasattr(action, 'layers'):
        for layer in action.layers:
            for strip in getattr(layer, 'strips', ()):
                try:
                    for channelbag in strip.channelbags:
                        fcurves.extend(channelbag.fcurves)
                except AttributeError:
                    continue
    if not fcurves and hasattr(action, 'fcurves'):
        fcurves = list(action.fcurves)
    for fcurve in fcurves:
        yield fcurve


def clean_name(name):
    """Removes trailing duplicate suffixes like .001 - including stacked
    ones ('Cube.001.002')."""
    while True:
        stripped = re.sub(r'\.\d+$', '', name)
        if stripped == name:
            return name
        name = stripped


def to_pascal_case(text):
    """Convert text to PascalCase"""
    # Remove special characters except spaces, underscores, and hyphens
    text = re.sub(r'[^\w\s\-]', '', text)

    # Split on spaces, underscores, and hyphens
    words = re.split(r'[\s_\-]+', text)

    # Capitalize each word
    pascal_words = []
    for word in words:
        if word:  # Skip empty strings
            # If word is all uppercase and longer than 1 char, make it title case
            if word.isupper() and len(word) > 1:
                pascal_words.append(word.capitalize())
            # If word is all lowercase
            elif word.islower():
                pascal_words.append(word.capitalize())
            # If word is mixed case, preserve it
            else:
                # Capitalize first letter, keep rest as is
                pascal_words.append(word[0].upper() + word[1:])

    return ''.join(pascal_words)


def normalize_name_for_unreal(name, object_type='MESH', preserve_collision=True, preserve_lod=True):
    """
    Normalize object name according to Unreal Engine conventions

    Args:
        name: Original name
        object_type: Type of object (MESH, MATERIAL, TEXTURE, etc.)
        preserve_collision: Keep collision prefixes (UCX_, UBX_, USP_)
        preserve_lod: Keep LOD suffixes (_LOD0, _LOD1, etc.)

    Returns:
        Normalized name in PascalCase with appropriate prefix
    """
    # Check for collision prefixes
    collision_prefix = None
    if preserve_collision:
        for prefix in ['UCX_', 'UBX_', 'USP_', 'UCP_']:
            if name.startswith(prefix):
                collision_prefix = prefix
                name = name[len(prefix):]
                break

    # Extract LOD suffix if present
    lod_suffix = None
    if preserve_lod:
        lod_match = re.search(r'_LOD\d+$', name, re.IGNORECASE)
        if lod_match:
            lod_suffix = lod_match.group(0).upper()  # Normalize to uppercase
            name = name[:lod_match.start()]

    # Remove Blender suffixes (.001, .002, etc.)
    name = clean_name(name)

    # Remove existing Unreal prefixes to avoid duplication
    for prefix in ['SM_', 'SK_', 'M_', 'T_', 'MI_', 'BP_', 'S_', 'A_', 'AM_', 'P_', 'L_', 'CAM_', 'COL_']:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break

    # Remove multiple underscores
    name = re.sub(r'_+', '_', name)
    name = name.strip('_')

    # Convert to PascalCase
    name = to_pascal_case(name)

    # If name is empty after cleaning, use default
    if not name:
        name = 'Object'

    # Add appropriate prefix based on object type
    if not collision_prefix:  # Don't add prefix if it's a collision mesh
        prefix_map = {
            'MESH': 'SM_',
            'MATERIAL': 'M_',
            'TEXTURE': 'T_',
            'LIGHT': 'L_',
            'CAMERA': 'CAM_',
            'COLLECTION': 'COL_',
            'ARMATURE': 'SK_',
            'SKELETON': 'SK_',
        }
        prefix = prefix_map.get(object_type, 'SM_')
        name = prefix + name
    else:
        # Restore collision prefix
        name = collision_prefix + name

    # Add LOD suffix back if it existed
    if lod_suffix:
        name = name + lod_suffix

    # Blender silently truncates names at 63 bytes, which can collide two
    # long names onto the same datablock - trim deterministically instead,
    # keeping the LOD tag intact at the end
    if len(name.encode('utf-8')) > 63:
        tail = lod_suffix or ''
        base = name[:-len(tail)] if tail else name
        budget = 63 - len(tail.encode('utf-8'))
        head = base.encode('utf-8')[:budget]
        while True:
            try:
                head_text = head.decode('utf-8')
                break
            except UnicodeDecodeError:
                head = head[:-1]
        name = head_text + tail

    return name


# ---------------------------------------------------------------------------
# Material fingerprint (Merge Duplicate Materials, Auto Clean, instances)
# ---------------------------------------------------------------------------
# A fingerprint must change with EVERY setting that changes the render: a
# Math node set to Multiply instead of Add, an RGB node colour, a ramp stop,
# a node group's content, an image from another folder. Two materials that
# fingerprint equal are merged and one of them is deleted, so when in doubt a
# setting is part of the key. Only what is purely cosmetic in the node editor
# (names, positions, sizes, colours, selection, collapsed state) is ignored.

# node properties that never change the render (node editor layout)
_COSMETIC_PROPERTIES = frozenset({
    'rna_type', 'name', 'label', 'location', 'location_absolute', 'width',
    'height', 'width_hidden', 'dimensions', 'select', 'hide', 'color',
    'color_tag', 'use_custom_color', 'parent', 'internal_links', 'inputs',
    'outputs', 'type', 'warning_propagation', 'is_hidden', 'description',
})
# for the structs a node holds (ramp stops, curve points, mappings), only the
# selection is cosmetic: their 'location' or 'color' IS the setting
_COSMETIC_NESTED = frozenset({'rna_type', 'select'})
# node types without any effect on the shading
_COSMETIC_NODES = frozenset({'NodeFrame'})
# material-level settings that change how the material renders
_MATERIAL_SETTINGS = (
    'surface_render_method', 'use_backface_culling', 'use_backface_culling_shadow',
    'use_backface_culling_lightprobe_volume', 'use_transparency_overlap',
    'use_transparent_shadow', 'use_raytrace_refraction', 'refraction_depth',
    'thickness_mode', 'use_thickness_from_shadow', 'displacement_method',
    'volume_intersection_method', 'max_vertex_displacement', 'pass_index',
    'alpha_threshold', 'blend_method',
)
_MAX_STRUCT_DEPTH = 8

# packed image hashes, keyed by (session uid, packed size): the merge dialog
# redraws often and hashing large packed images every time would stall it
_PACKED_HASHES = {}


def _is_cosmetic(identifier, nested=False):
    if identifier.startswith('show_') or identifier.startswith('bl_'):
        return True
    return identifier in (_COSMETIC_NESTED if nested else _COSMETIC_PROPERTIES)


def _plain_value(value, context=None):
    """Rounded, hashable version of an RNA value."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return round(value, 4) + 0.0   # + 0.0 turns -0.0 into 0.0
    if isinstance(value, (set, frozenset)):
        return tuple(sorted(value))
    if isinstance(value, bpy.types.ID):
        return _id_key(value, context if context is not None else _new_context())
    try:
        return tuple(_plain_value(item, context) for item in value)
    except TypeError:
        return str(value)


def _new_context():
    return {'trees': {}, 'stack': set(), 'structs': set()}


def _session_key(datablock):
    return getattr(datablock, 'session_uid', None) or datablock.as_pointer()


def _image_key(image):
    """What an image node actually reads. File images are keyed by their
    absolute normalized path (the same file loaded twice, rooftop_01.jpg and
    rooftop_01.jpg.001, is one texture; two files with the same name in two
    folders are not), packed images by a hash of their bytes. Colour space
    and alpha mode change the result, so they are part of the key. Generated
    images and images with unsaved painting only equal themselves."""
    import hashlib
    import os
    settings = (image.source, image.colorspace_settings.name, image.alpha_mode)
    if image.source in {'GENERATED', 'VIEWER'} or image.type in {'RENDER_RESULT', 'COMPOSITING'}:
        return ('image-id', image.name_full) + settings
    try:
        dirty = image.is_dirty
    except Exception:
        dirty = False
    if dirty:
        return ('image-id', image.name_full) + settings
    packed = image.packed_file
    if packed is not None:
        cache_key = (_session_key(image), packed.size)
        digest = _PACKED_HASHES.get(cache_key)
        if digest is None:
            digest = hashlib.md5(bytes(packed.data)).hexdigest()
            _PACKED_HASHES[cache_key] = digest
        return ('image-packed', digest) + settings
    path = image.filepath_raw or image.filepath
    if not path:
        return ('image-id', image.name_full) + settings
    absolute = bpy.path.abspath(path, library=image.library)
    return ('image-file', os.path.normcase(os.path.normpath(absolute))) + settings


def _id_key(datablock, context):
    if isinstance(datablock, bpy.types.Image):
        return _image_key(datablock)
    if isinstance(datablock, bpy.types.NodeTree):
        return _tree_fingerprint(datablock, context)
    return (type(datablock).__name__, datablock.name_full,
            datablock.library.filepath if datablock.library else '')


def _struct_key(struct, context, depth=0):
    """Every render-relevant property of an RNA struct (a node, a colour
    ramp and its stops, a curve mapping and its points, a texture mapping),
    recursively. ID pointers are keyed by what they are, not followed."""
    if depth > _MAX_STRUCT_DEPTH:
        return ('...',)
    marker = (struct.bl_rna.identifier, struct.as_pointer())
    if marker in context['structs']:
        return ('cycle',)
    context['structs'].add(marker)
    try:
        values = []
        for prop in struct.bl_rna.properties:
            identifier = prop.identifier
            if _is_cosmetic(identifier, nested=depth > 0):
                continue
            try:
                value = getattr(struct, identifier)
            except Exception:
                continue
            if prop.type == 'POINTER':
                if value is None:
                    entry = None
                elif isinstance(value, bpy.types.ID):
                    entry = _id_key(value, context)
                else:
                    entry = _struct_key(value, context, depth + 1)
            elif prop.type == 'COLLECTION':
                entry = tuple(_struct_key(item, context, depth + 1) for item in value)
            else:
                entry = _plain_value(value, context)
            values.append((identifier, entry))
        return (struct.bl_rna.identifier, tuple(values))
    finally:
        context['structs'].discard(marker)


def _socket_value(socket, context=None):
    """Rounded, hashable default value of a node socket."""
    return _plain_value(getattr(socket, 'default_value', None), context)


def _node_fingerprint(node, context=None):
    if context is None:
        context = _new_context()
    settings = _struct_key(node, context)
    inputs = tuple(
        (socket.identifier, _socket_value(socket, context))
        for socket in node.inputs
        if not socket.is_linked and hasattr(socket, 'default_value'))
    # outputs carry values too: an RGB or Value node stores its colour or
    # number in its OUTPUT socket, linked or not
    outputs = tuple(
        (socket.identifier, _socket_value(socket, context))
        for socket in node.outputs if hasattr(socket, 'default_value'))
    return (node.bl_idname, settings, inputs, outputs)


def _tree_fingerprint(tree, context):
    """Nodes and links of a node tree; node groups are followed recursively
    (a group using itself, directly or not, stops at the cycle)."""
    marker = tree.as_pointer()
    if marker in context['trees']:
        return context['trees'][marker]
    if marker in context['stack']:
        return ('tree-cycle', tree.name_full)
    context['stack'].add(marker)
    try:
        prints = {node.name: _node_fingerprint(node, context)
                  for node in tree.nodes if node.bl_idname not in _COSMETIC_NODES}
        nodes = tuple(sorted(prints.values(), key=repr))
        links = tuple(sorted(
            ((prints.get(link.from_node.name), link.from_socket.identifier,
              prints.get(link.to_node.name), link.to_socket.identifier,
              bool(getattr(link, 'is_muted', False)))
             for link in tree.links),
            key=repr))
    finally:
        context['stack'].discard(marker)
    result = ('tree', tree.bl_idname, nodes, links)
    context['trees'][marker] = result
    return result


def material_fingerprint(material):
    """Hashable description of what a material LOOKS like: every node with
    all its settings (operations, blend modes, ramps, curves, node groups),
    the images it reads (by file path or packed content), socket values,
    links, surface settings. Two materials with the same fingerprint render
    the same whatever their names (imports keep creating rooftop_01,
    rooftop_01.001, ... copies of one material)."""
    if material is None:
        return None
    surface = tuple((name, _plain_value(getattr(material, name, None)))
                    for name in _MATERIAL_SETTINGS)
    tree = getattr(material, 'node_tree', None)
    if tree is None or not tree.nodes:
        return ('flat', tuple(round(c, 4) for c in material.diffuse_color),
                round(material.roughness, 4), round(material.metallic, 4), surface)
    return ('nodes', _tree_fingerprint(tree, _new_context()), surface)


# ---------------------------------------------------------------------------
# Natural axes and rotation-invariant shape signature (Auto Align, Detect
# Duplicates and Split Repeated Parts with rotated copies)
# ---------------------------------------------------------------------------
# A shape's "natural axes" come from its flat faces: the direction holding
# the most surface (normals folded, n and -n are one axis), then the
# direction holding the most surface among the faces at right angle to it.
# Shapes without such a structure (organic, round) fall back to a principal
# component analysis of the surface. The axes are only known up to sign and
# order: the 24 rotations of a cube turn one valid frame into every other
# one, and a canonical form picks one of them from the geometry itself.

AXIS_CONE_DEGREES = 5.0
PLANAR_MIN_CONFIDENCE = 0.3
ROTATION_MIN_CONFIDENCE = 0.3

_CUBE_ROTATIONS = []


def cube_rotations():
    """The 24 proper rotations of a cube as 3x3 numpy arrays (signed
    permutation matrices of determinant +1), identity first, fixed order."""
    import itertools
    import numpy as np
    if not _CUBE_ROTATIONS:
        found = []
        for permutation in itertools.permutations(range(3)):
            for signs in itertools.product((1.0, -1.0), repeat=3):
                matrix = np.zeros((3, 3))
                for row, column in enumerate(permutation):
                    matrix[row, column] = signs[row]
                if np.linalg.det(matrix) > 0:
                    found.append(matrix)
        found.sort(key=lambda matrix: -np.trace(matrix))   # stable: identity first
        _CUBE_ROTATIONS.extend(found)
    return _CUBE_ROTATIONS


def _dominant_direction(directions, weights, cos_limit, max_candidates=48):
    """Axis (unit numpy vector) holding the most weight within the cone
    `cos_limit`, n and -n counting for the same axis, plus that weight.
    Candidates come from a coarse binning of the folded directions, each is
    scored against every direction, the winner is refined to the weighted
    mean of its cone."""
    import numpy as np
    if len(directions) == 0:
        return None, 0.0
    rows = np.arange(len(directions))
    largest = np.argmax(np.abs(directions), axis=1)
    signs = np.sign(directions[rows, largest])
    signs[signs == 0] = 1.0
    folded = directions * signs[:, None]
    keys = np.rint(folded * 16.0).astype(np.int64)
    unique, inverse = np.unique(keys, axis=0, return_inverse=True)
    inverse = np.asarray(inverse).ravel()
    bin_weight = np.bincount(inverse, weights=weights, minlength=len(unique))
    order = np.argsort(-bin_weight, kind='stable')[:max_candidates]
    sums = np.column_stack([
        np.bincount(inverse, weights=folded[:, k] * weights, minlength=len(unique))
        for k in range(3)])
    candidates = sums[order]
    lengths = np.linalg.norm(candidates, axis=1)
    keep = lengths > 1e-12
    if not keep.any():
        return None, 0.0
    candidates = candidates[keep] / lengths[keep][:, None]
    scores = np.zeros(len(candidates))
    block = 65536
    for start in range(0, len(directions), block):
        dots = np.abs(candidates @ directions[start:start + block].T)
        scores += (dots >= cos_limit) @ weights[start:start + block]
    best = int(np.argmax(scores))
    axis = candidates[best]
    dots = directions @ axis
    member = np.abs(dots) >= cos_limit
    if member.any():
        refined = ((directions[member] * np.sign(dots[member])[:, None])
                   * weights[member][:, None]).sum(axis=0)
        length = np.linalg.norm(refined)
        if length > 1e-12:
            axis = refined / length
    return axis, float(scores[best])


def _pca_axes(points, areas=None, centers=None):
    """Principal axes (columns, determinant +1) of the surface: face centers
    weighted by face area when given, vertices otherwise. Confidence is the
    smallest relative gap between the three spreads (0 for a sphere)."""
    import numpy as np
    if centers is not None and areas is not None and len(areas) and float(np.sum(areas)) > 0:
        samples = np.asarray(centers, dtype=float).reshape(-1, 3)
        weights = np.asarray(areas, dtype=float).ravel()
    else:
        samples = np.asarray(points, dtype=float).reshape(-1, 3)
        weights = np.ones(len(samples))
    if len(samples) == 0:
        return np.identity(3), 0.0
    total = weights.sum()
    mean = (samples * weights[:, None]).sum(axis=0) / total
    centered = samples - mean
    covariance = (centered * weights[:, None]).T @ centered / total
    values, vectors = np.linalg.eigh(covariance)
    order = np.argsort(values)[::-1]
    values = np.clip(values[order], 0.0, None)
    frame = vectors[:, order]
    if np.linalg.det(frame) < 0:
        frame[:, 2] = -frame[:, 2]
    if values[0] <= 1e-20:
        return frame, 0.0
    gap = min(values[0] - values[1], values[1] - values[2]) / values[0]
    return frame, float(gap)


def natural_axes(points, normals=None, areas=None, centers=None,
                 cone_degrees=AXIS_CONE_DEGREES):
    """Natural axes of a vertex cloud with its face normals and areas.

    Returns (frame, confidence, method): frame is a 3x3 numpy rotation
    (determinant +1) whose columns are the axes in the input's space,
    confidence is in [0, 1], method 'NORMALS' (area vote on the face
    normals) or 'PCA' (fallback for shapes without clear flat faces)."""
    import math
    import numpy as np
    cos_limit = math.cos(math.radians(cone_degrees))
    sin_limit = math.sin(math.radians(cone_degrees))
    planar = None
    if normals is not None and areas is not None and len(areas):
        normals = np.asarray(normals, dtype=float).reshape(-1, 3)
        areas = np.asarray(areas, dtype=float).ravel()
        lengths = np.linalg.norm(normals, axis=1)
        valid = (lengths > 1e-12) & (areas > 0)
        unit = normals[valid] / lengths[valid][:, None]
        weight = areas[valid]
        total = float(weight.sum())
        if total > 0:
            first, weight_first = _dominant_direction(unit, weight, cos_limit)
            if first is not None:
                dots = unit @ first
                side = np.abs(dots) <= sin_limit
                projected = unit[side] - dots[side][:, None] * first
                projected_length = np.linalg.norm(projected, axis=1)
                usable = projected_length > 1e-12
                second, weight_second = _dominant_direction(
                    projected[usable] / projected_length[usable][:, None],
                    weight[side][usable], cos_limit)
                if second is not None:
                    second = second - (second @ first) * first
                    second /= np.linalg.norm(second)
                    third = np.cross(first, second)
                    weight_third = float(weight[np.abs(unit @ third) >= cos_limit].sum())
                    confidence = min(1.0, (weight_first + weight_second + weight_third) / total)
                    planar = (np.column_stack((first, second, third)), confidence)
    if planar is not None and planar[1] >= PLANAR_MIN_CONFIDENCE:
        return planar[0], planar[1], 'NORMALS'
    frame, confidence = _pca_axes(points, areas, centers)
    if planar is not None and planar[1] >= confidence:
        return planar[0], planar[1], 'NORMALS'
    return frame, confidence, 'PCA'


def canonical_frames(points, frame, decimals=4):
    """Canonical form of a vertex cloud expressed in `frame` (columns =
    axes): among the 24 cube rotations of the frame, the one(s) giving the
    lexicographically smallest sorted tuple of rounded coordinates measured
    from the centroid.

    Returns (signature, rotations, centroid): signature is a tuple of
    (x, y, z) integers in units of 10**-decimals (rounded after rotating,
    so tiny values snap to 0 and -0 cannot appear); rotations lists the
    3x3 numpy matrices R of every tied frame, p - centroid = R @ u for the
    canonical coordinates u (several only for symmetric shapes)."""
    import numpy as np
    points = np.asarray(points, dtype=float).reshape(-1, 3)
    centroid = points.mean(axis=0) if len(points) else np.zeros(3)
    frame = np.asarray(frame, dtype=float)
    local = (points - centroid) @ frame
    scale = 10.0 ** decimals
    best = None
    tied = []
    for cube in cube_rotations():
        rounded = np.rint(local @ cube.T * scale).astype(np.int64)
        order = np.lexsort((rounded[:, 2], rounded[:, 1], rounded[:, 0]))
        flat = rounded[order].ravel()
        if best is None:
            best, tied = flat, [cube]
            continue
        differ = np.nonzero(flat != best)[0]
        if not len(differ):
            tied.append(cube)
        elif flat[differ[0]] < best[differ[0]]:
            best, tied = flat, [cube]
    signature = tuple(map(tuple, best.reshape(-1, 3).tolist())) if best is not None else ()
    rotations = [frame @ cube.T for cube in tied]
    return signature, rotations, centroid


def canonical_signature(points, normals=None, areas=None, centers=None, decimals=4):
    """Rotation and translation invariant signature of a vertex cloud.
    Returns (signature_tuple, rotation_used, centroid) with
    p - centroid = rotation_used @ u for the canonical coordinates u."""
    frame, _confidence, _method = natural_axes(points, normals, areas, centers)
    signature, rotations, centroid = canonical_frames(points, frame, decimals)
    return signature, rotations[0], centroid


def shape_invariant_key(points):
    """Coarse rotation-invariant description of a vertex cloud (count and
    spreads along the principal axes, 3 significant digits). Finds
    candidates when the exact signature missed a match on a rounding
    boundary; every candidate is verified afterwards."""
    import numpy as np
    points = np.asarray(points, dtype=float).reshape(-1, 3)
    if len(points) == 0:
        return (0,)
    centered = points - points.mean(axis=0)
    values = np.sort(np.clip(np.linalg.eigvalsh(centered.T @ centered / len(points)), 0, None))[::-1]
    spreads = np.sqrt(values)
    if spreads[0] <= 1e-9:
        return (len(points), 0.0)
    return (len(points), float('%.3g' % spreads[0])) + tuple(
        round(float(s / spreads[0]), 3) for s in spreads[1:])


def rigid_match_tolerance(points):
    """Distance under which two vertices count as one, for a shape this size."""
    import numpy as np
    points = np.asarray(points, dtype=float).reshape(-1, 3)
    if len(points) == 0:
        return 1e-4
    extent = float(np.max(points.max(axis=0) - points.min(axis=0)))
    return max(1e-4, extent * 1e-5)


def find_rigid_match(source, target, source_rotation, target_rotation,
                     source_center, target_center, tolerance,
                     same_order=True, accept=None):
    """Rotation `rot` (3x3 numpy) with target ~ target_center +
    rot @ (source - source_center), searched among the 24 frames equivalent
    to the canonical ones (rot = target_rotation @ Q @ source_rotation^T).

    same_order: vertex i of the source must land on vertex i of the target.
    Otherwise every source vertex must land on a distinct target vertex
    (KD-tree). `accept(mapping)` may refuse a candidate (UVs, materials):
    mapping[i] is the target index of source vertex i.
    Returns (rot, mapping) or (None, None)."""
    import numpy as np
    source = np.asarray(source, dtype=float).reshape(-1, 3)
    target = np.asarray(target, dtype=float).reshape(-1, 3)
    if len(source) != len(target) or len(source) == 0:
        return None, None
    tree = None
    if not same_order:
        import mathutils.kdtree
        tree = mathutils.kdtree.KDTree(len(target))
        for index, co in enumerate(target.tolist()):
            tree.insert(co, index)
        tree.balance()
    centered = source - np.asarray(source_center, dtype=float)
    for cube in cube_rotations():
        rotation = np.asarray(target_rotation) @ cube @ np.asarray(source_rotation).T
        moved = centered @ rotation.T + np.asarray(target_center, dtype=float)
        if same_order:
            if float(np.max(np.linalg.norm(moved - target, axis=1))) > tolerance:
                continue
            mapping = list(range(len(source)))
        else:
            mapping = []
            seen = set()
            for co in moved.tolist():
                _co, index, distance = tree.find(co)
                if index is None or distance > tolerance or index in seen:
                    mapping = None
                    break
                seen.add(index)
                mapping.append(index)
            if mapping is None:
                continue
        if accept is None or accept(mapping):
            return rotation, mapping
    return None, None


def mesh_surface_arrays(mesh):
    """(points, face normals, face areas, face centers) of a mesh as numpy
    arrays, in mesh space."""
    import numpy as np
    points = np.empty(len(mesh.vertices) * 3)
    mesh.vertices.foreach_get('co', points)
    count = len(mesh.polygons)
    normals = np.empty(count * 3)
    try:
        mesh.polygon_normals.foreach_get('vector', normals)
    except AttributeError:
        mesh.polygons.foreach_get('normal', normals)
    areas = np.empty(count)
    mesh.polygons.foreach_get('area', areas)
    centers = np.empty(count * 3)
    mesh.polygons.foreach_get('center', centers)
    return (points.reshape(-1, 3), normals.reshape(-1, 3), areas, centers.reshape(-1, 3))


def numpy_to_matrix3(array):
    """3x3 numpy array to a mathutils Matrix."""
    return mathutils.Matrix([[float(value) for value in row] for row in array])
