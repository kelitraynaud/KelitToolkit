"""Instance management operators for KelitToolkit"""

import bpy
import mathutils
from collections import defaultdict
from ..utils import clean_name, material_fingerprint


class OBJECT_OT_replace_with_active_instance(bpy.types.Operator):
    """Replace selected objects with instances of the active object"""
    bl_idname = "kelit_toolkit.replace_with_active_instance"
    bl_label = "Replace With Active Instance"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'OBJECT' and context.active_object is not None
                and bool(context.selected_objects))

    rename_to_mesh: bpy.props.BoolProperty(
        name="Rename to Mesh Name",
        description="Rename objects to match the mesh data name with clean numbering",
        default=True
    )

    def execute(self, context):
        active_obj = context.active_object
        if not active_obj:
            self.report({'WARNING'}, "No active object found")
            return {'CANCELLED'}
        if active_obj.type != 'MESH' or active_obj.data is None:
            # an active empty/curve would silently replace every selected
            # mesh with instances of nothing and delete the originals
            self.report({'WARNING'}, "The active object must be a mesh")
            return {'CANCELLED'}

        selected_objs = [obj for obj in context.selected_objects if obj != active_obj]
        if not selected_objs:
            self.report({'INFO'}, "No other objects selected")
            return {'CANCELLED'}

        # Determine base name for new objects
        if self.rename_to_mesh and active_obj.data:
            base_name = active_obj.data.name
        else:
            base_name = active_obj.name

        count = 0
        created_objects = []

        for obj in selected_objs:
            if obj.type != 'MESH':
                continue

            new_obj = bpy.data.objects.new(name=base_name, object_data=active_obj.data)

            # Link to collections first
            for coll in obj.users_collection:
                coll.objects.link(new_obj)

            # Copy the world matrix to preserve exact position/rotation/scale in world space
            new_obj.matrix_world = obj.matrix_world.copy()

            # Copy modifiers from original object to preserve them
            for mod in obj.modifiers:
                new_mod = new_obj.modifiers.new(name=mod.name, type=mod.type)
                # Copy all modifier properties
                for prop in [p.identifier for p in mod.bl_rna.properties if not p.is_readonly]:
                    try:
                        setattr(new_mod, prop, getattr(mod, prop))
                    except Exception:
                        # Some properties can't be set directly, skip them
                        pass

            created_objects.append(new_obj)
            bpy.data.objects.remove(obj, do_unlink=True)
            count += 1

        # Clean up numbering if rename option is enabled
        if self.rename_to_mesh and len(created_objects) > 1:
            for i, obj in enumerate(created_objects, start=1):
                obj.name = f"{base_name}.{i:03d}"

        self.report({'INFO'}, f"{count} objects replaced with instances of '{base_name}'")
        return {'FINISHED'}


class OBJECT_OT_group_similar_in_collection(bpy.types.Operator):
    """Group all objects with the same mesh as selected object into a sub-collection"""
    bl_idname = "kelit_toolkit.group_similar_in_collection"
    bl_label = "Group Similar in Collection"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        active_obj = context.active_object
        if not active_obj or active_obj.type != 'MESH':
            self.report({'WARNING'}, "No active mesh object selected")
            return {'CANCELLED'}

        mesh_data = active_obj.data

        # Find all objects with the same mesh data - CURRENT scene only:
        # scanning bpy.data.objects silently migrated objects across scenes
        similar_objects = [obj for obj in context.scene.objects
                           if obj.type == 'MESH' and obj.data == mesh_data]

        if len(similar_objects) <= 1:
            self.report({'INFO'}, "No other objects share this mesh")
            return {'CANCELLED'}

        # Create collection name based on mesh name
        collection_name = f"{mesh_data.name}_instances"

        # Check if collection already exists
        if collection_name in bpy.data.collections:
            target_collection = bpy.data.collections[collection_name]
            self.report({'INFO'}, f"Using existing collection '{collection_name}'")
        else:
            # Create new collection
            target_collection = bpy.data.collections.new(collection_name)
            # Link to scene
            context.scene.collection.children.link(target_collection)

        # Move all similar objects to the target collection
        moved_count = 0
        for obj in similar_objects:
            # Remove from current collections
            for coll in obj.users_collection:
                coll.objects.unlink(obj)

            # Add to target collection
            if obj.name not in target_collection.objects:
                target_collection.objects.link(obj)
                moved_count += 1

        self.report({'INFO'}, f"Grouped {moved_count} objects into collection '{collection_name}'")
        return {'FINISHED'}


