import http, { IncomingMessage, ServerResponse } from "node:http";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { DAEMON_VERSION, DEFAULT_API_HOST, DEFAULT_API_PORT } from "../config/defaults.js";
import { ProviderDetector } from "../doctor/provider-detector.js";
import { LogStore } from "../logs/log-store.js";
import { redact } from "../logs/redact.js";
import { CoreNative } from "../native/core.js";
import { PairingGateway } from "../pairing/server-pairing.js";
import { isSafeAgentId } from "../platform/agent-id.js";
import { normalizeDeclaredOperatorPublicKey } from "../platform/core-identity.js";
import { isValidCoreSlug } from "../platform/core-slug.js";
import { normalizePairingConfirmInput } from "../platform/pairing-confirm.js";
import { isValidPairingId } from "../platform/pairing-id.js";
import { isValidPairingPublicNonce } from "../platform/pairing-nonce.js";
import { isValidDeniedTool, normalizeDeniedTools } from "../policy/denied-tools.js";
import { fileAccessHasEntries, mergeFileAccess, normalizeFileAccess } from "../policy/file-access.js";
import {
  mergeProviderConfigPaths,
  normalizeProviderConfigPaths,
  isValidProviderConfigPathForProvider,
} from "../policy/provider-config-paths.js";
import { parseNetworkAccess } from "../policy/network-access.js";
import { AgentNotFoundError, PolicyResolutionError, RuntimeManager } from "../runtime/runtime-manager.js";
import { DeviceBinding, RequestBindingInput, StateStore } from "../state/store.js";
import { AgentConfig, AgentCreateInput, AgentPolicyUpdateInput, EnvironmentReport, ProviderCheck } from "../types.js";
import { isLoopbackHost, isPublicRoute, requestAuthTokens, requestHasToken } from "./auth.js";
import { ApiHttpError, errorBody } from "./errors.js";

export interface ApiServerDeps {
  store: StateStore;
  detector: ProviderDetector;
  runtime: RuntimeManager;
  logs: LogStore;
  core: CoreNative;
  pairing?: PairingGateway;
  apiToken?: string;
  allowedOrigins?: readonly string[];
  allowUnauthenticatedManagement?: boolean;
  devRoutes?: boolean;
  instanceId?: string;
}

const MAX_JSON_BODY_BYTES = 1024 * 1024;
const DEFAULT_LOCAL_GRANT_TTL_MS = 15 * 60 * 1000;
const MAX_LOCAL_GRANT_TTL_MS = 24 * 60 * 60 * 1000;
const DEFAULT_PAIRING_BROWSER_ORIGINS = [
  "https://chat.puffo.ai",
  "https://app.puffo.ai",
  "http://localhost:5173",
  "http://127.0.0.1:5173",
  "http://localhost:3000",
  "http://127.0.0.1:3000",
];

export class ApiServer {
  private server: http.Server | undefined;
  private apiToken: string | undefined;
  private readonly allowedOrigins: readonly string[] | undefined;

  constructor(private readonly deps: ApiServerDeps) {
    this.apiToken = deps.apiToken;
    this.allowedOrigins = normalizeAllowedOrigins(
      deps.allowedOrigins ?? parseAllowedOriginsEnv(process.env.AGENT_CORE_ALLOWED_ORIGINS),
      deps.allowedOrigins ? "allowedOrigins" : "AGENT_CORE_ALLOWED_ORIGINS",
    );
  }

  async listen(port = DEFAULT_API_PORT, host = DEFAULT_API_HOST): Promise<http.Server> {
    this.server = http.createServer((req, res) => {
      void this.handle(req, res);
    });
    try {
      await new Promise<void>((resolve, reject) => {
        const server = this.server;
        if (!server) return reject(new Error("api server was not initialized"));
        const onError = (error: Error) => {
          server.off("listening", onListening);
          reject(error);
        };
        const onListening = () => {
          server.off("error", onError);
          resolve();
        };
        server.once("error", onError);
        server.once("listening", onListening);
        server.listen(port, host);
      });
    } catch (error) {
      this.server = undefined;
      throw error;
    }
    return this.server;
  }

  async close(): Promise<void> {
    if (!this.server) return;
    await new Promise<void>((resolve, reject) => {
      this.server?.close((error) => (error ? reject(error) : resolve()));
    });
    this.server = undefined;
  }

  private async handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
    const originAllowed = setCors(req, res, this.allowedOrigins);
    if (!isLoopbackHost(req.headers.host)) return sendJson(res, 403, errorBody("forbidden", "loopback only"));
    if (!originAllowed) return sendJson(res, 403, errorBody("forbidden", "origin not allowed"));
    if (req.method === "OPTIONS") return sendJson(res, 204, {});

