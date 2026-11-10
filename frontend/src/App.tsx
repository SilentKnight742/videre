import { useEffect, useState } from "react";
import { getHealth } from "./api/client";
import type { Health } from "./types/api";

type State = { kind: "loading" } | { kind: "up"; health: Health } | { kind: "down"; error: string };

export default function App() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    getHealth().then(
      (health) => !cancelled && setState({ kind: "up", health }),
      (err: unknown) => !cancelled && setState({ kind: "down", error: String(err) }),
    );
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <main>
      <h1>Videre</h1>
      <p>See, and budget, what an AI agent does.</p>
      {state.kind === "loading" && <p role="status">Checking backend…</p>}
      {state.kind === "up" && (
        <p role="status">
          Backend ok · v{state.health.version} · rail: {state.health.rail}
        </p>
      )}
      {state.kind === "down" && <p role="alert">Backend unreachable: {state.error}</p>}
    </main>
  );
}
