import type { Health } from "../types/api";

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`${path} returned HTTP ${res.status}`);
  return (await res.json()) as T;
}

export const getHealth = () => getJson<Health>("/api/health");
