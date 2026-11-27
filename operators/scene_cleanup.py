"""Scene cleanup operators for KelitToolkit"""

import bpy
import mathutils


ORGANIZATION_ITEMS = [
    ('PARENTS', "Keep parent hierarchy",
     "Empties with children stay as they are organised. Their rotation and scale are "
     "pushed down to the children so the meshes can be cleaned. Only empties without "
     "children are deleted"),
    ('COLLECTIONS', "Turn into collections",
     "Each deleted empty that had children becomes a collection with the same name, "
     "nested like the empties were"),
    ('FLAT', "Flatten",
     "Empties are deleted, their children re-parented one level up"),
]


def _object_pointers(struct, depth=0):
    """(property, object) for every Object an RNA struct points at: the
    target and pole of a constraint, the targets list of an Armature
    constraint, the object of a Hook, Array offset, Curve, Lattice, Mirror,
    Boolean, Shrinkwrap modifier, a Texture Coordinate node, ... read from
    the RNA, so a new modifier type is covered too."""
    for prop in struct.bl_rna.properties:
        if prop.type == 'POINTER':
            fixed = getattr(prop, 'fixed_type', None)
            if fixed is None or fixed.identifier != 'Object':
                continue
            try:
                value = getattr(struct, prop.identifier)
            except Exception:
                continue
            if value is not None:
                yield prop.identifier, value
        elif prop.type == 'COLLECTION' and depth == 0 and prop.identifier not in {'rna_type'}:
            try:
                items = list(getattr(struct, prop.identifier))
            except Exception:
                continue
            for item in items[:64]:
                if isinstance(item, bpy.types.bpy_struct) and not isinstance(item, bpy.types.ID):
                    yield from _object_pointers(item, depth + 1)


def _tree_object_pointers(tree, seen):
    """Objects a node tree points at (node settings and Object sockets),
    node groups followed once."""
    if tree is None or tree.as_pointer() in seen:
        return
    seen.add(tree.as_pointer())
    for node in tree.nodes:
        for _prop, value in _object_pointers(node, depth=1):
            yield value
        for socket in node.inputs:
            value = getattr(socket, 'default_value', None)
            if isinstance(value, bpy.types.Object):
                yield value
        group = getattr(node, 'node_tree', None)
        if group is not None:
            yield from _tree_object_pointers(group, seen)


def _driver_object_targets(id_block):
    anim = getattr(id_block, 'animation_data', None)
    if anim is None:
        return
    for driver in anim.drivers:
        for variable in driver.driver.variables:
            for target in variable.targets:
                if isinstance(target.id, bpy.types.Object):
                    yield target.id


def empty_references(scene):
    """{empty name: why it is needed} for every empty something points at.
    Deleting one of those breaks a rig, a deformation or an instance:
    constraint targets (on objects and on pose bones, the hundreds of
    targets of a character rig), bone shapes (widgets), modifier objects
    (Hook, Array offset, Curve, Lattice, Mirror...), particle and Geometry
    Nodes inputs, shader nodes, driver targets, camera focus, empties
    parented to a bone, empties instancing a collection, members of a
    collection that is instanced, and any other datablock Blender reports
    as a user (bpy.data.user_map)."""
    found = {}

    def note(obj, reason):
        if isinstance(obj, bpy.types.Object) and obj.type == 'EMPTY' and obj.name not in found:
            found[obj.name] = reason

    instanced_collections = set()
    for obj in scene.objects:
        label = f"'{obj.name}'"
        for constraint in obj.constraints:
            for _prop, target in _object_pointers(constraint):
                note(target, f"constraint target of {label} ({constraint.name})")
        for modifier in obj.modifiers:
            for _prop, target in _object_pointers(modifier):
                note(target, f"{modifier.type.lower()} modifier of {label}")
            if modifier.type == 'NODES':
                for key in modifier.keys():
                    value = modifier.get(key)
                    if isinstance(value, bpy.types.Object):
                        note(value, f"geometry nodes input of {label}")
                for target in _tree_object_pointers(modifier.node_group, set()):
                    note(target, f"geometry nodes of {label}")
        if obj.pose is not None:
            for bone in obj.pose.bones:
                if bone.custom_shape is not None:
                    note(bone.custom_shape, f"bone shape of {label}:{bone.name}")
                for constraint in bone.constraints:
                    for _prop, target in _object_pointers(constraint):
                        note(target, f"constraint target of bone {label}:{bone.name}")
        for system in getattr(obj, 'particle_systems', []):
            settings = system.settings
            note(settings.instance_object, f"particles of {label}")
            if settings.instance_collection is not None:
                instanced_collections.add(settings.instance_collection)
        if obj.type == 'EMPTY':
            if obj.parent is not None and obj.parent_type == 'BONE':
                note(obj, f"parented to bone {obj.parent.name}:{obj.parent_bone}")
            if obj.instance_type == 'COLLECTION' and obj.instance_collection is not None:
                note(obj, f"instances collection '{obj.instance_collection.name}'")
                instanced_collections.add(obj.instance_collection)
        if obj.type == 'CAMERA' and obj.data is not None:
            note(obj.data.dof.focus_object, f"focus of camera {label}")
        for source in (obj, obj.data, getattr(getattr(obj, 'data', None), 'shape_keys', None)):
            if source is not None:
                for target in _driver_object_targets(source):
                    note(target, f"driver target of {label}")
    for collection in instanced_collections:
        for member in collection.all_objects:
            note(member, f"member of instanced collection '{collection.name}'")
    for material in bpy.data.materials:
        for target in _tree_object_pointers(material.node_tree, set()):
            note(target, f"shader of material '{material.name}'")
        for target in _driver_object_targets(material):
            note(target, f"driver target of material '{material.name}'")
    for id_block in (scene, scene.world):
        if id_block is not None:
            for target in _driver_object_targets(id_block):
                note(target, f"driver target of '{id_block.name}'")

    # anything else Blender knows uses it (another scene, a light, text...)
    remaining = {o for o in scene.objects if o.type == 'EMPTY' and o.name not in found}
    if remaining:
        try:
            users = bpy.data.user_map(subset=remaining)
        except Exception:
            users = {}
        for empty, owners in users.items():
            for owner in owners:
                if isinstance(owner, (bpy.types.Collection, bpy.types.Scene)):
                    continue
                if isinstance(owner, bpy.types.Object) and (owner.parent == empty or owner == empty):
                    continue   # a child points at its parent: that is not a use
                note(empty, f"used by {type(owner).__name__} '{owner.name}'")
                break
    return found


