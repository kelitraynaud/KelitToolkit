import bpy
import re
from collections import defaultdict
from ..utils import normalize_name_for_unreal, clean_name, is_editable


def to_snake_case_keeping_prefix(name):
    """PascalCase -> Snake_Case with the UE prefix kept intact:
    SM_MyChair -> SM_My_Chair (the old regex produced S_M__My_Chair).
    Runs of capitals stay whole, so SM_Chair_LOD2 keeps its LOD tag (the
    previous rule turned it into SM_Chair_L_O_D2, unreadable by Unreal) and
    HDRICapture becomes HDRI_Capture."""
    prefix = ''
    for candidate in ('UCX_', 'UBX_', 'USP_', 'UCP_', 'SM_', 'SK_', 'MI_',
                      'CAM_', 'COL_', 'AM_', 'BP_', 'M_', 'T_', 'A_', 'P_',
                      'L_', 'S_'):
        if name.startswith(candidate):
            prefix = candidate
            name = name[len(candidate):]
            break
    converted = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', name)
    converted = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', converted)
    return prefix + converted


# ============================================================================
# OPERATORS - NAMING
# ============================================================================

class OBJECT_OT_normalize_names_quick(bpy.types.Operator):
    """Quick normalize: PascalCase + Unreal prefixes (one click)"""
    bl_idname = "object.normalize_names_quick"
    bl_label = "Normalize Names (Quick)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    def execute(self, context):
        selected_objs = context.selected_objects

        if not selected_objs:
            self.report({'WARNING'}, "No objects selected")
            return {'CANCELLED'}

        renamed_objects = 0
        renamed_meshes = 0
        renamed_materials = 0
        skipped_linked = 0

        for obj in selected_objs:
            # library-linked datablocks cannot be renamed
            if obj.library is not None or obj.override_library is not None:
                skipped_linked += 1
                continue

            # Determine object type
            obj_type = 'MESH'
            if obj.type == 'LIGHT':
                obj_type = 'LIGHT'
            elif obj.type == 'CAMERA':
                obj_type = 'CAMERA'
            elif obj.type == 'ARMATURE':
                obj_type = 'ARMATURE'

            # Normalize object name
            old_name = obj.name
            new_name = normalize_name_for_unreal(old_name, obj_type)

            if new_name != old_name:
                obj.name = new_name
                renamed_objects += 1

            # Normalize mesh data name if it's a mesh
            if obj.type == 'MESH' and obj.data:
                old_mesh_name = obj.data.name
                new_mesh_name = normalize_name_for_unreal(old_mesh_name, 'MESH')

                if new_mesh_name != old_mesh_name and is_editable(obj.data):
                    obj.data.name = new_mesh_name
                    renamed_meshes += 1

                # Normalize material names
                for mat_slot in obj.material_slots:
                    if mat_slot.material:
                        old_mat_name = mat_slot.material.name
                        new_mat_name = normalize_name_for_unreal(old_mat_name, 'MATERIAL')

                        if new_mat_name != old_mat_name and is_editable(mat_slot.material):
                            mat_slot.material.name = new_mat_name
                            renamed_materials += 1

        self.report({'INFO'}, f"Normalized: {renamed_objects} object(s), {renamed_meshes} mesh(es), {renamed_materials} material(s)")
        return {'FINISHED'}