    try {
      const url = new URL(req.url ?? "/", "http://127.0.0.1");
      const path = url.pathname;
      if (!this.pairingBrowserOriginAllowed(req, path)) {
        res.removeHeader("Access-Control-Allow-Origin");
        return sendJson(res, 403, errorBody("forbidden", "pairing origin not allowed"));
      }
      if (!(await this.authorized(req, path))) {
        return sendJson(res, 401, errorBody("unauthorized", "missing or invalid local authorization token"));
      }
      if (!isPublicRoute(req.method, path) && await this.requestAccountBindingMismatch(req)) {
        return sendJson(
          res,
          409,
          errorBody(
            "account_mismatch",
            "local daemon is configured for a different account; re-pair this machine for the current Web account",
          ),
        );
      }

      if (req.method === "GET" && path === "/health") {
        const binding = await this.deps.store.readDeviceBinding();
        const body: Record<string, unknown> = {
          ok: true,
          version: DAEMON_VERSION,
          authRequired: this.managementAuthRequired(),
          ...(binding ? { binding: publicDeviceBinding(binding) } : {}),
        };
        if (this.deps.instanceId) body.instanceId = this.deps.instanceId;
        if (await this.requestHasPrivateAccess(req)) {
          body.stateHome = this.deps.store.paths.root;
        }
        return sendJson(res, 200, body);
      }

      if (req.method === "GET" && path === "/v1/info") {
        const binding = await this.deps.store.readDeviceBinding();
        const agents = await this.deps.runtime.listAgents();
        return sendJson(res, 200, {
          service: "puffo-agent-bridge",
          version: "v1",
          daemon_version: DAEMON_VERSION,
          runtime: "agent-core",
          pid: process.pid,
          hostname: os.hostname(),
          agent_count: agents.length,
          paired: Boolean(binding),
          paired_slug: binding?.operatorSlug ?? null,
          paired_device_id: binding?.deviceId ?? null,
        });
      }

      if (req.method === "GET" && path === "/configuration") {
        const binding = await this.deps.store.readDeviceBinding();
        const current = readCurrentAccountQuery(url.searchParams);
        const state = configurationState(binding, current);
        return sendJson(res, 200, {
          daemonAvailable: true,
          state,
          configured: state === "configured_for_current_account",
          ...(binding ? { binding: publicDeviceBinding(binding) } : {}),
          ...(current.accountId || current.operatorSlug ? { current } : {}),
        });
      }

      if (req.method === "GET" && path === "/providers") {
        const report = await this.deps.detector.detect();
        return sendJson(res, 200, (await this.requestHasPrivateAccess(req)) ? report : redactProviderPaths(report));
      }

      if (req.method === "GET" && path === "/diagnostics") {
        const binding = await this.deps.store.readDeviceBinding();
        return sendJson(res, 200, {
          health: { ok: true, version: DAEMON_VERSION },
          core: await this.coreStatus(),
          ...(binding ? { binding: publicDeviceBinding(binding) } : {}),
          environment: await this.deps.detector.detect(),
          agents: await this.deps.runtime.listAgents(),
        });
      }

      if (req.method === "GET" && path === "/local-grants") {
        if (!this.requestHasControlToken(req)) {
          return sendJson(res, 401, errorBody("unauthorized", "missing or invalid local control token"));
        }
        return sendJson(res, 200, { grants: await this.deps.store.listLocalAccessGrants() });
      }

      if (req.method === "POST" && path === "/local-grants") {
        if (!this.requestHasControlToken(req)) {
          return sendJson(res, 401, errorBody("unauthorized", "missing or invalid local control token"));
        }
        const input = await readJson<Record<string, unknown>>(req);
        validateLocalGrantCreate(input);
        const grant = await this.deps.store.createLocalAccessGrant({
          scopes: ["management"],
          ttlMs: validateLocalGrantTtl(input.ttlMs),
        });
        return sendJson(res, 201, grant);
      }

      if (req.method === "POST" && path === "/local-control-token/rotate") {
        if (!this.requestHasControlToken(req)) {
          return sendJson(res, 401, errorBody("unauthorized", "missing or invalid local control token"));
        }
        validateEmptyJsonBody(await readJson<Record<string, unknown>>(req), "local control token rotation");
        const token = await this.deps.store.rotateLocalControlToken();
        this.apiToken = token;
        return sendJson(res, 200, { token, rotated: true, grantsRevoked: true });
      }

      const localGrantMatch = path.match(/^\/local-grants\/([^/]+)$/);
      if (localGrantMatch && req.method === "DELETE") {
        if (!this.requestHasControlToken(req)) {
          return sendJson(res, 401, errorBody("unauthorized", "missing or invalid local control token"));
        }
        const id = readLocalGrantId(localGrantMatch[1]);
        if (!id) return sendJson(res, 404, errorBody("not_found", "route not found"));
        validateEmptyJsonBody(await readJson<Record<string, unknown>>(req), "local grant revoke");
        const revoked = await this.deps.store.revokeLocalAccessGrant(id);
        if (!revoked) return sendJson(res, 404, errorBody("not_found", "local grant not found"));
        return sendJson(res, 200, { id, revoked: true });
      }

      if (req.method === "POST" && path === "/pairing/start") {
        const input = await readJson<Record<string, unknown>>(req);
        validatePairingStart(input);
        if (this.deps.pairing) {
          const origin = localApiOrigin(req);
          return sendJson(
            res,
            200,
            await this.deps.pairing.startPairing(input, origin ? { localApiOrigin: origin } : {}),
          );
        }
        return sendJson(res, 200, await this.deps.core.startPairing(input));
      }

      const pairingPoll = path.match(/^\/pairing\/([^/]+)$/);
      if (pairingPoll && req.method === "GET") {
        const pairing = this.deps.pairing;
        if (!pairing) return sendJson(res, 404, errorBody("not_found", "route not found"));
        const pairingId = pairingPoll[1];
        if (!pairingId) throw new ApiHttpError(400, "bad_request", "pairing id is required");
        return sendJson(res, 200, await pairing.pollPairing(readPairingId(pairingId)));
      }

      if (req.method === "POST" && path === "/pairing/confirm") {
        const input = await readJson<Record<string, unknown>>(req);
        return sendJson(res, 200, await (this.deps.pairing ?? this.deps.core).confirmPairing(validatePairingConfirm(input)));
      }

      if (req.method === "GET" && path === "/agents") {
        return sendJson(res, 200, { agents: await this.deps.runtime.listAgents() });
      }

      if (req.method === "POST" && path === "/agents/preview") {
        const input = await readJson<AgentCreateInput>(req);
        await validateCreateAgent(input);
        return sendJson(res, 200, await this.deps.runtime.previewCreateAgent(input));
      }

      if (req.method === "POST" && path === "/agents") {
        const input = await readJson<AgentCreateInput>(req);
        await validateCreateAgent(input, { canCreateCoreIdentity: Boolean(this.deps.core.createAgentIdentity) });
        if (input.operatorSlug !== undefined && input.coreIdentity === undefined) {
          await requireCoreIdentityCreationReady(this.deps.core);
        }
        return sendJson(res, 201, await this.deps.runtime.createAgent(input));
      }

      const agentDetail = path.match(/^\/agents\/([^/]+)$/);
      if (agentDetail && (req.method === "GET" || req.method === "DELETE")) {
        const id = readAgentId(agentDetail[1]);
        if (!id) return sendJson(res, 404, errorBody("not_found", "route not found"));
        if (req.method === "DELETE") {
          validateEmptyJsonBody(await readJson<Record<string, unknown>>(req), "agent delete");
          return sendJson(res, 200, await this.deps.runtime.deleteAgent(id));
        }
        const agent = await this.deps.runtime.getAgent(id);
        if (!agent) return sendJson(res, 404, errorBody("not_found", "agent not found"));
        return sendJson(res, 200, agent);
      }

      const policyDetail = path.match(/^\/agents\/([^/]+)\/policy$/);
      if (policyDetail && req.method === "GET") {
        const id = readAgentId(policyDetail[1]);
        if (!id) return sendJson(res, 404, errorBody("not_found", "route not found"));
        return sendJson(res, 200, await this.deps.runtime.getAgentPolicy(id));
      }

      const agentAction = path.match(/^\/agents\/([^/]+)\/([^/]+)$/);
      if (agentAction && req.method === "POST") {
        const [, rawId, action] = agentAction;
        const id = readAgentId(rawId);
        if (!id || !action) return sendJson(res, 404, errorBody("not_found", "route not found"));
        if (action === "start") {
          await validateAgentActionBody(req, "agent start");
          const agent = await this.deps.runtime.getAgent(id);
          if (!agent) return sendJson(res, 404, errorBody("not_found", "agent not found"));
          requireCoreIdentityForApiStart(agent);
          return sendJson(res, 200, await this.deps.runtime.startAgent(id));
        }
        if (action === "stop") {
          await validateAgentActionBody(req, "agent stop");
          return sendJson(res, 200, await this.deps.runtime.stopAgent(id));
        }
        if (action === "restart") {
          await validateAgentActionBody(req, "agent restart");
          const agent = await this.deps.runtime.getAgent(id);
          if (!agent) return sendJson(res, 404, errorBody("not_found", "agent not found"));
          requireCoreIdentityForApiStart(agent);
          return sendJson(res, 200, await this.deps.runtime.restartAgent(id));
        }
        if (action === "reset-session") {
          await validateAgentActionBody(req, "agent reset-session");
          return sendJson(res, 200, await this.deps.runtime.resetSession(id));
        }
        if (action === "recheck") {
          await validateAgentActionBody(req, "agent recheck");
          const agent = await this.deps.runtime.getAgent(id);
          if (!agent) return sendJson(res, 404, errorBody("not_found", "agent not found"));
          return sendJson(res, 200, { provider: await this.deps.detector.detectOne(agent.provider) });
        }
        if (action === "policy") {
          const agent = await this.deps.runtime.getAgent(id);
          if (!agent) return sendJson(res, 404, errorBody("not_found", "agent not found"));
          const input = await readJson<AgentPolicyUpdateInput>(req);
          await validatePolicyUpdate(input, agent);
          if (readPolicyPreviewFlag(url.searchParams)) {
            return sendJson(res, 200, await this.deps.runtime.previewAgentPolicy(id, input));
          }
          if (agent.status === "running") requireCoreIdentityForApiStart(agent);
          return sendJson(res, 200, await this.deps.runtime.updateAgentPolicy(id, input));
        }
        if (action === "dev-inject") {
          if (!this.devRoutesEnabled()) {
            return sendJson(res, 404, errorBody("not_found", "route not found"));
          }
          const input = await readJson<Record<string, unknown>>(req);
          validateDevInjectInput(input);
          if (typeof input.body !== "string" || !input.body.trim()) {
            throw new ApiHttpError(400, "bad_request", "dev inject body is required");
          }
          return sendJson(
            res,
            200,
            await this.deps.runtime.devInjectMessage(id, {
              body: input.body,
              ...(typeof input.senderSlug === "string" && input.senderSlug.trim()
                ? { senderSlug: input.senderSlug }
                : {}),
            }),
          );
        }
      }

      const statusMatch = path.match(/^\/agents\/([^/]+)\/status$/);
      if (statusMatch && req.method === "GET") {
        const id = readAgentId(statusMatch[1]);
        if (!id) return sendJson(res, 404, errorBody("not_found", "route not found"));
        return sendJson(res, 200, await this.deps.runtime.getAgentStatus(id));
      }

      const logsMatch = path.match(/^\/agents\/([^/]+)\/logs$/);
      if (logsMatch && req.method === "GET") {
        const id = readAgentId(logsMatch[1]);
        if (!id) return sendJson(res, 404, errorBody("not_found", "route not found"));
        const agent = await this.deps.runtime.getAgent(id);
        if (!agent) return sendJson(res, 404, errorBody("not_found", "agent not found"));
        return sendJson(res, 200, { lines: await this.deps.logs.tail(id, readLogLineLimit(url.searchParams)) });
      }

      return sendJson(res, 404, errorBody("not_found", "route not found"));
    } catch (error) {
      if (error instanceof ApiHttpError) {
        return sendJson(res, error.status, errorBody(error.code, error.message));
      }
      if (error instanceof AgentNotFoundError) {
        return sendJson(res, 404, errorBody("not_found", "agent not found"));
      }
      if (error instanceof PolicyResolutionError) {
        return sendJson(res, 400, errorBody("bad_request", error.message));
      }
      const message = redact(error instanceof Error ? error.message : String(error));
      return sendJson(res, 500, errorBody("internal_error", message));
    }
  }

  private devRoutesEnabled(): boolean {
    return this.deps.devRoutes === true || process.env.AGENT_CORE_DEV_ROUTES === "1";
  }

  private async coreStatus() {
    try {
      return await this.deps.core.openOrCreateDevice({});
    } catch (error) {
      return {
        connected: false,
        status: "unavailable",
        reason: redact(error instanceof Error ? error.message : String(error)),
        blockedBy: ["native_core_error"],
        nextAction: "Run agent doctor and inspect diagnostics; native core failed before returning a structured status.",
      };
    }
  }

  private async authorized(req: IncomingMessage, path: string): Promise<boolean> {
    if (isPublicRoute(req.method, path)) return true;
    if (this.apiToken && requestHasToken(req, this.apiToken)) return true;
    if (await this.requestHasLocalGrant(req)) return true;
    return this.deps.allowUnauthenticatedManagement === true;
  }

  private async requestHasPrivateAccess(req: IncomingMessage): Promise<boolean> {
    if (this.apiToken && requestHasToken(req, this.apiToken)) return true;
    if (await this.requestHasLocalGrant(req)) return true;
    return this.deps.allowUnauthenticatedManagement === true;
  }

  private async requestHasLocalGrant(req: IncomingMessage): Promise<boolean> {
    const binding = requestAccountBinding(req);
    for (const token of requestAuthTokens(req)) {
      if (await this.deps.store.verifyLocalAccessGrant(token, "management", binding)) return true;
    }
    return false;
  }

  private requestHasControlToken(req: IncomingMessage): boolean {
    return Boolean(this.apiToken && requestHasToken(req, this.apiToken));
  }

  private managementAuthRequired(): boolean {
    return Boolean(this.apiToken) || this.deps.allowUnauthenticatedManagement !== true;
  }

  private pairingBrowserOriginAllowed(req: IncomingMessage, path: string): boolean {
    if (!isPublicPairingRoute(req.method, path)) return true;
    const origin = normalizedRequestOrigin(req);
    if (!origin) return true;
    return (this.allowedOrigins ?? DEFAULT_PAIRING_BROWSER_ORIGINS).includes(origin);
  }

  private async requestAccountBindingMismatch(req: IncomingMessage): Promise<boolean> {
    const presented = requestAccountBinding(req);
    if (!presented.accountId && !presented.operatorSlug) return false;
    const binding = await this.deps.store.readDeviceBinding();
    if (!binding) return false;
    return !bindingMatchesRequest(binding, presented);
  }
}

