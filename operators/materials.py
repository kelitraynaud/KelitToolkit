import bpy
from collections import defaultdict
from ..utils import clean_name, material_fingerprint


def viewports_to_solid(context):
    """Material Preview / Rendered viewports keep pointers to GPU materials
    and image buffers: deleting materials or images while EEVEE redraws
    crashed Blender (EXCEPTION_ACCESS_VIOLATION in image_acquire_ibuf, from
    eevee MaterialModule::end_sync). Those viewports go to Solid for the
    operation and come back half a second later, once the deletion is
    through. Returns the restore function: call it in a ``finally`` so the
    viewports come back even when the operation fails."""
    switched = []
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != 'VIEW_3D':
                continue
            for space in area.spaces:
                if space.type == 'VIEW_3D' and space.shading.type in {'MATERIAL', 'RENDERED'}:
                    switched.append((space, space.shading.type))
                    space.shading.type = 'SOLID'

    def restore_later():
        if not switched:
            return

        def restore():
            for space, shading in switched:
                try:
                    space.shading.type = shading
                except ReferenceError:
                    pass   # the area was closed meanwhile
            return None
        bpy.app.timers.register(restore, first_interval=0.5)
    return restore_later


def remove_datablocks(datablocks):
    """bpy.data.batch_remove, in one place (the tests make it fail to check
    that the viewports are restored whatever happens)."""
    if datablocks:
        bpy.data.batch_remove(datablocks)


def find_duplicate_materials():
    """Groups of local materials that look the same (same images, values,
    links and surface settings), whatever their names."""
    groups = defaultdict(list)
    for material in bpy.data.materials:
        if material.library is not None or getattr(material, 'is_grease_pencil', False):
            continue
        key = material_fingerprint(material)
        if key is not None:
            groups[key].append(material)
    return [group for group in groups.values() if len(group) > 1]


def pick_material_to_keep(group):
    """The copy whose name is closest to the base name: rooftop_01 before
    rooftop_01.001 before rooftop_01.010."""
    return min(group, key=lambda m: (m.name != clean_name(m.name), len(m.name), m.name))


def replace_materials(copies, keep):
    """Point every slot of the file that uses one of `copies` at `keep`."""
    copies = set(copies)
    for collection in (bpy.data.meshes, bpy.data.curves):
        for datablock in collection:
            if datablock.library is not None:
                continue
            for index, material in enumerate(datablock.materials):
                if material in copies:
                    datablock.materials[index] = keep
    for obj in bpy.data.objects:
        if obj.library is not None:
            continue
        for slot in obj.material_slots:
            if slot.link == 'OBJECT' and slot.material in copies:
                slot.material = keep


# ============================================================================
# OPERATORS - MATERIALS
# ============================================================================

class OBJECT_OT_create_material_by_mesh(bpy.types.Operator):
    """Create a unique material for each selected mesh"""
    bl_idname = "kelit_toolkit.create_material_by_mesh"
    bl_label = "Create Material By Mesh"
    bl_options = {'REGISTER', 'UNDO'}

    use_prefix_conversion: bpy.props.BoolProperty(
        name="Convert SM_ to M_",
        description="Replace SM_ prefix with M_ in material name",
        default=True
    )

    def execute(self, context):
        meshes_done = set()
        count = 0

        for obj in context.selected_objects:
            if obj.type != 'MESH':
                continue

            mesh = obj.data
            if mesh.name in meshes_done:
                continue

            meshes_done.add(mesh.name)

            if not mesh.materials or mesh.materials[0] is None:
                continue

            old_mat = mesh.materials[0]

            new_mat_name = mesh.name
            if self.use_prefix_conversion and new_mat_name.startswith("SM_"):
                new_mat_name = "M_" + new_mat_name[3:]
            else:
                new_mat_name = "M_" + new_mat_name

            if new_mat_name in bpy.data.materials:
                new_mat = bpy.data.materials[new_mat_name]
            else:
                new_mat = old_mat.copy()
                new_mat.name = new_mat_name
                count += 1

            mesh.materials.clear()
            mesh.materials.append(new_mat)

        self.report({'INFO'}, f"{count} material(s) created")
        return {'FINISHED'}


class OBJECT_OT_delete_unused_materials(bpy.types.Operator):
    """Delete unused material slots on selected objects"""
    bl_idname = "kelit_toolkit.delete_unused_materials"
    bl_label = "Delete Unused Materials"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        selected_objs = [obj for obj in context.selected_objects if obj.type == 'MESH']

        for obj in selected_objs:
            mesh = obj.data

            used_slots = set()
            for poly in mesh.polygons:
                used_slots.add(poly.material_index)

            if not used_slots or len(used_slots) == len(obj.material_slots):
                continue

            # Rebuild the slot list, then remap polygon indices to the new
            # positions - without this the polygons keep their old indices
            # and end up pointing at the wrong materials
            kept = [i for i in sorted(used_slots) if i < len(obj.material_slots)]
            index_remap = {old: new for new, old in enumerate(kept)}
            new_slots = [obj.material_slots[i].material for i in kept]

            new_indices = [index_remap.get(p.material_index, 0) for p in mesh.polygons]

            mesh.materials.clear()
            for mat in new_slots:
                mesh.materials.append(mat)
            for poly, new_index in zip(mesh.polygons, new_indices):
                poly.material_index = new_index
            mesh.update()

        self.report({'INFO'}, f"Materials cleaned on {len(selected_objs)} object(s)")
        return {'FINISHED'}