class OBJECT_OT_normalize_names_advanced(bpy.types.Operator):
    """Advanced normalize with custom options"""
    bl_idname = "object.normalize_names_advanced"
    bl_label = "Normalize Names (Advanced)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    apply_to_objects: bpy.props.BoolProperty(
        name="Apply to Objects",
        description="Normalize object names",
        default=True
    )

    apply_to_mesh_data: bpy.props.BoolProperty(
        name="Apply to Mesh Data",
        description="Normalize mesh data names",
        default=True
    )

    apply_to_materials: bpy.props.BoolProperty(
        name="Apply to Materials",
        description="Normalize material names",
        default=True
    )

    preserve_collision: bpy.props.BoolProperty(
        name="Preserve Collision Prefixes",
        description="Keep UCX_, UBX_, USP_ prefixes",
        default=True
    )

    preserve_lod: bpy.props.BoolProperty(
        name="Preserve LOD Suffixes",
        description="Keep _LOD0, _LOD1, etc.",
        default=True
    )

    add_prefix: bpy.props.BoolProperty(
        name="Auto-Add Unreal Prefix",
        description="Automatically add SM_, M_, etc.",
        default=True
    )

    case_style: bpy.props.EnumProperty(
        name="Case Style",
        description="Naming convention style",
        items=[
            ('PASCAL', "PascalCase", "PascalCase (MyAwesomeChair)"),
            ('SNAKE', "Snake_Case", "Snake_Case with underscores (My_Awesome_Chair)"),
        ],
        default='PASCAL'
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=400)

    def draw(self, _context):
        layout = self.layout

        box = layout.box()
        box.label(text="Apply To:", icon='FILTER')
        box.prop(self, "apply_to_objects")
        box.prop(self, "apply_to_mesh_data")
        box.prop(self, "apply_to_materials")

        box = layout.box()
        box.label(text="Options:", icon='PREFERENCES')
        box.prop(self, "case_style")
        box.prop(self, "add_prefix")
        box.prop(self, "preserve_collision")
        box.prop(self, "preserve_lod")

    def execute(self, context):
        selected_objs = context.selected_objects

        if not selected_objs:
            self.report({'WARNING'}, "No objects selected")
            return {'CANCELLED'}

        renamed_objects = 0
        renamed_meshes = 0
        renamed_materials = 0

        for obj in selected_objs:
            # Determine object type
            obj_type = 'MESH'
            if obj.type == 'LIGHT':
                obj_type = 'LIGHT'
            elif obj.type == 'CAMERA':
                obj_type = 'CAMERA'
            elif obj.type == 'ARMATURE':
                obj_type = 'ARMATURE'

            # Normalize object name
            if self.apply_to_objects:
                old_name = obj.name
                new_name = normalize_name_for_unreal(
                    old_name,
                    obj_type,
                    preserve_collision=self.preserve_collision,
                    preserve_lod=self.preserve_lod
                )

                # Apply case style
                if self.case_style == 'SNAKE':
                    new_name = to_snake_case_keeping_prefix(new_name)

                if new_name != old_name and is_editable(obj):
                    obj.name = new_name
                    renamed_objects += 1

            # Normalize mesh data name
            if self.apply_to_mesh_data and obj.type == 'MESH' and obj.data:
                old_mesh_name = obj.data.name
                new_mesh_name = normalize_name_for_unreal(
                    old_mesh_name,
                    'MESH',
                    preserve_collision=self.preserve_collision,
                    preserve_lod=self.preserve_lod
                )

                if self.case_style == 'SNAKE':
                    new_mesh_name = to_snake_case_keeping_prefix(new_mesh_name)

                if new_mesh_name != old_mesh_name and is_editable(obj.data):
                    obj.data.name = new_mesh_name
                    renamed_meshes += 1

            # Normalize material names
            if self.apply_to_materials and obj.type == 'MESH':
                for mat_slot in obj.material_slots:
                    if mat_slot.material:
                        old_mat_name = mat_slot.material.name
                        new_mat_name = normalize_name_for_unreal(old_mat_name, 'MATERIAL')

                        if self.case_style == 'SNAKE':
                            new_mat_name = to_snake_case_keeping_prefix(new_mat_name)

                        if new_mat_name != old_mat_name and is_editable(mat_slot.material):
                            mat_slot.material.name = new_mat_name
                            renamed_materials += 1

        self.report({'INFO'}, f"Normalized: {renamed_objects} object(s), {renamed_meshes} mesh(es), {renamed_materials} material(s)")
        return {'FINISHED'}


