"""Failure handling when one merchant cannot complete its part of an order."""

from __future__ import annotations

import httpx
import pytest

from ucp_shopping.agents.checkout_agent import CheckoutAgent
from ucp_shopping.config import Settings
from ucp_shopping.mock_merchants.merchant_factory import MerchantFactory
from ucp_shopping.models import MerchantInfo, SplitOrderItem, SplitOrderPlan
from ucp_shopping.protocols.ucp_client import UCPClient, UCPClientError


class ScriptedClient:
    """Fake merchant API: fails the steps listed in ``fail`` for given merchants."""

    def __init__(self, fail: dict[str, str] | None = None) -> None:
        self.fail = fail or {}  # "<merchant-url>:<step>" -> error text
        self.calls: list[tuple[str, str, str]] = []  # (step, url, session)
        self.keys: list[str | None] = []

    def _maybe_fail(self, step: str, url: str) -> None:
        message = self.fail.get(f"{url}:{step}")
        if message:
            raise UCPClientError(message)

    async def create_checkout(self, url, line_items, idempotency_key=None):
        self.calls.append(("create", url, ""))
        self.keys.append(idempotency_key)
        self._maybe_fail("create", url)
        return {"id": f"co-{url}"}

    async def update_checkout(self, url, sid, data):
        self.calls.append(("update", url, sid))
        self._maybe_fail("update", url)
        return {}

    async def complete_checkout(self, url, sid, idempotency_key=None):
        self.calls.append(("complete", url, sid))
        self.keys.append(idempotency_key)
        self._maybe_fail("complete", url)
        return {"order_id": f"ord-{url}"}

    async def cancel_checkout(self, url, sid):
        self.calls.append(("cancel", url, sid))
        self._maybe_fail("cancel", url)
        return {}

    async def close(self):
        return None


MERCHANTS = {
    "a": MerchantInfo(id="a", name="Shop A", url="http://a.test"),
    "b": MerchantInfo(id="b", name="Shop B", url="http://b.test"),
}


def _plan() -> SplitOrderPlan:
    items = [
        SplitOrderItem(
            product_name=f"item-{m}",
            product_id=f"p-{m}",
            merchant_name=f"Shop {m.upper()}",
            merchant_id=m,
            price=10.0,
            shipping_cost=2.0,
        )
        for m in ("a", "b")
    ]
    return SplitOrderPlan(items=items, merchants_used=2, grand_total=24.0)


def _agent(client: ScriptedClient) -> CheckoutAgent:
    agent = CheckoutAgent(Settings())
    agent._ucp_client = client  # type: ignore[assignment]
    return agent


def _steps(client: ScriptedClient, step: str) -> list[str]:
    return [url for s, url, _ in client.calls if s == step]


class TestBestEffort:
    async def test_all_merchants_succeed(self):
        result = await _agent(ScriptedClient()).execute_checkouts(_plan(), MERCHANTS)
        assert result.succeeded
        assert {o.merchant_id for o in result.orders} == {"a", "b"}
        assert result.failures == []

    async def test_one_failing_merchant_does_not_block_the_other(self):
        client = ScriptedClient({"http://b.test:create": "out of stock"})
        result = await _agent(client).execute_checkouts(_plan(), MERCHANTS)
        assert [o.merchant_id for o in result.orders] == ["a"]
        assert [(f.merchant_id, f.step) for f in result.failures] == [("b", "prepare")]
        assert "out of stock" in result.failures[0].error
        assert not result.succeeded
        assert "Shop B (prepare)" in result.summary()

    async def test_failed_completion_is_reported_and_not_cancelled(self):
        client = ScriptedClient({"http://b.test:complete": "timeout"})
        result = await _agent(client).execute_checkouts(_plan(), MERCHANTS)
        assert [o.merchant_id for o in result.orders] == ["a"]
        assert [(f.merchant_id, f.step) for f in result.failures] == [("b", "complete")]
        # A timed-out completion may have worked; cancelling could undo a real order.
        assert _steps(client, "cancel") == []

    async def test_all_merchants_failing_gives_no_orders(self):
        client = ScriptedClient({"http://a.test:create": "down", "http://b.test:create": "down"})
        result = await _agent(client).execute_checkouts(_plan(), MERCHANTS)
        assert result.orders == []
        assert len(result.failures) == 2

    async def test_unknown_merchant_is_reported(self):
        result = await _agent(ScriptedClient()).execute_checkouts(_plan(), {"a": MERCHANTS["a"]})
        assert [o.merchant_id for o in result.orders] == ["a"]
        assert result.failures[0].merchant_id == "b"

    async def test_half_prepared_session_is_released(self):
        client = ScriptedClient({"http://b.test:update": "bad address"})
        result = await _agent(client).execute_checkouts(_plan(), MERCHANTS)
        assert [f.step for f in result.failures] == ["prepare"]
        assert _steps(client, "cancel") == ["http://b.test"]


