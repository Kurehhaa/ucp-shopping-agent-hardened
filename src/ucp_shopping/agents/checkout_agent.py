"""Multi-merchant checkout specialist agent.

Checkout runs in two phases so that a failure at one merchant is handled
deliberately instead of silently:

1. **Prepare** - at every merchant, in parallel: create the checkout session
   and attach the delivery details. Nothing is bought yet.
2. **Complete** - place the orders. With ``require_all_merchants`` the agent
   only gets here if *every* merchant was prepared; otherwise all prepared
   sessions are cancelled and nothing is bought. Without it, merchants that
   were prepared are completed and the failures are reported.

Every write to a merchant carries an idempotency key derived from the
shopping session, so a retry (network timeout, repeated confirmation) cannot
create a second order.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from ucp_shopping.config import Settings
from ucp_shopping.models import (
    CheckoutFailure,
    CheckoutResult,
    MerchantInfo,
    OrderSummary,
    ShippingAddress,
    SplitOrderItem,
    SplitOrderPlan,
)
from ucp_shopping.protocols.ucp_client import UCPClient
from ucp_shopping.streaming import EVENT_CHECKOUT_PROGRESS, ShoppingEventStream

logger = structlog.get_logger(__name__)

# Used only when the caller supplies no address (demo / smoke tests).
DEMO_ADDRESS = ShippingAddress(
    full_name="Demo User",
    line1="123 AI Street",
    city="San Francisco",
    state="CA",
    postal_code="94105",
    country="US",
)


@dataclass
class _Prepared:
    """A merchant checkout session that is ready to be completed."""

    merchant: MerchantInfo
    items: list[SplitOrderItem]
    checkout_session_id: str


class _StepError(Exception):
    """A checkout step failed at one merchant."""

    def __init__(self, step: str, message: str) -> None:
        super().__init__(message)
        self.step = step


class CheckoutAgent:
    """Orchestrates checkouts across multiple merchants."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._ucp_client = UCPClient(
            timeout=settings.checkout_timeout, guard=settings.merchant_url_guard()
        )

    async def execute_checkouts(
        self,
        plan: SplitOrderPlan,
        merchants: dict[str, MerchantInfo],
        stream: ShoppingEventStream | None = None,
        session_id: str = "",
        shipping_address: ShippingAddress | None = None,
        require_all_merchants: bool = False,
    ) -> CheckoutResult:
        """Run the two-phase checkout and report what happened per merchant.

        Parameters
        ----------
        plan:
            The optimized split-order plan.
        merchants:
            Mapping of merchant_id -> MerchantInfo.
        stream:
            Optional SSE stream for progress events.
        session_id:
            Shopping session ID; also seeds the idempotency keys, so running
            the same session twice cannot double-order.
        shipping_address:
            Delivery address; a demo address is used (and logged) if missing.
        require_all_merchants:
            If true, buy nothing unless every merchant could be prepared.
        """
        if shipping_address is None:
            logger.warning("checkout_using_demo_address")
            shipping_address = DEMO_ADDRESS
        run_id = session_id or str(uuid.uuid4())

        merchant_items: dict[str, list[SplitOrderItem]] = defaultdict(list)
        for item in plan.items:
            merchant_items[item.merchant_id].append(item)

        failures: list[CheckoutFailure] = []
        targets: list[tuple[MerchantInfo, list[SplitOrderItem]]] = []
        for merchant_id, items in merchant_items.items():
            merchant = merchants.get(merchant_id)
            if merchant is None:
                logger.warning("checkout_merchant_not_found", merchant_id=merchant_id)
                failures.append(
                    CheckoutFailure(
                        merchant_id=merchant_id,
                        merchant_name=items[0].merchant_name,
                        step="prepare",
                        error="Merchant is no longer known to the agent.",
                    )
                )
                continue
            targets.append((merchant, items))

        # Phase 1: prepare everywhere
        prepared_results = await asyncio.gather(
            *(
                self._prepare(m, items, shipping_address, run_id, stream, session_id)
                for m, items in targets
            ),
            return_exceptions=True,
        )
        prepared: list[_Prepared] = []
        for (merchant, _items), result in zip(targets, prepared_results, strict=True):
            if isinstance(result, _Prepared):
                prepared.append(result)
            else:
                failures.append(await self._record_failure(merchant, result, stream, session_id))

        # All-or-nothing: if anything failed so far, undo the prepared sessions.
        if require_all_merchants and failures:
            for entry in prepared:
                failures.append(await self._cancel(entry, stream, session_id))
            logger.info("checkout_aborted_all_or_nothing", failures=len(failures))
            return CheckoutResult(orders=[], failures=failures)

        # Phase 2: complete the prepared sessions
        completed = await asyncio.gather(
            *(self._complete(entry, run_id, stream, session_id) for entry in prepared),
            return_exceptions=True,
        )
        orders: list[OrderSummary] = []
        for entry, outcome in zip(prepared, completed, strict=True):
            if isinstance(outcome, OrderSummary):
                orders.append(outcome)
            else:
                failures.append(
                    await self._record_failure(entry.merchant, outcome, stream, session_id)
                )
                # Not cancelled on purpose: a timed-out completion may have
                # succeeded, and repeating the checkout is idempotent.

        result_summary = CheckoutResult(orders=orders, failures=failures)
        logger.info(
            "checkouts_complete",
            total_merchants=len(merchant_items),
            successful_orders=len(orders),
            failures=len(failures),
        )
        return result_summary

    # ------------------------------------------------------------------
    # Phases
    # ------------------------------------------------------------------

    async def _prepare(
        self,
        merchant: MerchantInfo,
        items: list[SplitOrderItem],
        address: ShippingAddress,
        run_id: str,
        stream: ShoppingEventStream | None,
        session_id: str,
    ) -> _Prepared:
        """Create the checkout session and attach delivery details."""
        await self._progress(stream, session_id, merchant, "creating_session")
        checkout_id = ""
        try:
            line_items = [
                {"product_id": item.product_id, "quantity": item.quantity} for item in items
            ]
            checkout = await self._ucp_client.create_checkout(
                merchant.url,
                line_items,
                idempotency_key=f"{run_id}:{merchant.id}:create",
            )
            checkout_id = checkout.get("id", "")
            if not checkout_id:
                raise ValueError("merchant returned a checkout session without an id")

            await self._progress(stream, session_id, merchant, "updating_shipping")
            await self._ucp_client.update_checkout(
                merchant.url,
                checkout_id,
                {
                    "shipping_address": address.model_dump(exclude_none=True),
                    "selected_shipping_id": "standard",
                },
            )
        except Exception as exc:
            if checkout_id:  # do not leave a half-prepared session open
                with contextlib.suppress(Exception):
                    await self._ucp_client.cancel_checkout(merchant.url, checkout_id)
            raise _StepError("prepare", str(exc)) from exc
        return _Prepared(merchant=merchant, items=items, checkout_session_id=checkout_id)

    async def _complete(
        self,
        entry: _Prepared,
        run_id: str,
        stream: ShoppingEventStream | None,
        session_id: str,
    ) -> OrderSummary:
        """Place the order for a prepared session."""
        merchant = entry.merchant
        await self._progress(stream, session_id, merchant, "completing")
        try:
            completion = await self._ucp_client.complete_checkout(
                merchant.url,
                entry.checkout_session_id,
                idempotency_key=f"{run_id}:{merchant.id}:complete",
            )
        except Exception as exc:
            raise _StepError("complete", str(exc)) from exc

        order_id = completion.get("order_id") or completion.get("id") or entry.checkout_session_id
        order = OrderSummary(
            merchant_name=merchant.name,
            merchant_id=merchant.id,
            order_id=order_id,
            items=entry.items,
            total=round(sum(i.total for i in entry.items), 2),  # unit x quantity + shipping
            status="confirmed",
            tracking_url=completion.get("tracking_url"),
            created_at=datetime.now(tz=UTC),
        )
        await self._progress(
            stream,
            session_id,
            merchant,
            "completed",
            extra={"order_id": order_id},
            message=f"Order {order_id} confirmed at {merchant.name}.",
        )
        logger.info("merchant_checkout_complete", merchant=merchant.name, order_id=order_id)
        return order

    async def _cancel(
        self,
        entry: _Prepared,
        stream: ShoppingEventStream | None,
        session_id: str,
    ) -> CheckoutFailure:
        """Release a prepared session at the merchant (best effort)."""
        merchant = entry.merchant
        try:
            await self._ucp_client.cancel_checkout(merchant.url, entry.checkout_session_id)
            error = "Cancelled because another merchant could not be prepared."
        except Exception as exc:
            logger.error("checkout_cancel_failed", merchant=merchant.name, error=str(exc))
            error = f"Cancellation failed, the session may stay open: {exc}"
        await self._progress(stream, session_id, merchant, "cancelled", message=error)
        return CheckoutFailure(
            merchant_id=merchant.id, merchant_name=merchant.name, step="cancelled", error=error
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _record_failure(
        self,
        merchant: MerchantInfo,
        error: BaseException,
        stream: ShoppingEventStream | None,
        session_id: str,
    ) -> CheckoutFailure:
        step = error.step if isinstance(error, _StepError) else "prepare"
        message = str(error)
        logger.error("merchant_checkout_failed", merchant=merchant.name, step=step, error=message)
        await self._progress(
            stream,
            session_id,
            merchant,
            "failed",
            extra={"error": message},
            message=f"Checkout failed at {merchant.name}: {message}",
        )
        return CheckoutFailure(
            merchant_id=merchant.id, merchant_name=merchant.name, step=step, error=message
        )

    @staticmethod
    async def _progress(
        stream: ShoppingEventStream | None,
        session_id: str,
        merchant: MerchantInfo,
        step: str,
        extra: dict[str, str] | None = None,
        message: str | None = None,
    ) -> None:
        if stream is None:
            return
        await stream.emit(
            session_id,
            EVENT_CHECKOUT_PROGRESS,
            data={"merchant": merchant.name, "step": step, **(extra or {})},
            message=message or f"{step.replace('_', ' ').capitalize()} at {merchant.name}...",
        )

    async def close(self) -> None:
        """Shut down the underlying HTTP client."""
        await self._ucp_client.close()
