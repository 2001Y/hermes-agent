# Tracked Cost Records Architecture for Hermes Agent

**Date:** 2026-04-16  
**Branch:** `docs/cost-tracking-ledger-plan`  
**Worktree:** `.worktrees/docs-cost-tracking-ledger-plan`  
**Status:** superseded design document / implementation direction

> **Superseded by user decision (2026-04-17):** Do **not** persist per-call tool cost amounts, request counts, or tool-cost ledgers in Hermes for third-party services. Support tool/service tracking only when the external service exposes a live provider-managed monthly usage or billing API for the user's real API key, project, team, or account. Display provider units as-is (`usd`, `credits`, `usage`, etc.). If a service does not expose that kind of API, Hermes should omit it entirely rather than approximate from request counts, credits, or embedded price constants.

> This document captures an earlier ledger-oriented direction. Keep it only as rejected/superseded context unless explicitly revived.

---

## 1. Problem Statement

Hermes already has production cost/accounting behavior for model usage:

- `agent/usage_pricing.py` computes LLM cost metadata
- `run_agent.py` persists session-level token and cost deltas
- `gateway/run.py` also computes session-level cost
- `hermes_state.py` stores `sessions.estimated_cost_usd`, `actual_cost_usd`, `cost_status`, and related fields
- `hermes_cli/web_server.py` exposes aggregate usage totals via `/api/analytics/usage`

That means the main missing product gap is **not** “LLM cost exists nowhere.”

The real missing gap is:

- billable tool/service calls are not represented with provider/service attribution in analytics,
- the dashboard therefore cannot show a tracked cost breakdown across Hermes services,
- and the most obvious first missing area is web tools.

### Product goal

Add tracked cost attribution for **missing billable tool/service providers** while:

- preserving existing model/session cost behavior,
- avoiding duplicate LLM accounting systems,
- counting only services whose cost can be recorded confidently,
- and exposing a clean tracked-cost view in analytics/UI.

### Desired product behavior

1. Existing model/session cost remains intact and continues to power current totals.
2. New tracked-cost attribution focuses first on **missing billable tool/services**.
3. Hermes counts only services with retrievable or deterministic pricing support.
4. Unsupported services are omitted, not shown as zero and not merged into fake totals.
5. The UI shows a unified tracked-cost story without making users manage cost-tracking configuration.

---

## 2. Current Repository Reality

## 2.1 Existing LLM/model cost path already exists

Hermes already ships a model-cost flow:

- `agent/usage_pricing.py`
  - `CanonicalUsage`
  - `PricingEntry`
  - `CostResult`
  - `estimate_usage_cost(...)`
- `run_agent.py`
  - computes `cost_result`
  - updates in-memory session cost
  - persists session-level cost via `SessionDB.update_token_counts(...)`
- `gateway/run.py`
  - also derives cost from usage
- `hermes_state.py`
  - stores session-level aggregate cost fields
- `hermes_cli/web_server.py`
  - returns `total_estimated_cost` / `total_actual_cost`

This path is already the official baseline for model cost.

## 2.2 Missing tracked provider/service attribution for web tools

The natural first target is:

- `tools/web_tools.py`
  - `web_search`
  - `web_extract`
  - `web_crawl`
  - backend selection between providers such as Parallel and Firecrawl

This area is the best first candidate because:

- it sits near the actual billable provider call,
- it is not already fully represented by the current LLM/session accounting path,
- and it is simpler than browser-provider billing semantics.

## 2.3 Browser billing is a later step

`tools/browser_tool.py` and browser cloud providers are relevant, but they are a worse first target because Hermes supports:

- local non-billable mode,
- multiple cloud providers,
- different provider-specific session semantics.

That makes browser cost tracking a second phase, not the smallest v1.

## 2.4 Existing analytics endpoint is session-centric

`GET /api/analytics/usage` currently aggregates from `sessions` and returns:

- `daily`
- `by_model`
- `totals`

It does **not** yet expose a tracked ledger/breakdown for billable web tools.

---

## 3. Correct Scope for the New System

The new tracked-cost system should **augment** existing cost behavior, not replace it.

## 3.1 What the new system is for

The new system is for:

- provider/service attribution of missing billable tool costs,
- tracked-cost breakdowns in analytics and dashboard UI,
- future addition of more non-LLM billable services.

## 3.2 What the new system is not for

The new system is **not** for:

- rebuilding model cost from scratch,
- replacing the current LLM/session cost pipeline,
- forcing all current dashboard totals to move to a new table immediately,
- adding speculative abstraction layers before the real tracked services are integrated.

## 3.3 Consequence for v1

The first tracked services should be only:

