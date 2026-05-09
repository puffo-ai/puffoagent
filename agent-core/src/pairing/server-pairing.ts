import crypto from "node:crypto";
import os from "node:os";
import { DAEMON_VERSION, DEFAULT_SERVER_URL } from "../config/defaults.js";
import { CoreNative, DeviceStatus, PairingStatus } from "../native/core.js";
import { isValidCoreSlug } from "../platform/core-slug.js";
import { normalizePairingConfirmInput } from "../platform/pairing-confirm.js";
import { isValidPairingId } from "../platform/pairing-id.js";
import { isValidPairingPublicNonce } from "../platform/pairing-nonce.js";
import { normalizePairingAuthToken } from "../platform/pairing-token.js";
import { CreatedLocalAccessGrant, StateStore } from "../state/store.js";

export interface PairingGateway {
  startPairing(input: Record<string, unknown>, context?: PairingRequestContext): Promise<PairingStatus>;
  pollPairing(pairingId: string): Promise<PairingStatus>;
  confirmPairing(input: Record<string, unknown>): Promise<DeviceStatus>;
}

export interface PairingRequestContext {
  localApiOrigin?: string;
}

export interface ServerPairingGatewayOptions {
  serverUrl?: string;
  requestTimeoutMs?: number;
}

interface ServerStartPairingResponse {
  pairingId: string;
  userCode: string;
  confirmUrl: string;
  expiresAt: string;
  pollAfterMs: number;
}

interface ServerPollPairingResponse {
  status: "pending" | "confirmed" | "expired" | "canceled";
  expiresAt: string;
  pollAfterMs?: number;
  operatorSlug?: string;
  accountId?: string;
  authToken?: string;
  operatorBootstrap?: Record<string, unknown>;
  localWebGrant?: unknown;
}

interface ServerLocalWebGrant {
  mode: "daemon_mints";
  ttlMs: number;
}

const MIN_LOCAL_WEB_GRANT_TTL_MS = 1_000;
const MAX_LOCAL_WEB_GRANT_TTL_MS = 15 * 60 * 1_000;
const MIN_POLL_AFTER_MS = 250;
const MAX_POLL_AFTER_MS = 60_000;
const MAX_SERVER_PAIRING_RESPONSE_BYTES = 64 * 1024;

export class ServerPairingGateway implements PairingGateway {
  private readonly serverUrl: string;
  private readonly requestTimeoutMs: number;

  constructor(
    private readonly core: CoreNative,
    private readonly store: StateStore,
    options: ServerPairingGatewayOptions = {},
  ) {
    this.serverUrl = normalizeServerUrl(options.serverUrl || process.env.AGENT_CORE_SERVER_URL || DEFAULT_SERVER_URL);
    this.requestTimeoutMs = options.requestTimeoutMs ?? 10_000;
  }

  async startPairing(input: Record<string, unknown>, context: PairingRequestContext = {}): Promise<PairingStatus> {
    const startInput = readStartPairingInput(input);
    const body = {
      pairingPublicNonce: startInput.pairingPublicNonce,
      daemonVersion: DAEMON_VERSION,
      platform: process.platform,
      arch: process.arch || os.arch(),
      localApiOrigin: context.localApiOrigin,
    };
    const response = readStartPairingResponse(await this.requestJson("/agent-core/pairings", {
      method: "POST",
      body,
      expectedStatus: 201,
    }));
    return {
      status: "pending",
      pairingId: response.pairingId,
      userCode: response.userCode,
      confirmUrl: response.confirmUrl,
      expiresAt: response.expiresAt,
      pollAfterMs: response.pollAfterMs,
    };
  }