function redactProviderPaths(report: EnvironmentReport): EnvironmentReport {
  return {
    ...report,
    sandbox: redactSandboxPath(report.sandbox),
    providers: Object.fromEntries(
      Object.entries(report.providers).map(([provider, check]) => [provider, redactProviderCheckPath(check)]),
    ) as EnvironmentReport["providers"],
  };
}

function redactSandboxPath(sandbox: EnvironmentReport["sandbox"]): EnvironmentReport["sandbox"] {
  if (!sandbox.path) return sandbox;
  const { path: _path, ...redacted } = sandbox;
  return redacted;
}

function redactProviderCheckPath(check: ProviderCheck): ProviderCheck {
  if (!check.path) return check;
  const { path: _path, ...redacted } = check;
  return redacted;
}

function readAgentId(id: string | undefined): string | undefined {
  return isSafeAgentId(id) ? id : undefined;
}

function readLocalGrantId(id: string | undefined): string | undefined {
  return typeof id === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(id)
    ? id
    : undefined;
}

function readLogLineLimit(params: URLSearchParams): number | undefined {
  rejectUnsupportedQueryParams(params, ["maxLines"], "agent logs");
  const raw = readOptionalQueryParam(params, "maxLines");
  if (raw === undefined) return undefined;
  if (!/^\d+$/.test(raw)) throw new ApiHttpError(400, "bad_request", "maxLines must be an integer from 0 to 1000");
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value < 0 || value > 1000) {
    throw new ApiHttpError(400, "bad_request", "maxLines must be an integer from 0 to 1000");
  }
  return value;
}

