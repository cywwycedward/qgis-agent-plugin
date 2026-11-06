"""The package must import and expose what QGIS loads and what the pipeline serves."""

import qgis_agent_plugin


def test_class_factory_is_exported() -> None:
    assert callable(qgis_agent_plugin.classFactory)


def test_wire_surface_is_declared() -> None:
    from qgis_agent_plugin.commands import COMMANDS

    assert COMMANDS
    # Every declared command is reachable by the name it declares.
    for name, command in COMMANDS.items():
        assert command.name == name


def test_ping_answers_without_qgis_state() -> None:
    from qgis_agent_plugin.command import CommandContext, Params
    from qgis_agent_plugin.commands.qgis.ping import Ping, PingResult

    assert Ping().run(CommandContext(), Params()) == PingResult()
