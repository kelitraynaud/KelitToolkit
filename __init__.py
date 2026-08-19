# KelitToolkit
# Copyright (C) 2026 Kélit Raynaud
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
# dependencies/remote_execution.py is Copyright Epic Games, Inc., taken from
# the MIT-licensed BlenderTools project and redistributed under its own terms.

bl_info = {
    "name": "Kelit Toolkit",
    "author": "Kélit Raynaud",
    "version": (0, 9, 0),
    "blender": (5, 0, 0),
    "location": "3D View > Sidebar (N) > Kelit Toolkit",
    "description": "Prepare and send Blender scenes to Unreal Engine: naming, collisions, "
                   "LODs, materials, USD scene sync with native LevelSequence animation",
    "doc_url": "https://github.com/kelitraynaud/KelitToolkit",
    "tracker_url": "https://github.com/kelitraynaud/KelitToolkit/issues",
    "category": "Object",
}

import bpy

# Support Blender's "Reload Scripts": re-import submodules already in memory
if "settings" in locals():
    import importlib
    settings = importlib.reload(settings)
    operators = importlib.reload(operators)
    ui = importlib.reload(ui)
else:
    from . import settings
    from . import operators
    from . import ui

# For keyboard shortcuts
addon_keymaps = []


def register():
    """Register all addon components"""
    # Register settings first (needed by UI)
    settings.register()

    # Register operators
    operators.register()

    # Register UI
    ui.register()

    # Keyboard shortcuts
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name="Object Mode", space_type="EMPTY")

        # Alt+Shift+R : Replace With Active Instance
        kmi = km.keymap_items.new(
            "object.replace_with_active_instance",
            type='R', value='PRESS', alt=True, shift=True
        )
        addon_keymaps.append((km, kmi))

        # Alt+Shift+N : Normalize Names (Quick)
        kmi = km.keymap_items.new(
            "object.normalize_names_quick",
            type='N', value='PRESS', alt=True, shift=True
        )
        addon_keymaps.append((km, kmi))


def unregister():
    """Unregister all addon components"""
    # Remove keyboard shortcuts
    for km, kmi in addon_keymaps:
        km.keymap_items.remove(kmi)
    addon_keymaps.clear()

    # Unregister in reverse order
    ui.unregister()
    operators.unregister()
    settings.unregister()


if __name__ == "__main__":
    register()
