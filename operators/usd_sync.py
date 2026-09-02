"""USD Scene Sync - one-click scene transfer with the fidelity of USD.

1. Export the selected hierarchy (meshes + parent empties) to a USD file,
   post-processed with USD 'kind' metadata (component/group) so Unreal imports
   SEPARATE static meshes instead of one merged mesh.
2. Drive the running Unreal Editor through the vendored remote-execution
   client (dependencies/remote_execution.py):
   - import the USD as content assets (materials survive far better than FBX),
   - spawn one actor per Blender object at its exact world transform,
   - rebuild the Blender parent hierarchy with actor attachments.

Requirements in Unreal: 'USD Importer' plugin + Python remote execution
enabled (Project Settings > Plugins > Python).

Coordinate conversions follow the same convention as Epic's BlenderTools
(poly-hammer, MIT).
"""

import contextlib
import json
import math
import os
import re
import tempfile

import bpy

from .unreal_link import run_unreal_python
from .unified_export import (
    get_deform_armature,
    is_descendant_of,
    isolated_selection,
    action_frame_range,
    at_neutral_root,
    asset_filename,
)


# ============================================================================
# COORDINATE CONVERSIONS (same math as BlenderTools core/utilities.py)
# ============================================================================

def convert_blender_to_unreal_location(location):
    """Blender meters (right-handed) -> Unreal centimeters (left-handed)."""
    return [location[0] * 100, -location[1] * 100, location[2] * 100]


def convert_blender_rotation_to_unreal_rotation(rotation):
    """Blender euler XYZ radians -> Unreal rotator list [pitch, yaw, roll]."""
    x = math.degrees(rotation[0])
    y = math.degrees(rotation[1])
    z = math.degrees(rotation[2])
    return [-y, -z, x]


def decompose_signed(matrix, euler_reference=None):
    """
    (translation, euler XYZ, scale) with mirrored matrices handled: plain
    to_scale()/to_euler() silently drop a negative determinant, so mirrored
    objects (common in imports) arrived un-mirrored in Unreal. A negative
    determinant flips the X scale, keeping the rotation part pure so that
    T @ R @ S recomposes the input matrix.
    """
    import mathutils
    m3 = matrix.to_3x3()
    # column lengths = unsigned scale magnitudes (to_scale() distributes
    # signs its own way on mirrored matrices, so it cannot be trusted here)
    scale = mathutils.Vector((m3.col[0].length, m3.col[1].length,
                              m3.col[2].length))
    if m3.determinant() < 0:
        scale.x = -scale.x
    safe = [value if abs(value) > 1e-9 else 1e-9 for value in scale]
    rotation_m3 = m3 @ mathutils.Matrix.Diagonal(
        (1.0 / safe[0], 1.0 / safe[1], 1.0 / safe[2]))
    if euler_reference is not None:
        euler = rotation_m3.to_euler('XYZ', euler_reference)
    else:
        euler = rotation_m3.to_euler('XYZ')
    return matrix.to_translation(), euler, scale


def sanitize_prim_name(name):
    """Mirror the USD prim-name sanitization: ASCII identifiers only
    (pxr TfMakeValidIdentifier) - non-ASCII/non-alphanumeric become
    underscores and a leading digit is prefixed."""
    sanitized = ''.join(
        char if (char.isascii() and char.isalnum()) else '_' for char in name)
    if sanitized and sanitized[0].isdigit():
        sanitized = '_' + sanitized
    return sanitized or '_'


# ============================================================================
# SCENE GRAPH
# ============================================================================

SYNCABLE_TYPES = {'MESH', 'EMPTY', 'ARMATURE'}


def collect_scene_objects(selection):
    """
    Expand a selection into the full set of objects to sync:
    ancestors (parent empties) AND descendants (children of selected roots).
    Returned ordered parents-first (by hierarchy depth).
    """
    gathered = {}

    def add(obj):
        if obj is not None and obj.type in SYNCABLE_TYPES:
            gathered[obj.name] = obj

    for obj in selection:
        if obj.type not in SYNCABLE_TYPES:
            continue
        add(obj)
        # Walk up so hierarchy empties come along
        parent = obj.parent
        while parent is not None:
            add(parent)
            parent = parent.parent
        # Walk down so selecting a root empty sends the whole sub-tree
        for child in obj.children_recursive:
            add(child)

    def depth(obj):
        count = 0
        parent = obj.parent
        while parent is not None:
            count += 1
            parent = parent.parent
        return count

    return sorted(gathered.values(), key=depth)


def geometry_key(mesh):
    """
    Fingerprint of the mesh geometry (vertex/poly counts + hashed vertex
    positions). Objects whose geometries are identical share the same key -
    exactly the ones Unreal's USD importer deduplicates into a single asset.
    """
    import hashlib
    import numpy as np

    vert_count = len(mesh.vertices)
    coords = np.empty(vert_count * 3, dtype=np.float32)
    mesh.vertices.foreach_get('co', coords)
    digest = hashlib.md5(np.round(coords, 5).tobytes()).hexdigest()[:16]
    return f'{vert_count}_{len(mesh.polygons)}_{digest}'


def is_cache_driven(obj):
    """
    True when the object is driven by an Alembic/USD cache rather than by
    keyframes (MeshSequenceCache modifier or TransformCache constraint).

    C4D exports often carry BOTH a keyframed hierarchy and a cache-driven
    duplicate of it. The cache copies cannot be baked to transform keys, and
    their .abc is usually missing outside the authoring machine, so animation
    mode skips them.
    """
    for modifier in getattr(obj, 'modifiers', []):
        if modifier.type == 'MESH_SEQUENCE_CACHE':
            return True
    for constraint in getattr(obj, 'constraints', []):
        if constraint.type == 'TRANSFORM_CACHE':
            return True
    return False


def is_exportable(obj):
    """
    False only for objects excluded from the final render.

    'Disable in viewport' (the monitor toggle) is a working convenience -
    artists use it to lighten the viewport while still wanting the object in
    renders, e.g. a heavy clean mesh hidden behind its fractured stand-in.
    Blender's USD exporter nonetheless drops those objects, whatever the
    evaluation mode, so export_usd_hierarchy clears the flag for the duration
    of the export. Only hide_render means "leave this out".
    """
    return not obj.hide_render


# ============================================================================
# MATERIAL TRANSFER
# ============================================================================

def unreal_texture_name(image):
    """Mirror the asset name Unreal's USD import gives an imported texture."""
    stem = os.path.splitext(os.path.basename(image.name))[0]
    return 'T_' + sanitize_prim_name(stem)


def resolve_bsdf_input(socket, depth=0):
    """
    Describe what feeds a Principled BSDF input.

    Returns {'texture': <UE asset name>, 'image': <blender image>} when the
    chain ends on an image, {'value': ...} for a constant, or None when the
    chain is something we cannot express as a simple PBR parameter.
    """
    if socket is None:
        return None
    if not socket.is_linked:
        value = getattr(socket, 'default_value', None)
        if hasattr(value, '__len__') and not isinstance(value, str):
            return {'value': [round(float(v), 5) for v in value][:3]}
        if value is None:
            return None
        return {'value': round(float(value), 5)}

    node = socket.links[0].from_node
    hops = 0
    while node is not None and hops < 6:
        if node.type == 'TEX_IMAGE':
            if node.image is None:
                return None
            return {'texture': unreal_texture_name(node.image),
                    'image': node.image.name,
                    'colorspace': node.image.colorspace_settings.name}
        # step through normal-map / bump / colour-adjust nodes
        nxt = None
        for candidate in ('Color', 'Height', 'Fac'):
            inner = node.inputs.get(candidate)
            if inner is not None and inner.is_linked:
                nxt = inner.links[0].from_node
                break
        if nxt is None:
            for inner in node.inputs:
                if inner.is_linked:
                    nxt = inner.links[0].from_node
                    break
        node = nxt
        hops += 1
    return None


def get_principled(material):
    if not material or not material.use_nodes or material.node_tree is None:
        return None
    for node in material.node_tree.nodes:
        if node.type == 'BSDF_PRINCIPLED':
            return node
    return None


def has_parasitic_alpha(material):
    """
    True when Alpha is driven by the very image that drives Base Color.

    C4D/Redshift conversions routinely wire the diffuse map into Alpha as
    well. Blender hides it (the maps are opaque), but USD then exports an
    'opacity' input and Unreal builds a *translucent* material with a black
    base colour - the single biggest source of "it doesn't look like Blender".
    """
    bsdf = get_principled(material)
    if bsdf is None:
        return False
    alpha = resolve_bsdf_input(bsdf.inputs.get('Alpha'))
    if not alpha or 'texture' not in alpha:
        return False
    base = resolve_bsdf_input(bsdf.inputs.get('Base Color'))
    return bool(base and base.get('image') == alpha.get('image'))


@contextlib.contextmanager
def temporarily_opaque(materials):
    """
    Neutralise parasitic alpha for the duration of a block.

    The user's .blend is left exactly as it was: the link is re-created and
    the blend method restored on the way out.
    """
    undone = []
    for material in materials:
        if not has_parasitic_alpha(material):
            continue
        bsdf = get_principled(material)
        alpha = bsdf.inputs.get('Alpha')
        link = alpha.links[0]
        attribute, previous = surface_method(material)
        undone.append((material, bsdf, link.from_node, link.from_socket,
                       attribute, previous))
        material.node_tree.links.remove(link)
        alpha.default_value = 1.0
        if attribute is not None:
            try:
                setattr(material, attribute,
                        'DITHERED' if attribute == 'surface_render_method' else 'OPAQUE')
            except (AttributeError, TypeError):
                pass
    try:
        yield len(undone)
    finally:
        for material, bsdf, from_node, from_socket, attribute, previous in undone:
            material.node_tree.links.new(from_socket, bsdf.inputs['Alpha'])
            if attribute is not None:
                try:
                    setattr(material, attribute, previous)
                except (AttributeError, TypeError):
                    pass


def surface_method(material):
    """(attribute name, value) of the material's transparency mode. Blender
    4.2+ calls it surface_render_method; blend_method is the deprecated alias
    (a warning today, gone in a future release), read only as a fallback."""
    for name in ('surface_render_method', 'blend_method'):
        if hasattr(material, name):
            return name, getattr(material, name)
    return None, None


def focus_distance_to(camera_matrix, target_location):
    """Focus distance Blender uses for a focus object: the target's distance
    measured ALONG the view axis, not the straight-line distance (a target
    30 degrees off-axis at 10 m focuses at 8.66 m)."""
    forward = -(camera_matrix.to_3x3().col[2].normalized())
    return abs((target_location - camera_matrix.translation).dot(forward))


def collect_materials(objects):
    """Every unique material used by the objects being sent, in stable order."""
    seen = {}
    for obj in objects:
        for slot in getattr(obj, 'material_slots', []):
            if slot.material is not None:
                seen.setdefault(slot.material.name, slot.material)
    return [seen[name] for name in sorted(seen)]


def extract_material_data(materials):
    """
    Flatten each Blender material into the parameters of one Unreal material
    instance: a texture or a constant per PBR channel, nothing else.
    """
    records = {}
    for material in materials:
        bsdf = get_principled(material)
        record = {
            'name': sanitize_prim_name(material.name),
            'blender_name': material.name,
            'had_parasitic_alpha': has_parasitic_alpha(material),
        }
        if bsdf is None:
            record['unsupported'] = True
            records[material.name] = record
            continue

        for key, socket_name in (('base_color', 'Base Color'),
                                 ('metallic', 'Metallic'),
                                 ('roughness', 'Roughness'),
                                 ('normal', 'Normal'),
                                 ('emissive', 'Emission Color')):
            resolved = resolve_bsdf_input(bsdf.inputs.get(socket_name))
            if resolved:
                record[key] = resolved

        strength = bsdf.inputs.get('Emission Strength')
        if strength is not None and not strength.is_linked:
            record['emissive_strength'] = round(float(strength.default_value), 4)
        records[material.name] = record
    return records


def build_material_slots(objects):
    """
    Which material each Unreal mesh *section* will use.

    Blender's USD export only writes a GeomSubset for material slots that
    faces actually reference, and Unreal turns one subset into one section.
    An empty slot therefore produces no section, and reporting Blender's raw
    slot list would shift every later material onto the wrong geometry - the
    body of a laptop ending up with the trackpad's material. Only slots with
    faces are reported, in ascending slot order, which is the order Unreal
    creates its sections in.
    """
    slots = {}
    for obj in objects:
        if obj.type != 'MESH' or obj.data is None or not obj.material_slots:
            continue
        used = sorted({poly.material_index for poly in obj.data.polygons})
        names = [obj.material_slots[i].material.name
                 for i in used
                 if i < len(obj.material_slots) and obj.material_slots[i].material]
        if names:
            slots[obj.name] = names
    return slots


@contextlib.contextmanager
def temporarily_unique_material_names(materials):
    """
    Keep materials whose name ends in a digit distinguishable in Unreal.

    Unreal strips a trailing '_<digits>' when it names an imported asset, so
    'RS OpenPBR_7' and 'RS OpenPBR_8' both become 'RS_OpenPBR', collide, and
    get deduplicated into a single material. The mesh then comes back with
    fewer sections than Blender had, and every later slot lands on the wrong
    geometry. A non-numeric suffix during the export keeps them apart; the
    .blend gets its names back on the way out.
    """
    renamed = []
    for material in materials:
        if re.search(r'\d$', sanitize_prim_name(material.name)):
            original = material.name
            material.name = original + '_M'
            renamed.append((material, original))
    try:
        yield len(renamed)
    finally:
        for material, original in reversed(renamed):
            material.name = original


@contextlib.contextmanager
def temporarily_visible(objects):
    """
    Clear 'disable in viewport' / view-layer hiding for the duration of a block.

    Blender drops such objects from the dependency graph entirely: they are
    neither written to USD nor animated when their world matrix is sampled -
    even though the artist only hid them for viewport comfort and still
    renders them. Both the export and the animation bake need them back.
    """
    restored = []
    for obj in objects:
        if obj.hide_viewport:
            obj.hide_viewport = False
            restored.append((obj, 'viewport'))
        try:
            if obj.hide_get():
                obj.hide_set(False)
                restored.append((obj, 'layer'))
        except RuntimeError:
            pass  # object not in this view layer
    if restored:
        bpy.context.view_layer.update()
    try:
        yield
    finally:
        for obj, kind in restored:
            if kind == 'viewport':
                obj.hide_viewport = True
            else:
                try:
                    obj.hide_set(True)
                except RuntimeError:
                    pass


