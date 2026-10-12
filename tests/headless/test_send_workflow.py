"""Automatic rig detection, optional clips, and texture export invalidation."""
import os
import sys
import tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Harness
import bpy
from pxr import Usd

t = Harness()
sync = t.submodule('operators.usd_sync')
bridge = t.submodule('operators.ue_materials')
scene = t.fresh_scene(frame_start=1, frame_end=8)
folder = tempfile.mkdtemp(prefix='kelit_send_')

bpy.ops.mesh.primitive_cube_add()
mesh = bpy.context.object
mesh.name = 'SK_TestBody'
t.check('static_detected', not sync.split_skeletal_assets([mesh])[0])
bpy.ops.object.armature_add()
rig = bpy.context.object
rig.name = 'Rig_Test'
t.check('bare_armature_not_a_mesh', not sync.split_skeletal_assets([rig])[0])
modifier = mesh.modifiers.new('Armature', 'ARMATURE')
modifier.object = rig
mesh.parent = rig
group = mesh.vertex_groups.new(name=rig.data.bones[0].name)
group.add(list(range(len(mesh.data.vertices))), 1.0, 'REPLACE')
assets, static = sync.split_skeletal_assets([mesh])
t.check('selecting_skin_detects_rig', len(assets) == 1 and assets[0]['root'] == rig and not static)
asset = assets[0]
t.check('static_rig_has_no_animation', not sync.has_skeletal_animation(asset))
bone = rig.pose.bones[0]
bone.location = (0, 0, 0)
bone.keyframe_insert('location', frame=1)
bone.location = (0, 0, 1)
bone.keyframe_insert('location', frame=8)
t.check('pose_animation_detected', sync.has_skeletal_animation(asset))
scene.frame_set(3)
before = (scene.frame_start, scene.frame_end, scene.frame_current)
for enabled in (True, False):
    path = os.path.join(folder, 'clip_' + str(enabled) + '.usda')
    sync.export_skeletal_usd(path, asset, include_animation=enabled)
    stage = Usd.Stage.Open(path)
    animated = [str(attr.GetPath()) for prim in stage.Traverse()
                for attr in prim.GetAttributes() if attr.GetNumTimeSamples() > 1]
    t.check('clip_enabled' if enabled else 'clip_disabled', bool(animated) == enabled, animated)
    t.check('timeline_preserved_' + str(enabled), before == (scene.frame_start, scene.frame_end, scene.frame_current))

# A clean FILE image is reused; painting and external replacement invalidate it.
generated = bpy.data.images.new('CacheFixture', 2, 2, alpha=True)
generated.pixels = [1, 0, 0, 1] * 4
source = os.path.join(folder, 'source.png')
generated.file_format = 'PNG'
generated.save(filepath=source)
img = bpy.data.images.load(source, check_existing=False)
output = os.path.join(folder, 'export.png')
hash1 = bridge.export_texture_copy(img, output)
stamp1 = bridge.file_stamp(output)
hash2 = bridge.export_texture_copy(img, output)
t.check('clean_texture_reused', hash1 == hash2 and stamp1 == bridge.file_stamp(output))
img.pixels = [0, 1, 0, 0.5] * 4
hash3 = bridge.export_texture_copy(img, output)
t.check('painted_texture_detected', hash3 != hash2)
hash4 = bridge.export_texture_copy(img, output)
t.check('identical_dirty_pixels_same_hash', hash3 == hash4)
generated.pixels = [0, 0, 1, 1] * 4
generated.save(filepath=source)
img.reload()
hash5 = bridge.export_texture_copy(img, output)
t.check('reloaded_source_detected', hash5 != hash4)
os.unlink(output)
hash6 = bridge.export_texture_copy(img, output)
t.check('deleted_export_recreated', os.path.exists(output) and hash5 == hash6)
bridge._TEXTURE_EXPORT_CACHE.clear()
t.check('hash_stable_across_sessions', bridge.export_texture_copy(img, output) == hash6)
t.finish('send workflow')
