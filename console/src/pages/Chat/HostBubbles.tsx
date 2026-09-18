/**
 * pages/Chat/HostBubbles.tsx — host-side wrappers around the vendor's
 * AgentScopeRuntime{Request,Response}Card components.
 *
 * Why wrappers:
 * - Plugin extensions (chat.request.render / prepend / append and the
 *   response equivalents) need a render seam SDK doesn't expose.
 * - We register HostRequestCard / HostResponseCard into options.cards so the
 *   SDK Cards dispatcher invokes them instead of the vendor defaults.
 * - The wrapper itself subscribes to the chat extension registry via hooks,
 *   so it re-renders when plugins register/dispose — no need to rebuild the
 *   parent useMemo (and avoid re-mounting bubbles on every plugin change).
 *
 * Vendor response primitives are deep-imported because the SDK does not expose
 * a message-renderer seam. If their paths change, update the imports below.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";
import VendorRequestCardOriginal from "@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/Request/Card";
import AgentScopeRuntimeResponseBuilder from "@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/Response/Builder";
import ResponseActions from "@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/Response/Actions";
import ResponseError from "@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/Response/Error";
import ResponseReasoning from "@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/Response/Reasoning";
import ResponseTool from "@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/Response/Tool";
import {
  AgentScopeRuntimeContentType,
  AgentScopeRuntimeMessageType,
  AgentScopeRuntimeRunStatus,
  type IAgentScopeRuntimeMessage,
  type IAgentScopeRuntimeResponse,
} from "@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/types";
import { useChatAnywhereOptions } from "@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/Context/ChatAnywhereOptionsContext";
import Images from "@agentscope-ai/chat/lib/DefaultCards/Images";
import Videos from "@agentscope-ai/chat/lib/DefaultCards/Videos";
import Files from "@agentscope-ai/chat/lib/DefaultCards/Files";
import Audios from "@agentscope-ai/chat/lib/DefaultCards/Audios";
import { Bubble, Markdown } from "@agentscope-ai/chat";
import { Avatar, Flex } from "antd";
import { renderableCodeComponents } from "../../components/RenderableCodeBlock";
// Vendor `.d.ts` doesn't yet describe the request content slots.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const VendorRequestCard = VendorRequestCardOriginal as React.ComponentType<any>;
import {
  useChatScalarSnapshot,
  useChatListSnapshot,
} from "../../plugins/registry/useChatExtensions";
import { ChatScalar, ChatList } from "../../plugins/registry/slotKeys";
import { PluginSlotBoundary } from "../../plugins/registry/PluginSlotBoundary";
import type {
  ChatRequestData,
  ChatResponseData,
} from "../../plugins/registry/types";

function sortByOrder<T extends { item: { order?: number } }>(arr: T[]): T[] {
  return arr
    .slice()
    .sort((a, b) => (a.item.order ?? 100) - (b.item.order ?? 100));
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyCardProps = any;

const RESPONSE_RENDER_INTERVAL_MS = 150;

function isTerminalResponse(data: IAgentScopeRuntimeResponse): boolean {
  return [
    AgentScopeRuntimeRunStatus.Completed,
    AgentScopeRuntimeRunStatus.Failed,
    AgentScopeRuntimeRunStatus.Canceled,
  ].includes(data.status);
}

const TOOL_INPUT_TYPES = new Set([
  AgentScopeRuntimeMessageType.PLUGIN_CALL,
  AgentScopeRuntimeMessageType.TOOL_CALL,
  AgentScopeRuntimeMessageType.MCP_CALL,
]);
const TOOL_OUTPUT_TYPES = new Set([
  AgentScopeRuntimeMessageType.PLUGIN_CALL_OUTPUT,
  AgentScopeRuntimeMessageType.TOOL_CALL_OUTPUT,
  AgentScopeRuntimeMessageType.MCP_CALL_OUTPUT,
]);

function toolCallKey(content: any): string | null {
  const data = content?.data;
  if (!data) return null;
  return data.call_id || data.name || null;
}

/**
 * Cache of "input message + first output content block -> merged message".
 *
 * The response builder produces a fresh output array on every streamed
 * delta, so this merge re-runs on every render tick. Re-creating merged
 * objects each run would break React.memo on every historical tool card,
 * forcing them to re-parse (JSON.parse) and re-stringify their full
 * arguments at up to ~7 renders/sec. Immer's structural sharing keeps
 * unchanged messages reference-identical, so caching by input identity
 * lets completed tool cards keep a stable `data` prop across ticks.
 */