- `web_search`
- `web_extract`
- `web_crawl`

Browser providers are deferred until the web-tool path is proven.

---

## 4. Core Design Choice

Use a small ledger table for **tracked service cost records**.

The row should represent one logical billable service operation with a deterministic key.

Use **`cost_records`** rather than an append-only `cost_events` stream.

### Why `cost_records`?

Because Hermes needs:

- idempotent writes,
- safe retries,
- provider/service attribution,
- and future estimated → actual replacement

without double-counting.

A current-state row per logical operation is enough for this.

### Important semantic rule

A row stores the **current best-known tracked cost** for one logical external service operation.

That means:

- an estimated row may later be replaced with actual cost,
- the table is about correct current attribution,
- not historical estimate audit trails.

That is sufficient for dashboard analytics.

---

## 5. Relationship to Existing LLM Cost Fields

This is the most important architectural boundary.

## 5.1 Existing session cost stays authoritative for model/session accounting

For the first implementation:

- `sessions.estimated_cost_usd`
- `sessions.actual_cost_usd`
- `cost_status`
- `cost_source`

remain the shipped source for current model/session accounting behavior.

## 5.2 `cost_records` is additive

`cost_records` should begin as the additive tracked-cost layer for missing tool/service costs.

That means:

- do **not** rewrite `run_agent.py` around a new ledger-first LLM flow,
- do **not** duplicate LLM totals through a second competing path in v1,
- do **not** make the new table responsible for all existing dashboard totals immediately.

## 5.3 UI/API integration model

At the API/UI layer, Hermes can present a unified tracked-cost view that combines:

- existing session/model cost information,
- plus new tracked tool/service cost attribution,

but the internal write paths do not all need to be replaced at once.

This is the simplest smart integration path.

---

## 6. Proposed Schema

### 6.1 Recommended minimal `cost_records` table

```sql
CREATE TABLE cost_records (
    record_key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    occurred_at REAL NOT NULL,
    provider TEXT NOT NULL,
    service TEXT NOT NULL,
    amount_usd REAL NOT NULL,
    pricing_kind TEXT NOT NULL CHECK (pricing_kind IN ('estimated', 'actual')),
    pricing_source TEXT NOT NULL,
    external_id TEXT,
    details_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX idx_cost_records_session_id ON cost_records(session_id);
CREATE INDEX idx_cost_records_occurred_at ON cost_records(occurred_at);
CREATE INDEX idx_cost_records_provider ON cost_records(provider);
CREATE INDEX idx_cost_records_service ON cost_records(service);
```

### 6.2 Why `category` is intentionally omitted in v1

For the first tracked implementation, only web tools are in scope.

That means `category` would just be a repeated constant like `web`, which adds maintenance cost without adding meaning.

If Hermes later expands tracked services beyond web tools, category can be added when it becomes genuinely useful.

### 6.3 Suggested service names for v1

Use stable internal service identifiers:

- `web_search`
- `web_extract`
- `web_crawl`

---

## 7. Where to Record Cost

Record cost **as close as possible to the real billable provider boundary**.

## 7.1 Web tools

Primary target:

- `tools/web_tools.py`

This area already knows:

- which logical tool was called,
- which backend/provider is active,
- whether the operation actually succeeded,
- and usually enough context to determine the billing unit.

That makes it the right first place to emit tracked rows.

## 7.2 Browser tools

Browser tools are explicitly deferred.

They should not be part of the first tracked-cost implementation because their billing semantics are more complex and would bloat v1.

## 7.3 Existing LLM path

The existing LLM path should be treated as integrated baseline behavior, not first reimplementation target.

Future unification is possible later, but it is not the first missing feature.

---

## 8. Shared Types and Persistence

## 8.1 Shared draft type

A small shared draft object is still useful.

Suggested shape:

```python
@dataclass(frozen=True)
class CostRecordDraft:
    record_key: str
    occurred_at: float
    provider: str
    service: str
    amount_usd: float
    pricing_kind: Literal["estimated", "actual"]
    pricing_source: str
    external_id: str | None = None
    details: dict[str, Any] | None = None
```

## 8.2 Placement

Keep this boring.

Good options:
- `agent/cost_tracking.py`
- `hermes_state.py` adjacent helpers

Avoid a large registry/provider-discovery framework in v1.

The first need is not pluggability. The first need is a clean write contract.

## 8.3 Shared persistence API

Add to `hermes_state.py`:

```python
def upsert_cost_record(self, session_id: str, draft: CostRecordDraft) -> None:
    ...
```

Required v1 behavior:

1. upsert by `record_key`
2. serialize `details`
3. do not corrupt existing session cost fields
4. keep writes transactional and idempotent

