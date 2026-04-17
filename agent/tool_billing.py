"""Live provider-managed monthly usage / billing snapshots for tool providers.

No per-call persistence, no local price tables, no credits-to-USD conversion.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from hermes_cli.config import get_env_value

_EXA_ADMIN_BASE_URL = "https://admin-api.exa.ai/team-management"
_FIRECRAWL_BASE_URL = "https://api.firecrawl.dev/v2"
_TAVILY_BASE_URL = "https://api.tavily.com"


def _utc_now(now: float | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(now, tz=timezone.utc)


def _month_bounds(now: float | None = None) -> tuple[str, str]:
    dt = _utc_now(now)
    start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if dt.month == 12:
        next_month = dt.replace(year=dt.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        next_month = dt.replace(month=dt.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
    end = next_month.timestamp() - 1
    end_dt = datetime.fromtimestamp(end, tz=timezone.utc)
    return start.isoformat().replace("+00:00", "Z"), end_dt.isoformat().replace("+00:00", "Z")


def _fetch_exa_monthly_usage(now: float | None = None) -> Optional[dict[str, Any]]:
    service_key = get_env_value("EXA_SERVICE_API_KEY") or get_env_value("EXA_API_KEY")
    api_key_id = get_env_value("EXA_API_KEY_ID")
    if not service_key or not api_key_id:
        return None

    start, end = _month_bounds(now)
    response = httpx.get(
        f"{_EXA_ADMIN_BASE_URL}/api-keys/{api_key_id}/usage",
        headers={"x-api-key": service_key},
        params={"start_date": start, "end_date": end},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    fetched_at = now if now is not None else _utc_now().timestamp()
    return {
        "provider": "exa",
        "status": "supported",
        "scope": "api_key",
        "unit": "usd",
        "value": data.get("total_cost_usd", 0),
        "period": {
            "kind": "calendar_month",
            "start": data.get("period", {}).get("start", start),
            "end": data.get("period", {}).get("end", end),
        },
        "breakdown": {"cost_breakdown": data.get("cost_breakdown", [])},
        "fetched_at": fetched_at,
        "source": "provider_usage_api",
    }


def _fetch_firecrawl_monthly_usage(now: float | None = None) -> Optional[dict[str, Any]]:
    api_key = get_env_value("FIRECRAWL_API_KEY")
    if not api_key:
        return None
    base_url = (get_env_value("FIRECRAWL_API_URL") or _FIRECRAWL_BASE_URL).rstrip("/")
    response = httpx.get(
        f"{base_url}/team/credit-usage",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data", payload)
    remaining = data.get("remainingCredits", 0) or 0
    plan = data.get("planCredits", 0) or 0
    used = max(plan - remaining, 0)
    fetched_at = now if now is not None else _utc_now().timestamp()
    return {
        "provider": "firecrawl",
        "status": "supported",
        "scope": "team",
        "unit": "credits",
        "value": used,
        "period": {
            "kind": "billing_period",
            "start": data.get("billingPeriodStart"),
            "end": data.get("billingPeriodEnd"),
        },
        "breakdown": {
            "remainingCredits": remaining,
            "planCredits": plan,
        },
        "fetched_at": fetched_at,
        "source": "provider_usage_api",
    }


def _fetch_tavily_monthly_usage(now: float | None = None) -> Optional[dict[str, Any]]:
    api_key = get_env_value("TAVILY_API_KEY")
    if not api_key:
        return None
    start, end = _month_bounds(now)
    headers = {"Authorization": f"Bearer {api_key}"}
    project_id = get_env_value("TAVILY_PROJECT")
    if project_id:
        headers["X-Project-ID"] = project_id
    response = httpx.get(f"{_TAVILY_BASE_URL}/usage", headers=headers, timeout=30)
    response.raise_for_status()
    data = response.json()
    key_usage = data.get("key") or {}
    account_usage = data.get("account") or {}
    scope = "project" if project_id else ("api_key" if key_usage else "account")
    value = key_usage.get("usage") if key_usage else account_usage.get("plan_usage", 0)
    fetched_at = now if now is not None else _utc_now().timestamp()
    breakdown = {
        "search_usage": key_usage.get("search_usage", account_usage.get("search_usage", 0)),
        "extract_usage": key_usage.get("extract_usage", account_usage.get("extract_usage", 0)),
        "crawl_usage": key_usage.get("crawl_usage", account_usage.get("crawl_usage", 0)),
        "map_usage": key_usage.get("map_usage", account_usage.get("map_usage", 0)),
        "research_usage": key_usage.get("research_usage", account_usage.get("research_usage", 0)),
        "account_plan_usage": account_usage.get("plan_usage"),
        "account_paygo_usage": account_usage.get("paygo_usage"),
        "current_plan": account_usage.get("current_plan"),
    }
    return {
        "provider": "tavily",
        "status": "supported",
        "scope": scope,
        "unit": "usage",
        "value": value or 0,
        "period": {
            "kind": "calendar_month",
            "start": start,
            "end": end,
        },
        "breakdown": breakdown,
        "fetched_at": fetched_at,
        "source": "provider_usage_api",
    }


def get_supported_provider_monthly_usage(now: float | None = None) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    unsupported: list[dict[str, str]] = []

    for provider, fetcher in [
        ("exa", _fetch_exa_monthly_usage),
        ("firecrawl", _fetch_firecrawl_monthly_usage),
        ("tavily", _fetch_tavily_monthly_usage),
    ]:
        try:
            snapshot = fetcher(now=now)
        except Exception:
            snapshot = None
        if snapshot:
            sources.append(snapshot)
        else:
            unsupported.append({"provider": provider, "reason": "not_configured_or_supported"})

    unsupported.append({"provider": "parallel", "reason": "no_documented_usage_api"})
    sources.sort(key=lambda item: item["provider"])
    return {"sources": sources, "unsupported": unsupported}
