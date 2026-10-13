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

from ..utils import (ROTATION_MIN_CONFIDENCE, canonical_frames, clean_name,
                     find_rigid_match, mesh_users, natural_axes, numpy_to_matrix3,
                     rigid_match_tolerance, shape_invariant_key)


# ============================================================================
# REJOIN MATERIAL SPLITS
# ============================================================================

SPLIT_PATTERN = re.compile(r'^(?P<base>.+?)Material[A-Za-z0-9]*$')


_MODIFIER_SKIP = {'name', 'show_expanded', 'show_viewport', 'show_render', 'show_in_editmode',
                  'show_on_cage', 'is_active', 'is_override_data_editable', 'persistent_uid',
                  'use_pin_to_last', 'execution_time', 'rna_type', 'show_expanded_panel'}


def _modifier_value(value):
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    if isinstance(value, bpy.types.ID):
        return ('ID', value.name)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted(str(item) for item in value))
    try:
        return tuple(_modifier_value(item) for item in value)
    except TypeError:
        return repr(type(value))


def modifier_stack(obj):
    """Comparable description of an object's modifier stack: type and
    settings of each modifier, in order."""
    stack = []
    for modifier in obj.modifiers:
        settings = []
        for prop in modifier.bl_rna.properties:
            if prop.identifier in _MODIFIER_SKIP or prop.type == 'COLLECTION':
                continue
            try:
                value = getattr(modifier, prop.identifier)
            except Exception:
                continue
            if prop.type == 'POINTER' and not isinstance(value, (bpy.types.ID, type(None))):
                continue
            settings.append((prop.identifier, _modifier_value(value)))
        stack.append((modifier.type, tuple(settings)))
    return tuple(stack)


def split_groups(objects, refused=None):
    """Objects whose names share a base before a trailing 'Material...'
    token, grouped by base (Blender suffixes ignored). Only groups whose
    members are single-user meshes with the same parent and the same
    modifier stack are returned (joining keeps only the first member's
    modifiers). Groups left out for their modifiers are added to `refused`
    as (base, reason) when a list is given."""
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
        if len({modifier_stack(obj) for obj in members}) > 1:
            if refused is not None:
                refused.append((base, "members have different modifiers"))
            continue
        result.append((base, sorted(members, key=lambda o: o.name)))
    return sorted(result, key=lambda item: item[0])


class OBJECT_OT_rejoin_material_splits(bpy.types.Operator):
    """Join back objects an export cut into one object per material (Box074Material260, Box074Material310) into one object with several slots"""
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
        refused = []
        if not split_groups(self._objects(context), refused):
            if refused:
                self.report({'WARNING'}, "Nothing to join: " + "; ".join(
                    f"{base} ({reason})" for base, reason in refused))
                return {'CANCELLED'}
            self.report({'INFO'}, "No object split by material found (names ending in Material...)")
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=440)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "search_scope")
        refused = []
        groups = split_groups(self._objects(context), refused)
        joined = sum(len(members) for _base, members in groups)
        layout.label(text=f"{joined} object(s) will become {len(groups)}", icon='INFO')
        box = layout.box()
        for base, members in groups[:6]:
            box.label(text=f"  {base}: {len(members)} parts")
        if len(groups) > 6:
            box.label(text=f"  ... and {len(groups) - 6} more")
        for base, reason in refused[:4]:
            layout.label(text=f"{base} skipped: {reason}", icon='ERROR')

    def execute(self, context):
        refused = []
        groups = split_groups(self._objects(context), refused)
        refused_text = "; ".join(f"{base} skipped ({reason})" for base, reason in refused)
        if not groups:
            if refused:
                self.report({'WARNING'}, "Nothing joined - " + refused_text)
            else:
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
        message = f"{joined} part(s) joined into {len(groups)} object(s)"
        if refused:
            message += " - " + refused_text
        self.report({'WARNING'} if refused else {'INFO'}, message)
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


