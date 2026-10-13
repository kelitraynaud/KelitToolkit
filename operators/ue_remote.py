"""Standalone Unreal remote-execution transport.

Discover a running Unreal Editor, send Python to it, collect the output -
built on the vendored Epic remote_execution module (see dependencies/).
"""

import time
import os
from contextlib import contextmanager

_PINNED_EDITOR = None

POLL_SECONDS = 0.1
# Epic's transport pings every second: listening a little longer than one
# ping interval gives every open editor the chance to answer before
# Automatic mode decides there is only one.
DISCOVERY_WINDOW = 1.2
# after the first answer, wait this long for the others
SETTLE_SECONDS = 0.3
# the editor stops answering while its main thread is held (a modal dialog,
# a heavy import, shader compilation): short patience misreads a busy
# editor as a closed one
PATIENCE_SECONDS = 30.0


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
    projects = sorted({n.get('project_name') or n.get('project_root') or '?' for n in candidates})
    if len(projects) > 1:
        raise TargetSelectionError(
            '%d Unreal projects are open (%s). Choose the destination with Choose Project '
            'in the Send panel.' % (len(projects), ', '.join(projects)))
    raise TargetSelectionError('Several Unreal editors have %s open. Keep one editor per '
                               'project.' % projects[0])


def wait_for_target(poll, project='', pinned=None, window=DISCOVERY_WINDOW,
                    patience=PATIENCE_SECONDS, settle=SETTLE_SECONDS, interval=POLL_SECONDS,
                    clock=time.monotonic, sleep=time.sleep):
    """
    Listen to discovery replies and return the editor to talk to.

    :param callable poll: returns the editors currently discovered.
    :param str project: project chosen with Choose Project ('' = Automatic).
    :param dict pinned: the editor a running send is pinned to.

    Automatic mode keeps listening for the whole discovery window (and a
    short settle time after the first answer) before deciding, and refuses
    when more than one project answered. A chosen project, or the pinned
    editor, is waited for within the patience: a busy editor answers late.

    :raises TargetSelectionError: ambiguous, or the chosen editor never answered
        while others did.
    :raises ConnectionError: no editor answered at all.
    """
    start = clock()
    first_seen = None
    nodes = []
    while True:
        sleep(interval)
        now = clock()
        nodes = list(poll() or [])
        if pinned is not None:
            matches = [n for n in nodes if n.get('node_id') == pinned.get('node_id')
                       and project_key(n.get('project_root')) == project_key(pinned.get('project_root'))]
            if matches:
                return matches[0]
        elif project:
            matches = [n for n in nodes
                       if project_key(n.get('project_root')) == project_key(project)]
            if matches:
                return choose_editor(matches, project)
        elif nodes:
            if first_seen is None:
                first_seen = now
            if now - start >= window and now - first_seen >= settle:
                return choose_editor(nodes)
        if now - start >= patience:
            break
    if pinned is not None:
        raise TargetSelectionError('The pinned Unreal editor is no longer available. Retry the send.')
    if project and nodes:
        open_projects = ', '.join(sorted({n.get('project_name') or '?' for n in nodes}))
        raise TargetSelectionError('Selected Unreal project is not available: %s (open: %s). '
                                   'Open it, or pick another with Choose Project.'
                                   % (project, open_projects))
    raise ConnectionError('No Unreal editor answered discovery')


def selected_project():
    try:
        import bpy
        return bpy.context.scene.kelit_toolkit_settings.ue_target_project
    except (ImportError, AttributeError):
        return ''


def resolve_target(patience=PATIENCE_SECONDS):
    """The editor a send must use (the pinned one while a send runs)."""
    if _PINNED_EDITOR is not None:
        return _PINNED_EDITOR
    remote_execution = _get_remote_execution()
    remote_exec = remote_execution.RemoteExecution(_build_config(remote_execution))
    remote_exec.start()
    try:
        return wait_for_target(lambda: remote_exec.remote_nodes, selected_project(),
                               patience=patience)
    finally:
        remote_exec.stop()


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
        patience misreads a busy editor as a closed one.
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
    patience = max(1, attempts) * POLL_SECONDS
    deadline = time.monotonic() + patience
    try:
        while True:
            node = wait_for_target(
                lambda: remote_exec.remote_nodes,
                '' if _PINNED_EDITOR else selected_project(),
                pinned=_PINNED_EDITOR,
                patience=max(POLL_SECONDS, deadline - time.monotonic()))
            remote_exec.open_command_connection(node.get('node_id'))
            if remote_exec.has_command_connection():
                response = remote_exec.run_command('\n'.join(commands), unattended=False)
                if response and response.get('success') is False:
                    raise RuntimeError(response.get('result') or _format_response(response))
                return response
            if time.monotonic() >= deadline:
                raise ConnectionError('Could not find an open Unreal Editor instance!')
    except (ConnectionError, TargetSelectionError):
        raise
    except Exception as error:
        raise RuntimeError(f'Unreal command failed: {error}') from error


def find_editors(wait_seconds=DISCOVERY_WINDOW):
    """Return the remote nodes (running editors) currently discoverable."""
    remote_execution = _get_remote_execution()
    remote_exec = remote_execution.RemoteExecution(_build_config(remote_execution))
    remote_exec.start()
    try:
        time.sleep(wait_seconds)
        return list(remote_exec.remote_nodes)
    finally:
        remote_exec.stop()
