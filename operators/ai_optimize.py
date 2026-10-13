"""Optimize AI Meshes: batch decimation of AI-generated props (Meshy, Tripo,
Hunyuan...). UV layers are retained, but mapping, normals and silhouettes can
change. Compare from the final camera before applying a face budget.

Remeshing is deliberately not offered: on folded cloth with close layers and
a non-manifold source, it opened holes (63 to 1951 open edges in a test)."""

import bmesh
import bpy

from ..utils import mesh_users
from .materials import viewports_to_solid
from .report import open_report, set_report

MODIFIER_NAME = "KT_Optimize"
SEAM_GROUP = "KT_UVSeams"
EXCLUDE_PROPERTY = "kt_optimize_exclude"
HERO_PROPERTY = "kt_optimize_min_ratio"
REFERENCE_PROPERTY = "kt_optimize_reference"
# set on mesh data produced by a destructive Apply: such a mesh is already
# decimated and must never be saved as the "_KT_Original" reference
APPLIED_PROPERTY = "kt_optimize_applied"
DEFORMING = {'ARMATURE', 'LATTICE', 'CURVE', 'HOOK', 'MESH_DEFORM', 'SURFACE_DEFORM',
             'CAST', 'SIMPLE_DEFORM', 'WARP', 'WAVE', 'LAPLACIANDEFORM', 'CORRECTIVE_SMOOTH'}


def mesh_stats(mesh):
    """(faces, triangle share, surface area in mesh units^2)."""
    import numpy as np
    count = len(mesh.polygons)
    if not count:
        return 0, 0.0, 0.0
    totals = np.empty(count, dtype=np.int32)
    mesh.polygons.foreach_get('loop_total', totals)
    areas = np.empty(count, dtype=np.float32)
    mesh.polygons.foreach_get('area', areas)
    return count, float((totals == 3).mean()), float(areas.sum())


def skip_reason(obj):
    if obj.library is not None or obj.data.library is not None:
        return "linked"
    if obj.get(EXCLUDE_PROPERTY):
        return "excluded"
    if obj.data.shape_keys is not None:
        return "shape keys"
    if obj.parent is not None and obj.parent.type == 'ARMATURE' and obj.parent_type in {'ARMATURE', 'OBJECT'}:
        if any(m.type == 'ARMATURE' for m in obj.modifiers) or obj.parent_type == 'ARMATURE':
            return "armature"
    for modifier in obj.modifiers:
        if modifier.name == MODIFIER_NAME:
            if modifier.type != 'DECIMATE':
                return "KT_Optimize name used by another modifier"
            continue
        if modifier.type == 'DECIMATE':
            return "another Decimate modifier"
        if modifier.type in DEFORMING:
            return "armature" if modifier.type == 'ARMATURE' else "deforming modifier"
        if modifier.show_viewport or modifier.show_render:
            return "other active modifier (preview/apply would differ)"
    return None


def world_surface_area(mesh, obj, unit_scale=1.0, local_area=None):
    """Area including nonuniform scale, parents and scene units."""
    import numpy as np
    matrix = np.array(obj.matrix_world.to_3x3())
    gram = matrix.T @ matrix
    scale_squared = float(np.trace(gram) / 3.0)
    if np.allclose(gram, np.eye(3) * scale_squared, rtol=1e-6,
                   atol=max(scale_squared, 1e-30) * 1e-6):
        # Common instance case: avoid re-triangulating a large mesh per user.
        if local_area is None:
            local_area = mesh_stats(mesh)[2]
        return local_area * scale_squared * unit_scale ** 2
    mesh.calc_loop_triangles()
    vertices = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
    mesh.vertices.foreach_get('co', vertices)
    vertices = vertices.reshape((-1, 3)) @ matrix.T
    triangles = np.empty(len(mesh.loop_triangles) * 3, dtype=np.int32)
    mesh.loop_triangles.foreach_get('vertices', triangles)
    points = vertices[triangles.reshape((-1, 3))]
    return float(np.linalg.norm(np.cross(points[:, 1] - points[:, 0],
                                        points[:, 2] - points[:, 0]), axis=1).sum() * 0.5
                 * unit_scale ** 2)


