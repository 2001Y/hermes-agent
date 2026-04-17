# Provider-Managed Monthly Usage/Billing Plan

> **For Hermes:** Prefer the smallest possible implementation. Do not add per-call cost ledgers, request counters, or service-side price constants.

**Goal:** Show tool/service monthly usage or billing only for providers that expose a live provider-managed API for the user's real account, API key, project, or team.

**Architecture:** Hermes keeps existing model/session cost behavior unchanged. For tool/service cost or usage, Hermes adds only thin provider adapters that fetch live provider-managed monthly snapshots from external usage/billing APIs. Hermes does not reconstruct per-call history, does not convert credits to USD internally, and omits providers that do not expose a suitable monthly snapshot API.

**Tech Stack:** Python, FastAPI, React, TypeScript, pytest, Vitest

---

## Hard rules

- Do **not** store per-call tool cost rows in SQLite.
- Do **not** store request counts just to derive tool costs later.
- Do **not** embed service price constants in Hermes for tool-cost tracking.
- Do **not** convert credits or usage counts to USD inside Hermes.
- Do **not** add user-facing provider configuration just for cost tracking.
- If a provider has no suitable monthly usage/billing API, omit it.

---

## Minimal target shape

Hermes should eventually expose something like:

```json
{
  "provider_monthly_usage": {
    "sources": [
      {
        "provider": "example-provider",
        "status": "supported",
        "scope": "api_key",
        "period": {
          "kind": "calendar_month",
          "start": "2026-04-01T00:00:00Z",
          "end": "2026-04-30T23:59:59Z"
        },
        "unit": "credits",
        "value": 1234,
        "breakdown": {
          "search": 1000,
          "extract": 234
        },
        "fetched_at": 1710000000,
        "source": "provider_usage_api"
      }
    ],
    "unsupported": ["parallel"]
  }
}
```

Important: this is a live fetched summary view, not a reconstructed ledger.

---

## Implementation order

### Task 1: remove ledger-oriented tool-cost code

**Objective:** Align the branch with the billing-API-only direction.

**Files:**
- Modify: `hermes_state.py`
- Modify: `hermes_cli/web_server.py`
- Modify: `tests/test_hermes_state.py`
- Modify: `tests/hermes_cli/test_web_server.py`
- Modify: `web/src/lib/api.ts`
- Modify: `web/src/pages/AnalyticsPage.tsx`

**Requirements:**
- remove `cost_records` schema additions
- remove `CostRecordDraft`
- remove `upsert_cost_record(...)`
- remove `get_tracked_cost_analytics(...)`
- remove `tracked_costs` API/UI shape added for the ledger path
- restore tests accordingly

### Task 2: survey providers for provider-managed monthly usage/billing APIs

**Objective:** Support only real provider-managed monthly usage/billing endpoints.

**Files:**
- Create or modify a small design note under `docs/plans/` or `docs/references/`

**For each candidate provider, record:**
- whether it exposes account-level, team-level, project-level, or API-key-level monthly usage/billing via API
- what unit it returns (`usd`, `credits`, `usage`, etc.)
- whether Hermes already has credentials/session context needed
- whether the returned value is scoped to the current month or equivalent billing period

**Decision rule:**
- supported if the provider exposes a suitable provider-managed monthly usage/billing snapshot
- unsupported otherwise

### Task 3: add a thin live provider-monthly-usage interface only if at least one provider qualifies

**Objective:** Keep the implementation thin.

**Suggested shape:**
```python
@dataclass
class ProviderMonthlyUsageSnapshot:
    provider: str
    scope: str  # api_key / project / account / team
    unit: str   # usd / credits / usage
    value: float | int
    period_start: str
    period_end: str
    fetched_at: float
    source: str = "provider_usage_api"
    breakdown: dict[str, Any] | None = None
```

And one small fetcher layer, e.g.:
- `agent/tool_billing.py`

With functions like:
- `get_supported_provider_monthly_usage() -> list[ProviderMonthlyUsageSnapshot]`

No DB writes.

### Task 4: expose live provider-managed monthly snapshots in API

**Objective:** Return only live fetched supported monthly usage/billing summaries.

**Files:**
- Modify: `hermes_cli/web_server.py`
- Modify tests accordingly

**Requirements:**
- existing analytics payload stays stable unless there is a clear additive section name
- returned data must come from live fetchers, not internal rollups
- return provider unit as-is (`usd`, `credits`, etc.)
- failures degrade gracefully to empty/supported=false states

### Task 5: render minimal UI

**Objective:** Show supported live billing and clearly omit unsupported services.

**Files:**
- Modify: `web/src/lib/api.ts`
- Modify: `web/src/pages/AnalyticsPage.tsx`
- Add UI tests only after the response shape is fixed

**Requirements:**
- small section
- clear wording that only supported provider-managed monthly usage/billing APIs are shown
- display provider unit as-is instead of forcing USD
- no fake totals across unsupported services
- reuse existing dashboard primitives and styling:
  - `Card`, `CardHeader`, `CardTitle`, `CardContent`
  - `Badge`
  - `Button` / existing period selector style
  - plain HTML tables with the same utility classes already used in `AnalyticsPage.tsx`
- match existing page patterns already used in `StatusPage`, `CronPage`, `MemoryPage`, and `SessionsPage`:
  - top-level `flex flex-col gap-6`
  - cards with section headers and compact metadata
  - `overflow-x-auto` wrappers for tables
  - empty states inside `Card`
  - muted helper text and `Badge` variants instead of custom CSS blocks
- avoid introducing new bespoke CSS or one-off visual systems unless existing primitives are clearly insufficient

---

## Non-goals

- no provider-wide synthetic total spend
- no reconstructed history from request logs
- no per-tool per-call audit trail
- no pricing inference from static docs inside Hermes
- no internal credits-to-USD conversion

---

## Completion criteria

The work is complete only when:
1. ledger-oriented tool-cost code is removed
2. supported providers are limited to real provider-managed monthly usage/billing APIs
3. unsupported providers are explicitly omitted
4. no internal per-call tool-cost amounts, counters, or USD conversions are needed