  async pollPairing(pairingId: string): Promise<PairingStatus> {
    if (!isValidPairingId(pairingId)) throw new Error("pairingId must be a safe identifier");
    const response = readPollPairingResponse(await this.requestJson(
      `/agent-core/pairings/${encodeURIComponent(pairingId)}`,
      { method: "GET", expectedStatus: 200 },
    ));
    if (response.status !== "confirmed") {
      return {
        status: response.status,
        pairingId,
        expiresAt: response.expiresAt,
        ...(response.pollAfterMs !== undefined ? { pollAfterMs: response.pollAfterMs } : {}),
      };
    }

    const nativeStatus = response.authToken
      ? await this.core.confirmPairing({ authToken: response.authToken, pairingId })
      : undefined;
    const localWebGrant = normalizeLocalWebGrant(response.localWebGrant);
    const binding = await this.store.setDeviceBinding({
      accountId: response.accountId!,
      operatorSlug: response.operatorSlug!,
      ...(nativeStatus?.deviceId ? { deviceId: nativeStatus.deviceId } : {}),
      pairingId,
    }, { clearLocalGrants: Boolean(response.authToken) });
    const localGrant = response.authToken ? await this.mintLocalGrant(localWebGrant, binding) : undefined;
    return {
      status: "confirmed",
      pairingId,
      expiresAt: response.expiresAt,
      operatorSlug: binding.operatorSlug,
      accountId: binding.accountId,
      ...(response.operatorBootstrap ? { operatorBootstrap: publicOperatorBootstrap(response.operatorBootstrap) } : {}),
      ...(localGrant && localWebGrant ? { localWebGrant } : {}),
      ...(localGrant ? { localGrant } : {}),
      ...(nativeStatus ? { core: nativeStatus } : {}),
    };
  }

  async confirmPairing(input: Record<string, unknown>): Promise<DeviceStatus> {
    return this.core.confirmPairing(normalizePairingConfirmInput(input));
  }

  private async mintLocalGrant(
    localWebGrant: ServerLocalWebGrant | undefined,
    binding: { accountId: string; operatorSlug: string },
  ): Promise<CreatedLocalAccessGrant | undefined> {
    if (!localWebGrant) return undefined;
    return this.store.createLocalAccessGrant({
      scopes: ["management"],
      ttlMs: localWebGrant.ttlMs,
      binding,
    });
  }

  private async requestJson(
    path: string,
    options: { method: string; expectedStatus: number; body?: Record<string, unknown> },
  ): Promise<unknown> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.requestTimeoutMs);
    try {
      const response = await fetch(`${this.serverUrl}${path}`, {
        method: options.method,
        redirect: "error",
        signal: controller.signal,
        ...(options.body
          ? {
              headers: { "Accept": "application/json", "Content-Type": "application/json" },
              body: JSON.stringify(options.body),
            }
          : { headers: { "Accept": "application/json" } }),
      });
      if (response.status !== options.expectedStatus) {
        throw new Error(`server pairing request failed with HTTP ${response.status}`);
      }
      return await readBoundedJsonResponse(response);
    } catch (error) {
      if (error instanceof Error && error.name === "AbortError") {
        throw new Error(`server pairing request timed out after ${this.requestTimeoutMs}ms`);
      }
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  }
}

async function readBoundedJsonResponse(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type");
  if (!isJsonContentType(contentType)) {
    throw new Error("server pairing response had unsupported content type");
  }

  const contentLength = response.headers.get("content-length");
  if (contentLength !== null) {
    const length = Number(contentLength);
    if (!Number.isFinite(length) || length > MAX_SERVER_PAIRING_RESPONSE_BYTES) {
      throw new Error("server pairing response was too large");
    }
  }

  const reader = response.body?.getReader();
  if (!reader) {
    const text = await response.text();
    if (Buffer.byteLength(text, "utf8") > MAX_SERVER_PAIRING_RESPONSE_BYTES) {
      throw new Error("server pairing response was too large");
    }
    return parseJsonResponse(text);
  }

  const chunks: Uint8Array[] = [];
  let total = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    if (!value) continue;
    total += value.byteLength;
    if (total > MAX_SERVER_PAIRING_RESPONSE_BYTES) {
      await reader.cancel().catch(() => undefined);
      throw new Error("server pairing response was too large");
    }
    chunks.push(value);
  }
  return parseJsonResponse(Buffer.concat(chunks, total).toString("utf8"));
}

function isJsonContentType(value: string | null): boolean {
  if (!value) return false;
  const mediaType = value.split(";", 1)[0]?.trim().toLowerCase();
  return mediaType === "application/json" || Boolean(mediaType?.endsWith("+json"));
}

function normalizeServerUrl(value: string): string {
  const raw = value.trim();
  if (!raw) throw new Error("serverUrl is required");
  try {
    const parsed = new URL(raw);
    if (parsed.protocol !== "https:" && !(parsed.protocol === "http:" && isLoopbackUrlHostname(parsed.hostname))) {
      throw new Error("serverUrl must use HTTPS or loopback HTTP");
    }
    if (parsed.username || parsed.password) throw new Error("serverUrl must not include credentials");
    if (parsed.search || parsed.hash) throw new Error("serverUrl must not include query or fragment");
  } catch (error) {
    if (error instanceof Error && error.message.startsWith("serverUrl ")) throw error;
    throw new Error("serverUrl must be a valid URL");
  }
  return raw.replace(/\/+$/, "");
}

