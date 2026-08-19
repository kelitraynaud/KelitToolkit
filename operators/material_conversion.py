"""Material conversion operators for KelitToolkit"""

import bpy
import os


# ============================================================================
# OPERATORS - MATERIAL CONVERSION FOR UNREAL
# ============================================================================

class OBJECT_OT_convert_to_simple_pbr(bpy.types.Operator):
    """Convert complex node trees to simple PBR setup (Base Color, Metallic, Roughness, Normal)"""
    bl_idname = "object.convert_to_simple_pbr"
    bl_label = "Convert to Simple PBR"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    def execute(self, context):
        selected_objs = [obj for obj in context.selected_objects if obj.type == 'MESH']

        if not selected_objs:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}

        materials_converted = set()

        for obj in selected_objs:
            for slot in obj.material_slots:
                if not slot.material or slot.material in materials_converted:
                    continue

                mat = slot.material
                if not mat.use_nodes:
                    continue

                nodes = mat.node_tree.nodes
                links = mat.node_tree.links

                # Find existing Principled BSDF or create one
                principled = None
                for node in nodes:
                    if node.type == 'BSDF_PRINCIPLED':
                        principled = node
                        break

                if not principled:
                    principled = nodes.new('ShaderNodeBsdfPrincipled')
                    principled.location = (0, 0)

                # Find material output
                output = None
                for node in nodes:
                    if node.type == 'OUTPUT_MATERIAL':
                        output = node
                        break

                if not output:
                    output = nodes.new('ShaderNodeOutputMaterial')
                    output.location = (300, 0)

                # Connect Principled to Output
                links.new(principled.outputs['BSDF'], output.inputs['Surface'])

                # Extract texture nodes
                base_color_node = None
                normal_node = None
                roughness_node = None
                metallic_node = None

                for node in nodes:
                    if node.type == 'TEX_IMAGE':
                        # Guess texture type by name
                        name_lower = node.image.name.lower() if node.image else ""

                        if any(keyword in name_lower for keyword in ['diffuse', 'color', 'basecolor', 'albedo']):
                            base_color_node = node
                        elif any(keyword in name_lower for keyword in ['normal', 'nmap']):
                            normal_node = node
                        elif any(keyword in name_lower for keyword in ['rough', 'roughness']):
                            roughness_node = node
                        elif any(keyword in name_lower for keyword in ['metal', 'metallic']):
                            metallic_node = node

                # Link textures to Principled BSDF OR create value textures
                if base_color_node:
                    base_color_node.location = (-400, 300)
                    links.new(base_color_node.outputs['Color'], principled.inputs['Base Color'])

                # ROUGHNESS: Create texture from value if no texture exists
                if roughness_node:
                    roughness_node.location = (-400, 0)
                    links.new(roughness_node.outputs['Color'], principled.inputs['Roughness'])
                else:
                    # Get current roughness value from Principled BSDF
                    roughness_value = principled.inputs['Roughness'].default_value

                    # Create a 1x1 texture with the roughness value
                    img_name = f"{mat.name}_Roughness"
                    if img_name not in bpy.data.images:
                        img = bpy.data.images.new(img_name, width=4, height=4)
                        # data map: sRGB would color-manage the value (0.5
                        # would sample as ~0.21) - and alpha must stay opaque
                        img.colorspace_settings.name = 'Non-Color'
                        pixels = ([roughness_value, roughness_value,
                                   roughness_value, 1.0]) * (4 * 4)
                        img.pixels = pixels
                        img.pack()
                    else:
                        img = bpy.data.images[img_name]

                    # Create texture node
                    roughness_tex = nodes.new('ShaderNodeTexImage')
                    roughness_tex.image = img
                    roughness_tex.location = (-400, 0)
                    roughness_tex.interpolation = 'Closest'
                    links.new(roughness_tex.outputs['Color'], principled.inputs['Roughness'])

                # METALLIC: Create texture from value if no texture exists
                if metallic_node:
                    metallic_node.location = (-400, -150)
                    links.new(metallic_node.outputs['Color'], principled.inputs['Metallic'])
                else:
                    # Get current metallic value from Principled BSDF
                    metallic_value = principled.inputs['Metallic'].default_value

                    # Create a 1x1 texture with the metallic value
                    img_name = f"{mat.name}_Metallic"
                    if img_name not in bpy.data.images:
                        img = bpy.data.images.new(img_name, width=4, height=4)
                        # data map: keep out of sRGB color management
                        img.colorspace_settings.name = 'Non-Color'
                        pixels = ([metallic_value, metallic_value,
                                   metallic_value, 1.0]) * (4 * 4)
                        img.pixels = pixels
                        img.pack()
                    else:
                        img = bpy.data.images[img_name]

                    # Create texture node
                    metallic_tex = nodes.new('ShaderNodeTexImage')
                    metallic_tex.image = img
                    metallic_tex.location = (-400, -150)
                    metallic_tex.interpolation = 'Closest'
                    links.new(metallic_tex.outputs['Color'], principled.inputs['Metallic'])

                if normal_node:
                    normal_node.location = (-700, -300)
                    # Create Normal Map node
                    normal_map = None
                    for node in nodes:
                        if node.type == 'NORMAL_MAP':
                            normal_map = node
                            break
                    if not normal_map:
                        normal_map = nodes.new('ShaderNodeNormalMap')
                    normal_map.location = (-400, -300)
                    links.new(normal_node.outputs['Color'], normal_map.inputs['Color'])
                    links.new(normal_map.outputs['Normal'], principled.inputs['Normal'])

                # Remove unnecessary nodes (keep only texture nodes, principled, output, normal map)
                nodes_to_keep = {'BSDF_PRINCIPLED', 'OUTPUT_MATERIAL', 'TEX_IMAGE', 'NORMAL_MAP'}
                nodes_to_remove = [node for node in nodes if node.type not in nodes_to_keep]

                for node in nodes_to_remove:
                    nodes.remove(node)

                materials_converted.add(mat)

        self.report({'INFO'}, f"Converted {len(materials_converted)} material(s) to simple PBR")
        return {'FINISHED'}


