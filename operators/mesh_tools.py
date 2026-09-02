import bpy
import bmesh
import math
import mathutils
from ..utils import clean_name


# ============================================================================
# OPERATORS - MESH TOOLS
# ============================================================================

class OBJECT_OT_enhance_low_poly(bpy.types.Operator):
    """Improve low-poly object rendering with subdivision and bevel"""
    bl_idname = "object.enhance_low_poly"
    bl_label = "Enhance Low Poly"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    subdivision_levels: bpy.props.IntProperty(
        name="Subdivision Levels",
        description="Number of subdivision levels",
        default=1,
        min=0,
        max=3
    )

    sharp_angle: bpy.props.FloatProperty(
        name="Sharp Angle",
        description="Angle limit for sharp edges (in degrees)",
        default=70.0,
        min=0.0,
        max=180.0
    )

    use_bevel: bpy.props.BoolProperty(
        name="Use Bevel",
        description="Add a Bevel modifier",
        default=True
    )

    bevel_width: bpy.props.FloatProperty(
        name="Bevel Width",
        description="Bevel width",
        default=0.01,
        min=0.001,
        max=1.0
    )

    bevel_segments: bpy.props.IntProperty(
        name="Bevel Segments",
        description="Number of bevel segments",
        default=3,
        min=1,
        max=10
    )

    def execute(self, context):
        selected_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']

        if not selected_objects:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}

        for obj in selected_objects:
            if self.use_bevel:
                bevel = obj.modifiers.new(name="Bevel", type='BEVEL')
                bevel.width = self.bevel_width
                bevel.segments = self.bevel_segments
                bevel.limit_method = 'ANGLE'
                bevel.angle_limit = math.radians(self.sharp_angle)
                bevel.harden_normals = True

            if self.subdivision_levels > 0:
                subsurf = obj.modifiers.new(name="Subdivision", type='SUBSURF')
                subsurf.levels = self.subdivision_levels
                subsurf.render_levels = self.subdivision_levels
                subsurf.show_viewport = True
                subsurf.boundary_smooth = 'PRESERVE_CORNERS'

            weighted = obj.modifiers.new(name="WeightedNormal", type='WEIGHTED_NORMAL')
            weighted.weight = 100
            weighted.keep_sharp = True

            context.view_layer.objects.active = obj
            bpy.ops.object.shade_smooth()

        self.report({'INFO'}, f"{len(selected_objects)} object(s) enhanced")
        return {'FINISHED'}