# canonical slotted-action iterator lives in utils (shared addon-wide)
from ..utils import iter_action_fcurves  # noqa: E402


def authored_key_offsets(obj, frame_start, frame_end, include_ancestors=False):
    """
    Frame offsets (frame - frame_start) of the object's own keyframes, plus
    its ancestors' when the motion being baked inherits theirs (world-space
    bake). Used to seed key reduction so the Unreal curves keep the artist's
    original keys.
    """
    offsets = set()
    chain = [obj]
    if include_ancestors:
        parent = obj.parent
        while parent is not None:
            chain.append(parent)
            parent = parent.parent
    for link in chain:
        anim = link.animation_data
        if not anim or not anim.action:
            continue
        for fcurve in iter_action_fcurves(anim.action):
            for point in fcurve.keyframe_points:
                frame = int(round(point.co[0]))
                if frame_start <= frame <= frame_end:
                    offsets.add(int(frame - frame_start))
    return offsets


# what "identical to Blender" means per transform channel:
# 0-2 location (cm), 3-5 rotation (deg), 6-8 scale
CHANNEL_TOLERANCE = [0.05, 0.05, 0.05, 0.05, 0.05, 0.05, 0.0005, 0.0005, 0.0005]


def channel_tolerance(channel, values):
    """Perceptual tolerance for one channel: 0.1% of the motion's amplitude,
    floored at the absolute base (0.5 mm / 0.05 deg). A 3 m dolly does not
    need half-millimetre glue keys."""
    return max(CHANNEL_TOLERANCE[channel],
               0.001 * (max(values) - min(values)))


def reduce_channel_keys(values, seed_offsets, tolerance):
    """
    Turn a dense per-frame value list into sparse [offset, value] keys:
    seed with the artist's own key frames, insert glue keys where the
    interpolated curve would drift beyond the tolerance, then prune every
    key whose removal stays within it. The interpolation model matches
    UE 5.8's AUTO tangents as measured in the live editor: flat at the end
    keys, flat at interior extrema, otherwise the central difference clamped
    to 1.5x the smaller adjacent secant slope.
    """
    count = len(values)
    last = count - 1
    if count <= 2 or tolerance <= 0:
        return [[offset, values[offset]] for offset in range(count)]

    def tangent_at(key_offsets, j):
        # UE 5.8 'AUTO' tangents, measured in the live editor (exact to 4
        # decimals): flat at the end keys; flat at interior extrema;
        # otherwise the central difference clamped to 1.5x the smaller
        # adjacent secant slope.
        if j == 0 or j == len(key_offsets) - 1:
            return 0.0
        prev_o = key_offsets[j - 1]
        cur_o = key_offsets[j]
        next_o = key_offsets[j + 1]
        s_in = (values[cur_o] - values[prev_o]) / (cur_o - prev_o)
        s_out = (values[next_o] - values[cur_o]) / (next_o - cur_o)
        if s_in * s_out <= 0:
            return 0.0
        central = (values[next_o] - values[prev_o]) / (next_o - prev_o)
        magnitude = min(abs(central), 1.5 * min(abs(s_in), abs(s_out)))
        return magnitude if central >= 0 else -magnitude

    def segment_worst(key_offsets, j):
        # (worst |model - samples|, frame of that worst) between keys j, j+1
        f0, f1 = key_offsets[j], key_offsets[j + 1]
        span = f1 - f0
        if span <= 1:
            return 0.0, None
        v0, v1 = values[f0], values[f1]
        m0 = tangent_at(key_offsets, j) * span
        m1 = tangent_at(key_offsets, j + 1) * span
        worst, worst_offset = 0.0, None
        for offset in range(f0 + 1, f1):
            t = (offset - f0) / span
            t2, t3 = t * t, t * t * t
            interp = ((2 * t3 - 3 * t2 + 1) * v0 + (t3 - 2 * t2 + t) * m0
                      + (-2 * t3 + 3 * t2) * v1 + (t3 - t2) * m1)
            error = abs(interp - values[offset])
            if error > worst:
                worst, worst_offset = error, offset
        return worst, worst_offset

    def solve(initial_keys):
        keys = sorted(initial_keys)
        # noisy channels (physics, handheld) degenerate to near-dense keys
        # through an O(N^2) refine - past this point dense IS the answer
        dense_threshold = max(16, int(count * 0.4))

        # refine: insert keys until the whole curve fits within tolerance
        while True:
            worst, worst_offset = 0.0, None
            for j in range(len(keys) - 1):
                error, offset = segment_worst(keys, j)
                if error > worst:
                    worst, worst_offset = error, offset
            if worst <= tolerance or worst_offset is None:
                break
            if len(keys) >= dense_threshold:
                return list(range(count))
            insert_at = 0
            while insert_at < len(keys) and keys[insert_at] < worst_offset:
                insert_at += 1
            keys.insert(insert_at, worst_offset)

        # prune: drop any interior key whose removal keeps the curve within
        # tolerance. Tangents are local, so only the segments around the
        # removed key need re-checking.
        j = 1
        while 0 < j < len(keys) - 1:
            trial = keys[:j] + keys[j + 1:]
            seg_lo = max(0, j - 2)
            seg_hi = min(len(trial) - 2, j)
            removable = True
            for seg in range(seg_lo, seg_hi + 1):
                error, _ = segment_worst(trial, seg)
                if error > tolerance:
                    removable = False
                    break
            if removable:
                keys = trial
                j = max(1, j - 1)
            else:
                j += 1
        return keys

    # Two candidates: seeding with the artist's keyframes keeps their frames
    # when they matter, but densely-baked sources (C4D bakes = one key per
    # frame) trap the greedy prune in a local minimum - a fit built from the
    # ends alone escapes it. Both are within tolerance; keep the smaller,
    # preferring the seeded fit on ties. With no seeds the two solves are
    # identical, so run only one.
    seed_set = {0, last} | {int(o) for o in seed_offsets if 0 < int(o) < last}
    seeded = solve(seed_set)
    if len(seed_set) > 2:
        minimal = solve({0, last})
        best = minimal if len(minimal) < len(seeded) else seeded
    else:
        best = seeded
    return [[o, values[o]] for o in best]


def bake_world_animation(context, objects, frame_start, frame_end,
                         relative_to_parent=False, key_mode='BAKED',
                         camera_sampler=None):
    """
    Sample every object's evaluated transform on each frame and convert it to
    Unreal space.

    With relative_to_parent, objects whose parent is part of the export are
    sampled RELATIVE to that parent (parent_world^-1 @ world). Combined with
    actually attaching the actors in Unreal this reproduces the full animated
    hierarchy: Sequencer evaluates an attached actor's keys in parent space,
    and the Blender->Unreal conversion is a conjugation, so it distributes
    over parent/child composition and the world result is identical.

    key_mode 'BAKED' writes one key per frame (exact). 'AUTHORED' keeps the
    artist's own keyframes and only inserts extra keys where the interpolated
    curve would drift beyond CHANNEL_TOLERANCE - editable curves in Unreal.

    Returns {object_name: {'const': [9 floats],
                           'keys': {channel: [[offset, value], ...]}}}
    where only the channels that actually vary carry keys. Channel order is
    Unreal's transform-section order:
        0-2 location XYZ, 3-5 rotation (roll, pitch, yaw), 6-8 scale XYZ.

    Eulers are resolved against the previous frame so long rotations stay
    continuous instead of flipping at the +/-180 deg boundary.
    """
    scene = context.scene
    original_frame = scene.frame_current
    frames = range(int(frame_start), int(frame_end) + 1)

    staged_names = {obj.name for obj in objects}
    samples = {obj.name: [] for obj in objects}
    world_consts = {}
    previous_euler = {}

    def as_row(translation, euler, scale):
        location = convert_blender_to_unreal_location(translation)
        pitch, yaw, roll = convert_blender_rotation_to_unreal_rotation(euler)
        return [location[0], location[1], location[2],
                roll, pitch, yaw,
                scale[0], scale[1], scale[2]]

    try:
        first_frame = True
        for frame in frames:
            scene.frame_set(frame)
            depsgraph = context.evaluated_depsgraph_get()
            for obj in objects:
                world = obj.evaluated_get(depsgraph).matrix_world
                if first_frame:
                    # actors spawn at their frame-start WORLD transform even
                    # when the track itself is baked in parent space
                    w_loc, w_euler, w_scale = decompose_signed(world)
                    world_consts[obj.name] = [round(v, 5)
                                              for v in as_row(w_loc, w_euler, w_scale)]
                matrix = world
                if (relative_to_parent and obj.parent is not None
                        and obj.parent.name in staged_names):
                    parent_matrix = obj.parent.evaluated_get(depsgraph).matrix_world
                    # inverted_safe: a parent scale keyed through zero (a
                    # common hide trick) must not abort the whole bake
                    matrix = parent_matrix.inverted_safe() @ matrix
                # signed decomposition keeps mirrored (negative-scale) objects
                # mirrored instead of silently un-flipping them
                location, euler, scale = decompose_signed(
                    matrix, previous_euler.get(obj.name))
                previous_euler[obj.name] = euler
                samples[obj.name].append(as_row(location, euler, scale))
            if camera_sampler is not None:
                # camera sampled in the SAME sweep: a second full timeline
                # evaluation used to double the bake time
                camera_sampler.sample(depsgraph)
            first_frame = False
    finally:
        scene.frame_set(original_frame)

    objects_by_name = {obj.name: obj for obj in objects}
    tracks = {}
    for name, per_frame in samples.items():
        if not per_frame:
            continue
        const = [round(value, 5) for value in per_frame[0]]
        keys = {}
        seeds = None
        for channel in range(9):
            values = [row[channel] for row in per_frame]
            value_range = max(values) - min(values)
            if key_mode == 'AUTHORED':
                # anything below the perception floor is numerical noise from
                # the relative-space math - keep the channel constant
                if value_range <= CHANNEL_TOLERANCE[channel]:
                    continue
                if seeds is None:
                    obj = objects_by_name.get(name)
                    relative_used = (relative_to_parent and obj is not None
                                     and obj.parent is not None
                                     and obj.parent.name in staged_names)
                    seeds = authored_key_offsets(
                        obj, frame_start, frame_end,
                        include_ancestors=not relative_used) if obj else set()
                pairs = reduce_channel_keys(values, seeds,
                                            channel_tolerance(channel, values))
            else:
                if value_range <= 1e-5:
                    continue
                pairs = [[offset, value] for offset, value in enumerate(values)]
            keys[str(channel)] = [[offset, round(value, 5)]
                                  for offset, value in pairs]
        tracks[name] = {'const': const, 'keys': keys,
                        'world_const': world_consts.get(name, const)}
    return tracks


class CameraSampler:
    """Per-frame camera sampling, drivable from the merged bake sweep so the
    timeline is only evaluated once for objects AND camera."""

    def __init__(self, camera_obj, staged_names=None):
        self.camera_obj = camera_obj
        self.parent_obj = camera_obj.parent
        self.sample_parent = (self.parent_obj is not None
                              and (staged_names is None
                                   or self.parent_obj.name in staged_names))
        self.locations, self.forwards, self.ups = [], [], []
        self.parent_locations, self.parent_rotations, self.parent_scales = [], [], []
        self.focus_distances = []
        self._parent_euler = None

    def sample(self, depsgraph):
        import mathutils
        matrix = self.camera_obj.evaluated_get(depsgraph).matrix_world
        basis = matrix.to_3x3()
        forward = basis @ mathutils.Vector((0.0, 0.0, -1.0))
        up = basis @ mathutils.Vector((0.0, 1.0, 0.0))
        location = convert_blender_to_unreal_location(matrix.to_translation())
        self.locations.append([round(v, 5) for v in location])
        # directions only mirror Y, they are not scaled to centimeters
        self.forwards.append([round(forward.x, 6), round(-forward.y, 6), round(forward.z, 6)])
        self.ups.append([round(up.x, 6), round(-up.y, 6), round(up.z, 6)])

        if self.sample_parent:
            # the parent's WORLD transform per frame lets Unreal express
            # the camera keys in parent space without walking the chain
            p_matrix = self.parent_obj.evaluated_get(depsgraph).matrix_world
            self._parent_euler = (p_matrix.to_euler('XYZ', self._parent_euler)
                                  if self._parent_euler else p_matrix.to_euler('XYZ'))
            p_loc = convert_blender_to_unreal_location(p_matrix.to_translation())
            p_pitch, p_yaw, p_roll = convert_blender_rotation_to_unreal_rotation(self._parent_euler)
            p_scale = p_matrix.to_scale()
            self.parent_locations.append([round(v, 5) for v in p_loc])
            self.parent_rotations.append([round(p_pitch, 5), round(p_yaw, 5), round(p_roll, 5)])
            self.parent_scales.append([round(v, 5) for v in p_scale])

        # animated depth of field: sample the focus distance per frame
        # (focus_object evaluated through the depsgraph, so a moving target
        # is followed)
        dof = getattr(self.camera_obj.data, 'dof', None)
        if dof is not None and dof.use_dof:
            distance = dof.focus_distance
            if dof.focus_object is not None:
                target = dof.focus_object.evaluated_get(depsgraph)
                distance = focus_distance_to(matrix, target.matrix_world.translation)
            self.focus_distances.append(round(distance * 100.0, 3))