---

## 9. API Direction

## 9.1 `/api/analytics/usage` remains the main analytics endpoint

Extend the existing endpoint additively.

Do **not** create a parallel dashboard endpoint unless clearly necessary.

## 9.2 Recommended response extension

Add a nested `tracked_costs` section that is explicitly about tracked web-tool attribution.

Example:

```json
{
  "daily": [],
  "by_model": [],
  "totals": {
    "total_estimated_cost": 12.34,
    "total_actual_cost": 1.23
  },
  "tracked_costs": {
    "cost_usd": 3.21,
    "estimated_cost_usd": 2.50,
    "actual_cost_usd": 0.71,
    "tracked_session_count": 6,
    "record_count": 12,
    "by_provider": [],
    "by_service": []
  }
}
```

## 9.3 Meaning of the fields

- existing `totals.*cost*` continues to reflect current Hermes session/model cost behavior
- `tracked_costs.*` reflects the additive tracked ledger for attributed billable web-tool services

That keeps meanings clear instead of collapsing unlike concepts too early.

---

## 10. Web UI Direction

Relevant file:

- `web/src/pages/AnalyticsPage.tsx`

### Recommended additions

Show a dedicated tracked-cost section with:

- tracked cost summary
- provider breakdown
- service breakdown
- a clear note that tracked cost only includes explicitly supported services

### Recommended wording

Prefer:

- `Tracked cost`
- `Tracked cost breakdown`
- `Tracked services`

Avoid calling it universal total spend unless Hermes truly tracks all major billable surfaces.

---

## 11. First Supported Services

## 11.1 First-class v1 services

- `web_search`
- `web_extract`
- `web_crawl`

## 11.2 v1 inclusion rule

Only add a service when at least one of the following is true:

1. provider returns cost or billing metadata directly
2. pricing is deterministic enough to compute reliably from the successful operation
3. the billable unit is unambiguous in Hermes code

## 11.3 v1 exclusion rule

Do not track:

- browser providers yet
- local/non-billable paths
- services whose billing unit is unclear in the current implementation
- unsupported providers via fake zero-value rows

---

## 12. Testing Requirements

## 12.1 State-layer tests

Add tests for:

1. `cost_records` migration
2. idempotent upsert by `record_key`
3. estimated → actual replacement
4. cascade delete with session
5. aggregate tracked totals by provider/service

## 12.2 Tool integration tests

Add focused tests for:

6. successful `web_search` provider call writes one tracked row
7. successful `web_extract` provider call writes one tracked row
8. successful `web_crawl` provider call writes one tracked row
9. unsupported / non-billable path writes no tracked row

## 12.3 API tests

10. `/api/analytics/usage` remains backward compatible
11. `tracked_costs.by_provider` is correct
12. `tracked_costs.by_service` is correct
13. empty ledger state renders cleanly

## 12.4 UI tests

14. analytics page renders tracked-cost section when records exist
15. empty state is explicit when none exist
16. partial/tracked wording is clear

---

## 13. Rejected Directions

## 13.1 Rebuilding LLM cost as the first tracked-cost project

Rejected because Hermes already has shipped model/session cost behavior. Doing LLM-first again creates unnecessary duplication and misses the actual gap.

## 13.2 Tracking browser and web in the same first slice

Rejected because browser billing boundaries are more complex. Web tools are the smaller clean first target.

## 13.3 Treating tracked ledger as full replacement for all cost semantics immediately

Rejected because the first missing feature is web-tool attribution, not total replacement of the current cost model.

## 13.4 UI-managed provider selection

Rejected because it pushes internal tracking details into UX.

## 13.5 Fake totals for unsupported services

Rejected because it would mislead users.

---

## 14. Final Recommendation

Implement tracked cost as an additive provider/service ledger for **billable web tools first**, while preserving Hermes’s existing model/session cost path.

### In one sentence

> Hermes should keep its existing LLM/session cost implementation as the baseline, add `cost_records` for billable web-tool operations, and expose tracked provider/service breakdowns in analytics and the web dashboard.

This matches the real product need while staying small:

- no duplicate LLM cost system,
- no misleading unsupported totals,
- cost written near real web-tool billing boundaries,
- and a UI that can finally show tracked tool/service spend.

---

## 15. Suggested Next Step

Write a fresh implementation plan that starts from:

- `hermes_state.py`
- `tools/web_tools.py`
- `hermes_cli/web_server.py`
- `web/src/pages/AnalyticsPage.tsx`

and treats existing LLM/session cost as pre-existing integrated infrastructure rather than the first thing to rebuild.