function parseJsonResponse(text: string): unknown {
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new Error("server pairing response was invalid JSON");
  }
}

function randomNonce(): string {
  return crypto.randomBytes(32).toString("base64url");
}

function readStartPairingInput(input: Record<string, unknown>): { pairingPublicNonce: string } {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new Error("pairing start body must be an object");
  }
  const unsupported = Object.keys(input).find((key) => key !== "pairingPublicNonce");
  if (unsupported) throw new Error(`${unsupported} is not supported for pairing start`);
  return { pairingPublicNonce: readPairingPublicNonce(input.pairingPublicNonce) };
}

function readPairingPublicNonce(value: unknown): string {
  if (value === undefined) return randomNonce();
  if (!isValidPairingPublicNonce(value)) {
    throw new Error("pairingPublicNonce must be an unpadded base64url string from 32 to 128 characters");
  }
  return value;
}

function readString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function readStartPairingResponse(value: unknown): ServerStartPairingResponse {
  const input = readRecord(value);
  rejectUnsupportedFields(input, ["pairingId", "userCode", "confirmUrl", "expiresAt", "pollAfterMs"], "response");
  return {
    pairingId: requiredPairingId(input.pairingId, "pairingId"),
    userCode: requiredBoundedString(input.userCode, "userCode", 64),
    confirmUrl: requiredHttpUrl(input.confirmUrl, "confirmUrl"),
    expiresAt: requiredTimestamp(input.expiresAt, "expiresAt"),
    pollAfterMs: requiredPollAfterMs(input.pollAfterMs, "pollAfterMs"),
  };
}

function readPollPairingResponse(value: unknown): ServerPollPairingResponse {
  const input = readRecord(value);
  const status = requiredString(input.status, "status");
  if (status !== "pending" && status !== "confirmed" && status !== "expired" && status !== "canceled") {
    throw invalidResponse("status");
  }
  if (status !== "confirmed") {
    rejectUnsupportedFields(input, ["status", "expiresAt", "pollAfterMs"], "response");
  }
  if (status === "confirmed") {
    rejectUnsupportedFields(input, [
      "status",
      "expiresAt",
      "pollAfterMs",
      "operatorSlug",
      "accountId",
      "authToken",
      "operatorBootstrap",
      "localWebGrant",
    ], "response");
  }
  const operatorSlug = input.operatorSlug !== undefined
    ? requiredCoreSlug(input.operatorSlug, "operatorSlug")
    : undefined;
  const accountId = input.accountId !== undefined ? requiredBoundedString(input.accountId, "accountId", 256) : undefined;
  const operatorBootstrap = input.operatorBootstrap !== undefined
    ? readOperatorBootstrap(input.operatorBootstrap, operatorSlug)
    : undefined;
  if (status === "confirmed") {
    if (!operatorSlug) throw invalidResponse("operatorSlug");
    if (!accountId) throw invalidResponse("accountId");
  }
  return {
    status,
    expiresAt: requiredTimestamp(input.expiresAt, "expiresAt"),
    ...(input.pollAfterMs !== undefined
      ? { pollAfterMs: requiredPollAfterMs(input.pollAfterMs, "pollAfterMs") }
      : {}),
    ...(operatorSlug !== undefined ? { operatorSlug } : {}),
    ...(accountId !== undefined ? { accountId } : {}),
    ...(input.authToken !== undefined ? { authToken: requiredPairingAuthToken(input.authToken, "authToken") } : {}),
    ...(operatorBootstrap !== undefined ? { operatorBootstrap } : {}),
    ...(input.localWebGrant !== undefined ? { localWebGrant: input.localWebGrant } : {}),
  };
}

function normalizeLocalWebGrant(value: unknown): ServerLocalWebGrant | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const input = value as { mode?: unknown; ttlMs?: unknown };
  if (input.mode !== "daemon_mints") return undefined;
  if (
    typeof input.ttlMs !== "number" ||
    !Number.isInteger(input.ttlMs) ||
    input.ttlMs < MIN_LOCAL_WEB_GRANT_TTL_MS ||
    input.ttlMs > MAX_LOCAL_WEB_GRANT_TTL_MS
  ) {
    return undefined;
  }
  return { mode: "daemon_mints", ttlMs: input.ttlMs };
}

