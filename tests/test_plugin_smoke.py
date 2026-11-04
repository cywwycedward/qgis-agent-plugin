"""The package must import and expose what QGIS loads and what the gateway serves."""

import qgis_agent_plugin


def test_class_factory_is_exported() -> None:
    assert callable(qgis_agent_plugin.classFactory)


def test_wire_surface_is_declared() -> None:
    from qgis_agent_plugin.qgis_actions import COMMAND_NAMES, REGISTRY

    assert COMMAND_NAMES
    assert set(COMMAND_NAMES) == set(REGISTRY)


def test_ping_answers_without_qgis_state() -> None:
    from qgis_agent_plugin.qgis_actions.qgis.ping import Ping

    assert Ping.name == "ping"
