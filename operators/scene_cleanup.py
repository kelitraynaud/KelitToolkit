"""Scene cleanup operators for KelitToolkit"""

import bpy


# ============================================================================
# OPERATORS - SCENE CLEANUP
# ============================================================================

class OBJECT_OT_delete_hidden_objects(bpy.types.Operator):
    """Delete hidden or viewport-disabled objects from the CURRENT scene"""
    bl_idname = "object.delete_hidden_objects"
    bl_label = "Delete Hidden Objects"
    bl_options = {'REGISTER', 'UNDO'}

    include_render_disabled: bpy.props.BoolProperty(
        name="Also Render-Disabled",
        description="Also delete objects that are only disabled for render "
                    "(camera toggle) while still visible in the viewport - "
                    "careful, those are often bake sources or helpers",
        default=False
    )

    def execute(self, context):
        hidden_objects = []

        # current scene only: bpy.data.objects would also delete from OTHER
        # scenes in the file
        for obj in context.scene.objects:
            if obj.hide_viewport or obj.hide_get():
                hidden_objects.append(obj)
            elif self.include_render_disabled and obj.hide_render:
                hidden_objects.append(obj)

        if not hidden_objects:
            self.report({'INFO'}, "No hidden objects found")
            return {'CANCELLED'}

        # Remove hidden objects
        for obj in hidden_objects:
            bpy.data.objects.remove(obj, do_unlink=True)

        self.report({'INFO'}, f"Deleted {len(hidden_objects)} hidden object(s)")
        return {'FINISHED'}


class OBJECT_OT_remove_empty_collections(bpy.types.Operator):
    """Remove all empty collections from the scene"""
    bl_idname = "object.remove_empty_collections"
    bl_label = "Remove Empty Collections"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        empty_collections = []

        # Check all collections recursively
        def check_collection(collection):
            # A collection is empty if it has no objects and no children collections
            # or all its children are also empty
            has_objects = len(collection.objects) > 0
            has_non_empty_children = False

            for child in collection.children:
                if not check_collection(child):
                    has_non_empty_children = True

            is_empty = not has_objects and not has_non_empty_children

            # collections are reachable through several parents: avoid duplicates,
            # removing the same collection twice would crash
            if is_empty and collection != context.scene.collection and collection not in empty_collections:
                empty_collections.append(collection)

            return is_empty

        # Start from scene collection
        for collection in bpy.data.collections:
            check_collection(collection)

        if not empty_collections:
            self.report({'INFO'}, "No empty collections found")
            return {'CANCELLED'}

        # Remove empty collections
        for collection in empty_collections:
            bpy.data.collections.remove(collection)

        self.report({'INFO'}, f"Removed {len(empty_collections)} empty collection(s)")
        return {'FINISHED'}


class OBJECT_OT_purge_zero_face_meshes(bpy.types.Operator):
    """Remove mesh objects that have no faces/polygons"""
    bl_idname = "object.purge_zero_face_meshes"
    bl_label = "Purge Zero-Face Meshes"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        zero_face_objects = []

        for obj in bpy.data.objects:
            if obj.type == 'MESH' and obj.data:
                if len(obj.data.polygons) == 0:
                    zero_face_objects.append(obj)

        if not zero_face_objects:
            self.report({'INFO'}, "No zero-face meshes found")
            return {'CANCELLED'}

        # Remove objects with no faces
        for obj in zero_face_objects:
            bpy.data.objects.remove(obj, do_unlink=True)

        self.report({'INFO'}, f"Purged {len(zero_face_objects)} zero-face mesh(es)")
        return {'FINISHED'}


class OBJECT_OT_clean_vertex_groups(bpy.types.Operator):
    """Remove unused vertex groups from selected mesh objects"""
    bl_idname = "object.clean_vertex_groups"
    bl_label = "Clean Vertex Groups"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    def execute(self, context):
        selected_objs = [obj for obj in context.selected_objects if obj.type == 'MESH']

        if not selected_objs:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}

        total_removed = 0

        for obj in selected_objs:
            if not obj.vertex_groups:
                continue

            # Get list of vertex groups that have at least one vertex assigned
            used_groups = set()

            for vertex in obj.data.vertices:
                for group in vertex.groups:
                    if group.weight > 0:
                        used_groups.add(group.group)

            # Remove unused groups
            groups_to_remove = []
            for vg in obj.vertex_groups:
                if vg.index not in used_groups:
                    groups_to_remove.append(vg)

            for vg in groups_to_remove:
                obj.vertex_groups.remove(vg)
                total_removed += 1

        if total_removed == 0:
            self.report({'INFO'}, "No unused vertex groups found")
        else:
            self.report({'INFO'}, f"Removed {total_removed} unused vertex group(s) from {len(selected_objs)} object(s)")

        return {'FINISHED'}


