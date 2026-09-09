"""Two tools for meshes that came out of a DCC export the wrong way round:

- Rejoin Material Splits: objects cut into one object per material
  (SM_Box074Material260, SM_Box074Material310 ...) become one object with
  several material slots again.
- Split Repeated Parts: a mesh that holds several copies of the same
  element (16 air-conditioning boxes in one mesh) gives those copies back as
  instances of one shared mesh. Pieces that touch each other stay together
  (a balcony's bars and frame are one unit), unique pieces stay in place.
"""

import re
from collections import defaultdict

import bmesh
import bpy
import mathutils

from ..utils import clean_name, mesh_users


# ============================================================================
# REJOIN MATERIAL SPLITS
# ============================================================================

SPLIT_PATTERN = re.compile(r'^(?P<base>.+?)Material[A-Za-z0-9]*$')


def split_groups(objects):
    """Objects whose names share a base before a trailing 'Material...'
    token, grouped by base (Blender suffixes ignored). Only groups whose
    members are single-user meshes with the same parent are returned."""
    groups = defaultdict(list)
    for obj in objects:
        if obj.type != 'MESH' or obj.data is None or obj.library is not None:
            continue
        match = SPLIT_PATTERN.match(clean_name(obj.name))
        if match:
            groups[match.group('base')].append(obj)
    result = []
    for base, members in groups.items():
        if len(members) < 2:
            continue
        if any(len(mesh_users(obj.data)) > 1 for obj in members):
            continue
        if len({obj.parent.name if obj.parent else '' for obj in members}) > 1:
            continue
        result.append((base, sorted(members, key=lambda o: o.name)))
    return sorted(result, key=lambda item: item[0])


class OBJECT_OT_rejoin_material_splits(bpy.types.Operator):
    """Join back objects that an export cut into one object per material
    (SM_Box074Material260 + SM_Box074Material310 become SM_Box074 with two
    material slots). Groups are found from the names; members that share
    their mesh with other objects, or sit under different parents, are left
    alone"""
    bl_idname = "kelit_toolkit.rejoin_material_splits"
    bl_label = "Rejoin Material Splits"
    bl_options = {'REGISTER', 'UNDO'}

    search_scope: bpy.props.EnumProperty(
        name="Search Scope",
        items=[
            ('SELECTED', "Selected Only", "Only the selected objects"),
            ('SCENE', "Entire Scene", "Every mesh object of the scene"),
        ],
        default='SELECTED'
    )

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def _objects(self, context):
        pool = context.selected_objects if self.search_scope == 'SELECTED' else context.scene.objects
        return [obj for obj in pool if obj.visible_get()]

    def invoke(self, context, event):
        if not split_groups(self._objects(context)):
            self.report({'INFO'}, "No object split by material found (names ending in Material...)")
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=440)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "search_scope")
        groups = split_groups(self._objects(context))
        joined = sum(len(members) for _base, members in groups)
        layout.label(text=f"{joined} object(s) will become {len(groups)}", icon='INFO')
        box = layout.box()
        for base, members in groups[:6]:
            box.label(text=f"  {base}: {len(members)} parts")
        if len(groups) > 6:
            box.label(text=f"  ... and {len(groups) - 6} more")

    def execute(self, context):
        groups = split_groups(self._objects(context))
        if not groups:
            self.report({'INFO'}, "No object split by material found")
            return {'CANCELLED'}
        previous_selection = [obj.name for obj in context.selected_objects]
        joined = 0
        for base, members in groups:
            for obj in context.view_layer.objects:
                obj.select_set(False)
            for obj in members:
                obj.select_set(True)
            target = members[0]
            context.view_layer.objects.active = target
            try:
                bpy.ops.object.join()
            except RuntimeError as error:
                self.report({'WARNING'}, f"{base}: join failed ({str(error)[:80]})")
                continue
            target.name = base
            target.data.name = base
            joined += len(members) - 1
        for obj in context.view_layer.objects:
            obj.select_set(obj.name in previous_selection)
        self.report({'INFO'}, f"{joined} part(s) joined into {len(groups)} object(s)")
        return {'FINISHED'}