class OBJECT_OT_detect_unsupported_nodes(bpy.types.Operator):
    """Scan materials and report nodes that won't export properly to Unreal"""
    bl_idname = "object.detect_unsupported_nodes"
    bl_label = "Detect Unsupported Nodes"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        # Node types that are problematic for Unreal: legacy enums, their
        # 4.x/5.x replacements (MIX, SEPARATE/COMBINE_COLOR), and the
        # procedural textures UE cannot import at all
        unsupported_types = {
            'MATH', 'VALTORGB', 'MIX_RGB', 'SEPRGB', 'COMBRGB',
            'MIX', 'SEPARATE_COLOR', 'COMBINE_COLOR',
            'WAVELENGTH', 'BLACKBODY', 'BRIGHTCONTRAST',
            'HUE_SAT', 'INVERT', 'GAMMA', 'CURVE_RGB', 'CURVE_VEC',
            'TEX_NOISE', 'TEX_VORONOI', 'TEX_WAVE', 'TEX_MUSGRAVE',
            'TEX_GRADIENT', 'TEX_MAGIC', 'TEX_CHECKER', 'TEX_BRICK',
        }

        problematic_materials = {}

        for mat in bpy.data.materials:
            if not mat.use_nodes:
                continue

            unsupported_in_mat = []

            for node in mat.node_tree.nodes:
                if node.type in unsupported_types:
                    unsupported_in_mat.append(f"{node.name} ({node.type})")

            if unsupported_in_mat:
                problematic_materials[mat.name] = unsupported_in_mat

        if not problematic_materials:
            self.report({'INFO'}, "No unsupported nodes detected!")
            return {'FINISHED'}

        # Print report
        print("\n" + "="*60)
        print("UNSUPPORTED NODES DETECTED FOR UNREAL")
        print("="*60)

        for mat_name, nodes in problematic_materials.items():
            print(f"\nMaterial: {mat_name}")
            for node in nodes:
                print(f"  - {node}")

        print("\n" + "="*60 + "\n")

        self.report({'WARNING'}, f"Found unsupported nodes in {len(problematic_materials)} material(s) - Check console (Window > Toggle System Console)")
        return {'FINISHED'}