def unit_signatures(bm, units):
    """(signature, centroid) of every unit: shape, materials and UVs of the
    active layer, independent of where the unit sits. One pass over the
    faces for all units (a face belongs to the unit of its vertices)."""
    unit_of = {}
    centroids = []
    rounded = {}
    for number, verts in enumerate(units):
        centroid = sum((v.co for v in verts), mathutils.Vector()) / len(verts)
        centroids.append(centroid)
        for vertex in verts:
            unit_of[vertex.index] = number
            rounded[vertex.index] = tuple(round(c, 4) for c in (vertex.co - centroid))
    materials = [[] for _unit in units]
    uvs = [[] for _unit in units]
    uv_layer = bm.loops.layers.uv.active
    for face in bm.faces:
        number = unit_of.get(face.verts[0].index)
        if number is None:
            continue
        materials[number].append(face.material_index)
        if uv_layer is not None:
            # per loop, keyed by the loop's vertex position: the same
            # canonical order as the sorted coordinates
            for loop in face.loops:
                uv = loop[uv_layer].uv
                uvs[number].append((rounded[loop.vert.index], round(uv.x, 4), round(uv.y, 4)))
    result = []
    for number, verts in enumerate(units):
        coords = tuple(sorted(rounded[v.index] for v in verts))
        signature = (len(verts), coords, tuple(sorted(materials[number])),
                     tuple(sorted(uvs[number])))
        result.append((signature, centroids[number]))
    return result


def cluster_signature(bm, verts):
    """Shape + materials + UVs of one unit, independent of where it sits."""
    return unit_signatures(bm, [verts])[0]


def _unit_records(bm, units):
    """Surface of each unit for the rotation-aware comparison: vertex
    positions (numpy, in the unit's vertex order), face normals, areas and
    centers, faces as (sorted local vertex indices, material) and loops as
    (local vertex index, u, v). One pass over the faces."""
    import numpy as np
    local = {}
    for number, verts in enumerate(units):
        for position, vertex in enumerate(verts):
            local[vertex.index] = (number, position)
    records = [{'points': np.array([tuple(v.co) for v in verts], dtype=float).reshape(-1, 3),
                'normals': [], 'areas': [], 'centers': [], 'faces': [], 'loops': []}
               for verts in units]
    uv_layer = bm.loops.layers.uv.active
    for face in bm.faces:
        entry = local.get(face.verts[0].index)
        if entry is None:
            continue
        record = records[entry[0]]
        record['normals'].append(tuple(face.normal))
        record['areas'].append(face.calc_area())
        record['centers'].append(tuple(face.calc_center_median()))
        record['faces'].append((tuple(sorted(local[v.index][1] for v in face.verts)),
                                face.material_index))
        if uv_layer is not None:
            for loop in face.loops:
                uv = loop[uv_layer].uv
                record['loops'].append((local[loop.vert.index][1], round(uv.x, 4), round(uv.y, 4)))
    for record in records:
        record['faces'].sort()
        record['loops'].sort()
    return records


def _merge_rotated_units(bm, groups):
    """Merge the groups of `groups` (list of (signature, [(verts, centroid,
    rotation)])) whose units are the same element turned: same canonical
    signature (or coarse invariant key when the exact one missed on a
    rounding boundary), same materials and UVs, verified vertex by vertex
    with the faces' materials and every loop's UV following the vertices."""
    if len(groups) < 2:
        return groups
    bm.normal_update()
    records = _unit_records(bm, [items[0][0] for _signature, items in groups])
    for record in records:
        points = record['points']
        frame, confidence, _method = natural_axes(points, record['normals'], record['areas'],
                                                  record['centers'])
        if confidence < ROTATION_MIN_CONFIDENCE:
            record['keys'] = None
            continue
        canonical, rotations, centroid = canonical_frames(points, frame)
        surfacing = (tuple(sorted(material for _face, material in record['faces'])),
                     tuple(sorted((u, v) for _index, u, v in record['loops'])))
        record['keys'] = ((len(points), 'exact', canonical) + surfacing,
                          (len(points), 'coarse', shape_invariant_key(points)) + surfacing)
        record['rotation'] = rotations[0]
        record['centroid'] = centroid
        record['tolerance'] = rigid_match_tolerance(points)

    def surfacing_follows(source, target):
        def accept(mapping):
            faces = sorted((tuple(sorted(mapping[i] for i in face)), material)
                           for face, material in source['faces'])
            if faces != target['faces']:
                return False
            loops = sorted((mapping[i], u, v) for i, u, v in source['loops'])
            return loops == target['loops']
        return accept

    merged_into = list(range(len(groups)))
    relative = {}
    for key_index in (0, 1):
        buckets = defaultdict(list)
        for index, record in enumerate(records):
            if record['keys'] is not None and merged_into[index] == index:
                buckets[record['keys'][key_index]].append(index)
        for members in buckets.values():
            masters = []
            for index in members:
                record = records[index]
                for master in masters:
                    source = records[master]
                    rotation, _mapping = find_rigid_match(
                        source['points'], record['points'], source['rotation'],
                        record['rotation'], source['centroid'], record['centroid'],
                        max(source['tolerance'], record['tolerance']), same_order=False,
                        accept=surfacing_follows(source, record))
                    if rotation is not None:
                        merged_into[index] = master
                        relative[index] = rotation
                        break
                else:
                    masters.append(index)
    result = []
    for index, (signature, items) in enumerate(groups):
        if merged_into[index] != index:
            continue
        members = list(items)
        for other, target in enumerate(merged_into):
            if target == index and other != index:
                rotation = numpy_to_matrix3(relative[other])
                members.extend((verts, centroid, rotation)
                               for verts, centroid, _identity in groups[other][1])
        result.append((signature, members))
    return result