def build_camera_payload(context, camera_obj, frame_start, frame_end,
                         staged_names=None, sampler=None):
    """
    Bake the camera as world position + forward/up direction vectors (Unreal
    space). The rotator is built in Unreal from those vectors, which avoids
    any ambiguity between Blender's camera convention (looks down -Z, +Y up)
    and Unreal's (+X forward, +Z up).
    """
    import mathutils

    scene = context.scene
    original_frame = scene.frame_current
    render = scene.render

    # a sampler pre-filled by the merged bake sweep avoids a second full
    # timeline evaluation (object bake + camera bake used to sweep twice)
    if sampler is None:
        sampler = CameraSampler(camera_obj, staged_names)
        try:
            for frame in range(int(frame_start), int(frame_end) + 1):
                scene.frame_set(frame)
                sampler.sample(context.evaluated_depsgraph_get())
        finally:
            scene.frame_set(original_frame)

    parent_obj = sampler.parent_obj
    sample_parent = sampler.sample_parent
    locations, forwards, ups = sampler.locations, sampler.forwards, sampler.ups
    parent_locations = sampler.parent_locations
    parent_rotations = sampler.parent_rotations
    parent_scales = sampler.parent_scales

    camera_data = camera_obj.data
    aspect_x = render.resolution_x * render.pixel_aspect_x
    aspect_y = render.resolution_y * render.pixel_aspect_y
    # match Blender's sensor-fit rules, or portrait renders get a wrong FOV:
    # HORIZONTAL: sensor_width drives; VERTICAL: sensor_height drives;
    # AUTO: sensor_width applies to the dominant side of the resolution
    fit = camera_data.sensor_fit
    if fit == 'AUTO':
        fit = 'HORIZONTAL' if aspect_x >= aspect_y else 'VERTICAL'
        driving = camera_data.sensor_width
    else:
        driving = (camera_data.sensor_width if fit == 'HORIZONTAL'
                   else camera_data.sensor_height)
    if fit == 'HORIZONTAL':
        sensor_width = driving
        sensor_height = driving * (aspect_y / aspect_x) if aspect_x else camera_data.sensor_height
    else:
        sensor_height = driving
        sensor_width = driving * (aspect_x / aspect_y) if aspect_y else camera_data.sensor_width

    # Depth of field: Unreal's CineCamera defaults to a manual focus distance
    # that has nothing to do with the shot, so an un-blurred Blender camera
    # arrives blurred. Mirror Blender's setting instead, and ship the
    # per-frame focus curve so animated focus (or a moving focus target)
    # becomes a real track in the sequence.
    dof = getattr(camera_data, 'dof', None)
    focus = {'enabled': False, 'distance_cm': 0.0, 'fstop': 2.8}
    if dof is not None and dof.use_dof:
        distances = list(sampler.focus_distances)
        if distances:
            focus = {
                'enabled': True,
                'distance_cm': distances[0],
                'fstop': round(dof.aperture_fstop, 4),
                'distances_cm': distances,
            }
        else:
            scene.frame_set(int(frame_start))
            try:
                distance = dof.focus_distance
                if dof.focus_object is not None:
                    target = dof.focus_object.matrix_world.to_translation()
                    distance = focus_distance_to(camera_obj.matrix_world, target)
                focus = {
                    'enabled': True,
                    'distance_cm': round(distance * 100.0, 3),
                    'fstop': round(dof.aperture_fstop, 4),
                }
            finally:
                scene.frame_set(original_frame)

    return {
        'name': camera_obj.name,
        'parent': parent_obj.name if sample_parent else None,
        'parent_locations': parent_locations,
        'parent_rotations': parent_rotations,
        'parent_scales': parent_scales,
        # the artist's own key frames (as offsets), to seed key reduction.
        # Ancestors always included: whether the keys end up world or relative
        # is decided Unreal-side, and superfluous seeds only ever add a few
        # keys - the tolerance refinement guarantees correctness either way
        'key_frames': sorted(authored_key_offsets(
            camera_obj, frame_start, frame_end, include_ancestors=True)),
        'locations': locations,
        'forwards': forwards,
        'ups': ups,
        'focal_length': round(camera_data.lens, 4),
        'sensor_width': round(sensor_width, 4),
        'sensor_height': round(sensor_height, 4),
        'focus': focus,
    }


def build_scene_graph(objects):
    """World transforms (converted to Unreal space) + parent links + USD prim paths."""
    staged_names = {obj.name for obj in objects}
    prim_paths = {}
    entries = []
    # memoized per mesh data: 500 instances of one heavy mesh used to hash
    # the same vertices 500 times
    key_cache = {}

    def cached_geometry_key(mesh):
        cache_id = mesh.name_full
        if cache_id not in key_cache:
            key_cache[cache_id] = geometry_key(mesh)
        return key_cache[cache_id]

    for obj in objects:
        matrix = obj.matrix_world
        parent = obj.parent.name if (obj.parent and obj.parent.name in staged_names) else None
        # mirror the exported USD prim path so Unreal assets can be matched
        # exactly through their UsdAssetImportData, whatever the asset naming
        base_path = prim_paths.get(parent, '/root')
        prim_path = f'{base_path}/{sanitize_prim_name(obj.name)}'
        prim_paths[obj.name] = prim_path
        is_mesh = obj.type == 'MESH' and obj.data is not None
        location, euler, scale = decompose_signed(matrix)
        entries.append({
            'name': obj.name,
            'type': 'MESH' if is_mesh else 'EMPTY',
            'mesh_name': obj.data.name if is_mesh else None,
            'data_key': cached_geometry_key(obj.data) if is_mesh else None,
            'prim_path': prim_path,
            'parent': parent,
            'location': convert_blender_to_unreal_location(location),
            'rotation': convert_blender_rotation_to_unreal_rotation(euler),
            'scale': list(scale),
        })
    return entries


# ============================================================================
# SKELETAL ASSETS
# ============================================================================

def split_skeletal_assets(objects):
    """
    Pull the skeletal units out of a sync selection.

    A skeletal unit is an armature + every mesh it deforms (or that is parented
    under it) - the same grouping as the unified exporter, so selecting either
    the armature or one of its meshes brings the whole character.

    :return tuple: (skeletal_assets, remaining_objects) where remaining_objects
        keeps the original static/empty flow untouched.
    """
    armatures = []
    for obj in objects:
        armature = obj if obj.type == 'ARMATURE' else get_deform_armature(obj)
        if armature is not None and armature not in armatures:
            armatures.append(armature)

    skeletal_assets = []
    member_names = set()
    for armature in armatures:
        members = [armature]
        for mesh in bpy.data.objects:
            if mesh.type != 'MESH' or not is_exportable(mesh):
                continue
            if get_deform_armature(mesh) == armature or is_descendant_of(mesh, armature):
                members.append(mesh)
        skeletal_assets.append({'type': 'SKELETAL', 'name': armature.name,
                                'root': armature, 'objects': members})
        member_names.update(member.name for member in members)

    remaining = [obj for obj in objects
                 if obj.name not in member_names and obj.type in {'MESH', 'EMPTY'}]
    return skeletal_assets, remaining


def export_skeletal_usd(filepath, asset):
    """
    Export one skeletal asset to its own USD: armature + skinned meshes +
    blend shapes + the active action (UsdSkel), at the world origin so the
    imported SkeletalMesh asset is clean. The material quirks neutralised by
    the static flow are neutralised here too.
    """
    materials = collect_materials(asset['objects'])
    with temporarily_visible(asset['objects']), \
            temporarily_opaque(materials), \
            temporarily_unique_material_names(materials), \
            isolated_selection(asset['objects']), \
            action_frame_range(bpy.context, asset, True), \
            at_neutral_root(asset):
        bpy.ops.wm.usd_export(
            filepath=filepath,
            selected_objects_only=True,
            export_materials=True,
            convert_world_material=False,
            use_instancing=False,
            export_lights=False,
            export_cameras=False,
            export_armatures=True,
            export_shapekeys=True,
            export_animation=True,
            only_deform_bones=False,
        )


# ============================================================================
# USD EXPORT + KIND TAGGING
# ============================================================================

def export_usd_hierarchy(filepath, objects, export_materials=True):
    """
    Export *objects* to .usda with hierarchy preserved, then inject USD 'kind'
    metadata so Unreal keeps each mesh as a separate static mesh on import.
    Restores the user's selection afterwards. Returns the kind-tag counts.
    """
    original_selection = [o for o in bpy.context.selected_objects]
    original_active = bpy.context.view_layer.objects.active
    materials = collect_materials(objects)

    try:
        # three source-side quirks are neutralised for the duration of the
        # export, then undone: viewport-hidden objects, parasitic alpha that
        # would make Unreal build translucent materials, and material names
        # ending in digits that Unreal would collide and merge
        with temporarily_visible(objects), \
                temporarily_opaque(materials), \
                temporarily_unique_material_names(materials):
            for obj in bpy.data.objects:
                obj.select_set(False)
            for obj in objects:
                try:
                    obj.select_set(True)
                except RuntimeError:
                    pass  # object not in this view layer
            if objects:
                bpy.context.view_layer.objects.active = objects[0]

            bpy.ops.wm.usd_export(
                filepath=filepath,
                selected_objects_only=True,
                export_materials=export_materials,
                triangulate_meshes=True,
                export_animation=False,
                use_instancing=False,
                convert_world_material=False,
                export_lights=False,
                export_cameras=False,
            )
    finally:
        for obj in bpy.data.objects:
            obj.select_set(False)
        for obj in original_selection:
            try:
                obj.select_set(True)
            except RuntimeError:
                pass
        bpy.context.view_layer.objects.active = original_active

    tagged = inject_kind_metadata(filepath)
    asset_hints, data_hints = rename_mesh_prims_to_object_names(filepath)
    return tagged, asset_hints, data_hints


def rename_mesh_prims_to_object_names(filepath):
    """
    Rename every Mesh prim after its parent Xform (= the Blender OBJECT name).

    Blender names Mesh prims after the MESH DATA, which often differs from the
    object name (e.g. C4D imports). Unreal names its static mesh assets after
    the Mesh prim, so without this pass the assets get unpredictable names
    (data names + collision suffixes) and can't be matched back to objects.
    Returns two hint dicts: {xform_prim_name: final_mesh_prim_name} and
    {original_mesh_prim_name: final_mesh_prim_name}, both used for matching.
    """
    try:
        from pxr import Usd, Sdf
    except ImportError:
        return {}, {}

    stage = Usd.Stage.Open(filepath)
    layer = stage.GetRootLayer()
    edit = Sdf.BatchNamespaceEdit()
    hints = {}
    data_hints = {}
    used_names = set()
    edit_count = 0
    display_changed = 0

    for prim in stage.Traverse():
        if prim.GetTypeName() != 'Mesh':
            continue
        parent = prim.GetParent()
        if not parent or parent.IsPseudoRoot():
            continue
        target = parent.GetName()
        # UE's importer strips trailing '_<digits>' from mesh names (it reads
        # them as duplicate counters), collapsing e.g. Cap_1/Cap_2 -> 'Cap'.
        # A non-numeric suffix makes the name mangling-proof.
        if re.match(r'.*_\d+$', target):
            target += '_Mesh'
        final = target
        suffix = 1
        while final in used_names:
            final = f'{target}_v{suffix}x'
            suffix += 1
        used_names.add(final)
        hints[parent.GetName()] = final
        # second index keyed by the ORIGINAL mesh prim name (= the Blender
        # mesh DATA name, unique in the file): object names like 'Cube.001'
        # and 'Cube_001' sanitize identically and would collide in `hints`,
        # while their data names usually still tell them apart. Data names
        # that sanitize alike too are ambiguous: dropped (None) rather than
        # letting the second mesh inherit the first one's asset
        data_key = prim.GetName()
        data_hints[data_key] = None if data_key in data_hints else final

        # UE's USD importer names assets after the prim's *displayName*
        # metadata when present - Blender writes the mesh DATA name there,
        # so it must be aligned too or the prim rename is ignored
        try:
            prim.SetDisplayName(final)
        except AttributeError:
            prim.SetMetadata('displayName', final)
        display_changed += 1

        if prim.GetName() != final:
            edit.Add(Sdf.NamespaceEdit.Rename(prim.GetPath(), final))
            edit_count += 1

    if edit_count and not layer.Apply(edit):
        print("USD Scene Sync: mesh prim rename failed, keeping original names")
        return {}, {}
    if edit_count or display_changed:
        layer.Save()
    return hints, data_hints


def inject_kind_metadata(filepath):
    """
    Tag each Xform prim with a USD 'kind':
    - Xform with a direct Mesh child -> kind = "component" (separate asset in UE)
    - Xform with only Xform children -> kind = "group" (hierarchy node)
    Without these, Unreal's USD importer merges the whole tree into one mesh.

    Uses Blender's bundled pxr bindings (works on binary .usd too); falls back
    to a text pass for .usda files if pxr is unavailable.
    """
    try:
        from pxr import Usd
    except ImportError:
        return _inject_kind_metadata_text(filepath)

    stage = Usd.Stage.Open(filepath)
    tagged = {'component': 0, 'group': 0}
    for prim in stage.Traverse():
        if prim.GetTypeName() != 'Xform':
            continue
        child_types = {child.GetTypeName() for child in prim.GetChildren()}
        if 'Mesh' in child_types:
            kind = 'component'
        elif 'Xform' in child_types:
            kind = 'group'
        else:
            continue
        Usd.ModelAPI(prim).SetKind(kind)
        tagged[kind] += 1
    stage.GetRootLayer().Save()
    return tagged


def _inject_kind_metadata_text(filepath):
    """Regex fallback for ASCII .usda files when pxr is unavailable."""
    with open(filepath, 'r', encoding='utf-8') as usd_file:
        lines = usd_file.readlines()

    def indent_of(line):
        return len(line) - len(line.lstrip())

    def first_child_def(start_index, base_indent):
        for j in range(start_index + 1, len(lines)):
            match = re.match(r'^(\s*)def (Mesh|Xform)\b', lines[j])
            if match:
                child_indent = len(match.group(1))
                if child_indent > base_indent:
                    return match.group(2)
                return None  # sibling/uncle prim -> no child def
        return None

    output = []
    tagged = {'component': 0, 'group': 0}

    for i, line in enumerate(lines):
        bare = re.match(r'^(\s*)def Xform "([^"]+)"\s*$', line.rstrip('\n'))
        with_meta = re.match(r'^(\s*)def Xform "([^"]+)" \(\s*$', line.rstrip('\n'))

        if bare or with_meta:
            match = bare or with_meta
            indent, name = match.group(1), match.group(2)
            child = first_child_def(i, len(indent))
            kind = None
            if name != 'root' and child is not None:
                kind = 'component' if child == 'Mesh' else 'group'

            if kind is None:
                output.append(line)
            elif bare:
                output.append(f'{indent}def Xform "{name}" (\n')
                output.append(f'{indent}    kind = "{kind}"\n')
                output.append(f'{indent})\n')
                tagged[kind] += 1
            else:  # existing metadata block: inject after the opening paren
                output.append(line)
                output.append(f'{indent}    kind = "{kind}"\n')
                tagged[kind] += 1
        else:
            output.append(line)

    with open(filepath, 'w', encoding='utf-8') as usd_file:
        usd_file.writelines(output)

    return tagged


# ============================================================================
# UNREAL-SIDE SCRIPT
# ============================================================================