class OBJECT_OT_bake_procedural_to_texture(bpy.types.Operator):
    """Bake procedural materials to image textures"""
    bl_idname = "object.bake_procedural_to_texture"
    bl_label = "Bake Procedural to Texture"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    resolution: bpy.props.IntProperty(
        name="Resolution",
        description="Texture resolution",
        default=1024,
        min=256,
        max=4096
    )

    def execute(self, context):
        selected_objs = [obj for obj in context.selected_objects if obj.type == 'MESH']

        if not selected_objs:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}

        # baking is Cycles-only: switch temporarily instead of failing every
        # bake with an opaque error in EEVEE
        scene = context.scene
        previous_engine = scene.render.engine
        if previous_engine != 'CYCLES':
            scene.render.engine = 'CYCLES'

        baked_count = 0
        try:
            for obj in selected_objs:
                if not obj.material_slots:
                    continue

                for slot in obj.material_slots:
                    if not slot.material or not slot.material.use_nodes:
                        continue

                    mat = slot.material

                    # Create a new image for baking
                    image_name = f"{mat.name}_Baked"
                    if image_name in bpy.data.images:
                        image = bpy.data.images[image_name]
                    else:
                        image = bpy.data.images.new(image_name, self.resolution, self.resolution)

                    # Add image texture node for baking target
                    nodes = mat.node_tree.nodes
                    bake_node = nodes.new('ShaderNodeTexImage')
                    bake_node.image = image
                    bake_node.select = True
                    nodes.active = bake_node

                    # Select object and bake
                    bpy.ops.object.select_all(action='DESELECT')
                    obj.select_set(True)
                    context.view_layer.objects.active = obj

                    try:
                        bpy.ops.object.bake(type='DIFFUSE', pass_filter={'COLOR'})
                        # pack so the result survives a file reload, and drop
                        # the bake-target node from the material
                        image.pack()
                        baked_count += 1
                    except Exception as e:
                        self.report({'WARNING'}, f"Failed to bake {mat.name}: {str(e)}")
                    finally:
                        nodes.remove(bake_node)
        finally:
            if previous_engine != 'CYCLES':
                scene.render.engine = previous_engine

        self.report({'INFO'}, f"Baked {baked_count} material(s) (images packed)")
        return {'FINISHED'}


class OBJECT_OT_texture_path_validator(bpy.types.Operator):
    """Validate that all texture file paths exist on disk"""
    bl_idname = "object.texture_path_validator"
    bl_label = "Validate Texture Paths"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        missing_textures = []
        valid_count = 0

        for mat in bpy.data.materials:
            if not mat.use_nodes:
                continue

            for node in mat.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image:
                    image = node.image
                    # packed and generated images have no file on disk by
                    # design - they are not "missing"
                    if image.packed_file is not None or image.source == 'GENERATED':
                        valid_count += 1
                        continue
                    filepath = bpy.path.abspath(image.filepath)

                    if not filepath:
                        missing_textures.append(f"{mat.name} > {node.name}: No path set")
                    elif not os.path.exists(filepath):
                        missing_textures.append(f"{mat.name} > {node.name}: {filepath}")
                    else:
                        valid_count += 1

        if not missing_textures:
            self.report({'INFO'}, f"All {valid_count} texture paths are valid!")
            return {'FINISHED'}

        # Print report
        print("\n" + "="*60)
        print("MISSING OR INVALID TEXTURE PATHS")
        print("="*60)

        for tex in missing_textures:
            print(f"  - {tex}")

        print("\n" + "="*60 + "\n")

        self.report({'WARNING'}, f"{len(missing_textures)} texture(s) missing - Check console")
        return {'FINISHED'}


