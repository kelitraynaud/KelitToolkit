"""Settings and properties for KelitToolkit"""

import bpy


class UnrealToolkitSettings(bpy.types.PropertyGroup):
    """Panel sections (3 groups, workflow order) + Unreal connection settings"""
    panel_search: bpy.props.StringProperty(
        name="Search",
        description="Filter the toolkit's tools by name (label, description or id). "
                    "Clear to get the normal sections back",
        default="",
        options={'TEXTEDIT_UPDATE'},
    )

    show_send: bpy.props.BoolProperty(name="Send to Unreal", default=True)
    show_send_advanced: bpy.props.BoolProperty(name="Advanced", default=False)
    show_prepare: bpy.props.BoolProperty(name="Prepare Assets", default=False)
    show_clean: bpy.props.BoolProperty(name="Clean & Check", default=False)

    ue_project_dir: bpy.props.StringProperty(
        name="UE Project",
        description="Unreal project folder (the one containing the .uproject file) - "
                    "used to write the permanent Interchange FBX fix into Config/DefaultEngine.ini",
        subtype='DIR_PATH',
        default=""
    )

    ue_master_material: bpy.props.StringProperty(
        name="Master Material",
        description="Unreal master material the generated material instances are parented to. "
                    "Created on first use if it does not exist. Point it at your own master "
                    "to inherit your conventions (tessellation, extra parameters...)",
        default="/Game/BlenderSync/M_B2UE_Master"
    )

    usd_content_folder: bpy.props.StringProperty(
        name="Content Folder",
        description="Unreal content folder where USD Scene Sync imports the assets "
                    "(a sub-folder named after the .blend file is created inside). "
                    "Keep it separate from folders holding hand-made assets: a re-sync "
                    "overwrites what it owns, and cleanup is only safe on a folder the "
                    "tool alone writes to",
        default="/Game/BlenderSync"
    )

    # Last-used options of the sync dialog, remembered per scene so they
    # survive Blender restarts instead of resetting to defaults
    sync_source: bpy.props.StringProperty(default='SELECTED')
    sync_place_in_level: bpy.props.BoolProperty(default=True)
    sync_replace_existing: bpy.props.BoolProperty(default=True)
    sync_import_materials: bpy.props.BoolProperty(default=True)
    sync_fix_two_sided: bpy.props.BoolProperty(default=False)
    sync_include_skeletal: bpy.props.BoolProperty(default=True)
    sync_include_animation: bpy.props.BoolProperty(default=False)
    sync_include_camera: bpy.props.BoolProperty(default=True)
    sync_camera_spawnable: bpy.props.BoolProperty(default=True)
    sync_preserve_hierarchy: bpy.props.BoolProperty(default=True)
    sync_key_mode: bpy.props.StringProperty(default='AUTHORED')
    sync_options_saved: bpy.props.BoolProperty(default=False)


UPDATE_REPO_URL = "https://kelitraynaud.github.io/KelitToolkit/index.json"
UPDATE_REPO_NAME = "Kelit Toolkit"


def find_update_repo(context):
    """The registered Kelit Toolkit extension repository, or None."""
    try:
        repos = context.preferences.extensions.repos
    except AttributeError:
        return None
    for repo in repos:
        if getattr(repo, 'remote_url', '') == UPDATE_REPO_URL:
            return repo
    return None


class PREFERENCES_OT_setup_update_repo(bpy.types.Operator):
    """Register the Kelit Toolkit extension repository in Blender, so new
    versions show up natively in Preferences > Get Extensions. If you then
    install the toolkit from that repository, remove this zip-installed copy
    to avoid running two versions at once"""
    bl_idname = "unreal_toolkit.setup_update_repo"
    bl_label = "Enable Native Updates"
    bl_options = {'REGISTER'}

    def execute(self, context):
        try:
            repos = context.preferences.extensions.repos
        except AttributeError:
            self.report({'WARNING'},
                        "This Blender version has no extension repositories")
            return {'CANCELLED'}

        if find_update_repo(context) is not None:
            self.report({'INFO'}, "Update repository already registered")
            return {'FINISHED'}

        try:
            repo = repos.new(name=UPDATE_REPO_NAME,
                             module="kelittoolkit_updates",
                             remote_url=UPDATE_REPO_URL)
        except TypeError:
            repo = repos.new()
            repo.name = UPDATE_REPO_NAME
            repo.module = "kelittoolkit_updates"
        if hasattr(repo, 'use_remote_url'):
            repo.use_remote_url = True
        repo.remote_url = UPDATE_REPO_URL

        try:
            bpy.ops.wm.save_userpref()
        except Exception:
            pass
        # first sync so the extension shows up right away (needs network)
        try:
            bpy.ops.extensions.repo_sync_all()
        except Exception:
            pass
        self.report({'INFO'}, "Repository added: updates now show in "
                              "Preferences > Get Extensions")
        return {'FINISHED'}


class KelitToolkitPreferences(bpy.types.AddonPreferences):
    """Add-on preferences: native updates and Unreal remote-execution
    endpoints, for projects that changed UE's defaults."""
    bl_idname = __package__

    multicast_group: bpy.props.StringProperty(
        name="Multicast Group",
        description="UDP multicast group Unreal's Python plugin broadcasts on "
                    "(UE default: 239.0.0.1)",
        default="239.0.0.1"
    )

    multicast_port: bpy.props.IntProperty(
        name="Multicast Port",
        description="Port of the multicast group (UE default: 6766)",
        default=6766, min=1, max=65535
    )

    command_port: bpy.props.IntProperty(
        name="Command Port",
        description="Local TCP port this add-on listens on for the editor's "
                    "command connection (UE default: 6776)",
        default=6776, min=1, max=65535
    )

    def draw(self, context):
        layout = self.layout

        box = layout.box()
        box.label(text="Updates:", icon='FILE_REFRESH')
        if find_update_repo(context) is not None:
            box.label(text="Native updates enabled: new versions appear in "
                           "Preferences > Get Extensions", icon='CHECKMARK')
        else:
            box.operator(PREFERENCES_OT_setup_update_repo.bl_idname,
                         icon='URL')
            box.label(text="Registers the toolkit's update repository in "
                           "Blender (one click, once)")

        layout.separator()
        layout.label(text="Unreal remote execution - only change these if your "
                          "UE project uses non-default Python settings:")
        row = layout.row()
        row.prop(self, "multicast_group")
        row.prop(self, "multicast_port")
        layout.prop(self, "command_port")


def register():
    bpy.utils.register_class(UnrealToolkitSettings)
    bpy.utils.register_class(PREFERENCES_OT_setup_update_repo)
    bpy.utils.register_class(KelitToolkitPreferences)
    bpy.types.Scene.unreal_toolkit_settings = bpy.props.PointerProperty(type=UnrealToolkitSettings)


def unregister():
    del bpy.types.Scene.unreal_toolkit_settings
    bpy.utils.unregister_class(KelitToolkitPreferences)
    bpy.utils.unregister_class(PREFERENCES_OT_setup_update_repo)
    bpy.utils.unregister_class(UnrealToolkitSettings)