class OBJECT_OT_detect_and_replace_instances(bpy.types.Operator):
    """Detect duplicate meshes and replace them with instances for optimization"""
    bl_idname = "kelit_toolkit.detect_and_replace_instances"
    bl_label = "Detect Duplicates & Replace to Instances"
    bl_options = {'REGISTER', 'UNDO'}

    search_scope: bpy.props.EnumProperty(
        name="Search Scope",
        description="Where to search for duplicates",
        items=[
            ('SELECTED', "Selected Only", "Only search within selected objects"),
            ('SCENE', "Entire Scene", "Search all mesh objects in the scene"),
        ],
        default='SELECTED'
    )

    rename_to_mesh: bpy.props.BoolProperty(
        name="Rename to Mesh Name",
        description="Rename objects to match the mesh data name with clean numbering",
        default=True
    )

    baked_positions: bpy.props.BoolProperty(
        name="Positions Baked in Mesh",
        description="Compare the shape regardless of where it sits inside the mesh: "
                    "imports from Maya or Cinema 4D often write each copy's placement "
                    "into the vertices. The instances are offset so they stay exactly "
                    "where the originals were",
        default=True
    )

    compare_materials: bpy.props.EnumProperty(
        name="Materials",
        description="What makes two materials 'the same' for the comparison",
        items=[
            ('CONTENT', "Same textures and values",
             "Materials with the same images, values and links match, whatever "
             "their names (rooftop_01 and rooftop_01.001 are one material)"),
            ('NAME', "Same names", "Only objects using the very same materials match"),
            ('IGNORE', "Ignore materials", "Compare geometry and UVs only"),
        ],
        default='CONTENT'
    )

    def invoke(self, context, event):
        mesh_objects = self.get_search_objects(context)
        if not mesh_objects:
            self.report({'WARNING'}, "No mesh objects to analyze")
            return {'CANCELLED'}
        if not self._scan(context):
            self.report({'INFO'}, "No duplicate meshes found")
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=450)

    def _scan(self, context):
        """Duplicate groups for the current options, cached per option set so
        the dialog can redraw freely (a redo panel re-instantiates the
        operator without invoke: getattr with defaults everywhere)."""
        key = (self.search_scope, self.baked_positions, self.compare_materials)
        cached = getattr(self, '_scan_cache', None)
        if cached is not None and cached[0] == key:
            return cached[1]
        mesh_objects = self.get_search_objects(context)
        groups = self.find_duplicate_meshes(mesh_objects)
        self.total_objects = len(mesh_objects)
        self._scan_cache = (key, groups)
        return groups

    def draw(self, context):
        layout = self.layout
        groups = self._scan(context)
        total_objects = getattr(self, 'total_objects', 0)
        duplicate_count = sum(len(group) for group in groups.values())

        # Statistics box
        box = layout.box()
        box.label(text="Duplicate Detection Results:", icon='INFO')
        box.label(text=f"  Total objects analyzed: {total_objects}")
        box.label(text=f"  Duplicate groups found: {len(groups)}")
        box.label(text=f"  Objects that could be instanced: {duplicate_count}")

        layout.separator()

        # Show first few duplicate groups
        box = layout.box()
        box.label(text="Duplicate Groups:", icon='OUTLINER_OB_MESH')
        max_show = 5
        for mesh_data, objects in list(groups.items())[:max_show]:
            box.label(text=f"  {mesh_data.name}: {len(objects)} duplicates")
        if len(groups) > max_show:
            box.label(text=f"  ... and {len(groups) - max_show} more groups")
        if not groups:
            box.label(text="  none with these options")

        layout.separator()

        # Options (the counts above follow them)
        layout.prop(self, "baked_positions")
        layout.prop(self, "compare_materials")
        layout.prop(self, "rename_to_mesh")

        layout.separator()
        box = layout.box()
        box.label(text="Will convert to instances", icon='CHECKMARK')
        box.label(text="(Keeps first object as master)")

    def execute(self, context):
        # ALWAYS recompute here: a redo (F9) first undoes the previous run,
        # which both re-instantiates the operator (no invoke) and leaves any
        # stored object references dangling
        self._scan_cache = None
        mesh_objects = self.get_search_objects(context)
        self.duplicate_groups = self.find_duplicate_meshes(mesh_objects)
        if not self.duplicate_groups:
            self.report({'INFO'}, "No duplicate meshes found")
            return {'CANCELLED'}

        # Convert duplicates to instances
        converted_count = 0
        all_created_objects = {}  # Track created objects per group for renaming

        for mesh_data, objects in self.duplicate_groups.items():
            if len(objects) < 2:
                continue

            # First object becomes the master
            master = objects[0]

            # Determine base name for this group
            if self.rename_to_mesh and master.data:
                group_base_name = master.data.name
            else:
                group_base_name = master.name

            created_in_group = []

            # Replace others with instances
            for obj in objects[1:]:
                # Create instance
                new_obj = bpy.data.objects.new(name=group_base_name, object_data=master.data)

                # Link to same collections first
                for coll in obj.users_collection:
                    coll.objects.link(new_obj)

                # Copy the world matrix to preserve exact position/rotation/scale
                # in world space. With baked positions the two meshes differ by
                # a translation inside the mesh: shift the instance by that
                # delta (in its own local space) so the geometry lands exactly
                # where the original's did
                new_matrix = obj.matrix_world.copy()
                centroids = getattr(self, '_centroids', {})
                if obj.name in centroids and master.name in centroids:
                    delta = centroids[obj.name] - centroids[master.name]
                    new_matrix = new_matrix @ mathutils.Matrix.Translation(delta)
                new_obj.matrix_world = new_matrix

                # Copy modifiers from original object to preserve them
                for mod in obj.modifiers:
                    new_mod = new_obj.modifiers.new(name=mod.name, type=mod.type)
                    # Copy all modifier properties
                    for prop in [p.identifier for p in mod.bl_rna.properties if not p.is_readonly]:
                        try:
                            setattr(new_mod, prop, getattr(mod, prop))
                        except Exception:
                            # Some properties can't be set directly, skip them
                            pass

                created_in_group.append(new_obj)

                # Remove old object
                bpy.data.objects.remove(obj, do_unlink=True)
                converted_count += 1

            # Store created objects for this group
            if created_in_group:
                all_created_objects[group_base_name] = created_in_group

        # Clean up numbering if rename option is enabled
        if self.rename_to_mesh:
            for group_base_name, objs in all_created_objects.items():
                if len(objs) > 1:
                    for i, obj in enumerate(objs, start=1):
                        obj.name = f"{group_base_name}.{i:03d}"

        self.report({'INFO'}, f"Converted {converted_count} objects to instances ({len(self.duplicate_groups)} groups)")
        return {'FINISHED'}

    def get_search_objects(self, context):
        """Get objects to search based on scope"""
        if self.search_scope == 'SELECTED':
            return [obj for obj in context.selected_objects if obj.type == 'MESH' and obj.data]
        else:  # SCENE
            return [obj for obj in context.scene.objects if obj.type == 'MESH' and obj.data]

    def find_duplicate_meshes(self, objects):
        """Find objects with different mesh data but identical geometry"""
        # The signature must cover more than raw geometry: two meshes with
        # the same shape but different UVs, materials or shape keys are NOT
        # interchangeable - collapsing them would silently destroy variants
        baked = getattr(self, 'baked_positions', True)
        material_mode = getattr(self, 'compare_materials', 'CONTENT')
        centroids = {}

        def mesh_centroid(mesh_data):
            total = mathutils.Vector()
            for vertex in mesh_data.vertices:
                total += vertex.co
            return total / max(len(mesh_data.vertices), 1)

        def material_key(material):
            if material_mode == 'IGNORE':
                return ''
            if material_mode == 'NAME':
                return material.name if material else ''
            return material_fingerprint(material)

        def get_mesh_signature(mesh_data):
            """Create a hashable signature of the mesh + its surfacing"""
            if not mesh_data:
                return None

            # Count basic geometry
            vert_count = len(mesh_data.vertices)
            edge_count = len(mesh_data.edges)
            poly_count = len(mesh_data.polygons)

            # Vertex positions, rounded to a tenth of a millimetre (float32
            # coordinates far from the origin carry no more precision than
            # that). Baked mode measures them from the mesh centroid so the
            # same shape written at two places in the file still matches
            origin = mesh_centroid(mesh_data) if baked else mathutils.Vector()
            vert_positions = tuple(
                tuple(round(component, 4) for component in (v.co - origin))
                for v in mesh_data.vertices
            )

            # Get edge indices
            edge_indices = tuple(
                tuple(sorted([e.vertices[0], e.vertices[1]]))
                for e in mesh_data.edges
            )

            # material slots (in order) + per-face assignment checksum
            material_names = tuple(material_key(mat) for mat in mesh_data.materials)
            material_assignment = tuple(
                poly.material_index for poly in mesh_data.polygons)

            # UV checksum: layer names + rounded coordinates of the active
            # layer (full precision would be slow and needlessly strict)
            uv_signature = tuple(layer.name for layer in mesh_data.uv_layers)
            active_uv = mesh_data.uv_layers.active
            if active_uv is not None:
                uv_checksum = 0.0
                for loop_uv in active_uv.data:
                    uv_checksum += round(loop_uv.uv[0], 4) + round(loop_uv.uv[1], 4) * 7.0
                uv_signature = uv_signature + (round(uv_checksum, 2),)

            shape_key_names = tuple(
                block.name for block in mesh_data.shape_keys.key_blocks
            ) if mesh_data.shape_keys else ()

            return (vert_count, edge_count, poly_count, vert_positions,
                    edge_indices, material_names, material_assignment,
                    uv_signature, shape_key_names)

        # Group objects by mesh signature
        signature_groups = defaultdict(list)

        for obj in objects:
            if obj.data:
                signature = get_mesh_signature(obj.data)
                if signature:
                    signature_groups[signature].append(obj)
                    if baked:
                        centroids[obj.name] = mesh_centroid(obj.data)

        # Filter groups that have more than one object (duplicates)
        # Convert back to a dict with the first mesh as key for display
        duplicate_groups = {}
        for signature, objs in signature_groups.items():
            if len(objs) > 1:
                # Use the first object's mesh as the key
                duplicate_groups[objs[0].data] = objs

        self._centroids = centroids
        return duplicate_groups


