"""Security tests: SSRF guard, admin key, CORS, error handling and input limits."""

from __future__ import annotations

from typing import ClassVar

import pytest
from httpx import ASGITransport, AsyncClient

from ucp_shopping.config import Settings
from ucp_shopping.main import build_app
from ucp_shopping.protocols.ucp_client import UCPClient, UCPClientError
from ucp_shopping.security import MerchantURLError, MerchantURLGuard, origin_of


def _resolver(*addresses: str):
    async def resolve(host: str, port: int) -> list[str]:
        return list(addresses)

    return resolve


def _guard(resolver=None, **kwargs) -> MerchantURLGuard:
    return MerchantURLGuard(resolver=resolver or _resolver("93.184.216.34"), **kwargs)


class TestMerchantURLGuard:
    async def test_public_https_host_is_allowed(self):
        await _guard().check("https://shop.example.com/merchants/x")

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://shop.example.com/",
            "file:///etc/passwd",
            "gopher://shop.example.com/",
            "javascript:alert(1)",
            "https:///no-host",
            "https://user:pass@shop.example.com/",
            "https://shop.example.com:notaport/",
            "not a url",
            "",
        ],
    )
    async def test_malformed_or_odd_urls_are_rejected(self, url):
        with pytest.raises(MerchantURLError):
            await _guard().check(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://127.0.0.1/",
            "https://localhost/",
            "https://api.localhost/",
            "https://10.0.0.5/",
            "https://192.168.1.1/",
            "https://172.16.0.1/",
            "https://169.254.169.254/latest/meta-data/",
            "https://[::1]/",
            "https://[fe80::1]/",
            "https://[::ffff:127.0.0.1]/",
            "https://0.0.0.0/",
        ],
    )
    async def test_internal_ip_literals_are_rejected_without_dns(self, url):
        # The resolver would claim a public address; literals must not be trusted to it.
        with pytest.raises(MerchantURLError):
            await _guard(_resolver("93.184.216.34")).check(url)

    @pytest.mark.parametrize("address", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "::1"])
    async def test_hostname_resolving_to_internal_address_is_rejected(self, address):
        with pytest.raises(MerchantURLError):
            await _guard(_resolver(address)).check("https://innocent.example.com/")

    async def test_one_internal_address_among_public_ones_is_enough_to_reject(self):
        with pytest.raises(MerchantURLError):
            await _guard(_resolver("93.184.216.34", "10.0.0.1")).check("https://x.example.com/")

    async def test_plain_http_is_rejected_for_untrusted_hosts(self):
        with pytest.raises(MerchantURLError):
            await _guard().check("http://shop.example.com/")

    async def test_unresolvable_host_is_rejected(self):
        async def failing(host, port):
            raise OSError("no such host")

        with pytest.raises(MerchantURLError):
            await _guard(failing).check("https://nope.example.com/")

    async def test_operator_configured_origin_is_trusted_even_on_localhost(self):
        guard = _guard(trusted_urls=["http://localhost:8020/merchants/techzone"])
        await guard.check("http://localhost:8020/merchants/homegoods/api/v1/catalog")
        with pytest.raises(MerchantURLError):  # a different port is a different origin
            await guard.check("http://localhost:9999/")

    async def test_allow_private_hosts_switch(self):
        await _guard(allow_private_hosts=True).check("http://10.0.0.5/")

    def test_origin_fills_default_ports(self):
        assert origin_of("HTTPS://Shop.Example.com/x") == "https://shop.example.com:443"
        assert origin_of("http://a.test") == "http://a.test:80"


class TestClientEnforcesTheGuard:
    async def test_blocked_request_never_leaves_the_client(self):
        client = UCPClient(timeout=1, guard=_guard())
        with pytest.raises(UCPClientError, match="Request blocked"):
            await client.discover("https://169.254.169.254")

    async def test_redirects_are_not_followed(self):
        client = UCPClient(timeout=1)
        http = await client._get_client()
        assert http.follow_redirects is False
        await client.close()

    async def test_all_agents_carry_the_guard(self):
        from ucp_shopping.agents.checkout_agent import CheckoutAgent
        from ucp_shopping.agents.discovery_agent import DiscoveryAgent
        from ucp_shopping.agents.search_agent import SearchAgent

        settings = Settings()
        for agent in (CheckoutAgent(settings), DiscoveryAgent(settings), SearchAgent(settings)):
            assert agent._ucp_client._guard is not None


@pytest.fixture
def make_client():
    clients = []

    async def factory(**overrides):
        settings = Settings(environment="testing", human_confirmation_required=False, **overrides)
        app = build_app(settings)
        client = AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        )
        clients.append(client)
        return client, app

    yield factory


