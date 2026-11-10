import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import App from "./App";

afterEach(cleanup);

test("shows the backend status from /api/health", async () => {
  const fetchMock = vi.fn(async () =>
    Response.json({ status: "ok", version: "0.1.0", rail: "mock", demo_replay: false }),
  );
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);

  const status = await screen.findByText(/Backend ok/);
  expect(status.textContent).toContain("rail: mock");
  expect(fetchMock).toHaveBeenCalledWith("/api/health", expect.anything());
});

test("reports an unreachable backend", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response("nope", { status: 502 })),
  );

  render(<App />);

  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toContain("HTTP 502");
});