class OBJECT_OT_realize_modifiers_to_instances(bpy.types.Operator):
    """Convert modifiers (Array, Mirror, etc.) to individual mesh instances respecting stack order"""
    bl_idname = "kelit_toolkit.realize_modifiers_to_instances"
    bl_label = "Realize Modifiers to Instances"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    def execute(self, context):
        selected_objs = [obj for obj in context.selected_objects if obj.type == 'MESH']

        if not selected_objs:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}

        total_instances = 0
        processed_objects = 0

        for obj in selected_objs:
            # Get ALL modifiers to apply in stack order
            # Focus on modifiers that create geometry: Array, Mirror, Solidify, Geometry Nodes, etc.
            modifiers_to_apply = [
                mod for mod in obj.modifiers
                if mod.type in ('ARRAY', 'MIRROR', 'SOLIDIFY', 'NODES')
            ]

            if not modifiers_to_apply:
                self.report({'WARNING'}, f"No Array/Mirror/Solidify/GeometryNodes modifiers found on {obj.name}")
                continue

            processed_objects += 1
            original_name = clean_name(obj.name)

            # Select and make active
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            context.view_layer.objects.active = obj

            # Apply ALL modifiers in the stack order
            # This respects dependencies and user-defined order
            applied_count = 0
            for mod in modifiers_to_apply:
                try:
                    mod_name = mod.name
                    mod_type = mod.type
                    bpy.ops.object.modifier_apply(modifier=mod_name)
                    applied_count += 1
                    self.report({'INFO'}, f"Applied {mod_type} modifier '{mod_name}'")
                except Exception as e:
                    self.report({'WARNING'}, f"Could not apply {mod.type} '{mod.name}': {str(e)}")
                    continue

            if applied_count == 0:
                self.report({'WARNING'}, f"No modifiers could be applied on {obj.name}")
                continue

            # Separate by loose parts to create individual instances
            try:
                bpy.ops.object.mode_set(mode='EDIT')
                bpy.ops.mesh.select_all(action='SELECT')
                bpy.ops.mesh.separate(type='LOOSE')
                bpy.ops.object.mode_set(mode='OBJECT')
            except Exception as e:
                self.report({'WARNING'}, f"Could not separate mesh: {str(e)}")
                try:
                    bpy.ops.object.mode_set(mode='OBJECT')
                except Exception:
                    pass
                continue

            # Get the newly created instances
            separated_objects = list(context.selected_objects)
            instance_count = len(separated_objects)
            total_instances += instance_count

            self.report({'INFO'}, f"Created {instance_count} objects from {obj.name}")

            # Create a collection for these instances if multiple objects
            if instance_count > 1:
                collection_name = f"{original_name}_instances"

                # Find the parent collection of the original object
                parent_collection = None
                for coll in obj.users_collection:
                    parent_collection = coll
                    break

                # If no parent found, use scene collection
                if parent_collection is None:
                    parent_collection = context.scene.collection

                # Create or get collection
                if collection_name in bpy.data.collections:
                    target_collection = bpy.data.collections[collection_name]
                else:
                    target_collection = bpy.data.collections.new(collection_name)
                    # Link to the same parent collection as the original object
                    parent_collection.children.link(target_collection)

                # Move all instances to the collection
                for instance_obj in separated_objects:
                    # Remove from current collections
                    for coll in list(instance_obj.users_collection):
                        coll.objects.unlink(instance_obj)

                    # Add to target collection
                    target_collection.objects.link(instance_obj)

                # Rename instances with clean numbering
                for i, instance_obj in enumerate(separated_objects, start=1):
                    instance_obj.name = f"{original_name}.{i:03d}"
            else:
                # Single object, just rename it
                if separated_objects:
                    separated_objects[0].name = original_name

        if processed_objects == 0:
            self.report({'WARNING'}, "No objects with Array/Mirror modifiers found")
            return {'CANCELLED'}

        self.report({'INFO'}, f"✓ Created {total_instances} instances from {processed_objects} object(s)")
        return {'FINISHED'}


