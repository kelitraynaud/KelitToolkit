"""Main panel UI for KelitToolkit.

Three sections, in workflow order:
- Send to Unreal: the one-click USD sync (hero) + file export, advanced tools folded away
- Prepare Assets: transforms, instances, origins, mesh tools
- Clean & Check: naming, materials, scene cleanup, validation
"""

import bpy
from ..operators.instances import (
    OBJECT_OT_detect_and_replace_instances,
    OBJECT_OT_replace_with_active_instance,
    OBJECT_OT_group_similar_in_collection,
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
    OBJECT_OT_set_mesh_name_from_object,
    OBJECT_OT_set_object_name_from_mesh,
    OBJECT_OT_material_name_from_mesh,
    OBJECT_OT_add_prefix_to_selected,
    OBJECT_OT_clean_object_names,
)
from ..operators.materials import (
    OBJECT_OT_create_material_by_mesh,
    OBJECT_OT_delete_unused_materials,
    OBJECT_OT_purge_unused_materials,
)
from ..operators.material_conversion import (
    OBJECT_OT_convert_to_simple_pbr,
    OBJECT_OT_detect_unsupported_nodes,
    OBJECT_OT_bake_procedural_to_texture,
    OBJECT_OT_texture_path_validator,
    OBJECT_OT_rename_textures_for_unreal,
    OBJECT_OT_extract_textures_from_nodes,
)
from ..operators.textures import (
    OBJECT_OT_auto_setup_pbr_textures,
)
from ..operators.scene_cleanup import (
    OBJECT_OT_delete_hidden_objects,
    OBJECT_OT_remove_empty_collections,
    OBJECT_OT_purge_zero_face_meshes,
    OBJECT_OT_clean_vertex_groups,
    OBJECT_OT_delete_unused_empties,
    OBJECT_OT_remove_empty_parents,
    OBJECT_OT_remove_unused_mesh_data,
)
from ..operators.mesh_tools import (
    OBJECT_OT_enhance_low_poly,
    OBJECT_OT_create_collision_mesh,
    OBJECT_OT_generate_lods,
    OBJECT_OT_convert_to_low_poly,
)
from ..operators.export import (
    OBJECT_OT_validate_for_unreal,
    OBJECT_OT_apply_scale_instances,
    OBJECT_OT_apply_rotation_instances,
    OBJECT_OT_normalize_scene_scale,
    OBJECT_OT_bake_camera_animation,
    OBJECT_OT_batch_fbx_export,
)
from ..operators.unified_export import (
    UNREAL_OT_export_assets,
)
from ..operators.unreal_link import (
    UNREAL_OT_connection_doctor,
    UNREAL_OT_test_connection,
    UNREAL_OT_fix_interchange_permanent,
)
from ..operators.usd_sync import (
    UNREAL_OT_usd_scene_sync,
    UNREAL_OT_usd_make_spawnable,
    UNREAL_OT_usd_clear_synced,
    UNREAL_OT_usd_export_hierarchy,
)
from ..operators.ue_materials import (
    UNREAL_OT_build_material_instances,
)


# ============================================================================
# QUICK SEARCH
# ============================================================================
# The index is built from the operator modules' own `classes` tuples, so any
# operator added to the addon is searchable without touching this file.

from ..operators import (
    instances as _m_instances,
    origin as _m_origin,
    naming as _m_naming,
    materials as _m_materials,
    material_conversion as _m_material_conversion,
    textures as _m_textures,
    mesh_tools as _m_mesh_tools,
    scene_cleanup as _m_scene_cleanup,
    export as _m_export,
    unified_export as _m_unified_export,
    unreal_link as _m_unreal_link,
    usd_sync as _m_usd_sync,
    ue_materials as _m_ue_materials,
)

SEARCH_SECTIONS = (
    (_m_usd_sync, 'Send · USD'),
    (_m_unified_export, 'Send · Export'),
    (_m_unreal_link, 'Send · Unreal Link'),
    (_m_ue_materials, 'Send · Materials'),
    (_m_export, 'Send · Export / Validate'),
    (_m_instances, 'Prepare · Instances'),
    (_m_origin, 'Prepare · Origin'),
    (_m_mesh_tools, 'Prepare · Mesh'),
    (_m_naming, 'Clean · Naming'),
    (_m_materials, 'Clean · Materials'),
    (_m_material_conversion, 'Clean · Materials'),
    (_m_textures, 'Clean · Materials'),
    (_m_scene_cleanup, 'Clean · Scene'),
)

_search_index = None


