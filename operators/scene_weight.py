"""Scene Weight Report: what really weighs AT RENDER TIME, not what the
viewport shows. Objects hidden on screen but rendered are counted, objects
the render skips are not. Run on demand (it can take a few seconds on large
scenes); the result opens in the report window."""

import bpy

from .report import open_report, set_report


def renderable_collections(view_layer):
    """Collections that render: not excluded, not hidden from render, along
    the whole path from the scene collection."""
    result = set()

    def walk(layer_collection, parent_ok):
        collection = layer_collection.collection
        ok = parent_ok and not layer_collection.exclude and not collection.hide_render
        if ok:
            result.add(collection.name_full)
        for child in layer_collection.children:
            walk(child, ok)

    walk(view_layer.layer_collection, True)
    return result


def is_rendered(obj, collections):
    if obj.hide_render:
        return False
    return any(c.name_full in collections for c in obj.users_collection)


def mesh_counts(mesh):
    """(triangles, face corners) of a mesh: an n-gon is n - 2 triangles and
    n corners (sum of loop_total)."""
    import numpy as np
    count = len(mesh.polygons)
    if not count:
        return 0, 0
    totals = np.empty(count, dtype=np.int32)
    mesh.polygons.foreach_get('loop_total', totals)
    corners = int(totals.sum())
    return corners - 2 * count, corners


def subdivided_counts(triangles, corners, levels):
    """(triangles, corners) after `levels` Catmull-Clark levels. The first
    level turns every n-gon into n quads (a triangle into 3 quads, 6
    triangles; a quad into 4 quads, 8 triangles): as many quads as face
    corners. Each further level multiplies the quads by 4."""
    if levels <= 0:
        return triangles, corners
    quads = corners * 4 ** (levels - 1)
    return 2 * quads, 4 * quads


def subdivided_faces(mesh, levels):
    """Faces after `levels` Catmull-Clark levels (quads only)."""
    if levels <= 0:
        return len(mesh.polygons)
    return int(mesh_counts(mesh)[1] * 4 ** (levels - 1))


class WeightCounter:
    """Triangle counts at render settings, cached per mesh / collection."""

    def __init__(self, context):
        self.context = context
        self.depsgraph = context.evaluated_depsgraph_get()
        self.mesh_cache = {}
        self.collection_cache = {}
        self.evaluated = 0

    def base_counts(self, mesh):
        """(triangles, face corners) of the mesh as stored."""
        key = mesh.name_full
        if key not in self.mesh_cache:
            self.mesh_cache[key] = mesh_counts(mesh)
        return self.mesh_cache[key]

    def base_triangles(self, mesh):
        return self.base_counts(mesh)[0]

    def evaluated_triangles(self, obj):
        """Real evaluation, for what cannot be estimated (Geometry Nodes)."""
        self.evaluated += 1
        evaluated = obj.evaluated_get(self.depsgraph)
        try:
            mesh = evaluated.to_mesh()
        except RuntimeError:
            return 0
        try:
            if mesh is None:
                return 0
            mesh.calc_loop_triangles()
            return len(mesh.loop_triangles)
        finally:
            evaluated.to_mesh_clear()

    def object_triangles(self, obj):
        """(triangles, estimated) for one object at render settings."""
        if obj.type != 'MESH' or obj.data is None:
            if obj.type in {'CURVE', 'FONT', 'SURFACE', 'META'}:
                return self.evaluated_triangles(obj), False
            return 0, True
        modifiers = [m for m in obj.modifiers if m.show_render]
        if any(m.type == 'NODES' for m in modifiers):
            return self.evaluated_triangles(obj), False
        triangles, corners = (float(value) for value in self.base_counts(obj.data))
        for modifier in modifiers:
            if modifier.type in {'SUBSURF', 'MULTIRES'}:
                triangles, corners = subdivided_counts(triangles, corners, modifier.render_levels)
            elif modifier.type == 'ARRAY':
                if modifier.fit_type != 'FIXED_COUNT':
                    return self.evaluated_triangles(obj), False
                triangles *= modifier.count
                corners *= modifier.count
            elif modifier.type == 'MIRROR':
                factor = 2 ** sum(bool(axis) for axis in modifier.use_axis)
                triangles *= factor
                corners *= factor
            elif modifier.type == 'SOLIDIFY':
                triangles *= 2
                corners *= 2
            elif modifier.type == 'DECIMATE' and modifier.decimate_type == 'COLLAPSE':
                triangles *= modifier.ratio
                corners = triangles * 3     # Collapse leaves triangles
            elif modifier.type in {'SCREW', 'SKIN', 'REMESH', 'BOOLEAN', 'WELD', 'WIREFRAME'}:
                return self.evaluated_triangles(obj), False
        return int(round(triangles)), True

    def collection_triangles(self, collection, depth=0):
        key = collection.name_full
        if key in self.collection_cache:
            return self.collection_cache[key]
        self.collection_cache[key] = 0   # guards against recursive instancing
        total = 0
        if depth < 8:
            for obj in collection.all_objects:
                if obj.hide_render:
                    continue
                total += self.object_triangles(obj)[0] + self.instanced_triangles(obj, depth + 1)
        self.collection_cache[key] = total
        return total

    def instanced_triangles(self, obj, depth=0):
        """Collection instances and particle instances carried by `obj`."""
        total = 0
        if obj.instance_type == 'COLLECTION' and obj.instance_collection is not None:
            total += self.collection_triangles(obj.instance_collection, depth)
        for system in getattr(obj, 'particle_systems', []):
            settings = system.settings
            if settings.render_type == 'OBJECT' and settings.instance_object is not None:
                per_instance = (self.object_triangles(settings.instance_object)[0]
                                + self.instanced_triangles(settings.instance_object, depth + 1))
            elif settings.render_type == 'COLLECTION' and settings.instance_collection is not None:
                members = [o for o in settings.instance_collection.all_objects if not o.hide_render]
                if not members:
                    continue
                if settings.use_whole_collection:
                    per_instance = self.collection_triangles(settings.instance_collection, depth + 1)
                else:
                    per_instance = sum(self.object_triangles(o)[0] for o in members) / len(members)
            else:
                continue
            count = settings.count
            if settings.child_type != 'NONE':
                count *= max(settings.rendered_child_count, 1)
            total += int(count * per_instance)
        return total


