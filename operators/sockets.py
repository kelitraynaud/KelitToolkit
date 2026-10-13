"""Static Mesh sockets, with the same convention as Unreal's FBX import.

An Empty named 'SOCKET_<Name>' (prefix case-insensitive) parented to a mesh
object defines the socket <Name> on that mesh's Static Mesh. Its transform
is the empty relative to the mesh object, converted to Unreal space the way
Send to Unreal converts locations and rotations.

Send to Unreal keeps these empties out of the level and the USD file and
writes them as StaticMeshSockets on the asset instead; the FBX exports ship
them as children of their mesh, which Unreal's FBX importer reads natively.
SOCKET_ empties under an armature (bone sockets) are not supported.
"""

import bpy

SOCKET_PREFIX = 'SOCKET_'


def has_socket_prefix(obj):
    return obj.type == 'EMPTY' and obj.name.upper().startswith(SOCKET_PREFIX)


def is_mesh_socket(obj):
    """An Empty named SOCKET_* whose parent is a mesh object."""
    return (has_socket_prefix(obj) and obj.parent is not None
            and obj.parent.type == 'MESH')


def is_bone_socket(obj):
    """A SOCKET_* empty parented to an armature: out of scope, ignored."""
    return (has_socket_prefix(obj) and obj.parent is not None
            and obj.parent.type == 'ARMATURE')


def is_socket_marker(obj):
    """Socket empties never become level actors or USD prims."""
    return is_mesh_socket(obj) or is_bone_socket(obj)


def socket_children(obj):
    """The SOCKET_ empties directly parented to a mesh object, by name."""
    if obj is None or obj.type != 'MESH':
        return []
    return sorted((child for child in obj.children if is_mesh_socket(child)),
                  key=lambda child: child.name)


def bone_sockets(objects):
    """Names of the SOCKET_ empties parented to the armatures in *objects*."""
    found = []
    for obj in objects:
        if obj.type == 'ARMATURE':
            found.extend(child.name for child in obj.children if is_bone_socket(child))
    return sorted(set(found))


def sanitize_socket_name(name):
    """Unreal-safe socket name: ASCII letters, digits and underscores."""
    cleaned = ''.join(char if (char.isascii() and (char.isalnum() or char == '_')) else '_'
                      for char in name).strip('_')
    return cleaned or 'Socket'


def socket_name(obj):
    """'SOCKET_Muzzle.001' -> 'Muzzle_001'."""
    return sanitize_socket_name(obj.name[len(SOCKET_PREFIX):])


def _clean(values, digits):
    # rounded, and never '-0.0' (keeps payloads stable between sends)
    return [round(float(value), digits) + 0.0 for value in values]


def extract_sockets(mesh_obj):
    """
    [{'name', 'blender_name', 'location', 'rotation', 'scale'}] for the
    sockets of one mesh object, in Unreal space relative to the mesh: the
    asset's geometry is the mesh in its own local space, so the socket is
    mesh_world^-1 @ empty_world. Location in cm (Y flipped), rotation as
    [pitch, yaw, roll] degrees, scale as is.
    """
    from .usd_sync import (convert_blender_to_unreal_location,
                           convert_blender_rotation_to_unreal_rotation,
                           decompose_signed)
    sockets = []
    taken = set()
    inverse = mesh_obj.matrix_world.inverted_safe()
    for child in socket_children(mesh_obj):
        base = socket_name(child)
        name, index = base, 1
        # Unreal compares socket names without case
        while name.lower() in taken:
            index += 1
            name = f'{base}_{index}'
        taken.add(name.lower())
        location, euler, scale = decompose_signed(inverse @ child.matrix_world)
        sockets.append({
            'name': name,
            'blender_name': child.name,
            'location': _clean(convert_blender_to_unreal_location(location), 4),
            'rotation': _clean(convert_blender_rotation_to_unreal_rotation(euler), 4),
            'scale': _clean(scale, 5),
        })
    return sockets


# ============================================================================
# OPERATOR
# ============================================================================

class OBJECT_OT_kelit_add_socket(bpy.types.Operator):
    """Add a socket to the active mesh: a SOCKET_ empty at the 3D cursor, parented to the mesh. Sent to Unreal as a Static Mesh socket"""
    bl_idname = "kelit_toolkit.add_socket"
    bl_label = "Add Socket"
    bl_options = {'REGISTER', 'UNDO'}

    socket_name: bpy.props.StringProperty(
        name="Socket Name",
        description="Name of the socket in Unreal. The empty is called SOCKET_<name>",
        default="Socket",
    )

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return context.mode == 'OBJECT' and obj is not None and obj.type == 'MESH'

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        mesh_obj = context.active_object
        base = SOCKET_PREFIX + sanitize_socket_name(self.socket_name)
        name, index = base, 1
        taken = {obj.name.lower() for obj in bpy.data.objects}
        while name.lower() in taken:
            index += 1
            name = f'{base}_{index}'

        empty = bpy.data.objects.new(name, None)
        empty.empty_display_type = 'ARROWS'
        size = max(mesh_obj.dimensions)
        empty.empty_display_size = size * 0.25 if size > 1e-6 else 0.25
        collections = list(mesh_obj.users_collection) or [context.scene.collection]
        for collection in collections:
            collection.objects.link(empty)
        # parent with the world transform kept: the empty stays at the cursor
        empty.matrix_world = context.scene.cursor.matrix.copy()
        empty.parent = mesh_obj
        empty.matrix_parent_inverse = mesh_obj.matrix_world.inverted_safe()

        for obj in context.selected_objects:
            obj.select_set(False)
        empty.select_set(True)
        context.view_layer.objects.active = empty
        self.report({'INFO'}, f"Socket '{name}' added to {mesh_obj.name}")
        return {'FINISHED'}


classes = (
    OBJECT_OT_kelit_add_socket,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
