import bpy
from ..utils import clean_name


# ============================================================================
# OPERATORS - MATERIALS
# ============================================================================

class OBJECT_OT_create_material_by_mesh(bpy.types.Operator):
    """Create a unique material for each selected mesh"""
    bl_idname = "kelit_toolkit.create_material_by_mesh"
    bl_label = "Create Material By Mesh"
    bl_options = {'REGISTER', 'UNDO'}

    use_prefix_conversion: bpy.props.BoolProperty(
        name="Convert SM_ to M_",
        description="Replace SM_ prefix with M_ in material name",
        default=True
    )

    def execute(self, context):
        meshes_done = set()
        count = 0

        for obj in context.selected_objects:
            if obj.type != 'MESH':
                continue

            mesh = obj.data
            if mesh.name in meshes_done:
                continue

            meshes_done.add(mesh.name)

            if not mesh.materials or mesh.materials[0] is None:
                continue

            old_mat = mesh.materials[0]

            new_mat_name = mesh.name
            if self.use_prefix_conversion and new_mat_name.startswith("SM_"):
                new_mat_name = "M_" + new_mat_name[3:]
            else:
                new_mat_name = "M_" + new_mat_name

            if new_mat_name in bpy.data.materials:
                new_mat = bpy.data.materials[new_mat_name]
            else:
                new_mat = old_mat.copy()
                new_mat.name = new_mat_name
                count += 1

            mesh.materials.clear()
            mesh.materials.append(new_mat)

        self.report({'INFO'}, f"{count} material(s) created")
        return {'FINISHED'}


class OBJECT_OT_delete_unused_materials(bpy.types.Operator):
    """Delete unused material slots on selected objects"""
    bl_idname = "kelit_toolkit.delete_unused_materials"
    bl_label = "Delete Unused Materials"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        selected_objs = [obj for obj in context.selected_objects if obj.type == 'MESH']

        for obj in selected_objs:
            mesh = obj.data

            used_slots = set()
            for poly in mesh.polygons:
                used_slots.add(poly.material_index)

            if not used_slots or len(used_slots) == len(obj.material_slots):
                continue

            # Rebuild the slot list, then remap polygon indices to the new
            # positions - without this the polygons keep their old indices
            # and end up pointing at the wrong materials
            kept = [i for i in sorted(used_slots) if i < len(obj.material_slots)]
            index_remap = {old: new for new, old in enumerate(kept)}
            new_slots = [obj.material_slots[i].material for i in kept]

            new_indices = [index_remap.get(p.material_index, 0) for p in mesh.polygons]

            mesh.materials.clear()
            for mat in new_slots:
                mesh.materials.append(mat)
            for poly, new_index in zip(mesh.polygons, new_indices):
                poly.material_index = new_index
            mesh.update()

        self.report({'INFO'}, f"Materials cleaned on {len(selected_objs)} object(s)")
        return {'FINISHED'}


class OBJECT_OT_purge_unused_materials(bpy.types.Operator):
    """Delete all unused materials from the Blender project"""
    bl_idname = "kelit_toolkit.purge_unused_materials"
    bl_label = "Purge Unused Materials"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        # List of materials to remove (those with 0 users)
        materials_to_remove = [mat for mat in bpy.data.materials if mat.users == 0]

        # Remove unused materials
        for mat in materials_to_remove:
            bpy.data.materials.remove(mat)

        removed_count = len(materials_to_remove)
        remaining_count = len(bpy.data.materials)

        if removed_count > 0:
            self.report({'INFO'}, f"{removed_count} material(s) removed - {remaining_count} remaining")
        else:
            self.report({'INFO'}, "No unused materials found")

        return {'FINISHED'}


classes = (
    OBJECT_OT_create_material_by_mesh,
    OBJECT_OT_delete_unused_materials,
    OBJECT_OT_purge_unused_materials,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
