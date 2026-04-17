# Provider-Managed Monthly Usage/Billing API Survey

**Date:** 2026-04-17  
**Decision rule:** Hermes supports tool/service tracking only when the provider exposes a live provider-managed monthly usage or billing API for the user's real API key, project, team, or account. Hermes displays provider units as-is and does not convert credits to USD internally.

---

## Summary

| Provider | Monthly usage/billing API found | Unit from API | Scope available | Recommended Hermes status |
| --- | --- | --- | --- | --- |
| Exa | Yes | USD | API key | **Supported candidate** |
| Firecrawl | Yes | Credits | Team / billing period | **Supported candidate** |
| Tavily | Yes | Usage / plan usage | API key / account / optional project | **Supported candidate** |
| Parallel | No suitable documented monthly usage/billing API found | N/A | N/A | **Unsupported for now** |

---

## Findings

## Exa

### Evidence
- Docs page: `GET /api-keys/{id}/usage`
- URL: `https://exa.ai/docs/reference/team-management/get-api-key-usage`
- Example response includes:
  - `total_cost_usd`
  - `cost_breakdown[].amount_usd`
- Docs explicitly say this endpoint returns:
  - "cost data from Exa’s billing system"
  - "an authoritative view of what you’re being billed for that API key"

### Assessment
Exa exceeds the minimum bar.

### Hermes recommendation
- Support Exa if Hermes already has or can safely obtain the required service/team API credential.
- Fetch live monthly API-key usage snapshots directly from Exa.
- Display the returned USD values as-is.
- Do not persist reconstructed per-call costs locally.

---

## Firecrawl

### Evidence
- Docs page: `GET /team/credit-usage`
- Docs page: `GET /team/credit-usage/historical`
- URLs:
  - `https://docs.firecrawl.dev/api-reference/endpoint/credit-usage`
  - `https://docs.firecrawl.dev/api-reference/endpoint/credit-usage-historical`
- Returned fields are credits-oriented:
  - `remainingCredits`
  - `planCredits`
  - `totalCredits`
  - billing period timestamps

### Assessment
Firecrawl exposes provider-managed monthly team usage over the current billing period and historical monthly periods. It does not expose USD spend in the surveyed endpoints, but that is acceptable under the current design because Hermes can display provider-managed credits as-is.

### Hermes recommendation
- Support Firecrawl as a monthly usage source.
- Treat the scope as team/account-level billing-period usage, not per-call usage.
- Display credits as credits; do not convert to USD.

---

## Tavily

### Evidence
- Docs page: `GET /usage`
- URL: `https://docs.tavily.com/documentation/api-reference/endpoint/usage`
- Response includes provider-managed usage snapshots such as:
  - `key.usage`
  - `key.search_usage`
  - `key.extract_usage`
  - `account.plan_usage`
  - `account.paygo_usage`
- Optional header:
  - `X-Project-ID` to scope usage to a specific project
- This confirms Tavily has a real API endpoint for API-key/account usage retrieval.

### Assessment
Tavily clearly meets the minimum bar for provider-managed monthly usage tracking.
It does not expose authoritative USD spend in the surveyed endpoint, but that is acceptable because Hermes can display the provider-managed usage values as-is.

### Hermes recommendation
- Support Tavily as a monthly usage source.
- Prefer API-key usage when available; fall back to account usage; allow project-scoped usage when the user already has project IDs.
- Display usage/credits as returned; do not convert to USD.

---

## Parallel

### Evidence
- Surveyed docs and unified docs dump show public pricing tables for Search / Extract / Task / Monitor.
- Parallel FAQ explicitly says:
  - `Platform > Usage` shows real-time request counts and spend.
- AWS Marketplace docs explicitly say:
  - for more granular reporting, use the `Usage` tab in the Parallel platform.
- However, in the surveyed documentation, no documented API endpoint was found for retrieving that usage/spend data programmatically.

### Assessment
Parallel clearly has usage/spend data in the hosted Platform UI, but I could not verify a documented provider-managed API for monthly usage/billing retrieval.
So the current status is: **UI exists, API not yet verified**.

### Hermes recommendation
- Treat Parallel as unsupported for now in the API-driven implementation.
- Only support it once a documented usage/billing API endpoint is confirmed.

---

## Recommended v1 scope

### Support candidates
- Exa
- Firecrawl
- Tavily

### Explicitly unsupported
- Parallel

---

## Minimal implementation direction

If Hermes proceeds now, the smallest clean implementation is:

1. Add thin live monthly-usage fetchers for Exa, Firecrawl, and Tavily.
2. Return provider-managed monthly snapshots from API/UI without normalizing units to USD.
3. Show unsupported providers as omitted / unsupported.
4. Do not add SQLite tool-cost schema.
5. Do not store per-call request counts, price constants, or credits-to-USD conversion logic.
