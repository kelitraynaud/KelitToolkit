"""Origin manipulation operators for KelitToolkit"""

import bpy
import mathutils

from ..utils import offset_mesh_geometry


class OBJECT_OT_set_origin_preset(bpy.types.Operator):
    """Set origin to preset position for selected objects"""
    bl_idname = "object.set_origin_preset"
    bl_label = "Set Origin Preset"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'OBJECT' and context.active_object is not None
                and bool(context.selected_objects))

    preset: bpy.props.EnumProperty(
        name="Preset",
        description="Origin position preset",
        items=[
            ('BOTTOM_CENTER', "Bottom Center", "Origin at bottom center (ideal for Unreal)"),
            ('CENTER', "Center", "Origin at object center"),
            ('TOP_CENTER', "Top Center", "Origin at top center (lights, ceilings)"),
            ('BOTTOM_CORNER', "Bottom Corner", "Origin at bottom corner (architecture)"),
        ],
        default='BOTTOM_CENTER'
    )

    def execute(self, context):
        selected_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']

        if not selected_objects:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}

        bpy.ops.object.mode_set(mode='OBJECT')

        processed_meshes = set()
        mesh_count = 0

        for obj in selected_objects:
            if obj.data in processed_meshes:
                continue

            processed_meshes.add(obj.data)
            mesh = obj.data

            # Get bounding box in LOCAL space (not world space)
            local_coords = [mathutils.Vector(corner) for corner in obj.bound_box]

            # Calculate min and max in local coordinates
            min_x = min(v.x for v in local_coords)
            max_x = max(v.x for v in local_coords)
            min_y = min(v.y for v in local_coords)
            max_y = max(v.y for v in local_coords)
            min_z = min(v.z for v in local_coords)
            max_z = max(v.z for v in local_coords)

            # Determine new origin offset in local space based on preset
            if self.preset == 'BOTTOM_CENTER':
                offset = mathutils.Vector((
                    -(min_x + max_x) / 2,
                    -(min_y + max_y) / 2,
                    -min_z
                ))
            elif self.preset == 'CENTER':
                offset = mathutils.Vector((
                    -(min_x + max_x) / 2,
                    -(min_y + max_y) / 2,
                    -(min_z + max_z) / 2
                ))
            elif self.preset == 'TOP_CENTER':
                offset = mathutils.Vector((
                    -(min_x + max_x) / 2,
                    -(min_y + max_y) / 2,
                    -max_z
                ))
            elif self.preset == 'BOTTOM_CORNER':
                offset = mathutils.Vector((-min_x, -min_y, -min_z))

            # Store world positions of all instances BEFORE modifying mesh
            instance_data = []
            for inst in [o for o in bpy.data.objects if o.data == mesh]:
                # Store world position and matrix
                world_pos = inst.matrix_world.translation.copy()
                instance_data.append((inst, world_pos))

            # Move the geometry (shape keys included) by the offset
            offset_mesh_geometry(mesh, offset)

            # Restore world positions for all instances
            for inst, original_world_pos in instance_data:
                # Calculate what the new world position would be after mesh change
                # The mesh moved by 'offset', which affects world position
                # We need to move the object to compensate

                # Get the offset in world space - FULL 3x3 (scale included):
                # normalizing dropped the scale and shifted scaled objects
                world_offset = inst.matrix_world.to_3x3() @ offset

                # Set location to restore original world position
                # new_world_pos = original_world_pos - world_offset
                target_pos = original_world_pos - world_offset

                # If object has no parent, we can set location directly in world space
                if inst.parent is None:
                    inst.location = target_pos
                else:
                    # If has parent, need to convert to local space
                    inst.location = inst.parent.matrix_world.inverted() @ target_pos

            mesh_count += 1

        # Get preset name for reporting
        preset_names = {
            'BOTTOM_CENTER': 'Bottom Center',
            'CENTER': 'Center',
            'TOP_CENTER': 'Top Center',
            'BOTTOM_CORNER': 'Bottom Corner'
        }
        preset_name = preset_names.get(self.preset, self.preset)

        self.report({'INFO'}, f"Origin set to {preset_name} for {mesh_count} mesh(es) and their instances")
        return {'FINISHED'}


