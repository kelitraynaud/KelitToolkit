"""Unreal Editor remote link.

Sends Python to a running Unreal Editor through the vendored remote-execution
transport (Epic's module, see dependencies/). Also hosts the permanent
'Interchange.FeatureFlags.Import.FBX=False' fix for projects that import FBX
through UE's legacy importer.

Unreal side requirement: the 'Python Editor Script Plugin' enabled with
remote execution turned on.
"""

import json
import os
import re
import shutil

import bpy


INTERCHANGE_CVAR = 'Interchange.FeatureFlags.Import.FBX'
CONNECTION_MARKER = 'KelitToolkit-OK'
PYTHON_SETTINGS_SECTION = '[/Script/PythonScriptPlugin.PythonScriptPluginSettings]'


def run_unreal_python(commands):
    """
    Run python statements in the running Unreal Editor.

    :param list commands: python statements to execute in Unreal.
    :return tuple: (success, message)
    """
    no_editor = ("No running Unreal Editor found. Open your UE project and enable "
                 "'Python Editor Script Plugin' with remote execution")
    try:
        from . import ue_remote
    except Exception:
        return False, "Could not load the Unreal remote-execution transport"

    try:
        output = ue_remote.run_commands(commands)
        return True, str(output or '').strip()
    except ConnectionError:
        return False, no_editor
    except Exception as error:
        return False, f"Remote execution failed: {error}"


# ============================================================================
# INTERCHANGE FIX HELPERS
# ============================================================================

def find_default_engine_ini(project_dir):
    """
    Locate Config/DefaultEngine.ini from a UE project folder.

    Accepts the project root (folder containing the .uproject) or the Config
    folder itself. Returns (ini_path, error_message).
    """
    project_dir = bpy.path.abspath(project_dir) if project_dir else ''
    project_dir = os.path.normpath(project_dir)

    if not project_dir or not os.path.isdir(project_dir):
        return None, "Set a valid Unreal project folder first"

    # Allow pointing directly at the Config folder
    if os.path.basename(project_dir).lower() == 'config':
        project_dir = os.path.dirname(project_dir)

    uprojects = [f for f in os.listdir(project_dir) if f.lower().endswith('.uproject')]
    if not uprojects:
        return None, f"No .uproject file found in '{project_dir}'"

    return os.path.join(project_dir, 'Config', 'DefaultEngine.ini'), None


def apply_interchange_fix_to_ini(ini_path):
    """
    Ensure '[ConsoleVariables] Interchange.FeatureFlags.Import.FBX=False' is
    present in DefaultEngine.ini. Creates the file/section if missing and a
    '.bak' backup before modifying an existing file.

    :return tuple: (changed, message)
    """
    cvar_line = f'{INTERCHANGE_CVAR}=False'
    cvar_pattern = re.compile(
        r'^(\s*' + re.escape(INTERCHANGE_CVAR) + r'\s*=\s*)(\S+)\s*$',
        re.IGNORECASE | re.MULTILINE
    )
    section_pattern = re.compile(r'^\[ConsoleVariables\]\s*$', re.IGNORECASE | re.MULTILINE)

    if os.path.isfile(ini_path):
        with open(ini_path, 'r', encoding='utf-8-sig', errors='replace') as ini_file:
            content = ini_file.read()

        match = cvar_pattern.search(content)
        if match:
            if match.group(2).lower() == 'false':
                return False, "Already configured - the legacy FBX importer is permanent"
            new_content = cvar_pattern.sub(r'\g<1>False', content, count=1)
        else:
            section_match = section_pattern.search(content)
            if section_match:
                insert_at = section_match.end()
                new_content = content[:insert_at] + '\n' + cvar_line + content[insert_at:]
            else:
                new_content = content.rstrip('\n') + f'\n\n[ConsoleVariables]\n{cvar_line}\n'

        shutil.copy2(ini_path, ini_path + '.bak')
    else:
        os.makedirs(os.path.dirname(ini_path), exist_ok=True)
        new_content = f'[ConsoleVariables]\n{cvar_line}\n'

    with open(ini_path, 'w', encoding='utf-8') as ini_file:
        ini_file.write(new_content)

    return True, "DefaultEngine.ini updated (restart Unreal to take effect)"


# ============================================================================
# PROJECT SETUP (diagnosis + automatic fix)
# ============================================================================

def resolve_project_dir(project_dir):
    """Normalize the user's UE project path. Returns (dir, uproject_path,
    error_message)."""
    project_dir = bpy.path.abspath(project_dir) if project_dir else ''
    project_dir = os.path.normpath(project_dir)
    if not project_dir or not os.path.isdir(project_dir):
        return None, None, "Point 'UE Project' at the folder containing the .uproject file"
    if os.path.basename(project_dir).lower() == 'config':
        project_dir = os.path.dirname(project_dir)
    uprojects = [f for f in os.listdir(project_dir) if f.lower().endswith('.uproject')]
    if not uprojects:
        return None, None, f"No .uproject file found in '{project_dir}'"
    return project_dir, os.path.join(project_dir, uprojects[0]), None


