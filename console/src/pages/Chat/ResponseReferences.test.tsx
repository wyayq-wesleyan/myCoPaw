// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

import { ResponseReferences } from "./ResponseReferences";

const references = Array.from({ length: 5 }, (_, index) => ({
  kind: "knowledge",
  name: `文件${index + 1}.pdf`,
}));

describe("ResponseReferences", () => {
  it("shows three references and expands to all", () => {
    render(<ResponseReferences references={references} />);

    expect(screen.getAllByRole("listitem")).toHaveLength(3);
    expect(screen.queryByText("文件5.pdf")).toBeNull();

    fireEvent.click(screen.getByText("chat.references.more"));

    expect(screen.getAllByRole("listitem")).toHaveLength(5);
    expect(screen.getByText("文件5.pdf")).toBeTruthy();

    fireEvent.click(screen.getByText("chat.references.less"));
    expect(screen.getAllByRole("listitem")).toHaveLength(3);
  });

  it("renders nothing without references", () => {
    const { container } = render(<ResponseReferences references={[]} />);
    expect(container.firstChild).toBeNull();
  });
});