function readCurrentAccountQuery(params: URLSearchParams): RequestBindingInput {
  rejectUnsupportedQueryParams(params, ["accountId", "operatorSlug"], "configuration");
  const accountId = readOptionalAccountId(readOptionalQueryParam(params, "accountId"), "accountId");
  const operatorSlug = readOptionalOperatorSlug(readOptionalQueryParam(params, "operatorSlug"), "operatorSlug");
  return {
    ...(accountId ? { accountId } : {}),
    ...(operatorSlug ? { operatorSlug } : {}),
  };
}

function rejectUnsupportedQueryParams(params: URLSearchParams, allowed: readonly string[], label: string): void {
  const allowedSet = new Set(allowed);
  for (const key of params.keys()) {
    if (!allowedSet.has(key)) throw new ApiHttpError(400, "bad_request", `${key} is not supported for ${label}`);
  }
}

function readOptionalQueryParam(params: URLSearchParams, key: string): string | undefined {
  const values = params.getAll(key);
  if (values.length === 0) return undefined;
  if (values.length > 1) throw new ApiHttpError(400, "bad_request", `${key} must be provided at most once`);
  return values[0] || undefined;
}

function configurationState(
  binding: DeviceBinding | undefined,
  current: RequestBindingInput,
): "not_configured" | "configured_for_existing_account" | "configured_for_current_account" | "configured_for_different_account" {
  if (!binding) return "not_configured";
  if (!current.accountId && !current.operatorSlug) return "configured_for_existing_account";
  return bindingMatchesRequest(binding, current)
    ? "configured_for_current_account"
    : "configured_for_different_account";
}

function publicDeviceBinding(binding: DeviceBinding): Record<string, string> {
  return {
    accountId: binding.accountId,
    operatorSlug: binding.operatorSlug,
    pairedAt: binding.pairedAt,
    ...(binding.deviceId ? { deviceId: binding.deviceId } : {}),
    ...(binding.pairingId ? { pairingId: binding.pairingId } : {}),
  };
}

function requestAccountBinding(req: IncomingMessage): RequestBindingInput {
  const accountId = readOptionalAccountId(
    readOptionalHeader(req.headers["x-agent-core-account-id"], "X-Agent-Core-Account-Id"),
    "X-Agent-Core-Account-Id",
  );
  const operatorSlug = readOptionalOperatorSlug(
    readOptionalHeader(req.headers["x-agent-core-operator-slug"], "X-Agent-Core-Operator-Slug"),
    "X-Agent-Core-Operator-Slug",
  );
  return {
    ...(accountId ? { accountId } : {}),
    ...(operatorSlug ? { operatorSlug } : {}),
  };
}

