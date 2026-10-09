# UCP Shopping Agent

> Based on [Skopaq-AI/ucp-shopping-agent](https://github.com/Skopaq-AI/ucp-shopping-agent) (MIT). This repository contains my modifications, listed in [What changed from upstream](#what-changed-from-upstream); the original copyright notice is kept in [LICENSE](LICENSE).
>
> **Status: educational demo.** Merchants are in-process mocks, sessions and orders are kept in memory, no real payments are made, and it has not been validated against an official UCP conformance suite. See [Limitations](#limitations).

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED.svg?logo=docker)](Dockerfile)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg?logo=fastapi)](https://fastapi.tiangolo.com)
[![LangGraph](https://img.shields.io/badge/LangGraph-agent-4B0082.svg)](https://github.com/langchain-ai/langgraph)
[![UCP](https://img.shields.io/badge/UCP-2026--01--11-FF6F00.svg)](https://ucp.dev)

**AI Shopping Agent** that discovers UCP merchants, compares prices across vendors, optimizes multi-merchant orders, and orchestrates autonomous purchases -- like what powers Google AI Mode in Search and Gemini.

> **Buyer-side UCP implementation.** While the [UCP Merchant Server](https://github.com/Skopaq-AI/ucp-merchant-server) shows how merchants serve the protocol (like Shopify), this project shows how AI agents **consume** it to shop across multiple stores.

---

## What This Project Demonstrates

| Concept | Implementation |
|---------|---------------|
| **UCP Discovery** | Fetch `/.well-known/ucp` from multiple merchants in parallel |
| **Multi-Merchant Search** | Fan-out product search across all discovered merchants |
| **Price Comparison** | Build comparison matrix with price, shipping, and ratings |
| **Split-Order Optimization** | Buy items from cheapest vendors to minimize total cost |
| **LangGraph Workflow** | State graph: plan -> discover -> search -> compare -> optimize -> present -> confirm (pause) -> checkout -> complete |
| **Human-in-the-Loop** | Confirmation step before any purchase is executed |
| **SSE Streaming** | Real-time shopping progress events |
| **MCP Tool Surface** | All operations exposed as MCP tools |
| **3 Mock Merchants** | Built-in TechZone, HomeGoods, MegaMart for instant demo |

---

## Quick Start

### Docker Compose (Recommended)

```bash
git clone https://github.com/Kurehhaa/ucp-shopping-agent-hardened.git
cd ucp-shopping-agent-hardened

cp .env.example .env
# Edit .env with your API keys (optional - works without LLM for basic flows)

docker compose up --build
```

The API will be available at **http://localhost:8020**. Docs at **http://localhost:8020/docs**.

### Local Development

```bash
git clone https://github.com/Kurehhaa/ucp-shopping-agent-hardened.git
cd ucp-shopping-agent-hardened

python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

python -m ucp_shopping.main
```

---

## Shopping Workflow

```
User: "Find me the best deal on a mechanical keyboard and a USB-C hub"

    [PLAN] Parse intent -> extract items + constraints
      |
    [DISCOVER] Fetch /.well-known/ucp from 3 merchants
      |
    [SEARCH] Fan-out parallel search across all merchants
      |
    [COMPARE] Build price/shipping comparison matrix
      |                          +---------------------------+
      |                          | Keyboard    | USB-C Hub   |
      |                          |-------------|-------------|
      |                          | TechZone    | $79 + $5.99 |
      |                          | HomeGoods   | $89 + FREE  |
      |                          | MegaMart    | $69 + $8.99 |
      |                          +---------------------------+
      |
    [OPTIMIZE] Split-order: keyboard from MegaMart ($69), hub from HomeGoods ($34)
      |
    [PRESENT] Show comparison + recommendation via SSE
      |
    [CONFIRM] Wait for human approval
      |
    [CHECKOUT] Phase 1: prepare sessions at MegaMart + HomeGoods (nothing bought yet)
      |        Phase 2: place the orders; one merchant failing is reported, not hidden
      |
    [COMPLETE] Aggregate orders -> unified tracking
```

---

## API Reference

### Shopping (Main Flow)

```bash
# Start a shopping session
curl -X POST http://localhost:8020/api/v1/shop \
  -H "Content-Type: application/json" \
  -d '{
        "query": "2x mechanical keyboard and a usb hub",
        "budget": 300,
        "shipping_address": {
          "full_name": "Aigerim S.", "line1": "12 Abay Ave", "city": "Almaty",
          "postal_code": "050000", "country": "KZ"
        },
        "preferences": {"require_all_merchants": false}
      }'

# Check shopping progress
curl http://localhost:8020/api/v1/shop/{session_id}

# Stream real-time progress (SSE)
curl -N http://localhost:8020/api/v1/shop/{session_id}/stream

# Confirm purchase (nothing is bought before this call)
curl -X POST http://localhost:8020/api/v1/shop/{session_id}/confirm

# Cancel shopping
curl -X POST http://localhost:8020/api/v1/shop/{session_id}/cancel
```

### Price Comparison

```bash
# Compare a product across all merchants
curl -X POST http://localhost:8020/api/v1/compare \
  -H "Content-Type: application/json" \
  -d '{"product_query": "gaming mouse"}'

# Re-run the optimizer for an existing session (uses its comparison, budget and preferences)
curl -X POST http://localhost:8020/api/v1/optimize \
  -H "Content-Type: application/json" \
  -d '{"session_id": "<session_id>"}'
```

### Merchant Discovery

```bash
# List known merchants
curl http://localhost:8020/api/v1/merchants

# Register a new merchant (admin only: needs ADMIN_API_KEY, see Configuration)
curl -X POST http://localhost:8020/api/v1/merchants/discover \
  -H "Content-Type: application/json" -H "X-API-Key: $ADMIN_API_KEY" \
  -d '{"urls": ["https://merchant.example.com"]}'

# Browse a merchant's catalog
curl http://localhost:8020/api/v1/merchants/techzone/catalog
```

### Order Tracking

```bash
# List all orders across merchants
curl http://localhost:8020/api/v1/orders

# Get specific order
curl http://localhost:8020/api/v1/orders/{order_id}
```

---

## Built-in Mock Merchants

Three UCP-compliant mock merchants are included for instant demo:

| Merchant | Products | Specialty | Port Path |
|----------|----------|-----------|-----------|
| **TechZone** | 20 electronics | Laptops, keyboards, mice | `/merchants/techzone` |
| **HomeGoods** | 20 home/office | Desk accessories, lighting | `/merchants/homegoods` |
| **MegaMart** | 20 general | Overlapping catalog, different prices | `/merchants/megamart` |

Each merchant serves `/.well-known/ucp`, product catalog, checkout sessions, and orders.

---

## SSE Events

When streaming a shopping session (`GET /api/v1/shop/{id}/stream`), you'll receive these events
(payloads below are illustrative; see `src/ucp_shopping/streaming.py` and the graph nodes for the exact fields):

```
event: planning
data: {"message": "Parsing shopping request...", "items": ["keyboard", "hub"]}

event: merchants_discovered
data: {"count": 3, "merchants": ["TechZone", "HomeGoods", "MegaMart"]}

event: searching
data: {"merchant": "TechZone", "query": "keyboard"}

event: products_found
data: {"merchant": "TechZone", "count": 5}

event: comparison_ready
data: {"matrix": [...], "best_price": {...}}

event: optimization_ready
data: {"plan": {...}, "savings": "$12.50"}

event: awaiting_confirmation
data: {"total": "$103.99", "merchants": 2}

event: checkout_progress
data: {"merchant": "MegaMart", "status": "completed"}

event: completed
data: {"orders": [...], "total": "$103.99"}
```

---

## Configuration

Copy `.env.example` to `.env`. Everything has a default, and the demo needs no API key
(without an LLM key the planner falls back to keyword extraction).

| Variable | Default | Purpose |
|----------|---------|---------|
| `ADMIN_API_KEY` | empty | Enables `POST /api/v1/merchants/discover` (sent as `X-API-Key`). Empty = endpoint disabled (403). |
| `CORS_ALLOW_ORIGINS` | empty | Comma-separated browser origins allowed to call the API. Empty = none. Credentials are never allowed. |
| `KNOWN_MERCHANT_URLS` | the 3 local mocks | Merchants trusted by the operator (may be on localhost/private networks). |
| `ALLOW_PRIVATE_MERCHANT_HOSTS` | `false` | Let *any* merchant URL point at private addresses. Only for trusted networks. |
| `EXPOSE_ERROR_DETAILS` | `false` | Show internal error text in 500 responses (debugging only). |
| `MAX_ACTIVE_SESSIONS` | `500` | Upper bound on sessions that are still in progress (503 above it). |
| `HUMAN_CONFIRMATION_REQUIRED` | `true` | Wait for `POST .../confirm` before buying. |

## What changed from upstream

Each point below is covered by tests in `tests/`.

- **The purchase flow works end to end.** In the original the graph ran straight through the confirmation step and ended as `failed`, so `/confirm` always answered 400 and no order could be placed. The graph now pauses at the gate and resumes at checkout (verified against a real HTTP server in `tests/test_end_to_end.py`).
- **Real quantities and delivery address.** Quantity was hard-coded to 1 and the address to a demo user. Both now come from the request (`2x keyboard`, `shipping_address`), are validated, and are used in prices and in the order sent to the merchant.
- **Failing merchants are handled explicitly.** Checkout is two-phase (prepare everywhere, then complete). With `require_all_merchants` nothing is bought unless every merchant could be prepared and prepared sessions are cancelled. Otherwise each failure is reported with merchant, step and reason. A session where nothing was ordered is `failed`, not `completed`.
- **No double orders.** All writes carry an `Idempotency-Key` derived from the shopping session; the mock merchant honours it. Confirming twice is safe.
- **Budget, stock and shipping logic.** The budget is enforced (an over-budget plan is never ordered); out-of-stock items and insufficient stock are not selectable; unavailable items are reported instead of dropped; shipping is charged once per merchant order and the free-shipping threshold comes from the merchant; product matching compares whole words instead of substrings.
- **Robust LLM output handling.** Malformed or hostile planner JSON (wrong types, NaN/negative budgets, huge lists, unknown preference keys) can no longer crash planning; it falls back to the keyword planner.
- **API security.** SSRF guard on every outbound merchant request (see below), admin key for registering merchants, CORS closed by default, no exception text in 500 responses, bounded request sizes and session count.
- **Engineering hygiene.** Ruff, formatting and mypy are clean (they were not: 53 / 13 files / 26), CI runs on Python 3.11 and 3.13, tests no longer read a developer's local `.env`. Tests went from 25 to 175 (coverage 63% -> 83%).

## Security notes

The agent fetches merchant URLs that come from outside and later sends delivery details to those
merchants, so outbound requests are restricted (`src/ucp_shopping/security.py`): http(s) only, no
credentials in the URL, redirects are not followed, and every resolved address must be public.
Merchants listed in `KNOWN_MERCHANT_URLS` are trusted so the local demo works; everything else must
be https on a public host. The address is checked just before each request; a determined DNS
rebinding attack between the check and the connection is not fully excluded (pinning the connection
to the checked IP would be the next step).

## Limitations

- Demo grade: in-memory sessions and orders (lost on restart, no TTL cleanup), mock merchants, no real payment or authentication of end users. Session ids are the only capability needed to confirm or cancel a session.
- No automatic refund/cancellation of an order that was already completed at one merchant when another merchant fails later. A completion that fails is reported but not cancelled, because a timed-out completion may have gone through; repeating the checkout is idempotent.
- Cancelling a session while checkout is running stops the task but does not roll back orders already placed.
- Money is handled as floats rounded to cents (fine for a demo; use `Decimal`/integer cents in production).
- The A2A bridge (`protocols/a2a_bridge.py`) is not wired into the API and is not covered by tests.
- The MCP `shop` tool creates a session but does not start the workflow.

## Testing

```bash
pytest tests/ -v
pytest tests/ -v --cov=src/ucp_shopping --cov-report=term-missing
ruff check . && ruff format --check . && mypy src
```

`tests/test_end_to_end.py` starts a real server on a free local port with the in-process mock
merchants; the other files are unit and API tests.

---

## Project Structure

```
ucp-shopping-agent/
├── src/ucp_shopping/
│   ├── main.py                    # Entry point + mount mock merchants
│   ├── api.py                     # FastAPI routes
│   ├── config.py                  # Settings
│   ├── models.py                  # All Pydantic models
│   ├── streaming.py               # SSE event stream
│   ├── orchestrator/
│   │   ├── graph.py               # LangGraph shopping workflow
│   │   ├── state.py               # Graph state schema
│   │   └── planner.py             # LLM-powered intent parser
│   ├── protocols/
│   │   ├── ucp_client.py          # UCP protocol client
│   │   ├── a2a_bridge.py          # A2A agent integration
│   │   └── mcp_surface.py         # MCP tool definitions
│   ├── agents/
│   │   ├── discovery_agent.py     # Merchant discovery
│   │   ├── search_agent.py        # Multi-merchant search
│   │   ├── comparison_agent.py    # Price comparison matrix
│   │   ├── optimizer.py           # Split-order optimization
│   │   └── checkout_agent.py      # Multi-merchant checkout
│   └── mock_merchants/
│       ├── merchant_factory.py    # Create mock UCP merchants
│       ├── merchant_app.py        # Reusable merchant mini-app
│       └── catalogs/              # Product data (JSON)
│           ├── techzone.json
│           ├── homegoods.json
│           └── megamart.json
├── tests/
├── k8s/
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

---

## Key Technologies

- **[UCP](https://ucp.dev)** - Universal Commerce Protocol (agent-side client)
- **[LangGraph](https://github.com/langchain-ai/langgraph)** - Multi-step shopping workflow
- **[A2A](https://github.com/google/A2A)** - Agent-to-Agent protocol bridge
- **[MCP](https://modelcontextprotocol.io)** - Model Context Protocol tool surface
- **[FastAPI](https://fastapi.tiangolo.com)** - API framework + SSE streaming

---

## License

MIT License - see [LICENSE](LICENSE) for details.