class TestRequireAllMerchants:
    async def test_nothing_is_bought_if_one_merchant_cannot_be_prepared(self):
        client = ScriptedClient({"http://b.test:create": "out of stock"})
        result = await _agent(client).execute_checkouts(
            _plan(), MERCHANTS, require_all_merchants=True
        )
        assert result.orders == []
        assert _steps(client, "complete") == []
        # the merchant that was prepared is released again
        assert _steps(client, "cancel") == ["http://a.test"]
        assert {(f.merchant_id, f.step) for f in result.failures} == {
            ("b", "prepare"),
            ("a", "cancelled"),
        }

    async def test_failed_cancellation_is_reported_not_hidden(self):
        client = ScriptedClient(
            {"http://b.test:create": "out of stock", "http://a.test:cancel": "502"}
        )
        result = await _agent(client).execute_checkouts(
            _plan(), MERCHANTS, require_all_merchants=True
        )
        cancelled = next(f for f in result.failures if f.merchant_id == "a")
        assert "may stay open" in cancelled.error

    async def test_everything_works_when_all_merchants_are_fine(self):
        result = await _agent(ScriptedClient()).execute_checkouts(
            _plan(), MERCHANTS, require_all_merchants=True
        )
        assert result.succeeded


class TestIdempotency:
    async def test_keys_are_stable_for_the_same_session(self):
        first, second = ScriptedClient(), ScriptedClient()
        await _agent(first).execute_checkouts(_plan(), MERCHANTS, session_id="s1")
        await _agent(second).execute_checkouts(_plan(), MERCHANTS, session_id="s1")
        assert sorted(k for k in first.keys if k) == sorted(k for k in second.keys if k)
        assert all(k and k.startswith("s1:") for k in first.keys)

    async def test_keys_differ_between_merchants_steps_and_sessions(self):
        one, two = ScriptedClient(), ScriptedClient()
        await _agent(one).execute_checkouts(_plan(), MERCHANTS, session_id="s1")
        await _agent(two).execute_checkouts(_plan(), MERCHANTS, session_id="s2")
        keys = [k for k in one.keys + two.keys if k]
        assert len(keys) == len(set(keys)) == 8


@pytest.fixture
def merchant_client():
    """A real UCPClient talking to the in-process mock merchant."""
    merchant = MerchantFactory.create_all_merchants()["techzone"]
    transport = httpx.ASGITransport(app=merchant.app)
    client = UCPClient(timeout=5)

    async def _get() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, base_url="http://merchant.test")

    client._get_client = _get  # type: ignore[method-assign]
    return client


class TestMockMerchantContract:
    async def _first_product(self, client: UCPClient) -> str:
        http = await client._get_client()
        data = (await http.get("/api/v1/catalog/products")).json()
        products = data["products"] if isinstance(data, dict) else data
        return products[0]["id"]

    async def test_create_with_same_key_returns_the_same_session(self, merchant_client):
        pid = await self._first_product(merchant_client)
        items = [{"product_id": pid, "quantity": 1}]
        a = await merchant_client.create_checkout("http://merchant.test", items, "k1")
        b = await merchant_client.create_checkout("http://merchant.test", items, "k1")
        c = await merchant_client.create_checkout("http://merchant.test", items, "k2")
        assert a["id"] == b["id"] != c["id"]

    async def test_completing_twice_returns_the_same_order(self, merchant_client):
        pid = await self._first_product(merchant_client)
        url = "http://merchant.test"
        session = await merchant_client.create_checkout(url, [{"product_id": pid, "quantity": 1}])
        first = await merchant_client.complete_checkout(url, session["id"])
        second = await merchant_client.complete_checkout(url, session["id"])
        assert first["order_id"] == second["order_id"]

    async def test_cancelled_session_cannot_be_completed(self, merchant_client):
        pid = await self._first_product(merchant_client)
        url = "http://merchant.test"
        session = await merchant_client.create_checkout(url, [{"product_id": pid, "quantity": 1}])
        await merchant_client.cancel_checkout(url, session["id"])
        with pytest.raises(UCPClientError):
            await merchant_client.complete_checkout(url, session["id"])

    async def test_completed_session_cannot_be_cancelled(self, merchant_client):
        pid = await self._first_product(merchant_client)
        url = "http://merchant.test"
        session = await merchant_client.create_checkout(url, [{"product_id": pid, "quantity": 1}])
        await merchant_client.complete_checkout(url, session["id"])
        with pytest.raises(UCPClientError):
            await merchant_client.cancel_checkout(url, session["id"])


class TestFinalState:
    async def test_session_is_failed_when_no_order_was_placed(self):
        from ucp_shopping.models import CheckoutFailure, ShoppingSessionState
        from ucp_shopping.orchestrator.graph import _make_complete_node
        from ucp_shopping.streaming import ShoppingEventStream

        node = _make_complete_node(ShoppingEventStream())
        failure = CheckoutFailure(
            merchant_id="a", merchant_name="Shop A", step="prepare", error="down"
        )
        out = await node(
            {
                "session_id": "s1",
                "completed_orders": [],
                "checkout_failures": [failure],
                "error": "0 order(s) placed; not completed: Shop A (prepare).",
            }
        )
        assert out["current_state"] == ShoppingSessionState.FAILED

    async def test_partial_success_is_completed_but_keeps_the_failures(self):
        from ucp_shopping.models import ShoppingSessionState
        from ucp_shopping.orchestrator.graph import _make_complete_node
        from ucp_shopping.streaming import ShoppingEventStream

        result = await _agent(ScriptedClient({"http://b.test:create": "x"})).execute_checkouts(
            _plan(), MERCHANTS
        )
        node = _make_complete_node(ShoppingEventStream())
        out = await node(
            {
                "session_id": "s1",
                "completed_orders": result.orders,
                "checkout_failures": result.failures,
            }
        )
        assert out["current_state"] == ShoppingSessionState.COMPLETED
        assert len(out["checkout_failures"]) == 1