def read_project_setup(project_dir):
    """
    Read the UE project files and report the state of everything the sync
    needs: Python plugin, USD Importer plugin, remote execution flag.

    :return tuple: (state dict, error message). state is None on error.
    """
    project_dir, uproject_path, error = resolve_project_dir(project_dir)
    if error:
        return None, error

    try:
        with open(uproject_path, 'r', encoding='utf-8-sig') as handle:
            data = json.load(handle)
    except Exception as read_error:
        return None, f"Could not read {os.path.basename(uproject_path)}: {read_error}"

    plugins = {entry.get('Name'): bool(entry.get('Enabled'))
               for entry in data.get('Plugins', [])}
    state = {
        'uproject': uproject_path,
        'ini': os.path.join(project_dir, 'Config', 'DefaultEngine.ini'),
        'python_plugin': plugins.get('PythonScriptPlugin', False),
        'usd_plugin': plugins.get('USDImporter', False),
        'remote_execution': False,
    }
    if os.path.isfile(state['ini']):
        with open(state['ini'], 'r', encoding='utf-8-sig', errors='replace') as handle:
            content = handle.read()
        state['remote_execution'] = bool(re.search(
            r'^\s*bRemoteExecution\s*=\s*True\s*$', content,
            re.IGNORECASE | re.MULTILINE))
    return state, None


def fix_project_setup(project_dir):
    """
    Write the missing settings into the project files: enable the Python and
    USD Importer plugins in the .uproject, turn remote execution on in
    DefaultEngine.ini. Backups (.bak) are created before each modified file.

    :return tuple: (list of fixed items, error message or None)
    """
    state, error = read_project_setup(project_dir)
    if error:
        return [], error

    fixed = []

    # 1) plugins in the .uproject
    if not (state['python_plugin'] and state['usd_plugin']):
        path = state['uproject']
        with open(path, 'r', encoding='utf-8-sig') as handle:
            data = json.load(handle)
        plugins = data.setdefault('Plugins', [])
        for name in ('PythonScriptPlugin', 'USDImporter'):
            entry = next((p for p in plugins if p.get('Name') == name), None)
            if entry is None:
                plugins.append({'Name': name, 'Enabled': True})
                fixed.append(name)
            elif not entry.get('Enabled'):
                entry['Enabled'] = True
                fixed.append(name)
        if fixed:
            shutil.copy2(path, path + '.bak')
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump(data, handle, indent='\t')
                handle.write('\n')

    # 2) remote execution in DefaultEngine.ini
    if not state['remote_execution']:
        ini_path = state['ini']
        line = 'bRemoteExecution=True'
        if os.path.isfile(ini_path):
            with open(ini_path, 'r', encoding='utf-8-sig', errors='replace') as handle:
                content = handle.read()
            shutil.copy2(ini_path, ini_path + '.bak')
            key_pattern = re.compile(r'^\s*bRemoteExecution\s*=\s*\S+\s*$',
                                     re.IGNORECASE | re.MULTILINE)
            if key_pattern.search(content):
                content = key_pattern.sub(line, content, count=1)
            else:
                section_pattern = re.compile(
                    re.escape(PYTHON_SETTINGS_SECTION) + r'\s*$', re.MULTILINE)
                section_match = section_pattern.search(content)
                if section_match:
                    insert_at = section_match.end()
                    content = content[:insert_at] + '\n' + line + content[insert_at:]
                else:
                    content = (content.rstrip('\n')
                               + f'\n\n{PYTHON_SETTINGS_SECTION}\n{line}\n')
        else:
            os.makedirs(os.path.dirname(ini_path), exist_ok=True)
            content = f'{PYTHON_SETTINGS_SECTION}\n{line}\n'
        with open(ini_path, 'w', encoding='utf-8') as handle:
            handle.write(content)
        fixed.append('Remote Execution')

    return fixed, None


# ============================================================================
# OPERATORS
# ============================================================================

