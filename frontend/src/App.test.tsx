import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

describe("ResearchAgent workbench", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const payload = url.includes("capabilities")
        ? { model: { available: true, model: "test-model" }, paperlens: { available: false, base_url: "", workspace_id: "test" }, web: { available: true, provider: "DDGS" }, persistence: { available: true } }
        : [];
      return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
  });

  it("shows the research scope and composer", async () => {
    render(<App />);
    expect(screen.getByText("把一个问题，变成", { exact: false })).toBeInTheDocument();
    expect(screen.getByPlaceholderText("输入需要深度研究的问题…")).toBeInTheDocument();
    expect(await screen.findByText("test-model")).toBeInTheDocument();
  });
});

