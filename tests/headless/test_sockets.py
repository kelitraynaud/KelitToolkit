"""Static Mesh sockets: SOCKET_ empties parented to a mesh become sockets of
its Static Mesh (Send to Unreal), ride inside the FBX exports, and never
become level actors or USD prims. The Unreal side runs against a small fake
'unreal' module: nothing here talks to Unreal."""
import glob
import json
import math
import os
import re
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Harness  # noqa: E402

import bpy  # noqa: E402
from mathutils import Euler, Matrix, Vector  # noqa: E402

t = Harness()
sync = t.submodule('operators.usd_sync')
sockets_mod = t.submodule('operators.sockets')
from KelitToolkit.operators import ue_remote as remote  # noqa: E402 (imported lazily)
scene = t.fresh_scene()
folder = tempfile.mkdtemp(prefix='kelit_sockets_')
R = math.radians


def new_empty(name, parent=None):
    obj = bpy.data.objects.new(name, None)
    scene.collection.objects.link(obj)
    obj.parent = parent
    return obj


def new_cube(name, **kwargs):
    bpy.ops.mesh.primitive_cube_add(**kwargs)
    obj = bpy.context.active_object
    obj.name = name
    return obj


def close(values, expected, tolerance=1e-3):
    return len(values) == len(expected) and all(
        abs(a - b) <= tolerance for a, b in zip(values, expected))


def by_name(sockets):
    return {socket['name']: socket for socket in sockets}


def socket_ok(socket, location, rotation, scale=(1, 1, 1)):
    return (socket is not None and close(socket['location'], location)
            and close(socket['rotation'], rotation) and close(socket['scale'], scale))


# ---------------------------------------------------------------------------
# 1. extraction vs hand-computed values
# ---------------------------------------------------------------------------
gun = new_cube('Gun', location=(1, 0, 0), rotation=(0, 0, R(90)))
muzzle = new_empty('SOCKET_Muzzle', gun)
grip = new_empty('socket_Grip.001', gun)       # prefix is case-insensitive
top = new_empty('SOCKET_Top', gun)
pivot = new_empty('Pivot', gun)                 # a plain child empty stays an actor
bpy.context.view_layer.update()
gun_world = gun.matrix_world.copy()
muzzle.matrix_world = Matrix.LocRotScale(Vector((1, 2, 0)), Euler((0, 0, R(90))), None)
grip.matrix_world = Matrix.LocRotScale(Vector((0, 0, 0)), Euler((0, 0, R(135))), None)
top.matrix_world = gun_world @ Matrix.Translation((0, 0, 1)) @ Euler((R(30), 0, 0)).to_matrix().to_4x4()
bpy.context.view_layer.update()

found = by_name(sockets_mod.extract_sockets(gun))
t.check('socket_names', sorted(found) == ['Grip_001', 'Muzzle', 'Top'], sorted(found))
# M = T(1,0,0) Rz90. Muzzle: Rz-90 applied to (0,2,0) = (2,0,0) -> 200 cm
t.check('socket_location_converted', socket_ok(found.get('Muzzle'), [200, 0, 0], [0, 0, 0]),
        found.get('Muzzle'))
# Grip: Rz-90 applied to (-1,0,0) = (0,1,0) -> Y flipped -100; Rz45 -> yaw -45
t.check('socket_rotation_converted', socket_ok(found.get('Grip_001'), [0, -100, 0], [0, -45, 0]),
        found.get('Grip_001'))
# Top: 1 m above the origin, rotated 30 deg about X -> roll 30
t.check('socket_roll_converted', socket_ok(found.get('Top'), [0, 0, 100], [0, 0, 30]),
        found.get('Top'))

# nested parents: the grandparent's transform must not leak into the socket
holder = new_empty('Holder')
holder.location = (5, 5, 5)
holder.rotation_euler = (0, R(40), 0)
rifle = new_cube('Rifle')
rifle.parent = holder
rifle.location = (1, 0, 0)
rifle.rotation_euler = (0, 0, R(90))
sight = new_empty('SOCKET_Sight', rifle)
sight.location = (2, 0, 0)
sight.rotation_euler = (0, 0, R(45))
bpy.context.view_layer.update()
nested = by_name(sockets_mod.extract_sockets(rifle))
t.check('socket_nested_parents', socket_ok(nested.get('Sight'), [200, 0, 0], [0, -45, 0]), nested)