class TestAdminKey:
    URL = "/api/v1/merchants/discover"
    BODY: ClassVar[dict] = {"urls": ["https://shop.example.com"]}

    async def test_disabled_when_no_key_is_configured(self, make_client):
        client, _ = await make_client()
        resp = await client.post(self.URL, json=self.BODY)
        assert resp.status_code == 403
        assert "ADMIN_API_KEY" in resp.json()["detail"]

    async def test_even_a_sent_key_does_not_help_when_unconfigured(self, make_client):
        client, _ = await make_client()
        resp = await client.post(self.URL, json=self.BODY, headers={"X-API-Key": "anything"})
        assert resp.status_code == 403

    @pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}, {"X-API-Key": ""}])
    async def test_wrong_or_missing_key_is_401(self, make_client, headers):
        client, _ = await make_client(admin_api_key="s3cret")
        resp = await client.post(self.URL, json=self.BODY, headers=headers)
        assert resp.status_code == 401

    async def test_correct_key_is_accepted(self, make_client):
        client, _ = await make_client(admin_api_key="s3cret")
        resp = await client.post(self.URL, json=self.BODY, headers={"X-API-Key": "s3cret"})
        assert resp.status_code == 200
        # the guard refused the host (no real network involved) so nothing was discovered
        assert resp.json()["discovered"] == 0

    async def test_internal_urls_cannot_be_registered_even_with_the_key(self, make_client):
        client, _ = await make_client(admin_api_key="s3cret")
        resp = await client.post(
            self.URL,
            json={"urls": ["https://169.254.169.254", "http://127.0.0.1:8020/merchants/x"]},
            headers={"X-API-Key": "s3cret"},
        )
        assert resp.json()["discovered"] == 0

    async def test_key_is_not_leaked_by_settings_repr(self):
        assert "s3cret" not in repr(Settings(admin_api_key="s3cret"))

    async def test_other_routes_do_not_need_the_key(self, make_client):
        client, _ = await make_client()
        assert (await client.get("/health")).status_code == 200


class TestCORS:
    async def _preflight(self, client, origin):
        return await client.options(
            "/api/v1/shop",
            headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
        )

    async def test_no_cross_origin_access_by_default(self, make_client):
        client, _ = await make_client()
        resp = await self._preflight(client, "https://evil.example")
        assert "access-control-allow-origin" not in resp.headers

    async def test_configured_origin_is_allowed_without_credentials(self, make_client):
        client, _ = await make_client(cors_allow_origins="https://app.example.com")
        resp = await self._preflight(client, "https://app.example.com")
        assert resp.headers["access-control-allow-origin"] == "https://app.example.com"
        assert "access-control-allow-credentials" not in resp.headers

    async def test_other_origins_are_still_refused(self, make_client):
        client, _ = await make_client(cors_allow_origins="https://app.example.com")
        resp = await self._preflight(client, "https://evil.example")
        assert "access-control-allow-origin" not in resp.headers

    def test_origin_list_is_parsed(self):
        s = Settings(cors_allow_origins=" https://a.test , https://b.test ,, ")
        assert s.cors_origins == ["https://a.test", "https://b.test"]


class TestErrorsAndLimits:
    async def test_unhandled_error_does_not_leak_details(self, make_client):
        client, app = await make_client()

        @app.get("/boom")
        async def boom():
            raise RuntimeError("secret internal path /srv/keys.pem")

        resp = await client.get("/boom")
        assert resp.status_code == 500
        assert "keys.pem" not in resp.text
        assert "Reference:" in resp.json()["detail"]

    async def test_details_can_be_enabled_for_debugging(self, make_client):
        client, app = await make_client(expose_error_details=True)

        @app.get("/boom")
        async def boom():
            raise RuntimeError("visible")

        assert "visible" in (await client.get("/boom")).text

    @pytest.mark.parametrize(
        "body",
        [
            {"query": ""},
            {"query": "x" * 1001},
            {"query": "ok", "budget": -5},
            {"query": "ok", "budget": 10**9},
            {"query": "ok", "shipping_address": {"full_name": "A"}},
        ],
    )
    async def test_invalid_shop_requests_are_rejected(self, make_client, body):
        client, _ = await make_client()
        assert (await client.post("/api/v1/shop", json=body)).status_code == 422

    @pytest.mark.parametrize(
        "body",
        [
            {"urls": []},
            {"urls": ["https://a.test"] * 21},
            {"urls": ["https://a.test/" + "x" * 2100]},
        ],
    )
    async def test_discover_input_is_bounded(self, make_client, body):
        client, _ = await make_client(admin_api_key="k")
        resp = await client.post(
            "/api/v1/merchants/discover", json=body, headers={"X-API-Key": "k"}
        )
        assert resp.status_code == 422

    async def test_compare_result_limit_is_bounded(self, make_client):
        client, _ = await make_client()
        resp = await client.post(
            "/api/v1/compare", json={"product_query": "x", "max_results_per_merchant": 10_000}
        )
        assert resp.status_code == 422

    async def test_session_creation_is_capped(self, make_client):
        client, _ = await make_client(max_active_sessions=1)
        first = await client.post("/api/v1/shop", json={"query": "keyboard"})
        second = await client.post("/api/v1/shop", json={"query": "keyboard"})
        assert first.status_code == 200
        assert second.status_code == 503

    async def test_upstream_error_text_is_truncated(self):
        import httpx

        def handler(request):
            return httpx.Response(400, text="E" * 5000)

        client = UCPClient(timeout=1, max_retries=0)
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with pytest.raises(UCPClientError) as info:
            await client.discover("http://a.test")
        assert len(str(info.value)) < 400