UNREAL_SCRIPT_TEMPLATE = '''
import json
import traceback
import unreal

PAYLOAD = json.loads(__PAYLOAD__)


def log(message):
    text = '[B2UE] ' + str(message)
    unreal.log(text)
    print(text)


def sanitize(name):
    return ''.join(char if char.isalnum() else '_' for char in name)


def reduce_channel_keys(values, seed_offsets, tolerance):
    """Dense per-frame values -> sparse [offset, value] keys within tolerance.
    Mirror of the Blender-side reducer (usd_sync.reduce_channel_keys): cubic
    hermite with UE AUTO tangents as measured in the live editor."""
    count = len(values)
    last = count - 1
    if count <= 2 or tolerance <= 0:
        return [[offset, values[offset]] for offset in range(count)]

    def tangent_at(key_offsets, j):
        # UE 5.8 'AUTO' tangents, measured in the live editor (exact to 4
        # decimals): flat at the end keys; flat at interior extrema;
        # otherwise the central difference clamped to 1.5x the smaller
        # adjacent secant slope.
        if j == 0 or j == len(key_offsets) - 1:
            return 0.0
        prev_o = key_offsets[j - 1]
        cur_o = key_offsets[j]
        next_o = key_offsets[j + 1]
        s_in = (values[cur_o] - values[prev_o]) / (cur_o - prev_o)
        s_out = (values[next_o] - values[cur_o]) / (next_o - cur_o)
        if s_in * s_out <= 0:
            return 0.0
        central = (values[next_o] - values[prev_o]) / (next_o - prev_o)
        magnitude = min(abs(central), 1.5 * min(abs(s_in), abs(s_out)))
        return magnitude if central >= 0 else -magnitude

    def segment_worst(key_offsets, j):
        # (worst |model - samples|, frame of that worst) between keys j, j+1
        f0, f1 = key_offsets[j], key_offsets[j + 1]
        span = f1 - f0
        if span <= 1:
            return 0.0, None
        v0, v1 = values[f0], values[f1]
        m0 = tangent_at(key_offsets, j) * span
        m1 = tangent_at(key_offsets, j + 1) * span
        worst, worst_offset = 0.0, None
        for offset in range(f0 + 1, f1):
            t = (offset - f0) / span
            t2, t3 = t * t, t * t * t
            interp = ((2 * t3 - 3 * t2 + 1) * v0 + (t3 - 2 * t2 + t) * m0
                      + (-2 * t3 + 3 * t2) * v1 + (t3 - t2) * m1)
            error = abs(interp - values[offset])
            if error > worst:
                worst, worst_offset = error, offset
        return worst, worst_offset

    def solve(initial_keys):
        keys = sorted(initial_keys)
        # noisy channels (physics, handheld) degenerate to near-dense keys
        # through an O(N^2) refine - past this point dense IS the answer
        dense_threshold = max(16, int(count * 0.4))

        # refine: insert keys until the whole curve fits within tolerance
        while True:
            worst, worst_offset = 0.0, None
            for j in range(len(keys) - 1):
                error, offset = segment_worst(keys, j)
                if error > worst:
                    worst, worst_offset = error, offset
            if worst <= tolerance or worst_offset is None:
                break
            if len(keys) >= dense_threshold:
                return list(range(count))
            insert_at = 0
            while insert_at < len(keys) and keys[insert_at] < worst_offset:
                insert_at += 1
            keys.insert(insert_at, worst_offset)

        # prune: drop any interior key whose removal keeps the curve within
        # tolerance. Tangents are local, so only the segments around the
        # removed key need re-checking.
        j = 1
        while 0 < j < len(keys) - 1:
            trial = keys[:j] + keys[j + 1:]
            seg_lo = max(0, j - 2)
            seg_hi = min(len(trial) - 2, j)
            removable = True
            for seg in range(seg_lo, seg_hi + 1):
                error, _ = segment_worst(trial, seg)
                if error > tolerance:
                    removable = False
                    break
            if removable:
                keys = trial
                j = max(1, j - 1)
            else:
                j += 1
        return keys

    # Two candidates: seeding with the artist's keyframes keeps their frames
    # when they matter, but densely-baked sources (C4D bakes = one key per
    # frame) trap the greedy prune in a local minimum - a fit built from the
    # ends alone escapes it. Both are within tolerance; keep the smaller,
    # preferring the seeded fit on ties. With no seeds the two solves are
    # identical, so run only one.
    seed_set = {0, last} | {int(o) for o in seed_offsets if 0 < int(o) < last}
    seeded = solve(seed_set)
    if len(seed_set) > 2:
        minimal = solve({0, last})
        best = minimal if len(minimal) < len(seeded) else seeded
    else:
        best = seeded
    return [[o, values[o]] for o in best]


try:
    # the whole flow depends on the USD Importer plugin: fail early with a
    # clear message instead of a confusing import error further down
    if not hasattr(unreal, 'UsdStageImportOptions'):
        raise RuntimeError('USD Importer plugin is not enabled in this Unreal '
                           'project. Enable it (the Connection Doctor can do '
                           'it) and restart Unreal.')

    dest = PAYLOAD['content_folder']
    scene_tag = 'B2UE:' + PAYLOAD['scene_name']

    # 1) Import USD files as content assets (geometry + materials)
    def import_usd(filename, destination, skeletal):
        task = unreal.AssetImportTask()
        task.filename = filename
        task.destination_path = destination
        task.automated = True
        task.replace_existing = True
        task.save = False
        try:
            options = unreal.UsdStageImportOptions()
            for prop_name, value in [
                ('import_actors', False),
                ('import_geometry', True),
                ('import_materials', PAYLOAD['import_materials']),
                ('import_skeletal_animations', skeletal),
                ('import_level_sequences', False),
                ('import_groom_assets', False),
                # never merge prims into one mesh - one static mesh per Mesh prim,
                # whatever the hierarchy (empties or mesh-under-mesh)
                ('use_prim_kinds_for_collapsing', False),
                ('kinds_to_collapse', 0),
            ]:
                try:
                    options.set_editor_property(prop_name, value)
                except Exception:
                    pass
            task.options = options
        except Exception as error:
            log('UsdStageImportOptions unavailable, using import defaults: %s' % error)
        unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])

    if PAYLOAD['usd_file']:
        import_usd(PAYLOAD['usd_file'], dest, False)

    # 1b) Import each skeletal asset (SkeletalMesh + Skeleton + AnimSequence)
    skeletal_assets = {}
    for skel in PAYLOAD.get('skeletal', []):
        sk_dest = dest + '/' + PAYLOAD['scene_name'] + '/' + skel['asset_name']
        import_usd(skel['usd_file'], sk_dest, True)
        sk_mesh = None
        anim_seq = None
        if unreal.EditorAssetLibrary.does_directory_exist(sk_dest):
            for asset_path in unreal.EditorAssetLibrary.list_assets(sk_dest, recursive=True):
                try:
                    asset = unreal.EditorAssetLibrary.load_asset(asset_path)
                except Exception:
                    continue
                if sk_mesh is None and isinstance(asset, unreal.SkeletalMesh):
                    sk_mesh = asset
                elif anim_seq is None and isinstance(asset, unreal.AnimSequence):
                    anim_seq = asset
        skeletal_assets[skel['name']] = (sk_mesh, anim_seq)
        log('skeletal %s: mesh=%s anim=%s' % (skel['name'], sk_mesh is not None, anim_seq is not None))

    # 2) Map imported static meshes.
    # UE 5.8's USD importer renames assets (adds an 'SM_' prefix) and
    # deduplicates identical geometries, so name equality is not enough:
    #   a) exact match through the asset's UsdAssetImportData prim path,
    #   b) name variants (with/without 'SM_', object or mesh-data name),
    #   c) shared-geometry fallback through the Blender mesh data key.
    scan_root = dest + '/' + PAYLOAD['scene_name']
    if not unreal.EditorAssetLibrary.does_directory_exist(scan_root):
        scan_root = dest
    prim_map = {}
    name_map = {}
    if unreal.EditorAssetLibrary.does_directory_exist(scan_root):
        for asset_path in unreal.EditorAssetLibrary.list_assets(scan_root, recursive=True):
            try:
                asset = unreal.EditorAssetLibrary.load_asset(asset_path)
            except Exception:
                continue
            if not isinstance(asset, unreal.StaticMesh):
                continue
            asset_name = asset.get_name()
            name_map[sanitize(asset_name)] = asset
            if asset_name.startswith('SM_'):
                name_map.setdefault(sanitize(asset_name[3:]), asset)
            try:
                import_data = asset.get_editor_property('asset_import_data')
                prim = str(import_data.get_editor_property('prim_path'))
                if prim:
                    prim_map[prim] = asset
            except Exception:
                pass
    log('assets found: %d static meshes (%d with prim paths)' % (len(name_map), len(prim_map)))

    # 2b) Repair double-sided materials.
    # UE's USD importer honours the USD 'doubleSided' flag by creating a
    # '*_TwoSided' instance with two_sided=True in its base property
    # overrides - but it leaves override_two_sided=False, so the value never
    # takes effect and the material stays single-sided. Anything seen from
    # behind, and any zero-thickness surface (a screen plane, a decal), then
    # renders as a hole and reads as if the normals were inverted.
    two_sided_fixed = 0
    scan_two_sided = (PAYLOAD.get('fix_two_sided')
                      and unreal.EditorAssetLibrary.does_directory_exist(scan_root))
    for asset_path in (unreal.EditorAssetLibrary.list_assets(scan_root, recursive=True)
                       if scan_two_sided else []):
        clean_path = asset_path.split('.')[0]
        try:
            material = unreal.EditorAssetLibrary.load_asset(clean_path)
        except Exception:
            continue
        if not isinstance(material, unreal.MaterialInstanceConstant):
            continue
        try:
            overrides = material.get_editor_property('base_property_overrides')
            wants_two_sided = bool(overrides.get_editor_property('two_sided'))
            already = bool(overrides.get_editor_property('override_two_sided'))
            if wants_two_sided and not already:
                overrides.set_editor_property('override_two_sided', True)
                material.set_editor_property('base_property_overrides', overrides)
                unreal.EditorAssetLibrary.save_asset(clean_path, only_if_is_dirty=False)
                two_sided_fixed += 1
        except Exception as error:
            log('two-sided fixup skipped for %s: %s' % (clean_path, error))
    result_two_sided = two_sided_fixed
    log('double-sided materials repaired: %d' % two_sided_fixed)

    def find_asset(entry):
        prim_path = entry.get('prim_path')
        if prim_path and prim_map:
            leaf = prim_path.rsplit('/', 1)[-1]
            for candidate in (prim_path, prim_path + '/' + leaf):
                if candidate in prim_map:
                    return prim_map[candidate]
            for prim, asset in prim_map.items():
                if prim.startswith(prim_path + '/') or prim.endswith('/' + leaf):
                    return asset
        for raw in (entry.get('asset_hint') or '', entry['name'], entry.get('mesh_name') or ''):
            if not raw:
                continue
            key = sanitize(raw)
            for candidate in ('SM_' + key, key):
                if candidate in name_map:
                    return name_map[candidate]
        return None

    # resolve every mesh first, then fill gaps via shared Blender mesh data
    resolved = {}
    data_assets = {}
    for entry in PAYLOAD['objects']:
        if entry['type'] != 'MESH':
            continue
        asset = find_asset(entry)
        if asset is not None:
            resolved[entry['name']] = asset
            data_key = entry.get('data_key')
            if data_key:
                data_assets.setdefault(data_key, asset)
    for entry in PAYLOAD['objects']:
        if entry['type'] == 'MESH' and entry['name'] not in resolved:
            asset = data_assets.get(entry.get('data_key') or '')
            if asset is not None:
                resolved[entry['name']] = asset

    result = {'assets': len(name_map), 'spawned': 0, 'attached': 0, 'missing': [],
              'sequence_bindings': 0, 'sequence': None, 'camera_cut': False,
              'two_sided_fixed': result_two_sided, 'skeletal': 0}

    if PAYLOAD['place_in_level']:
        subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

        # 3) Remove ONLY the previously synced versions of the objects being
        # sent now (never other objects synced earlier from the same .blend)
        if PAYLOAD['replace_existing']:
            sent_tags = set('B2UE:obj:' + entry['name'] for entry in PAYLOAD['objects'])
            sent_tags.update('B2UE:obj:' + skel['name'] for skel in PAYLOAD.get('skeletal', []))
            for actor in list(subsystem.get_all_level_actors()):
                try:
                    tags = [str(tag) for tag in actor.tags]
                    if scene_tag in tags and sent_tags.intersection(tags):
                        subsystem.destroy_actor(actor)
                except Exception:
                    pass

        # 4) Spawn one actor per Blender object at its world transform.
        # Per-object guard: one bad object must not abort the run after the
        # previous versions were already destroyed in step 3.
        spawned = {}
        for entry in PAYLOAD['objects']:
            name = entry['name']
            try:
                actor = None
                if entry['type'] == 'MESH':
                    asset = resolved.get(name)
                    if asset is None:
                        result['missing'].append(name)
                    else:
                        actor = subsystem.spawn_actor_from_object(
                            asset, entry['location'], entry['rotation'])
                if actor is None:
                    # empties (and meshes whose asset was not found) become plain
                    # StaticMeshActors with no mesh: reliable transform + attach root
                    actor = subsystem.spawn_actor_from_class(
                        unreal.StaticMeshActor, entry['location'], entry['rotation'])
                if actor is None:
                    result['missing'].append(name)
                    continue
                actor.set_actor_label(name)
                # NEVER rename the actor object here. UObject::Rename onto a name
                # that is still registered is a *fatal* engine error, not a Python
                # exception, and destroy_actor() only marks the previous actor for
                # deletion - its name stays taken until garbage collection. A
                # re-sync would then hard-crash the editor.
                actor.set_actor_scale3d(entry['scale'])
                actor.tags = [scene_tag, 'B2UE:obj:' + name]
                if PAYLOAD.get('outliner_folder'):
                    actor.set_folder_path(PAYLOAD['outliner_folder'])
                spawned[name] = actor
                result['spawned'] += 1
            except Exception as error:
                log('spawn failed for %s: %s' % (name, str(error)[:120]))
                result['missing'].append(name)

        # 4b) Spawn skeletal actors with their animation assigned
        for skel in PAYLOAD.get('skeletal', []):
            name = skel['name']
            sk_mesh, anim_seq = skeletal_assets.get(name, (None, None))
            if sk_mesh is None:
                result['missing'].append(name)
                continue
            actor = subsystem.spawn_actor_from_object(
                sk_mesh, skel['location'], skel['rotation'])
            if actor is None:
                result['missing'].append(name)
                continue
            actor.set_actor_label(name)
            actor.set_actor_scale3d(skel['scale'])
            actor.tags = [scene_tag, 'B2UE:obj:' + name]
            if PAYLOAD.get('outliner_folder'):
                actor.set_folder_path(PAYLOAD['outliner_folder'])
            component = actor.skeletal_mesh_component
            if component is not None and anim_seq is not None:
                try:
                    component.set_editor_property(
                        'animation_mode', unreal.AnimationMode.ANIMATION_SINGLE_NODE)
                    component.set_editor_property('anim_to_play', anim_seq)
                except Exception as error:
                    log('anim assign failed for %s: %s' % (name, error))
            spawned[name] = actor
            result['spawned'] += 1
            result['skeletal'] += 1

        # 5) Rebuild the Blender parent hierarchy with attachments.
        # Sequencer keys an attached actor RELATIVE to its parent, so the two
        # must agree: in preserve-hierarchy mode the keys were baked in parent
        # space and every pair is attached; otherwise the keys are world-space
        # and animated pairs stay top-level to avoid corrupting the motion.
        anim_tracks = PAYLOAD.get('animation') or {}
        preserve_hierarchy = bool(PAYLOAD.get('preserve_hierarchy'))
        for entry in PAYLOAD['objects']:
            name = entry['name']
            actor = spawned.get(name)
            parent_name = entry.get('parent') or ''
            parent_actor = spawned.get(parent_name)
            if not actor or not parent_actor:
                continue
            if not preserve_hierarchy and (
                    anim_tracks.get(name, {}).get('keys')
                    or anim_tracks.get(parent_name, {}).get('keys')):
                continue
            actor.attach_to_actor(
                parent_actor,
                '',
                unreal.AttachmentRule.KEEP_WORLD,
                unreal.AttachmentRule.KEEP_WORLD,
                unreal.AttachmentRule.KEEP_WORLD,
                False
            )
            result['attached'] += 1

        # With the hierarchy preserved, anything under an animated ancestor is
        # carried by the attachment - it needs MOVABLE mobility even without
        # keys of its own, or it will stay frozen at runtime.
        if preserve_hierarchy and anim_tracks:
            parents = {entry['name']: entry.get('parent') or ''
                       for entry in PAYLOAD['objects']}
            for entry in PAYLOAD['objects']:
                name = entry['name']
                ancestor = parents.get(name) or ''
                while ancestor:
                    if anim_tracks.get(ancestor, {}).get('keys'):
                        actor = spawned.get(name)
                        if actor is not None:
                            try:
                                actor.set_mobility(unreal.ComponentMobility.MOVABLE)
                            except Exception:
                                pass
                        break
                    ancestor = parents.get(ancestor) or ''

        # 6) Build a native LevelSequence from the transforms baked in Blender
        anim = PAYLOAD.get('animation')
        camera_data = PAYLOAD.get('camera')
        if anim or camera_data:
            fps = PAYLOAD['fps']
            frame_start = PAYLOAD['frame_start']
            # keys land on ticks 0..N-1 for N sampled frames; the playback end
            # is EXCLUSIVE, so it must sit one past the last key or the final
            # frame can never be evaluated (scrubbing to it wraps to start)
            frame_count = PAYLOAD['frame_end'] - frame_start + 1

            seq_path = dest + '/' + PAYLOAD['scene_name']
            seq_name = 'LS_' + PAYLOAD['scene_name']
            full_seq_path = seq_path + '/' + seq_name

            # Reuse an existing sequence rather than delete/recreate: deleting
            # fails while level actors or an open Sequencer tab still reference
            # it, and create_asset then silently returns None. Reusing also
            # keeps the asset path stable for anything already pointing at it.
            sequence = None
            if unreal.EditorAssetLibrary.does_asset_exist(full_seq_path):
                sequence = unreal.EditorAssetLibrary.load_asset(full_seq_path)
                if sequence is not None:
                    for binding in list(sequence.get_bindings()):
                        try:
                            binding.remove()
                        except Exception:
                            pass
                    for track in list(sequence.get_tracks()):
                        try:
                            sequence.remove_track(track)
                        except Exception:
                            pass
                    log('reusing existing sequence %s' % full_seq_path)
            if sequence is None:
                sequence = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
                    seq_name, seq_path, unreal.LevelSequence, unreal.LevelSequenceFactoryNew())
            if sequence is None:
                raise RuntimeError('could not create or reuse LevelSequence at ' + full_seq_path)

            # rational rate so NTSC frame rates (23.976, 29.97) stay exact
            if abs(fps - round(fps)) > 1e-4:
                sequence.set_display_rate(unreal.FrameRate(round(fps * 1001), 1001))
            else:
                sequence.set_display_rate(unreal.FrameRate(int(round(fps)), 1))
            sequence.set_playback_start(0)
            sequence.set_playback_end(frame_count)

            # renamed across UE versions (SequenceTimeUnit -> MovieSceneTimeUnit)
            time_unit_enum = getattr(unreal, 'MovieSceneTimeUnit', None) or \
                getattr(unreal, 'SequenceTimeUnit', None)
            display_rate = time_unit_enum.DISPLAY_RATE

            # cubic AUTO keys so sparse (AUTHORED) curves interpolate smoothly;
            # for dense BAKED keys the interpolation mode is irrelevant
            KEY_INTERP = getattr(unreal, 'MovieSceneKeyInterpolation', None)
            KEY_INTERP = getattr(KEY_INTERP, 'AUTO', None) if KEY_INTERP else None

            def add_channel_key(channel, offset, value):
                if KEY_INTERP is not None:
                    channel.add_key(unreal.FrameNumber(int(offset)), float(value),
                                    0.0, display_rate, KEY_INTERP)
                else:
                    channel.add_key(unreal.FrameNumber(int(offset)), float(value),
                                    0.0, display_rate)

            def key_transform(binding, track_data):
                """Add a transform track and key only the channels that move.
                Keys arrive as [frame_offset, value] pairs - dense (one per
                frame, BAKED) or sparse (the artist's keyframes, AUTHORED)."""
                track = binding.add_track(unreal.MovieScene3DTransformTrack)
                section = track.add_section()
                section.set_range(0, frame_count)
                try:
                    channels = section.get_all_channels()
                except AttributeError:
                    channels = section.get_channels()
                const = track_data['const']
                keys = track_data.get('keys') or {}
                for index in range(min(9, len(channels))):
                    channel = channels[index]
                    pairs = keys.get(str(index))
                    if pairs:
                        for offset, value in pairs:
                            add_channel_key(channel, offset, value)
                    else:
                        # a channel with no key at all would evaluate to 0
                        channel.add_key(unreal.FrameNumber(0), float(const[index]),
                                        0.0, display_rate)
                return section

            # 6a) One binding per animated object - keys are parent-space for
            # attached actors (preserve_hierarchy), world-space otherwise
            object_bindings = {}
            for name, track_data in (anim or {}).items():
                actor = spawned.get(name)
                if actor is None or not track_data.get('keys'):
                    continue
                try:
                    actor.set_mobility(unreal.ComponentMobility.MOVABLE)
                except Exception:
                    pass
                try:
                    binding = sequence.add_possessable(actor)
                    key_transform(binding, track_data)
                    object_bindings[name] = binding
                    result['sequence_bindings'] += 1
                except Exception as error:
                    log('binding failed for %s: %s' % (name, str(error)[:120]))

            # 6a-bis) Skeletal actors: bind their AnimSequence in the sequence
            for skel in PAYLOAD.get('skeletal', []):
                actor = spawned.get(skel['name'])
                anim_seq = skeletal_assets.get(skel['name'], (None, None))[1]
                if actor is None or anim_seq is None:
                    continue
                try:
                    binding = sequence.add_possessable(actor)
                    track = binding.add_track(unreal.MovieSceneSkeletalAnimationTrack)
                    section = track.add_section()
                    section.set_range(0, frame_count)
                    params = section.get_editor_property('params')
                    params.set_editor_property('animation', anim_seq)
                    section.set_editor_property('params', params)
                    result['sequence_bindings'] += 1
                except Exception as error:
                    log('skeletal sequence binding failed for %s: %s' % (skel['name'], error))

            # 6b) A real CineCameraActor, bound and cut to in the sequence
            if camera_data:
                cam_label = camera_data['name']
                for actor in list(subsystem.get_all_level_actors()):
                    try:
                        tags = [str(t) for t in actor.tags]
                        # scene-scoped: nearly every .blend names its camera
                        # "Camera" - without the scene tag this would delete
                        # other synced scenes' cameras in a shared level
                        if scene_tag in tags and 'B2UE:obj:' + cam_label in tags:
                            subsystem.destroy_actor(actor)
                    except Exception:
                        pass
                cam_actor = subsystem.spawn_actor_from_class(
                    unreal.CineCameraActor, unreal.Vector(0, 0, 0), unreal.Rotator(0, 0, 0))
                cam_actor.set_actor_label(cam_label)
                cam_actor.tags = [scene_tag, 'B2UE:obj:' + cam_label]
                if PAYLOAD.get('outliner_folder'):
                    cam_actor.set_folder_path(PAYLOAD['outliner_folder'])

                cam_component = cam_actor.get_cine_camera_component()
                cam_component.set_editor_property('current_focal_length',
                                                  camera_data['focal_length'])
                try:
                    filmback = cam_component.get_editor_property('filmback')
                    filmback.set_editor_property('sensor_width', camera_data['sensor_width'])
                    filmback.set_editor_property('sensor_height', camera_data['sensor_height'])
                    cam_component.set_editor_property('filmback', filmback)
                except Exception as error:
                    log('filmback not set: %s' % error)

                # match Blender's depth of field, rather than leaving Unreal's
                # default manual focus distance in place
                focus = camera_data.get('focus') or {}
                try:
                    settings = cam_component.get_editor_property('focus_settings')
                    if focus.get('enabled'):
                        settings.set_editor_property(
                            'focus_method', unreal.CameraFocusMethod.MANUAL)
                        settings.set_editor_property(
                            'manual_focus_distance', float(focus['distance_cm']))
                        cam_component.set_editor_property(
                            'current_aperture', float(focus.get('fstop', 2.8)))
                    else:
                        settings.set_editor_property(
                            'focus_method', unreal.CameraFocusMethod.DISABLE)
                    cam_component.set_editor_property('focus_settings', settings)
                except Exception as error:
                    log('focus settings not applied: %s' % error)

                # Build the rotator from forward/up: unambiguous across the
                # Blender (-Z forward) / Unreal (+X forward) conventions.
                # In preserve-hierarchy mode the keys are re-expressed in the
                # parent's space (Sequencer evaluates an attached actor's keys
                # RELATIVE to its parent), using the parent world transforms
                # sampled per frame in Blender.
                parent_name = camera_data.get('parent') or ''
                parent_actor = spawned.get(parent_name)
                p_locs = camera_data.get('parent_locations') or []
                p_rots = camera_data.get('parent_rotations') or []
                p_scales = camera_data.get('parent_scales') or []

                # Plan: carry the camera - and the private null chain driving
                # it - INSIDE the sequence as spawnables, so every sequence
                # owns its camera package. A rig null qualifies only while
                # every one of its children is the camera or another chain
                # member: a null that also carries scene objects must stay a
                # level actor.
                camera_spawnable = bool(PAYLOAD.get('camera_spawnable'))
                children_map = {}
                parent_map = {}
                for entry in PAYLOAD['objects']:
                    parent_map[entry['name']] = entry.get('parent') or ''
                    children_map.setdefault(
                        entry.get('parent') or '', []).append(entry['name'])
                will_convert_camera = (camera_spawnable
                                       and not children_map.get(cam_label))
                rig_chain = []
                if will_convert_camera and preserve_hierarchy:
                    allowed = {cam_label}
                    cursor = parent_name
                    while cursor and spawned.get(cursor) is not None:
                        if any(kid not in allowed
                               for kid in children_map.get(cursor, [])):
                            break
                        rig_chain.append(cursor)
                        allowed.add(cursor)
                        cursor = parent_map.get(cursor) or ''

                # A spawnable can only be attached to another BINDING (attach
                # track): a chain null (always bound) or an animated
                # possessable. With no such target the keys must stay world.
                parent_attachable = (parent_name in rig_chain
                                     or bool(anim_tracks.get(parent_name, {}).get('keys')))
                use_relative = (preserve_hierarchy and parent_actor is not None
                                and len(p_locs) == len(camera_data['locations'])
                                and (not will_convert_camera or parent_attachable))

                cam_const = None
                cam_world_const = None
                cam_keys = {index: [] for index in range(9)}
                previous = None
                for index, location in enumerate(camera_data['locations']):
                    forward = camera_data['forwards'][index]
                    up = camera_data['ups'][index]
                    rotator = unreal.MathLibrary.make_rot_from_xz(
                        unreal.Vector(forward[0], forward[1], forward[2]),
                        unreal.Vector(up[0], up[1], up[2]))
                    loc_vec = unreal.Vector(location[0], location[1], location[2])
                    if cam_world_const is None:
                        cam_world_const = [loc_vec.x, loc_vec.y, loc_vec.z,
                                           rotator.roll, rotator.pitch, rotator.yaw]
                    if use_relative:
                        cam_t = unreal.Transform()
                        cam_t.translation = loc_vec
                        cam_t.rotation = rotator.quaternion()
                        p_loc = p_locs[index]
                        p_rot = p_rots[index]  # [pitch, yaw, roll]
                        p_scale = p_scales[index] if index < len(p_scales) else [1.0, 1.0, 1.0]
                        parent_t = unreal.Transform()
                        parent_t.translation = unreal.Vector(p_loc[0], p_loc[1], p_loc[2])
                        parent_t.rotation = unreal.Rotator(
                            roll=p_rot[2], pitch=p_rot[0], yaw=p_rot[1]).quaternion()
                        parent_t.scale3d = unreal.Vector(p_scale[0], p_scale[1], p_scale[2])
                        rel = unreal.MathLibrary.make_relative_transform(cam_t, parent_t)
                        loc_vec = rel.translation
                        rotator = rel.rotation.rotator()
                    values = [rotator.pitch, rotator.yaw, rotator.roll]
                    if previous is not None:
                        # keep successive keys continuous across +/-180 wrap
                        for axis in range(3):
                            while values[axis] - previous[axis] > 180.0:
                                values[axis] -= 360.0
                            while values[axis] - previous[axis] < -180.0:
                                values[axis] += 360.0
                    previous = values
                    pitch, yaw, roll = values
                    row = [loc_vec.x, loc_vec.y, loc_vec.z,
                           roll, pitch, yaw, 1.0, 1.0, 1.0]
                    if cam_const is None:
                        cam_const = list(row)
                    for channel in range(9):
                        cam_keys[channel].append(row[channel])

                cam_track = {'const': cam_const or [0] * 9, 'keys': {}}
                cam_seeds = set(int(v) for v in (camera_data.get('key_frames') or []))
                authored_keys = PAYLOAD.get('key_mode') == 'AUTHORED'
                CAM_TOLERANCE = [0.05, 0.05, 0.05, 0.05, 0.05, 0.05,
                                 0.0005, 0.0005, 0.0005]
                for channel, values in cam_keys.items():
                    if not values:
                        continue
                    value_range = max(values) - min(values)
                    if authored_keys:
                        # below the perception floor = numerical noise from
                        # the relative-transform math: keep it constant
                        if value_range <= CAM_TOLERANCE[channel]:
                            continue
                        tolerance = max(CAM_TOLERANCE[channel], 0.001 * value_range)
                        pairs = reduce_channel_keys(values, cam_seeds, tolerance)
                    else:
                        if value_range <= 1e-5:
                            continue
                        pairs = [[offset, value]
                                 for offset, value in enumerate(values)]
                    cam_track['keys'][str(channel)] = pairs
                if cam_world_const:
                    # place the actor in WORLD space (keys may be parent-space);
                    # unreal.Rotator takes (roll, pitch, yaw) positionally
                    cam_actor.set_actor_location_and_rotation(
                        unreal.Vector(cam_world_const[0], cam_world_const[1], cam_world_const[2]),
                        unreal.Rotator(roll=cam_world_const[3], pitch=cam_world_const[4],
                                       yaw=cam_world_const[5]),
                        False, False)

                def name_binding(binding, name):
                    """Safe spawnable naming: sequence-namespace only, never
                    a level actor rename (fatal on clash)."""
                    binding.set_display_name(name)
                    try:
                        template = binding.get_object_template()
                        outer = template.get_outer() if template is not None else None
                        if outer is not None and unreal.find_object(outer, name) is None:
                            binding.set_name(name)
                    except Exception as error:
                        log('binding not renamed for %s: %s' % (name, str(error)[:60]))

                def add_attach_track(child_binding, parent_binding):
                    """Attach one binding to another: spawnables spawn
                    parentless, this is how their parent-space keys resolve."""
                    attach_track = child_binding.add_track(unreal.MovieScene3DAttachTrack)
                    attach_section = attach_track.add_section()
                    attach_section.set_range(0, frame_count)
                    try:
                        target_id = sequence.make_binding_id(parent_binding)
                    except Exception:
                        target_id = unreal.MovieSceneObjectBindingID()
                        target_id.set_editor_property('guid', parent_binding.get_id())
                    try:
                        attach_section.set_constraint_binding_id(target_id)
                    except Exception:
                        attach_section.set_editor_property('constraint_binding_id', target_id)
                    for rule in ('attachment_location_rule', 'attachment_rotation_rule',
                                 'attachment_scale_rule'):
                        try:
                            attach_section.set_editor_property(
                                rule, unreal.AttachmentRule.KEEP_RELATIVE)
                        except Exception:
                            pass

                # 6b-bis) By default the camera travels INSIDE the sequence
                # as a spawnable (with its rig nulls when the chain is
                # private), so each sequence owns its camera package
                cam_binding = None
                camera_converted = False
                if will_convert_camera:
                    try:
                        cam_binding = sequence.add_spawnable_from_instance(cam_actor)
                        camera_converted = True
                        name_binding(cam_binding, cam_label)
                    except Exception as error:
                        log('camera spawnable conversion failed, keeping a level '
                            'camera: %s' % str(error)[:80])
                        rig_chain = []

                if not camera_converted:
                    # level camera: reproduce the Blender hierarchy with a
                    # real attachment. Parent-space keys make it always safe;
                    # world-space keys only when neither side moves.
                    rig_chain = []
                    if parent_actor is not None:
                        cam_moves = bool(cam_track['keys'])
                        parent_moves = bool(anim_tracks.get(parent_name, {}).get('keys'))
                        if use_relative or (not cam_moves and not parent_moves):
                            cam_actor.attach_to_actor(
                                parent_actor, '',
                                unreal.AttachmentRule.KEEP_WORLD,
                                unreal.AttachmentRule.KEEP_WORLD,
                                unreal.AttachmentRule.KEEP_WORLD,
                                False)
                            result['attached'] += 1
                        else:
                            log('camera "%s" left unattached from "%s": its sequence keys are '
                                'world-space and would be corrupted under a moving parent. '
                                'Bake the camera in Blender (Bake Camera Animation) or use '
                                'Make Sequence Self-Contained.' % (cam_label, parent_name))
                    cam_binding = sequence.add_possessable(cam_actor)

                key_transform(cam_binding, cam_track)
                result['sequence_bindings'] += 1

                if camera_converted:
                    # rig nulls -> spawnables (replacing their possessable
                    # binding when they had keys, else a constant track from
                    # the level transform)
                    converted = {}
                    for null_name in rig_chain:
                        null_actor = spawned.get(null_name)
                        try:
                            null_binding = sequence.add_spawnable_from_instance(null_actor)
                        except Exception as error:
                            log('rig null %s not converted: %s'
                                % (null_name, str(error)[:80]))
                            continue
                        name_binding(null_binding, null_name)
                        track_data = anim_tracks.get(null_name)
                        if not track_data or not track_data.get('keys'):
                            target_name = parent_map.get(null_name) or ''
                            attached_target = (target_name in rig_chain
                                               or target_name in object_bindings)
                            if attached_target:
                                root = null_actor.root_component
                                loc = root.relative_location
                                rot = root.relative_rotation
                                scale = root.relative_scale3d
                            else:
                                loc = null_actor.get_actor_location()
                                rot = null_actor.get_actor_rotation()
                                scale = null_actor.get_actor_scale3d()
                            track_data = {'const': [loc.x, loc.y, loc.z,
                                                    rot.roll, rot.pitch, rot.yaw,
                                                    scale.x, scale.y, scale.z],
                                          'keys': {}}
                        key_transform(null_binding, track_data)
                        old_binding = object_bindings.pop(null_name, None)
                        if old_binding is not None:
                            old_binding.remove()
                        else:
                            result['sequence_bindings'] += 1
                        converted[null_name] = null_binding

                    # attach tracks: camera -> parent, nulls -> their parents
                    for null_name in list(converted):
                        target_name = parent_map.get(null_name) or ''
                        target = (converted.get(target_name)
                                  or object_bindings.get(target_name))
                        if target is not None and preserve_hierarchy:
                            add_attach_track(converted[null_name], target)
                    if parent_actor is not None:
                        target = (converted.get(parent_name)
                                  or object_bindings.get(parent_name))
                        cam_moves = bool(cam_track['keys'])
                        parent_moves = bool(anim_tracks.get(parent_name, {}).get('keys'))
                        if target is not None and (
                                use_relative or (not cam_moves and not parent_moves)):
                            add_attach_track(cam_binding, target)

                    # the sequence owns the camera package now: remove the
                    # level copies (their transforms live in the bindings)
                    for gone_name in list(converted):
                        gone = spawned.pop(gone_name, None)
                        if gone is not None:
                            try:
                                subsystem.destroy_actor(gone)
                            except Exception:
                                pass
                    try:
                        subsystem.destroy_actor(cam_actor)
                    except Exception:
                        pass
                    result['camera_package'] = 1 + len(converted)

                # Animated depth of field is NOT keyed as a sequence track
                # for now: every python route tried (component possessable of
                # a spawnable template, float track with a component-traversing
                # property path) produced a sequence that CRASHES the editor
                # when opened in Sequencer (UE 5.8). The per-frame focus curve
                # is still sampled and shipped in the payload; the static
                # first-frame value is applied to the camera instead.
                focus_curve = (camera_data.get('focus') or {}).get('distances_cm') or []
                if focus_curve and max(focus_curve) - min(focus_curve) > 0.1:
                    log('animated focus detected: carried as static first-frame '
                        'focus for now (sequence focus tracks are unsafe to '
                        'author from python in UE 5.8)')

                # Camera Cuts track so the sequence renders through this camera
                try:
                    cut_track = sequence.add_track(unreal.MovieSceneCameraCutTrack)
                    cut_section = cut_track.add_section()
                    cut_section.set_range(0, frame_count)
                    try:
                        binding_id = sequence.make_binding_id(cam_binding)
                    except Exception:
                        binding_id = unreal.MovieSceneObjectBindingID()
                        binding_id.set_editor_property('guid', cam_binding.get_id())
                    cut_section.set_camera_binding_id(binding_id)
                    result['camera_cut'] = True
                except Exception as error:
                    log('camera cut track failed: %s' % error)

            unreal.EditorAssetLibrary.save_asset(full_seq_path)
            result['sequence'] = full_seq_path

    log('B2UE_RESULT ' + json.dumps(result))
except Exception:
    log('B2UE_ERROR ' + traceback.format_exc().replace('\\n', ' | '))
'''