class OBJECT_OT_delete_unused_empties(bpy.types.Operator):
    """Delete empties that serve no purpose, while protecting anything a
    camera rig or an animation still needs (parents of cameras, constraint
    and DOF targets, animated parents). Children are re-parented with their
    world transform preserved"""
    bl_idname = "object.delete_unused_empties"
    bl_label = "Delete Unused Empties"
    bl_options = {'REGISTER', 'UNDO'}

    preserve_camera_rig: bpy.props.BoolProperty(
        name="Preserve Camera Rigs",
        description="Keep every empty a camera depends on: its parent chain, its "
                    "constraint targets and its depth-of-field focus target. "
                    "Untick only after 'Bake Camera Animation'",
        default=True
    )

    process_all: bpy.props.BoolProperty(
        name="Process All Scene",
        description="Consider every empty in the scene, not just the selected ones",
        default=True
    )

    # ------------------------------------------------------------------
    def _ancestors(self, obj):
        parent = obj.parent
        while parent is not None:
            yield parent
            parent = parent.parent

    def _has_animation(self, obj):
        anim = obj.animation_data
        return anim is not None and (anim.action is not None or anim.nla_tracks)

    def _driver_targets(self, id_block, into):
        anim = getattr(id_block, 'animation_data', None)
        if anim is None:
            return
        for driver in anim.drivers:
            for variable in driver.driver.variables:
                for target in variable.targets:
                    if target.id is not None and getattr(target.id, 'type', None) == 'EMPTY':
                        into.add(target.id.name)

    def _protected_empties(self, context):
        """Names of empties that must survive, with the reason kept for the report."""
        protected = {}

        def protect(obj, reason):
            if obj is not None and obj.type == 'EMPTY' and obj.name not in protected:
                protected[obj.name] = reason
                # what a protected object depends on is protected too
                for ancestor in self._ancestors(obj):
                    protect(ancestor, f"parent of protected '{obj.name}'")
                for constraint in obj.constraints:
                    target = getattr(constraint, 'target', None)
                    protect(target, f"constraint target of '{obj.name}'")

        # 1. camera rigs
        if self.preserve_camera_rig:
            for obj in context.scene.objects:
                if obj.type != 'CAMERA':
                    continue
                for ancestor in self._ancestors(obj):
                    protect(ancestor, f"parent chain of camera '{obj.name}'")
                for constraint in obj.constraints:
                    protect(getattr(constraint, 'target', None),
                            f"constraint target of camera '{obj.name}'")
                dof = obj.data.dof
                if dof.focus_object is not None:
                    protect(dof.focus_object, f"DOF focus of camera '{obj.name}'")

        # 2. anything referenced by a NON-empty object's constraints or drivers
        driver_hits = set()
        for obj in context.scene.objects:
            if obj.type == 'EMPTY':
                continue
            for constraint in obj.constraints:
                protect(getattr(constraint, 'target', None),
                        f"constraint target of '{obj.name}'")
            self._driver_targets(obj, driver_hits)
            if obj.data is not None:
                self._driver_targets(obj.data, driver_hits)
        for name in driver_hits:
            protect(bpy.data.objects.get(name), "driver target")

        # 3. animated empties that drive real content below them
        for obj in context.scene.objects:
            if obj.type == 'EMPTY' and self._has_animation(obj):
                drives_content = any(child.type != 'EMPTY' or child.name in protected
                                     for child in obj.children_recursive)
                if drives_content:
                    protect(obj, "animated parent of real content")

        return protected

    # ------------------------------------------------------------------
    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "preserve_camera_rig")
        layout.prop(self, "process_all")

        protected = self._protected_empties(context)
        pool = (context.scene.objects if self.process_all else context.selected_objects)
        candidates = [o for o in pool if o.type == 'EMPTY' and o.name not in protected]
        box = layout.box()
        box.label(text=f"{len(candidates)} empty(ies) will be deleted", icon='TRASH')
        box.label(text=f"{len(protected)} kept (rig / animation)", icon='LOCKED')
        for name in list(protected)[:4]:
            box.label(text=f"   kept: {name} - {protected[name]}")

    def execute(self, context):
        protected = self._protected_empties(context)
        pool = (context.scene.objects if self.process_all else context.selected_objects)
        candidates = [o for o in pool if o.type == 'EMPTY' and o.name not in protected]
        if not candidates:
            self.report({'INFO'}, "No unused empty found")
            return {'CANCELLED'}

        # deepest first, so children are re-parented at most once per level
        def depth(obj):
            return sum(1 for _ in self._ancestors(obj))

        deleted = 0
        for empty in sorted(candidates, key=depth, reverse=True):
            grandparent = empty.parent
            for child in list(empty.children):
                world = child.matrix_world.copy()
                child.parent = grandparent
                child.matrix_world = world
            bpy.data.objects.remove(empty, do_unlink=True)
            deleted += 1

        message = f"Deleted {deleted} empty(ies)"
        if protected:
            message += f", kept {len(protected)} (camera rig / animation)"
        self.report({'INFO'}, message)
        return {'FINISHED'}