class OBJECT_OT_set_origin_custom(bpy.types.Operator):
    """Set origin to custom position using percentage sliders"""
    bl_idname = "object.set_origin_custom"
    bl_label = "Set Origin Custom"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'OBJECT' and context.active_object is not None
                and bool(context.selected_objects))

    x_position: bpy.props.FloatProperty(
        name="X Position",
        description="X axis position (0% = left, 50% = center, 100% = right)",
        default=50.0,
        min=0.0,
        max=100.0,
        subtype='PERCENTAGE'
    )

    y_position: bpy.props.FloatProperty(
        name="Y Position",
        description="Y axis position (0% = front, 50% = center, 100% = back)",
        default=50.0,
        min=0.0,
        max=100.0,
        subtype='PERCENTAGE'
    )

    z_position: bpy.props.FloatProperty(
        name="Z Position",
        description="Z axis position (0% = bottom, 50% = center, 100% = top)",
        default=0.0,
        min=0.0,
        max=100.0,
        subtype='PERCENTAGE'
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=350)

    def draw(self, _context):
        layout = self.layout

        box = layout.box()
        box.label(text="Custom Origin Position:", icon='ORIENTATION_GIMBAL')

        col = box.column(align=True)
        col.prop(self, "x_position", slider=True)
        col.prop(self, "y_position", slider=True)
        col.prop(self, "z_position", slider=True)

        layout.separator()

        # Preview box
        preview = layout.box()
        preview.label(text="Preview:", icon='INFO')
        preview.label(text=f"  X: {self.x_position:.0f}% ({'Left' if self.x_position < 33 else 'Center' if self.x_position < 67 else 'Right'})")
        preview.label(text=f"  Y: {self.y_position:.0f}% ({'Front' if self.y_position < 33 else 'Center' if self.y_position < 67 else 'Back'})")
        preview.label(text=f"  Z: {self.z_position:.0f}% ({'Bottom' if self.z_position < 33 else 'Center' if self.z_position < 67 else 'Top'})")

    def execute(self, context):
        selected_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']

        if not selected_objects:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}

        bpy.ops.object.mode_set(mode='OBJECT')

        processed_meshes = set()
        mesh_count = 0

        for obj in selected_objects:
            if obj.data in processed_meshes:
                continue

            processed_meshes.add(obj.data)
            mesh = obj.data

            # Get bounding box in LOCAL space (not world space)
            local_coords = [mathutils.Vector(corner) for corner in obj.bound_box]

            # Calculate min and max in local coordinates
            min_x = min(v.x for v in local_coords)
            max_x = max(v.x for v in local_coords)
            min_y = min(v.y for v in local_coords)
            max_y = max(v.y for v in local_coords)
            min_z = min(v.z for v in local_coords)
            max_z = max(v.z for v in local_coords)

            # Calculate offset based on percentages
            x_percent = self.x_position / 100.0
            y_percent = self.y_position / 100.0
            z_percent = self.z_position / 100.0

            # Calculate offset in local space
            offset = mathutils.Vector((
                -(min_x + (max_x - min_x) * x_percent),
                -(min_y + (max_y - min_y) * y_percent),
                -(min_z + (max_z - min_z) * z_percent)
            ))

            # Store world positions of all instances BEFORE modifying mesh
            instance_data = []
            for inst in [o for o in bpy.data.objects if o.data == mesh]:
                # Store world position and matrix
                world_pos = inst.matrix_world.translation.copy()
                instance_data.append((inst, world_pos))

            # Move the geometry (shape keys included) by the offset
            offset_mesh_geometry(mesh, offset)

            # Restore world positions for all instances
            for inst, original_world_pos in instance_data:
                # Calculate what the new world position would be after mesh change
                # The mesh moved by 'offset', which affects world position
                # We need to move the object to compensate

                # Get the offset in world space - FULL 3x3 (scale included):
                # normalizing dropped the scale and shifted scaled objects
                world_offset = inst.matrix_world.to_3x3() @ offset

                # Set location to restore original world position
                # new_world_pos = original_world_pos - world_offset
                target_pos = original_world_pos - world_offset

                # If object has no parent, we can set location directly in world space
                if inst.parent is None:
                    inst.location = target_pos
                else:
                    # If has parent, need to convert to local space
                    inst.location = inst.parent.matrix_world.inverted() @ target_pos

            mesh_count += 1

        self.report({'INFO'}, f"Custom origin set for {mesh_count} mesh(es) and their instances")
        return {'FINISHED'}