def find_candidates(objects, settings):
    """One plan per mesh, using only eligible objects in the chosen scope."""
    seen = {}
    skipped = {}
    pool = {obj.as_pointer(): obj for obj in objects if obj.type == 'MESH'}
    if settings.get('include_instances', False):
        for obj in list(pool.values()):
            for user in mesh_users(obj.data):
                pool[user.as_pointer()] = user
    eligible = {}
    for obj in pool.values():
        reason = skip_reason(obj)
        if reason:
            skipped[obj.name_full] = reason
        else:
            eligible.setdefault(obj.data.name_full, []).append(obj)
    for users in eligible.values():
        obj = users[0]
        if obj.type != 'MESH' or obj.data is None:
            continue
        mesh = obj.data
        faces, tri_share, area = mesh_stats(mesh)
        if faces == 0:
            continue
        world_area = max(world_surface_area(mesh, user, settings.get('unit_scale', 1.0), area)
                         for user in users)
        if world_area <= 1e-9:
            # zero scale (or a degenerate mesh): no surface means no budget,
            # and an infinite density would decimate it to the minimum
            for user in users:
                skipped[user.name_full] = "zero scale or no surface"
            continue
        density = faces / world_area
        dense = tri_share >= settings['min_tri_share'] and density >= settings['density']
        existing = any(user.modifiers.get(MODIFIER_NAME) for user in users)
        if not (dense or faces >= settings['max_faces_absolute'] or existing):
            continue
        target = world_area * settings['budget']
        target = max(settings['min_faces'], min(settings['max_faces'], target))
        ratio = max(0.01, min(1.0, target / faces))
        # "hero" objects (seen up close) carry a minimum ratio: the highest
        # among the users of the mesh wins, since they all share the result
        hero = max((float(user.get(HERO_PROPERTY, 0.0) or 0.0) for user in users), default=0.0)
        hero = max(0.0, min(1.0, hero))
        auto_ratio = ratio
        ratio = max(ratio, hero, settings.get('min_ratio', 0.5))
        if ratio >= 0.95 and not existing and hero < 0.95:
            continue
        seen[mesh.name_full] = {'mesh': mesh, 'users': users, 'faces': faces,
                                'tri_share': tri_share, 'area': world_area,
                                'density': density, 'ratio': ratio,
                                'auto_ratio': auto_ratio, 'hero': hero if hero > auto_ratio else 0.0,
                                'target': int(faces * ratio)}
    return list(seen.values()), skipped


def uv_seam_vertices(mesh):
    """Indices of vertices on UV island borders (an edge whose two faces do
    not share the same UVs on both ends)."""
    bm = bmesh.new()
    bm.from_mesh(mesh)
    layer = bm.loops.layers.uv.active
    seams = set()
    if layer is not None:
        for edge in bm.edges:
            if len(edge.link_loops) != 2:
                seams.update(v.index for v in edge.verts)
                continue
            first, second = edge.link_loops
            # the loops run opposite ways along the shared edge
            a1, a2 = first[layer].uv, first.link_loop_next[layer].uv
            b1, b2 = second.link_loop_next[layer].uv, second[layer].uv
            if (a1 - b1).length > 1e-5 or (a2 - b2).length > 1e-5:
                seams.update(v.index for v in edge.verts)
    bm.free()
    return sorted(seams)


def protect_seams(plan):
    """Weight the UV border vertices in a vertex group the decimation leaves
    alone. Weights live on the shared mesh, the group on every user at the
    same index; returns False when that cannot be guaranteed (and then
    removes the groups it created, leaving the objects as they were)."""
    users = plan['users']
    indices = set()
    created = []
    for user in users:
        group = user.vertex_groups.get(SEAM_GROUP)
        if group is None:
            group = user.vertex_groups.new(name=SEAM_GROUP)
            created.append((user, group))
        indices.add(group.index)
    if len(indices) != 1:
        for user, group in created:
            user.vertex_groups.remove(group)
        return False
    group = users[0].vertex_groups[SEAM_GROUP]
    vertices = uv_seam_vertices(plan['mesh'])
    group.remove(list(range(len(plan['mesh'].vertices))))
    if vertices:
        group.add(vertices, 1.0, 'REPLACE')
    return True