function bindingMatchesRequest(binding: DeviceBinding, current: RequestBindingInput): boolean {
  if (!current.accountId && !current.operatorSlug) return false;
  if (current.accountId !== undefined && current.accountId !== binding.accountId) return false;
  if (current.operatorSlug !== undefined && current.operatorSlug !== binding.operatorSlug) return false;
  return true;
}

function readOptionalAccountId(value: string | null | undefined, label: string): string | undefined {
  if (value === undefined || value === null || value === "") return undefined;
  if (value.length > 256) throw new ApiHttpError(400, "bad_request", `${label} must be 256 characters or less`);
  return value;
}

function readOptionalOperatorSlug(value: string | null | undefined, label: string): string | undefined {
  if (value === undefined || value === null || value === "") return undefined;
  if (!isValidCoreSlug(value)) throw new ApiHttpError(400, "bad_request", `${label} must be a lowercase slug`);
  return value;
}

function readOptionalHeader(value: string | string[] | undefined, label: string): string | undefined {
  if (Array.isArray(value)) {
    if (value.length !== 1) throw new ApiHttpError(400, "bad_request", `${label} must be provided at most once`);
    value = value[0];
  }
  const raw = value;
  const trimmed = raw?.trim();
  return trimmed || undefined;
}

function readPolicyPreviewFlag(params: URLSearchParams): boolean {
  rejectUnsupportedQueryParams(params, ["preview"], "agent policy update");
  const raw = readOptionalQueryParam(params, "preview");
  if (raw === undefined) return false;
  if (raw === "true" || raw === "1") return true;
  if (raw === "false" || raw === "0") return false;
  throw new ApiHttpError(400, "bad_request", "preview must be true or false");
}

function validateLocalGrantTtl(value: unknown): number {
  if (value === undefined) return DEFAULT_LOCAL_GRANT_TTL_MS;
  if (typeof value !== "number" || !Number.isSafeInteger(value)) {
    throw new ApiHttpError(400, "bad_request", "ttlMs must be an integer number of milliseconds");
  }
  if (value < 1000 || value > MAX_LOCAL_GRANT_TTL_MS) {
    throw new ApiHttpError(400, "bad_request", "ttlMs must be between 1000 and 86400000 milliseconds");
  }
  return value;
}

function validateLocalGrantCreate(input: Record<string, unknown>): void {
  validateJsonObjectBody(input, "local grant creation");
  const unsupported = Object.keys(input).find((key) => key !== "ttlMs");
  if (unsupported) {
    throw new ApiHttpError(400, "bad_request", `${unsupported} is not supported for local grant creation`);
  }
}

function validateEmptyJsonBody(input: Record<string, unknown>, label: string): void {
  validateJsonObjectBody(input, label);
  const unsupported = Object.keys(input)[0];
  if (unsupported) {
    throw new ApiHttpError(400, "bad_request", `${label} body must be empty`);
  }
}

function validateJsonObjectBody(input: unknown, label: string): asserts input is Record<string, unknown> {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new ApiHttpError(400, "bad_request", `${label} body must be a JSON object`);
  }
}

async function validateAgentActionBody(req: IncomingMessage, label: string): Promise<void> {
  validateEmptyJsonBody(await readJson<Record<string, unknown>>(req), label);
}

function validatePairingStart(input: Record<string, unknown>): void {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new ApiHttpError(400, "bad_request", "pairing start body must be an object");
  }
  if (input.localApiOrigin !== undefined) {
    throw new ApiHttpError(400, "bad_request", "localApiOrigin is derived by the daemon");
  }
  rejectUnsupportedInputFields(input, ["pairingPublicNonce"], "pairing start");
  if (input.pairingPublicNonce !== undefined && !isValidPairingPublicNonce(input.pairingPublicNonce)) {
    throw new ApiHttpError(
      400,
      "bad_request",
      "pairingPublicNonce must be an unpadded base64url string from 32 to 128 characters",
    );
  }
}

function readPairingId(value: string): string {
  let decoded: string;
  try {
    decoded = decodeURIComponent(value);
  } catch {
    throw new ApiHttpError(400, "bad_request", "pairing id must be a valid encoded safe identifier");
  }
  if (!isValidPairingId(decoded)) {
    throw new ApiHttpError(400, "bad_request", "pairing id must be a safe identifier");
  }
  return decoded;
}

function localApiOrigin(req: IncomingMessage): string | undefined {
  if (!req.headers.host || !isLoopbackHost(req.headers.host)) return undefined;
  return `http://${req.headers.host}`;
}

function validatePairingConfirm(input: Record<string, unknown>): Record<string, unknown> {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new ApiHttpError(400, "bad_request", "pairing confirm body must be an object");
  }
  try {
    return normalizePairingConfirmInput(input);
  } catch (error) {
    throw new ApiHttpError(400, "bad_request", error instanceof Error ? error.message : String(error));
  }
}