class OBJECT_OT_batch_find_replace(bpy.types.Operator):
    """Find and replace text in object names"""
    bl_idname = "object.batch_find_replace"
    bl_label = "Batch Find & Replace"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    find_text: bpy.props.StringProperty(
        name="Find",
        description="Text to find",
        default=""
    )

    replace_text: bpy.props.StringProperty(
        name="Replace",
        description="Text to replace with",
        default=""
    )

    use_regex: bpy.props.BoolProperty(
        name="Use Regex",
        description="Use regular expressions",
        default=False
    )

    case_sensitive: bpy.props.BoolProperty(
        name="Case Sensitive",
        description="Match case when searching",
        default=False
    )

    apply_to_objects: bpy.props.BoolProperty(
        name="Objects",
        description="Apply to object names",
        default=True
    )

    apply_to_mesh_data: bpy.props.BoolProperty(
        name="Mesh Data",
        description="Apply to mesh data names",
        default=True
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=400)

    def draw(self, _context):
        layout = self.layout

        layout.prop(self, "find_text")
        layout.prop(self, "replace_text")

        row = layout.row()
        row.prop(self, "use_regex")
        row.prop(self, "case_sensitive")

        layout.separator()
        layout.label(text="Apply To:")
        row = layout.row()
        row.prop(self, "apply_to_objects")
        row.prop(self, "apply_to_mesh_data")

    def execute(self, context):
        if not self.find_text:
            self.report({'WARNING'}, "Find text cannot be empty")
            return {'CANCELLED'}

        selected_objs = context.selected_objects
        if not selected_objs:
            self.report({'WARNING'}, "No objects selected")
            return {'CANCELLED'}

        if self.use_regex:
            try:
                re.compile(self.find_text)
            except re.error as error:
                self.report({'WARNING'}, f"Invalid regex: {error}")
                return {'CANCELLED'}

        renamed_count = 0

        for obj in selected_objs:
            # Replace in object name
            if self.apply_to_objects:
                old_name = obj.name

                if self.use_regex:
                    flags = 0 if self.case_sensitive else re.IGNORECASE
                    new_name = re.sub(self.find_text, self.replace_text, old_name, flags=flags)
                else:
                    if self.case_sensitive:
                        new_name = old_name.replace(self.find_text, self.replace_text)
                    else:
                        # Case insensitive replacement
                        pattern = re.compile(re.escape(self.find_text), re.IGNORECASE)
                        new_name = pattern.sub(self.replace_text, old_name)

                if new_name != old_name and is_editable(obj):
                    obj.name = new_name
                    renamed_count += 1

            # Replace in mesh data name
            if self.apply_to_mesh_data and obj.type == 'MESH' and obj.data:
                old_mesh_name = obj.data.name

                if self.use_regex:
                    flags = 0 if self.case_sensitive else re.IGNORECASE
                    new_mesh_name = re.sub(self.find_text, self.replace_text, old_mesh_name, flags=flags)
                else:
                    if self.case_sensitive:
                        new_mesh_name = old_mesh_name.replace(self.find_text, self.replace_text)
                    else:
                        pattern = re.compile(re.escape(self.find_text), re.IGNORECASE)
                        new_mesh_name = pattern.sub(self.replace_text, old_mesh_name)

                if new_mesh_name != old_mesh_name and is_editable(obj.data):
                    obj.data.name = new_mesh_name
                    renamed_count += 1

        self.report({'INFO'}, f"Replaced text in {renamed_count} name(s)")
        return {'FINISHED'}


class OBJECT_OT_set_mesh_name_from_object(bpy.types.Operator):
    """Rename objects and mesh data (adds SM_ if missing)"""
    bl_idname = "object.set_mesh_name_from_object"
    bl_label = "Set Mesh Name From Object"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    def execute(self, context):
        obj_count = 0
        mesh_count = 0

        for obj in context.selected_objects:
            if obj.type == 'MESH' and obj.data:
                # Clean and prepare new name
                new_name = clean_name(obj.name)

                # Add SM_ prefix if not already present
                if not new_name.startswith("SM_"):
                    new_name = "SM_" + new_name

                # Rename object
                if obj.name != new_name:
                    obj.name = new_name
                    obj_count += 1

                # Rename mesh data
                if obj.data.name != new_name:
                    obj.data.name = new_name
                    mesh_count += 1

        if obj_count > 0 and mesh_count > 0:
            self.report({'INFO'}, f"{obj_count} object(s) and {mesh_count} mesh(es) renamed")
        elif obj_count > 0:
            self.report({'INFO'}, f"{obj_count} object(s) renamed")
        elif mesh_count > 0:
            self.report({'INFO'}, f"{mesh_count} mesh(es) renamed")
        else:
            self.report({'INFO'}, "No changes needed")

        return {'FINISHED'}


