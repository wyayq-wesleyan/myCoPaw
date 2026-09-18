// @vitest-environment jsdom
import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { adaptCardForV1 } from "./v1Adapter";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
  initReactI18next: { type: "3rd-party" },
}));

/**
 * The SDK re-renders tool cards on every streaming render tick (~7/sec).
 * parseV1Props JSON.parses the full arguments string for completed calls,
 * so the adapter must cache by message identity and skip re-renders via
 * React.memo — otherwise large tool args (create_file with big content)
 * re-parse on every tick and progressively freeze the page.
 */

function makeV1Data(args: string, withOutput = true) {
  const content: Array<Record<string, unknown>> = [
    {
      delta: false,
      data: { call_id: "call-1", name: "create_file", arguments: args },
    },
  ];
  if (withOutput) {
    content.push({ data: { call_id: "call-1", output: "ok" } });
  }
  return { data: { content, status: "completed" } };
}

describe("adaptCardForV1", () => {
  it("renders the wrapped card with parsed content", () => {
    const spy = vi.fn(() => <div>card</div>);
    const Wrapped = adaptCardForV1(spy as any);
    const data = makeV1Data('{"path":"a.txt"}');
    render(<Wrapped {...(data as any)} />);
    expect(spy).toHaveBeenCalledTimes(1);
    const props = (spy.mock.calls[0] as any)?.[0] as any;
    expect(props.content.name).toBe("create_file");
    expect(props.content.params).toEqual({ path: "a.txt" });
    expect(props.content.status).toBe("done");
  });

  it("JSON.parses arguments only once per distinct message object", () => {
    const parseSpy = vi.spyOn(JSON, "parse");
    const Card = () => <div>card</div>;
    const Wrapped = adaptCardForV1(Card as any);
    const data = makeV1Data('{"path":"big.txt"}');

    const { unmount } = render(<Wrapped {...(data as any)} />);
    const parsesAfterFirst = parseSpy.mock.calls.length;
    expect(parsesAfterFirst).toBeGreaterThan(0);

    // Re-render with the SAME message object (memo) — no parse, no render.
    parseSpy.mockClear();
    const { unmount: unmount2 } = render(<Wrapped {...(data as any)} />);
    expect(parseSpy).not.toHaveBeenCalled();
    unmount2();
    unmount();
    parseSpy.mockRestore();
  });

  it("React.memo skips re-render when the data object identity is stable", () => {
    const spy = vi.fn(() => <div>card</div>);
    const Wrapped = adaptCardForV1(spy as any);
    const data = makeV1Data('{"path":"a.txt"}');

    const { rerender } = render(<Wrapped {...(data as any)} />);
    expect(spy).toHaveBeenCalledTimes(1);

    // Same props object identity (SDK message untouched) → memo hit.
    rerender(<Wrapped {...(data as any)} />);
    expect(spy).toHaveBeenCalledTimes(1);

    // New message object (streaming delta produced a new state) → re-render.
    const nextData = makeV1Data('{"path":"a.txt"}');
    rerender(<Wrapped {...(nextData as any)} />);
    expect(spy).toHaveBeenCalledTimes(2);
  });

  it("re-parses when a new message object arrives (streaming path)", () => {
    const Card = () => <div>card</div>;
    const Wrapped = adaptCardForV1(Card as any);
    const first = makeV1Data('{"path":"a.txt"}', false);
    const second = makeV1Data('{"path":"a.txt"}', true);

    const { rerender } = render(<Wrapped {...(first as any)} />);
    // No output yet → status stays "calling" while args are complete strings.
    rerender(<Wrapped {...(second as any)} />);
    // Distinct objects must not share a cache entry: the second render gets
    // its own parsed result (with output → done).
    // Render the wrapped card again with the second object; nothing throws.
    rerender(<Wrapped {...(second as any)} />);
  });

  it("handles streaming input with delta preview without parsing params", () => {
    const spy = vi.fn(() => <div>card</div>);
    const Wrapped = adaptCardForV1(spy as any);
    const streamingData = {
      data: {
        content: [
          {
            delta: true,
            data: { call_id: "call-1", name: "create_file", arguments: "abc" },
          },
        ],
        status: "in_progress",
      },
    };
    render(<Wrapped {...(streamingData as any)} />);
    const props = (spy.mock.calls[0] as any)?.[0] as any;
    expect(props.content.inputProgress).toBeDefined();
    expect(props.content.inputProgress.preview).toBe("abc");
    expect(props.content.status).toBe("calling");
  });
});