class OBJECT_OT_set_origin_with_modifier_compensation(bpy.types.Operator):
    """Set origin position while compensating Array/Mirror modifier offsets to keep visual result identical"""
    bl_idname = "object.set_origin_with_modifier_compensation"
    bl_label = "Set Origin (Compensate Modifiers)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'OBJECT' and context.active_object is not None
                and bool(context.selected_objects))

    preset: bpy.props.EnumProperty(
        name="Preset",
        description="Origin position preset",
        items=[
            ('BOTTOM_CENTER', "Bottom Center", "Origin at bottom center (ideal for Unreal)"),
            ('CENTER', "Center", "Origin at object center"),
            ('TOP_CENTER', "Top Center", "Origin at top center (lights, ceilings)"),
            ('BOTTOM_CORNER', "Bottom Corner", "Origin at bottom corner (architecture)"),
        ],
        default='BOTTOM_CENTER'
    )

    def execute(self, context):
        selected_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']

        if not selected_objects:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}

        bpy.ops.object.mode_set(mode='OBJECT')

        processed_count = 0
        processed_meshes = set()

        for obj in selected_objects:
            mesh = obj.data
            # shared mesh data: offsetting it once already moved every
            # instance - a second pass would offset the vertices twice
            if mesh in processed_meshes:
                continue
            processed_meshes.add(mesh)

            # Get bounding box in LOCAL space
            local_coords = [mathutils.Vector(corner) for corner in obj.bound_box]

            # Calculate min and max in local coordinates
            min_x = min(v.x for v in local_coords)
            max_x = max(v.x for v in local_coords)
            min_y = min(v.y for v in local_coords)
            max_y = max(v.y for v in local_coords)
            min_z = min(v.z for v in local_coords)
            max_z = max(v.z for v in local_coords)

            # Determine new origin offset in local space based on preset
            if self.preset == 'BOTTOM_CENTER':
                offset = mathutils.Vector((
                    -(min_x + max_x) / 2,
                    -(min_y + max_y) / 2,
                    -min_z
                ))
            elif self.preset == 'CENTER':
                offset = mathutils.Vector((
                    -(min_x + max_x) / 2,
                    -(min_y + max_y) / 2,
                    -(min_z + max_z) / 2
                ))
            elif self.preset == 'TOP_CENTER':
                offset = mathutils.Vector((
                    -(min_x + max_x) / 2,
                    -(min_y + max_y) / 2,
                    -max_z
                ))
            elif self.preset == 'BOTTOM_CORNER':
                offset = mathutils.Vector((-min_x, -min_y, -min_z))

            # STEP 1: Compensate modifiers BEFORE changing origin
            # The offset will move the mesh, we need to compensate Array/Mirror offsets
            for mod in obj.modifiers:
                if mod.type == 'ARRAY':
                    # Compensate constant offset
                    # When mesh moves by 'offset', the array copies will be at wrong positions
                    # We need to add the inverse offset to constant_offset_displace
                    if mod.use_constant_offset:
                        # The mesh will move by 'offset', so we compensate by subtracting it
                        mod.constant_offset_displace -= offset

                    # Note: relative_offset is in percentage and shouldn't need adjustment
                    # object_offset is handled by the offset object's position

                elif mod.type == 'MIRROR':
                    # Mirror modifier doesn't have offsets, but has mirror_object
                    # If mirror_object exists, we might need to adjust it
                    # For now, mirrors should work correctly as they're based on object center
                    pass

            # STEP 2: Store world position BEFORE changing mesh
            original_world_pos = obj.matrix_world.translation.copy()

            # STEP 3: Move the geometry (shape keys included) by the offset
            offset_mesh_geometry(mesh, offset)

            # STEP 4: Restore world position by adjusting object location.
            # FULL 3x3 (scale included): normalizing shifted scaled objects
            world_offset = obj.matrix_world.to_3x3() @ offset

            # Calculate target position to restore original world position
            target_pos = original_world_pos - world_offset

            # Set location to restore original world position
            if obj.parent is None:
                obj.location = target_pos
            else:
                # If has parent, convert to local space
                obj.location = obj.parent.matrix_world.inverted() @ target_pos

            processed_count += 1

        # Get preset name for reporting
        preset_names = {
            'BOTTOM_CENTER': 'Bottom Center',
            'CENTER': 'Center',
            'TOP_CENTER': 'Top Center',
            'BOTTOM_CORNER': 'Bottom Corner'
        }
        preset_name = preset_names.get(self.preset, self.preset)

        self.report({'INFO'}, f"Origin set to {preset_name} with modifier compensation for {processed_count} object(s)")
        return {'FINISHED'}


# Registration
classes = (
    OBJECT_OT_set_origin_preset,
    OBJECT_OT_set_origin_custom,
    OBJECT_OT_set_origin_with_modifier_compensation,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
