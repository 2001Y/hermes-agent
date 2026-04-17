import pytest


def _response(payload):
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    return _Resp()


def test_get_supported_provider_monthly_usage_includes_tavily_and_firecrawl(monkeypatch):
    from agent import tool_billing

    monkeypatch.setattr(tool_billing, "_fetch_tavily_monthly_usage", lambda now=None: {
        "provider": "tavily",
        "status": "supported",
        "scope": "api_key",
        "unit": "usage",
        "value": 150,
        "period": {"kind": "calendar_month", "start": "2026-04-01T00:00:00Z", "end": "2026-04-30T23:59:59Z"},
        "breakdown": {"search_usage": 100, "extract_usage": 25},
        "fetched_at": 1710000000.0,
        "source": "provider_usage_api",
    })
    monkeypatch.setattr(tool_billing, "_fetch_firecrawl_monthly_usage", lambda now=None: {
        "provider": "firecrawl",
        "status": "supported",
        "scope": "team",
        "unit": "credits",
        "value": 250,
        "period": {"kind": "billing_period", "start": "2026-04-01T00:00:00Z", "end": "2026-04-30T23:59:59Z"},
        "breakdown": {"remainingCredits": 750, "planCredits": 1000},
        "fetched_at": 1710000000.0,
        "source": "provider_usage_api",
    })
    monkeypatch.setattr(tool_billing, "_fetch_exa_monthly_usage", lambda now=None: None)

    result = tool_billing.get_supported_provider_monthly_usage(now=1710000000.0)

    assert [entry["provider"] for entry in result["sources"]] == ["firecrawl", "tavily"]
    assert result["unsupported"] == [{"provider": "exa", "reason": "not_configured_or_supported"}, {"provider": "parallel", "reason": "no_documented_usage_api"}]


def test_fetch_tavily_monthly_usage_maps_usage_response(monkeypatch):
    from agent import tool_billing

    monkeypatch.setattr(tool_billing, "get_env_value", lambda key: "tvly-key" if key == "TAVILY_API_KEY" else None)
    monkeypatch.setattr(tool_billing.httpx, "get", lambda *args, **kwargs: _response({
        "key": {
            "usage": 150,
            "search_usage": 100,
            "extract_usage": 25,
            "crawl_usage": 15,
            "map_usage": 7,
            "research_usage": 3,
        },
        "account": {
            "current_plan": "Bootstrap",
            "plan_usage": 500,
            "paygo_usage": 25,
        },
    }))

    snapshot = tool_billing._fetch_tavily_monthly_usage(now=1710000000.0)

    assert snapshot["provider"] == "tavily"
    assert snapshot["scope"] == "api_key"
    assert snapshot["unit"] == "usage"
    assert snapshot["value"] == 150
    assert snapshot["breakdown"]["account_plan_usage"] == 500
    assert snapshot["breakdown"]["account_paygo_usage"] == 25


def test_fetch_firecrawl_monthly_usage_maps_credit_usage(monkeypatch):
    from agent import tool_billing

    monkeypatch.setattr(tool_billing, "get_env_value", lambda key: "fc-key" if key == "FIRECRAWL_API_KEY" else None)
    monkeypatch.setattr(tool_billing.httpx, "get", lambda *args, **kwargs: _response({
        "success": True,
        "data": {
            "remainingCredits": 750,
            "planCredits": 1000,
            "billingPeriodStart": "2026-04-01T00:00:00Z",
            "billingPeriodEnd": "2026-04-30T23:59:59Z",
        },
    }))

    snapshot = tool_billing._fetch_firecrawl_monthly_usage(now=1710000000.0)

    assert snapshot["provider"] == "firecrawl"
    assert snapshot["scope"] == "team"
    assert snapshot["unit"] == "credits"
    assert snapshot["value"] == 250
    assert snapshot["period"]["start"] == "2026-04-01T00:00:00Z"
    assert snapshot["breakdown"]["remainingCredits"] == 750


def test_fetch_exa_monthly_usage_requires_api_key_id(monkeypatch):
    from agent import tool_billing

    values = {
        "EXA_API_KEY": "exa-service-key",
        "EXA_API_KEY_ID": None,
        "EXA_SERVICE_API_KEY": None,
    }
    monkeypatch.setattr(tool_billing, "get_env_value", lambda key: values.get(key))

    assert tool_billing._fetch_exa_monthly_usage(now=1710000000.0) is None
