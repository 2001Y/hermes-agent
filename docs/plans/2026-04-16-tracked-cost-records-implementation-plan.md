# Tracked Tool-Service Cost v1 Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Add tracked-cost attribution for missing billable web tools, integrate that attribution into the existing analytics/dashboard flow, and leave the existing model/session cost implementation untouched.

**Architecture:** Keep Hermes’s current LLM/session cost path as the baseline. Add a small `cost_records` ledger in `hermes_state.py`, write tracked rows at the real provider billing boundary in `tools/web_tools.py`, then extend `/api/analytics/usage` and `AnalyticsPage.tsx` with additive tracked provider/service breakdowns.

**Tech Stack:** Python, FastAPI, React, TypeScript, Vitest, pytest

> **Superseded by user decision (2026-04-17):** This ledger-based implementation plan should not be followed as written. The preferred design is: do not store per-tool cost amounts, request counts, or service-specific price constants in Hermes; only integrate services that expose a live provider-managed monthly usage or billing API for the user's actual API key, project, team, or account; report those live snapshots directly with provider units shown as-is; omit unsupported services entirely.

---

## Constraints and guardrails

- Do **not** rebuild or replace the existing LLM/session cost path.
- Do **not** reinterpret `sessions.estimated_cost_usd` / `actual_cost_usd` in this work.
- Do **not** create a second competing model-cost system.
- Do **not** add browser-provider tracking in this first implementation.
- Do **not** add Web UI settings for choosing tracked providers.
- Do **not** count unsupported services as zero.
- Follow TDD strictly: every production change starts with a failing test.

---

## File map

**Primary implementation files**
- Modify: `hermes_state.py`
- Modify: `hermes_cli/web_server.py`
- Modify: `web/src/lib/api.ts`
- Modify: `web/src/pages/AnalyticsPage.tsx`
- Modify: `tools/web_tools.py`

**Primary test files**
- Modify: `tests/test_hermes_state.py`
- Modify: `tests/hermes_cli/test_web_server.py`
- Modify: `tests/tools/test_web_tools_config.py`
- Create: `tests/tools/test_web_tools_tracked_costs.py`
- Create: `web/src/pages/AnalyticsPage.test.tsx`

---

## Task 1: Add failing schema tests for tracked web-tool records

**Objective:** Define the additive tracked-cost ledger table before touching implementation.

**Files:**
- Modify: `tests/test_hermes_state.py`
- Modify: `hermes_state.py`

**Step 1: Write failing tests**

Add tests to `tests/test_hermes_state.py` for:

```python
def test_cost_records_table_exists_on_fresh_db(db):
    row = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='cost_records'"
    ).fetchone()
    assert row is not None


def test_cost_records_indexes_exist_on_fresh_db(db):
    indexes = {
        r["name"]
        for r in db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='cost_records'"
        ).fetchall()
    }
    assert "idx_cost_records_session_id" in indexes
    assert "idx_cost_records_occurred_at" in indexes
    assert "idx_cost_records_provider" in indexes
    assert "idx_cost_records_service" in indexes
```

Also add a migration test that simulates an existing pre-`cost_records` DB and verifies initialization upgrades it cleanly.

**Step 2: Run test to verify failure**

Run:
```bash
source venv/bin/activate && python -m pytest tests/test_hermes_state.py -k cost_records -q
```

Expected: FAIL — table/indexes do not exist yet.

**Step 3: Write minimal implementation**

In `hermes_state.py`:
- bump `SCHEMA_VERSION`
- add a `cost_records` table with these columns:
  - `record_key`
  - `session_id`
  - `occurred_at`
  - `provider`
  - `service`
  - `amount_usd`
  - `pricing_kind`
  - `pricing_source`
  - `external_id`
  - `details_json`
  - `created_at`
  - `updated_at`
- add the required indexes
- add the migration block for existing DBs

**Step 4: Run test to verify pass**

Run:
```bash
source venv/bin/activate && python -m pytest tests/test_hermes_state.py -k cost_records -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add hermes_state.py tests/test_hermes_state.py
git commit -m "test: add tracked web tool cost schema coverage"
```

---

## Task 2: Add failing persistence tests for `upsert_cost_record(...)`

**Objective:** Lock down a small, idempotent write contract for tracked service records.

**Files:**
- Modify: `tests/test_hermes_state.py`
- Modify: `hermes_state.py`

**Step 1: Write failing tests**

Add tests for a new helper such as:

```python
def upsert_cost_record(self, session_id: str, draft: CostRecordDraft) -> None:
    ...
```

Test cases:
1. inserts first row
2. second write with same `record_key` updates instead of duplicating
3. estimated → actual replacement leaves one row
4. cascade delete removes child rows with the session

Suggested test draft shape:

