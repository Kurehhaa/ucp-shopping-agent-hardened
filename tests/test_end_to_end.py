"""End-to-end: real HTTP server, in-process mock merchants, full shopping flow."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import uvicorn

from ucp_shopping.config import Settings
from ucp_shopping.main import build_app

ADDRESS = {
    "full_name": "Aigerim S.",
    "line1": "12 Abay Ave",
    "city": "Almaty",
    "postal_code": "050000",
    "country": "KZ",
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def server() -> AsyncIterator[str]:
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    settings = Settings(
        environment="testing",
        host="127.0.0.1",
        port=port,
        human_confirmation_required=True,
        known_merchant_urls=[
            f"{base}/merchants/{m}" for m in ("techzone", "homegoods", "megamart")
        ],
        openai_api_key="",
        anthropic_api_key="",
    )
    config = uvicorn.Config(build_app(settings), host="127.0.0.1", port=port, log_level="warning")
    uv = uvicorn.Server(config)
    task = asyncio.create_task(uv.serve())
    for _ in range(100):
        if uv.started:
            break
        await asyncio.sleep(0.05)
    assert uv.started, "test server did not start"
    try:
        yield base
    finally:
        uv.should_exit = True
        await task


async def _wait_for(client: httpx.AsyncClient, session_id: str, states: set[str]) -> dict[str, Any]:
    for _ in range(200):
        data = (await client.get(f"/api/v1/shop/{session_id}")).json()
        if data["state"] in states:
            return data
        await asyncio.sleep(0.05)
    raise AssertionError(f"session never reached {states}, last state: {data['state']}")


async def _merchant_orders(client: httpx.AsyncClient, base: str) -> list[dict[str, Any]]:
    orders: list[dict[str, Any]] = []
    for slug in ("techzone", "homegoods", "megamart"):
        body = (await client.get(f"{base}/merchants/{slug}/api/v1/orders")).json()
        orders.extend(body["orders"] if isinstance(body, dict) else body)
    return orders


async def test_full_flow_places_the_requested_quantity_with_the_given_address(server):
    async with httpx.AsyncClient(base_url=server, timeout=10) as client:
        created = await client.post(
            "/api/v1/shop",
            json={"query": "2x mechanical keyboard", "shipping_address": ADDRESS},
        )
        assert created.status_code == 200
        session_id = created.json()["session_id"]

        waiting = await _wait_for(client, session_id, {"awaiting_confirmation", "failed"})
        assert waiting["state"] == "awaiting_confirmation"
        plan = waiting["optimization_plan"]
        assert plan["items"] and all(i["quantity"] == 2 for i in plan["items"])
        assert plan["over_budget"] is False
        assert plan["grand_total"] == round(sum(i["total"] for i in plan["items"]), 2)

        # nothing is bought before the user confirms
        assert await _merchant_orders(client, server) == []

        assert (await client.post(f"/api/v1/shop/{session_id}/confirm")).status_code == 200
        done = await _wait_for(client, session_id, {"completed", "failed"})
        assert done["state"] == "completed", done.get("error")
        assert done["checkout_failures"] == []

        placed = await _merchant_orders(client, server)
        assert len(placed) == len(done["orders"]) >= 1
        for order in placed:
            assert all(line["quantity"] == 2 for line in order["line_items"])
            assert order["shipping_address"]["city"] == "Almaty"
            assert order["shipping_address"]["country"] == "KZ"


async def test_over_budget_plan_is_never_ordered(server):
    async with httpx.AsyncClient(base_url=server, timeout=10) as client:
        created = await client.post(
            "/api/v1/shop",
            json={"query": "mechanical keyboard", "budget": 10, "shipping_address": ADDRESS},
        )
        session_id = created.json()["session_id"]
        waiting = await _wait_for(client, session_id, {"awaiting_confirmation", "failed"})
        assert waiting["optimization_plan"]["over_budget"] is True

        await client.post(f"/api/v1/shop/{session_id}/confirm")
        done = await _wait_for(client, session_id, {"completed", "failed"})
        assert done["state"] == "failed"
        assert "budget" in done["error"]
        assert await _merchant_orders(client, server) == []


async def test_confirming_twice_does_not_order_twice(server):
    async with httpx.AsyncClient(base_url=server, timeout=10) as client:
        created = await client.post(
            "/api/v1/shop", json={"query": "mechanical keyboard", "shipping_address": ADDRESS}
        )
        session_id = created.json()["session_id"]
        await _wait_for(client, session_id, {"awaiting_confirmation"})

        await asyncio.gather(
            client.post(f"/api/v1/shop/{session_id}/confirm"),
            client.post(f"/api/v1/shop/{session_id}/confirm"),
        )
        done = await _wait_for(client, session_id, {"completed", "failed"})
        assert done["state"] == "completed"
        assert len(await _merchant_orders(client, server)) == len(done["orders"])


async def test_unavailable_product_is_reported(server):
    async with httpx.AsyncClient(base_url=server, timeout=10) as client:
        created = await client.post(
            "/api/v1/shop", json={"query": "zzzqqq unobtainium", "shipping_address": ADDRESS}
        )
        session_id = created.json()["session_id"]
        data = await _wait_for(client, session_id, {"awaiting_confirmation", "failed"})
        plan = data.get("optimization_plan")
        assert plan is None or plan["items"] == []