function readRecord(value: unknown, field = "response"): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw invalidResponse(field);
  return value as Record<string, unknown>;
}

function requiredString(value: unknown, field: string): string {
  if (typeof value !== "string" || !value.trim()) throw invalidResponse(field);
  return value.trim();
}

function requiredBoundedString(value: unknown, field: string, maxLength: number): string {
  const text = requiredString(value, field);
  if (text.length > maxLength) throw invalidResponse(field);
  return text;
}

function requiredPairingId(value: unknown, field: string): string {
  const pairingId = requiredString(value, field);
  if (!isValidPairingId(pairingId)) throw invalidResponse(field);
  return pairingId;
}

function requiredHttpUrl(value: unknown, field: string): string {
  const url = requiredBoundedString(value, field, 2048);
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:" && !(parsed.protocol === "http:" && isLoopbackUrlHostname(parsed.hostname))) {
      throw invalidResponse(field);
    }
    if (parsed.username || parsed.password) throw invalidResponse(field);
  } catch {
    throw invalidResponse(field);
  }
  return url;
}

function isLoopbackUrlHostname(hostname: string): boolean {
  const normalized = hostname.toLowerCase();
  return normalized === "localhost" || normalized === "127.0.0.1" || normalized === "::1" || normalized === "[::1]";
}

function requiredTimestamp(value: unknown, field: string): string {
  const timestamp = requiredString(value, field);
  if (!Number.isFinite(Date.parse(timestamp))) throw invalidResponse(field);
  return timestamp;
}

function requiredPollAfterMs(value: unknown, field: string): number {
  const delay = requiredPositiveInteger(value, field);
  if (delay < MIN_POLL_AFTER_MS || delay > MAX_POLL_AFTER_MS) throw invalidResponse(field);
  return delay;
}

function requiredPairingAuthToken(value: unknown, field: string): string {
  const token = normalizePairingAuthToken(value);
  if (!token) throw invalidResponse(field);
  return token;
}

function requiredCoreSlug(value: unknown, field: string): string {
  const slug = requiredString(value, field);
  if (!isValidCoreSlug(slug)) throw invalidResponse(field);
  return slug;
}

function readOperatorBootstrap(value: unknown, topLevelOperatorSlug: string | undefined): Record<string, unknown> {
  const bootstrap = readRecord(value, "operatorBootstrap");
  const kind = requiredString(bootstrap.kind, "operatorBootstrap.kind");
  const operatorSlug = requiredCoreSlug(bootstrap.operatorSlug, "operatorBootstrap.operatorSlug");
  if (topLevelOperatorSlug && operatorSlug !== topLevelOperatorSlug) {
    throw invalidResponse("operatorBootstrap.operatorSlug");
  }
  if (kind === "existing_local_identity") {
    rejectUnsupportedFields(bootstrap, ["kind", "operatorSlug"], "operatorBootstrap");
    return { kind, operatorSlug };
  }
  if (kind === "restore_or_enroll") {
    rejectUnsupportedFields(bootstrap, ["kind", "operatorSlug", "payload"], "operatorBootstrap");
    return {
      kind,
      operatorSlug,
      payload: bootstrap.payload !== undefined ? readRecord(bootstrap.payload, "operatorBootstrap.payload") : {},
    };
  }
  throw invalidResponse("operatorBootstrap.kind");
}

function publicOperatorBootstrap(value: Record<string, unknown>): Record<string, unknown> {
  const kind = requiredString(value.kind, "operatorBootstrap.kind");
  const operatorSlug = requiredCoreSlug(value.operatorSlug, "operatorBootstrap.operatorSlug");
  return { kind, operatorSlug };
}

function rejectUnsupportedFields(input: Record<string, unknown>, allowed: string[], field: string): void {
  const allowedSet = new Set(allowed);
  const unsupported = Object.keys(input).find((key) => !allowedSet.has(key));
  if (unsupported) throw invalidResponse(`${field}.${unsupported}`);
}

function requiredPositiveInteger(value: unknown, field: string): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < 1) throw invalidResponse(field);
  return value;
}

function invalidResponse(field: string): Error {
  return new Error(`server pairing response was invalid: ${field}`);
}
