// Mirrors the backend's pydantic models (backend/src/videre/api). Keep in sync by hand.

export interface Health {
  status: "ok";
  version: string;
  rail: "paypal" | "mock";
  demo_replay: boolean;
}