```python
from hermes_state import CostRecordDraft


def test_upsert_cost_record_is_idempotent(db):
    db.create_session(session_id="s1", source="cli")
    draft = CostRecordDraft(
        record_key="s1:web_search:1",
        occurred_at=123.0,
        provider="firecrawl",
        service="web_search",
        amount_usd=0.25,
        pricing_kind="estimated",
        pricing_source="official_docs_snapshot",
        external_id=None,
        details={"query": "Hermes", "result_count": 5},
    )
    db.upsert_cost_record("s1", draft)
    db.upsert_cost_record("s1", draft)
    count = db._conn.execute("SELECT COUNT(*) AS n FROM cost_records").fetchone()["n"]
    assert count == 1
```

**Step 2: Run test to verify failure**

Run:
```bash
source venv/bin/activate && python -m pytest tests/test_hermes_state.py -k upsert_cost_record -q
```

Expected: FAIL — helper/type do not exist yet.

**Step 3: Write minimal implementation**

In `hermes_state.py`, add:

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
    external_id: Optional[str] = None
    details: Optional[dict[str, Any]] = None
```

and implement `upsert_cost_record(...)`.

Behavior:
- serialize `details` to JSON
- insert-or-update by `record_key`
- preserve `created_at` on update
- refresh `updated_at` on update
- do **not** change existing `sessions.*cost*` fields

**Step 4: Run test to verify pass**

Run:
```bash
source venv/bin/activate && python -m pytest tests/test_hermes_state.py -k upsert_cost_record -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add hermes_state.py tests/test_hermes_state.py
git commit -m "feat: add tracked web tool cost upsert path"
```

---

## Task 3: Add failing aggregate-query tests for provider/service breakdowns

**Objective:** Define the tracked-cost analytics contract in the state layer before touching the API.

**Files:**
- Modify: `tests/test_hermes_state.py`
- Modify: `hermes_state.py`

**Step 1: Write failing tests**

Add tests for a helper like:

```python
def get_tracked_cost_analytics(self, *, occurred_after: float | None = None) -> dict:
    ...
```

Expected shape:

```python
{
    "cost_usd": 1.50,
    "estimated_cost_usd": 1.00,
    "actual_cost_usd": 0.50,
    "tracked_session_count": 2,
    "record_count": 3,
    "by_provider": [
        {"provider": "firecrawl", "cost_usd": 1.25, "record_count": 2, "session_count": 2}
    ],
    "by_service": [
        {"service": "web_extract", "cost_usd": 1.00, "record_count": 1, "session_count": 1}
    ],
}
```

Add an explicit time-filter test.

**Step 2: Run test to verify failure**

Run:
```bash
source venv/bin/activate && python -m pytest tests/test_hermes_state.py -k tracked_cost_analytics -q
```

Expected: FAIL — helper does not exist yet.

**Step 3: Write minimal implementation**

In `hermes_state.py`, add a helper that computes:
- top-level tracked totals
- grouped provider totals
- grouped service totals

Use `occurred_at` for the tracked ledger time filter.

**Step 4: Run test to verify pass**

Run:
```bash
source venv/bin/activate && python -m pytest tests/test_hermes_state.py -k tracked_cost_analytics -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add hermes_state.py tests/test_hermes_state.py
git commit -m "feat: add tracked web cost analytics queries"
```

---

## Task 4: Add failing backend-resolution tests for billable web tools

**Objective:** Freeze which web tool paths are billable and therefore eligible for tracked rows.

**Files:**
- Modify: `tests/tools/test_web_tools_config.py`
- Modify: `tools/web_tools.py`

**Step 1: Write failing tests**

Add tests that make the billable backend decision explicit:

1. `Parallel` backend resolves to tracked provider `parallel`
2. `Firecrawl` backend resolves to tracked provider `firecrawl`
3. unsupported/unknown backend resolves to no tracked provider

If no helper exists, add tests for a small helper such as:

```python
def get_web_cost_tracking_metadata(tool_name: str) -> dict | None:
    ...