class OBJECT_OT_rename_textures_for_unreal(bpy.types.Operator):
    """Rename texture DATABLOCKS with the Unreal naming convention
    (_BaseColor, _Normal, ...). Files on disk keep their names"""
    bl_idname = "object.rename_textures_for_unreal"
    bl_label = "Rename Textures for Unreal"
    bl_options = {'REGISTER', 'UNDO'}

    # full suffix words stripped before re-suffixing, longest first, so the
    # operator is idempotent: wood_roughness -> T_wood_Roughness stays stable
    # on every re-run instead of growing a new suffix each time
    STRIP_SUFFIXES = (
        '_basecolor', '_base_color', '_albedo', '_diffuse', '_color',
        '_roughness', '_rough', '_metallic', '_metalness', '_metal',
        '_normal', '_nmap', '_nrm', '_occlusion', '_ambientocclusion',
        '_ambient', '_ao', '_emissive', '_emission',
        '_BaseColor', '_Normal', '_Roughness', '_Metallic', '_AO', '_Emissive',
    )

    def execute(self, context):
        renamed_count = 0

        for mat in bpy.data.materials:
            if not mat.use_nodes:
                continue

            for node in mat.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image:
                    if node.image.library is not None:
                        continue
                    old_name = node.image.name
                    name_lower = old_name.lower()

                    new_suffix = None

                    # Detect texture type and assign Unreal suffix
                    if any(keyword in name_lower for keyword in ['diffuse', 'color', 'basecolor', 'albedo']):
                        new_suffix = "_BaseColor"
                    elif any(keyword in name_lower for keyword in ['normal', 'nmap']):
                        new_suffix = "_Normal"
                    elif any(keyword in name_lower for keyword in ['rough', 'roughness']):
                        new_suffix = "_Roughness"
                    elif any(keyword in name_lower for keyword in ['metal', 'metallic']):
                        new_suffix = "_Metallic"
                    elif any(keyword in name_lower for keyword in ['ao', 'ambient', 'occlusion']):
                        new_suffix = "_AO"
                    elif any(keyword in name_lower for keyword in ['emissive', 'emission']):
                        new_suffix = "_Emissive"

                    if new_suffix:
                        # Remove extension and add suffix
                        base_name = old_name.rsplit('.', 1)[0]

                        # strip ALL existing type suffixes (longest first)
                        # until none remains - this is what makes re-runs
                        # converge instead of growing the name
                        stripped = True
                        while stripped:
                            stripped = False
                            for old_suffix in sorted(self.STRIP_SUFFIXES, key=len, reverse=True):
                                if base_name.lower().endswith(old_suffix.lower()):
                                    base_name = base_name[:-len(old_suffix)]
                                    stripped = True
                                    break

                        # Get extension
                        ext = old_name.rsplit('.', 1)[-1] if '.' in old_name else 'png'

                        new_name = f"{base_name}{new_suffix}.{ext}"

                        if new_name != old_name:
                            node.image.name = new_name
                            renamed_count += 1

        self.report({'INFO'}, f"Renamed {renamed_count} texture datablock(s) for Unreal")
        return {'FINISHED'}


class OBJECT_OT_extract_textures_from_nodes(bpy.types.Operator):
    """Extract and save all texture images from material nodes to a folder"""
    bl_idname = "object.extract_textures_from_nodes"
    bl_label = "Extract Textures from Nodes"
    bl_options = {'REGISTER', 'UNDO'}

    output_path: bpy.props.StringProperty(
        name="Output Folder",
        description="Folder to save extracted textures",
        default="//textures/",
        subtype='DIR_PATH'
    )

    def execute(self, context):
        output_dir = bpy.path.abspath(self.output_path)

        if not os.path.exists(output_dir):
            try:
                os.makedirs(output_dir)
            except Exception as e:
                self.report({'ERROR'}, f"Could not create directory: {str(e)}")
                return {'CANCELLED'}

        saved_count = 0

        for mat in bpy.data.materials:
            if not mat.use_nodes:
                continue

            for node in mat.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image:
                    image = node.image

                    # Skip if already saved to the target directory
                    if image.filepath.startswith(output_dir):
                        continue

                    # Determine file format
                    if image.file_format == '':
                        image.file_format = 'PNG'

                    # Build output path
                    filename = f"{image.name}.{image.file_format.lower()}"
                    output_filepath = os.path.join(output_dir, filename)

                    # Save image
                    try:
                        image.filepath_raw = output_filepath
                        image.save()
                        saved_count += 1
                    except Exception as e:
                        self.report({'WARNING'}, f"Failed to save {image.name}: {str(e)}")

        self.report({'INFO'}, f"Extracted {saved_count} texture(s) to {output_dir}")
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


classes = (
    OBJECT_OT_convert_to_simple_pbr,
    OBJECT_OT_detect_unsupported_nodes,
    OBJECT_OT_bake_procedural_to_texture,
    OBJECT_OT_texture_path_validator,
    OBJECT_OT_rename_textures_for_unreal,
    OBJECT_OT_extract_textures_from_nodes,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
