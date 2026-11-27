import bpy
from collections import defaultdict
from ..utils import FolderIndex, clean_name, is_animated, material_fingerprint


class Progress:
    """wm.progress_begin / update / end around a long scan, safe without a
    window (background, tests)."""

    def __init__(self, context, total):
        self.wm = getattr(context, 'window_manager', None) if context else None
        self.total = max(total, 1)
        if self.wm is not None:
            self.wm.progress_begin(0, 100)

    def update(self, done):
        if self.wm is not None and done % 25 == 0:
            self.wm.progress_update(int(100 * done / self.total))

    def end(self):
        if self.wm is not None:
            self.wm.progress_end()
            self.wm = None


# A scan done for a dialog (invoke, then every redraw of draw) is reused by
# draw and execute: recomputing 1 200 material fingerprints or hashing
# network files on each redraw froze Blender for minutes. Only for a few
# minutes, and only while the same datablocks are there.
_SCANS = {}
SCAN_LIFETIME = 300.0


def _scan_signature(collection):
    return (len(collection), hash(tuple(d.name_full for d in collection)))


def remember_scan(kind, collection, value):
    import time
    _SCANS[kind] = (time.time(), _scan_signature(collection), value)


def recall_scan(kind, collection, consume=False):
    import time
    entry = _SCANS.pop(kind, None) if consume else _SCANS.get(kind)
    if entry is None:
        return None
    stamp, signature, value = entry
    if time.time() - stamp > SCAN_LIFETIME or signature != _scan_signature(collection):
        _SCANS.pop(kind, None)
        return None
    return value


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


# ----------------------------------------------------------------------------
# References a merge must not rewrite. user_remap rewrites EVERY pointer to a
# copy: material slots, but also a rig's custom properties (a Character DNA
# empty listing 7 601 driver owners) or another datablock's drivers. Those
# copies stay, and the report says why.
# ----------------------------------------------------------------------------

def _holds(value, target, depth=0):
    """True when an ID property value (group, list, nested) points at `target`."""
    if isinstance(value, bpy.types.ID):
        return value == target
    if depth > 8 or value is None or isinstance(value, (str, bytes, int, float, bool)):
        return False
    if hasattr(value, 'keys') and hasattr(value, 'get'):
        return any(_holds(value.get(key), target, depth + 1) for key in value.keys())
    if isinstance(value, (list, tuple)) or type(value).__name__ == 'IDPropertyGroupViewItems':
        return any(_holds(item, target, depth + 1) for item in value)
    try:
        items = list(value)
    except TypeError:
        return False
    if items and not isinstance(items[0], (int, float, bool)):
        return any(_holds(item, target, depth + 1) for item in items)
    return False


def _custom_property_holds(id_block, target):
    try:
        keys = list(id_block.keys())
    except Exception:
        return False
    return any(_holds(id_block.get(key), target) for key in keys)


def _driver_reads(id_block, target):
    anim = getattr(id_block, 'animation_data', None)
    if anim is None:
        return False
    return any(t.id == target for fcurve in anim.drivers
               for variable in fcurve.driver.variables for t in variable.targets)


def foreign_references(datablocks, plain_users, slot_check=None):
    """{name: reason} for the datablocks something points at otherwise than
    through plain use: a custom property, a driver of another datablock, a
    user of a type not in `plain_users`, an object that holds it outside its
    material slots (`slot_check(object, datablock)` says whether it is a
    plain use)."""
    found = {}
    if not datablocks:
        return found
    try:
        users = bpy.data.user_map(subset=set(datablocks))
    except Exception:
        return {db.name: "users could not be checked" for db in datablocks}
    for datablock, owners in users.items():
        for owner in owners:
            if owner == datablock:
                continue
            label = f"{type(owner).__name__} '{owner.name}'"
            reason = None
            if _custom_property_holds(owner, datablock):
                reason = f"custom property of {label}"
            elif _driver_reads(owner, datablock):
                reason = f"driver of {label}"
            elif not isinstance(owner, plain_users):
                reason = f"used by {label}"
            elif (slot_check is not None and isinstance(owner, bpy.types.Object)
                  and not slot_check(owner, datablock)):
                reason = f"referenced by {label} outside its material slots"
            if reason:
                found[datablock.name] = reason
                break
    return found


PLAIN_MATERIAL_USERS = (bpy.types.Mesh, bpy.types.Curve, bpy.types.MetaBall, bpy.types.Object)
PLAIN_IMAGE_USERS = (bpy.types.Material, bpy.types.NodeTree, bpy.types.World, bpy.types.Texture,
                     bpy.types.Light, bpy.types.Brush, bpy.types.Object, bpy.types.Camera,
                     bpy.types.Screen, bpy.types.Scene, bpy.types.WindowManager)