def get_search_index():
    """[(section, label, idname, haystack)] for every operator of the addon."""
    global _search_index
    if _search_index is None:
        index = []
        for module, section in SEARCH_SECTIONS:
            for cls in getattr(module, 'classes', ()):
                idname = getattr(cls, 'bl_idname', None)
                label = getattr(cls, 'bl_label', None)
                if not idname or not label:
                    continue
                description = (cls.__doc__ or '').strip()
                haystack = ' '.join((label, idname, description, section)).lower()
                index.append((section, label, idname, haystack))
        _search_index = index
    return _search_index


def search_operators(query):
    """Entries whose text contains every word of *query* (case-insensitive)."""
    words = [w for w in query.lower().split() if w]
    if not words:
        return []
    return [entry for entry in get_search_index()
            if all(word in entry[3] for word in words)]


class UNREAL_OT_clear_panel_search(bpy.types.Operator):
    """Clear the toolkit search and bring the sections back"""
    bl_idname = "unreal_toolkit.clear_panel_search"
    bl_label = "Clear Search"

    def execute(self, context):
        context.scene.unreal_toolkit_settings.panel_search = ""
        return {'FINISHED'}


def section_header(layout, settings, prop_name, title, icon):
    """Collapsible section header row. Returns True when the section is open."""
    box = layout.box()
    row = box.row()
    expanded = getattr(settings, prop_name)
    row.prop(settings, prop_name,
             icon='DOWNARROW_HLT' if expanded else 'RIGHTARROW',
             icon_only=True, emboss=False)
    row.label(text=title, icon=icon)
    return box, expanded


