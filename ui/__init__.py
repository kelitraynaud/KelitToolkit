"""UI package for KelitToolkit"""

from . import panel
from . import menu


def register():
    panel.register()
    menu.register()


def unregister():
    menu.unregister()
    panel.unregister()
