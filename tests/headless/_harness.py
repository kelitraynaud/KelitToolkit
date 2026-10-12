"""Shared helpers for the headless tests (each script runs inside
``blender -b --python <script> -- <report.json>``).

The add-on must be installed in the Blender that runs the tests, under the
module name ``KelitToolkit``.
"""
import json
import os
import sys

import addon_utils
import bpy

ADDON = 'KelitToolkit'


def refuse_real_preferences():
    """Hard stop before anything runs: a headless test must never be able
    to write the user's real Blender preferences. On 7 October 2026 a test
    saved factory settings over Kelit's userpref.blend and disabled every
    add-on. Tests only run with BLENDER_USER_CONFIG pointing at a temporary
    folder (tests/run_tests.py sets it)."""
    import tempfile
    config = os.environ.get('BLENDER_USER_CONFIG', '')
    temp_root = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
    if not config or not os.path.normcase(os.path.abspath(config)).startswith(temp_root):
        print('REFUSED: run the tests through tests/run_tests.py, which isolates the '
              'Blender preferences (BLENDER_USER_CONFIG must be a temporary folder)')
        sys.exit(2)
    original = bpy.ops.wm.save_userpref

    def blocked(*_args, **_kwargs):
        raise RuntimeError('save_userpref is blocked inside the test suite')
    try:
        bpy.ops.wm.save_userpref = blocked
    except Exception:
        pass
    return original


class Harness:
    def __init__(self):
        refuse_real_preferences()
        self.report = {}
        self.failures = []
        self.out_path = None
        if '--' in sys.argv and len(sys.argv) > sys.argv.index('--') + 1:
            self.out_path = sys.argv[sys.argv.index('--') + 1]
        source = os.environ.get('KELIT_TEST_ADDON_ROOT')
        if source:
            sys.path.insert(0, os.path.dirname(os.path.abspath(source)))
        addon_utils.enable(ADDON, default_set=False, handle_error=None)
        self.module = sys.modules.get(ADDON)
        if self.module is None:
            raise RuntimeError(f'{ADDON} is not installed in this Blender')
        if source and os.path.normcase(os.path.dirname(self.module.__file__)) != os.path.normcase(os.path.abspath(source)):
            raise RuntimeError('Tests loaded another addon copy: ' + self.module.__file__)

    def check(self, name, condition, detail=None):
        ok = bool(condition)
        self.report[name] = ok if detail is None else {'ok': ok, 'detail': detail}
        if not ok:
            self.failures.append(name)
        return ok

    def note(self, name, value):
        self.report[name] = value

    def submodule(self, name):
        return sys.modules[f'{ADDON}.{name}']

    @staticmethod
    def fresh_scene(frame_start=1, frame_end=20):
        bpy.ops.wm.read_homefile(use_empty=True)
        scene = bpy.context.scene
        scene.frame_start, scene.frame_end = frame_start, frame_end
        return scene

    @staticmethod
    def deselect_all():
        for obj in bpy.data.objects:
            obj.select_set(False)

    def finish(self, label):
        self.report['failures'] = self.failures
        self.report['ok'] = not self.failures
        if self.out_path:
            with open(self.out_path, 'w', encoding='utf-8') as handle:
                json.dump(self.report, handle, indent=1)
        status = 'OK' if not self.failures else 'FAILED: ' + ', '.join(self.failures)
        print(f'{label} {status}')
        sys.exit(0 if not self.failures else 1)
