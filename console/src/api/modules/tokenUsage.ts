import { request } from "../request";
import type {
  AccountUsageSummary,
  TokenUsageSummary,
  TokenUsageRecord,
} from "../types/tokenUsage";

export interface GetTokenUsageParams {
  start_date: string;
  end_date: string;
  model?: string;
  provider?: string;
}

export interface GetAccountUsageParams {
  start_date: string;
  end_date: string;
  /** Optional account allowlist; omit to request every account. */
  accounts?: string[];
}

function buildQuery(params: GetTokenUsageParams): string {
  const search = new URLSearchParams({
    start_date: params.start_date,
    end_date: params.end_date,
  });
  if (params.model) search.set("model", params.model);
  if (params.provider) search.set("provider", params.provider);
  return `?${search.toString()}`;
}

export const tokenUsageApi = {
  // Original summary endpoint (backend aggregation)
  getTokenUsage: (params: GetTokenUsageParams) =>
    request<TokenUsageSummary>(`/token-usage${buildQuery(params)}`),

  // New details endpoint (raw records for frontend aggregation)
  getTokenUsageDetails: (params: GetTokenUsageParams) =>
    request<TokenUsageRecord[]>(`/token-usage/details${buildQuery(params)}`),

  /**
   * Per-account usage for administrators. Always fetched unfiltered so the
   * account picker can list every account that has usage in the range;
   * narrowing happens client-side for an instant response.
   */
  getAccountUsage: (params: GetAccountUsageParams) => {
    const search = new URLSearchParams({
      start_date: params.start_date,
      end_date: params.end_date,
    });
    if (params.accounts?.length) {
      search.set("accounts", params.accounts.join(","));
    }
    return request<AccountUsageSummary>(
      `/token-usage/accounts?${search.toString()}`,
    );
  },
};
