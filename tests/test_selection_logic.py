"""Tests for product matching, the optimizer (budget, stock, shipping) and plan parsing."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from ucp_shopping.agents.comparison_agent import ComparisonAgent
from ucp_shopping.agents.optimizer import DEFAULT_FREE_SHIPPING_THRESHOLD, SplitOrderOptimizer
from ucp_shopping.models import (
    CheckoutResult,
    ComparisonEntry,
    ComparisonMatrix,
    ProductResult,
    ShippingOption,
    ShoppingPlan,
    ShoppingPreferences,
    ShoppingRequest,
    ShoppingSessionState,
    SplitOrderItem,
    SplitOrderPlan,
)
from ucp_shopping.orchestrator.graph import _effective_budget, _make_checkout_node
from ucp_shopping.orchestrator.planner import MAX_PLAN_ITEMS, ShoppingPlanner
from ucp_shopping.streaming import ShoppingEventStream


def product(
    pid: str,
    merchant: str = "m1",
    name: str | None = None,
    price: float = 20.0,
    shipping: float = 5.0,
    in_stock: bool = True,
    stock: int = 0,
    description: str = "",
) -> ProductResult:
    return ProductResult(
        product_id=pid,
        name=name or pid,
        description=description,
        price=price,
        merchant_id=merchant,
        merchant_name=merchant.upper(),
        shipping_options=[ShippingOption(id="standard", price=shipping)],
        in_stock=in_stock,
        stock_quantity=stock,
    )


def matrix(*entries: tuple[str, int, list[ProductResult]]) -> ComparisonMatrix:
    return ComparisonMatrix(
        entries=[
            ComparisonEntry(product_query=q, quantity=n, merchant_results=r) for q, n, r in entries
        ]
    )


class TestProductMatching:
    def _match(self, item: str, *names: str) -> list[str]:
        products = [product(f"p{i}", name=n) for i, n in enumerate(names)]
        return [p.name for p in ComparisonAgent._find_matching_products(products, item)]

    def test_all_keywords_must_be_present(self):
        found = self._match("mechanical keyboard", "Mechanical Keyboard Pro", "Mechanical Pencil")
        assert found == ["Mechanical Keyboard Pro"]

    def test_plurals_match(self):
        assert self._match("keyboards", "Wireless Keyboard") == ["Wireless Keyboard"]

    def test_whole_words_only(self):
        assert self._match("hub", "GitHub Sticker Pack", "USB Hub 7-in-1") == ["USB Hub 7-in-1"]

    def test_longer_queries_tolerate_one_missing_word(self):
        found = self._match("usb c hub adapter", "USB-C Hub Adapter")
        assert found == ["USB-C Hub Adapter"]

    def test_unrelated_products_do_not_match(self):
        assert self._match("monitor", "Desk Lamp", "Coffee Mug") == []

    def test_short_query_still_works(self):
        assert self._match("tv", "Smart TV 55 inch") == ["Smart TV 55 inch"]


class TestStock:
    def test_out_of_stock_product_is_never_chosen_even_if_cheapest(self):
        m = matrix(
            (
                "keyboard",
                1,
                [
                    product("cheap", "m1", price=5, in_stock=False),
                    product("dear", "m2", price=50),
                ],
            )
        )
        plan = SplitOrderOptimizer()._optimize_split(m, ShoppingPreferences())
        assert [i.product_id for i in plan.items] == ["dear"]

    def test_stock_below_wanted_quantity_is_skipped(self):
        m = matrix(
            (
                "keyboard",
                5,
                [product("few", "m1", price=5, stock=3), product("many", "m2", price=9, stock=10)],
            )
        )
        plan = SplitOrderOptimizer()._optimize_split(m, ShoppingPreferences())
        assert plan.items[0].product_id == "many"

    def test_unknown_stock_count_only_needs_the_in_stock_flag(self):
        m = matrix(("keyboard", 5, [product("a", stock=0)]))
        plan = SplitOrderOptimizer()._optimize_split(m, ShoppingPreferences())
        assert len(plan.items) == 1

    @pytest.mark.parametrize("single", [False, True])
    async def test_unavailable_item_is_reported_not_silently_dropped(self, single):
        m = matrix(
            ("keyboard", 1, [product("kb")]),
            ("usb hub", 1, [product("hub", in_stock=False)]),
        )
        prefs = ShoppingPreferences(prefer_single_merchant=single)
        plan = await SplitOrderOptimizer().optimize(m, prefs)
        assert plan.unavailable_items == ["usb hub"]
        if not single:
            assert [i.product_id for i in plan.items] == ["kb"]
            assert "usb hub" in plan.reasoning


class TestShipping:
    def test_shipping_is_charged_once_per_merchant(self):
        m = matrix(
            ("keyboard", 1, [product("kb", "m1", price=30, shipping=5)]),
            ("mouse", 1, [product("ms", "m1", price=20, shipping=7)]),
        )
        plan = SplitOrderOptimizer()._optimize_split(m, ShoppingPreferences())
        assert plan.total_shipping_cost == 7.0  # the highest of the two, once
        assert plan.grand_total == 57.0
        assert plan.grand_total == round(sum(i.total for i in plan.items), 2)

    def test_two_merchants_pay_two_shipping_charges(self):
        m = matrix(
            ("keyboard", 1, [product("kb", "m1", price=30, shipping=5)]),
            ("mouse", 1, [product("ms", "m2", price=20, shipping=7)]),
        )
        plan = SplitOrderOptimizer()._optimize_split(m, ShoppingPreferences())
        assert plan.total_shipping_cost == 12.0

    def test_free_shipping_uses_the_merchants_own_threshold(self):
        m = matrix(("keyboard", 1, [product("kb", "m1", price=120, shipping=9)]))
        optimizer = SplitOrderOptimizer()
        default = optimizer._optimize_split(m, ShoppingPreferences())
        stricter = optimizer._optimize_split(m, ShoppingPreferences(), {"m1": 150.0})
        assert DEFAULT_FREE_SHIPPING_THRESHOLD <= 120
        assert default.total_shipping_cost == 0.0
        assert stricter.total_shipping_cost == 9.0

    def test_single_merchant_plan_also_consolidates_and_uses_thresholds(self):
        m = matrix(
            ("keyboard", 1, [product("kb", "m1", price=60, shipping=5)]),
            ("mouse", 1, [product("ms", "m1", price=60, shipping=5)]),
        )
        plan = SplitOrderOptimizer()._optimize_single_merchant(m, ShoppingPreferences())
        assert plan.total_shipping_cost == 0.0  # 120 >= default threshold
        plan = SplitOrderOptimizer()._optimize_single_merchant(
            m, ShoppingPreferences(), {"m1": 500.0}
        )
        assert plan.total_shipping_cost == 5.0


class TestBudget:
    async def _plan(self, budget):
        m = matrix(("keyboard", 2, [product("kb", price=30, shipping=5)]))
        return await SplitOrderOptimizer().optimize(m, budget=budget)

    async def test_plan_over_budget_is_flagged(self):
        plan = await self._plan(50.0)  # total is 65
        assert plan.over_budget and plan.budget == 50.0
        assert "exceeds the budget" in plan.reasoning

    async def test_plan_at_or_under_budget_is_fine(self):
        assert not (await self._plan(65.0)).over_budget
        assert not (await self._plan(500.0)).over_budget

    async def test_no_budget_means_no_limit(self):
        plan = await self._plan(None)
        assert not plan.over_budget and plan.budget is None

    def test_request_budget_wins_over_the_one_the_planner_found(self):
        state = {
            "request": ShoppingRequest(query="x", budget=Decimal("80")),
            "shopping_plan": ShoppingPlan(overall_budget=Decimal("20")),
        }
        assert _effective_budget(state) == 80.0

    def test_planner_budget_is_used_when_the_request_has_none(self):
        state = {
            "request": ShoppingRequest(query="x"),
            "shopping_plan": ShoppingPlan(overall_budget=Decimal("20")),
        }
        assert _effective_budget(state) == 20.0
        assert _effective_budget({}) is None


class ExplodingCheckoutAgent:
    async def execute_checkouts(self, *args, **kwargs):
        raise AssertionError("checkout must not run")


class TestCheckoutGuards:
    def _plan(self, **kwargs) -> SplitOrderPlan:
        item = SplitOrderItem(
            product_name="kb",
            product_id="kb",
            merchant_name="M1",
            merchant_id="m1",
            price=30,
            shipping_cost=5,
        )
        return SplitOrderPlan(items=[item], grand_total=35, merchants_used=1, **kwargs)

    async def test_over_budget_plan_is_never_ordered(self):
        node = _make_checkout_node(ExplodingCheckoutAgent(), ShoppingEventStream())
        out = await node(
            {
                "session_id": "s",
                "optimization_plan": self._plan(budget=20.0, over_budget=True),
                "user_confirmed": True,
            }
        )
        assert out["current_state"] == ShoppingSessionState.FAILED
        assert "above your budget" in out["error"]

    async def test_empty_plan_is_never_ordered(self):
        node = _make_checkout_node(ExplodingCheckoutAgent(), ShoppingEventStream())
        out = await node({"session_id": "s", "optimization_plan": SplitOrderPlan()})
        assert out["current_state"] == ShoppingSessionState.FAILED

    async def test_within_budget_plan_proceeds(self):
        class Agent:
            async def execute_checkouts(self, *a, **k):
                return CheckoutResult()

        node = _make_checkout_node(Agent(), ShoppingEventStream())
        out = await node({"session_id": "s", "optimization_plan": self._plan(budget=100.0)})
        assert out["current_state"] == ShoppingSessionState.FAILED  # nothing was bought
        assert "above your budget" not in (out["error"] or "")


def parse(payload, query="wireless mouse") -> ShoppingPlan:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return ShoppingPlanner._parse_plan_json(raw, query)


class TestPlanParsing:
    def test_valid_plan(self):
        plan = parse(
            {
                "items": [{"name": "keyboard", "quantity": 2, "budget": 80, "keywords": ["rgb"]}],
                "overall_budget": 200,
                "preferences": {"prefer_free_shipping": True, "max_shipping_days": 4},
                "reasoning": "ok",
            }
        )
        assert plan.items[0].quantity == 2
        assert plan.items[0].budget == Decimal("80")
        assert plan.overall_budget == Decimal("200")
        assert plan.preferences.prefer_free_shipping
        assert plan.preferences.max_shipping_days == 4

    @pytest.mark.parametrize("raw", ["[]", '"text"', "42", "null", "not json at all"])
    def test_non_object_or_garbage_falls_back_to_keywords(self, raw):
        plan = parse(raw, "wireless mouse")
        assert plan.items[0].name == "wireless mouse"
        assert "Fallback" in plan.reasoning

    @pytest.mark.parametrize(
        "payload",
        [
            {"items": "keyboard"},
            {"items": None},
            {"items": [1, "x", None, []]},
            {"items": [{"name": ""}, {"name": "   "}, {"quantity": 2}, {"name": 5}]},
            {},
        ],
    )
    def test_no_usable_items_falls_back_to_keywords(self, payload):
        assert "Fallback" in parse(payload).reasoning

    def test_bad_items_are_skipped_good_ones_kept(self):
        plan = parse({"items": [None, {"name": "keyboard"}, {"name": ""}, {"name": "mouse"}]})
        assert [i.name for i in plan.items] == ["keyboard", "mouse"]

    @pytest.mark.parametrize("budget", ["abc", -5, 0, True, "NaN", "Infinity", [], {}])
    def test_unusable_budgets_are_dropped(self, budget):
        plan = parse({"items": [{"name": "x", "budget": budget}], "overall_budget": budget})
        assert plan.items[0].budget is None
        assert plan.overall_budget is None

    def test_string_budget_is_accepted(self):
        assert parse(
            {"items": [{"name": "x"}], "overall_budget": "99.5"}
        ).overall_budget == Decimal("99.5")

    def test_item_count_is_capped(self):
        plan = parse({"items": [{"name": f"item {i}"} for i in range(50)]})
        assert len(plan.items) == MAX_PLAN_ITEMS

    def test_bad_preferences_do_not_break_the_plan(self):
        plan = parse({"items": [{"name": "x"}], "preferences": {"max_shipping_days": "soon"}})
        assert plan.items and plan.preferences.max_shipping_days is None

    def test_unknown_preference_keys_cannot_be_injected(self):
        plan = parse({"items": [{"name": "x"}], "preferences": {"require_all_merchants": True}})
        assert plan.preferences.require_all_merchants is False

    def test_markdown_fences_are_stripped(self):
        assert parse('```json\n{"items": [{"name": "kb"}]}\n```').items[0].name == "kb"

    def test_text_fields_are_bounded_and_typed(self):
        plan = parse(
            {
                "items": [{"name": "x" * 1000, "keywords": ["a", {"b": 1}, 3] + ["k"] * 100}],
                "reasoning": "r" * 5000,
            }
        )
        assert len(plan.items[0].name) == 200
        assert all(isinstance(k, str) for k in plan.items[0].keywords)
        assert len(plan.items[0].keywords) <= 20
        assert len(plan.reasoning) == 500