class OBJECT_OT_remove_empty_parents(bpy.types.Operator):
    """Remove Empty objects and keep only mesh children with proper transforms (perfect for Sketchfab imports)"""
    bl_idname = "object.remove_empty_parents"
    bl_label = "Remove Empty Parents"
    bl_options = {'REGISTER', 'UNDO'}

    apply_transforms: bpy.props.BoolProperty(
        name="Apply Transforms",
        description="Apply the parent's transform to the child mesh before removing",
        default=True
    )

    process_all: bpy.props.BoolProperty(
        name="Process All Scene",
        description="Process all empty objects in the scene, not just selected",
        default=False
    )

    def execute(self, context):
        # Determine which objects to process
        if self.process_all:
            empty_objects = [obj for obj in bpy.data.objects if obj.type == 'EMPTY']
        else:
            empty_objects = [obj for obj in context.selected_objects if obj.type == 'EMPTY']

        if not empty_objects:
            self.report({'WARNING'}, "No empty objects found")
            return {'CANCELLED'}

        removed_count = 0
        processed_meshes = []

        # Process each empty object
        for empty in empty_objects:
            # Get all children
            children = [child for child in empty.children]

            if not children:
                # Empty has no children, just delete it
                bpy.data.objects.remove(empty, do_unlink=True)
                removed_count += 1
                continue

            # Process each child
            for child in children:
                # Store the original parent (could be the empty's parent)
                grandparent = empty.parent

                if self.apply_transforms:
                    # Re-assign the world matrix AFTER the final parenting:
                    # setting it before would leave the grandparent's
                    # transform applied twice through matrix_parent_inverse
                    world_matrix = child.matrix_world.copy()
                    child.parent = grandparent
                    child.matrix_world = world_matrix
                else:
                    child.parent = grandparent
                    if grandparent:
                        child.matrix_parent_inverse = grandparent.matrix_world.inverted()

                # Track processed meshes
                if child.type == 'MESH':
                    processed_meshes.append(child)

            # Delete the empty object
            bpy.data.objects.remove(empty, do_unlink=True)
            removed_count += 1

        # Select the processed meshes
        bpy.ops.object.select_all(action='DESELECT')
        for mesh in processed_meshes:
            mesh.select_set(True)

        if processed_meshes:
            context.view_layer.objects.active = processed_meshes[0]

        self.report({'INFO'}, f"Removed {removed_count} empty object(s), kept {len(processed_meshes)} mesh(es)")
        return {'FINISHED'}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "apply_transforms")
        layout.prop(self, "process_all")


class OBJECT_OT_remove_unused_mesh_data(bpy.types.Operator):
    """Remove orphaned mesh data blocks with zero users"""
    bl_idname = "object.remove_unused_mesh_data"
    bl_label = "Remove Unused Mesh Data"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        # Get all mesh data with 0 users
        unused_meshes = [mesh for mesh in bpy.data.meshes if mesh.users == 0]

        if not unused_meshes:
            self.report({'INFO'}, "No unused mesh data found")
            return {'CANCELLED'}

        # Remove unused meshes
        for mesh in unused_meshes:
            bpy.data.meshes.remove(mesh)

        self.report({'INFO'}, f"Removed {len(unused_meshes)} unused mesh data block(s)")
        return {'FINISHED'}


classes = (
    OBJECT_OT_delete_hidden_objects,
    OBJECT_OT_remove_empty_collections,
    OBJECT_OT_purge_zero_face_meshes,
    OBJECT_OT_clean_vertex_groups,
    OBJECT_OT_delete_unused_empties,
    OBJECT_OT_remove_empty_parents,
    OBJECT_OT_remove_unused_mesh_data,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