def _in_slots(obj, material):
    return any(slot.material == material for slot in obj.material_slots)


def _split_blocked(groups, pick, plain_users, slot_check=None):
    """Removes from each group the copies something else points at.
    Returns (groups still worth merging, {name: reason})."""
    copies = [d for group in groups for d in group if d != pick(group)]
    blocked = foreign_references(copies, plain_users, slot_check)
    kept = []
    for group in groups:
        keep = pick(group)
        remaining = [d for d in group if d == keep or d.name not in blocked]
        if len(remaining) > 1:
            kept.append(remaining)
    return kept, blocked


def find_duplicate_materials(context=None, include_animated=False, skipped=None):
    """Groups of local materials that look the same (same images, values,
    links, surface settings, drivers and keys), whatever their names. Node
    groups shared by many materials are fingerprinted once. Materials with
    drivers or keys are left out unless `include_animated` (a list as
    `skipped` collects them). With a context, the progress shows in the
    status bar."""
    groups = defaultdict(list)
    shared = {'trees': {}, 'stack': set(), 'structs': set()}
    materials = list(bpy.data.materials)
    progress = Progress(context, len(materials))
    try:
        for index, material in enumerate(materials):
            progress.update(index)
            if material.library is not None or getattr(material, 'is_grease_pencil', False):
                continue
            if not include_animated and (is_animated(material)
                                         or is_animated(getattr(material, 'node_tree', None))):
                if skipped is not None:
                    skipped.append(material)
                continue
            key = material_fingerprint(material, shared)
            if key is not None:
                groups[key].append(material)
    finally:
        progress.end()
    return [group for group in groups.values() if len(group) > 1]


def safe_material_groups(context=None, include_animated=False):
    """(groups to merge, animated materials left out, {copy: why it stays})."""
    animated = []
    groups = find_duplicate_materials(context, include_animated, animated)
    groups, blocked = _split_blocked(groups, pick_material_to_keep, PLAIN_MATERIAL_USERS,
                                     _in_slots)
    return groups, animated, blocked


def _scanned_material_groups(context, include_animated=False, consume=False):
    """(groups, animated, blocked) found by the dialog, or a fresh scan."""
    kind = f'materials:{include_animated}'
    found = recall_scan(kind, bpy.data.materials, consume)
    if found is not None:
        names, animated_names, blocked = found
        groups = [[bpy.data.materials.get(n) for n in group] for group in names]
        if all(m is not None for group in groups for m in group):
            return groups, [bpy.data.materials.get(n) for n in animated_names], blocked
    groups, animated, blocked = safe_material_groups(context, include_animated)
    if not consume:
        remember_scan(kind, bpy.data.materials,
                      ([[m.name for m in g] for g in groups], [m.name for m in animated], blocked))
    return groups, animated, blocked


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

    include_animated: bpy.props.BoolProperty(
        name="Include Animated/Driven Materials",
        description="Also merge materials that carry drivers or keys. Off by default: "
                    "a rig's shaders are driven by THAT rig, merging them hands one "
                    "character's face to another rig",
        default=False,
    )

    def invoke(self, context, event):
        for kind in [k for k in _SCANS if k.startswith('materials')]:
            _SCANS.pop(kind, None)
        groups, animated, blocked = _scanned_material_groups(context, self.include_animated)
        if not groups and not animated and not blocked:
            self.report({'INFO'}, "No duplicate materials found")
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, context):
        layout = self.layout
        groups, animated, blocked = _scanned_material_groups(context, self.include_animated)
        layout.prop(self, "include_animated")
        if animated and not self.include_animated:
            layout.label(text=f"{len(animated)} animated or driven material(s) left out",
                         icon='DRIVER')
        if blocked:
            layout.label(text=f"{len(blocked)} copy(ies) kept: referenced by a custom "
                              "property or a driver", icon='LOCKED')
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
        from .report import set_report
        groups, animated, blocked = _scanned_material_groups(
            _context, self.include_animated, consume=True)
        lines = [('WARNING', f"Kept {name}: {reason}") for name, reason in sorted(blocked.items())]
        if animated and not self.include_animated:
            lines += [('INFO', f"Left out {m.name}: drivers or keys") for m in animated]
        if lines:
            set_report(_context, "Merge Duplicate Materials", lines)
        if not groups:
            self.report({'INFO'}, "No duplicate materials found" + (
                f" ({len(blocked)} referenced copy(ies) kept, see Show Last Report)"
                if blocked else ""))
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

        message = (f"{merged} duplicate material(s) merged into {len(groups)}, "
                   f"{len(bpy.data.materials)} material(s) remaining")
        if blocked or (animated and not self.include_animated):
            message += (f"; {len(blocked)} referenced copy(ies) kept, {len(animated)} "
                        "animated left out (see Show Last Report)")
        self.report({'INFO'}, message)
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