def get_staging_dir():
    staging = os.path.join(tempfile.gettempdir(), 'KelitToolkit')
    os.makedirs(staging, exist_ok=True)
    return staging


def get_scene_name():
    if bpy.data.filepath:
        stem = os.path.splitext(os.path.basename(bpy.data.filepath))[0]
    else:
        stem = bpy.context.scene.name or 'BlenderScene'
    return sanitize_prim_name(stem)


def parse_sync_result(output):
    """Extract the B2UE_RESULT / B2UE_ERROR line from the Unreal response."""
    if not output:
        return None, None
    for line in str(output).splitlines():
        if 'B2UE_ERROR' in line:
            return None, line.split('B2UE_ERROR', 1)[1].strip()
        if 'B2UE_RESULT' in line:
            try:
                return json.loads(line.split('B2UE_RESULT', 1)[1].strip()), None
            except (json.JSONDecodeError, IndexError):
                # the run completed but its report is unreadable - say so
                # instead of silently pretending everything is unknown-fine
                return None, 'completed, but the result line was unreadable'
    return None, None


# ============================================================================
# OPERATORS
# ============================================================================

class UNREAL_OT_usd_scene_sync(bpy.types.Operator):
    """Send the scene to Unreal via USD: separate assets with material fidelity,
    one actor per object at its exact position, hierarchy rebuilt with
    attachments. Standalone: needs only UE's USD Importer plugin and Python
    remote execution enabled. Note: re-syncing rebuilds the LevelSequence
    from scratch - tracks added to it by hand are cleared"""
    bl_idname = "kelit_toolkit.usd_scene_sync"
    bl_label = "Send Scene via USD"
    bl_options = {'REGISTER'}

    source: bpy.props.EnumProperty(
        name="Source",
        description="Which objects to send",
        items=[
            ('SELECTED', "Selection (+ parents/children)", "Selected objects, expanded to their full hierarchy"),
            ('EXPORT_COLLECTION', "Export Collection", "Everything in the 'Export' collection"),
        ],
        default='SELECTED'
    )

    place_in_level: bpy.props.BoolProperty(
        name="Place Actors in Level",
        description="Spawn one actor per object at its Blender world position and rebuild the hierarchy",
        default=True
    )

    replace_existing: bpy.props.BoolProperty(
        name="Replace Sent Objects",
        description="Replace the previously synced versions of the objects being sent. "
                    "Other objects synced earlier from this .blend are kept in the level",
        default=True
    )

    import_materials: bpy.props.BoolProperty(
        name="Import Materials",
        description="Import the USD materials (better fidelity than FBX)",
        default=True
    )

    include_skeletal: bpy.props.BoolProperty(
        name="Skeletal Meshes + Animation",
        description="Detect armatures in the selection and send them as "
                    "SkeletalMesh assets (armature + skinned meshes + active "
                    "action as AnimSequence), spawned in the level with their "
                    "animation assigned",
        default=True
    )

    include_animation: bpy.props.BoolProperty(
        name="Include Animation",
        description="Bake the scene animation into a native Unreal LevelSequence "
                    "(one transform track per moving object) and send the active "
                    "camera as a CineCameraActor with a Camera Cuts track",
        default=False
    )

    include_camera: bpy.props.BoolProperty(
        name="Include Camera",
        description="Send the scene's active camera as an animated CineCameraActor",
        default=True
    )

    camera_spawnable: bpy.props.BoolProperty(
        name="Camera as Spawnable",
        description="Carry the camera - and the null chain that only drives it - "
                    "inside the sequence as spawnables with attach tracks, so every "
                    "sequence owns its camera package and opens in any level. "
                    "Untick to keep the camera as a level actor",
        default=True
    )

    preserve_hierarchy: bpy.props.BoolProperty(
        name="Preserve Hierarchy (Animated)",
        description="Attach animated actors (camera included) to their Blender "
                    "parents and bake their keys in parent space, reproducing the "
                    "full rig hierarchy in the level. Untick to get flat actors "
                    "with world-space keys instead",
        default=True
    )

    key_mode: bpy.props.EnumProperty(
        name="Keyframes",
        description="How the sequence keys are written",
        items=[
            ('AUTHORED', "Blender Keys (editable)",
             "Keep the keyframes authored in Blender and only insert extra "
             "keys where the Unreal curve would drift beyond a tiny tolerance "
             "(constraint- or parent-driven motion). Clean, editable curves"),
            ('BAKED', "Every Frame (exact)",
             "One key per frame: bit-exact motion, but dense curves that are "
             "hard to edit in the Sequencer"),
        ],
        default='AUTHORED'
    )

    fix_two_sided: bpy.props.BoolProperty(
        name="Force Double-Sided Materials",
        description="Unreal creates '*_TwoSided' material instances for USD doubleSided "
                    "meshes but leaves the override disabled, so they still render single-sided. "
                    "Enable this only if flat, zero-thickness surfaces disappear - double-sided "
                    "shading costs more and defeats some optimisations",
        default=False
    )

    def _base_objects(self, context):
        if self.source == 'EXPORT_COLLECTION':
            export_collection = bpy.data.collections.get('Export')
            return list(export_collection.all_objects) if export_collection else []
        base = list(context.selected_objects)
        if not base:
            export_collection = bpy.data.collections.get('Export')
            if export_collection:
                base = list(export_collection.all_objects)
        return base

    def _resolve_objects(self, context):
        base = self._base_objects(context)
        objects = [obj for obj in collect_scene_objects(base) if is_exportable(obj)]
        if self.include_animation:
            # cache-driven duplicates cannot be baked to transform keys
            objects = [obj for obj in objects if not is_cache_driven(obj)]
        return objects

    def _dialog_summary(self, context):
        """Scene summary for draw(), cached: draw() runs on every dialog
        redraw and a full-scene recompute froze the popup on large scenes."""
        # include_animation is part of the key: _resolve_objects filters the
        # cache-driven objects only in that mode
        key = (self.source, self.include_skeletal, self.include_animation)
        cached = getattr(self, '_summary_cache', None)
        if cached is not None and cached[0] == key:
            return cached[1]

        objects = self._resolve_objects(context)
        skeletal_assets = []
        if self.include_skeletal:
            skeletal_assets, objects = split_skeletal_assets(objects)
        # the raw pool follows the chosen source (it used to always read the
        # selection, giving wrong skip counts in Export-collection mode)
        raw = collect_scene_objects(self._base_objects(context))
        summary = {
            'mesh_count': sum(1 for o in objects if o.type == 'MESH'),
            'empty_count': sum(1 for o in objects if o.type == 'EMPTY'),
            'skeletal_names': [asset['name'] for asset in skeletal_assets],
            'hidden': sum(1 for o in raw if not is_exportable(o)),
            'cache_skipped': sum(1 for o in raw
                                 if is_exportable(o) and is_cache_driven(o)),
        }
        self._summary_cache = (key, summary)
        return summary

    # dialog options remembered per scene (saved in execute, restored here)
    REMEMBERED_OPTIONS = (
        'source', 'place_in_level', 'replace_existing', 'import_materials',
        'fix_two_sided', 'include_skeletal', 'include_animation',
        'include_camera', 'camera_spawnable', 'preserve_hierarchy', 'key_mode',
    )

    def invoke(self, context, event):
        settings = context.scene.kelit_toolkit_settings
        if settings.sync_options_saved:
            for name in self.REMEMBERED_OPTIONS:
                stored = getattr(settings, f'sync_{name}', None)
                if stored is not None and stored != '':
                    try:
                        setattr(self, name, stored)
                    except TypeError:
                        pass  # stored enum value no longer exists
        return context.window_manager.invoke_props_dialog(self, width=400)

    def _remember_options(self, context):
        settings = context.scene.kelit_toolkit_settings
        for name in self.REMEMBERED_OPTIONS:
            setattr(settings, f'sync_{name}', getattr(self, name))
        settings.sync_options_saved = True

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "source")
        layout.prop(self, "place_in_level")
        layout.prop(self, "replace_existing")
        layout.prop(self, "import_materials")
        if self.import_materials:
            layout.prop(self, "fix_two_sided")

        layout.separator()
        layout.prop(self, "include_skeletal")
        layout.prop(self, "include_animation")
        if self.include_animation:
            row = layout.row()
            row.prop(self, "include_camera")
            if self.include_camera:
                row = layout.row()
                row.prop(self, "camera_spawnable")
            row = layout.row()
            row.prop(self, "preserve_hierarchy")
            row = layout.row()
            row.prop(self, "key_mode", text="Keys")

        summary = self._dialog_summary(context)
        box = layout.box()
        box.label(text=f"{summary['mesh_count']} mesh(es), "
                       f"{summary['empty_count']} empty/null(s)",
                  icon='OUTLINER_COLLECTION')
        if summary['skeletal_names']:
            names = ", ".join(summary['skeletal_names'][:3])
            box.label(text=f"{len(summary['skeletal_names'])} skeletal: {names}",
                      icon='ARMATURE_DATA')

        if summary['hidden']:
            box.label(text=f"{summary['hidden']} render-disabled object(s) skipped",
                      icon='INFO')

        if self.include_animation:
            scene = context.scene
            box.label(text=f"Frames {scene.frame_start}-{scene.frame_end} "
                           f"@ {scene.render.fps} fps", icon='TIME')
            if summary['cache_skipped']:
                box.label(text=f"{summary['cache_skipped']} cache-driven object(s) skipped",
                          icon='INFO')
        box.label(text="Requires: UE open + 'USD Importer' plugin", icon='INFO')

    def execute(self, context):
        self._remember_options(context)
        wm = context.window_manager
        wm.progress_begin(0, 100)
        try:
            return self._execute_sync(context, wm)
        finally:
            wm.progress_end()
            try:
                context.workspace.status_text_set(None)
            except Exception:
                pass

    def _step(self, context, wm, value, label):
        wm.progress_update(value)
        try:
            context.workspace.status_text_set(f"Kelit Toolkit: {label}")
        except Exception:
            pass
        print(f"[KelitToolkit] {label}")

    def _execute_sync(self, context, wm):
        objects = self._resolve_objects(context)
        skeletal_assets = []
        if self.include_skeletal:
            skeletal_assets, objects = split_skeletal_assets(objects)
        mesh_objects = [o for o in objects if o.type == 'MESH']
        if not mesh_objects and not skeletal_assets:
            self.report({'WARNING'}, "No mesh objects to send")
            return {'CANCELLED'}

        settings = context.scene.kelit_toolkit_settings
        scene_name = get_scene_name()
        staging_dir = get_staging_dir()
        # binary .usd (crate): much smaller/faster than ASCII for heavy meshes
        usd_path = os.path.join(staging_dir, f'{scene_name}.usd')
        script_path = os.path.join(staging_dir, f'{scene_name}_ue_import.py')

        # 1) Export USD with kind tags + deterministic mesh prim names
        self._step(context, wm, 10, "exporting USD...")
        tagged = {'component': 0, 'group': 0}
        asset_hints = {}
        data_hints = {}
        if mesh_objects:
            try:
                tagged, asset_hints, data_hints = export_usd_hierarchy(
                    usd_path, objects, self.import_materials)
            except RuntimeError as error:
                self.report({'ERROR'}, f"USD export failed: {error}")
                return {'CANCELLED'}
        else:
            usd_path = None
        self._step(context, wm, 25, "USD exported")

        # 1b) One USD per skeletal asset (armature + skinned meshes + action)
        skeletal_payload = []
        for asset in skeletal_assets:
            asset_name = sanitize_prim_name(asset_filename(asset))
            skel_path = os.path.join(staging_dir, f'{scene_name}_{asset_name}.usd')
            try:
                export_skeletal_usd(skel_path, asset)
            except RuntimeError as error:
                self.report({'ERROR'}, f"Skeletal USD export failed ({asset['name']}): {error}")
                return {'CANCELLED'}
            matrix = asset['root'].matrix_world
            location, euler, scale = decompose_signed(matrix)
            skeletal_payload.append({
                'name': asset['name'],
                'asset_name': asset_name,
                'usd_file': skel_path.replace('\\', '/'),
                'location': convert_blender_to_unreal_location(location),
                'rotation': convert_blender_rotation_to_unreal_rotation(euler),
                'scale': list(scale),
            })

        # 2) Build the payload + Unreal-side script
        graph = build_scene_graph(objects)
        for entry in graph:
            # the data-name index wins: object names like 'Cube.001' and
            # 'Cube_001' sanitize to the same key, mesh data names do not
            hint = None
            if entry.get('mesh_name'):
                hint = data_hints.get(sanitize_prim_name(entry['mesh_name']))
            if hint is None:
                hint = asset_hints.get(sanitize_prim_name(entry['name']))
            entry['asset_hint'] = hint

        content_root = (settings.usd_content_folder or '/Game/BlenderSync').rstrip('/')
        scene = context.scene
        payload = {
            'usd_file': usd_path.replace('\\', '/') if usd_path else None,
            'skeletal': skeletal_payload,
            # the USD importer creates a '<scene_name>/' sub-folder by itself
            'content_folder': content_root,
            'scene_name': scene_name,
            'outliner_folder': scene_name,
            'place_in_level': self.place_in_level,
            'replace_existing': self.replace_existing,
            'import_materials': self.import_materials,
            'fix_two_sided': self.fix_two_sided,
            'objects': graph,
            'fps': scene.render.fps / max(scene.render.fps_base, 1e-6),
            'frame_start': scene.frame_start,
            'frame_end': scene.frame_end,
            'animation': None,
            'camera': None,
            'preserve_hierarchy': self.preserve_hierarchy,
            'key_mode': self.key_mode,
            'camera_spawnable': self.camera_spawnable,
        }

        # 2b) Bake the animation - in parent space when the hierarchy is kept,
        # in world space otherwise. The camera (and its ancestors) must be in
        # the temporarily-visible set too: a viewport-hidden camera is
        # excluded from the depsgraph and would bake frozen motion.
        if self.include_animation:
            self._step(context, wm, 35, "baking animation...")
        if self.include_animation:
            camera_obj = scene.camera
            with_camera = (self.include_camera and camera_obj is not None
                           and camera_obj.type == 'CAMERA')
            bake_visible = list(objects)
            camera_sampler = None
            if with_camera:
                chain = camera_obj
                while chain is not None:
                    if chain not in bake_visible:
                        bake_visible.append(chain)
                    chain = chain.parent
                camera_sampler = CameraSampler(camera_obj, {o.name for o in objects})
            with temporarily_visible(bake_visible):
                payload['animation'] = bake_world_animation(
                    context, objects, scene.frame_start, scene.frame_end,
                    relative_to_parent=self.preserve_hierarchy,
                    key_mode=self.key_mode,
                    camera_sampler=camera_sampler)
                if with_camera:
                    payload['camera'] = build_camera_payload(
                        context, camera_obj, scene.frame_start, scene.frame_end,
                        {o.name for o in objects}, sampler=camera_sampler)

            # Spawn actors where the sequence starts, not at whatever frame the
            # .blend happened to be saved on, so the level's rest state matches
            # frame 0 of the sequence instead of jumping when it evaluates.
            # Always the WORLD transform: track consts may be in parent space.
            for entry in graph:
                track = payload['animation'].get(entry['name'])
                if not track:
                    continue
                const = track.get('world_const') or track['const']
                entry['location'] = const[0:3]
                # spawn takes a list, read by Unreal as (pitch, yaw, roll)
                entry['rotation'] = [const[4], const[5], const[3]]
                entry['scale'] = const[6:9]

        script = UNREAL_SCRIPT_TEMPLATE.replace('__PAYLOAD__', json.dumps(json.dumps(payload)))
        with open(script_path, 'w', encoding='utf-8') as script_file:
            script_file.write(script)

        # 3) Run it in the Unreal Editor through the remote-execution channel
        self._step(context, wm, 55,
                   "sending to Unreal (import + placement, this can take a while)...")
        script_fwd = script_path.replace('\\', '/')
        success, output = run_unreal_python([
            f'exec(compile(open("{script_fwd}", encoding="utf-8").read(), "b2ue_usd_sync", "exec"))'
        ])
        self._step(context, wm, 90, "Unreal answered, reading the result...")

        if not success:
            self.report({'ERROR'}, f"Unreal connection failed: {output}")
            # open the doctor: it explains why and can fix the project files
            # (only when no editor answered, the case it diagnoses)
            from .unreal_link import is_no_editor_error
            if is_no_editor_error(output):
                bpy.ops.kelit_toolkit.connection_doctor('INVOKE_DEFAULT')
            return {'CANCELLED'}

        result, error = parse_sync_result(output)
        if error and 'USD Importer' in error:
            self.report({'ERROR'}, "The USD Importer plugin is not enabled in this "
                                   "Unreal project")
            bpy.ops.kelit_toolkit.connection_doctor('INVOKE_DEFAULT')
            return {'CANCELLED'}
        if error and 'unreadable' in error:
            # the run completed - only its report line failed to parse
            self.report({'WARNING'}, "Sync completed but the result summary was "
                                     "unreadable - check the UE Output Log")
            return {'FINISHED'}
        if error:
            print(f"USD Scene Sync - Unreal error:\n{error}")
            self.report({'ERROR'}, "Unreal-side error - see console / UE Output Log")
            return {'CANCELLED'}

        if result:
            missing = result.get('missing') or []
            message = (f"{result.get('assets', 0)} asset(s), "
                       f"{result.get('spawned', 0)} actor(s) placed, "
                       f"{result.get('attached', 0)} attached")
            if result.get('skeletal'):
                message += f", {result['skeletal']} skeletal (anim)"
            if result.get('two_sided_fixed'):
                message += f", {result['two_sided_fixed']} two-sided fix"
            if result.get('sequence'):
                message += (f" - LevelSequence: {result.get('sequence_bindings', 0)} binding(s)"
                            + (" + camera cut" if result.get('camera_cut') else ""))
                print(f"USD Scene Sync - sequence created: {result['sequence']}")
            if missing:
                message += f" - {len(missing)} mesh(es) not matched (see console)"
                print(f"USD Scene Sync - unmatched meshes: {missing}")
            self.report({'INFO'}, message)
        else:
            self.report({'INFO'}, f"Sync sent ({len(mesh_objects)} meshes, "
                                  f"{len(skeletal_payload)} skeletal) - check the UE Output Log")
        return {'FINISHED'}


