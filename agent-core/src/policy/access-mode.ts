import { AccessMode } from "../types.js";

export function parseAccessMode(value: unknown): AccessMode {
  if (value === "safe" || value === "project" || value === "trusted") return value;
  return "safe";
}
