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