# scaled parent: the asset is unscaled geometry, the socket is in its space
crate = new_cube('Crate', location=(0, -3, 0))
crate.scale = (2, 2, 2)
handle = new_empty('SOCKET_Handle', crate)
handle.location = (1, 0, 0)
handle.rotation_euler = (R(90), 0, 0)
handle.scale = (0.5, 0.5, 0.5)
scaler = new_empty('Scaler')
scaler.scale = (3, 3, 3)
barrel = new_cube('Barrel')
barrel.parent = scaler
spout = new_empty('SOCKET_Spout', barrel)
spout.location = (1, 0, 0)
bpy.context.view_layer.update()
scaled = by_name(sockets_mod.extract_sockets(crate))
t.check('socket_scaled_parent', socket_ok(scaled.get('Handle'), [100, 0, 0], [0, 0, 90],
                                          [0.5, 0.5, 0.5]), scaled)
t.check('socket_scaled_grandparent',
        socket_ok(by_name(sockets_mod.extract_sockets(barrel)).get('Spout'), [100, 0, 0], [0, 0, 0]))

# ---------------------------------------------------------------------------
# 2. scene graph: no socket actors, bone sockets ignored
# ---------------------------------------------------------------------------
bpy.ops.object.armature_add()
rig = bpy.context.active_object
rig.name = 'Rig'
hand = new_empty('SOCKET_Hand', rig)
collected = [obj.name for obj in sync.collect_scene_objects([gun])]
t.check('sockets_not_collected', 'Gun' in collected and 'Pivot' in collected
        and not any(name.upper().startswith('SOCKET_') for name in collected), collected)
t.check('selected_socket_brings_mesh',
        [obj.name for obj in sync.collect_scene_objects([muzzle])] == ['Gun'])
t.check('bone_socket_ignored', 'SOCKET_Hand' not in [o.name for o in sync.collect_scene_objects([rig])]
        and sockets_mod.bone_sockets([rig]) == ['SOCKET_Hand']
        and not sockets_mod.is_mesh_socket(hand))
graph = sync.build_scene_graph(sync.collect_scene_objects([gun]))
gun_entry = next((entry for entry in graph if entry['name'] == 'Gun'), {})
t.check('graph_has_sockets', len(gun_entry.get('sockets', [])) == 3
        and not any(entry['name'].upper().startswith('SOCKET_') for entry in graph))

# ---------------------------------------------------------------------------
# 3. USD export: no socket prim
# ---------------------------------------------------------------------------
try:
    from pxr import Usd
    usd_path = os.path.join(folder, 'gun.usda')
    sync.export_usd_hierarchy(usd_path, sync.collect_scene_objects([gun]), False)
    usd_stage = Usd.Stage.Open(usd_path)   # kept alive: prims expire with their stage
    names = [prim.GetName() for prim in usd_stage.Traverse()]
    t.check('usd_has_no_socket_prim', 'Pivot' in names
            and not any('socket' in name.lower() for name in names), names)
except ImportError:
    t.note('pxr', 'unavailable, USD check skipped')

# ---------------------------------------------------------------------------
# 4. payload and final report through the real operator (Unreal faked)
# ---------------------------------------------------------------------------
Harness.deselect_all()
gun.select_set(True)
bpy.context.view_layer.objects.active = gun
captured = {}
reports = []
saved = (remote.resolve_target, sync.run_unreal_python)


def fake_run(commands):
    captured['commands'] = commands
    return True, 'B2UE_RESULT ' + json.dumps({'assets': 1, 'spawned': 2, 'attached': 1,
                                              'missing': [], 'sockets': 3})


remote.resolve_target = lambda *a, **k: {'node_id': '1', 'project_root': folder,
                                         'project_name': 'Fake'}
sync.run_unreal_python = fake_run
# the final report is built by sync_result_message: wrap it to see it
message_builder = getattr(sync, 'sync_result_message', None)
if message_builder is not None:
    sync.sync_result_message = lambda result: reports.append(message_builder(result)) or reports[-1]
try:
    state = bpy.ops.kelit_toolkit.usd_scene_sync(
        'EXEC_DEFAULT', source='SELECTED', import_materials=False,
        place_in_level=True, include_animation=False)
    sync_error = ''
except RuntimeError as error:
    state, sync_error = None, str(error)
finally:
    remote.resolve_target, sync.run_unreal_python = saved
    if message_builder is not None:
        sync.sync_result_message = message_builder
t.check('send_finished', state == {'FINISHED'}, sync_error[-400:])
payload = {}
match = re.search(r'open\("([^"]+)"', (captured.get('commands') or [''])[0])
if match:
    with open(match.group(1), encoding='utf-8') as handle:
        for line in handle:
            if line.startswith('PAYLOAD = json.loads('):
                payload = json.loads(json.loads(line[len('PAYLOAD = json.loads('):].rstrip().rstrip(')')))
                break
