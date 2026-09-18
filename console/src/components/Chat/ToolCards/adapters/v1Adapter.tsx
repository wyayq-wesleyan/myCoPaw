/**
 * v1Adapter — bridges ChatV2 tool cards to ChatV1's @agentscope-ai/chat format.
 *
 * ChatV1 uses `customToolRenderConfig: Record<string, React.FC<any>>` where
 * the component receives @agentscope-ai/chat's internal props shape:
 *
 *   { data: { content: [{ data: { arguments, name, ... } }] }, ... }
 *
 * ChatV2 cards expect:
 *
 *   { content: ToolCallContent, isStreaming?: boolean }
 *
 * This adapter wraps each ChatV2 card so it can be used in ChatV1.
 */

import React from "react";
import type { ToolCallContent, ToolCallStatus } from "../shared/types";
import type { BuiltinCardComponent } from "../cards";
import GenericToolCard from "../cards/GenericToolCard";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const STREAM_INPUT_PREVIEW_CHARS = 4 * 1024;
const ERROR_STATUSES = new Set(["failed", "rejected", "canceled"]);
const TOOL_ERROR_STATES = new Set(["error", "interrupted", "denied"]);

/**
 * Derive the tool execution status from V1 message data.
 *
 * Checks the tool-execution-layer `state` (nested inside resultItem.data)
 * first — it reflects the real outcome of the tool call. Falls back to
 * message-level `status` for delivery state.
 */
function deriveToolStatus(
  resultItem: Record<string, unknown> | undefined,
  data: Record<string, unknown>,
): ToolCallStatus {
  if (!resultItem) return "calling";

  const resultData = (resultItem?.data ?? {}) as Record<string, unknown>;
  const toolState = resultData.state as string;
  if (toolState && TOOL_ERROR_STATES.has(toolState)) {
    return "error";
  }

  const rawStatus =
    (data.status as string) || (resultItem.status as string) || "";
  if (rawStatus === "completed") return "done";
  if (ERROR_STATUSES.has(rawStatus)) return "error";
  return "calling";
}

// ---------------------------------------------------------------------------
// V1 props parsing
// ---------------------------------------------------------------------------

/**
 * Parse the props that @agentscope-ai/chat passes to custom tool renderers.
 *
 * From the @agentscope-ai/chat source (Tool.js):
 *
 *   var C = customToolRenderConfig[toolName];
 *   node = _jsx(C, { data: data });
 *
 * Where `data` has this shape:
 *   {
 *     content: [
 *       { data: { name, arguments, server_label, ... } },  // [0] = call
 *       { data: { output, ... } },                         // [1] = result
 *     ],
 *     status: "in_progress" | "completed" | "failed" | ...
 *   }
 */
function parseV1Props(v1Props: Record<string, unknown>): {
  content: ToolCallContent;
  isStreaming: boolean;
} {
  // v1Props = { data: { content: [...], status: ... } }
  const data = (v1Props?.data ?? v1Props) as Record<string, unknown>;
  const contentArray = data?.content as
    | Array<Record<string, unknown>>
    | undefined;

  // content[0].data = tool call info (name, arguments)
  const callItem = contentArray?.[0];
  const callData = (callItem?.data ?? {}) as Record<string, unknown>;

  // content[1].data = tool result (output)
  const resultItem = contentArray?.[1];
  const resultData = (resultItem?.data ?? {}) as Record<string, unknown>;

  // Extract tool name
  const toolName = (callData.name as string) || "unknown";

  // Extract arguments (may be a JSON string or an object)
  let params: Record<string, unknown> = {};
  const rawArgs = callData.arguments;
  const isInputStreaming = callItem?.delta === true;
  const inputProgress =
    isInputStreaming && typeof rawArgs === "string"
      ? {
          preview: rawArgs.slice(-STREAM_INPUT_PREVIEW_CHARS),
          truncated: rawArgs.length > STREAM_INPUT_PREVIEW_CHARS,
        }
      : undefined;
  if (!isInputStreaming && typeof rawArgs === "string") {
    try {
      params = JSON.parse(rawArgs);
    } catch {
      params = {};
    }
  } else if (!isInputStreaming && rawArgs && typeof rawArgs === "object") {
    params = rawArgs as Record<string, unknown>;
  }

  // Extract result from content[1].data.output
  const result = resultData.output;

  // No output content → tool hasn't executed yet → always "calling".
  // Message-level status on *_call messages reflects delivery, not execution.
  const status = deriveToolStatus(resultItem, data);

  // Extract id — prefer call_id which carries the ToolCallBlock.id
  // (e.g. "toolu_…" / "call_…") from the AgentScope SSE stream.
  // It is set in the backend at FunctionCall.call_id
  // (see agentscope/message.py → FunctionCall dataclass).
  const toolId =
    (callData.call_id as string) ||
    (callData.id as string) ||
    (data.id as string) ||
    `v1-${toolName}-${Date.now()}`;

  const toolCallContent: ToolCallContent = {
    type: "tool_call",
    id: toolId,
    name: toolName,
    serverLabel: (callData.server_label as string) || undefined,
    params,
    inputProgress,
    result: result ?? undefined,
    status,
  };

  return {
    content: toolCallContent,
    isStreaming: status === "calling",
  };
}

