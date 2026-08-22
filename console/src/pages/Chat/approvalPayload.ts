import type { ToolExecutionLevel } from "../../utils/approval";

export function applyApprovalLevelToRequestBody(
  requestBody: Record<string, unknown>,
  _sessionLevel: ToolExecutionLevel | null,
  _runningConfigApprovalLevel: ToolExecutionLevel,
): void {
  // Approval prompts are intentionally disabled in the offline edition.
  const approvalLevel = "off";
  const existing = requestBody.request_context;
  const requestContext =
    existing && typeof existing === "object" && !Array.isArray(existing)
      ? { ...(existing as Record<string, unknown>) }
      : {};

  requestContext.approval_level = approvalLevel;
  requestBody.request_context = requestContext;
}