class OBJECT_OT_create_collision_mesh(bpy.types.Operator):
    """Create simplified collision mesh for Unreal (UCX_, UBX_, USP_)"""
    bl_idname = "object.create_collision_mesh"
    bl_label = "Create Collision Mesh"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    collision_type: bpy.props.EnumProperty(
        name="Collision Type",
        description="Type of collision to create",
        items=[
            ('UCX', "Convex (UCX_)", "Convex collision - most common"),
            ('UBX', "Box (UBX_)", "Simple box collision"),
            ('USP', "Sphere (USP_)", "Spherical collision"),
        ],
        default='UCX'
    )

    def execute(self, context):
        selected_objs = [obj for obj in context.selected_objects if obj.type == 'MESH']

        if not selected_objs:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}

        created_count = 0
        for obj in selected_objs:
            # Local bounding box (independent of rotation and origin placement)
            local_corners = [mathutils.Vector(corner) for corner in obj.bound_box]
            local_min = mathutils.Vector((min(v[i] for v in local_corners) for i in range(3)))
            local_max = mathutils.Vector((max(v[i] for v in local_corners) for i in range(3)))
            local_center = (local_min + local_max) / 2
            local_size = local_max - local_min

            # Create collision based on type
            if self.collision_type == 'UBX':
                # Oriented bounding box: same rotation as the object, centered
                # on its bbox center (not its origin)
                bpy.ops.mesh.primitive_cube_add(size=1)
                collision_obj = context.active_object
                collision_obj.matrix_world = obj.matrix_world @ mathutils.Matrix.Translation(local_center)
                collision_obj.scale = mathutils.Vector(
                    (collision_obj.scale[i] * local_size[i] for i in range(3)))

            elif self.collision_type == 'USP':
                # Sphere centered on the world bbox center
                bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5)
                collision_obj = context.active_object
                world_size = [abs(local_size[i] * obj.matrix_world.to_scale()[i]) for i in range(3)]
                avg_dim = sum(world_size) / 3
                collision_obj.location = obj.matrix_world @ local_center
                collision_obj.scale = (avg_dim, avg_dim, avg_dim)

            else:  # UCX
                # UCX must be CONVEX: Unreal silently hulls whatever it gets,
                # so a decimated concave copy diverged from what Blender
                # showed. Build the actual convex hull, then simplify it.
                collision_obj = obj.copy()
                collision_obj.data = obj.data.copy()
                # the copy inherits the modifier stack: a Subsurf or Array
                # left on the UCX re-shaped the hull again at export time
                collision_obj.modifiers.clear()
                context.collection.objects.link(collision_obj)
                context.view_layer.objects.active = collision_obj

                # hull the EVALUATED geometry (what Blender shows, modifiers
                # included), not the raw mesh
                evaluated = obj.evaluated_get(context.evaluated_depsgraph_get())
                hull_bm = bmesh.new()
                hull_bm.from_mesh(evaluated.data)
                result = bmesh.ops.convex_hull(hull_bm, input=hull_bm.verts)
                interior = [element for element in result.get('geom_interior', [])
                            if isinstance(element, bmesh.types.BMVert)]
                if interior:
                    bmesh.ops.delete(hull_bm, geom=interior, context='VERTS')
                hull_bm.to_mesh(collision_obj.data)
                hull_bm.free()

                # merge near-coplanar hull faces (planar dissolve keeps the
                # hull convex, unlike a collapse decimate which can dent it)
                decimate = collision_obj.modifiers.new(name="Decimate", type='DECIMATE')
                decimate.decimate_type = 'DISSOLVE'
                decimate.angle_limit = math.radians(5.0)
                bpy.ops.object.modifier_apply(modifier=decimate.name)

            # Name according to Unreal convention
            base_obj_name = clean_name(obj.name)
            collision_obj.name = f"{self.collision_type}_{base_obj_name}"

            # Reuse the shared semi-transparent collision material
            mat = bpy.data.materials.get("Collision_Material")
            if mat is None:
                mat = bpy.data.materials.new(name="Collision_Material")
                mat.use_nodes = True
                # blend_method is deprecated in Blender 5.x in favour of
                # surface_render_method - support both while they coexist
                if hasattr(mat, 'surface_render_method'):
                    mat.surface_render_method = 'BLENDED'
                elif hasattr(mat, 'blend_method'):
                    mat.blend_method = 'BLEND'
                bsdf = mat.node_tree.nodes.get('Principled BSDF')
                if bsdf:
                    bsdf.inputs['Base Color'].default_value = (0, 1, 0, 1)
                    bsdf.inputs['Alpha'].default_value = 0.3

            if collision_obj.data.materials:
                collision_obj.data.materials[0] = mat
            else:
                collision_obj.data.materials.append(mat)

            created_count += 1

        self.report({'INFO'}, f"{created_count} collision mesh(es) created")
        return {'FINISHED'}


class OBJECT_OT_generate_lods(bpy.types.Operator):
    """Automatically generate LODs for selected objects"""
    bl_idname = "object.generate_lods"
    bl_label = "Generate LODs"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    lod_count: bpy.props.IntProperty(
        name="LOD Count",
        description="Number of LODs to generate",
        default=3,
        min=1,
        max=5
    )

    reduction_factor: bpy.props.FloatProperty(
        name="Reduction Factor",
        description="Reduction factor between each LOD",
        default=0.5,
        min=0.1,
        max=0.9
    )

    visual_offset: bpy.props.BoolProperty(
        name="Visual Offset",
        description="Shift each LOD on the X axis for inspection. Leave OFF for "
                    "export: offset LODs would come in displaced in Unreal",
        default=False
    )

    def execute(self, context):
        selected_objs = [obj for obj in context.selected_objects if obj.type == 'MESH']

        if not selected_objs:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}

        lods_created = 0
        for obj in selected_objs:
            current_ratio = 1.0

            for lod_level in range(1, self.lod_count + 1):
                current_ratio *= self.reduction_factor

                # Duplicate object
                lod_obj = obj.copy()
                lod_obj.data = obj.data.copy()
                context.collection.objects.link(lod_obj)

                # Name according to convention
                base_name = clean_name(obj.name)
                lod_obj.name = f"{base_name}_LOD{lod_level}"

                # Apply Decimate
                context.view_layer.objects.active = lod_obj
                decimate = lod_obj.modifiers.new(name="Decimate", type='DECIMATE')
                decimate.ratio = current_ratio
                bpy.ops.object.modifier_apply(modifier=decimate.name)

                if self.visual_offset:
                    lod_obj.location.x = obj.location.x + (lod_level * 2)

                lods_created += 1

        self.report({'INFO'}, f"{lods_created} LOD(s) generated")
        return {'FINISHED'}


