"""Kelit Toolkit test runner.

    python tests/run_tests.py            # everything
    python tests/run_tests.py --quick    # skips the slow bake test
    python tests/run_tests.py --pure     # no Blender needed

Headless tests run inside Blender (found via the BLENDER_EXE environment
variable or the usual install paths) and need the add-on installed there
under the module name ``KelitToolkit``.
"""
import glob
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SLOW = {'test_bake.py'}


def find_blender():
    candidates = [os.environ.get('BLENDER_EXE', '')]
    candidates.append(r'C:\Program Files (x86)\Steam\steamapps\common\Blender\blender.exe')
    candidates.extend(sorted(glob.glob(r'C:\Program Files\Blender Foundation\Blender *\blender.exe'),
                             reverse=True))
    candidates.append('/Applications/Blender.app/Contents/MacOS/Blender')
    candidates.append('blender')
    for candidate in candidates:
        if candidate and (os.path.isfile(candidate) or candidate == 'blender'):
            return candidate
    return None


def run_headless(blender, script):
    report_path = os.path.join(tempfile.gettempdir(), f'kelit_{os.path.basename(script)}.json')
    if os.path.exists(report_path):
        os.remove(report_path)
    # the tests run against a throwaway config folder: a test that saves the
    # preferences (registering the update repository does) must never write
    # the user's real userpref.blend. With --factory-startup it once replaced
    # it with factory settings, every add-on disabled.
    env = dict(os.environ)
    env['BLENDER_USER_CONFIG'] = tempfile.mkdtemp(prefix='kelit_test_config_')
    process = subprocess.run(
        [blender, '-b', '--factory-startup', '--python', script, '--', report_path],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=900, env=env)
    if os.path.exists(report_path):
        with open(report_path, encoding='utf-8') as handle:
            return json.load(handle), process.stdout
    return {'ok': False, 'failures': ['no report written'],
            'crash': process.stdout[-1500:]}, process.stdout


def backup_real_preferences():
    """Second safety net: copy the user's preferences aside before every
    run, so even an unforeseen write could be undone."""
    import shutil
    import time
    appdata = os.environ.get('APPDATA', '')
    root = os.path.join(appdata, 'Blender Foundation', 'Blender')
    if not os.path.isdir(root):
        return
    backup_dir = os.path.join(tempfile.gettempdir(), 'kelit_userpref_backups',
                              time.strftime('%Y%m%d_%H%M%S'))
    for version in os.listdir(root):
        source = os.path.join(root, version, 'config', 'userpref.blend')
        if os.path.isfile(source):
            target = os.path.join(backup_dir, version)
            os.makedirs(target, exist_ok=True)
            shutil.copy2(source, target)


def main(argv):
    backup_real_preferences()
    quick = '--quick' in argv
    pure_only = '--pure' in argv
    all_ok = True

    sys.path.insert(0, HERE)
    import test_pure
    result = test_pure.run()
    all_ok &= result['ok']
    print(f"[pure]        {'OK' if result['ok'] else 'FAILED: ' + ', '.join(result['failures'])}")

    if pure_only:
        return 0 if all_ok else 1

    blender = find_blender()
    if blender is None:
        print('Blender not found: set BLENDER_EXE to run the headless tests')
        return 1

    scripts = sorted(glob.glob(os.path.join(HERE, 'headless', 'test_*.py')))
    for script in scripts:
        name = os.path.basename(script)
        if quick and name in SLOW:
            print(f"[{name[:-3]:<15}] skipped (--quick)")
            continue
        report, output = run_headless(blender, script)
        ok = report.get('ok', False)
        all_ok &= ok
        status = 'OK' if ok else 'FAILED: ' + ', '.join(report.get('failures', []))
        print(f"[{name[:-3]:<15}] {status}")
        if not ok and report.get('crash'):
            print(report['crash'])

    print('ALL OK' if all_ok else 'SOME TESTS FAILED')
    return 0 if all_ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