def isolate_seam_data(plan):
    """Weights are shared mesh data: keep out-of-scope users untouched."""
    targets = {o.as_pointer() for o in plan['users']}
    if any(o.as_pointer() not in targets for o in mesh_users(plan['mesh'])):
        plan['mesh'] = plan['mesh'].copy()
        for obj in plan['users']:
            obj.data = plan['mesh']


def ensure_reference(context, plan):
    """Independent, excluded reference before the first optimization."""
    for obj in plan['users']:
        reference = bpy.data.objects.get(obj.get(REFERENCE_PROPERTY, ''))
        if reference is not None and reference.get(EXCLUDE_PROPERTY):
            return reference
    collection = bpy.data.collections.get('KT_Optimize_References')
    if collection is None:
        collection = bpy.data.collections.new('KT_Optimize_References')
        context.scene.collection.children.link(collection)
    source = plan['users'][0]
    reference = source.copy()
    reference.data = plan['mesh'].copy()
    # a mesh baked by an earlier Apply is no longer the original: the copy is
    # still a useful backup, but it must not pretend to be the source
    decimated = bool(plan['mesh'].get(APPLIED_PROPERTY))
    reference.name = source.name + ('_KT_Decimated' if decimated else '_KT_Original')
    reference.modifiers.clear()
    reference[EXCLUDE_PROPERTY] = True
    reference.hide_render = True
    reference.hide_viewport = True
    collection.objects.link(reference)
    for obj in plan['users']:
        obj[REFERENCE_PROPERTY] = reference.name
    return reference


def evaluated_stats(context, obj):
    context.view_layer.update()
    evaluated = obj.evaluated_get(context.evaluated_depsgraph_get())
    return len(evaluated.data.polygons), bool(evaluated.data.uv_layers)


def add_modifier(obj, ratio, protected):
    modifier = obj.modifiers.get(MODIFIER_NAME)
    if modifier is None:
        modifier = obj.modifiers.new(MODIFIER_NAME, 'DECIMATE')
    elif modifier.type != 'DECIMATE':
        raise ValueError('KT_Optimize is not a Decimate modifier')
    modifier.decimate_type = 'COLLAPSE'
    modifier.ratio = ratio
    modifier.use_collapse_triangulate = True
    modifier.show_viewport = True
    modifier.show_render = True
    if protected:
        modifier.vertex_group = SEAM_GROUP
        modifier.invert_vertex_group = True
        modifier.vertex_group_factor = 1.0
    else:
        modifier.vertex_group = ""
    return modifier


def apply_plan(context, plan, protected):
    """Decimate the shared mesh data once, in place for every user: a
    temporary object evaluates ONLY the decimation, the result becomes the
    new mesh data of all users (they stay instances of each other)."""
    mesh = plan['mesh']
    temp = bpy.data.objects.new("KT_OptimizeTemp", mesh)
    context.scene.collection.objects.link(temp)
    try:
        for group in plan['users'][0].vertex_groups:
            if temp.vertex_groups.get(group.name) is None:
                temp.vertex_groups.new(name=group.name)
        add_modifier(temp, plan['ratio'], protected)
        depsgraph = context.evaluated_depsgraph_get()
        evaluated = temp.evaluated_get(depsgraph)
        new_mesh = bpy.data.meshes.new_from_object(
            evaluated, preserve_all_data_layers=True, depsgraph=depsgraph)
    finally:
        bpy.data.objects.remove(temp, do_unlink=True)
    name = mesh.name
    for user in plan['users']:
        user.data = new_mesh
        stale = user.modifiers.get(MODIFIER_NAME)
        if stale is not None:
            user.modifiers.remove(stale)
        group = user.vertex_groups.get(SEAM_GROUP)
        if group is not None:
            user.vertex_groups.remove(group)
    if mesh.users == 0:
        bpy.data.meshes.remove(mesh)
    new_mesh.name = name
    new_mesh[APPLIED_PROPERTY] = True
    return new_mesh


