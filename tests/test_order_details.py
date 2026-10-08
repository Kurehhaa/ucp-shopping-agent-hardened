"""Tests for quantities and delivery details flowing through to the merchants."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from ucp_shopping.agents.checkout_agent import DEMO_ADDRESS, CheckoutAgent
from ucp_shopping.agents.comparison_agent import ComparisonAgent
from ucp_shopping.agents.optimizer import SplitOrderOptimizer
from ucp_shopping.config import Settings
from ucp_shopping.models import (
    MAX_QUANTITY,
    MerchantInfo,
    ProductResult,
    ShippingAddress,
    ShippingOption,
    ShoppingPlanItem,
    ShoppingPreferences,
    SplitOrderItem,
    SplitOrderPlan,
)
from ucp_shopping.orchestrator.planner import ShoppingPlanner, _safe_quantity

ADDRESS = ShippingAddress(
    full_name="Aigerim S.",
    line1="12 Abay Ave",
    city="Almaty",
    postal_code="050000",
    country="KZ",
)


def _product(pid: str = "kb-1", price: float = 10.0, shipping: float = 5.0) -> ProductResult:
    return ProductResult(
        product_id=pid,
        name="Keyboard",
        price=price,
        merchant_id="m1",
        merchant_name="Shop One",
        shipping_options=[ShippingOption(id="standard", price=shipping)],
    )


class TestQuantityParsing:
    def test_llm_quantity_is_used(self):
        plan = ShoppingPlanner._parse_plan_json(
            '{"items": [{"name": "keyboard", "quantity": 3}]}', "3 keyboards"
        )
        assert plan.items[0].quantity == 3

    @pytest.mark.parametrize("raw", [None, "many", 0, -4, float("nan"), float("inf")])
    def test_bad_llm_quantity_falls_back_to_a_valid_number(self, raw):
        assert 1 <= _safe_quantity(raw) <= MAX_QUANTITY

    def test_missing_llm_quantity_defaults_to_one(self):
        plan = ShoppingPlanner._parse_plan_json('{"items": [{"name": "x"}]}', "x")
        assert plan.items[0].quantity == 1

    def test_huge_quantity_is_capped(self):
        plan = ShoppingPlanner._parse_plan_json(
            '{"items": [{"name": "x", "quantity": 100000}]}', "x"
        )
        assert plan.items[0].quantity == MAX_QUANTITY

    def test_fallback_planner_reads_explicit_quantity(self):
        plan = ShoppingPlanner._plan_with_keywords("2x mechanical keyboard and 3 pcs usb hub")
        assert [(i.name, i.quantity) for i in plan.items] == [
            ("mechanical keyboard", 2),
            ("usb hub", 3),
        ]

    def test_fallback_planner_does_not_mistake_a_size_for_a_quantity(self):
        plan = ShoppingPlanner._plan_with_keywords("27 inch monitor")
        assert plan.items[0].quantity == 1
        assert plan.items[0].name == "27 inch monitor"

    def test_model_rejects_invalid_quantity(self):
        with pytest.raises(ValidationError):
            ShoppingPlanItem(name="x", quantity=0)
        with pytest.raises(ValidationError):
            ShoppingPlanItem(name="x", quantity=MAX_QUANTITY + 1)


class TestQuantityPricing:
    def test_item_total_is_unit_price_times_quantity_plus_shipping(self):
        item = SplitOrderItem(
            product_name="k",
            product_id="p",
            merchant_name="m",
            merchant_id="m1",
            price=10.0,
            quantity=3,
            shipping_cost=5.0,
        )
        assert item.subtotal == 30.0
        assert item.total == 35.0

    async def test_optimizer_totals_include_quantity_in_both_strategies(self):
        matrix = await ComparisonAgent().build_comparison(
            {"m1": [_product()]}, ["Keyboard"], {"Keyboard": 4}
        )
        assert matrix.entries[0].quantity == 4
        optimizer = SplitOrderOptimizer()
        split = optimizer._optimize_split(matrix, optimizer_prefs())
        single = optimizer._optimize_single_merchant(matrix, optimizer_prefs())
        for plan in (split, single):
            assert plan.items[0].quantity == 4
            assert plan.total_product_cost == 40.0
            assert plan.grand_total == 45.0

    async def test_free_shipping_threshold_uses_the_quantity(self):
        # 1 x 30 stays under the 100 threshold; 4 x 30 reaches it.
        for qty, expected_shipping in ((1, 5.0), (4, 0.0)):
            matrix = await ComparisonAgent().build_comparison(
                {"m1": [_product(price=30.0)]}, ["Keyboard"], {"Keyboard": qty}
            )
            plan = SplitOrderOptimizer()._optimize_split(matrix, optimizer_prefs())
            assert plan.total_shipping_cost == expected_shipping
            assert plan.grand_total == round(30.0 * qty + expected_shipping, 2)


def optimizer_prefs() -> ShoppingPreferences:
    return ShoppingPreferences()


class FakeUCPClient:
    """Records what the agent sends to the merchant."""

    def __init__(self) -> None:
        self.created: list[list[dict[str, Any]]] = []
        self.updates: list[dict[str, Any]] = []

    async def create_checkout(self, url: str, line_items: list[dict[str, Any]]) -> dict[str, Any]:
        self.created.append(line_items)
        return {"id": "co_1"}

    async def update_checkout(self, url: str, sid: str, data: dict[str, Any]) -> dict[str, Any]:
        self.updates.append(data)
        return {}

    async def complete_checkout(self, url: str, sid: str) -> dict[str, Any]:
        return {"order_id": "ord_1"}

    async def close(self) -> None:
        return None


def _plan(quantity: int) -> SplitOrderPlan:
    item = SplitOrderItem(
        product_name="Keyboard",
        product_id="kb-1",
        merchant_name="Shop One",
        merchant_id="m1",
        price=10.0,
        quantity=quantity,
        shipping_cost=5.0,
    )
    return SplitOrderPlan(items=[item], grand_total=item.total, merchants_used=1)


MERCHANTS = {"m1": MerchantInfo(id="m1", name="Shop One", url="http://shop.test")}


class TestCheckoutDetails:
    async def _run(self, quantity: int, address: ShippingAddress | None):
        agent = CheckoutAgent(Settings())
        fake = FakeUCPClient()
        agent._ucp_client = fake  # type: ignore[assignment]
        orders = await agent.execute_checkouts(_plan(quantity), MERCHANTS, shipping_address=address)
        return fake, orders

    async def test_real_quantity_is_sent_to_the_merchant(self):
        fake, orders = await self._run(3, ADDRESS)
        assert fake.created == [[{"product_id": "kb-1", "quantity": 3}]]
        assert orders[0].total == 35.0

    async def test_address_from_the_request_is_used(self):
        fake, _ = await self._run(1, ADDRESS)
        sent = fake.updates[0]["shipping_address"]
        assert sent["city"] == "Almaty"
        assert sent["country"] == "KZ"
        assert "line2" not in sent

    async def test_demo_address_only_when_none_is_given(self):
        fake, _ = await self._run(1, None)
        assert fake.updates[0]["shipping_address"]["line1"] == DEMO_ADDRESS.line1


class TestShippingAddressValidation:
    def test_country_must_be_two_letters(self):
        with pytest.raises(ValidationError):
            ShippingAddress(
                full_name="A", line1="B", city="C", postal_code="1", country="Kazakhstan"
            )

    def test_required_fields_cannot_be_empty(self):
        with pytest.raises(ValidationError):
            ShippingAddress(full_name="", line1="B", city="C", postal_code="1", country="KZ")