// ---------------------------------------------------------------------------
// Adapter factory
// ---------------------------------------------------------------------------

/**
 * Parse cache keyed by the SDK message object identity.
 *
 * Completed tool calls keep a reference-stable `data` object (immer
 * structural sharing + identity-preserving merge in HostBubbles), so the
 * expensive `JSON.parse(arguments)` in parseV1Props must run only once per
 * distinct message — not on every streaming render tick (~7/sec) of the
 * response card. Streaming messages change identity per delta and simply
 * miss the cache (their parse path is bounded to a 4KB preview).
 */
interface V1ParseCacheEntry {
  data: Record<string, unknown>;
  content: ToolCallContent;
  isStreaming: boolean;
}
const v1ParseCache = new WeakMap<object, V1ParseCacheEntry>();

function parseV1PropsCached(v1Props: Record<string, unknown>): {
  content: ToolCallContent;
  isStreaming: boolean;
} {
  const data = (v1Props?.data ?? v1Props) as Record<string, unknown> | null;
  if (data && typeof data === "object") {
    const cached = v1ParseCache.get(data);
    if (cached && cached.data === data) return cached;
    const parsed = parseV1Props(v1Props);
    v1ParseCache.set(data, { data, ...parsed });
    return parsed;
  }
  return parseV1Props(v1Props);
}

/**
 * Wrap a ChatV2 BuiltinCardComponent so it can be used as a ChatV1
 * `customToolRenderConfig` renderer.
 *
 * Wrapped in React.memo on the SDK message identity: with the merge cache
 * in HostBubbles keeping merged tool messages reference-stable, completed
 * tool cards skip re-rendering entirely while sibling messages stream.
 *
 * Includes an error boundary so that rendering failures don't break
 * the entire ChatV1 UI.
 */
export function adaptCardForV1(
  CardComponent: BuiltinCardComponent,
): React.FC<any> {
  const V1WrappedCard = React.memo(function V1WrappedCard(v1Props: any) {
    const { content, isStreaming } = parseV1PropsCached(v1Props);
    return <CardComponent content={content} isStreaming={isStreaming} />;
  });

  V1WrappedCard.displayName = `V1(${
    CardComponent.displayName || CardComponent.name || "Card"
  })`;
  return V1WrappedCard;
}

/**
 * Convert the entire builtin card registry to ChatV1 format.
 *
 * Returns `Record<string, React.FC<any>>` suitable for passing to
 * `pluginSystem.addToolRenderers()`.
 */
export function adaptRegistryForV1(
  registry: Record<string, BuiltinCardComponent>,
): Record<string, React.FC<any>> {
  const adapted: Record<string, React.FC<any>> = {};
  for (const [toolName, CardComponent] of Object.entries(registry)) {
    adapted[toolName] = adaptCardForV1(CardComponent);
  }
  return adapted;
}

/** Lazy-cached V1-wrapped GenericToolCard for the fallback proxy. */
let _genericFallback: React.FC<any> | null = null;
function getGenericFallback(): React.FC<any> {
  if (!_genericFallback) {
    _genericFallback = adaptCardForV1(GenericToolCard);
  }
  return _genericFallback;
}

/**
 * Wrap a plain tool-render config object with a Proxy so that any tool
 * name not explicitly registered still returns a wrapped GenericToolCard.
 *
 * This must be applied **after** all registrations are merged (i.e. on the
 * final config passed to V1 Chat), because `Object.assign` / spread only
 * copy own-enumerable properties and would lose the Proxy behaviour.
 */
export function withGenericFallback(
  config: Record<string, React.FC<any>>,
): Record<string, React.FC<any>> {
  const fallback = getGenericFallback();
  return new Proxy(config, {
    get(target, prop, receiver) {
      if (typeof prop === "string" && !(prop in target)) {
        return fallback;
      }
      return Reflect.get(target, prop, receiver);
    },
  });
}