def remove_optimization(objects):
    removed = 0
    # linked objects cannot be edited here (removing a modifier raised)
    objects = [obj for obj in objects if obj.library is None]
    for obj in objects:
        modifier = obj.modifiers.get(MODIFIER_NAME) if obj.type == 'MESH' else None
        if modifier is not None and modifier.type == 'DECIMATE':
            obj.modifiers.remove(modifier)
            removed += 1
    for obj in objects:
        group = obj.vertex_groups.get(SEAM_GROUP) if obj.type == 'MESH' else None
        in_use = obj.type == 'MESH' and any(
            getattr(modifier, 'vertex_group', '') == SEAM_GROUP
            for user in mesh_users(obj.data) for modifier in user.modifiers)
        if group is not None and not in_use:
            obj.vertex_groups.remove(group)
    return removed


def _faces(value):
    return f"{value / 1e6:.2f} M" if value >= 1e6 else f"{value / 1e3:.1f} k"


class OBJECT_OT_optimize_ai_meshes(bpy.types.Operator):
    """Decimate AI-generated props (Meshy, Tripo, Hunyuan...): dense triangle
    soups get a Collapse decimation to a face budget proportional to their
    surface. UV layers remain but visual detail can change: compare at the
    final camera distance. Non-destructive by default"""
    bl_idname = "kelit_toolkit.optimize_ai_meshes"
    bl_label = "Optimize AI Meshes"
    bl_options = {'REGISTER', 'UNDO'}

    scope: bpy.props.EnumProperty(
        name="Scope",
        items=[('SELECTED', "Selected Only", "The selected objects"),
               ('SCENE', "Entire Scene", "Every mesh object of the scene")],
        default='SELECTED')
    min_tri_share: bpy.props.FloatProperty(
        name="Min Triangle Share", description="Share of triangles a mesh needs to count as AI-made",
        default=0.9, min=0.0, max=1.0, subtype='FACTOR')
    density: bpy.props.FloatProperty(
        name="Density Threshold", description="Faces per square metre above which a mesh is too dense",
        default=20000.0, min=1.0)
    max_faces_absolute: bpy.props.IntProperty(
        name="Face Count Threshold", description="Meshes above this face count are always treated",
        default=100000, min=1000)
    budget: bpy.props.FloatProperty(
        name="Budget (faces/m²)", description="Target face count per square metre of surface",
        default=5000.0, min=10.0)
    min_faces: bpy.props.IntProperty(name="Min Faces", default=2000, min=10)
    max_faces: bpy.props.IntProperty(name="Max Faces", default=60000, min=100)
    min_ratio: bpy.props.FloatProperty(
        name="Minimum Retained Ratio", default=0.5, min=0.01, max=1.0, subtype='FACTOR',
        description="Overrides the face budget to limit reduction. Not a visual quality guarantee")
    include_instances: bpy.props.BoolProperty(
        name="Include Linked Instances Outside Scope", default=False,
        description="Also treat eligible objects sharing mesh data, including other scenes")
    create_reference: bpy.props.BoolProperty(
        name="Keep Original Reference", default=True,
        description="Create an independent excluded copy, hidden in viewport and render")
    apply: bpy.props.BoolProperty(
        name="Apply (destructive)",
        description="Bake the decimation into the mesh data instead of adding a modifier. "
                    "Only targeted instances receive the result; other users keep their original mesh",
        default=False)
    protect_uv_seams: bpy.props.BoolProperty(
        name="Protect UV Island Borders",
        description="Keep the vertices on UV island borders out of the decimation, "
                    "May still change shading and textures. May copy mesh data to protect other instances",
        default=False)

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def _objects(self, context):
        pool = context.selected_objects if self.scope == 'SELECTED' else context.scene.objects
        return [obj for obj in pool if obj.type == 'MESH']

    def _settings(self, context=None):
        return {'min_tri_share': self.min_tri_share, 'density': self.density,
                'max_faces_absolute': self.max_faces_absolute, 'budget': self.budget,
                'min_faces': self.min_faces, 'max_faces': max(self.max_faces, self.min_faces),
                'min_ratio': self.min_ratio, 'include_instances': self.include_instances,
                'unit_scale': context.scene.unit_settings.scale_length if context else 1.0}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "scope")
        layout.prop(self, "include_instances")
        col = layout.column(align=True)
        col.prop(self, "min_tri_share")
        col.prop(self, "density")
        col.prop(self, "max_faces_absolute")
        col = layout.column(align=True)
        col.prop(self, "budget")
        row = col.row(align=True)
        row.prop(self, "min_faces")
        row.prop(self, "max_faces")
        col.prop(self, "min_ratio")
        layout.prop(self, "protect_uv_seams")
        layout.prop(self, "create_reference")
        layout.prop(self, "apply")
        layout.label(text="Lossy reduction: compare from the final camera before Apply.", icon='ERROR')
        plans, skipped = find_candidates(self._objects(context), self._settings(context))
        objects = sum(len(p['users']) for p in plans)
        before = sum(p['faces'] for p in plans)
        after = sum(p['target'] for p in plans)
        box = layout.box()
        box.label(text=f"Estimate: {objects} object(s) / {len(plans)} unique mesh(es): "
                       f"{_faces(before)} to {_faces(after)} faces", icon='INFO')
        for plan in sorted(plans, key=lambda p: p['faces'], reverse=True)[:6]:
            box.label(text=f"  {plan['mesh'].name}: {_faces(plan['faces'])} to "
                           f"{_faces(plan['target'])} ({len(plan['users'])} user(s))")
        if skipped:
            box.label(text=f"{len(skipped)} object(s) skipped")
            for name, reason in list(skipped.items())[:3]:
                box.label(text=f"  {name}: {reason}")

    def execute(self, context):
        plans, skipped = find_candidates(self._objects(context), self._settings(context))
        if not plans:
            message = "; ".join(f"{name}: {reason}" for name, reason in list(skipped.items())[:3])
            self.report({'WARNING'} if skipped else {'INFO'}, message or "No dense mesh found with these settings")
            return {'CANCELLED'}
        restore = viewports_to_solid(context) if self.apply else (lambda: None)
        lines = []
        unprotected = []
        try:
            for plan in sorted(plans, key=lambda p: p['faces'], reverse=True):
                if self.create_reference:
                    ensure_reference(context, plan)
                if plan['ratio'] >= 1.0:
                    remove_optimization(plan['users'])
                    plan['result'] = plan['faces']
                    lines.append(('INFO', f"{plan['mesh'].name}: base mesh restored (100%)",
                                  plan['users'][0].name))
                    continue
                if self.protect_uv_seams:
                    isolate_seam_data(plan)
                protected = self.protect_uv_seams and protect_seams(plan)
                if self.protect_uv_seams and not protected:
                    unprotected.append(plan['mesh'].name)
                if self.apply:
                    name = plan['mesh'].name
                    new_mesh = apply_plan(context, plan, protected)
                    result = len(new_mesh.polygons)
                    uv_ok = bool(new_mesh.uv_layers)
                else:
                    name = plan['mesh'].name
                    for user in plan['users']:
                        add_modifier(user, plan['ratio'], protected)
                    result, uv_ok = evaluated_stats(context, plan['users'][0])
                plan['result'] = result
                lines.append(('INFO',
                              f"{name}: {plan['faces']:,} to {result:,} faces, ratio "
                              f"{plan['ratio']:.3f}, {len(plan['users'])} user(s)"
                              + (f", hero minimum {plan['hero']:.2f} (auto {plan['auto_ratio']:.3f})"
                                 if plan['hero'] else "")
                              + ("" if uv_ok else "  NO UV LAYER"),
                              plan['users'][0].name))
        finally:
            restore()
        before = sum(p['faces'] for p in plans)
        after = sum(p['result'] for p in plans)
        shown_before = sum(p['faces'] * len(p['users']) for p in plans)
        shown_after = sum(p['result'] * len(p['users']) for p in plans)
        header = [('INFO', f"{'Applied' if self.apply else 'KT_Optimize modifiers'}: "
                           f"{len(plans)} unique mesh(es), {sum(len(p['users']) for p in plans)} "
                           f"object(s). Unique {_faces(before)} to {_faces(after)} faces, "
                           f"in the scene {_faces(shown_before)} to {_faces(shown_after)}")]
        if unprotected:
            header.append(('WARNING', "UV borders not protected (vertex groups differ between "
                                      "users): " + ", ".join(unprotected[:5])))
        header.append(('WARNING', "UV layers retained does not mean identical textures or shading. "
                                  "Compare the original at the final camera distance before Apply."))
        from .scene_weight import heavy_subdivisions
        for name, base, evaluated, size in heavy_subdivisions(self._objects(context)):
            header.append(('WARNING', f"Heavy subdivision on {name}: {base:,} to {evaluated:,} faces "
                                      f"at render for a {size:.2f} m object", name))
        for mesh_name, reason in list(skipped.items())[:10]:
            header.append(('WARNING', f"Skipped {mesh_name}: {reason}"))
        set_report(context, "Optimize AI Meshes", header + lines)
        open_report(context)
        self.report({'INFO'}, f"{len(plans)} mesh(es): {_faces(before)} to {_faces(after)} faces"
                              + ("" if self.apply else " (modifiers, remove them to revert)"))
        return {'FINISHED'}