class OBJECT_OT_set_object_name_from_mesh(bpy.types.Operator):
    """Rename all selected objects with their mesh data name (creates uniform naming)"""
    bl_idname = "object.set_object_name_from_mesh"
    bl_label = "Set Object Name From Mesh"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    def execute(self, context):
        renamed_count = 0
        mesh_data_dict = defaultdict(list)

        # Group objects by mesh data
        for obj in context.selected_objects:
            if obj.type == 'MESH' and obj.data:
                mesh_data_dict[obj.data].append(obj)

        # Rename objects based on their mesh data
        for mesh_data, objects in mesh_data_dict.items():
            base_mesh_name = clean_name(mesh_data.name)

            if len(objects) == 1:
                # Single object using this mesh
                obj = objects[0]
                if obj.name != base_mesh_name:
                    obj.name = base_mesh_name
                    renamed_count += 1
            else:
                # Multiple objects using the same mesh - add numerical suffix
                for idx, obj in enumerate(objects, start=1):
                    new_name = f"{base_mesh_name}.{idx:03d}"
                    if obj.name != new_name:
                        obj.name = new_name
                        renamed_count += 1

        if renamed_count > 0:
            self.report({'INFO'}, f"{renamed_count} object(s) renamed from mesh data")
        else:
            self.report({'INFO'}, "No changes needed")

        return {'FINISHED'}


class OBJECT_OT_add_prefix_to_selected(bpy.types.Operator):
    """Add a prefix to selected objects"""
    bl_idname = "object.add_prefix_to_selected"
    bl_label = "Add Prefix to Selected"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    prefix: bpy.props.StringProperty(
        name="Prefix",
        description="Prefix to add (e.g.: SM_, S_)",
        default="SM_"
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        count = 0
        for obj in context.selected_objects:
            if not obj.name.startswith(self.prefix):
                obj.name = self.prefix + obj.name
                count += 1

        self.report({'INFO'}, f"Prefix '{self.prefix}' added to {count} object(s)")
        return {'FINISHED'}


class OBJECT_OT_material_name_from_mesh(bpy.types.Operator):
    """Rename materials based on mesh name (SM_ → M_), keeps shared materials by default"""
    bl_idname = "object.material_name_from_mesh"
    bl_label = "Material Name from Mesh"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    make_unique: bpy.props.BoolProperty(
        name="Make Unique if Shared",
        description="Create a unique copy of the material if it's shared with other objects",
        default=False
    )

    skip_shared: bpy.props.BoolProperty(
        name="Skip Shared Materials",
        description="Don't rename materials that are used by multiple objects",
        default=False
    )

    def invoke(self, context, event):
        # Check if any materials are shared
        selected_objs = [obj for obj in context.selected_objects if obj.type == 'MESH']
        self.has_shared_materials = False
        self.shared_material_names = []

        for obj in selected_objs:
            if obj.data:
                for mat_slot in obj.material_slots:
                    if mat_slot.material and mat_slot.material.users > 1:
                        self.has_shared_materials = True
                        if mat_slot.material.name not in self.shared_material_names:
                            self.shared_material_names.append(mat_slot.material.name)

        return context.window_manager.invoke_props_dialog(self, width=400)

    def draw(self, _context):
        layout = self.layout

        # the redo panel re-instantiates the operator without invoke()
        has_shared = getattr(self, 'has_shared_materials', False)
        shared_names = getattr(self, 'shared_material_names', [])

        if has_shared:
            box = layout.box()
            box.label(text="⚠ Shared Materials Detected:", icon='ERROR')
            for mat_name in shared_names[:3]:  # Show max 3
                box.label(text=f"  • {mat_name}")
            if len(shared_names) > 3:
                box.label(text=f"  ... and {len(shared_names) - 3} more")

            layout.separator()

        layout.prop(self, "skip_shared")

        # Only show make_unique if skip_shared is disabled
        if not self.skip_shared:
            layout.prop(self, "make_unique")

        # Show appropriate warning
        if has_shared:
            box = layout.box()
            if self.skip_shared:
                box.label(text="ℹ Shared materials will keep", icon='INFO')
                box.label(text="their original names")
            elif not self.make_unique:
                box.label(text="⚠ Warning:", icon='ERROR')
                box.label(text="Shared materials will be renamed")
                box.label(text="for ALL objects using them!")
            else:
                box.label(text="✓ Unique copies will be created", icon='CHECKMARK')
                box.label(text="for each selected object")

    def execute(self, context):
        selected_objs = [obj for obj in context.selected_objects if obj.type == 'MESH']

        if not selected_objs:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}

        renamed_count = 0
        made_unique_count = 0

        for obj in selected_objs:
            if not obj.data:
                continue

            # Get base name from mesh (SM_Chair → Chair)
            mesh_name = obj.data.name
            base_name = mesh_name

            # Remove SM_ prefix if present
            if base_name.startswith('SM_'):
                base_name = base_name[3:]

            # Process each material slot
            for slot_idx, mat_slot in enumerate(obj.material_slots):
                if not mat_slot.material:
                    continue

                mat = mat_slot.material

                # Check if material is shared with other objects
                is_shared = mat.users > 1

                # If skip_shared is enabled and material is shared, skip it
                if self.skip_shared and is_shared:
                    continue

                # Determine new material name
                if len(obj.material_slots) > 1:
                    # Multiple materials: add suffix _01, _02, etc.
                    new_mat_name = f"M_{base_name}_{slot_idx + 1:02d}"
                else:
                    # Single material: just M_BaseName
                    new_mat_name = f"M_{base_name}"

                # If material is shared and make_unique is enabled
                if is_shared and self.make_unique:
                    # Create a copy of the material
                    new_mat = mat.copy()
                    new_mat.name = new_mat_name
                    mat_slot.material = new_mat
                    made_unique_count += 1
                    renamed_count += 1
                else:
                    # Just rename the existing material
                    if mat.name != new_mat_name:
                        mat.name = new_mat_name
                        renamed_count += 1

        if made_unique_count > 0:
            self.report({'INFO'}, f"Renamed {renamed_count} material(s), made {made_unique_count} unique")
        else:
            self.report({'INFO'}, f"Renamed {renamed_count} material(s)")

        return {'FINISHED'}