entries = {entry['name']: entry for entry in payload.get('objects', [])}
t.check('payload_no_socket_actor', 'Gun' in entries and 'Pivot' in entries
        and not any(name.upper().startswith('SOCKET_') for name in entries), sorted(entries))
payload_sockets = by_name(entries.get('Gun', {}).get('sockets', []))
t.check('payload_sockets', sorted(payload_sockets) == ['Grip_001', 'Muzzle', 'Top']
        and socket_ok(payload_sockets.get('Muzzle'), [200, 0, 0], [0, 0, 0]), payload_sockets)
t.check('report_counts_sockets', any('3 socket(s)' in message for message in reports), reports)


# ---------------------------------------------------------------------------
# 5. Unreal side against a fake 'unreal' module
# ---------------------------------------------------------------------------
class FakeSocket:
    def __init__(self, outer=None, name='None', location=(0, 0, 0)):
        self.outer = outer
        self.props = {'socket_name': name, 'relative_location': location,
                      'relative_rotation': None, 'relative_scale': (1, 1, 1)}

    def get_editor_property(self, key):
        return self.props[key]

    def set_editor_property(self, key, value):
        self.props[key] = value


class FakeMesh:
    def __init__(self, name, path):
        self.name, self.path = name, path
        self.sockets, self.meta, self.saves = [], {}, 0

    def get_name(self):
        return self.name

    def get_path_name(self):
        return self.path

    def get_editor_property(self, key):
        if key == 'sockets':
            return list(self.sockets)
        raise AttributeError(key)

    def find_socket(self, name):
        return next((s for s in self.sockets
                     if str(s.props['socket_name']).lower() == str(name).lower()), None)

    def add_socket(self, socket):
        self.sockets.append(socket)

    def remove_socket(self, socket):
        self.sockets.remove(socket)

    def modify(self):
        return True


def fake_unreal(assets, logs, with_sockets=True):
    module = types.ModuleType('unreal')
    module.log = lambda text: logs.append(str(text))
    module.StaticMesh = FakeMesh
    module.SkeletalMesh = type('SkeletalMesh', (), {})
    module.UsdStageImportOptions = type('UsdStageImportOptions', (), {})
    module.Vector = lambda x, y, z: (float(x), float(y), float(z))
    module.Rotator = lambda roll=0.0, pitch=0.0, yaw=0.0: {'roll': roll, 'pitch': pitch, 'yaw': yaw}
    if with_sockets:
        module.StaticMeshSocket = FakeSocket
        module.new_object = lambda cls, outer=None: cls(outer)

    def save(asset, only_if_is_dirty=True):
        asset.saves += 1
        return True

    module.EditorAssetLibrary = types.SimpleNamespace(
        does_asset_exist=lambda path: path.split('.')[0] in assets,
        load_asset=lambda path: assets.get(path.split('.')[0]),
        get_metadata_tag=lambda asset, tag: asset.meta.get(tag, ''),
        set_metadata_tag=lambda asset, tag, value: asset.meta.__setitem__(tag, value),
        remove_metadata_tag=lambda asset, tag: asset.meta.pop(tag, None),
        save_loaded_asset=save,
        save_asset=lambda *a, **k: True,
        save_directory=lambda *a, **k: True,
        does_directory_exist=lambda path: False,
        list_assets=lambda *a, **k: [])
    return module


def run_template(sockets_payload, module):
    previous = sys.modules.get('unreal')
    sys.modules['unreal'] = module
    script = sync.UNREAL_SCRIPT_TEMPLATE.replace(
        '__PAYLOAD__', json.dumps(json.dumps(sockets_payload)))
    try:
        exec(compile(script, 'b2ue_fake', 'exec'), {'__name__': 'b2ue_fake'})
    finally:
        if previous is None:
            sys.modules.pop('unreal', None)
        else:
            sys.modules['unreal'] = previous


def make_assets():
    assets = {}
    for name in ('Gun', 'Crate', 'Barrel'):
        mesh = FakeMesh('SM_' + name, '/Game/S/SM_' + name)
        assets[mesh.path] = mesh
    gun_mesh = assets['/Game/S/SM_Gun']
    hand_made = FakeSocket(gun_mesh, 'Hand')
    old_muzzle = FakeSocket(gun_mesh, 'Muzzle', (1.0, 1.0, 1.0))
    gun_mesh.sockets = [hand_made, old_muzzle, FakeSocket(gun_mesh, 'Old')]
    gun_mesh.meta['B2UE_Sockets'] = json.dumps(['Muzzle', 'Old'])
    barrel_mesh = assets['/Game/S/SM_Barrel']
    barrel_mesh.sockets = [FakeSocket(barrel_mesh, 'Gone')]
    barrel_mesh.meta['B2UE_Sockets'] = json.dumps(['Gone'])
    return assets, hand_made, old_muzzle