class VIEW3D_PT_unreal_toolkit(bpy.types.Panel):
    bl_label = "Kelit Toolkit"
    bl_idname = "VIEW3D_PT_unreal_toolkit"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Kelit Toolkit"
    bl_context = "objectmode"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.unreal_toolkit_settings

        # quick search: type to filter every tool, clear to get sections back
        row = layout.row(align=True)
        row.prop(settings, "panel_search", text="", icon='VIEWZOOM')
        if settings.panel_search:
            row.operator(UNREAL_OT_clear_panel_search.bl_idname, text="", icon='X')
            self.draw_search_results(layout, settings.panel_search)
            return

        self.draw_send(layout, settings)
        self.draw_prepare(layout, settings)
        self.draw_clean(layout, settings)

    def draw_search_results(self, layout, query):
        matches = search_operators(query)
        if not matches:
            box = layout.box()
            box.label(text="No matching tool", icon='INFO')
            return

        current_section = None
        col = None
        for section, label, idname, _haystack in matches:
            if section != current_section:
                current_section = section
                box = layout.box()
                box.label(text=section)
                col = box.column(align=True)
            col.operator(idname, text=label)

    # ------------------------------------------------------------------
    # SEND TO UNREAL
    # ------------------------------------------------------------------
    def draw_send(self, layout, settings):
        box, expanded = section_header(layout, settings, "show_send",
                                       "Send to Unreal", 'EXPORT')
        if not expanded:
            return

        col = box.column(align=True)
        col.scale_y = 1.5
        col.operator(UNREAL_OT_usd_scene_sync.bl_idname,
                     text="Send to Unreal (USD)", icon='EXPORT')

        sub = box.box()
        sub.label(text="Static + Skeletal (anim) auto-detected", icon='INFO')
        sub.label(text="Sends the selection ('Export' collection if empty)")

        box.prop(settings, "usd_content_folder")

        box.operator(UNREAL_OT_export_assets.bl_idname,
                     text="Export Files Instead (USD / FBX)", icon='FILE_TICK')

        # --- advanced, folded by default ---
        row = box.row()
        row.prop(settings, "show_send_advanced",
                 icon='DOWNARROW_HLT' if settings.show_send_advanced else 'RIGHTARROW',
                 icon_only=True, emboss=False)
        row.label(text="Advanced", icon='PREFERENCES')
        if not settings.show_send_advanced:
            return

        col = box.column(align=True)
        col.operator(UNREAL_OT_test_connection.bl_idname,
                     text="Test Unreal Connection", icon='CHECKMARK')
        col.operator(UNREAL_OT_connection_doctor.bl_idname,
                     text="Connection Doctor (Setup Help)", icon='COMMUNITY')
        col.operator(UNREAL_OT_usd_clear_synced.bl_idname,
                     text="Clear All Synced Actors", icon='TRASH')

        box.separator()
        box.label(text="Sequence & Materials:")
        col = box.column(align=True)
        col.operator(UNREAL_OT_usd_make_spawnable.bl_idname,
                     text="Make Sequence Self-Contained", icon='SEQUENCE')
        col.operator(UNREAL_OT_build_material_instances.bl_idname,
                     text="Build Material Instances", icon='MATERIAL')
        box.prop(settings, "ue_master_material")

        box.separator()
        box.label(text="File Export:")
        col = box.column(align=True)
        col.operator(UNREAL_OT_usd_export_hierarchy.bl_idname,
                     text="Export USD Hierarchy Only", icon='FILE_3D')
        col.operator(OBJECT_OT_batch_fbx_export.bl_idname,
                     text="Batch FBX Export", icon='EXPORT')

        box.separator()
        box.label(text="Interchange FBX Fix (UE 5.5+):")
        col = box.column(align=True)
        col.operator(UNREAL_OT_fix_interchange_permanent.bl_idname,
                     text="Fix Permanently (DefaultEngine.ini)", icon='FILE_TICK')
        box.prop(settings, "ue_project_dir")

    # ------------------------------------------------------------------
    # PREPARE ASSETS
    # ------------------------------------------------------------------
    def draw_prepare(self, layout, settings):
        box, expanded = section_header(layout, settings, "show_prepare",
                                       "Prepare Assets", 'TOOL_SETTINGS')
        if not expanded:
            return

        box.label(text="Transforms (Instances Safe):")
        col = box.column(align=True)
        col.operator(OBJECT_OT_apply_scale_instances.bl_idname,
                     text="Apply Scale", icon='FULLSCREEN_ENTER')
        col.operator(OBJECT_OT_apply_rotation_instances.bl_idname,
                     text="Apply Rotation", icon='ORIENTATION_GIMBAL')
        col.operator(OBJECT_OT_normalize_scene_scale.bl_idname,
                     text="Normalize Scene Scale", icon='FULLSCREEN_ENTER')
        col.operator(OBJECT_OT_bake_camera_animation.bl_idname,
                     text="Bake Camera Animation", icon='CAMERA_DATA')

        box.separator()
        box.label(text="Instances:")
        col = box.column(align=True)
        col.operator(OBJECT_OT_detect_and_replace_instances.bl_idname,
                     text="Detect Duplicates > Instances", icon='VIEWZOOM')
        col.operator(OBJECT_OT_replace_with_active_instance.bl_idname,
                     text="Replace with Active", icon='AUTOMERGE_ON')
        col.operator(OBJECT_OT_group_similar_in_collection.bl_idname,
                     text="Group Similar in Collection", icon='GROUP')
        col.operator(OBJECT_OT_realize_modifiers_to_instances.bl_idname,
                     text="Realize Modifiers to Instances", icon='MODIFIER')

        box.separator()
        box.label(text="Origin:")
        col = box.column(align=True)
        col.operator(OBJECT_OT_set_origin_preset.bl_idname,
                     text="Bottom Center", icon='ANCHOR_BOTTOM').preset = 'BOTTOM_CENTER'
        col.operator(OBJECT_OT_set_origin_preset.bl_idname,
                     text="Center", icon='ANCHOR_CENTER').preset = 'CENTER'
        col.operator(OBJECT_OT_set_origin_preset.bl_idname,
                     text="Top Center", icon='ANCHOR_TOP').preset = 'TOP_CENTER'
        col.operator(OBJECT_OT_set_origin_custom.bl_idname,
                     text="Custom Position", icon='ORIENTATION_GIMBAL')
        col.operator(OBJECT_OT_set_origin_with_modifier_compensation.bl_idname,
                     text="Bottom Center (Fix Array)", icon='MODIFIER').preset = 'BOTTOM_CENTER'

        box.separator()
        box.label(text="Mesh:")
        col = box.column(align=True)
        col.operator(OBJECT_OT_apply_all_modifiers.bl_idname,
                     text="Apply All Modifiers", icon='MODIFIER')
        col.operator(OBJECT_OT_enhance_low_poly.bl_idname,
                     text="Enhance Low Poly", icon='SMOOTHCURVE')
        col.operator(OBJECT_OT_convert_to_low_poly.bl_idname,
                     text="High to Low Poly (Bake)", icon='MOD_REMESH')
        col.operator(OBJECT_OT_create_collision_mesh.bl_idname,
                     text="Create Collision Mesh", icon='MESH_ICOSPHERE')
        col.operator(OBJECT_OT_generate_lods.bl_idname,
                     text="Generate LODs", icon='OUTLINER_DATA_MESH')

    # ------------------------------------------------------------------
    # CLEAN & CHECK
    # ------------------------------------------------------------------
    def draw_clean(self, layout, settings):
        box, expanded = section_header(layout, settings, "show_clean",
                                       "Clean & Check", 'BRUSH_DATA')
        if not expanded:
            return

        box.label(text="Naming:")
        col = box.column(align=True)
        col.operator(OBJECT_OT_normalize_names_quick.bl_idname,
                     text="Normalize Names (Quick)", icon='FILE_REFRESH')
        col.operator(OBJECT_OT_normalize_names_advanced.bl_idname,
                     text="Normalize (Advanced)", icon='PREFERENCES')
        col.operator(OBJECT_OT_batch_find_replace.bl_idname,
                     text="Find & Replace", icon='ZOOM_IN')
        col.operator(OBJECT_OT_clean_object_names.bl_idname,
                     text="Clean Suffixes", icon='BRUSH_DATA')
        col.operator(OBJECT_OT_set_mesh_name_from_object.bl_idname,
                     text="Mesh Name from Object", icon='FONT_DATA')
        col.operator(OBJECT_OT_set_object_name_from_mesh.bl_idname,
                     text="Object Name from Mesh", icon='OBJECT_DATA')
        col.operator(OBJECT_OT_material_name_from_mesh.bl_idname,
                     text="Material Name from Mesh", icon='MATERIAL')
        col.operator(OBJECT_OT_add_prefix_to_selected.bl_idname,
                     text="Add Prefix", icon='ADD')

        box.separator()
        box.label(text="Materials:")
        col = box.column(align=True)
        col.operator(OBJECT_OT_auto_setup_pbr_textures.bl_idname,
                     text="Auto Setup PBR Textures", icon='TEXTURE')
        col.operator(OBJECT_OT_create_material_by_mesh.bl_idname,
                     text="Create Material by Mesh", icon='ADD')
        col.operator(OBJECT_OT_convert_to_simple_pbr.bl_idname,
                     text="Convert to Simple PBR", icon='SHADING_SOLID')
        col.operator(OBJECT_OT_detect_unsupported_nodes.bl_idname,
                     text="Detect Unsupported Nodes", icon='ERROR')
        col.operator(OBJECT_OT_bake_procedural_to_texture.bl_idname,
                     text="Bake Procedural to Texture", icon='RENDER_STILL')
        col.operator(OBJECT_OT_texture_path_validator.bl_idname,
                     text="Validate Texture Paths", icon='CHECKMARK')
        col.operator(OBJECT_OT_rename_textures_for_unreal.bl_idname,
                     text="Rename Textures for Unreal", icon='FILE_TEXT')
        col.operator(OBJECT_OT_extract_textures_from_nodes.bl_idname,
                     text="Extract Textures", icon='IMPORT')
        col.operator(OBJECT_OT_delete_unused_materials.bl_idname,
                     text="Delete Unused Materials", icon='TRASH')
        col.operator(OBJECT_OT_purge_unused_materials.bl_idname,
                     text="Purge Unused Materials", icon='CANCEL')

        box.separator()
        box.label(text="Scene Cleanup:")
        col = box.column(align=True)
        col.operator(OBJECT_OT_delete_hidden_objects.bl_idname,
                     text="Delete Hidden Objects", icon='RESTRICT_VIEW_ON')
        col.operator(OBJECT_OT_delete_unused_empties.bl_idname,
                     text="Delete Unused Empties", icon='OUTLINER_OB_EMPTY')
        col.operator(OBJECT_OT_remove_empty_parents.bl_idname,
                     text="Remove Empty Parents", icon='OUTLINER_DATA_EMPTY')
        col.operator(OBJECT_OT_remove_empty_collections.bl_idname,
                     text="Remove Empty Collections", icon='OUTLINER_COLLECTION')
        col.operator(OBJECT_OT_purge_zero_face_meshes.bl_idname,
                     text="Purge Zero-Face Meshes", icon='MESH_DATA')
        col.operator(OBJECT_OT_clean_vertex_groups.bl_idname,
                     text="Clean Vertex Groups", icon='GROUP_VERTEX')
        col.operator(OBJECT_OT_remove_unused_mesh_data.bl_idname,
                     text="Remove Unused Mesh Data", icon='TRASH')

        box.separator()
        col = box.column(align=True)
        col.scale_y = 1.2
        col.operator(OBJECT_OT_validate_for_unreal.bl_idname,
                     text="Validate for Unreal", icon='CHECKMARK')


# Registration
classes = (
    UNREAL_OT_clear_panel_search,
    VIEW3D_PT_unreal_toolkit,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