class OBJECT_OT_convert_to_low_poly(bpy.types.Operator):
    """Create a low-poly copy of each selected object and bake the high-poly
    surface detail onto it (tangent normal + AO, optionally base color).
    The original is kept, hidden. Bakes run in Cycles and can take a while"""
    bl_idname = "object.convert_to_low_poly"
    bl_label = "High to Low Poly (Bake Details)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    target_faces: bpy.props.IntProperty(
        name="Target Faces",
        description="Face budget for the low-poly copy (decimated from the "
                    "evaluated high-poly, modifiers included)",
        default=5000,
        min=50
    )

    bake_resolution: bpy.props.EnumProperty(
        name="Resolution",
        description="Baked texture size",
        items=[('512', "512", ""), ('1024', "1024", ""),
               ('2048', "2048", ""), ('4096', "4096", "")],
        default='1024'
    )

    bake_normal: bpy.props.BoolProperty(
        name="Normal Map",
        description="Bake the high-poly surface into a tangent-space normal map",
        default=True
    )

    bake_ao: bpy.props.BoolProperty(
        name="Ambient Occlusion",
        description="Bake ambient occlusion (the high/low pair is isolated "
                    "from the rest of the scene during the bake)",
        default=True
    )

    bake_basecolor: bpy.props.BoolProperty(
        name="Base Color",
        description="Bake the high-poly albedo (no lighting) so the low-poly "
                    "looks right on its own, even from procedural materials",
        default=True
    )

    bake_samples: bpy.props.IntProperty(
        name="Samples",
        description="Cycles samples for the bakes (mostly affects AO quality)",
        default=32,
        min=8,
        max=256
    )

    cage_extrusion: bpy.props.FloatProperty(
        name="Cage Extrusion",
        description="How far the bake rays start outside the low-poly surface. "
                    "Raise it if the high-poly pokes through the low-poly",
        default=0.02,
        min=0.0,
        max=1.0,
        subtype='DISTANCE'
    )

    keep_original: bpy.props.BoolProperty(
        name="Keep Original",
        description="Keep the high-poly in the scene (hidden in viewport and "
                    "render). Untick to delete it after the bake",
        default=True
    )

    def _make_image(self, name, size, non_color):
        existing = bpy.data.images.get(name)
        if existing is not None:
            bpy.data.images.remove(existing)
        image = bpy.data.images.new(name, size, size, alpha=False)
        if non_color:
            image.colorspace_settings.name = 'Non-Color'
        return image

    def _bake_pair(self, context, high, size):
        """Build the LP copy of one HP object and bake the maps. Returns
        (low_object, error_message)."""
        depsgraph = context.evaluated_depsgraph_get()
        evaluated = high.evaluated_get(depsgraph)
        mesh = bpy.data.meshes.new_from_object(
            evaluated, preserve_all_data_layers=True, depsgraph=depsgraph)
        if len(mesh.polygons) == 0:
            bpy.data.meshes.remove(mesh)
            return None, "no faces"

        base_name = clean_name(high.name)
        low = bpy.data.objects.new(f"{base_name}_LP", mesh)
        low.matrix_world = high.matrix_world.copy()
        for collection in high.users_collection or [context.collection]:
            collection.objects.link(low)

        # decimate to the face budget through the depsgraph (headless-safe).
        # Decimate collapse works on triangles, so budget in triangles too
        mesh.calc_loop_triangles()
        source_faces = max(1, len(mesh.loop_triangles))
        ratio = min(1.0, self.target_faces / source_faces)
        if ratio < 1.0:
            decimate = low.modifiers.new(name="Decimate", type='DECIMATE')
            decimate.ratio = ratio
            depsgraph = context.evaluated_depsgraph_get()
            decimated = bpy.data.meshes.new_from_object(
                low.evaluated_get(depsgraph))
            low.modifiers.remove(decimate)
            old_mesh = low.data
            low.data = decimated
            bpy.data.meshes.remove(old_mesh)
        for polygon in low.data.polygons:
            polygon.use_smooth = True

        # fresh non-overlapping UVs for the bake target
        context.view_layer.objects.active = low
        for other in context.selected_objects:
            other.select_set(False)
        low.select_set(True)
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.uv.smart_project(angle_limit=math.radians(66),
                                 island_margin=0.02)
        bpy.ops.object.mode_set(mode='OBJECT')

        # one bake material with one image node per map; the node selected
        # as active decides where each bake pass lands
        material = bpy.data.materials.get(f"M_{base_name}_LP")
        if material is not None:
            bpy.data.materials.remove(material)
        material = bpy.data.materials.new(f"M_{base_name}_LP")
        material.use_nodes = True
        nodes = material.node_tree.nodes
        links = material.node_tree.links
        principled = next(n for n in nodes if n.type == 'BSDF_PRINCIPLED')

        bake_nodes = {}
        maps = []
        if self.bake_basecolor:
            maps.append(('BaseColor', False, 'DIFFUSE'))
        if self.bake_normal:
            maps.append(('Normal', True, 'NORMAL'))
        if self.bake_ao:
            maps.append(('AO', True, 'AO'))
        for offset, (suffix, non_color, _bake_type) in enumerate(maps):
            image = self._make_image(f"T_{base_name}_{suffix}", size, non_color)
            node = nodes.new('ShaderNodeTexImage')
            node.image = image
            node.location = (-600, 300 - offset * 300)
            bake_nodes[suffix] = node

        if 'BaseColor' in bake_nodes:
            links.new(bake_nodes['BaseColor'].outputs['Color'],
                      principled.inputs['Base Color'])
        if 'Normal' in bake_nodes:
            normal_map = nodes.new('ShaderNodeNormalMap')
            normal_map.location = (-300, -300)
            links.new(bake_nodes['Normal'].outputs['Color'],
                      normal_map.inputs['Color'])
            links.new(normal_map.outputs['Normal'],
                      principled.inputs['Normal'])

        low.data.materials.clear()
        low.data.materials.append(material)

        # selected-to-active: high selected, low active
        high.select_set(True)
        high.hide_set(False)
        context.view_layer.objects.active = low

        for suffix, _non_color, bake_type in maps:
            for node in nodes:
                node.select = False
            bake_nodes[suffix].select = True
            nodes.active = bake_nodes[suffix]
            kwargs = {
                'type': bake_type,
                'use_selected_to_active': True,
                'cage_extrusion': self.cage_extrusion,
                'use_clear': True,
                'margin': 8,
            }
            if bake_type == 'DIFFUSE':
                kwargs['pass_filter'] = {'COLOR'}
            result = bpy.ops.object.bake(**kwargs)
            if result != {'FINISHED'}:
                return low, f"{suffix} bake failed"
            bake_nodes[suffix].image.pack()

        high.select_set(False)
        return low, None

    def execute(self, context):
        targets = [obj for obj in context.selected_objects
                   if obj.type == 'MESH' and not obj.name.endswith('_LP')
                   and not obj.library]
        if not targets:
            self.report({'WARNING'}, "No editable mesh objects selected")
            return {'CANCELLED'}
        if not (self.bake_normal or self.bake_ao or self.bake_basecolor):
            self.report({'WARNING'}, "Pick at least one map to bake")
            return {'CANCELLED'}
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        scene = context.scene
        size = int(self.bake_resolution)
        previous_engine = scene.render.engine
        previous_samples = None
        hidden_states = {}
        converted, failures = [], []
        # everything that changes scene state lives inside the try: a failure
        # halfway (a linked object refusing hide_render, a bake error) must
        # still put the render engine and the visibility flags back
        try:
            scene.render.engine = 'CYCLES'
            if hasattr(scene, 'cycles'):
                previous_samples = scene.cycles.samples
                scene.cycles.samples = self.bake_samples

            # isolate each bake pair: anything else contributing AO or
            # shadows would pollute the maps (linked objects are read-only
            # and stay as they are)
            for obj in context.view_layer.objects:
                if obj.library:
                    continue
                hidden_states[obj.name] = obj.hide_render
                obj.hide_render = True

            for high in targets:
                high.hide_render = False
                try:
                    low, error = self._bake_pair(context, high, size)
                except RuntimeError as bake_error:
                    low, error = None, str(bake_error)[:120]
                if low is not None:
                    hidden_states[low.name] = False
                if error:
                    failures.append(f"{high.name} ({error})")
                    high.hide_render = True
                    continue
                converted.append((high, low))
                high.hide_render = True
        finally:
            for obj in context.view_layer.objects:
                if obj.name in hidden_states:
                    obj.hide_render = hidden_states[obj.name]
            scene.render.engine = previous_engine
            if previous_samples is not None and hasattr(scene, 'cycles'):
                scene.cycles.samples = previous_samples

        for high, low in converted:
            if self.keep_original:
                high.hide_set(True)
                high.hide_render = True
            else:
                bpy.data.objects.remove(high)
            low.select_set(True)
            context.view_layer.objects.active = low

        message = f"{len(converted)} object(s) converted to low poly"
        if converted:
            faces = sum(len(low.data.polygons) for _high, low in converted)
            message += f" ({faces} faces total, maps packed in .blend)"
        if failures:
            message += f" - {len(failures)} failed: {', '.join(failures[:3])}"
            self.report({'WARNING'}, message)
        else:
            self.report({'INFO'}, message)
        return {'FINISHED'} if converted else {'CANCELLED'}


classes = (
    OBJECT_OT_enhance_low_poly,
    OBJECT_OT_create_collision_mesh,
    OBJECT_OT_generate_lods,
    OBJECT_OT_convert_to_low_poly,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