```

**Step 2: Run test to verify failure**

Run:
```bash
source venv/bin/activate && python -m pytest tests/tools/test_web_tools_config.py -q
```

Expected: FAIL — helper/contract absent.

**Step 3: Write minimal implementation**

In `tools/web_tools.py`, add the smallest helper needed to answer:
- current backend/provider
- normalized service name
- whether this execution path is trackable

Do **not** write rows yet in this task.

**Step 4: Run test to verify pass**

Run:
```bash
source venv/bin/activate && python -m pytest tests/tools/test_web_tools_config.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add tools/web_tools.py tests/tools/test_web_tools_config.py
git commit -m "test: define billable web backend tracking metadata"
```

---

## Task 5: Add failing tracked-row tests for `web_search` / `web_extract` / `web_crawl`

**Objective:** Record one tracked row at the real successful web-tool billing boundary.

**Files:**
- Create: `tests/tools/test_web_tools_tracked_costs.py`
- Modify: `tools/web_tools.py`
- Modify: `hermes_state.py` only if a small access helper is needed

**Step 1: Write failing tests**

Create `tests/tools/test_web_tools_tracked_costs.py`.

Test cases:
1. successful `web_search_tool(...)` on a trackable backend writes one tracked row
2. successful `web_extract_tool(...)` writes one tracked row
3. successful `web_crawl_tool(...)` writes one tracked row
4. unsupported/non-trackable backend writes no row
5. same logical operation retried uses same `record_key`

Use monkeypatches/fakes so the test does not make real provider calls.

Suggested assertions:
- `provider` matches normalized backend
- `service` is one of `web_search`, `web_extract`, `web_crawl`
- `details_json` includes enough operation context to debug

**Step 2: Run test to verify failure**

Run:
```bash
source venv/bin/activate && python -m pytest tests/tools/test_web_tools_tracked_costs.py -q
```

Expected: FAIL — tracked rows not written yet.

**Step 3: Write minimal implementation**

In `tools/web_tools.py`:
- after a successful external-provider operation, build a `CostRecordDraft`
- write it via `SessionDB.upsert_cost_record(...)`
- only do this when billing metadata is deterministic enough for the chosen backend

If access to the current `session_id` / `task_id` is awkward, add the smallest plumbing needed. Avoid broad API redesign.

**Step 4: Run test to verify pass**

Run:
```bash
source venv/bin/activate && python -m pytest tests/tools/test_web_tools_tracked_costs.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add tools/web_tools.py tests/tools/test_web_tools_tracked_costs.py
git commit -m "feat: track billable web tool costs"
```

---

## Task 6: Add failing API tests for additive `tracked_costs`

**Objective:** Extend `/api/analytics/usage` without disturbing the existing model/session totals.

**Files:**
- Modify: `tests/hermes_cli/test_web_server.py`
- Modify: `hermes_cli/web_server.py`

**Step 1: Write failing tests**

Extend the analytics endpoint tests to assert:

```python
def test_analytics_usage_includes_tracked_costs(self):
    resp = self.client.get("/api/analytics/usage?days=7")
    assert resp.status_code == 200
    data = resp.json()
    assert "tracked_costs" in data
    assert set(data["tracked_costs"].keys()) == {
        "cost_usd",
        "estimated_cost_usd",
        "actual_cost_usd",
        "tracked_session_count",
        "record_count",
        "by_provider",
        "by_service",
    }