def analyse_repeats(obj, min_vertices, min_repeats, tolerance_ratio, rotated=True):
    """Units of `obj` that repeat: list of (signature, [(verts, centroid,
    rotation), ...]) plus the number of units found. With `rotated`, units
    equal up to a rotation are one element (rotation relative to the first
    item, identity otherwise). Read-only."""
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
    candidates = [verts for verts in units if len(verts) >= min_vertices]
    identity = mathutils.Matrix.Identity(3)
    for verts, (signature, centroid) in zip(candidates, unit_signatures(bm, candidates)):
        groups[signature].append((verts, centroid, identity))
    grouped = list(groups.items())
    if rotated:
        grouped = _merge_rotated_units(bm, grouped)
    repeated = [(signature, items) for signature, items in grouped
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
    """Turn elements repeated inside the selected meshes into instances of one shared mesh. Pieces that touch stay together; small pieces stay in place"""
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
    rotated_copies: bpy.props.BoolProperty(
        name="Rotated Copies",
        description="Elements equal up to a rotation share one mesh, each instance "
                    "turned like the element it replaces",
        default=True
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
        layout.prop(self, "rotated_copies")
        box = layout.box()
        shown = 0
        total_instances = 0
        for obj in self._targets(context):
            repeated, units, bm = analyse_repeats(obj, self.min_vertices, self.min_repeats,
                                                  self.contact_tolerance, self.rotated_copies)
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
                                                   self.contact_tolerance, self.rotated_copies)
            if not repeated:
                if bm is not None:
                    bm.free()
                continue
            touched += 1
            extracted = set()
            for number, (_signature, items) in enumerate(repeated, start=1):
                base_name = f"{clean_name(obj.name)}_Part{number:02d}"
                first_verts, first_centroid, _first_rotation = items[0]
                shared = extract_unit(bm, first_verts, first_centroid, obj.data, base_name)
                for verts, centroid, rotation in items:
                    instance = bpy.data.objects.new(base_name, shared)
                    for collection in obj.users_collection:
                        collection.objects.link(instance)
                    instance.parent = obj.parent
                    instance.matrix_parent_inverse = obj.matrix_parent_inverse.copy()
                    instance.matrix_world = (obj.matrix_world
                                             @ mathutils.Matrix.Translation(centroid)
                                             @ rotation.to_4x4())
                    extracted.update(v.index for v in verts)
                    created += 1
                elements += 1
            # what is left of the original: everything that was not extracted
            doomed = [v for v in bm.verts if v.index in extracted]
            bmesh.ops.delete(bm, geom=doomed, context='VERTS')
            if len(bm.verts) == 0:
                bm.free()
                # its children stay where they are, under its own parent
                for child in list(obj.children):
                    child_world = child.matrix_world.copy()
                    child.parent = obj.parent
                    child.matrix_world = child_world
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