class OBJECT_OT_apply_all_modifiers(bpy.types.Operator):
    """Apply all modifiers on selected objects"""
    bl_idname = "kelit_toolkit.apply_all_modifiers"
    bl_label = "Apply All Modifiers"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    def execute(self, context):
        selected_objs = [obj for obj in context.selected_objects if obj.type == 'MESH']

        if not selected_objs:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}

        total_modifiers = 0
        processed_objects = 0

        skipped = 0
        for obj in selected_objs:
            if not obj.modifiers:
                continue

            # applying on multi-user mesh data fails for EVERY modifier -
            # skip the object whole instead of failing one by one
            if obj.data and obj.data.users - int(obj.data.use_fake_user) > 1:
                self.report({'WARNING'},
                            f"{obj.name}: shared mesh data ({obj.data.users} users) - "
                            "make it single-user first (or use Realize Modifiers)")
                skipped += 1
                continue

            # Select and make active
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            context.view_layer.objects.active = obj

            applied_count = 0

            # Apply from first to last; on failure SKIP the modifier (never
            # remove it - deleting a user's modifier stack is data loss)
            failed_index = 0
            while failed_index < len(obj.modifiers):
                mod = obj.modifiers[failed_index]
                try:
                    bpy.ops.object.modifier_apply(modifier=mod.name)
                    applied_count += 1
                except Exception as e:
                    self.report({'WARNING'}, f"Could not apply modifier '{mod.name}' on {obj.name}: {str(e)}")
                    failed_index += 1

            if applied_count > 0:
                total_modifiers += applied_count
                processed_objects += 1

        if processed_objects == 0 and skipped == 0:
            self.report({'INFO'}, "No modifiers found on selected objects")
            return {'CANCELLED'}

        message = f"Applied {total_modifiers} modifiers on {processed_objects} objects"
        if skipped:
            message += f" - {skipped} skipped (shared mesh data)"
        self.report({'INFO'}, message)
        return {'FINISHED'}


# Registration
classes = (
    OBJECT_OT_replace_with_active_instance,
    OBJECT_OT_group_similar_in_collection,
    OBJECT_OT_detect_and_replace_instances,
    OBJECT_OT_realize_modifiers_to_instances,
    OBJECT_OT_apply_all_modifiers,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