# ============================================================================
# SPLIT REPEATED PARTS
# ============================================================================

def loose_parts(bm):
    """Connected components of a bmesh, as lists of BMVert."""
    seen = set()
    parts = []
    for start in bm.verts:
        if start.index in seen:
            continue
        seen.add(start.index)
        stack = [start]
        part = []
        while stack:
            vertex = stack.pop()
            part.append(vertex)
            for edge in vertex.link_edges:
                other = edge.other_vert(vertex)
                if other.index not in seen:
                    seen.add(other.index)
                    stack.append(other)
        parts.append(part)
    return parts


def cluster_parts(parts, tolerance):
    """Group loose parts whose bounding boxes touch (expanded by `tolerance`)
    into units. A sweep on X keeps it fast on meshes with thousands of parts."""
    boxes = []
    for index, part in enumerate(parts):
        xs = [v.co.x for v in part]
        ys = [v.co.y for v in part]
        zs = [v.co.z for v in part]
        boxes.append((min(xs) - tolerance, max(xs) + tolerance, min(ys) - tolerance,
                      max(ys) + tolerance, min(zs) - tolerance, max(zs) + tolerance, index))
    parent = list(range(len(parts)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    boxes.sort()
    active = []
    for box in boxes:
        active = [other for other in active if other[1] >= box[0]]
        for other in active:
            if (box[2] <= other[3] and other[2] <= box[3]
                    and box[4] <= other[5] and other[4] <= box[5]):
                union(box[6], other[6])
        active.append(box)
    clusters = defaultdict(list)
    for index, part in enumerate(parts):
        clusters[find(index)].extend(part)
    return list(clusters.values())


def cluster_signature(bm, verts):
    """Shape + materials of a unit, independent of where it sits."""
    centroid = sum((v.co for v in verts), mathutils.Vector()) / len(verts)
    coords = tuple(sorted(
        tuple(round(component, 4) for component in (v.co - centroid)) for v in verts))
    vert_set = set(v.index for v in verts)
    materials = tuple(sorted(
        face.material_index for face in bm.faces
        if all(v.index in vert_set for v in face.verts)))
    return (len(verts), coords, materials), centroid


def analyse_repeats(obj, min_vertices, min_repeats, tolerance_ratio):
    """Units of `obj` that repeat: list of (signature, [(verts, centroid), ...])
    plus the number of units found. Read-only."""
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    parts = loose_parts(bm)
    if len(parts) < 2:
        bm.free()
        return [], len(parts), None
    tolerance = max(obj.dimensions) * tolerance_ratio if max(obj.dimensions) > 0 else 1e-4
    units = cluster_parts(parts, tolerance)
    groups = defaultdict(list)
    for verts in units:
        if len(verts) < min_vertices:
            continue
        signature, centroid = cluster_signature(bm, verts)
        groups[signature].append((verts, centroid))
    repeated = [(signature, items) for signature, items in groups.items()
                if len(items) >= min_repeats]
    return repeated, len(units), bm


def extract_unit(source_bm, verts, centroid, template_mesh, name):
    """New mesh holding one unit, centered on its centroid."""
    keep = set(v.index for v in verts)
    copy = source_bm.copy()
    copy.verts.ensure_lookup_table()
    doomed = [v for v in copy.verts if v.index not in keep]
    bmesh.ops.delete(copy, geom=doomed, context='VERTS')
    for vertex in copy.verts:
        vertex.co -= centroid
    mesh = bpy.data.meshes.new(name)
    copy.to_mesh(mesh)
    copy.free()
    for material in template_mesh.materials:
        mesh.materials.append(material)
    return mesh


class OBJECT_OT_split_repeated_parts(bpy.types.Operator):
    """Find the elements repeated inside a mesh (the same box 16 times, the
    same balcony 4 times) and give them back as instances of one shared
    mesh, placed where they were. Pieces that touch each other count as one
    element, so a balcony stays a balcony; unique pieces stay in the object.
    Rotated copies are not recognised, only moved ones"""
    bl_idname = "kelit_toolkit.split_repeated_parts"
    bl_label = "Split Repeated Parts"
    bl_options = {'REGISTER', 'UNDO'}

    min_vertices: bpy.props.IntProperty(
        name="Min Vertices per Element",
        description="Smaller repeated pieces (bars, bolts, letters) are left in the mesh",
        default=24, min=3
    )
    min_repeats: bpy.props.IntProperty(
        name="Min Repeats",
        description="An element must appear at least this many times to be extracted",
        default=2, min=2
    )
    contact_tolerance: bpy.props.FloatProperty(
        name="Contact Tolerance",
        description="Pieces closer than this fraction of the object's size are one element",
        default=0.005, min=0.0, max=0.1, precision=3
    )

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    def _targets(self, context):
        return [obj for obj in context.selected_objects
                if obj.type == 'MESH' and obj.data and obj.library is None
                and len(mesh_users(obj.data)) == 1]

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=440)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "min_vertices")
        layout.prop(self, "min_repeats")
        layout.prop(self, "contact_tolerance")
        box = layout.box()
        shown = 0
        total_instances = 0
        for obj in self._targets(context):
            repeated, units, bm = analyse_repeats(obj, self.min_vertices, self.min_repeats,
                                                  self.contact_tolerance)
            if bm is not None:
                bm.free()
            if not repeated:
                continue
            instances = sum(len(items) for _signature, items in repeated)
            total_instances += instances
            if shown < 6:
                box.label(text=f"  {obj.name}: {instances} instance(s) of "
                               f"{len(repeated)} element(s), {units} unit(s) in the mesh")
            shown += 1
        if shown == 0:
            box.label(text="  nothing repeated with these settings")
        elif shown > 6:
            box.label(text=f"  ... and {shown - 6} more object(s)")
        layout.label(text=f"{total_instances} instance(s) will be created", icon='INFO')

    def execute(self, context):
        created = 0
        elements = 0
        touched = 0
        for obj in self._targets(context):
            repeated, _units, bm = analyse_repeats(obj, self.min_vertices, self.min_repeats,
                                                   self.contact_tolerance)
            if not repeated:
                if bm is not None:
                    bm.free()
                continue
            touched += 1
            extracted = set()
            for number, (_signature, items) in enumerate(repeated, start=1):
                base_name = f"{clean_name(obj.name)}_Part{number:02d}"
                first_verts, first_centroid = items[0]
                shared = extract_unit(bm, first_verts, first_centroid, obj.data, base_name)
                for verts, centroid in items:
                    instance = bpy.data.objects.new(base_name, shared)
                    for collection in obj.users_collection:
                        collection.objects.link(instance)
                    instance.parent = obj.parent
                    instance.matrix_parent_inverse = obj.matrix_parent_inverse.copy()
                    instance.matrix_world = obj.matrix_world @ mathutils.Matrix.Translation(centroid)
                    extracted.update(v.index for v in verts)
                    created += 1
                elements += 1
            # what is left of the original: everything that was not extracted
            doomed = [v for v in bm.verts if v.index in extracted]
            bmesh.ops.delete(bm, geom=doomed, context='VERTS')
            if len(bm.verts) == 0:
                bm.free()
                bpy.data.objects.remove(obj, do_unlink=True)
            else:
                bm.to_mesh(obj.data)
                bm.free()
                obj.data.update()
        if touched == 0:
            self.report({'INFO'}, "Nothing repeated found with these settings")
            return {'CANCELLED'}
        self.report({'INFO'}, f"{created} instance(s) of {elements} element(s) "
                              f"extracted from {touched} object(s)")
        return {'FINISHED'}


classes = (
    OBJECT_OT_rejoin_material_splits,
    OBJECT_OT_split_repeated_parts,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
