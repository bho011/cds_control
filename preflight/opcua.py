"""Prüft, ob der OPC-UA-Endpoint lesbar ist (ein Testwert)."""

from __future__ import annotations

import asyncio

from asyncua import Client as OpcUaClient

from mqtt_sensor_bridge import NODE_IDS
from services.system_config import get_opcua_config

from .report import PreflightReport

_SYSTEM_OPCUA_CONFIG = get_opcua_config()

OPCUA_ENDPOINT = str(_SYSTEM_OPCUA_CONFIG["endpoint"])
OPCUA_TIMEOUT_SECONDS = 5


async def check_opcua_endpoint_async(report: PreflightReport):
    try:
        async def read_test_value():
            async with OpcUaClient(url=OPCUA_ENDPOINT) as opcua_client:
                node_id = NODE_IDS["ro_level_raw_ibc1"]
                node = opcua_client.get_node(node_id)
                value = await node.read_value()
                return node_id, value

        node_id, value = await asyncio.wait_for(
            read_test_value(),
            timeout=OPCUA_TIMEOUT_SECONDS,
        )

        report.ok("OPC-UA read test", f"{node_id} -> {value}")

    except Exception as exc:
        report.fail("OPC-UA read test", str(exc))


def check_opcua_endpoint(report: PreflightReport):
    asyncio.run(check_opcua_endpoint_async(report))