class OBJECT_OT_remove_ai_optimize(bpy.types.Operator):
    """Remove KT_Optimize modifiers and unused UV protection groups.
    Destructive Apply cannot be undone here: use Undo or the saved reference"""
    bl_idname = "kelit_toolkit.remove_ai_optimize"
    bl_label = "Remove KT_Optimize"
    bl_options = {'REGISTER', 'UNDO'}

    scope: bpy.props.EnumProperty(
        name="Scope",
        items=[('SCENE', "Entire Scene", ""), ('SELECTED', "Selected Only", "")],
        default='SCENE')

    def execute(self, context):
        pool = context.selected_objects if self.scope == 'SELECTED' else context.scene.objects
        removed = remove_optimization(pool)
        self.report({'INFO'}, f"{removed} KT_Optimize modifier(s) removed")
        return {'FINISHED'}


class OBJECT_OT_mark_ai_hero(bpy.types.Operator):
    """Give the selected objects a minimum decimation ratio for Optimize AI
    Meshes (objects seen up close: a low ratio damages UV seams). Every
    object in the selection gets it. Ratio 0 removes the mark, 1 restores the
    undecimated mesh when Optimize AI Meshes is run again"""
    bl_idname = "kelit_toolkit.mark_ai_hero"
    bl_label = "Mark as Hero"
    bl_options = {'REGISTER', 'UNDO'}

    min_ratio: bpy.props.FloatProperty(
        name="Minimum Ratio", description="Never decimate these meshes below this ratio "
                                          "(0 removes the mark)",
        default=0.5, min=0.0, max=1.0, subtype='FACTOR')
    include_instances: bpy.props.BoolProperty(
        name="Include Linked Instances", default=False,
        description="Also mark eligible objects sharing the selected mesh data")

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and any(o.type == 'MESH' for o in context.selected_objects)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=320)

    def execute(self, context):
        marked = set()
        for obj in context.selected_objects:
            if obj.type != 'MESH' or obj.data is None:
                continue
            for user in mesh_users(obj.data) if self.include_instances else [obj]:
                if user.library is not None or user.get(EXCLUDE_PROPERTY):
                    continue
                if self.min_ratio > 0:
                    user[HERO_PROPERTY] = self.min_ratio
                elif HERO_PROPERTY in user:
                    del user[HERO_PROPERTY]
                marked.add(user.name)
        if self.min_ratio > 0:
            self.report({'INFO'}, f"{len(marked)} object(s) marked hero, minimum ratio "
                                  f"{self.min_ratio:.2f} (run Optimize AI Meshes again to update)")
        else:
            self.report({'INFO'}, f"Hero mark removed from {len(marked)} object(s)")
        return {'FINISHED'}


classes = (
    OBJECT_OT_optimize_ai_meshes,
    OBJECT_OT_remove_ai_optimize,
    OBJECT_OT_mark_ai_hero,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
