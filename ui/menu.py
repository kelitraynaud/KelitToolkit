"""Menu UI for KelitToolkit"""

import bpy
from ..operators.instances import (
    OBJECT_OT_replace_with_active_instance,
    OBJECT_OT_realize_modifiers_to_instances,
    OBJECT_OT_apply_all_modifiers,
)
from ..operators.origin import (
    OBJECT_OT_set_origin_preset,
    OBJECT_OT_set_origin_custom,
    OBJECT_OT_set_origin_with_modifier_compensation,
)
from ..operators.naming import (
    OBJECT_OT_normalize_names_quick,
    OBJECT_OT_normalize_names_advanced,
    OBJECT_OT_batch_find_replace,
    OBJECT_OT_clean_object_names,
    OBJECT_OT_set_mesh_name_from_object,
    OBJECT_OT_material_name_from_mesh,
    OBJECT_OT_add_prefix_to_selected,
)
from ..operators.materials import (
    OBJECT_OT_create_material_by_mesh,
    OBJECT_OT_delete_unused_materials,
    OBJECT_OT_purge_unused_materials,
)
from ..operators.material_conversion import (
    OBJECT_OT_convert_to_simple_pbr,
    OBJECT_OT_detect_unsupported_nodes,
)
from ..operators.textures import (
    OBJECT_OT_auto_setup_pbr_textures,
)
from ..operators.scene_cleanup import (
    OBJECT_OT_delete_hidden_objects,
    OBJECT_OT_remove_empty_collections,
    OBJECT_OT_purge_zero_face_meshes,
)
from ..operators.mesh_tools import (
    OBJECT_OT_enhance_low_poly,
    OBJECT_OT_create_collision_mesh,
    OBJECT_OT_generate_lods,
)
from ..operators.export import (
    OBJECT_OT_validate_for_unreal,
    OBJECT_OT_apply_scale_instances,
    OBJECT_OT_normalize_scene_scale,
    OBJECT_OT_apply_all_transforms,
    OBJECT_OT_batch_fbx_export,
)
from ..operators.unified_export import (
    UNREAL_OT_export_assets,
)


class VIEW3D_MT_unreal_toolkit_menu(bpy.types.Menu):
    bl_label = "Unreal Toolkit"
    bl_idname = "VIEW3D_MT_unreal_toolkit_menu"

    def draw(self, context):
        layout = self.layout

        layout.label(text="Instances")
        layout.operator(OBJECT_OT_replace_with_active_instance.bl_idname, icon='AUTOMERGE_ON')
        layout.operator(OBJECT_OT_realize_modifiers_to_instances.bl_idname, icon='MODIFIER', text="Realize Modifiers to Instances")

        layout.separator()
        layout.label(text="Origin")
        layout.operator(OBJECT_OT_set_origin_preset.bl_idname, icon='SNAP_MIDPOINT', text="Bottom Center").preset = 'BOTTOM_CENTER'
        layout.operator(OBJECT_OT_set_origin_custom.bl_idname, icon='ORIENTATION_GIMBAL')
        layout.operator(OBJECT_OT_set_origin_with_modifier_compensation.bl_idname, icon='MODIFIER', text="Bottom Center (Fix Array)").preset = 'BOTTOM_CENTER'

        layout.separator()
        layout.label(text="Naming")
        layout.operator(OBJECT_OT_normalize_names_quick.bl_idname, icon='FILE_REFRESH')
        layout.operator(OBJECT_OT_normalize_names_advanced.bl_idname, icon='PREFERENCES')
        layout.operator(OBJECT_OT_batch_find_replace.bl_idname, icon='ZOOM_IN')
        layout.operator(OBJECT_OT_clean_object_names.bl_idname, icon='BRUSH_DATA')
        layout.operator(OBJECT_OT_set_mesh_name_from_object.bl_idname, icon='FONT_DATA')
        layout.operator(OBJECT_OT_material_name_from_mesh.bl_idname, icon='MATERIAL')
        layout.operator(OBJECT_OT_add_prefix_to_selected.bl_idname, icon='ADD')

        layout.separator()
        layout.label(text="Materials")
        layout.operator(OBJECT_OT_auto_setup_pbr_textures.bl_idname, icon='TEXTURE')
        layout.operator(OBJECT_OT_create_material_by_mesh.bl_idname, icon='ADD')
        layout.operator(OBJECT_OT_delete_unused_materials.bl_idname, icon='TRASH')
        layout.operator(OBJECT_OT_purge_unused_materials.bl_idname, icon='CANCEL')

        layout.separator()
        layout.label(text="Material Conversion (Unreal)")
        layout.operator(OBJECT_OT_convert_to_simple_pbr.bl_idname, icon='SHADING_SOLID')
        layout.operator(OBJECT_OT_detect_unsupported_nodes.bl_idname, icon='ERROR')

        layout.separator()
        layout.label(text="Scene Cleanup")
        layout.operator(OBJECT_OT_delete_hidden_objects.bl_idname, icon='RESTRICT_VIEW_ON')
        layout.operator(OBJECT_OT_remove_empty_collections.bl_idname, icon='OUTLINER_COLLECTION')
        layout.operator(OBJECT_OT_purge_zero_face_meshes.bl_idname, icon='MESH_DATA')

        layout.separator()
        layout.label(text="Mesh Tools")
        layout.operator(OBJECT_OT_apply_all_modifiers.bl_idname, icon='MODIFIER')
        layout.operator(OBJECT_OT_enhance_low_poly.bl_idname, icon='SMOOTHCURVE')

        layout.separator()
        layout.label(text="Collisions & LODs")
        layout.operator(OBJECT_OT_create_collision_mesh.bl_idname, icon='CUBE')
        layout.operator(OBJECT_OT_generate_lods.bl_idname, icon='MESH_DATA')

        layout.separator()
        layout.label(text="Export")
        layout.operator(UNREAL_OT_export_assets.bl_idname,
                        text="Export Assets (USD / FBX)", icon='EXPORT')
        layout.operator(OBJECT_OT_validate_for_unreal.bl_idname, icon='CHECKMARK')
        layout.operator(OBJECT_OT_apply_scale_instances.bl_idname, icon='FULLSCREEN_ENTER')
        layout.operator(OBJECT_OT_normalize_scene_scale.bl_idname, icon='FULLSCREEN_ENTER')
        layout.operator(OBJECT_OT_apply_all_transforms.bl_idname, icon='ORIENTATION_GLOBAL')
        layout.operator(OBJECT_OT_batch_fbx_export.bl_idname, icon='EXPORT')


def menu_func(self, context):
    self.layout.menu(VIEW3D_MT_unreal_toolkit_menu.bl_idname)


# Registration
classes = (
    VIEW3D_MT_unreal_toolkit_menu,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.VIEW3D_MT_object.append(menu_func)


def unregister():
    bpy.types.VIEW3D_MT_object.remove(menu_func)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