class UNREAL_OT_connection_doctor(bpy.types.Operator):
    """Diagnose why the Unreal connection fails and fix the project files if
    needed: enables the Python and USD Importer plugins and turns remote
    execution on (with .bak backups). Restart Unreal after a fix"""
    bl_idname = "unreal_toolkit.connection_doctor"
    bl_label = "Unreal Connection Doctor"
    bl_options = {'REGISTER'}

    apply_fix: bpy.props.BoolProperty(
        name="Fix Project Files",
        description="Write the missing settings into the .uproject and "
                    "Config/DefaultEngine.ini (backups are created). "
                    "Restart Unreal afterwards",
        default=True
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=440)

    def _diagnose(self, context):
        key = context.scene.unreal_toolkit_settings.ue_project_dir
        cached = getattr(self, '_diag_cache', None)
        if cached is not None and cached[0] == key:
            return cached[1]
        result = read_project_setup(key)
        self._diag_cache = (key, result)
        return result

    def draw(self, context):
        layout = self.layout
        settings = context.scene.unreal_toolkit_settings

        box = layout.box()
        box.label(text="Checklist:", icon='INFO')
        box.label(text="1. Unreal Editor must be OPEN with your project loaded")
        box.label(text="2. Only ONE Unreal Editor running (the wrong one may answer)")
        box.label(text="3. Project setup below must be all green, then restart Unreal")

        layout.prop(settings, "ue_project_dir")

        state, error = self._diagnose(context)
        box = layout.box()
        if error:
            box.label(text=error, icon='ERROR')
            return

        def status_row(label, ok):
            row = box.row()
            row.label(text=label, icon='CHECKMARK' if ok else 'CANCEL')

        status_row("Python Editor Script Plugin enabled", state['python_plugin'])
        status_row("USD Importer plugin enabled", state['usd_plugin'])
        status_row("Remote execution turned on", state['remote_execution'])

        all_good = (state['python_plugin'] and state['usd_plugin']
                    and state['remote_execution'])
        if all_good:
            box.label(text="Project files look good. If it still fails,", icon='INFO')
            box.label(text="restart Unreal and check points 1 and 2.")
        else:
            layout.prop(self, "apply_fix")

    def execute(self, context):
        settings = context.scene.unreal_toolkit_settings
        state, error = read_project_setup(settings.ue_project_dir)
        if error:
            self.report({'WARNING'}, error)
            return {'CANCELLED'}

        all_good = (state['python_plugin'] and state['usd_plugin']
                    and state['remote_execution'])
        if all_good:
            self.report({'INFO'}, "Project files are correctly configured. Make sure "
                                  "Unreal is running (and restart it if settings just changed)")
            return {'FINISHED'}

        if not self.apply_fix:
            self.report({'INFO'}, "Nothing changed (Fix Project Files was unticked)")
            return {'CANCELLED'}

        fixed, fix_error = fix_project_setup(settings.ue_project_dir)
        if fix_error:
            self.report({'ERROR'}, fix_error)
            return {'CANCELLED'}
        self.report({'INFO'}, f"Fixed: {', '.join(fixed)}. Now open/restart Unreal Editor")
        return {'FINISHED'}


class UNREAL_OT_test_connection(bpy.types.Operator):
    """Check that a running Unreal Editor answers over remote execution"""
    bl_idname = "unreal_toolkit.test_connection"
    bl_label = "Test Unreal Connection"
    bl_options = {'REGISTER'}

    def execute(self, context):
        success, message = run_unreal_python([
            'import unreal',
            f"unreal.log('{CONNECTION_MARKER}')",
            f"print('{CONNECTION_MARKER}')",
        ])

        if success:
            self.report({'INFO'}, "Unreal Editor connected ✓")
            return {'FINISHED'}

        self.report({'WARNING'}, message)
        # open the doctor so the user sees WHY and can fix the project files
        bpy.ops.unreal_toolkit.connection_doctor('INVOKE_DEFAULT')
        return {'CANCELLED'}


class UNREAL_OT_fix_interchange_permanent(bpy.types.Operator):
    """Write 'Interchange.FeatureFlags.Import.FBX=False' into the project's
    Config/DefaultEngine.ini so FBX files imported into Unreal use the legacy
    FBX importer. A .bak backup of the file is created first (requires an
    Unreal restart)"""
    bl_idname = "unreal_toolkit.fix_interchange_permanent"
    bl_label = "Fix Interchange FBX (Permanent)"
    bl_options = {'REGISTER'}

    def invoke(self, context, event):
        settings = context.scene.unreal_toolkit_settings
        ini_path, error = find_default_engine_ini(settings.ue_project_dir)
        if error:
            self.report({'WARNING'}, error)
            return {'CANCELLED'}

        self._ini_path = ini_path
        return context.window_manager.invoke_props_dialog(self, width=450)

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text="This will modify:", icon='FILE_TICK')
        box.label(text=getattr(self, '_ini_path', ''))
        box.label(text="A .bak backup will be created first.", icon='INFO')
        box.label(text="Restart Unreal Editor afterwards.", icon='INFO')

    def execute(self, context):
        settings = context.scene.unreal_toolkit_settings
        ini_path, error = find_default_engine_ini(settings.ue_project_dir)
        if error:
            self.report({'WARNING'}, error)
            return {'CANCELLED'}

        try:
            changed, message = apply_interchange_fix_to_ini(ini_path)
        except OSError as os_error:
            self.report({'ERROR'}, f"Could not write DefaultEngine.ini: {os_error}")
            return {'CANCELLED'}

        self.report({'INFO'}, message)
        return {'FINISHED'}


classes = (
    UNREAL_OT_connection_doctor,
    UNREAL_OT_test_connection,
    UNREAL_OT_fix_interchange_permanent,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