interface ToolMergeCacheEntry {
  outputContent: unknown;
  merged: any; // eslint-disable-line @typescript-eslint/no-explicit-any
}
const toolMergeCache = new WeakMap<object, ToolMergeCacheEntry>();

export function mergeToolMessagesLinear(
  messages: IAgentScopeRuntimeResponse["output"],
) {
  const inputIndexByKey = new Map<string, number>();
  const result: typeof messages = [];

  for (const message of messages) {
    const hasContent = message.content?.length > 0;
    if (TOOL_INPUT_TYPES.has(message.type) && hasContent) {
      const key = toolCallKey(message.content[0]);
      if (key) inputIndexByKey.set(key, result.length);
      result.push(message);
    } else if (TOOL_OUTPUT_TYPES.has(message.type) && hasContent) {
      const key = toolCallKey(message.content[0]);
      const inputIdx = key != null ? inputIndexByKey.get(key) : undefined;
      if (inputIdx !== undefined) {
        const input = result[inputIdx];
        const outputContent = message.content[0];
        let merged: typeof input;
        const cached = toolMergeCache.get(input as object);
        if (cached && cached.outputContent === outputContent) {
          merged = cached.merged;
        } else {
          merged = {
            ...input,
            content: [...input.content, outputContent],
          };
          toolMergeCache.set(input as object, { outputContent, merged });
        }
        result[inputIdx] = merged;
      } else {
        result.push(message);
      }
    } else {
      result.push(message);
    }
  }

  return result;
}

/**
 * The SDK publishes one React message update for every SSE frame. Keep the
 * builder state fully up to date, but limit expensive Markdown/tool-tree
 * rendering while a long response is still streaming. Terminal frames always
 * render immediately so the final answer and actions are never delayed.
 */
function useThrottledResponseData(
  data: IAgentScopeRuntimeResponse,
): IAgentScopeRuntimeResponse {
  const [rendered, setRendered] = useState(data);
  const latestRef = useRef(data);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    latestRef.current = data;

    if (isTerminalResponse(data)) {
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      setRendered(data);
      return;
    }

    if (timerRef.current === null) {
      timerRef.current = setTimeout(() => {
        timerRef.current = null;
        setRendered(latestRef.current);
      }, RESPONSE_RENDER_INTERVAL_MS);
    }
  }, [data]);

  useEffect(
    () => () => {
      if (timerRef.current !== null) clearTimeout(timerRef.current);
    },
    [],
  );

  return rendered;
}