class OBJECT_OT_purge_unused_materials(bpy.types.Operator):
    """Delete every material with no user from the .blend file"""
    bl_idname = "kelit_toolkit.purge_unused_materials"
    bl_label = "Purge Unused Materials"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        # List of materials to remove (those with 0 users)
        materials_to_remove = [mat for mat in bpy.data.materials if mat.users == 0]

        # Remove unused materials (viewports in Solid meanwhile, see
        # viewports_to_solid: EEVEE crashed on deleted materials)
        restore_viewports = viewports_to_solid(context)
        try:
            remove_datablocks(materials_to_remove)
        finally:
            restore_viewports()

        removed_count = len(materials_to_remove)
        remaining_count = len(bpy.data.materials)

        if removed_count > 0:
            self.report({'INFO'}, f"{removed_count} material(s) removed - {remaining_count} remaining")
        else:
            self.report({'INFO'}, "No unused materials found")

        return {'FINISHED'}


class OBJECT_OT_merge_duplicate_materials(bpy.types.Operator):
    """Merge materials with identical nodes and images into one, whatever their names. Applies to the whole file; the copies are deleted"""
    bl_idname = "kelit_toolkit.merge_duplicate_materials"
    bl_label = "Merge Duplicate Materials"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        if not find_duplicate_materials():
            self.report({'INFO'}, "No duplicate materials found")
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, _context):
        layout = self.layout
        groups = find_duplicate_materials()
        removed = sum(len(group) - 1 for group in groups)
        layout.label(text=f"{removed} material(s) will be merged into {len(groups)}", icon='INFO')
        box = layout.box()
        max_show = 6
        for group in groups[:max_show]:
            box.label(text=f"  {pick_material_to_keep(group).name}: {len(group) - 1} copies")
        if len(groups) > max_show:
            box.label(text=f"  ... and {len(groups) - max_show} more")
        layout.label(text="Applies to every object of the file. The copies are deleted.")

    def execute(self, _context):
        groups = find_duplicate_materials()
        if not groups:
            self.report({'INFO'}, "No duplicate materials found")
            return {'CANCELLED'}

        restore_viewports = viewports_to_solid(_context)
        merged = 0
        doomed = []
        try:
            for group in groups:
                keep = pick_material_to_keep(group)
                copies = [material for material in group if material != keep]
                replace_materials(copies, keep)
                for copy in copies:
                    copy.user_remap(keep)
                    copy.use_fake_user = False
                    doomed.append(copy)
                    merged += 1
                base = clean_name(keep.name)
                if base != keep.name and base not in bpy.data.materials:
                    keep.name = base
            remove_datablocks(doomed)
        finally:
            restore_viewports()

        self.report({'INFO'}, f"{merged} duplicate material(s) merged into {len(groups)}, "
                              f"{len(bpy.data.materials)} material(s) remaining")
        return {'FINISHED'}


# ============================================================================
# MERGE DUPLICATE IMAGES
# ============================================================================

SKIPPED_IMAGE_TYPES = {'RENDER_RESULT', 'COMPOSITING'}


def _file_md5(path, chunk=1 << 20):
    import hashlib
    digest = hashlib.md5()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(chunk), b''):
            digest.update(block)
    return digest.hexdigest()


def image_memory_bytes(image):
    """Pixel memory of a LOADED image (w x h x channels x bytes per channel);
    0 when its pixels are not in memory (reading .size would load them)."""
    if not image.has_data:
        return 0
    width, height = image.size
    return width * height * max(image.channels, 1) * (4 if image.is_float else 1)


def _has_unsaved_pixels(image):
    try:
        return bool(image.is_dirty)
    except Exception:
        return False


