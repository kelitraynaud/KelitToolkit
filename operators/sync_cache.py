"""Conservative whole-batch static import cache (never skips Blender export)."""
import hashlib
import json
import os


def static_fingerprint(filepath, geometry_only=True):
    from pxr import Usd, UsdShade
    import numpy as np
    stage = Usd.Stage.Open(filepath)
    digest = hashlib.sha256(b'KelitGeometry-v2')

    def text(value):
        digest.update(json.dumps(value, sort_keys=True, default=str).encode('utf-8'))
        digest.update(b'\0')

    def value(data):
        # Hash USD numeric arrays directly. Converting a million vertices
        # into decimal text costs several seconds and creates a huge string.
        array = np.asarray(data)
        if array.dtype.kind in 'biuf' and array.size:
            text((array.dtype.str, array.shape))
            digest.update(np.ascontiguousarray(array).tobytes())
        else:
            text(data)

    for key in ('upAxis', 'metersPerUnit', 'timeCodesPerSecond'):
        text((key, stage.GetMetadata(key)))
    ignored = []
    for prim in sorted(stage.Traverse(), key=lambda p: str(p.GetPath())):
        path = str(prim.GetPath())
        if geometry_only and (prim.IsA(UsdShade.Material) or prim.IsA(UsdShade.Shader)):
            ignored.append(path)
            continue
        if any(path.startswith(parent + '/') for parent in ignored):
            continue
        text((path, prim.GetTypeName(), prim.GetAllMetadata()))
        for attr in sorted(prim.GetAttributes(), key=lambda a: a.GetName()):
            text((attr.GetName(), attr.GetAllMetadata()))
            value(attr.Get())
            for sample in attr.GetTimeSamples():
                text(sample)
                value(attr.Get(sample))
            text([str(p) for p in attr.GetConnections()])
        for relationship in sorted(prim.GetRelationships(), key=lambda r: r.GetName()):
            text((relationship.GetName(), [str(p) for p in relationship.GetTargets()]))
    return digest.hexdigest()


def cache_path(staging_dir, project_root, destination, scene_name):
    identity = json.dumps([os.path.normcase(os.path.normpath(project_root)), destination, scene_name])
    folder = os.path.join(staging_dir, 'import_cache')
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, hashlib.sha256(identity.encode()).hexdigest() + '.json')


def skeletal_material_order(filepath, name_map):
    """Match USD skinning-target traversal and first-occurrence slot merging.

    UE's UsdSkel importer merges shared material bindings, naming slots 0,1,...
    rather than retaining Blender slot names. Read the bindings actually
    exported, including material-name sanitization and numeric suffixes.
    """
    from pxr import Usd, UsdGeom, UsdShade, UsdSkel
    stage = Usd.Stage.Open(filepath)
    cache = UsdSkel.Cache()
    paths = []
    for prim in stage.Traverse():
        if not prim.IsA(UsdSkel.Root):
            continue
        root = UsdSkel.Root(prim)
        cache.Populate(root, Usd.PrimDefaultPredicate)
        for binding in cache.ComputeSkelBindings(root, Usd.PrimDefaultPredicate):
            for query in binding.GetSkinningTargets():
                mesh = UsdGeom.Mesh(query.GetPrim())
                if not mesh or not mesh.GetPrim().HasAPI(UsdSkel.BindingAPI):
                    continue
                api = UsdShade.MaterialBindingAPI(mesh)
                subsets = api.GetMaterialBindSubsets()
                bindings = [UsdShade.MaterialBindingAPI(s).ComputeBoundMaterial()[0] for s in subsets]
                if not subsets or sum(len(s.GetIndicesAttr().Get() or []) for s in subsets) != len(mesh.GetFaceVertexCountsAttr().Get() or []):
                    bindings.append(api.ComputeBoundMaterial()[0])
                for material in bindings:
                    path = str(material.GetPath()) if material else ''
                    key = (path, bool(mesh.GetDoubleSidedAttr().Get()))
                    if key not in paths:
                        paths.append(key)
    unknown = [path for path, _ in paths if path and path.rsplit('/', 1)[-1] not in name_map]
    if unknown:
        raise RuntimeError('Unmapped skeletal material bindings: ' + ', '.join(unknown))
    return [name_map.get(path.rsplit('/', 1)[-1]) if path else None for path, _double_sided in paths]