SPAWNABLE_SCRIPT = '''
import json
import traceback
import unreal

PAYLOAD = json.loads(__PAYLOAD__)


def log(message):
    text = "[B2UE-SPAWN] " + str(message)
    unreal.log(text)
    print(text)


try:
    EAL = unreal.EditorAssetLibrary
    seq_path = PAYLOAD["sequence"]
    result = {"converted": 0, "skipped": [], "camera_cut": False,
              "actors_removed": 0, "attach_tracks": 0}

    sequence = EAL.load_asset(seq_path)
    if sequence is None:
        raise RuntimeError("sequence not found: " + seq_path)

    time_unit = getattr(unreal, "MovieSceneTimeUnit", None) or unreal.SequenceTimeUnit
    display_rate = time_unit.DISPLAY_RATE
    subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    # write keys with the same explicit AUTO interpolation the sync used, so
    # sparse (AUTHORED) curves survive the copy identically
    KEY_INTERP = getattr(unreal, "MovieSceneKeyInterpolation", None)
    KEY_INTERP = getattr(KEY_INTERP, "AUTO", None) if KEY_INTERP else None

    # the actors this sequence was built from, by label - scene-scoped, so a
    # level holding several synced .blends only contributes THIS scene's
    # actors (and only those get removed as level copies below)
    scene_tag = PAYLOAD.get("scene_tag") or ""
    actors = {}
    for actor in subsystem.get_all_level_actors():
        tags = [str(t) for t in actor.tags]
        if scene_tag and scene_tag not in tags:
            continue
        for tag in tags:
            if tag.startswith("B2UE:obj:"):
                actors[tag[len("B2UE:obj:"):]] = actor

    # Snapshot of the level's attach graph and of each actor's transform
    # RELATIVE to its attach parent (for an unattached root this equals its
    # world transform). The level graph already encodes the key semantics:
    # attached actors carry parent-space keys (preserve_hierarchy), free
    # actors carry world-space keys - so replaying the very same graph as
    # Attach Tracks keeps the motion identical in both modes.
    labels_by_path = {}
    for label, actor in actors.items():
        labels_by_path[actor.get_path_name()] = label
    attach_parents = {}
    rel_transforms = {}
    for label, actor in actors.items():
        parent = actor.get_attach_parent_actor()
        if parent is not None:
            parent_label = labels_by_path.get(parent.get_path_name())
            if parent_label:
                attach_parents[label] = parent_label
        root = actor.root_component
        if root is not None:
            loc = root.relative_location
            rot = root.relative_rotation
            scale = root.relative_scale3d
            rel_transforms[label] = [loc.x, loc.y, loc.z,
                                     rot.roll, rot.pitch, rot.yaw,
                                     scale.x, scale.y, scale.z]

    def read_track(binding):
        """Capture the transform keys of a binding before it is replaced."""
        for track in binding.get_tracks():
            if not isinstance(track, unreal.MovieScene3DTransformTrack):
                continue
            for section in track.get_sections():
                channels = section.get_all_channels()
                data = []
                for channel in channels:
                    keys = []
                    for key in channel.get_keys():
                        frame_time = key.get_time(display_rate)
                        keys.append((frame_time.frame_number.value,
                                     float(frame_time.sub_frame),
                                     float(key.get_value())))
                    data.append(keys)
                return data, section.get_start_frame(), section.get_end_frame()
        return None, None, None

    camera_binding = None
    spawnables = {}
    for binding in list(sequence.get_bindings()):
        name = binding.get_name()
        try:
            if binding.get_object_template() is not None:
                # already a spawnable (e.g. the synced camera package)
                spawnables[name] = binding
                continue
        except Exception:
            pass
        actor = actors.get(name)
        if actor is None:
            result["skipped"].append(name)
            continue

        channel_data, start, end = read_track(binding)

        # a spawnable stores a copy of the actor inside the sequence asset,
        # so the sequence no longer depends on the level at all
        try:
            spawnable = sequence.add_spawnable_from_instance(actor)
        except Exception as error:
            result["skipped"].append("%s (%s)" % (name, str(error)[:60]))
            continue
        spawnable.set_display_name(name)
        # set_name renames the binding *inside the sequence asset*, which is
        # what the Outliner shows for a spawned actor. Unlike renaming a level
        # actor this is safe - the names live in the sequence's own namespace -
        # but a clash would still be fatal, so only rename onto a free name.
        try:
            template = spawnable.get_object_template()
            outer = template.get_outer() if template is not None else None
            if outer is not None and unreal.find_object(outer, name) is None:
                spawnable.set_name(name)
        except Exception as error:
            log("binding not renamed for %s: %s" % (name, str(error)[:60]))

        if channel_data:
            track = spawnable.add_track(unreal.MovieScene3DTransformTrack)
            section = track.add_section()
            section.set_range(start if start is not None else PAYLOAD["frame_start"],
                              end if end is not None else PAYLOAD["frame_end"])
            new_channels = section.get_all_channels()
            for index, keys in enumerate(channel_data):
                if index >= len(new_channels):
                    break
                for frame, sub_frame, value in keys:
                    if KEY_INTERP is not None:
                        new_channels[index].add_key(
                            unreal.FrameNumber(frame), value, sub_frame,
                            display_rate, KEY_INTERP)
                    else:
                        new_channels[index].add_key(
                            unreal.FrameNumber(frame), value, sub_frame, display_rate)

        if isinstance(actor, unreal.CineCameraActor):
            camera_binding = spawnable

        spawnables[name] = spawnable
        binding.remove()
        result["converted"] += 1

    # Actors that never had a binding (no keys of their own: static children
    # carried by an attachment, plain static meshes) must live in the sequence
    # too, or it is not self-contained - and the level cleanup below would
    # simply delete them. Their level transform becomes a constant transform
    # track so the spawned copy lands exactly where the level actor stood.
    for label, actor in actors.items():
        if label in spawnables:
            continue
        try:
            spawnable = sequence.add_spawnable_from_instance(actor)
        except Exception as error:
            result["skipped"].append("%s (%s)" % (label, str(error)[:60]))
            continue
        spawnable.set_display_name(label)
        try:
            template = spawnable.get_object_template()
            outer = template.get_outer() if template is not None else None
            if outer is not None and unreal.find_object(outer, label) is None:
                spawnable.set_name(label)
        except Exception as error:
            log("binding not renamed for %s: %s" % (label, str(error)[:60]))
        row = rel_transforms.get(label)
        if row:
            track = spawnable.add_track(unreal.MovieScene3DTransformTrack)
            section = track.add_section()
            section.set_range(sequence.get_playback_start(),
                              sequence.get_playback_end())
            channels = section.get_all_channels()
            for index in range(min(9, len(channels))):
                channels[index].add_key(
                    unreal.FrameNumber(0), float(row[index]), 0.0, display_rate)
        spawnables[label] = spawnable
        result["converted"] += 1

    # Replay the level's attach graph as native Attach Tracks: a spawnable
    # spawns parentless, so without these the parent-space keys of a
    # preserved hierarchy would be read as world-space and the motion would
    # collapse. KEEP_RELATIVE makes the transform track values be interpreted
    # in the parent's space, matching how the level actors were evaluated.
    for child_label, parent_label in attach_parents.items():
        child_binding = spawnables.get(child_label)
        parent_binding = spawnables.get(parent_label)
        if child_binding is None or parent_binding is None:
            continue
        try:
            attach_track = child_binding.add_track(unreal.MovieScene3DAttachTrack)
            attach_section = attach_track.add_section()
            attach_section.set_range(sequence.get_playback_start(),
                                     sequence.get_playback_end())
            try:
                binding_id = sequence.make_binding_id(parent_binding)
            except Exception:
                binding_id = unreal.MovieSceneObjectBindingID()
                binding_id.set_editor_property("guid", parent_binding.get_id())
            try:
                attach_section.set_constraint_binding_id(binding_id)
            except Exception:
                attach_section.set_editor_property("constraint_binding_id", binding_id)
            for rule in ("attachment_location_rule", "attachment_rotation_rule",
                         "attachment_scale_rule"):
                try:
                    attach_section.set_editor_property(
                        rule, unreal.AttachmentRule.KEEP_RELATIVE)
                except Exception:
                    pass
            result["attach_tracks"] += 1
        except Exception as error:
            log("attach track failed for %s -> %s: %s"
                % (child_label, parent_label, str(error)[:80]))

    # re-point the camera cut at the spawned camera
    if camera_binding is not None:
        for track in list(sequence.get_tracks()):
            if isinstance(track, unreal.MovieSceneCameraCutTrack):
                sequence.remove_track(track)
        cut_track = sequence.add_track(unreal.MovieSceneCameraCutTrack)
        cut_section = cut_track.add_section()
        # span the sequence's own playback range (end is exclusive)
        cut_section.set_range(sequence.get_playback_start(), sequence.get_playback_end())
        try:
            binding_id = sequence.make_binding_id(camera_binding)
        except Exception:
            binding_id = unreal.MovieSceneObjectBindingID()
            binding_id.set_editor_property("guid", camera_binding.get_id())
        cut_section.set_camera_binding_id(binding_id)
        result["camera_cut"] = True

    # the level copies are redundant now: the sequence carries its own.
    # Scene-scoped: other synced scenes' actors stay untouched.
    if PAYLOAD["remove_level_actors"]:
        for actor in list(subsystem.get_all_level_actors()):
            try:
                tags = [str(t) for t in actor.tags]
                if scene_tag and scene_tag not in tags:
                    continue
                if any(t.startswith("B2UE:obj:") for t in tags):
                    subsystem.destroy_actor(actor)
                    result["actors_removed"] += 1
            except Exception:
                pass

    EAL.save_asset(seq_path, only_if_is_dirty=False)
    log("B2UE_SPAWN_RESULT " + json.dumps(result))
except Exception:
    log("B2UE_SPAWN_ERROR " + traceback.format_exc().replace("\\n", " | "))
'''


