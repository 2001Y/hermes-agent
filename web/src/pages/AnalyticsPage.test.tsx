import { screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AnalyticsPage from "@/pages/AnalyticsPage";
import { renderWithAppProviders } from "@/test/render";
import type { AnalyticsResponse } from "@/lib/api";

const mockApi = vi.hoisted(() => ({
  getAnalytics: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      ...actual.api,
      getAnalytics: mockApi.getAnalytics,
    },
  };
});

const baseResponse: AnalyticsResponse = {
  daily: [
    {
      day: "2026-04-16",
      input_tokens: 1200,
      output_tokens: 800,
      cache_read_tokens: 0,
      reasoning_tokens: 0,
      estimated_cost: 0.25,
      actual_cost: 0.1,
      sessions: 2,
    },
  ],
  by_model: [
    {
      model: "anthropic/claude-sonnet-4",
      input_tokens: 1200,
      output_tokens: 800,
      estimated_cost: 0.25,
      sessions: 2,
    },
  ],
  totals: {
    total_input: 1200,
    total_output: 800,
    total_cache_read: 0,
    total_reasoning: 0,
    total_estimated_cost: 0.25,
    total_actual_cost: 0.1,
    total_sessions: 2,
  },
  provider_monthly_usage: {
    sources: [
      {
        provider: "tavily",
        status: "supported",
        scope: "api_key",
        unit: "usage",
        value: 150,
        period: {
          kind: "calendar_month",
          start: "2026-04-01T00:00:00Z",
          end: "2026-04-30T23:59:59Z",
        },
        breakdown: {
          search_usage: 100,
          extract_usage: 25,
        },
        fetched_at: 1710000000,
        source: "provider_usage_api",
      },
      {
        provider: "firecrawl",
        status: "supported",
        scope: "team",
        unit: "credits",
        value: 250,
        period: {
          kind: "billing_period",
          start: "2026-04-01T00:00:00Z",
          end: "2026-04-30T23:59:59Z",
        },
        breakdown: {
          remainingCredits: 750,
          planCredits: 1000,
        },
        fetched_at: 1710000000,
        source: "provider_usage_api",
      },
    ],
    unsupported: [
      {
        provider: "parallel",
        reason: "no_documented_usage_api",
      },
    ],
  },
};

describe("AnalyticsPage provider monthly usage", () => {
  beforeEach(() => {
    mockApi.getAnalytics.mockResolvedValue(baseResponse);
  });

  it("renders provider monthly usage section with supported and unsupported providers", async () => {
    renderWithAppProviders(<AnalyticsPage />);

    expect(await screen.findByRole("heading", { name: /provider monthly usage/i })).toBeInTheDocument();
    expect(screen.getByText(/provider-managed monthly usage/i)).toBeInTheDocument();
    expect(screen.getByText("tavily")).toBeInTheDocument();
    expect(screen.getByText("firecrawl")).toBeInTheDocument();
    expect(screen.getByText("parallel")).toBeInTheDocument();
  });

  it("renders units and scope badges without forcing usd", async () => {
    renderWithAppProviders(<AnalyticsPage />);

    const section = await screen.findByTestId("provider-monthly-usage");
    expect(within(section).getByText("usage")).toBeInTheDocument();
    expect(within(section).getByText("credits")).toBeInTheDocument();
    expect(within(section).getByText("api_key")).toBeInTheDocument();
    expect(within(section).getByText("team")).toBeInTheDocument();
    expect(within(section).getByText("150")).toBeInTheDocument();
    expect(within(section).getByText("250")).toBeInTheDocument();
  });

  it("renders clean empty state when no providers are supported", async () => {
    mockApi.getAnalytics.mockResolvedValueOnce({
      ...baseResponse,
      provider_monthly_usage: {
        sources: [],
        unsupported: [{ provider: "parallel", reason: "no_documented_usage_api" }],
      },
    });

    renderWithAppProviders(<AnalyticsPage />);

    expect(await screen.findByText(/no provider monthly usage available/i)).toBeInTheDocument();
    expect(screen.getByText("parallel")).toBeInTheDocument();
  });
});
