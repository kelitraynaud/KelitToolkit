"""Operators package for KelitToolkit"""

from . import instances
from . import origin
from . import naming
from . import materials
from . import material_conversion
from . import textures
from . import mesh_tools
from . import scene_cleanup
from . import export
from . import unified_export
from . import unreal_link
from . import usd_sync
from . import ue_materials


def register():
    instances.register()
    origin.register()
    naming.register()
    materials.register()
    material_conversion.register()
    textures.register()
    mesh_tools.register()
    scene_cleanup.register()
    export.register()
    unified_export.register()
    unreal_link.register()
    usd_sync.register()
    ue_materials.register()


def unregister():
    ue_materials.unregister()
    usd_sync.unregister()
    unreal_link.unregister()
    unified_export.unregister()
    export.unregister()
    scene_cleanup.unregister()
    mesh_tools.unregister()
    textures.unregister()
    material_conversion.unregister()
    materials.unregister()
    naming.unregister()
    origin.unregister()
    instances.unregister()