class UNREAL_OT_usd_make_spawnable(bpy.types.Operator):
    """Turn the LevelSequence's bindings into spawnables, so the sequence
    carries its own actors and can be opened in any level. The synced
    hierarchy survives: level attachments are replayed as Attach Tracks,
    and static objects join the sequence with their level transform.
    Run this after 'Send Scene via USD' (and after building materials)"""
    bl_idname = "kelit_toolkit.usd_make_spawnable"
    bl_label = "Make Sequence Self-Contained"
    bl_options = {'REGISTER'}

    remove_level_actors: bpy.props.BoolProperty(
        name="Remove Level Copies",
        description="Delete the synced actors from the level once the sequence "
                    "carries its own. Leave off to keep them for layout work",
        default=True
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=380)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "remove_level_actors")
        box = layout.box()
        box.label(text="Spawnables live inside the sequence:", icon='INFO')
        box.label(text="no level dependency, no broken bindings")

    def execute(self, context):
        settings = context.scene.kelit_toolkit_settings
        scene = context.scene
        scene_name = get_scene_name()
        content_root = (settings.usd_content_folder or '/Game/BlenderSync').rstrip('/')

        payload = {
            'sequence': f'{content_root}/{scene_name}/LS_{scene_name}',
            'scene_tag': f'B2UE:{scene_name}',
            'frame_start': 0,
            'frame_end': scene.frame_end - scene.frame_start,
            'remove_level_actors': self.remove_level_actors,
        }
        script = SPAWNABLE_SCRIPT.replace('__PAYLOAD__', json.dumps(json.dumps(payload)))
        script_path = os.path.join(get_staging_dir(), f'{scene_name}_ue_spawnable.py')
        with open(script_path, 'w', encoding='utf-8') as handle:
            handle.write(script)

        success, output = run_unreal_python([
            f'exec(compile(open("{script_path.replace(chr(92), "/")}", encoding="utf-8").read(),'
            f' "b2ue_spawnable", "exec"))'
        ])
        if not success:
            self.report({'ERROR'}, f"Unreal connection failed: {output}")
            return {'CANCELLED'}

        data, error = None, None
        for line in str(output).splitlines():
            if 'B2UE_SPAWN_ERROR' in line:
                error = line.split('B2UE_SPAWN_ERROR', 1)[1].strip()
            elif 'B2UE_SPAWN_RESULT' in line:
                try:
                    data = json.loads(line.split('B2UE_SPAWN_RESULT', 1)[1].strip())
                except json.JSONDecodeError:
                    pass

        if error:
            print(f"Make Sequence Self-Contained - Unreal error:\n{error}")
            self.report({'ERROR'}, "Unreal-side error - see console / UE Output Log")
            return {'CANCELLED'}

        if data:
            message = f"{data['converted']} spawnable(s)"
            if data.get('attach_tracks'):
                message += f", {data['attach_tracks']} attach track(s)"
            if data.get('camera_cut'):
                message += " + camera cut"
            if data.get('actors_removed'):
                message += f", {data['actors_removed']} level actor(s) removed"
            if data.get('skipped'):
                message += f" - {len(data['skipped'])} skipped (see console)"
                print(f"Make Sequence Self-Contained - skipped: {data['skipped']}")
            self.report({'INFO'}, message)
        else:
            self.report({'INFO'}, "Conversion sent - check the UE Output Log")
        return {'FINISHED'}