def heavy_subdivisions(objects, max_faces=50000, max_size=1.0):
    """(name, base faces, faces at render, size in m) for small objects whose
    render subdivision produces too many faces (a 1276-face ball at level 3
    renders 80k faces). Alert only, nothing is changed."""
    found = []
    for obj in objects:
        if obj.type != 'MESH' or obj.data is None or obj.hide_render:
            continue
        levels = sum(m.render_levels for m in obj.modifiers
                     if m.show_render and m.type in {'SUBSURF', 'MULTIRES'})
        if not levels:
            continue
        base = len(obj.data.polygons)
        evaluated = subdivided_faces(obj.data, levels)
        size = max(obj.dimensions) if obj.dimensions else 0.0
        if evaluated > max_faces and size < max_size:
            found.append((obj.name, base, evaluated, size))
    return sorted(found, key=lambda row: row[2], reverse=True)


def _png_header(head):
    """(width, height, is_float) from the IHDR chunk of a PNG. Blender
    loads 16-bit PNGs into a float buffer."""
    import struct
    if len(head) < 26 or head[:8] != b'\x89PNG\r\n\x1a\n' or head[12:16] != b'IHDR':
        return None
    width, height = struct.unpack('>II', head[16:24])
    return width, height, head[24] > 8


def _jpeg_header(head):
    """(width, height, False) from the first SOF marker of a JPEG."""
    import struct
    if head[:2] != b'\xff\xd8':
        return None
    index = 2
    while index + 9 < len(head):
        if head[index] != 0xFF:
            index += 1
            continue
        marker = head[index + 1]
        if marker == 0xFF:
            index += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        length = struct.unpack('>H', head[index + 2:index + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height, width = struct.unpack('>HH', head[index + 5:index + 9])
            return width, height, False
        index += 2 + length
    return None


def header_size(image, max_bytes=1 << 18):
    """(width, height, is_float) read from the PNG or JPEG header of an
    image that is not loaded, without decoding it. None when the format is
    another one or the header cannot be read: the caller then loads it."""
    import os
    if image.source != 'FILE':
        return None
    try:
        if image.packed_file is not None:
            head = bytes(image.packed_file.data[:max_bytes])
        else:
            path = bpy.path.abspath(image.filepath_raw or image.filepath, library=image.library)
            if not path or not os.path.isfile(path):
                return None
            with open(path, 'rb') as handle:
                head = handle.read(max_bytes)
    except Exception:
        return None
    found = _png_header(head) or _jpeg_header(head)
    if found is None or not found[0] or not found[1]:
        return None
    return found


def texture_weights(min_users=1):
    """(name, bytes, width, height, users) of every used image, heaviest first.
    Images not loaded yet are measured from their PNG / JPEG header (memory
    as Blender loads them: 4 channels, 1 byte or a float each); other
    formats have to be loaded to read their size: the slow part of the
    report."""
    rows = []
    for image in bpy.data.images:
        if image.type in {'RENDER_RESULT', 'COMPOSITING'} or image.users < min_users:
            continue
        found = None if image.has_data else header_size(image)
        if found is not None:
            width, height, is_float = found
            memory = width * height * 4 * (4 if is_float else 1)
            rows.append((image.name, memory, width, height, image.users))
            continue
        try:
            width, height = image.size
        except Exception:
            continue
        if not width or not height:
            continue
        memory = width * height * max(image.channels, 1) * (4 if image.is_float else 1)
        rows.append((image.name, memory, width, height, image.users))
    return sorted(rows, key=lambda row: row[1], reverse=True)


def compute_scene_weight(context, top=20):
    """Everything the report shows, as plain data (testable)."""
    collections = renderable_collections(context.view_layer)
    counter = WeightCounter(context)
    rows = []
    unique_by_data = {}
    for obj in context.scene.objects:
        if not is_rendered(obj, collections):
            continue
        own, estimated = counter.object_triangles(obj)
        instanced = counter.instanced_triangles(obj)
        total = own + instanced
        if total <= 0:
            continue
        collection = obj.users_collection[0].name if obj.users_collection else ''
        rows.append({'name': obj.name, 'collection': collection, 'triangles': total,
                     'estimated': estimated, 'data': obj.data.name_full if obj.data else None})
        if obj.type == 'MESH' and obj.data is not None and not any(
                m.show_render for m in obj.modifiers):
            # same mesh data, no modifier: a renderer loads it only once
            unique_by_data[obj.data.name_full] = own
            unique_by_data[obj.name_full + '#inst'] = instanced
        else:
            unique_by_data[obj.name_full] = total
    rows.sort(key=lambda row: row['triangles'], reverse=True)
    displayed = sum(row['triangles'] for row in rows)
    unique = sum(unique_by_data.values())

    shared = {}
    for row in rows:
        if row['data']:
            shared.setdefault(row['data'], []).append(row)
    shared_rows = sorted(
        ((data, members) for data, members in shared.items() if len(members) > 1),
        key=lambda item: sum(r['triangles'] for r in item[1]), reverse=True)

    rendered = [obj for obj in context.scene.objects if is_rendered(obj, collections)]
    return {'heavy_subdivisions': heavy_subdivisions(rendered),
            'rows': rows, 'top': rows[:top], 'displayed': displayed, 'unique': unique,
            'shared': shared_rows, 'textures': texture_weights(),
            'evaluated': counter.evaluated, 'rendered_objects': len(rows)}


def _mtris(value):
    return f"{value / 1e6:.2f} M" if value >= 1e5 else f"{value / 1e3:.1f} k"


def _mem(value):
    return f"{value / (1024 ** 3):.2f} GB" if value >= 1024 ** 3 else f"{value / (1024 ** 2):.0f} MB"


class OBJECT_OT_scene_weight_report(bpy.types.Operator):
    """List what weighs at RENDER time: rendered objects by triangle count
    (hidden in the viewport but rendered included, render-disabled skipped),
    modifiers, collection instances and particles counted, shared meshes
    flagged, and textures by memory. PNG and JPEG sizes are read from the
    file header; images in other formats (EXR, TIFF, TGA...) are loaded to
    read their size, which can take a while. Click a line to select its
    object"""
    bl_idname = "kelit_toolkit.scene_weight_report"
    bl_label = "Scene Weight Report"
    bl_options = {'REGISTER'}

    top: bpy.props.IntProperty(name="Objects Listed", default=20, min=5, max=200)

    def execute(self, context):
        import time
        start = time.time()
        data = compute_scene_weight(context, self.top)
        elapsed = time.time() - start
        displayed = max(data['displayed'], 1)

        lines = [('INFO', f"Rendered: {_mtris(data['displayed'])}tris on "
                          f"{data['rendered_objects']} object(s), {_mtris(data['unique'])}tris "
                          "unique (shared meshes loaded once)")]
        for row in data['top']:
            flag = '' if row['estimated'] else ' (evaluated)'
            lines.append(('WARNING' if row['triangles'] / displayed > 0.05 else 'INFO',
                          f"{row['name']}  [{row['collection']}]  {_mtris(row['triangles'])}tris  "
                          f"{100.0 * row['triangles'] / displayed:.1f} %{flag}", row['name']))
        for data_name, members in data['shared'][:10]:
            shown = sum(r['triangles'] for r in members)
            lines.append(('INFO', f"Shared mesh {data_name}: {len(members)} objects, "
                                  f"{_mtris(shown)}tris shown, {_mtris(members[0]['triangles'])}"
                                  "tris real", members[0]['name']))
        for name, base, evaluated, size in data['heavy_subdivisions'][:15]:
            lines.append(('WARNING', f"Heavy subdivision on {name}: {base:,} to {evaluated:,} faces "
                                     f"at render for a {size:.2f} m object", name))
        texture_total = sum(row[1] for row in data['textures'])
        lines.append(('INFO', f"Textures: {_mem(texture_total)} for {len(data['textures'])} image(s)"))
        for name, memory, width, height, users in data['textures'][:20]:
            big = max(width, height) >= 8192
            lines.append(('WARNING' if big else 'INFO',
                          f"{name}  {width}x{height}  {_mem(memory)}  {users} user(s)"
                          + ("  8K or more" if big else "")))

        set_report(context, "Scene Weight", lines)
        open_report(context)
        self.report({'INFO'}, f"{_mtris(data['displayed'])}tris rendered "
                              f"({_mtris(data['unique'])}tris unique), "
                              f"{_mem(texture_total)} of textures, {elapsed:.1f} s")
        return {'FINISHED'}


classes = (
    OBJECT_OT_scene_weight_report,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