class OBJECT_OT_clean_object_names(bpy.types.Operator):
    """Clean object names by removing numerical suffixes (.001, .002, etc.)"""
    bl_idname = "object.clean_object_names"
    bl_label = "Clean Object Names"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    clean_mesh_data: bpy.props.BoolProperty(
        name="Clean Mesh Data Names",
        description="Also clean mesh data names",
        default=True
    )

    def execute(self, context):
        selected_objs = context.selected_objects

        if not selected_objs:
            self.report({'WARNING'}, "No objects selected")
            return {'CANCELLED'}

        renamed_objects = 0
        renamed_meshes = 0

        # Dictionary to avoid duplicates
        used_names = {}

        for obj in selected_objs:
            # Clean object name
            original_name = obj.name
            clean = clean_name(original_name)

            # If name has changed
            if clean != original_name:
                # Handle duplicates by adding a counter if necessary
                if clean in used_names:
                    used_names[clean] += 1
                    new_name = f"{clean}_{used_names[clean]}"
                else:
                    used_names[clean] = 0
                    new_name = clean

                obj.name = new_name
                renamed_objects += 1

            # Clean mesh data if option is enabled
            if self.clean_mesh_data and obj.type == 'MESH' and obj.data:
                mesh_original_name = obj.data.name
                mesh_clean = clean_name(mesh_original_name)

                if mesh_clean != mesh_original_name:
                    obj.data.name = mesh_clean
                    renamed_meshes += 1

        if renamed_meshes > 0:
            self.report({'INFO'}, f"{renamed_objects} object(s) and {renamed_meshes} mesh(es) renamed")
        else:
            self.report({'INFO'}, f"{renamed_objects} object(s) renamed")

        return {'FINISHED'}


classes = (
    OBJECT_OT_normalize_names_quick,
    OBJECT_OT_normalize_names_advanced,
    OBJECT_OT_batch_find_replace,
    OBJECT_OT_set_mesh_name_from_object,
    OBJECT_OT_set_object_name_from_mesh,
    OBJECT_OT_add_prefix_to_selected,
    OBJECT_OT_material_name_from_mesh,
    OBJECT_OT_clean_object_names,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