```

Also verify:
- existing `totals.total_estimated_cost` and `totals.total_actual_cost` remain present
- empty tracked ledger returns empty tracked breakdowns cleanly

**Step 2: Run test to verify failure**

Run:
```bash
source venv/bin/activate && python -m pytest tests/hermes_cli/test_web_server.py -k analytics_usage -q
```

Expected: FAIL — `tracked_costs` absent.

**Step 3: Write minimal implementation**

In `hermes_cli/web_server.py`:
- keep existing totals logic intact
- call `db.get_tracked_cost_analytics(occurred_after=cutoff)`
- return that under `tracked_costs`

Do not rewrite existing session-total SQL.

**Step 4: Run test to verify pass**

Run:
```bash
source venv/bin/activate && python -m pytest tests/hermes_cli/test_web_server.py -k analytics_usage -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add hermes_cli/web_server.py tests/hermes_cli/test_web_server.py
git commit -m "feat: expose tracked web tool costs in analytics api"
```

---

## Task 7: Add failing TypeScript usage for `tracked_costs`

**Objective:** Update client-side analytics types before UI rendering.

**Files:**
- Modify: `web/src/lib/api.ts`
- Modify: `web/src/pages/AnalyticsPage.tsx`

**Step 1: Write failing usage**

First update `AnalyticsPage.tsx` to reference `data.tracked_costs.by_provider` and `by_service`. Let the web build fail because the types do not include those fields yet.

**Step 2: Run build to verify failure**

Run:
```bash
cd web && npm run build
```

Expected: FAIL with TypeScript complaints about missing `tracked_costs` fields.

**Step 3: Write minimal implementation**

In `web/src/lib/api.ts`, extend `AnalyticsResponse` with:

```ts
tracked_costs: {
  cost_usd: number;
  estimated_cost_usd: number;
  actual_cost_usd: number;
  tracked_session_count: number;
  record_count: number;
  by_provider: Array<{
    provider: string;
    cost_usd: number;
    record_count: number;
    session_count: number;
  }>;
  by_service: Array<{
    service: string;
    cost_usd: number;
    record_count: number;
    session_count: number;
  }>;
};
```

**Step 4: Run build to verify pass**

Run:
```bash
cd web && npm run build
```

Expected: type-level errors for `tracked_costs` are resolved.

**Step 5: Commit**

```bash
git add web/src/lib/api.ts web/src/pages/AnalyticsPage.tsx
git commit -m "feat: add tracked web tool cost response types"
```

---

## Task 8: Add failing UI tests for tracked web-tool breakdowns

**Objective:** Define the dashboard UX for additive tracked web-tool costs.

**Files:**
- Create: `web/src/pages/AnalyticsPage.test.tsx`
- Modify: `web/src/pages/AnalyticsPage.tsx`

**Step 1: Write failing tests**

Create `web/src/pages/AnalyticsPage.test.tsx` following the style used in `web/src/pages/MemoryPage.test.tsx`.

Test cases:
1. renders tracked-cost summary section when `record_count > 0`
2. renders provider breakdown table
3. renders service breakdown table
4. renders explanatory note that tracked cost includes supported services only
5. renders clean empty state when no tracked records exist

Mock `api.getAnalytics` directly.

**Step 2: Run test to verify failure**

Run:
```bash
cd web && npm test -- AnalyticsPage.test.tsx
```

Expected: FAIL — UI section does not exist yet.

**Step 3: Write minimal implementation**

In `web/src/pages/AnalyticsPage.tsx`, add a dedicated tracked-cost section with:
- summary cards
- provider table
- service table
- clear partial/tracked note
- empty state when `record_count === 0`

Keep the existing model/session analytics cards intact.

**Step 4: Run test to verify pass**

Run:
```bash
cd web && npm test -- AnalyticsPage.test.tsx
```

Expected: PASS.

**Step 5: Commit**

```bash
git add web/src/pages/AnalyticsPage.tsx web/src/pages/AnalyticsPage.test.tsx
git commit -m "feat: show tracked web tool costs in analytics page"
```

---

## Task 9: Run focused verification suites

**Objective:** Validate the new tracked web-tool slice before broad regression testing.

**Files:**
- No source change required unless failures are found

**Step 1: Run focused backend tests**

Run:
```bash
source venv/bin/activate && python -m pytest \
  tests/test_hermes_state.py \
  tests/hermes_cli/test_web_server.py \
  tests/tools/test_web_tools_config.py \
  tests/tools/test_web_tools_tracked_costs.py -q
```

**Step 2: Run focused frontend test**

Run:
```bash
cd web && npm test -- AnalyticsPage.test.tsx
```

**Step 3: Run frontend build**

Run:
```bash
cd web && npm run build
```

**Step 4: Fix regressions minimally**

If failures appear:
- confirm the failing expectation is correct
- add or adjust tests first if expectation is wrong
- otherwise make the smallest production fix
- rerun the same focused command until green

**Step 5: Commit**

```bash
git add -A
git commit -m "test: verify tracked web tool slice"
```

---

## Task 10: Run broad regression suite

**Objective:** Ensure the additive tracked web-tool work did not destabilize the rest of Hermes.

**Files:**
- No source change required unless failures are found

**Step 1: Run full backend suite**

Run:
```bash
source venv/bin/activate && python -m pytest tests/ -q
```

**Step 2: Run full frontend checks**

Run:
```bash
cd web && npm test
cd web && npm run build
```

**Step 3: Fix only real regressions**

Keep changes scoped. Do not use regression failures as an excuse to broaden the tracked-cost project.

**Step 4: Verify working tree**

Run:
```bash
git status --short
```

Expected: only intentional tracked-cost changes remain.

**Step 5: Commit**

```bash
git add -A
git commit -m "feat: complete tracked web tool cost v1"
```

---

## Notes for the implementer

### Billing-boundary rule

Only emit a tracked row where Hermes knows that a provider-billable web-tool operation succeeded and knows enough to price it reliably.

### Existing LLM rule

If you find yourself adding new LLM tracked rows in `run_agent.py` as the main v1 work, stop. That is explicitly not this plan.

### Browser rule

Browser-provider tracking is deliberately deferred. Do not pull it into v1 unless the user explicitly expands scope.

### Minimal acceptable final product

The work is complete when:
- Hermes writes tracked rows for supported billable web tools
- `/api/analytics/usage` exposes additive provider/service tracked breakdowns
- `AnalyticsPage` renders that tracked breakdown cleanly
- existing LLM/session cost behavior remains untouched
- tests cover schema, idempotency, web writes, API shape, and UI rendering

---

## Suggested execution handoff

Plan complete and saved. Ready to execute using subagent-driven-development — dispatch one fresh subagent per task, with spec-compliance review after each task and code-quality review before proceeding.