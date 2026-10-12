"""Standalone Unreal remote-execution transport.

Discover a running Unreal Editor, send Python to it, collect the output -
built on the vendored Epic remote_execution module (see dependencies/).
"""

import time
import os
from contextlib import contextmanager

_PINNED_EDITOR = None


class TargetSelectionError(RuntimeError):
    pass


def project_key(path):
    return os.path.normcase(os.path.normpath(path or '')).replace('\\', '/').casefold()


def choose_editor(nodes, project=''):
    candidates = [n for n in nodes if not project or
                  project_key(n.get('project_root')) == project_key(project)]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise TargetSelectionError('Selected Unreal project is not available: ' + project
                                   if project else 'No Unreal editor answered discovery')
    raise TargetSelectionError('Multiple Unreal editors are open. Choose the destination '
                               'with Choose Project in the Send panel (one editor per project).')


def selected_project():
    try:
        import bpy
        return bpy.context.scene.kelit_toolkit_settings.ue_target_project
    except (ImportError, AttributeError):
        return ''


def resolve_target():
    if _PINNED_EDITOR is not None:
        return _PINNED_EDITOR
    return choose_editor(find_editors(wait_seconds=0.8), selected_project())


@contextmanager
def pin_editor(node):
    """Keep geometry and material RPCs on the same running editor."""
    global _PINNED_EDITOR
    previous = _PINNED_EDITOR
    _PINNED_EDITOR = dict(node)
    try:
        yield
    finally:
        _PINNED_EDITOR = previous


def _get_remote_execution():
    from .. import dependencies
    from ..dependencies import remote_execution
    return remote_execution


def addon_root_package(package_name):
    """Root package of the add-on, the key of its preferences: 'KelitToolkit'
    for a classic install, 'bl_ext.<repo>.kelittoolkit' for an install from
    the extension repository. Splitting on the first dot returned 'bl_ext'
    for the latter, and the custom endpoints were silently ignored."""
    return package_name.rsplit('.operators', 1)[0]


def _build_config(remote_execution):
    """Connection config, honouring the add-on preferences when they exist
    (projects that changed UE's default remote-execution endpoints)."""
    config = remote_execution.RemoteExecutionConfig()
    try:
        import bpy
        package = addon_root_package(__package__)
        prefs = bpy.context.preferences.addons[package].preferences
        config.multicast_group_endpoint = (prefs.multicast_group, prefs.multicast_port)
        config.command_endpoint = ('127.0.0.1', prefs.command_port)
    except Exception:
        pass
    return config


def _format_response(response):
    """Flatten Unreal's structured response into plain text (warnings dropped)."""
    if not response:
        return ''
    parts = []
    output = response.get('output')
    if output:
        parts.append('\n'.join(
            line['output'] for line in output if line.get('type') != 'Warning'))
    result = response.get('result')
    if result and result != 'None':
        parts.append(str(result))
    return '\n'.join(parts)


def run_commands(commands, connection_attempts=300):
    """
    Run a list of Python statements in a running Unreal Editor and return its
    stdout as text.

    :param list commands: Python statements to execute in Unreal.
    :param int connection_attempts: discovery retries, ~0.1s apart (300 = 30s).
        The editor stops answering discovery pings while its main thread is
        held - a modal dialog, a heavy import, shader compilation - so short
        patience misreads a busy editor as a closed one. Returns as soon as
        an editor answers, so the usual cost is well under a second.
    :raises ConnectionError: when no editor answers.
    :return str: the editor's textual output.
    """
    remote_execution = _get_remote_execution()

    remote_exec = remote_execution.RemoteExecution(_build_config(remote_execution))
    remote_exec.start()
    try:
        response = _run(remote_exec, commands, connection_attempts)
    finally:
        remote_exec.stop()
    return _format_response(response)


def _run(remote_exec, commands, attempts):
    try:
        for attempt in range(max(1, attempts)):
            time.sleep(0.1)
            nodes = list(remote_exec.remote_nodes)
            if _PINNED_EDITOR is not None:
                nodes = [n for n in nodes if n.get('node_id') == _PINNED_EDITOR.get('node_id')
                         and project_key(n.get('project_root')) == project_key(_PINNED_EDITOR.get('project_root'))]
                if not nodes:
                    continue
            elif attempt < 7:
                continue  # collect discovery replies before deciding there is just one editor
            if not nodes:
                continue
            node = choose_editor(nodes, '' if _PINNED_EDITOR else selected_project())
            remote_exec.open_command_connection(node.get('node_id'))
            if remote_exec.has_command_connection():
                response = remote_exec.run_command('\n'.join(commands), unattended=False)
                if response and response.get('success') is False:
                    raise RuntimeError(response.get('result') or _format_response(response))
                return response
        if _PINNED_EDITOR:
            raise TargetSelectionError('The pinned Unreal editor is no longer available. Retry the send.')
        raise ConnectionError('Could not find an open Unreal Editor instance!')
    except (ConnectionError, TargetSelectionError):
        raise
    except Exception as error:
        raise RuntimeError(f'Unreal command failed: {error}') from error


def find_editors(wait_seconds=2.0):
    """Return the remote nodes (running editors) currently discoverable."""
    remote_execution = _get_remote_execution()
    remote_exec = remote_execution.RemoteExecution(_build_config(remote_execution))
    remote_exec.start()
    try:
        time.sleep(wait_seconds)
        return list(remote_exec.remote_nodes)
    finally:
        remote_exec.stop()