def find_duplicate_images(skipped=None, context=None):
    """Groups of images with identical content. Packed images are compared by
    their packed bytes, file images by size then a full-file md5. The colour
    space and alpha mode are part of the key: the same file read as sRGB and
    as Non-Color is two different images. Linked, tiled and render/viewer
    images are left alone. Generated images and images with unsaved painting
    (is_dirty) are never merged: their pixels are not what is on disk. Pass a
    list as `skipped` to collect those. File existence and sizes are read one
    folder at a time (network drives); with a context, the progress shows
    in the status bar."""
    import os
    candidates = []
    by_size = defaultdict(list)
    images = list(bpy.data.images)
    progress = Progress(context, len(images))
    try:
        _sort_images(images, skipped, candidates, by_size, FolderIndex(), progress)
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
    finally:
        progress.end()
    groups = defaultdict(list)
    for key, image in candidates:
        groups[key].append(image)
    return [sorted(group, key=lambda i: i.name) for group in groups.values() if len(group) > 1]


def _sort_images(images, skipped, candidates, by_size, folders, progress):
    """First pass: packed images hashed, file images sorted by size."""
    import hashlib
    for index, image in enumerate(images):
        progress.update(index)
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
        size = folders.size(path) if path else None
        if size is None:
            continue
        by_size[size].append((path, settings, image))


def safe_image_groups(context=None):
    """(groups to merge, skipped generated/unsaved images, {copy: why it stays})."""
    skipped = []
    groups = find_duplicate_images(skipped, context)
    groups, blocked = _split_blocked(groups, pick_image_to_keep, PLAIN_IMAGE_USERS)
    return groups, skipped, blocked


def _scanned_image_groups(context, consume=False):
    """(groups, skipped, blocked) found by the dialog, or a fresh scan."""
    found = recall_scan('images', bpy.data.images, consume)
    if found is not None:
        names, skipped_names, blocked = found
        groups = [[bpy.data.images.get(n) for n in group] for group in names]
        skipped = [bpy.data.images.get(n) for n in skipped_names]
        if all(i is not None for group in groups for i in group) and all(skipped):
            return groups, skipped, blocked
    groups, skipped, blocked = safe_image_groups(context)
    if not consume:
        remember_scan('images', bpy.data.images,
                      ([[i.name for i in g] for g in groups], [i.name for i in skipped], blocked))
    return groups, skipped, blocked


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


def _blocked_note(blocked):
    if not blocked:
        return ""
    return f" ({len(blocked)} referenced copy(ies) kept, see Show Last Report)"


def _format_bytes(value):
    return f"{value / (1024 ** 3):.2f} GB" if value >= 1024 ** 3 else f"{value / (1024 ** 2):.0f} MB"


class OBJECT_OT_merge_duplicate_images(bpy.types.Operator):
    """Merge images with identical content into one, whatever their names. Applies to the whole file; the copies are deleted, unsaved painting is skipped"""
    bl_idname = "kelit_toolkit.merge_duplicate_images"
    bl_label = "Merge Duplicate Images"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        _SCANS.pop('images', None)
        groups, skipped, blocked = _scanned_image_groups(context)
        if not groups:
            self.report({'INFO'}, "No duplicate images found" + _skipped_note(skipped)
                        + _blocked_note(blocked))
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, context):
        layout = self.layout
        groups, skipped, blocked = _scanned_image_groups(context)
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
        if blocked:
            layout.label(text=f"{len(blocked)} copy(ies) kept: referenced by a custom property "
                              "or a driver", icon='LOCKED')
        if skipped:
            layout.label(text=f"{len(skipped)} image(s) left alone (generated or unsaved painting)")
        layout.label(text="Applies to the whole file. The copies are deleted.")

    def execute(self, context):
        from .report import set_report
        groups, skipped, blocked = _scanned_image_groups(context, consume=True)
        if blocked:
            set_report(context, "Merge Duplicate Images",
                       [('WARNING', f"Kept {name}: {reason}")
                        for name, reason in sorted(blocked.items())])
        if not groups:
            self.report({'INFO'}, "No duplicate images found" + _skipped_note(skipped)
                        + _blocked_note(blocked))
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
        message += _skipped_note(skipped) + _blocked_note(blocked)
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