def find_duplicate_images(skipped=None):
    """Groups of images with identical content. Packed images are compared by
    their packed bytes, file images by size then a full-file md5. The colour
    space and alpha mode are part of the key: the same file read as sRGB and
    as Non-Color is two different images. Linked, tiled and render/viewer
    images are left alone. Generated images and images with unsaved painting
    (is_dirty) are never merged: their pixels are not what is on disk. Pass a
    list as `skipped` to collect those."""
    import hashlib
    import os
    candidates = []
    by_size = defaultdict(list)
    for image in bpy.data.images:
        if image.library is not None or image.type in SKIPPED_IMAGE_TYPES:
            continue
        if image.source == 'GENERATED' or _has_unsaved_pixels(image):
            if skipped is not None:
                skipped.append(image)
            continue
        if image.source != 'FILE':
            continue
        settings = (image.colorspace_settings.name, image.alpha_mode)
        if image.packed_file is not None:
            data = bytes(image.packed_file.data)
            candidates.append((('packed', hashlib.md5(data).hexdigest()) + settings, image))
            continue
        path = bpy.path.abspath(image.filepath_raw or image.filepath)
        if not path or not os.path.isfile(path):
            continue
        by_size[os.path.getsize(path)].append((path, settings, image))
    for entries in by_size.values():
        if len(entries) < 2:
            continue   # a size nothing else shares: no need to hash it
        hashes = {}
        for path, settings, image in entries:
            key = os.path.normcase(os.path.abspath(path))
            if key not in hashes:
                try:
                    hashes[key] = _file_md5(path)
                except OSError:
                    continue
            candidates.append((('file', hashes[key]) + settings, image))
    groups = defaultdict(list)
    for key, image in candidates:
        groups[key].append(image)
    return [sorted(group, key=lambda i: i.name) for group in groups.values() if len(group) > 1]


def pick_image_to_keep(group):
    """Same rule as materials: the cleanest, shortest name."""
    return min(group, key=lambda i: (i.name != clean_name(i.name), len(i.name), i.name))


def _group_bytes(group):
    """Memory the copies of a group would free: pixel memory when loaded,
    packed size otherwise."""
    total = 0
    for image in group:
        if image == pick_image_to_keep(group):
            continue
        memory = image_memory_bytes(image)
        if not memory and image.packed_file is not None:
            memory = image.packed_file.size
        total += memory
    return total


def _skipped_note(skipped):
    if not skipped:
        return ""
    return f" ({len(skipped)} generated or unsaved image(s) skipped)"


def _format_bytes(value):
    return f"{value / (1024 ** 3):.2f} GB" if value >= 1024 ** 3 else f"{value / (1024 ** 2):.0f} MB"


class OBJECT_OT_merge_duplicate_images(bpy.types.Operator):
    """Merge images with identical content into one, whatever their names. Applies to the whole file; the copies are deleted, unsaved painting is skipped"""
    bl_idname = "kelit_toolkit.merge_duplicate_images"
    bl_label = "Merge Duplicate Images"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        skipped = []
        self._groups = find_duplicate_images(skipped)
        if not self._groups:
            self.report({'INFO'}, "No duplicate images found" + _skipped_note(skipped))
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, _context):
        layout = self.layout
        groups = getattr(self, '_groups', None) or find_duplicate_images()
        removed = sum(len(group) - 1 for group in groups)
        saved = sum(_group_bytes(group) for group in groups)
        layout.label(text=f"{removed} image(s) will be merged into {len(groups)}", icon='INFO')
        if saved:
            layout.label(text=f"About {_format_bytes(saved)} freed (loaded or packed copies)")
        box = layout.box()
        ranked = sorted(groups, key=_group_bytes, reverse=True)
        for group in ranked[:6]:
            box.label(text=f"  {pick_image_to_keep(group).name}: {len(group) - 1} copies")
        if len(groups) > 6:
            box.label(text=f"  ... and {len(groups) - 6} more")
        skipped = []
        find_duplicate_images(skipped)
        if skipped:
            layout.label(text=f"{len(skipped)} image(s) left alone (generated or unsaved painting)")
        layout.label(text="Applies to the whole file. The copies are deleted.")

    def execute(self, context):
        skipped = []
        groups = find_duplicate_images(skipped)
        if not groups:
            self.report({'INFO'}, "No duplicate images found" + _skipped_note(skipped))
            return {'CANCELLED'}
        saved = sum(_group_bytes(group) for group in groups)
        restore_viewports = viewports_to_solid(context)
        doomed = []
        try:
            for group in groups:
                keep = pick_image_to_keep(group)
                for image in group:
                    if image == keep:
                        continue
                    image.user_remap(keep)
                    image.use_fake_user = False
                    doomed.append(image)
                base = clean_name(keep.name)
                if base != keep.name and base not in bpy.data.images:
                    keep.name = base
            merged = len(doomed)
            remove_datablocks(doomed)
        finally:
            restore_viewports()

        message = f"{merged} duplicate image(s) merged into {len(groups)}"
        if saved:
            message += f", about {_format_bytes(saved)} freed"
        message += _skipped_note(skipped)
        self.report({'INFO'}, message)
        return {'FINISHED'}


classes = (
    OBJECT_OT_create_material_by_mesh,
    OBJECT_OT_delete_unused_materials,
    OBJECT_OT_purge_unused_materials,
    OBJECT_OT_merge_duplicate_materials,
    OBJECT_OT_merge_duplicate_images,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
