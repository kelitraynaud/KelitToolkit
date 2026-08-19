"""Unreal Editor remote link.

Sends Python to a running Unreal Editor through the vendored remote-execution
transport (Epic's module, see dependencies/). Also hosts the permanent
'Interchange.FeatureFlags.Import.FBX=False' fix for projects that import FBX
through UE's legacy importer.

Unreal side requirement: the 'Python Editor Script Plugin' enabled with
remote execution turned on.
"""

import os
import re
import shutil

import bpy


INTERCHANGE_CVAR = 'Interchange.FeatureFlags.Import.FBX'
CONNECTION_MARKER = 'KelitToolkit-OK'


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
# OPERATORS
# ============================================================================

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
    UNREAL_OT_test_connection,
    UNREAL_OT_fix_interchange_permanent,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