const HostMessage = React.memo(function HostMessage({
  data,
}: {
  data: IAgentScopeRuntimeMessage;
}) {
  const replaceMediaURL = useChatAnywhereOptions(
    (options) => options.api?.replaceMediaURL,
  );
  const onFileCardClick = useChatAnywhereOptions(
    (options) => options.api?.onFileCardClick,
  );
  const formatMediaURL = (url?: string) =>
    url ? replaceMediaURL?.(url) || url : url;

  if (!data.content?.length) return null;

  return (
    <>
      {data.content.map((item, index) => {
        switch (item.type) {
          case AgentScopeRuntimeContentType.TEXT:
            return (
              <Markdown
                key={index}
                components={renderableCodeComponents}
                content={item.text}
                cursor={item.status === AgentScopeRuntimeRunStatus.InProgress}
              />
            );
          case AgentScopeRuntimeContentType.REFUSAL:
            return <Markdown key={index} content={item.refusal} raw />;
          case AgentScopeRuntimeContentType.IMAGE:
            return (
              <Images
                key={index}
                data={[{ url: formatMediaURL(item.image_url) }]}
              />
            );
          case AgentScopeRuntimeContentType.VIDEO:
            return (
              <Videos
                key={index}
                data={[
                  {
                    poster: formatMediaURL(item.video_poster),
                    src: formatMediaURL(item.video_url) || "",
                  },
                ]}
              />
            );
          case AgentScopeRuntimeContentType.FILE:
            return (
              <Files
                key={index}
                data={[
                  {
                    name: item.file_name || item.fileName || item.file_id,
                    size: item.file_size,
                    url: formatMediaURL(item.file_url),
                  },
                ]}
                onClick={onFileCardClick}
              />
            );
          case AgentScopeRuntimeContentType.AUDIO:
            return (
              <Audios
                key={index}
                data={[
                  { src: formatMediaURL(item.audio_url || item.data) || "" },
                ]}
              />
            );
          default:
            return <div key={index}>{JSON.stringify(item)}</div>;
        }
      })}
    </>
  );
});

const DefaultHostResponseCard = React.memo(function DefaultHostResponseCard({
  data,
  isLast,
  contentPrepend,
  contentAppend,
}: {
  data: IAgentScopeRuntimeResponse;
  isLast?: boolean;
  contentPrepend?: React.ReactNode;
  contentAppend?: React.ReactNode;
}) {
  const avatar = useChatAnywhereOptions((options) => options.welcome?.avatar);
  const nick = useChatAnywhereOptions((options) => options.welcome?.nick);
  const messages = useMemo(
    () => mergeToolMessagesLinear(data.output),
    [data.output],
  );

  if (
    !messages.length &&
    AgentScopeRuntimeResponseBuilder.maybeGenerating(data)
  ) {
    return <Bubble.Spin />;
  }

  return (
    <>
      {avatar ? (
        <Flex align="center" gap={8} style={{ marginBottom: 8 }}>
          <Avatar src={avatar} />
          {nick ? <span>{nick}</span> : null}
        </Flex>
      ) : null}
      {contentPrepend}
      {messages.map((item) => {
        switch (item.type) {
          case AgentScopeRuntimeMessageType.MESSAGE:
            return <HostMessage key={item.id} data={item} />;
          case AgentScopeRuntimeMessageType.PLUGIN_CALL:
          case AgentScopeRuntimeMessageType.PLUGIN_CALL_OUTPUT:
          case AgentScopeRuntimeMessageType.TOOL_CALL:
          case AgentScopeRuntimeMessageType.TOOL_CALL_OUTPUT:
          case AgentScopeRuntimeMessageType.MCP_CALL:
          case AgentScopeRuntimeMessageType.MCP_CALL_OUTPUT:
            return <ResponseTool key={item.id} data={item} />;
          case AgentScopeRuntimeMessageType.MCP_APPROVAL_REQUEST:
            return <ResponseTool key={item.id} data={item} isApproval />;
          case AgentScopeRuntimeMessageType.REASONING:
            return <ResponseReasoning key={item.id} data={item} />;
          case AgentScopeRuntimeMessageType.ERROR:
            return <ResponseError key={item.id} data={item} />;
          case AgentScopeRuntimeMessageType.HEARTBEAT:
            return null;
          default:
            console.warn(`[WIP] Unknown message type: ${item.type}`);
            return null;
        }
      })}
      {data.error ? <ResponseError data={data.error} /> : null}
      {contentAppend}
      <ResponseActions data={data} isLast={isLast} />
    </>
  );
});

