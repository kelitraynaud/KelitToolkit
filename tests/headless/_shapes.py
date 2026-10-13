"""Shared synthetic shapes for the rotation tests (natural axes, Auto Align,
rotated copies). Imported by the headless test scripts, not a test itself."""
import math
import random

import bmesh
import bpy
import mathutils
import mathutils.kdtree

BOX_SIZE = (2.0, 1.2, 0.8)


def add_beveled_box(bm, size=BOX_SIZE, matrix=None):
    """Box of `size` with ONE vertical edge beveled (no proper rotation maps
    it onto itself), transformed by `matrix`. Returns its vertices."""
    before = set(bm.verts)
    result = bmesh.ops.create_cube(bm, size=1.0)
    for vertex in result['verts']:
        vertex.co = mathutils.Vector((vertex.co.x * size[0], vertex.co.y * size[1],
                                      vertex.co.z * size[2]))
    edge = next(e for e in bm.edges
                if all(v in result['verts'] and v.co.x > 0 and v.co.y > 0 for v in e.verts))
    bmesh.ops.bevel(bm, geom=[edge] + list(edge.verts), offset=0.15, offset_type='OFFSET',
                    segments=1, profile=0.5, affect='EDGES')
    verts = [v for v in bm.verts if v not in before]
    if matrix is not None:
        bmesh.ops.transform(bm, matrix=matrix, verts=verts)
    return verts


def shape_mesh(name, size=BOX_SIZE, matrix=None):
    bm = bmesh.new()
    add_beveled_box(bm, size, matrix)
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
    return mesh


def link(name, mesh, matrix_world=None):
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    if matrix_world is not None:
        obj.matrix_world = matrix_world
    return obj


def random_rotation(rng):
    return mathutils.Euler((rng.uniform(-math.pi, math.pi), rng.uniform(-math.pi, math.pi),
                            rng.uniform(-math.pi, math.pi))).to_matrix().to_4x4()


def world_points(obj):
    return [obj.matrix_world @ v.co for v in obj.data.vertices]


def same_points(first, second, tolerance=1e-3):
    """Every point of `first` has a distinct point of `second` within
    `tolerance` (same count)."""
    if len(first) != len(second):
        return False
    tree = mathutils.kdtree.KDTree(len(second))
    for index, co in enumerate(second):
        tree.insert(co, index)
    tree.balance()
    used = set()
    for co in first:
        found = [index for _co, index, distance in tree.find_range(co, tolerance)
                 if index not in used]
        if not found:
            return False
        used.add(found[0])
    return True


def extents(points):
    return sorted(max(p[k] for p in points) - min(p[k] for p in points) for k in range(3))


def rng(seed=7):
    return random.Random(seed)