async function validateCreateAgent(
  input: AgentCreateInput,
  options: { canCreateCoreIdentity?: boolean } = {},
): Promise<void> {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new ApiHttpError(400, "bad_request", "agent creation body must be an object");
  }
  rejectUnsupportedInputFields(input as unknown as Record<string, unknown>, [
    "name",
    "provider",
    "accessMode",
    "networkAccess",
    "deniedTools",
    "fileAccess",
    "providerConfigPaths",
    "instructions",
    "projectPath",
    "operatorSlug",
    "agentSlug",
    "coreIdentity",
    "start",
  ], "agent creation");
  if (!input || typeof input.name !== "string" || !input.name.trim()) {
    throw new ApiHttpError(400, "bad_request", "agent name is required");
  }
  if (input.provider !== "claude" && input.provider !== "codex") {
    throw new ApiHttpError(400, "bad_request", "provider must be claude or codex");
  }
  if (
    input.accessMode !== undefined &&
    input.accessMode !== "safe" &&
    input.accessMode !== "project" &&
    input.accessMode !== "trusted"
  ) {
    throw new ApiHttpError(400, "bad_request", "accessMode must be safe, project, or trusted");
  }
  if (input.instructions !== undefined && typeof input.instructions !== "string") {
    throw new ApiHttpError(400, "bad_request", "instructions must be a string");
  }
  if (input.start !== undefined && typeof input.start !== "boolean") {
    throw new ApiHttpError(400, "bad_request", "start must be a boolean");
  }
  if (
    input.networkAccess !== undefined &&
    input.networkAccess !== "inherit" &&
    input.networkAccess !== "deny"
  ) {
    throw new ApiHttpError(400, "bad_request", "networkAccess must be inherit or deny");
  }
  if (input.deniedTools !== undefined) {
    if (!Array.isArray(input.deniedTools) || !input.deniedTools.every(isValidDeniedTool)) {
      throw new ApiHttpError(400, "bad_request", "deniedTools must be an array of non-empty strings");
    }
  }
  if (input.fileAccess !== undefined) input.fileAccess = await canonicalFileAccessInput(input.fileAccess);
  if (input.providerConfigPaths !== undefined) {
    validateProviderConfigPaths(input.provider, input.providerConfigPaths);
    input.providerConfigPaths = normalizeProviderConfigPaths(input.providerConfigPaths);
  }
  rejectTrustedIgnoredPolicy(
    input.accessMode ?? "safe",
    parseNetworkAccess(input.networkAccess, "inherit"),
    normalizeDeniedTools(input.deniedTools),
    normalizeFileAccess(input.fileAccess),
    normalizeProviderConfigPaths(input.providerConfigPaths),
  );
  if (input.accessMode === "project") {
    if (typeof input.projectPath !== "string" || !input.projectPath.trim()) {
      throw new ApiHttpError(400, "bad_request", "projectPath is required when accessMode is project");
    }
  }
  if (input.projectPath !== undefined) {
    if (typeof input.projectPath !== "string" || !input.projectPath.trim()) {
      throw new ApiHttpError(400, "bad_request", "projectPath must be a non-empty string");
    }
    if (!path.isAbsolute(input.projectPath)) throw new ApiHttpError(400, "bad_request", "projectPath must be absolute");
    const canonicalProjectPath = await canonicalDirectory(input.projectPath);
    if (!canonicalProjectPath) throw new ApiHttpError(400, "bad_request", "projectPath must be an existing directory");
    input.projectPath = canonicalProjectPath;
  }
  if (input.operatorSlug !== undefined && !isValidCoreSlug(input.operatorSlug)) {
    throw new ApiHttpError(400, "bad_request", "operatorSlug must be a lowercase slug");
  }
  if (input.agentSlug !== undefined && !isValidCoreSlug(input.agentSlug)) {
    throw new ApiHttpError(400, "bad_request", "agentSlug must be a lowercase slug");
  }
  validateCoreIdentity(input);
  if (input.agentSlug !== undefined && input.operatorSlug === undefined && input.coreIdentity === undefined) {
    throw new ApiHttpError(400, "bad_request", "agentSlug requires operatorSlug");
  }
  if (input.start === true && input.operatorSlug === undefined && input.coreIdentity === undefined) {
    throw new ApiHttpError(400, "bad_request", "operatorSlug or coreIdentity is required when start is true");
  }
  if (input.operatorSlug !== undefined && input.coreIdentity === undefined && options.canCreateCoreIdentity === false) {
    throw new ApiHttpError(400, "bad_request", "core identity creation is unavailable");
  }
}

function validateCoreIdentity(input: AgentCreateInput): void {
  if (input.coreIdentity === undefined) return;
  const identity = input.coreIdentity;
  if (!identity || typeof identity !== "object" || Array.isArray(identity)) {
    throw new ApiHttpError(400, "bad_request", "coreIdentity must be an object");
  }
  const allowedIdentityFields = new Set([
    "operatorSlug",
    "agentSlug",
    "identityType",
    "declaredOperatorPublicKey",
    "source",
  ]);
  const unsupportedField = Object.keys(identity).find((field) => !allowedIdentityFields.has(field));
  if (unsupportedField) {
    throw new ApiHttpError(400, "bad_request", `coreIdentity.${unsupportedField} is not supported`);
  }
  if (!isValidCoreSlug(identity.operatorSlug)) {
    throw new ApiHttpError(400, "bad_request", "coreIdentity.operatorSlug must be a lowercase slug");
  }
  if (!isValidCoreSlug(identity.agentSlug)) {
    throw new ApiHttpError(400, "bad_request", "coreIdentity.agentSlug must be a lowercase slug");
  }
  if (identity.identityType !== "agent") {
    throw new ApiHttpError(400, "bad_request", "coreIdentity.identityType must be agent");
  }
  const declaredOperatorPublicKey = normalizeDeclaredOperatorPublicKey(identity.declaredOperatorPublicKey);
  if (!declaredOperatorPublicKey) {
    throw new ApiHttpError(400, "bad_request", "coreIdentity.declaredOperatorPublicKey must be a non-empty string of 4096 characters or less");
  }
  identity.declaredOperatorPublicKey = declaredOperatorPublicKey;
  if (identity.source !== undefined && identity.source !== "web_signed") {
    throw new ApiHttpError(400, "bad_request", "coreIdentity.source must be web_signed when supplied through the local API");
  }
  identity.source ??= "web_signed";
  if (input.operatorSlug !== undefined && input.operatorSlug !== identity.operatorSlug) {
    throw new ApiHttpError(400, "bad_request", "operatorSlug must match coreIdentity.operatorSlug");
  }
  if (input.agentSlug !== undefined && input.agentSlug !== identity.agentSlug) {
    throw new ApiHttpError(400, "bad_request", "agentSlug must match coreIdentity.agentSlug");
  }
}

