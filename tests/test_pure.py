"""Pure-Python tests (no Blender needed): key reduction, naming helpers.

Run directly: ``python tests/test_pure.py`` or through ``tests/run_tests.py``.
Functions that only depend on the standard library are loaded straight from
the source files, with ``bpy``/``mathutils`` stubbed for import.
"""
import ast
import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_function(relative_path, name, constants=()):
    """Extract one module-level function from a source file by AST, together
    with the module-level constants it needs."""
    source = open(os.path.join(ROOT, relative_path), encoding='utf-8').read()
    tree = ast.parse(source)
    namespace = {'re': __import__('re')}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id in constants
                for target in node.targets):
            exec(ast.get_source_segment(source, node), namespace)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            exec(ast.get_source_segment(source, node), namespace)
            return namespace[name]
    raise LookupError(f'{name} not found in {relative_path}')


def load_utils():
    """Import utils.py with bpy/mathutils stubbed (only the pure functions
    are exercised)."""
    for stub in ('bpy', 'mathutils'):
        sys.modules.setdefault(stub, types.ModuleType(stub))
    spec = importlib.util.spec_from_file_location('kelit_utils', os.path.join(ROOT, 'utils.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run():
    report = {}
    failures = []

    def check(name, condition):
        report[name] = bool(condition)
        if not condition:
            failures.append(name)

    reduce = load_function('operators/usd_sync.py', 'reduce_channel_keys')
    ease = [100.0 * (3 * x * x - 2 * x ** 3) for x in (i / 99 for i in range(100))]
    check('reduce_smoothstep_2keys', len(reduce(ease, {0, 99}, 0.1)) == 2)
    check('reduce_dense_seeds_collapse', len(reduce(ease, set(range(100)), 0.1)) == 2)
    check('reduce_const', len(reduce([5.0] * 100, set(), 0.05)) == 2)
    check('reduce_two_frames', reduce([1.0, 2.0], set(), 0.1) == [[0, 1.0], [1, 2.0]])
    pairs = reduce([10.0 * i for i in range(100)], set(), 0.3)
    check('reduce_keeps_ends', pairs[0][0] == 0 and pairs[-1][0] == 99)

    utils = load_utils()
    check('clean_name_stacked', utils.clean_name('Cube.001.002') == 'Cube')
    check('clean_name_plain', utils.clean_name('Cube') == 'Cube')
    check('normalize_pascal_prefix', utils.normalize_name_for_unreal('my chair.001') == 'SM_MyChair')
    check('normalize_material', utils.normalize_name_for_unreal('wood', 'MATERIAL') == 'M_Wood')
    check('normalize_collision_kept',
          utils.normalize_name_for_unreal('UCX_chair') == 'UCX_Chair')
    check('normalize_lod_kept',
          utils.normalize_name_for_unreal('chair_lod2').endswith('_LOD2'))
    long_name = utils.normalize_name_for_unreal('x' * 90 + '_LOD3')
    check('normalize_truncates_63_bytes',
          len(long_name.encode('utf-8')) <= 63 and long_name.endswith('_LOD3'))
    check('normalize_idempotent',
          utils.normalize_name_for_unreal('SM_MyChair') == 'SM_MyChair')

    snake = load_function('operators/naming.py', 'to_snake_case_keeping_prefix')
    check('snake_keeps_prefix', snake('SM_MyChair') == 'SM_My_Chair')
    check('snake_plain', snake('MyChair') == 'My_Chair')
    check('snake_keeps_lod', snake('SM_Chair_LOD2') == 'SM_Chair_LOD2')
    check('snake_acronym', snake('SM_HDRICapture') == 'SM_HDRI_Capture')
    check('snake_idempotent', snake('SM_My_Chair') == 'SM_My_Chair')

    root_package = load_function('operators/ue_remote.py', 'addon_root_package')
    check('root_package_classic', root_package('KelitToolkit.operators') == 'KelitToolkit')
    check('root_package_extension',
          root_package('bl_ext.user_default.kelittoolkit.operators')
          == 'bl_ext.user_default.kelittoolkit')

    no_editor = load_function('operators/unreal_link.py', 'is_no_editor_error',
                              constants=('NO_EDITOR_MESSAGE',))
    check('no_editor_detected', no_editor('No running Unreal Editor found. Open your UE project'))
    check('no_editor_not_for_failed_command', not no_editor('Remote execution failed: boom'))
    check('no_editor_not_for_none', not no_editor(None))

    sanitize = load_function('operators/usd_sync.py', 'sanitize_prim_name')
    check('sanitize_ascii', sanitize('Café Table') == 'Caf__Table')
    check('sanitize_leading_digit', sanitize('3Dwall') == '_3Dwall')

    report['failures'] = failures
    report['ok'] = not failures
    return report


if __name__ == '__main__':
    result = run()
    print('PURE', 'OK' if result['ok'] else 'FAILED: ' + ', '.join(result['failures']))
    sys.exit(0 if result['ok'] else 1)
