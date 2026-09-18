/** Single token usage record (per date + provider + model). */
export interface TokenUsageRecord {
  date: string; // YYYY-MM-DD
  provider_id: string;
  model: string;
  prompt_tokens: number;
  completion_tokens: number;
  call_count: number;
}

/** Per-model (has provider_id, model) or per-date (no provider_id, model) stats. */
export interface TokenUsageStats {
  provider_id?: string;
  model?: string;
  prompt_tokens: number;
  completion_tokens: number;
  call_count: number;
}

export interface TokenUsageSummary {
  total_prompt_tokens: number;
  total_completion_tokens: number;
  total_calls: number;
  by_model: Record<string, TokenUsageStats>;
  by_date: Record<string, TokenUsageStats>;
}

/** One account's usage in the administrator view. */
export interface AccountUsage extends TokenUsageStats {
  user_id: string;
  by_date: Record<string, TokenUsageStats>;
  by_model: Record<string, TokenUsageStats>;
}

/** Per-account usage summary (administrators only). */
export interface AccountUsageSummary {
  total_prompt_tokens: number;
  total_completion_tokens: number;
  total_calls: number;
  by_user: Record<string, AccountUsage>;
  by_date: Record<string, TokenUsageStats>;
  /**
   * Usage recorded before accounts were tracked, or that could not be
   * resolved. Kept visible so the figures still reconcile.
   */
  unattributed: TokenUsageStats;
}
