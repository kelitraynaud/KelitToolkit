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


class KelitToolkitPreferences(bpy.types.AddonPreferences):
    """Add-on preferences: Unreal remote-execution endpoints, for projects
    that changed UE's defaults (Project Settings > Plugins > Python)."""
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

    def draw(self, _context):
        layout = self.layout
        layout.label(text="Unreal remote execution - only change these if your "
                          "UE project uses non-default Python settings:")
        row = layout.row()
        row.prop(self, "multicast_group")
        row.prop(self, "multicast_port")
        layout.prop(self, "command_port")


def register():
    bpy.utils.register_class(UnrealToolkitSettings)
    bpy.utils.register_class(KelitToolkitPreferences)
    bpy.types.Scene.unreal_toolkit_settings = bpy.props.PointerProperty(type=UnrealToolkitSettings)


def unregister():
    del bpy.types.Scene.unreal_toolkit_settings
    bpy.utils.unregister_class(KelitToolkitPreferences)
    bpy.utils.unregister_class(UnrealToolkitSettings)