class UNREAL_OT_usd_clear_synced(bpy.types.Operator):
    """Delete ALL actors previously synced from this .blend file in the
    Unreal level (assets in the Content Browser are kept)"""
    bl_idname = "kelit_toolkit.usd_clear_synced"
    bl_label = "Clear All Synced Actors"
    bl_options = {'REGISTER'}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        scene_tag = 'B2UE:' + get_scene_name()
        success, output = run_unreal_python([
            'import unreal',
            'subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)',
            'count = 0',
            'for actor in list(subsystem.get_all_level_actors()):',
            '    try:',
            f'        if "{scene_tag}" in [str(tag) for tag in actor.tags]:',
            '            subsystem.destroy_actor(actor)',
            '            count += 1',
            '    except Exception:',
            '        pass',
            "print('B2UE_CLEARED ' + str(count))",
        ])
        if not success:
            self.report({'ERROR'}, f"Unreal connection failed: {output}")
            return {'CANCELLED'}

        cleared = None
        for line in str(output).splitlines():
            if 'B2UE_CLEARED' in line:
                cleared = line.split('B2UE_CLEARED', 1)[1].strip()
                break
        self.report({'INFO'}, f"{cleared or '?'} synced actor(s) removed from the level")
        return {'FINISHED'}


class UNREAL_OT_usd_export_hierarchy(bpy.types.Operator):
    """Export the selection (with its full hierarchy) to a kind-tagged .usda
    file, ready for a manual 'Import Into Level' in Unreal"""
    bl_idname = "kelit_toolkit.usd_export_hierarchy"
    bl_label = "Export USD File (Hierarchy)"
    bl_options = {'REGISTER'}

    filepath: bpy.props.StringProperty(subtype='FILE_PATH')
    filter_glob: bpy.props.StringProperty(default='*.usda;*.usd', options={'HIDDEN'})

    def invoke(self, context, event):
        if not self.filepath:
            self.filepath = os.path.join(get_staging_dir(), f'{get_scene_name()}.usda')
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        objects = collect_scene_objects(list(context.selected_objects))
        if not objects:
            export_collection = bpy.data.collections.get('Export')
            if export_collection:
                objects = collect_scene_objects(list(export_collection.all_objects))
        if not any(o.type == 'MESH' for o in objects):
            self.report({'WARNING'}, "No mesh objects to export")
            return {'CANCELLED'}

        filepath = self.filepath
        if not filepath.lower().endswith(('.usd', '.usda')):
            filepath += '.usda'

        try:
            tagged, _hints, _data_hints = export_usd_hierarchy(filepath, objects)
        except RuntimeError as error:
            self.report({'ERROR'}, f"USD export failed: {error}")
            return {'CANCELLED'}

        self.report({'INFO'}, f"USD exported: {tagged['component']} component(s), "
                              f"{tagged['group']} group(s) -> {filepath}")
        return {'FINISHED'}


classes = (
    UNREAL_OT_usd_scene_sync,
    UNREAL_OT_usd_make_spawnable,
    UNREAL_OT_usd_clear_synced,
    UNREAL_OT_usd_export_hierarchy,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