export function HostRequestCard(props: { data: ChatRequestData }) {
  const extScalar = useChatScalarSnapshot();
  const extLists = useChatListSnapshot();

  const renderEntry = extScalar[ChatScalar.requestRender];
  const renderFn = renderEntry?.value;
  const prependList = sortByOrder(extLists[ChatList.requestPrepend]);
  const appendList = sortByOrder(extLists[ChatList.requestAppend]);

  // prepend/append routed through vendor's contentPrepend/contentAppend
  // slot so actions stay last. Mirrors HostResponseCard.
  const contentPrepend =
    prependList.length === 0 ? null : (
      <>
        {prependList.map((e) => (
          <PluginSlotBoundary
            key={e.item.id}
            slot={ChatList.requestPrepend}
            pluginId={e.pluginId}
          >
            {e.item.render({ data: props.data })}
          </PluginSlotBoundary>
        ))}
      </>
    );
  const contentAppend =
    appendList.length === 0 ? null : (
      <>
        {appendList.map((e) => (
          <PluginSlotBoundary
            key={e.item.id}
            slot={ChatList.requestAppend}
            pluginId={e.pluginId}
          >
            {e.item.render({ data: props.data })}
          </PluginSlotBoundary>
        ))}
      </>
    );

  const fallback = () => (
    <VendorRequestCard
      data={props.data as AnyCardProps}
      contentPrepend={contentPrepend as AnyCardProps}
      contentAppend={contentAppend as AnyCardProps}
    />
  );

  if (renderFn) {
    return (
      <PluginSlotBoundary
        slot={ChatScalar.requestRender}
        pluginId={renderEntry!.pluginId}
        fallback={fallback()}
      >
        {renderFn({ data: props.data, fallback })}
      </PluginSlotBoundary>
    );
  }
  return fallback();
}

export function HostResponseCard(props: {
  data: ChatResponseData;
  isLast?: boolean;
}) {
  const throttledData = useThrottledResponseData(
    props.data as unknown as IAgentScopeRuntimeResponse,
  );
  const extScalar = useChatScalarSnapshot();
  const extLists = useChatListSnapshot();

  const renderEntry = extScalar[ChatScalar.responseRender];
  const renderFn = renderEntry?.value;
  const prependList = useMemo(
    () => sortByOrder(extLists[ChatList.responsePrepend]),
    [extLists],
  );
  const appendList = useMemo(
    () => sortByOrder(extLists[ChatList.responseAppend]),
    [extLists],
  );

  // prepend/append are routed through vendor's contentPrepend/contentAppend
  // slot so they land BETWEEN messages and Actions — actions always last.
  // Vendor change: see Response/Card.js DefaultResponseRender, which now
  // reads props.contentPrepend / props.contentAppend.
  const contentPrepend = useMemo(
    () =>
      prependList.length === 0 ? null : (
        <>
          {prependList.map((e) => (
            <PluginSlotBoundary
              key={e.item.id}
              slot={ChatList.responsePrepend}
              pluginId={e.pluginId}
            >
              {e.item.render({ data: throttledData as unknown as ChatResponseData, isLast: props.isLast })}
            </PluginSlotBoundary>
          ))}
        </>
      ),
    [prependList, throttledData, props.isLast],
  );
  const contentAppend = useMemo(
    () =>
      appendList.length === 0 ? null : (
        <>
          {appendList.map((e) => (
            <PluginSlotBoundary
              key={e.item.id}
              slot={ChatList.responseAppend}
              pluginId={e.pluginId}
            >
              {e.item.render({ data: throttledData as unknown as ChatResponseData, isLast: props.isLast })}
            </PluginSlotBoundary>
          ))}
        </>
      ),
    [appendList, throttledData, props.isLast],
  );

  const fallback = () => (
    <DefaultHostResponseCard
      data={throttledData}
      isLast={props.isLast}
      contentPrepend={contentPrepend}
      contentAppend={contentAppend}
    />
  );

  if (renderFn) {
    return (
      <PluginSlotBoundary
        slot={ChatScalar.responseRender}
        pluginId={renderEntry!.pluginId}
        fallback={fallback()}
      >
        {renderFn({
          data: throttledData as unknown as ChatResponseData,
          isLast: props.isLast,
          fallback,
        })}
      </PluginSlotBoundary>
    );
  }
  return fallback();
}
