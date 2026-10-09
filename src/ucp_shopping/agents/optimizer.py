"""Split-order optimizer.

Determines the cheapest way to purchase all items in the comparison matrix,
potentially splitting the order across multiple merchants to minimise total
cost (product price + shipping).
"""

from __future__ import annotations

from collections import defaultdict

import structlog

from ucp_shopping.models import (
    ComparisonMatrix,
    ProductResult,
    ShoppingPreferences,
    SplitOrderItem,
    SplitOrderPlan,
)

logger = structlog.get_logger(__name__)


# Used for merchants that do not publish a free-shipping threshold
DEFAULT_FREE_SHIPPING_THRESHOLD = 100.0


class SplitOrderOptimizer:
    """Computes optimal purchase plans across merchants."""

    async def optimize(
        self,
        matrix: ComparisonMatrix,
        preferences: ShoppingPreferences | None = None,
        budget: float | None = None,
        free_shipping_thresholds: dict[str, float] | None = None,
    ) -> SplitOrderPlan:
        """Build an optimized split-order plan.

        Algorithm
        ---------
        1. For each item in the comparison matrix, select the cheapest
           option considering price + shipping.
        2. Group selected items by merchant and check free-shipping
           thresholds: if the subtotal from one merchant exceeds the
           threshold, zero out the shipping cost.
        3. Compare against a single-merchant baseline (buying everything
           from the cheapest single merchant) to calculate savings.

        Parameters
        ----------
        matrix:
            The comparison matrix containing scored options per item.
        preferences:
            User preferences that may override optimisation decisions.
        budget:
            Maximum grand total; the plan is flagged ``over_budget`` if exceeded.
        free_shipping_thresholds:
            merchant_id -> subtotal from which that merchant ships for free
            (``DEFAULT_FREE_SHIPPING_THRESHOLD`` for merchants not listed).

        Returns
        -------
        SplitOrderPlan
        """
        prefs = preferences or ShoppingPreferences()
        thresholds = free_shipping_thresholds or {}

        if prefs.prefer_single_merchant:
            plan = self._optimize_single_merchant(matrix, prefs, thresholds)
        else:
            plan = self._optimize_split(matrix, prefs, thresholds)

        plan.budget = budget
        plan.over_budget = budget is not None and plan.grand_total > budget
        if plan.over_budget:
            plan.reasoning += f" Total ${plan.grand_total:.2f} exceeds the budget of ${budget:.2f}."
        return plan

    # ------------------------------------------------------------------
    # Split-order optimisation
    # ------------------------------------------------------------------

    def _optimize_split(
        self,
        matrix: ComparisonMatrix,
        prefs: ShoppingPreferences,
        thresholds: dict[str, float] | None = None,
    ) -> SplitOrderPlan:
        """Pick the cheapest option per item regardless of merchant."""
        items: list[SplitOrderItem] = []
        unavailable: list[str] = []

        for entry in matrix.entries:
            buyable = [p for p in entry.merchant_results if self._can_supply(p, entry.quantity)]
            if not buyable:
                unavailable.append(entry.product_query)
                continue

            # Filter by preferences
            candidates = self._apply_preference_filters(buyable, prefs)
            if not candidates:
                candidates = buyable

            # Find cheapest total (price + cheapest shipping)
            best = min(
                candidates,
                key=lambda p: p.price * entry.quantity + self._cheapest_shipping_cost(p),
            )
            shipping = self._cheapest_shipping_cost(best)

            items.append(
                SplitOrderItem(
                    product_name=best.name,
                    product_id=best.product_id,
                    merchant_name=best.merchant_name,
                    merchant_id=best.merchant_id,
                    merchant_url=self._get_merchant_url(best),
                    price=best.price,
                    quantity=entry.quantity,
                    shipping_cost=shipping,
                )
            )

        # One shipping charge per merchant order, then free-shipping thresholds
        items = self._consolidate_shipping(items)
        items = self._apply_free_shipping_thresholds(items, matrix, thresholds)

        # Calculate totals
        total_product = round(sum(i.subtotal for i in items), 2)
        total_shipping = round(sum(i.shipping_cost for i in items), 2)
        grand_total = round(total_product + total_shipping, 2)

        # Calculate savings vs single-merchant baseline
        single_plan = self._optimize_single_merchant(matrix, prefs, thresholds)
        savings = round(max(0.0, single_plan.grand_total - grand_total), 2)

        # Count distinct merchants
        merchant_ids = {i.merchant_id for i in items}

        plan = SplitOrderPlan(
            items=items,
            total_product_cost=total_product,
            total_shipping_cost=total_shipping,
            grand_total=grand_total,
            savings_vs_single=savings,
            merchants_used=len(merchant_ids),
            reasoning=self._build_reasoning(items, savings, unavailable),
            unavailable_items=unavailable,
        )

        logger.info(
            "split_order_optimized",
            items=len(items),
            merchants=len(merchant_ids),
            grand_total=grand_total,
            savings=savings,
        )
        return plan

    # ------------------------------------------------------------------
    # Single-merchant optimisation
    # ------------------------------------------------------------------

    def _optimize_single_merchant(
        self,
        matrix: ComparisonMatrix,
        prefs: ShoppingPreferences,
        thresholds: dict[str, float] | None = None,
    ) -> SplitOrderPlan:
        """Find the best single merchant to fulfil all items."""
        # Group available products by merchant
        merchant_items: dict[str, list[tuple[str, ProductResult]]] = defaultdict(list)
        quantities = {entry.product_query: entry.quantity for entry in matrix.entries}

        unavailable: list[str] = []
        for entry in matrix.entries:
            buyable = [p for p in entry.merchant_results if self._can_supply(p, entry.quantity)]
            if not buyable:
                unavailable.append(entry.product_query)
            for result in buyable:
                merchant_items[result.merchant_id].append((entry.product_query, result))

        # Evaluate each merchant that can fulfil all items
        num_items = len(matrix.entries)
        best_plan: SplitOrderPlan | None = None

        for available in merchant_items.values():
            # Pick cheapest option per item from this merchant
            item_map: dict[str, ProductResult] = {}
            for item_query, product in available:
                if item_query not in item_map or product.price < item_map[item_query].price:
                    item_map[item_query] = product

            # Only consider merchants that can cover all items
            if len(item_map) < num_items:
                continue

            items: list[SplitOrderItem] = []
            for item_query, product in item_map.items():
                shipping = self._cheapest_shipping_cost(product)
                items.append(
                    SplitOrderItem(
                        product_name=product.name,
                        product_id=product.product_id,
                        merchant_name=product.merchant_name,
                        merchant_id=product.merchant_id,
                        merchant_url=self._get_merchant_url(product),
                        price=product.price,
                        quantity=quantities[item_query],
                        shipping_cost=shipping,
                    )
                )

            items = self._consolidate_shipping(items)
            items = self._apply_free_shipping_thresholds(items, matrix, thresholds)
            total_product = round(sum(i.subtotal for i in items), 2)
            total_shipping = round(sum(i.shipping_cost for i in items), 2)
            grand_total = round(total_product + total_shipping, 2)

            plan = SplitOrderPlan(
                items=items,
                total_product_cost=total_product,
                total_shipping_cost=total_shipping,
                grand_total=grand_total,
                savings_vs_single=0.0,
                merchants_used=1,
                reasoning=f"All items from {items[0].merchant_name if items else 'unknown'}.",
            )

            if best_plan is None or grand_total < best_plan.grand_total:
                best_plan = plan

        if best_plan is not None:
            return best_plan

        # If no single merchant can fulfil all items, fall back to split
        # but mark it as a single-merchant attempt
        return SplitOrderPlan(
            items=[],
            reasoning="No single merchant can fulfil all items.",
            unavailable_items=unavailable,
        )

    # ------------------------------------------------------------------
    # Free shipping threshold logic
    # ------------------------------------------------------------------

    def _apply_free_shipping_thresholds(
        self,
        items: list[SplitOrderItem],
        matrix: ComparisonMatrix,
        thresholds: dict[str, float] | None = None,
    ) -> list[SplitOrderItem]:
        """Zero out shipping for merchants whose subtotal reaches their threshold.

        The threshold comes from the merchant's manifest; merchants that do
        not publish one use ``DEFAULT_FREE_SHIPPING_THRESHOLD``.
        """
        # Group items by merchant and compute subtotals
        merchant_subtotals: dict[str, float] = defaultdict(float)
        for item in items:
            merchant_subtotals[item.merchant_id] += item.subtotal

        known = thresholds or {}

        updated: list[SplitOrderItem] = []
        for item in items:
            threshold = known.get(item.merchant_id, DEFAULT_FREE_SHIPPING_THRESHOLD)
            if merchant_subtotals[item.merchant_id] >= threshold:
                updated.append(
                    item.model_copy(
                        update={
                            "shipping_cost": 0.0,
                            "total": item.subtotal,
                        }
                    )
                )
            else:
                updated.append(item)

        return updated

    # ------------------------------------------------------------------
    # Preference filters
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_preference_filters(
        products: list[ProductResult],
        prefs: ShoppingPreferences,
    ) -> list[ProductResult]:
        """Filter products based on user preferences."""
        filtered = list(products)

        # Filter by max shipping days
        if prefs.max_shipping_days is not None:
            filtered = [
                p
                for p in filtered
                if any(
                    so.estimated_days_max <= prefs.max_shipping_days for so in p.shipping_options
                )
                or not p.shipping_options
            ]

        # Filter by free shipping preference
        if prefs.prefer_free_shipping:
            free_options = [
                p for p in filtered if any(so.is_free or so.price == 0 for so in p.shipping_options)
            ]
            if free_options:
                filtered = free_options

        # Filter by preferred brands
        if prefs.preferred_brands:
            brand_lower = {b.lower() for b in prefs.preferred_brands}
            brand_matches = [p for p in filtered if p.brand.lower() in brand_lower]
            if brand_matches:
                filtered = brand_matches

        # Filter by minimum rating
        if prefs.min_rating is not None:
            rated = [p for p in filtered if p.rating >= prefs.min_rating]
            if rated:
                filtered = rated

        return filtered

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _can_supply(product: ProductResult, quantity: int) -> bool:
        """True if the product is in stock in the wanted quantity.

        ``stock_quantity == 0`` means the merchant did not report a count, so
        only the in-stock flag is checked in that case.
        """
        if not product.in_stock:
            return False
        return product.stock_quantity == 0 or product.stock_quantity >= quantity

    @staticmethod
    def _consolidate_shipping(items: list[SplitOrderItem]) -> list[SplitOrderItem]:
        """Charge shipping once per merchant (an order ships as one parcel).

        The highest per-item shipping cost of the merchant is kept on its first
        item and the others are set to zero.
        """
        highest: dict[str, float] = {}
        for item in items:
            highest[item.merchant_id] = max(highest.get(item.merchant_id, 0.0), item.shipping_cost)

        charged: set[str] = set()
        result: list[SplitOrderItem] = []
        for item in items:
            shipping = 0.0 if item.merchant_id in charged else highest[item.merchant_id]
            charged.add(item.merchant_id)
            result.append(
                item.model_copy(
                    update={"shipping_cost": shipping, "total": round(item.subtotal + shipping, 2)}
                )
            )
        return result

    @staticmethod
    def _cheapest_shipping_cost(product: ProductResult) -> float:
        """Return the cheapest shipping cost for a product."""
        if not product.shipping_options:
            return 5.99
        return min(so.price for so in product.shipping_options)

    @staticmethod
    def _get_merchant_url(product: ProductResult) -> str:
        """Derive the merchant URL from product metadata."""
        return product.url.rsplit("/api", 1)[0] if "/api" in product.url else ""

    @staticmethod
    def _build_reasoning(
        items: list[SplitOrderItem], savings: float, unavailable: list[str] | None = None
    ) -> str:
        """Build a human-readable explanation of the plan."""
        if not items:
            return "No items to purchase."

        merchant_names = sorted({i.merchant_name for i in items})
        if len(merchant_names) == 1:
            reasoning = f"All {len(items)} item(s) purchased from {merchant_names[0]}."
        else:
            reasoning = (
                f"Order split across {len(merchant_names)} merchants "
                f"({', '.join(merchant_names)}) for optimal pricing."
            )

        if savings > 0:
            reasoning += f" Saves ${savings:.2f} compared to single-merchant purchase."

        if unavailable:
            reasoning += f" Not available in the wanted quantity: {', '.join(unavailable)}."

        return reasoning
