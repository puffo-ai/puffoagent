import crypto from "node:crypto";
import { IncomingMessage } from "node:http";

const PUBLIC_PAIRING_POLL_ROUTE = /^\/pairing\/[^/]+$/;

export function isLoopbackHost(host: string | undefined): boolean {
  if (!host) return true;
  const normalized = normalizeHost(host);
  if (!normalized) return false;
  return (
    normalized === "localhost" ||
    normalized === "127.0.0.1" ||
    normalized === "::1"
  );
}

export function isPublicRoute(method: string | undefined, path: string): boolean {
  return (
    method === "OPTIONS" ||
    (method === "GET" && path === "/health") ||
    (method === "GET" && path === "/v1/info") ||
    (method === "GET" && path === "/configuration") ||
    (method === "GET" && path === "/providers") ||
    (method === "POST" && path === "/pairing/start") ||
    (method === "GET" && PUBLIC_PAIRING_POLL_ROUTE.test(path))
  );
}

export function requestHasToken(req: IncomingMessage, token: string): boolean {
  return requestAuthTokens(req).some((presented) => timingSafeEqual(presented, token));
}

export function requestAuthToken(req: IncomingMessage): string | undefined {
  return requestAuthTokens(req)[0];
}

export function requestAuthTokens(req: IncomingMessage): string[] {
  const tokens = [
    bearerToken(req.headers.authorization),
    ...headerTokens(req.headers["x-agent-core-token"]),
  ].filter((token): token is string => Boolean(token));
  return [...new Set(tokens)];
}

function bearerToken(value: string | undefined): string | undefined {
  if (!value) return undefined;
  const match = value.match(/^Bearer\s+(.+)$/i);
  return match?.[1]?.trim();
}

function headerTokens(value: string | string[] | undefined): string[] {
  const rawValues = Array.isArray(value) ? value : [value];
  return rawValues
    .map((raw) => raw?.trim())
    .filter((token): token is string => Boolean(token));
}

function timingSafeEqual(left: string, right: string): boolean {
  const leftBuffer = Buffer.from(left);
  const rightBuffer = Buffer.from(right);
  if (leftBuffer.length !== rightBuffer.length) return false;
  return crypto.timingSafeEqual(leftBuffer, rightBuffer);
}

function normalizeHost(host: string): string | undefined {
  const value = host.trim().toLowerCase();
  if (!value) return undefined;
  if (value.startsWith("[")) {
    const match = value.match(/^\[([^\]]+)\](?::(\d+))?$/);
    if (!match) return undefined;
    if (!validOptionalPort(match[2])) return undefined;
    return match[1];
  }
  if (value === "::1") return "::1";
  if (value.includes(":")) {
    const parts = value.split(":");
    if (parts.length !== 2) return undefined;
    if (!validOptionalPort(parts[1])) return undefined;
    return parts[0] || undefined;
  }
  return value;
}

function validOptionalPort(value: string | undefined): boolean {
  if (value === undefined) return true;
  if (!/^\d+$/.test(value)) return false;
  const port = Number(value);
  return Number.isSafeInteger(port) && port >= 1 && port <= 65_535;
}
