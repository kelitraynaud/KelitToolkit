import bpy


# ============================================================================
# OPERATORS - TEXTURE AUTO-SETUP (ENHANCED)
# ============================================================================

class OBJECT_OT_auto_setup_pbr_textures(bpy.types.Operator):
    """Detect and automatically configure PBR textures from a folder"""
    bl_idname = "object.auto_setup_pbr_textures"
    bl_label = "Auto Setup PBR Textures"
    bl_options = {'REGISTER', 'UNDO'}

    directory: bpy.props.StringProperty(
        name="Texture Folder",
        description="Folder containing textures",
        subtype='DIR_PATH'
    )

    include_subfolders: bpy.props.BoolProperty(
        name="Include Subfolders",
        description="Also search in subfolders",
        default=True
    )

    create_new_materials: bpy.props.BoolProperty(
        name="Create New Materials",
        description="Create new materials if none exist",
        default=True
    )

    assign_to_objects: bpy.props.BoolProperty(
        name="Auto-Assign to Objects",
        description="Automatically assign to objects with similar name",
        default=True
    )

    replace_empty_materials: bpy.props.BoolProperty(
        name="Replace Empty Materials",
        description="Replace materials without textures",
        default=True
    )

    # Enhanced patterns with more variations
    TEXTURE_PATTERNS = {
        'base_color': ['_BaseColor', '_Albedo', '_Diffuse', '_Color', '_BC', '_ALB', '_Base_Color', '_D'],
        'metallic': ['_Metallic', '_Metal', '_M', '_MET'],
        'roughness': ['_Roughness', '_Rough', '_R', '_RGH'],
        'normal': ['_Normal', '_Norm', '_N', '_NRM', '_NormalMap'],
        'ambient_occlusion': ['_AmbientOcclusion', '_AO', '_Occlusion', '_Ambient_Occlusion'],
        'alpha': ['_Alpha', '_Opacity', '_A', '_OPC'],
        'emission': ['_Emission', '_Emissive', '_E', '_EM'],
        'height': ['_Height', '_Displacement', '_H', '_Disp', '_DISP'],
        'subsurface': ['_Subsurface', '_SSS', '_Scattering'],
        'specular': ['_Specular', '_Spec', '_SP'],
    }

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        import os
        from pathlib import Path

        if not self.directory:
            self.report({'WARNING'}, "No folder selected")
            return {'CANCELLED'}

        # Scan textures
        texture_sets = self.scan_textures(self.directory, self.include_subfolders)

        if not texture_sets:
            self.report({'WARNING'}, "No PBR textures detected")
            return {'CANCELLED'}

        # Create or update materials (BATCH PROCESSING)
        materials_created = 0
        materials_updated = 0
        objects_assigned = 0

        for base_name, textures in texture_sets.items():
            # Check if material exists before creation
            existing_mat_names = {mat.name for mat in bpy.data.materials}

            material = self.get_or_create_material(base_name, textures)

            if material:
                # Determine if it's a creation or update
                is_new = material.name not in existing_mat_names

                # Check if material needs setup
                needs_setup = not material.use_nodes or len([n for n in material.node_tree.nodes if n.type == 'TEX_IMAGE']) == 0

                if needs_setup:
                    self.setup_pbr_nodes(material, textures)
                    if is_new:
                        materials_created += 1
                    else:
                        materials_updated += 1

                # Assign to objects
                if self.assign_to_objects:
                    assigned = self.assign_material_to_objects(material, base_name)
                    objects_assigned += assigned

        self.report({'INFO'},
                   f"Batch completed: {materials_created} material(s) created, "
                   f"{materials_updated} updated, {objects_assigned} object(s) assigned. "
                   f"{len(texture_sets)} set(s) detected")
        return {'FINISHED'}

    def scan_textures(self, directory, include_subfolders):
        """Scan folder and group textures by set (BATCH)"""
        import os
        from pathlib import Path

        texture_sets = {}
        valid_extensions = {'.png', '.jpg', '.jpeg', '.tga', '.tiff', '.exr', '.hdr', '.bmp'}

        if include_subfolders:
            texture_files = [f for f in Path(directory).rglob('*') if f.suffix.lower() in valid_extensions]
        else:
            texture_files = [f for f in Path(directory).glob('*') if f.suffix.lower() in valid_extensions]

        # match patterns as SUFFIXES of the stem, longest first: substring
        # matching classified "Wood_Door" as a BaseColor of set "Wood" (_D)
        # and "Old_Metal_Crate" as a Metallic of set "Old" (_Metal)
        suffix_patterns = []
        for map_type, patterns in self.TEXTURE_PATTERNS.items():
            for pattern in patterns:
                suffix_patterns.append((pattern.lower(), map_type))
        suffix_patterns.sort(key=lambda item: len(item[0]), reverse=True)

        for texture_path in texture_files:
            texture_name = texture_path.stem
            stem_lower = texture_name.lower()

            texture_type = None
            base_name = None
            for pattern, map_type in suffix_patterns:
                if stem_lower.endswith(pattern):
                    texture_type = map_type
                    base_name = texture_name[:len(texture_name) - len(pattern)]
                    break

            if texture_type and base_name:
                base_name = base_name.strip('_- ')
                if base_name:
                    texture_sets.setdefault(base_name, {})[texture_type] = str(texture_path)

        return texture_sets

    def get_or_create_material(self, base_name, textures):
        """Get or create a material"""
        mat_name = base_name.strip('_- ')
        if not mat_name:
            mat_name = "Material"

        # Look for existing material
        existing_mat = None
        for mat in bpy.data.materials:
            if mat_name.lower() == mat.name.lower() or mat_name.lower() in mat.name.lower():
                if self.replace_empty_materials:
                    if mat.use_nodes:
                        has_textures = any(node.type == 'TEX_IMAGE' for node in mat.node_tree.nodes)
                        if not has_textures:
                            existing_mat = mat
                            break
                else:
                    existing_mat = mat
                    break

        if existing_mat:
            return existing_mat
        elif self.create_new_materials:
            mat = bpy.data.materials.new(name=mat_name)
            mat.use_nodes = True
            return mat

        return None

    def setup_pbr_nodes(self, material, textures):
        """Configure PBR nodes optimally"""
        nodes = material.node_tree.nodes
        links = material.node_tree.links

        # Clean existing nodes (except output)
        output_node = None
        for node in nodes:
            if node.type == 'OUTPUT_MATERIAL':
                output_node = node
            else:
                nodes.remove(node)

        if not output_node:
            output_node = nodes.new('ShaderNodeOutputMaterial')

        output_node.location = (800, 0)

        # Create Principled BSDF
        bsdf = nodes.new('ShaderNodeBsdfPrincipled')
        bsdf.location = (400, 0)
        links.new(bsdf.outputs['BSDF'], output_node.inputs['Surface'])

        y_offset = 400
        x_pos = -400

        # Mapping and Texture Coordinate
        mapping = nodes.new('ShaderNodeMapping')
        mapping.location = (-800, 0)

        tex_coord = nodes.new('ShaderNodeTexCoord')
        tex_coord.location = (-1000, 0)
        links.new(tex_coord.outputs['UV'], mapping.inputs['Vector'])

        # Base Color
        if 'base_color' in textures:
            tex_node = self.create_texture_node(nodes, textures['base_color'],
                                               x_pos, y_offset, "Base Color")
            links.new(mapping.outputs['Vector'], tex_node.inputs['Vector'])
            links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])
            y_offset -= 300

        # Metallic
        if 'metallic' in textures:
            tex_node = self.create_texture_node(nodes, textures['metallic'],
                                               x_pos, y_offset, "Metallic")
            tex_node.image.colorspace_settings.name = 'Non-Color'
            links.new(mapping.outputs['Vector'], tex_node.inputs['Vector'])
            links.new(tex_node.outputs['Color'], bsdf.inputs['Metallic'])
            y_offset -= 300

        # Roughness
        if 'roughness' in textures:
            tex_node = self.create_texture_node(nodes, textures['roughness'],
                                               x_pos, y_offset, "Roughness")
            tex_node.image.colorspace_settings.name = 'Non-Color'
            links.new(mapping.outputs['Vector'], tex_node.inputs['Vector'])
            links.new(tex_node.outputs['Color'], bsdf.inputs['Roughness'])
            y_offset -= 300

        # Normal Map
        if 'normal' in textures:
            tex_node = self.create_texture_node(nodes, textures['normal'],
                                               x_pos, y_offset, "Normal")
            tex_node.image.colorspace_settings.name = 'Non-Color'

            normal_map = nodes.new('ShaderNodeNormalMap')
            normal_map.location = (100, y_offset)

            links.new(mapping.outputs['Vector'], tex_node.inputs['Vector'])
            links.new(tex_node.outputs['Color'], normal_map.inputs['Color'])
            links.new(normal_map.outputs['Normal'], bsdf.inputs['Normal'])
            y_offset -= 300

        # Ambient Occlusion (smartly mixed)
        if 'ambient_occlusion' in textures:
            ao_node = self.create_texture_node(nodes, textures['ambient_occlusion'],
                                              x_pos, y_offset, "AO")
            ao_node.image.colorspace_settings.name = 'Non-Color'
            links.new(mapping.outputs['Vector'], ao_node.inputs['Vector'])

            # If Base Color exists, mix with AO (Blender 4.0+ uses Mix Color)
            if 'base_color' in textures:
                # Use new Mix node (Blender 4.0+)
                try:
                    mix = nodes.new('ShaderNodeMix')
                    mix.data_type = 'RGBA'
                    mix.blend_type = 'MULTIPLY'
                    mix.location = (100, 200)
                    mix.inputs['Factor'].default_value = 1.0

                    for node in nodes:
                        if node.label == "Base Color" and node.type == 'TEX_IMAGE':
                            links.new(node.outputs['Color'], mix.inputs[6])  # A socket
                            break

                    links.new(ao_node.outputs['Color'], mix.inputs[7])  # B socket
                    links.new(mix.outputs[2], bsdf.inputs['Base Color'])  # Result socket
                except Exception:
                    # Fallback for older versions
                    mix = nodes.new('ShaderNodeMixRGB')
                    mix.blend_type = 'MULTIPLY'
                    mix.location = (100, 200)
                    mix.inputs['Fac'].default_value = 1.0

                    for node in nodes:
                        if node.label == "Base Color" and node.type == 'TEX_IMAGE':
                            links.new(node.outputs['Color'], mix.inputs['Color1'])
                            break

                    links.new(ao_node.outputs['Color'], mix.inputs['Color2'])
                    links.new(mix.outputs['Color'], bsdf.inputs['Base Color'])
            y_offset -= 300

        # Alpha
        if 'alpha' in textures:
            tex_node = self.create_texture_node(nodes, textures['alpha'],
                                               x_pos, y_offset, "Alpha")
            tex_node.image.colorspace_settings.name = 'Non-Color'
            links.new(mapping.outputs['Vector'], tex_node.inputs['Vector'])
            links.new(tex_node.outputs['Color'], bsdf.inputs['Alpha'])
            # blend_method is deprecated in 5.x - support both while they coexist
            if hasattr(material, 'surface_render_method'):
                material.surface_render_method = 'BLENDED'
            elif hasattr(material, 'blend_method'):
                material.blend_method = 'BLEND'
            y_offset -= 300

        # Emission (Blender 4.0+ renamed the socket)
        if 'emission' in textures:
            tex_node = self.create_texture_node(nodes, textures['emission'],
                                               x_pos, y_offset, "Emission")
            links.new(mapping.outputs['Vector'], tex_node.inputs['Vector'])

            # Blender 4.0+ uses 'Emission Color' instead of 'Emission'
            try:
                links.new(tex_node.outputs['Color'], bsdf.inputs['Emission Color'])
            except Exception:
                links.new(tex_node.outputs['Color'], bsdf.inputs['Emission'])

            bsdf.inputs['Emission Strength'].default_value = 1.0
            y_offset -= 300

        # Height/Displacement
        if 'height' in textures:
            tex_node = self.create_texture_node(nodes, textures['height'],
                                               x_pos, y_offset, "Height")
            tex_node.image.colorspace_settings.name = 'Non-Color'
            links.new(mapping.outputs['Vector'], tex_node.inputs['Vector'])

            disp = nodes.new('ShaderNodeDisplacement')
            disp.location = (600, -300)
            links.new(tex_node.outputs['Color'], disp.inputs['Height'])
            links.new(disp.outputs['Displacement'], output_node.inputs['Displacement'])
            y_offset -= 300

    def create_texture_node(self, nodes, image_path, x, y, label):
        """Create a texture node with loaded image"""
        tex_node = nodes.new('ShaderNodeTexImage')
        tex_node.location = (x, y)
        tex_node.label = label

        # Load image (avoid duplicates)
        img_name = bpy.path.basename(image_path)
        if img_name in bpy.data.images:
            tex_node.image = bpy.data.images[img_name]
        else:
            try:
                tex_node.image = bpy.data.images.load(image_path)
            except Exception:
                print(f"Error loading {image_path}")

        if tex_node.image is None:
            # callers dereference .image (colorspace, etc.) - give them a
            # visible magenta placeholder instead of an AttributeError
            placeholder = bpy.data.images.get('B2UE_MissingTexture')
            if placeholder is None:
                placeholder = bpy.data.images.new('B2UE_MissingTexture', 4, 4)
                placeholder.pixels = [1.0, 0.0, 1.0, 1.0] * 16
                placeholder.pack()
            tex_node.image = placeholder
            tex_node.label = f"{label} (MISSING)"

        return tex_node

    def assign_material_to_objects(self, material, base_name):
        """Assign material to matching objects"""
        assigned_count = 0

        for obj in bpy.context.scene.objects:
            if obj.type != 'MESH':
                continue

            # Check name matching
            name_match = (base_name.lower() in obj.name.lower() or
                         obj.name.lower() in base_name.lower())

            # Check if object has no material
            has_no_material = len(obj.data.materials) == 0

            # Check if existing material is empty
            has_empty_material = False
            if len(obj.data.materials) > 0 and obj.data.materials[0]:
                mat = obj.data.materials[0]
                if mat.use_nodes:
                    has_textures = any(node.type == 'TEX_IMAGE' for node in mat.node_tree.nodes)
                    has_empty_material = not has_textures

            # Assign - the NAME must match: with several texture sets, an
            # unconditional material-less fallback handed every bare object
            # whichever set happened to be processed first
            if name_match and (has_no_material
                               or (self.replace_empty_materials and has_empty_material)):
                if len(obj.data.materials) == 0:
                    obj.data.materials.append(material)
                else:
                    obj.data.materials[0] = material
                assigned_count += 1

        return assigned_count


classes = (
    OBJECT_OT_auto_setup_pbr_textures,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