# ============================================================================
# OPERATORS - SCENE CLEANUP
# ============================================================================

class OBJECT_OT_delete_hidden_objects(bpy.types.Operator):
    """Delete hidden or viewport-disabled objects from the CURRENT scene"""
    bl_idname = "kelit_toolkit.delete_hidden_objects"
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
    bl_idname = "kelit_toolkit.remove_empty_collections"
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
    bl_idname = "kelit_toolkit.purge_zero_face_meshes"
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
    bl_idname = "kelit_toolkit.clean_vertex_groups"
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
    """Delete empties nothing needs, keeping their children in place. Camera rigs, constraint targets and animated parents are kept"""
    bl_idname = "kelit_toolkit.delete_unused_empties"
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

    organization: bpy.props.EnumProperty(
        name="Organization",
        description="What becomes of the structure the empties gave the scene",
        items=ORGANIZATION_ITEMS,
        default='PARENTS'
    )

    dry_run: bpy.props.BoolProperty(
        name="Dry Run (List Only)",
        description="Change nothing: list in the report window what would be deleted "
                    "and why each kept empty is needed",
        default=False,
        options={'SKIP_SAVE'}   # never remembered: a real run is the default
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

        # 2. anything something points at: constraints (bones included),
        # bone shapes, modifiers, drivers, instancing, Blender users
        for name, reason in empty_references(context.scene).items():
            protect(bpy.data.objects.get(name), reason)

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

    def _candidates(self, context):
        """(empties to delete, structural empties kept because of the
        organization mode, protected empties with their reason)."""
        protected = self._protected_empties(context)
        pool = (context.scene.objects if self.process_all else context.selected_objects)
        candidates = [o for o in pool if o.type == 'EMPTY' and o.name not in protected]
        structural = []
        if self.organization == 'PARENTS':
            structural = [o for o in candidates if o.children]
            candidates = [o for o in candidates if not o.children]
        return candidates, structural, protected

    def _depth(self, obj):
        return sum(1 for _ in self._ancestors(obj))

    def _collections_for(self, context, candidates):
        """One collection per deleted empty that has children, nested like
        the empties (top-down so parents exist before their children)."""
        result = {}
        for empty in sorted(candidates, key=self._depth):
            if not empty.children:
                continue
            parent = empty.parent
            if parent is not None and parent.name in result:
                container = result[parent.name]
            elif empty.users_collection:
                container = empty.users_collection[0]
            else:
                container = context.scene.collection
            collection = container.children.get(empty.name)
            if collection is None:
                collection = bpy.data.collections.new(empty.name)
                container.children.link(collection)
            result[empty.name] = collection
        return result

    def _push_transforms_down(self, structural):
        """Keep the empties but leave them a plain location: their rotation
        and scale go to the children (world transforms unchanged), so the
        meshes can then be applied. Top-down, level by level."""
        for empty in sorted(structural, key=self._depth):
            saved = [(child, child.matrix_world.copy()) for child in empty.children]
            empty.matrix_world = mathutils.Matrix.Translation(empty.matrix_world.translation)
            for child, world in saved:
                child.matrix_world = world
        bpy.context.view_layer.update()

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "preserve_camera_rig")
        layout.prop(self, "process_all")
        layout.prop(self, "organization")
        layout.prop(self, "dry_run")

        # scanned once per set of options, not on every redraw: the scan
        # reads every constraint, modifier and driver of the scene
        options = (self.preserve_camera_rig, self.process_all, self.organization)
        cached = getattr(self, '_preview', None)
        if cached is None or cached[0] != options:
            cached = self._preview = (options, self._candidates(context))
        candidates, structural, protected = cached[1]
        box = layout.box()
        verb = "would be" if self.dry_run else "will be"
        box.label(text=f"{len(candidates)} empty(ies) {verb} deleted", icon='TRASH')
        if structural:
            box.label(text=f"{len(structural)} parent empty(ies) kept, transforms pushed down",
                      icon='OUTLINER_OB_EMPTY')
        box.label(text=f"{len(protected)} kept (referenced by a rig, a modifier, a driver...)",
                  icon='LOCKED')
        for name in list(protected)[:4]:
            box.label(text=f"   kept: {name} - {protected[name]}")

    def _write_report(self, context, names, structural, protected, done):
        """The deleted (or, in a dry run, deletable) empties, then every kept
        one with what needs it."""
        from .report import set_report
        verb = "Deleted" if done else "Would delete"
        lines = [('INFO', f"{verb} {len(names)} empty(ies); {len(protected)} kept because "
                          f"something uses them; {len(structural)} parent(s) kept")]
        # after a real deletion the names point at nothing: no object link
        lines += [('WARNING', f"{verb}: {name}", '' if done else name) for name in sorted(names)]
        lines += [('INFO', f"Kept {name}: {reason}", name)
                  for name, reason in sorted(protected.items())]
        set_report(context, "Delete Unused Empties", lines)

    def execute(self, context):
        self._preview = None
        candidates, structural, protected = self._candidates(context)
        if self.dry_run:
            from .report import open_report
            self._write_report(context, [o.name for o in candidates], structural, protected,
                               done=False)
            open_report(context)
            self.report({'INFO'}, f"Dry run: {len(candidates)} empty(ies) would be deleted, "
                                  f"{len(protected)} kept as referenced. Nothing changed")
            return {'CANCELLED'}
        if not candidates and not structural:
            self.report({'INFO'}, "No unused empty found")
            return {'CANCELLED'}
        deleted_names = [o.name for o in candidates]

        if structural:
            self._push_transforms_down(structural)

        collections = {}
        if self.organization == 'COLLECTIONS':
            collections = self._collections_for(context, candidates)

        # deepest first, so children are re-parented at most once per level
        deleted = 0
        for empty in sorted(candidates, key=self._depth, reverse=True):
            grandparent = empty.parent
            target = collections.get(empty.name)
            for child in list(empty.children):
                world = child.matrix_world.copy()
                child.parent = grandparent
                child.matrix_world = world
                if target is not None and child.name not in target.objects:
                    # the child moves into the empty's collection, leaving
                    # the collections it only shared with the empty
                    target.objects.link(child)
                    for collection in list(child.users_collection):
                        if collection != target and empty.name in collection.objects:
                            collection.objects.unlink(child)
            bpy.data.objects.remove(empty, do_unlink=True)
            deleted += 1

        message = f"Deleted {deleted} empty(ies)"
        if collections:
            message += f", {len(collections)} collection(s) created"
        if structural:
            message += f", {len(structural)} parent empty(ies) kept"
        if protected:
            message += f", kept {len(protected)} in use (see Show Last Report)"
        self._write_report(context, deleted_names, structural, protected, done=True)
        self.report({'INFO'}, message)
        return {'FINISHED'}


class OBJECT_OT_remove_empty_parents(bpy.types.Operator):
    """Remove Empty objects and keep only mesh children with proper transforms (perfect for Sketchfab imports)"""
    bl_idname = "kelit_toolkit.remove_empty_parents"
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
            # current scene only, as the option says (bpy.data.objects would
            # reach into every scene of the file)
            empty_objects = [obj for obj in context.scene.objects if obj.type == 'EMPTY']
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
    bl_idname = "kelit_toolkit.remove_unused_mesh_data"
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
