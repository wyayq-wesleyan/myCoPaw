import { describe, expect, it } from "vitest";
import { mergeToolMessagesLinear } from "./HostBubbles";
import {
  AgentScopeRuntimeMessageType,
} from "@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/types";

function makeToolInput(id: string, callId: string, name?: string) {
  return {
    id,
    type: AgentScopeRuntimeMessageType.TOOL_CALL,
    role: "assistant",
    status: "completed",
    content: [
      {
        type: "data",
        delta: false,
        data: { call_id: callId, name: name ?? callId },
      },
    ],
  };
}

function makeToolOutput(id: string, callId: string, output: string) {
  return {
    id,
    type: AgentScopeRuntimeMessageType.TOOL_CALL_OUTPUT,
    role: "tool",
    status: "completed",
    content: [
      {
        type: "data",
        delta: false,
        data: { call_id: callId, output },
      },
    ],
  };
}

function makeMessage(id: string, text: string) {
  return {
    id,
    type: AgentScopeRuntimeMessageType.MESSAGE,
    role: "assistant",
    status: "completed",
    content: [{ type: "text", text, delta: false }],
  };
}

describe("mergeToolMessagesLinear", () => {
  it("returns empty array for empty input", () => {
    expect(mergeToolMessagesLinear([])).toEqual([]);
  });

  it("passes through plain messages", () => {
    const msgs = [makeMessage("m1", "hello"), makeMessage("m2", "world")];
    const result = mergeToolMessagesLinear(msgs as any);
    expect(result).toHaveLength(2);
    expect(result[0].id).toBe("m1");
    expect(result[1].id).toBe("m2");
  });

  it("merges tool input and output by call_id", () => {
    const input = makeToolInput("t1", "call-1");
    const output = makeToolOutput("t2", "call-1", "result");
    const result = mergeToolMessagesLinear([input, output] as any);
    expect(result).toHaveLength(1);
    expect(result[0].id).toBe("t1");
    expect(result[0].content).toHaveLength(2);
    expect((result[0].content[1] as any).data.output).toBe("result");
  });

  it("merges multiple tool calls independently", () => {
    const i1 = makeToolInput("t1", "call-1", "search");
    const o1 = makeToolOutput("t2", "call-1", "result-1");
    const i2 = makeToolInput("t3", "call-2", "read");
    const o2 = makeToolOutput("t4", "call-2", "result-2");
    const result = mergeToolMessagesLinear([i1, o1, i2, o2] as any);
    expect(result).toHaveLength(2);
    expect(result[0].id).toBe("t1");
    expect(result[0].content).toHaveLength(2);
    expect(result[1].id).toBe("t3");
    expect(result[1].content).toHaveLength(2);
  });

  it("handles out-of-order tool output (output before input)", () => {
    const input = makeToolInput("t1", "call-1");
    const output = makeToolOutput("t2", "call-1", "result");
    const result = mergeToolMessagesLinear([output, input] as any);
    // Output arrives before input — cannot merge, so both appear standalone.
    expect(result).toHaveLength(2);
  });

  it("merges interleaved tool calls correctly", () => {
    const i1 = makeToolInput("t1", "call-1");
    const i2 = makeToolInput("t2", "call-2");
    const o1 = makeToolOutput("t3", "call-1", "r1");
    const o2 = makeToolOutput("t4", "call-2", "r2");
    const result = mergeToolMessagesLinear([i1, i2, o1, o2] as any);
    expect(result).toHaveLength(2);
    // i1 gets o1 appended, i2 gets o2 appended
    expect(result[0].id).toBe("t1");
    expect(result[0].content).toHaveLength(2);
    expect((result[0].content[1] as any).data.output).toBe("r1");
    expect(result[1].id).toBe("t2");
    expect(result[1].content).toHaveLength(2);
    expect((result[1].content[1] as any).data.output).toBe("r2");
  });

  it("preserves messages between tool calls", () => {
    const i1 = makeToolInput("t1", "call-1");
    const m1 = makeMessage("m1", "thinking...");
    const o1 = makeToolOutput("t2", "call-1", "result");
    const m2 = makeMessage("m2", "done");
    const result = mergeToolMessagesLinear([i1, m1, o1, m2] as any);
    expect(result).toHaveLength(3);
    expect(result[0].id).toBe("t1");
    expect(result[0].content).toHaveLength(2);
    expect(result[1].id).toBe("m1");
    expect(result[2].id).toBe("m2");
  });

  it("handles 50 tool calls in O(N) without quadratic blowup", () => {
    const msgs: any[] = [];
    for (let i = 0; i < 50; i++) {
      msgs.push(makeToolInput(`i${i}`, `call-${i}`));
      msgs.push(makeToolOutput(`o${i}`, `call-${i}`, `result-${i}`));
    }
    const start = performance.now();
    const result = mergeToolMessagesLinear(msgs);
    const elapsed = performance.now() - start;
    expect(result).toHaveLength(50);
    expect(elapsed).toBeLessThan(10);
    // Verify last entry merged correctly
    expect(result[49].content).toHaveLength(2);
    expect((result[49].content[1] as any).data.output).toBe("result-49");
  });

  it("handles tool output with no matching input (orphan output)", () => {
    const output = makeToolOutput("t1", "call-1", "result");
    const result = mergeToolMessagesLinear([output] as any);
    expect(result).toHaveLength(1);
    expect(result[0].id).toBe("t1");
  });

  it("handles tool input with no content gracefully", () => {
    const inputNoContent = {
      id: "t1",
      type: AgentScopeRuntimeMessageType.TOOL_CALL,
      role: "assistant",
      status: "completed",
      content: [],
    };
    const result = mergeToolMessagesLinear([inputNoContent] as any);
    expect(result).toHaveLength(1);
  });

  it("preserves merged object identity across repeated merge runs", () => {
    // The response builder emits a fresh output array on every streamed
    // delta. Re-merging the same (reference-stable) messages must return
    // the SAME merged objects so React.memo on tool cards holds.
    const input = makeToolInput("t1", "call-1");
    const output = makeToolOutput("t2", "call-1", "result");
    const msgs = [input, output];

    const first = mergeToolMessagesLinear(msgs as any);
    const second = mergeToolMessagesLinear(msgs as any);
    const third = mergeToolMessagesLinear(msgs as any);

    expect(second[0]).toBe(first[0]);
    expect(third[0]).toBe(first[0]);
    // Content arrays keep identity too (no per-run re-creation).
    expect(third[0].content).toBe(first[0].content);
  });

  it("preserves identity for interleaved multi-tool runs", () => {
    const i1 = makeToolInput("t1", "call-1");
    const o1 = makeToolOutput("t2", "call-1", "r1");
    const i2 = makeToolInput("t3", "call-2");
    const o2 = makeToolOutput("t4", "call-2", "r2");
    const msgs = [i1, i2, o1, o2];

    const first = mergeToolMessagesLinear(msgs as any);
    const second = mergeToolMessagesLinear(msgs as any);

    expect(second[0]).toBe(first[0]);
    expect(second[1]).toBe(first[1]);
  });

  it("re-merges with new identity when the output content changes", () => {
    const input = makeToolInput("t1", "call-1");
    const output1 = makeToolOutput("t2", "call-1", "r1");
    const msgs1 = [input, output1];
    const first = mergeToolMessagesLinear(msgs1 as any);

    // A new output object for the same call must produce a new merge.
    const output2 = makeToolOutput("t2", "call-1", "r2");
    const msgs2 = [input, output2];
    const second = mergeToolMessagesLinear(msgs2 as any);

    expect(second[0]).not.toBe(first[0]);
    expect((second[0].content[1] as any).data.output).toBe("r2");
  });

  it("handles a second output chunk appended to an already merged call", () => {
    const input = makeToolInput("t1", "call-1");
    const o1 = makeToolOutput("t2", "call-1", "chunk1");
    const o2 = makeToolOutput("t3", "call-1", "chunk2");
    const msgs = [input, o1, o2];

    const first = mergeToolMessagesLinear(msgs as any);
    const second = mergeToolMessagesLinear(msgs as any);

    expect(first[0].content).toHaveLength(3); // input + 2 outputs
    expect(second[0]).toBe(first[0]);
    expect((second[0].content[2] as any).data.output).toBe("chunk2");
  });
});