async function validatePolicyUpdate(input: AgentPolicyUpdateInput, current: AgentConfig): Promise<void> {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new ApiHttpError(400, "bad_request", "policy update body is required");
  }
  rejectUnsupportedInputFields(input as unknown as Record<string, unknown>, [
    "accessMode",
    "networkAccess",
    "deniedTools",
    "fileAccess",
    "providerConfigPaths",
    "projectPath",
  ], "policy update");
  if (
    input.accessMode !== undefined &&
    input.accessMode !== "safe" &&
    input.accessMode !== "project" &&
    input.accessMode !== "trusted"
  ) {
    throw new ApiHttpError(400, "bad_request", "accessMode must be safe, project, or trusted");
  }
  if (
    input.networkAccess !== undefined &&
    input.networkAccess !== "inherit" &&
    input.networkAccess !== "deny"
  ) {
    throw new ApiHttpError(400, "bad_request", "networkAccess must be inherit or deny");
  }
  if (input.deniedTools !== undefined) {
    if (!Array.isArray(input.deniedTools) || !input.deniedTools.every(isValidDeniedTool)) {
      throw new ApiHttpError(400, "bad_request", "deniedTools must be an array of non-empty strings");
    }
  }
  const nextAccessMode = input.accessMode ?? current.accessMode;
  const nextNetworkAccess = input.networkAccess === undefined
    ? parseNetworkAccess(current.networkAccess, "inherit")
    : parseNetworkAccess(input.networkAccess, "inherit");
  const nextDeniedTools = input.deniedTools === undefined
    ? normalizeDeniedTools(current.deniedTools)
    : normalizeDeniedTools(input.deniedTools);
  if (input.fileAccess !== undefined && input.fileAccess !== null) {
    input.fileAccess = await canonicalFileAccessInput(input.fileAccess);
  }
  const nextFileAccess = mergeFileAccess(current.fileAccess, input.fileAccess);
  if (input.providerConfigPaths !== undefined && input.providerConfigPaths !== null) {
    validateProviderConfigPaths(current.provider, input.providerConfigPaths);
    input.providerConfigPaths = normalizeProviderConfigPaths(input.providerConfigPaths);
  }
  const nextProviderConfigPaths = mergeProviderConfigPaths(current.providerConfigPaths, input.providerConfigPaths);
  rejectTrustedIgnoredPolicy(
    nextAccessMode,
    nextNetworkAccess,
    nextDeniedTools,
    nextFileAccess,
    nextProviderConfigPaths,
  );
  const nextProjectPath = input.projectPath === undefined ? current.projectPath : input.projectPath;
  if (nextAccessMode === "project" && (typeof nextProjectPath !== "string" || !nextProjectPath.trim())) {
    throw new ApiHttpError(400, "bad_request", "projectPath is required when accessMode is project");
  }
  if (nextProjectPath !== undefined && nextProjectPath !== null) {
    if (typeof nextProjectPath !== "string" || !nextProjectPath.trim()) {
      throw new ApiHttpError(400, "bad_request", "projectPath must be a non-empty string");
    }
    if (!path.isAbsolute(nextProjectPath)) throw new ApiHttpError(400, "bad_request", "projectPath must be absolute");
    const canonicalProjectPath = await canonicalDirectory(nextProjectPath);
    if (!canonicalProjectPath) throw new ApiHttpError(400, "bad_request", "projectPath must be an existing directory");
    if (input.projectPath !== undefined) input.projectPath = canonicalProjectPath;
  }
}

function rejectTrustedIgnoredPolicy(
  accessMode: string,
  networkAccess: string,
  deniedTools: string[],
  fileAccess: ReturnType<typeof normalizeFileAccess>,
  providerConfigPaths: string[],
): void {
  if (accessMode !== "trusted") return;
  if (networkAccess === "deny") {
    throw new ApiHttpError(400, "bad_request", "networkAccess deny requires safe or project accessMode");
  }
  if (deniedTools.length > 0) {
    throw new ApiHttpError(400, "bad_request", "deniedTools require safe or project accessMode");
  }
  if (fileAccessHasEntries(fileAccess)) {
    throw new ApiHttpError(400, "bad_request", "fileAccess requires safe or project accessMode");
  }
  if (providerConfigPaths.length > 0) {
    throw new ApiHttpError(400, "bad_request", "providerConfigPaths require safe or project accessMode");
  }
}

function requireCoreIdentityForApiStart(agent: AgentConfig): void {
  if (!agent.coreIdentity) {
    throw new ApiHttpError(
      400,
      "bad_request",
      "agent must have coreIdentity from operatorSlug or supplied coreIdentity before it can be started",
    );
  }
}

async function requireCoreIdentityCreationReady(core: CoreNative): Promise<void> {
  try {
    const device = await core.openOrCreateDevice({});
    if (device.status === "ready") return;
    throw new ApiHttpError(
      400,
      "bad_request",
      `core identity creation requires native core ready; current status is ${device.status}`,
    );
  } catch (error) {
    if (error instanceof ApiHttpError) throw error;
    throw new ApiHttpError(
      400,
      "bad_request",
      `core identity creation is unavailable: ${redact(error instanceof Error ? error.message : String(error))}`,
    );
  }
}

async function canonicalDirectory(value: string): Promise<string | undefined> {
  try {
    const realPath = await fs.realpath(value);
    return (await fs.stat(realPath)).isDirectory() ? realPath : undefined;
  } catch {
    return undefined;
  }
}

async function canonicalFileAccessInput(value: unknown): Promise<{ readablePaths?: string[]; writablePaths?: string[] }> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new ApiHttpError(400, "bad_request", "fileAccess must be an object");
  }
  const input = value as Record<string, unknown>;
  rejectUnsupportedInputFields(input, ["readablePaths", "writablePaths"], "fileAccess");
  const output: { readablePaths?: string[]; writablePaths?: string[] } = {};
  if ("readablePaths" in input) {
    output.readablePaths = await canonicalFileAccessPaths(input.readablePaths, "fileAccess.readablePaths");
  }
  if ("writablePaths" in input) {
    output.writablePaths = await canonicalFileAccessPaths(input.writablePaths, "fileAccess.writablePaths");
  }
  return output;
}

function rejectUnsupportedInputFields(input: Record<string, unknown>, allowed: readonly string[], label: string): void {
  const allowedSet = new Set(allowed);
  const unsupported = Object.keys(input).find((key) => !allowedSet.has(key));
  if (unsupported) {
    throw new ApiHttpError(400, "bad_request", `${unsupported} is not supported for ${label}`);
  }
}

