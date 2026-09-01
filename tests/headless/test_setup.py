"""Setup helpers: Connection Doctor on a fake UE project, remembered sync
options, native-updates repository registration (prefs restored after)."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Harness  # noqa: E402

import bpy  # noqa: E402

t = Harness()
unreal_link = t.submodule('operators.unreal_link')
usd_sync = t.submodule('operators.usd_sync')
settings_mod = t.submodule('settings')

# ---- Connection Doctor on a fake project ----
project = tempfile.mkdtemp(prefix='kelit_fake_ue_')
with open(os.path.join(project, 'Fake.uproject'), 'w', encoding='utf-8') as handle:
    json.dump({'FileVersion': 3, 'EngineAssociation': '5.8'}, handle)
state, error = unreal_link.read_project_setup(project)
t.check('doctor_reads_missing', error is None and state is not None
        and not state['python_plugin'] and not state['usd_plugin']
        and not state['remote_execution'])
fixed, fix_error = unreal_link.fix_project_setup(project)
state2, _ = unreal_link.read_project_setup(project)
t.check('doctor_fixes_all', fix_error is None and len(fixed) == 3
        and state2['python_plugin'] and state2['usd_plugin'] and state2['remote_execution'])
t.check('doctor_backup', os.path.isfile(os.path.join(project, 'Fake.uproject.bak')))
fixed2, _ = unreal_link.fix_project_setup(project)
t.check('doctor_idempotent', fixed2 == [])
with open(os.path.join(project, 'Fake.uproject'), encoding='utf-8') as handle:
    data = json.load(handle)
t.check('doctor_uproject_valid_json', any(
    p.get('Name') == 'USDImporter' and p.get('Enabled') for p in data.get('Plugins', [])))
t.check('doctor_bad_path_message', unreal_link.read_project_setup('Z:/nope')[1] is not None)

# ---- remembered sync options ----
scene = t.fresh_scene()
scene_settings = scene.unreal_toolkit_settings
scene_settings.sync_options_saved = False
op = usd_sync.UNREAL_OT_usd_scene_sync
fake = type('FakeOp', (), {name: None for name in op.REMEMBERED_OPTIONS})()
fake.REMEMBERED_OPTIONS = op.REMEMBERED_OPTIONS
for name in op.REMEMBERED_OPTIONS:
    setattr(fake, name, 'SELECTED' if name == 'source' else True)
fake.key_mode = 'BAKED'
fake.preserve_hierarchy = False
op._remember_options(fake, bpy.context)
t.check('options_saved', scene_settings.sync_options_saved
        and scene_settings.sync_key_mode == 'BAKED'
        and scene_settings.sync_preserve_hierarchy is False)

# ---- native updates repository (restore prefs afterwards) ----
repos = bpy.context.preferences.extensions.repos
pre_existing = settings_mod.find_update_repo(bpy.context) is not None
before = len(repos)
t.check('update_repo_first_run', list(bpy.ops.unreal_toolkit.setup_update_repo()) == ['FINISHED'])
repo = settings_mod.find_update_repo(bpy.context)
t.check('update_repo_registered', repo is not None
        and repo.remote_url == settings_mod.UPDATE_REPO_URL)
bpy.ops.unreal_toolkit.setup_update_repo()
t.check('update_repo_idempotent', len(repos) == before + (0 if pre_existing else 1))
if not pre_existing and repo is not None:
    repos.remove(repo)
    try:
        bpy.ops.wm.save_userpref()
    except Exception:
        pass
t.check('update_repo_prefs_restored',
        pre_existing or settings_mod.find_update_repo(bpy.context) is None)

t.finish('SETUP')