def mesh_entry(name, data_key, sockets):
    return {'name': name, 'type': 'MESH', 'mesh_name': None, 'data_key': data_key,
            'prim_path': None, 'parent': None, 'location': [0, 0, 0],
            'rotation': [0, 0, 0], 'scale': [1, 1, 1], 'sockets': sockets}


blender_sockets = [
    {'name': 'Muzzle', 'location': [10, 0, 0], 'rotation': [0, 90, 0], 'scale': [1, 1, 1]},
    {'name': 'Grip', 'location': [0, 5, 0], 'rotation': [0, 0, 0], 'scale': [1, 1, 1]},
]
fake_payload = {
    'usd_file': None, 'static_fingerprint': '', 'force_reimport': False, 'skeletal': [],
    'content_folder': '/Game/S', 'scene_name': 'S', 'import_materials': False,
    'place_in_level': False, 'replace_existing': True, 'uv_signatures': {},
    'objects': [mesh_entry('Gun', 'k_gun', blender_sockets),
                # shares Gun's asset (same geometry key) with other sockets
                mesh_entry('GunCopy', 'k_gun', [dict(blender_sockets[0], name='Other')]),
                mesh_entry('Crate', 'k_crate', []),
                mesh_entry('Barrel', 'k_barrel', [])],
}
assets, hand_made, old_muzzle = make_assets()
logs = []
run_template(fake_payload, fake_unreal(assets, logs))
result_line = next((line for line in logs if 'B2UE_RESULT' in line), None)
error_line = next((line for line in logs if 'B2UE_ERROR' in line), None)
t.check('fake_sync_ran', result_line is not None and error_line is None, error_line or logs[-5:])
result = json.loads(result_line.split('B2UE_RESULT', 1)[1]) if result_line else {}
gun_mesh = assets['/Game/S/SM_Gun']
socket_names = [s.props['socket_name'] for s in gun_mesh.sockets]
t.check('ue_sockets_created_updated_removed', socket_names == ['Hand', 'Muzzle', 'Grip'], socket_names)
t.check('ue_hand_made_socket_kept', gun_mesh.sockets[0] is hand_made
        and hand_made.props['relative_location'] == (0, 0, 0))
t.check('ue_socket_updated_in_place', gun_mesh.sockets[1] is old_muzzle
        and old_muzzle.props['relative_location'] == (10.0, 0.0, 0.0)
        and old_muzzle.props['relative_rotation'] == {'roll': 0, 'pitch': 0, 'yaw': 90},
        old_muzzle.props)
t.check('ue_socket_tagged', json.loads(gun_mesh.meta.get('B2UE_Sockets', '[]')) == ['Muzzle', 'Grip']
        and gun_mesh.saves >= 1)
barrel_mesh = assets['/Game/S/SM_Barrel']
t.check('ue_stale_tagged_removed', barrel_mesh.sockets == [] and 'B2UE_Sockets' not in barrel_mesh.meta)
t.check('ue_untouched_without_sockets', assets['/Game/S/SM_Crate'].saves == 0)
t.check('ue_shared_mesh_warns', any('WARNING' in line and 'GunCopy' in line for line in logs))
t.check('ue_result_counts', result.get('sockets') == 2 and result.get('sockets_removed') == 2, result)

# a UE build without the socket API: the send still completes
assets, _hand, _muzzle = make_assets()
logs = []
run_template(fake_payload, fake_unreal(assets, logs, with_sockets=False))
t.check('ue_missing_api_never_fails', any('B2UE_RESULT' in line for line in logs)
        and not any('B2UE_ERROR' in line for line in logs), logs[-5:])