function validateDevInjectInput(input: Record<string, unknown>): void {
  validateJsonObjectBody(input, "dev inject");
  rejectUnsupportedInputFields(input, ["senderSlug", "body"], "dev inject");
  if (input.senderSlug !== undefined && !isValidCoreSlug(input.senderSlug)) {
    throw new ApiHttpError(400, "bad_request", "senderSlug must be a lowercase slug");
  }
}

function validateProviderConfigPaths(provider: AgentConfig["provider"], value: unknown): void {
  if (!Array.isArray(value)) {
    throw new ApiHttpError(400, "bad_request", "providerConfigPaths must be an array of provider-owned relative home paths");
  }
  if (value.length > 32) {
    throw new ApiHttpError(400, "bad_request", "providerConfigPaths must contain at most 32 paths");
  }
  if (!value.every((entry) => isValidProviderConfigPathForProvider(provider, entry))) {
    throw new ApiHttpError(
      400,
      "bad_request",
      `providerConfigPaths entries must be supported ${provider} config, command, prompt, or skill paths`,
    );
  }
}

async function canonicalFileAccessPaths(value: unknown, label: string): Promise<string[]> {
  if (!Array.isArray(value)) {
    throw new ApiHttpError(400, "bad_request", `${label} must be an array of absolute directory paths`);
  }
  if (value.length > 64) {
    throw new ApiHttpError(400, "bad_request", `${label} must contain at most 64 paths`);
  }
  const canonical = await Promise.all(value.map((entry) => canonicalFileAccessPath(entry, label)));
  return Array.from(new Set(canonical));
}

async function canonicalFileAccessPath(value: unknown, label: string): Promise<string> {
  if (typeof value !== "string" || !value.trim()) {
    throw new ApiHttpError(400, "bad_request", `${label} entries must be non-empty strings`);
  }
  if (value.length > 4096) {
    throw new ApiHttpError(400, "bad_request", `${label} entries must be 4096 characters or less`);
  }
  if (!path.isAbsolute(value)) {
    throw new ApiHttpError(400, "bad_request", `${label} entries must be absolute paths`);
  }
  const canonical = await canonicalDirectory(value);
  if (!canonical) {
    throw new ApiHttpError(400, "bad_request", `${label} entries must be existing directories`);
  }
  return canonical;
}

async function readJson<T = Record<string, unknown>>(req: IncomingMessage): Promise<T> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += buffer.length;
    if (size > MAX_JSON_BODY_BYTES) {
      throw new ApiHttpError(413, "payload_too_large", "JSON body is too large");
    }
    chunks.push(buffer);
  }
  const text = Buffer.concat(chunks).toString("utf8").trim();
  if (!text) return {} as T;
  if (!isJsonContentType(req.headers["content-type"])) {
    throw new ApiHttpError(415, "unsupported_media_type", "Content-Type must be application/json");
  }
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new ApiHttpError(400, "bad_request", "invalid JSON body");
  }
}

function isJsonContentType(value: string | string[] | undefined): boolean {
  const raw = Array.isArray(value) ? value[0] : value;
  return Boolean(raw?.toLowerCase().split(";")[0]?.trim() === "application/json");
}

function parseAllowedOriginsEnv(value: string | undefined): readonly string[] | undefined {
  if (value === undefined || !value.trim()) return undefined;
  return value.split(",");
}

function normalizeAllowedOrigins(values: readonly string[] | undefined, label: string): readonly string[] | undefined {
  if (values === undefined) return undefined;
  const origins = new Set<string>();
  for (const value of values) {
    const normalized = normalizeAllowedOrigin(value, label);
    if (normalized === "*") return undefined;
    if (normalized) origins.add(normalized);
  }
  return [...origins];
}

function normalizeAllowedOrigin(value: string, label: string): string | undefined {
  const trimmed = value.trim();
  if (!trimmed) return undefined;
  if (trimmed === "*") return "*";

  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    throw new Error(`${label} contains invalid origin: ${trimmed}`);
  }

  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new Error(`${label} only supports http and https origins`);
  }
  if (parsed.username || parsed.password || parsed.pathname !== "/" || parsed.search || parsed.hash) {
    throw new Error(`${label} entries must be origins without path, query, fragment, or credentials`);
  }
  return parsed.origin;
}

function requestOrigin(req: IncomingMessage): string | undefined {
  const header = req.headers.origin;
  if (Array.isArray(header)) return header[0];
  return header;
}

function normalizedRequestOrigin(req: IncomingMessage): string | undefined {
  const origin = requestOrigin(req)?.trim();
  if (!origin) return undefined;
  try {
    return new URL(origin).origin;
  } catch {
    return "__invalid_origin__";
  }
}

function isPublicPairingRoute(method: string | undefined, path: string): boolean {
  return method === "POST" && path === "/pairing/start" || method === "GET" && /^\/pairing\/[^/]+$/.test(path);
}

function setCors(req: IncomingMessage, res: ServerResponse, allowedOrigins: readonly string[] | undefined): boolean {
  const origin = requestOrigin(req);
  res.setHeader("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS");
  res.setHeader(
    "Access-Control-Allow-Headers",
    "Content-Type,Authorization,X-Agent-Core-Token,X-Agent-Core-Account-Id,X-Agent-Core-Operator-Slug",
  );
  res.setHeader("Access-Control-Allow-Private-Network", "true");
  res.setHeader("Cache-Control", "no-store");
  res.setHeader("X-Content-Type-Options", "nosniff");
  res.setHeader(
    "Vary",
    allowedOrigins ? "Origin, Access-Control-Request-Private-Network" : "Access-Control-Request-Private-Network",
  );

  if (!allowedOrigins) {
    res.setHeader("Access-Control-Allow-Origin", "*");
    return true;
  }
  if (!origin) return true;
  if (!allowedOrigins.includes(origin)) return false;

  res.setHeader("Access-Control-Allow-Origin", origin);
  return true;
}

function sendJson(res: ServerResponse, status: number, body: unknown): void {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.end(status === 204 ? undefined : JSON.stringify(body));
}
