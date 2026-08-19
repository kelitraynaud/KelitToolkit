"""Standalone Unreal remote-execution transport.

Discover a running Unreal Editor, send Python to it, collect the output -
built on the vendored Epic remote_execution module (see dependencies/).
"""

import time


def _get_remote_execution():
    from .. import dependencies
    from ..dependencies import remote_execution
    return remote_execution


def _build_config(remote_execution):
    """Connection config, honouring the add-on preferences when they exist
    (projects that changed UE's default remote-execution endpoints)."""
    config = remote_execution.RemoteExecutionConfig()
    try:
        import bpy
        package = __package__.split('.')[0]
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
        for _ in range(max(1, attempts)):
            time.sleep(0.1)
            for node in remote_exec.remote_nodes:
                remote_exec.open_command_connection(node.get('node_id'))
            if remote_exec.has_command_connection():
                return remote_exec.run_command('\n'.join(commands), unattended=False)
        raise ConnectionError('Could not find an open Unreal Editor instance!')
    except ConnectionError:
        raise
    except Exception as error:
        raise ConnectionError(f'Could not reach the Unreal Editor: {error}')


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