# ---------------------------------------------------------------------------
# 6. Add Socket operator
# ---------------------------------------------------------------------------
body = new_cube('Body', location=(0, 0, 0))
body.scale = (2, 1, 1)
bpy.context.view_layer.update()
scene.cursor.location = (1, 2, 3)
scene.cursor.rotation_euler = (0, 0, 0)
Harness.deselect_all()
body.select_set(True)
bpy.context.view_layer.objects.active = body
t.check('add_socket_poll_mesh', bpy.ops.kelit_toolkit.add_socket.poll())
bpy.ops.kelit_toolkit.add_socket('EXEC_DEFAULT', socket_name='Tip')
first = bpy.data.objects.get('SOCKET_Tip')
bpy.context.view_layer.update()
t.check('add_socket_created', first is not None and first.parent is body
        and first.empty_display_type == 'ARROWS'
        and close(first.matrix_world.translation, (1, 2, 3))
        and close(first.matrix_world.to_scale(), (1, 1, 1))
        and abs(first.empty_display_size - 1.0) < 1e-4,
        None if first is None else [list(first.matrix_world.translation), first.empty_display_size])
t.check('add_socket_poll_needs_mesh', not bpy.ops.kelit_toolkit.add_socket.poll())
bpy.context.view_layer.objects.active = body
bpy.ops.kelit_toolkit.add_socket('EXEC_DEFAULT', socket_name='Tip')
t.check('add_socket_unique_name', bpy.data.objects.get('SOCKET_Tip_2') is not None
        and sorted(s['name'] for s in sockets_mod.extract_sockets(body)) == ['Tip', 'Tip_2'])
desc = sockets_mod.OBJECT_OT_kelit_add_socket.__doc__ or ''
t.check('add_socket_tooltips_short', 0 < len(desc) < 200)

# ---------------------------------------------------------------------------
# 7. FBX exports carry the SOCKET_ children
# ---------------------------------------------------------------------------
def fbx_files(directory):
    return sorted(glob.glob(os.path.join(directory, '*.fbx')))


def contains(path, text):
    with open(path, 'rb') as handle:
        return text.encode('utf-8') in handle.read()


def matrix_close(a, b):
    return all(abs(a[i][j] - b[i][j]) < 1e-5 for i in range(4) for j in range(4))


def fbx_names(path):
    with open(path, 'rb') as handle:
        data = handle.read()
    return sorted(set(m.decode('latin-1') for m in re.findall(rb'([A-Za-z_][A-Za-z0-9_.]{2,40})\x00\x01Model', data)))


gun_before = gun.matrix_world.copy()
assets_dir = os.path.join(folder, 'assets')
os.makedirs(assets_dir)
Harness.deselect_all()
for obj in (gun, muzzle):          # a selected socket must not become a scene file
    obj.select_set(True)
bpy.context.view_layer.objects.active = gun
bpy.ops.kelit_toolkit.export_assets('EXEC_DEFAULT', directory=assets_dir, file_format='FBX',
                                    asset_type='AUTO')
files = fbx_files(assets_dir)
t.check('export_assets_fbx_sockets', len(files) == 1
        and all(contains(files[0], name) for name in ('SOCKET_Muzzle', 'socket_Grip.001', 'SOCKET_Top'))
        and 'Pivot' not in fbx_names(files[0]), [os.path.basename(f) for f in files] + (fbx_names(files[0]) if files else []))
t.check('export_assets_restored', matrix_close(gun.matrix_world, gun_before)
        and muzzle.select_get() and gun.select_get())

usd_dir = os.path.join(folder, 'assets_usd')
os.makedirs(usd_dir)
Harness.deselect_all()
gun.select_set(True)
bpy.ops.kelit_toolkit.export_assets('EXEC_DEFAULT', directory=usd_dir, file_format='USD',
                                    asset_type='AUTO')
try:
    from pxr import Usd
    usd_files = glob.glob(os.path.join(usd_dir, '*.usd*'))
    asset_stage = Usd.Stage.Open(usd_files[0]) if usd_files else None
    prim_names = [p.GetName() for p in asset_stage.Traverse()] if asset_stage else []
    t.check('export_assets_usd_no_socket', len(usd_files) == 1
            and not any('socket' in name.lower() for name in prim_names), prim_names)
except ImportError:
    pass

batch_dir = os.path.join(folder, 'batch')
os.makedirs(batch_dir)
Harness.deselect_all()
gun.select_set(True)
bpy.context.view_layer.objects.active = gun
bpy.ops.kelit_toolkit.batch_fbx_export('EXEC_DEFAULT', directory=batch_dir)
files = fbx_files(batch_dir)
t.check('batch_fbx_sockets', len(files) == 1
        and all(contains(files[0], name) for name in ('SOCKET_Muzzle', 'socket_Grip.001'))
        and 'Pivot' not in fbx_names(files[0]), [os.path.basename(f) for f in files] + (fbx_names(files[0]) if files else []))
t.check('batch_fbx_restored', matrix_close(gun.matrix_world, gun_before) and not muzzle.select_get())

t.finish('sockets')
