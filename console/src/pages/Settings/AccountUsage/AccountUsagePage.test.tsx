/**
 * Account usage page — rendering and selection behaviour.
 *
 * The chart library and the page's layout providers are mocked so the test
 * asserts on the page's own logic: which accounts appear, how totals react to
 * filtering, and that the unattributed bucket is surfaced.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/test/common_setup";
import AccountUsagePage from "./index";

const mockGetAccountUsage = vi.fn();

vi.mock("../../../api", () => ({
  default: {
    getAccountUsage: (...args: unknown[]) => mockGetAccountUsage(...args),
  },
}));

vi.mock("../../../contexts/ThemeContext", () => ({
  useTheme: () => ({ isDark: false }),
}));

vi.mock("../../../hooks/useAppMessage", () => ({
  useAppMessage: () => ({ message: { error: vi.fn() } }),
}));

vi.mock("@/components/PageHeader", () => ({
  PageHeader: ({ current }: { current: string }) => <div>{current}</div>,
}));

// The chart is out of scope here; assert on the data feeding it instead.
vi.mock("@ant-design/plots", () => ({
  Column: ({ data }: { data: Array<Record<string, unknown>> }) => (
    <div data-testid="chart">
      {data.map((row) => `${row.account}:${row.value}`).join(",")}
    </div>
  ),
}));

vi.mock("../TokenUsage/components", () => ({
  LoadingState: ({ message }: { message: string }) => <div>{message}</div>,
}));

const SUMMARY = {
  total_prompt_tokens: 1300,
  total_completion_tokens: 130,
  total_calls: 6,
  by_user: {
    alice: {
      user_id: "alice",
      prompt_tokens: 100,
      completion_tokens: 10,
      call_count: 2,
      by_date: {},
      by_model: {},
    },
    bob: {
      user_id: "bob",
      prompt_tokens: 200,
      completion_tokens: 20,
      call_count: 3,
      by_date: {},
      by_model: {},
    },
    carol: {
      user_id: "carol",
      prompt_tokens: 1000,
      completion_tokens: 100,
      call_count: 1,
      by_date: {},
      by_model: {},
    },
  },
  by_date: {},
  unattributed: { prompt_tokens: 0, completion_tokens: 0, call_count: 0 },
};

describe("AccountUsagePage", () => {
  beforeEach(() => {
    mockGetAccountUsage.mockReset();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("lists every account by default, heaviest first", async () => {
    mockGetAccountUsage.mockResolvedValue(SUMMARY);

    renderWithProviders(<AccountUsagePage />);

    await waitFor(() => {
      expect(screen.getByText("carol")).toBeInTheDocument();
    });
    expect(screen.getByText("alice")).toBeInTheDocument();
    expect(screen.getByText("bob")).toBeInTheDocument();

    // Chart receives all three, ordered by total tokens (carol > bob > alice).
    const chart = screen.getByTestId("chart");
    expect(chart.textContent).toBe("carol:1100,bob:220,alice:110");
  });

  it("shows the combined totals across accounts", async () => {
    mockGetAccountUsage.mockResolvedValue(SUMMARY);

    renderWithProviders(<AccountUsagePage />);

    // 1300 prompt + 130 completion = 1430 total tokens, 6 calls.
    await waitFor(() => {
      expect(screen.getByText("1.4K")).toBeInTheDocument();
    });
    expect(screen.getByText("6")).toBeInTheDocument();
  });

  it("requests the current date range from the API", async () => {
    mockGetAccountUsage.mockResolvedValue(SUMMARY);

    renderWithProviders(<AccountUsagePage />);

    await waitFor(() => {
      expect(mockGetAccountUsage).toHaveBeenCalledTimes(1);
    });
    const params = mockGetAccountUsage.mock.calls[0][0];
    expect(params.start_date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(params.end_date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });

  it("surfaces unattributed usage as its own row", async () => {
    mockGetAccountUsage.mockResolvedValue({
      ...SUMMARY,
      by_user: { alice: SUMMARY.by_user.alice },
      unattributed: {
        prompt_tokens: 500,
        completion_tokens: 50,
        call_count: 4,
      },
    });

    renderWithProviders(<AccountUsagePage />);

    // No i18n instance is loaded in tests, so t() resolves to the key.
    await waitFor(() => {
      expect(
        screen.getByText("accountUsage.unattributed"),
      ).toBeInTheDocument();
    });
    // The bucket ranks first (550 tokens) ahead of alice (110).
    expect(screen.getByTestId("chart").textContent).toBe(
      "accountUsage.unattributed:550,alice:110",
    );
  });

  it("removing an account drops it from the chart and the totals", async () => {
    mockGetAccountUsage.mockResolvedValue(SUMMARY);
    const user = userEvent.setup();

    renderWithProviders(<AccountUsagePage />);
    await waitFor(() => {
      expect(screen.getByTestId("chart").textContent).toContain("carol:1100");
    });

    // Unselect carol via the account picker.
    const picker = document.querySelector(".ant-select-selector");
    expect(picker).not.toBeNull();
    await user.click(picker as Element);

    const option = await screen.findByTitle("carol");
    await user.click(option);

    await waitFor(() => {
      const chart = screen.getByTestId("chart");
      expect(chart.textContent).toBe("bob:220,alice:110");
    });
    // Remaining totals: 300 prompt + 30 completion = 330, 5 calls.
    expect(screen.getByText("330")).toBeInTheDocument();
    expect(screen.getByText("5")).toBeInTheDocument();
  });
});
