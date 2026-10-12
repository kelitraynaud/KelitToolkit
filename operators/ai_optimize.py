"""Optimize AI Meshes: batch decimation of AI-generated props (Meshy, Tripo,
Hunyuan...). Those meshes are triangle soups far denser than their size
needs (a 14 cm can with 548k faces) while their UV-mapped textures carry
the detail, so a Collapse decimation to a budget proportional to the surface
keeps the look and the textures.

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


def area_scale(obj):
    """Factor turning a mesh-space area into a world-space one (mean of the
    three face orientations' scale products)."""
    sx, sy, sz = (abs(v) for v in obj.matrix_world.to_scale())
    return (sx * sy + sy * sz + sx * sz) / 3.0


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
        if modifier.type in DEFORMING:
            return "armature" if modifier.type == 'ARMATURE' else "deforming modifier"
    return None


def find_candidates(objects, settings):
    """One entry per unique mesh data worth decimating, with its plan."""
    seen = {}
    skipped = {}
    for obj in objects:
        if obj.type != 'MESH' or obj.data is None:
            continue
        mesh = obj.data
        if mesh.name_full in seen or mesh.name_full in skipped:
            continue
        users = mesh_users(mesh)
        reasons = [r for r in (skip_reason(user) for user in users) if r]
        if reasons:
            skipped[mesh.name_full] = reasons[0]
            continue
        faces, tri_share, area = mesh_stats(mesh)
        if faces == 0:
            continue
        world_area = area * area_scale(users[0])
        density = faces / world_area if world_area > 1e-9 else float('inf')
        dense = tri_share >= settings['min_tri_share'] and density >= settings['density']
        if not (dense or faces >= settings['max_faces_absolute']):
            continue
        target = world_area * settings['budget']
        target = max(settings['min_faces'], min(settings['max_faces'], target))
        ratio = max(0.01, min(1.0, target / faces))
        # "hero" objects (seen up close) carry a minimum ratio: the highest
        # among the users of the mesh wins, since they all share the result
        hero = max((float(user.get(HERO_PROPERTY, 0.0) or 0.0) for user in users), default=0.0)
        hero = max(0.0, min(1.0, hero))
        auto_ratio = ratio
        ratio = max(ratio, hero)
        if ratio >= 0.95:
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
    same index; returns False when that cannot be guaranteed."""
    users = plan['users']
    indices = set()
    for user in users:
        group = user.vertex_groups.get(SEAM_GROUP) or user.vertex_groups.new(name=SEAM_GROUP)
        indices.add(group.index)
    if len(indices) != 1:
        return False
    group = users[0].vertex_groups[SEAM_GROUP]
    vertices = uv_seam_vertices(plan['mesh'])
    group.remove(list(range(len(plan['mesh'].vertices))))
    if vertices:
        group.add(vertices, 1.0, 'REPLACE')
    return True


def add_modifier(obj, ratio, protected):
    modifier = obj.modifiers.get(MODIFIER_NAME)
    if modifier is None:
        modifier = obj.modifiers.new(MODIFIER_NAME, 'DECIMATE')
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
        if protected:
            temp.vertex_groups.new(name=SEAM_GROUP)
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
    return new_mesh


def remove_optimization(objects):
    removed = 0
    for obj in objects:
        modifier = obj.modifiers.get(MODIFIER_NAME) if obj.type == 'MESH' else None
        if modifier is not None:
            obj.modifiers.remove(modifier)
            removed += 1
        group = obj.vertex_groups.get(SEAM_GROUP) if obj.type == 'MESH' else None
        if group is not None:
            obj.vertex_groups.remove(group)
    return removed


def _faces(value):
    return f"{value / 1e6:.2f} M" if value >= 1e6 else f"{value / 1e3:.1f} k"


class OBJECT_OT_optimize_ai_meshes(bpy.types.Operator):
    """Decimate AI-generated props (Meshy, Tripo, Hunyuan...): dense triangle
    soups get a Collapse decimation to a face budget proportional to their
    surface, textures and UVs kept. Non-destructive by default (a KT_Optimize
    modifier on each object); each shared mesh is handled once"""
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
    apply: bpy.props.BoolProperty(
        name="Apply (destructive)",
        description="Bake the decimation into the mesh data instead of adding a modifier. "
                    "Instances keep sharing one mesh",
        default=False)
    protect_uv_seams: bpy.props.BoolProperty(
        name="Protect UV Island Borders",
        description="Keep the vertices on UV island borders out of the decimation, "
                    "to avoid texture smearing along seams (keeps more faces)",
        default=False)

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def _objects(self, context):
        pool = context.selected_objects if self.scope == 'SELECTED' else context.scene.objects
        return [obj for obj in pool if obj.type == 'MESH']

    def _settings(self):
        return {'min_tri_share': self.min_tri_share, 'density': self.density,
                'max_faces_absolute': self.max_faces_absolute, 'budget': self.budget,
                'min_faces': self.min_faces, 'max_faces': max(self.max_faces, self.min_faces)}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "scope")
        col = layout.column(align=True)
        col.prop(self, "min_tri_share")
        col.prop(self, "density")
        col.prop(self, "max_faces_absolute")
        col = layout.column(align=True)
        col.prop(self, "budget")
        row = col.row(align=True)
        row.prop(self, "min_faces")
        row.prop(self, "max_faces")
        layout.prop(self, "protect_uv_seams")
        layout.prop(self, "apply")
        plans, skipped = find_candidates(self._objects(context), self._settings())
        objects = sum(len(p['users']) for p in plans)
        before = sum(p['faces'] for p in plans)
        after = sum(p['target'] for p in plans)
        box = layout.box()
        box.label(text=f"{objects} object(s) / {len(plans)} unique mesh(es): "
                       f"{_faces(before)} to {_faces(after)} faces", icon='INFO')
        for plan in sorted(plans, key=lambda p: p['faces'], reverse=True)[:6]:
            box.label(text=f"  {plan['mesh'].name}: {_faces(plan['faces'])} to "
                           f"{_faces(plan['target'])} ({len(plan['users'])} user(s))")
        if skipped:
            box.label(text=f"{len(skipped)} mesh(es) skipped (armature, shape keys, linked...)")

    def execute(self, context):
        plans, skipped = find_candidates(self._objects(context), self._settings())
        if not plans:
            self.report({'INFO'}, "No AI-like dense mesh found with these settings")
            return {'CANCELLED'}
        restore = viewports_to_solid(context) if self.apply else (lambda: None)
        lines = []
        unprotected = []
        try:
            for plan in sorted(plans, key=lambda p: p['faces'], reverse=True):
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
                    result = plan['target']
                    uv_ok = True
                plan['result'] = result
                lines.append(('INFO',
                              f"{name}: {plan['faces']:,} to {result:,} faces, ratio "
                              f"{plan['ratio']:.3f}, {len(plan['users'])} user(s)"
                              + (f", hero minimum {plan['hero']:.2f} (auto {plan['auto_ratio']:.3f})"
                                 if plan['hero'] else "")
                              + ("" if uv_ok else "  NO UV AFTER APPLY"),
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
    """Remove the KT_Optimize modifiers (and the UV border vertex groups)
    added by Optimize AI Meshes: back to the original meshes"""
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
    object sharing the mesh gets it too. Ratio 0 removes the mark"""
    bl_idname = "kelit_toolkit.mark_ai_hero"
    bl_label = "Mark as Hero"
    bl_options = {'REGISTER', 'UNDO'}

    min_ratio: bpy.props.FloatProperty(
        name="Minimum Ratio", description="Never decimate these meshes below this ratio "
                                          "(0 removes the mark)",
        default=0.2, min=0.0, max=1.0, subtype='FACTOR')

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
            for user in mesh_users(obj.data):
                if user.library is not None:
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
