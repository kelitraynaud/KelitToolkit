"""Scene Weight Report: what really weighs AT RENDER TIME, not what the
viewport shows. Objects hidden on screen but rendered are counted, objects
the render skips are not. Run on demand (it can take a few seconds on large
scenes); the result opens in the report window."""

import bpy

from .report import open_report, set_report
from ..utils import FolderIndex


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
        self.sources_cache = {}

    def geometry_key(self, obj):
        """What a renderer stores once: the mesh data when nothing modifies
        it (every object sharing it points at one copy), the object itself
        otherwise."""
        if obj.type == 'MESH' and obj.data is not None and not any(
                m.show_render for m in obj.modifiers):
            return obj.data.name_full
        return obj.name_full

    def instanced_sources(self, obj, depth=0):
        """{geometry key: triangles} of every source an instancer points at
        (collection instances, particles in Object or Collection mode),
        each source once however many copies render: 8 145 grass tufts of
        one 17 000-triangle source are 17 000 triangles of memory."""
        key = obj.name_full
        if key in self.sources_cache:
            return self.sources_cache[key]
        self.sources_cache[key] = {}   # guards against recursive instancing
        found = {}
        if depth < 8:
            members = []
            if obj.instance_type == 'COLLECTION' and obj.instance_collection is not None:
                members.extend(obj.instance_collection.all_objects)
            for system in getattr(obj, 'particle_systems', []):
                settings = system.settings
                if settings.render_type == 'OBJECT' and settings.instance_object is not None:
                    members.append(settings.instance_object)
                elif settings.render_type == 'COLLECTION' and settings.instance_collection is not None:
                    members.extend(settings.instance_collection.all_objects)
            for member in members:
                if member.hide_render:
                    continue
                own = self.object_triangles(member)[0]
                if own:
                    found[self.geometry_key(member)] = own
                found.update(self.instanced_sources(member, depth + 1))
        self.sources_cache[key] = found
        return found

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


def header_size(image, max_bytes=1 << 18, folders=None):
    """(width, height, is_float) read from the PNG or JPEG header of an
    image that is not loaded, without decoding it. None when the format is
    another one or the header cannot be read: the caller then loads it.
    An empty tuple when the file is missing. `folders` (a FolderIndex) checks the file exists one folder at a time."""
    if image.source != 'FILE':
        return None
    try:
        if image.packed_file is not None:
            head = bytes(image.packed_file.data[:max_bytes])
        else:
            path = bpy.path.abspath(image.filepath_raw or image.filepath, library=image.library)
            if not path or (folders or FolderIndex()).size(path) is None:
                return ()   # missing: loading it would only search again
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
    folders = FolderIndex()
    for image in bpy.data.images:
        if image.type in {'RENDER_RESULT', 'COMPOSITING'} or image.users < min_users:
            continue
        found = None if image.has_data else header_size(image, folders=folders)
        if found is not None:
            if not found:
                continue   # file missing: nothing to load, nothing to weigh
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


def compute_scene_weight(context, top=20, progress=None):
    """Everything the report shows, as plain data (testable).

    displayed: every triangle the render sees, instances included (what the
    viewport draws and the BVH holds). instanced: the part of it coming from
    collection instances and particles. unique: the geometry really stored,
    each mesh and each instanced source counted once.
    `progress(done, total)` is called while the objects are measured."""
    collections = renderable_collections(context.view_layer)
    counter = WeightCounter(context)
    rows = []
    unique_by_data = {}
    objects = [obj for obj in context.scene.objects if is_rendered(obj, collections)]
    for index, obj in enumerate(objects):
        if progress is not None and index % 50 == 0:
            progress(index, len(objects))
        own, estimated = counter.object_triangles(obj)
        instanced = counter.instanced_triangles(obj)
        total = own + instanced
        if total <= 0:
            continue
        collection = obj.users_collection[0].name if obj.users_collection else ''
        rows.append({'name': obj.name, 'collection': collection, 'triangles': total,
                     'instanced': instanced, 'estimated': estimated,
                     'data': obj.data.name_full if obj.data else None})
        if own:
            # same mesh data, no modifier: a renderer loads it only once
            unique_by_data[counter.geometry_key(obj)] = own
        if instanced:
            unique_by_data.update(counter.instanced_sources(obj))
    rows.sort(key=lambda row: row['triangles'], reverse=True)
    displayed = sum(row['triangles'] for row in rows)
    instanced_total = sum(row['instanced'] for row in rows)
    unique = sum(unique_by_data.values())

    shared = {}
    for row in rows:
        if row['data']:
            shared.setdefault(row['data'], []).append(row)
    shared_rows = sorted(
        ((data, members) for data, members in shared.items() if len(members) > 1),
        key=lambda item: sum(r['triangles'] for r in item[1]), reverse=True)

    return {'heavy_subdivisions': heavy_subdivisions(objects),
            'rows': rows, 'top': rows[:top], 'displayed': displayed, 'unique': unique,
            'instanced': instanced_total,
            'shared': shared_rows, 'textures': texture_weights(),
            'evaluated': counter.evaluated, 'rendered_objects': len(rows)}


def _mtris(value):
    return f"{value / 1e6:.2f} M" if value >= 1e5 else f"{value / 1e3:.1f} k"


def _mem(value):
    return f"{value / (1024 ** 3):.2f} GB" if value >= 1024 ** 3 else f"{value / (1024 ** 2):.0f} MB"


class OBJECT_OT_scene_weight_report(bpy.types.Operator):
    """List what weighs at render time: triangles per object, shared meshes, textures by memory, heavy subdivisions. Reads only, changes nothing"""
    bl_idname = "kelit_toolkit.scene_weight_report"
    bl_label = "Scene Weight Report"
    bl_options = {'REGISTER'}

    top: bpy.props.IntProperty(name="Objects Listed", default=20, min=5, max=200)

    def execute(self, context):
        import time
        start = time.time()
        wm = context.window_manager
        wm.progress_begin(0, 100)
        try:
            data = compute_scene_weight(
                context, self.top,
                progress=lambda done, total: wm.progress_update(int(90 * done / max(total, 1))))
        finally:
            wm.progress_end()
        elapsed = time.time() - start
        displayed = max(data['displayed'], 1)

        lines = [('INFO', f"Rendered: {_mtris(data['displayed'])}tris on "
                          f"{data['rendered_objects']} object(s): "
                          f"{_mtris(data['displayed'] - data['instanced'])}tris of objects + "
                          f"{_mtris(data['instanced'])}tris of instances (viewport / BVH cost)"),
                 ('INFO', f"Unique geometry in memory: {_mtris(data['unique'])}tris "
                          "(shared meshes and instanced sources counted once)")]
        for row in data['top']:
            flag = '' if row['estimated'] else ' (evaluated)'
            if row['instanced']:
                flag += f" ({_mtris(row['instanced'])}tris instanced)"
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
                              f"({_mtris(data['instanced'])}tris instanced, "
                              f"{_mtris(data['unique'])}tris unique), "
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
