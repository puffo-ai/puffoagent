use std::collections::HashMap;
use std::io::BufRead;
use std::io::{self, Write};

const PROD_PAIRING_AND_SYNC_REASON: &str =
    "production native profile parsed; backend PR #26 must merge/deploy for pairing and backend PR #25 must merge/deploy from dev before spaces/invites sync can be trusted";
const PROD_PAIRING_AND_SYNC_NEXT_ACTION: &str =
    "Merge/deploy backend PR #26 for pairing, then merge/deploy backend PR #25 from dev and run production agent identity publication plus account-bound sync verification.";

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct RpcRequest {
    id: u64,
    method: String,
    #[serde(default)]
    params: serde_json::Value,
}

#[derive(serde::Serialize)]
struct RpcResponse {
    id: u64,
    ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    result: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
}

fn main() {
    if let Err(error) = run() {
        let _ = writeln!(io::stderr(), "{error}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), String> {
    match NativeProfile::from_env()? {
        NativeProfile::DevMock => run_dev_mock(),
        NativeProfile::Prod => {
            let mut sidecar = ProdSidecar::from_env();
            sidecar.serve()
        }
    }
}

#[cfg(feature = "dev-tools")]
fn run_dev_mock() -> Result<(), String> {
    DevSidecar::new()?.serve()
}

#[cfg(not(feature = "dev-tools"))]
fn run_dev_mock() -> Result<(), String> {
    Err("dev_mock native profile requires agent-native built with dev-tools".to_string())
}

#[cfg(feature = "dev-tools")]
mod dev {
    use std::collections::{BTreeSet, HashMap};
    use std::io::BufRead;

    use agent_native::{NativeCore, NativeSession};
    use client::api::SignupCommand;
    use client::domain::{IdentityRecord, MessageRecord};
    use serde::de::DeserializeOwned;
    use serde_json::{json, Value};
    use types::IdentityType;

    use super::{write_response, RpcRequest};

    pub(super) struct DevSidecar {
        core: NativeCore,
        sessions: HashMap<String, NativeSession>,
        delivered: HashMap<String, BTreeSet<String>>,
    }

    impl DevSidecar {
        pub(super) fn new() -> Result<Self, String> {
            Ok(Self {
                core: NativeCore::for_dev_mock().map_err(|err| err.to_string())?,
                sessions: HashMap::new(),
                delivered: HashMap::new(),
            })
        }

        pub(super) fn serve(&mut self) -> Result<(), String> {
            let stdin = std::io::stdin();
            let mut stdout = std::io::stdout();
            for line in stdin.lock().lines() {
                let line = line.map_err(|err| err.to_string())?;
                if line.trim().is_empty() {
                    continue;
                }
                let response = match serde_json::from_str::<RpcRequest>(&line) {
                    Ok(request) => {
                        let id = request.id;
                        match self.handle(request) {
                            Ok(result) => super::ok_response(id, result),
                            Err(error) => super::error_response(id, error),
                        }
                    }
                    Err(error) => super::error_response(0, format!("invalid request: {error}")),
                };
                write_response(&mut stdout, &response)?;
            }
            Ok(())
        }

        fn handle(&mut self, request: RpcRequest) -> Result<Value, String> {
            match request.method.as_str() {
                "health" => Ok(json!({
                    "connected": true,
                    "status": "ready",
                    "deviceId": "native-sidecar-dev_mock",
                    "mode": "dev_mock",
                    "version": env!("CARGO_PKG_VERSION")
                })),
                "startPairing" => Ok(json!({
                    "status": "unavailable",
                    "reason": "server pairing start is owned by the daemon pairing gateway, not the dev_mock native sidecar",
                    "blockedBy": ["daemon_pairing_gateway"],
                    "nextAction": "Start the daemon normally so /pairing/start can call the backend pairing contract."
                })),
                "confirmPairing" => Ok(json!({
                    "connected": false,
                    "status": "unavailable",
                    "reason": "production auth-token activation requires the prod native profile",
                    "blockedBy": ["prod_native_profile"],
                    "nextAction": "Run the native sidecar in production profile before handing it a server-issued authToken."
                })),
                "createAgentIdentity" => {
                    let params: CreateAgentIdentityParams = parse_params(request.params)?;
                    let status =
                        self.create_agent_identity(&params.operator_slug, &params.agent_slug)?;
                    Ok(json!(status))
                }
                "openAgentSession" => {
                    let params: OpenAgentSessionParams = parse_params(request.params)?;
                    let handle = self.ensure_agent_session(&params.agent_slug)?;
                    Ok(json!({
                        "handle": handle,
                        "slug": params.agent_slug
                    }))
                }
                "syncOnce" => {
                    let params: HandleParams = parse_params(request.params)?;
                    let session = self.session(&params.handle)?;
                    let report = session.sync_once().map_err(|err| err.to_string())?;
                    Ok(json!({
                        "certsUpdated": report.certs_updated,
                        "invitationsUpdated": report.invitations_updated,
                        "spacesUpdated": report.spaces_updated,
                        "messagesProcessed": report.messages_processed
                    }))
                }
                "processPendingMessages" => {
                    let params: HandleParams = parse_params(request.params)?;
                    let slug = slug_from_handle(&params.handle)?;
                    let messages = {
                        let session = self.session(&params.handle)?;
                        let _processed = session
                            .process_pending_messages()
                            .map_err(|err| err.to_string())?;
                        session.list_messages().map_err(|err| err.to_string())?
                    };
                    let opened = self.opened_messages(&params.handle, &slug, messages);
                    Ok(json!({ "messages": opened }))
                }
                "sendChannelReply" => {
                    let params: SendChannelParams = parse_params(request.params)?;
                    let session = self.session(&params.handle)?;
                    let message = session
                        .send_channel_text(
                            params.space_id,
                            params.channel_id,
                            params.body,
                            params.thread_root_id,
                            params.reply_to_id,
                        )
                        .map_err(|err| err.to_string())?;
                    Ok(json!({ "messageId": message.envelope_id }))
                }
                "sendDirectReply" => {
                    let params: SendDirectParams = parse_params(request.params)?;
                    let session = self.session(&params.handle)?;
                    let message = session
                        .send_direct_text(params.recipient_slug, params.body, params.reply_to_id)
                        .map_err(|err| err.to_string())?;
                    Ok(json!({ "messageId": message.envelope_id }))
                }
                "snapshot" => {
                    let params: HandleParams = parse_params(request.params)?;
                    let slug = slug_from_handle(&params.handle)?;
                    Ok(json!({ "handle": params.handle, "slug": slug }))
                }
                "closeSession" => {
                    let params: HandleParams = parse_params(request.params)?;
                    self.sessions.remove(&params.handle);
                    self.delivered.remove(&params.handle);
                    Ok(json!({ "closed": true }))
                }
                "devInjectChannelMessage" => {
                    let params: DevInjectChannelMessageParams = parse_params(request.params)?;
                    let injected = self.dev_inject_channel_message(params)?;
                    Ok(json!(injected))
                }
                other => Err(format!("unknown method: {other}")),
            }
        }

        fn create_agent_identity(
            &mut self,
            operator_slug: &str,
            agent_slug: &str,
        ) -> Result<AgentIdentityStatus, String> {
            let operator_handle = self.ensure_human_session(operator_slug)?;
            let agent_handle = session_handle(agent_slug);
            if !self.sessions.contains_key(&agent_handle) {
                let agent = {
                    let operator = self.session(&operator_handle)?;
                    operator
                        .create_agent_identity(agent_slug.to_string())
                        .map_err(|err| err.to_string())?
                };
                self.sessions.insert(agent_handle, agent);
            }
            let record = self
                .identity_record(agent_slug)?
                .ok_or_else(|| "agent identity not found after create".to_string())?;
            Ok(AgentIdentityStatus {
                ok: true,
                operator_slug: operator_slug.to_string(),
                agent_slug: agent_slug.to_string(),
                identity_type: "agent".to_string(),
                declared_operator_public_key: record.identity_cert.declared_operator_public_key,
            })
        }

        fn ensure_human_session(&mut self, slug: &str) -> Result<String, String> {
            let handle = session_handle(slug);
            if !self.sessions.contains_key(&handle) {
                let session = match self.core.open_session(slug.to_string()) {
                    Ok(session) => session,
                    Err(_) => NativeSession::from_session_for_bridge(
                        self.core
                            .sdk_for_bridge()
                            .signup(SignupCommand {
                                slug: slug.to_string(),
                                identity_type: IdentityType::Human,
                            })
                            .map_err(|err| err.to_string())?,
                    ),
                };
                self.sessions.insert(handle.clone(), session);
            }
            Ok(handle)
        }

        fn ensure_agent_session(&mut self, slug: &str) -> Result<String, String> {
            let handle = session_handle(slug);
            if !self.sessions.contains_key(&handle) {
                let session = match self.core.open_session(slug.to_string()) {
                    Ok(session) => session,
                    Err(_) => {
                        let operator_slug = format!("dev-operator-{slug}");
                        let operator_handle = self.ensure_human_session(&operator_slug)?;
                        let operator = self.session(&operator_handle)?;
                        operator
                            .create_agent_identity(slug.to_string())
                            .map_err(|err| err.to_string())?
                    }
                };
                self.sessions.insert(handle.clone(), session);
            }
            Ok(handle)
        }

        fn session(&self, handle: &str) -> Result<&NativeSession, String> {
            self.sessions
                .get(handle)
                .ok_or_else(|| format!("session handle not found: {handle}"))
        }

        fn identity_record(&self, slug: &str) -> Result<Option<IdentityRecord>, String> {
            Ok(self
                .core
                .sdk_for_bridge()
                .list_identities()
                .map_err(|err| err.to_string())?
                .into_iter()
                .find(|identity| identity.slug == slug))
        }

        fn opened_messages(
            &mut self,
            handle: &str,
            self_slug: &str,
            messages: Vec<MessageRecord>,
        ) -> Vec<OpenedMessageResponse> {
            let delivered = self.delivered.entry(handle.to_string()).or_default();
            messages
                .into_iter()
                .filter(|message| message.sender_slug != self_slug)
                .filter(|message| delivered.insert(message.envelope_id.clone()))
                .map(|message| opened_message_response(message, self_slug))
                .collect()
        }

        fn dev_inject_channel_message(
            &mut self,
            params: DevInjectChannelMessageParams,
        ) -> Result<DevInjectedMessage, String> {
            let agent_handle = self.ensure_agent_session(&params.agent_slug)?;
            let sender_handle = self.ensure_human_session(&params.sender_slug)?;
            let (space_id, channel_id) = unique_channel_route();
            let invitation_event_id = {
                let sender = self.session(&sender_handle)?;
                sender
                    .create_space(space_id.clone(), "Dev injected conversation")
                    .map_err(|err| err.to_string())?;
                sender
                    .create_channel(
                        space_id.clone(),
                        channel_id.clone(),
                        "Dev injected conversation",
                        Vec::new(),
                    )
                    .map_err(|err| err.to_string())?;
                sender
                    .invite_to_channel(
                        space_id.clone(),
                        channel_id.clone(),
                        params.agent_slug.clone(),
                    )
                    .map_err(|err| err.to_string())?
                    .invitation_event_id
            };
            {
                let agent = self.session(&agent_handle)?;
                agent
                    .accept_channel_invite(
                        space_id.clone(),
                        channel_id.clone(),
                        invitation_event_id,
                    )
                    .map_err(|err| err.to_string())?;
            }
            let message_id = {
                let sender = self.session(&sender_handle)?;
                let message = sender
                    .send_channel_text(
                        space_id.clone(),
                        channel_id.clone(),
                        params.body,
                        None,
                        None,
                    )
                    .map_err(|err| err.to_string())?;
                message.envelope_id
            };
            Ok(DevInjectedMessage {
                message_id,
                space_id,
                channel_id,
            })
        }
    }

    #[derive(serde::Serialize)]
    #[serde(rename_all = "camelCase")]
    struct AgentIdentityStatus {
        ok: bool,
        operator_slug: String,
        agent_slug: String,
        identity_type: String,
        declared_operator_public_key: Option<String>,
    }

    #[derive(serde::Serialize)]
    #[serde(rename_all = "camelCase")]
    struct OpenedMessageResponse {
        id: String,
        body: String,
        sender_slug: Option<String>,
        space_id: Option<String>,
        channel_id: Option<String>,
        thread_root_id: Option<String>,
        reply_to_id: Option<String>,
        mentioned: bool,
        dm: bool,
        must_respond: bool,
    }

    #[derive(serde::Serialize)]
    #[serde(rename_all = "camelCase")]
    struct DevInjectedMessage {
        message_id: String,
        space_id: String,
        channel_id: String,
    }

    #[derive(serde::Deserialize)]
    #[serde(rename_all = "camelCase", deny_unknown_fields)]
    struct CreateAgentIdentityParams {
        operator_slug: String,
        agent_slug: String,
    }

    #[derive(serde::Deserialize)]
    #[serde(rename_all = "camelCase", deny_unknown_fields)]
    struct OpenAgentSessionParams {
        agent_slug: String,
    }

    #[derive(serde::Deserialize)]
    #[serde(rename_all = "camelCase", deny_unknown_fields)]
    struct HandleParams {
        handle: String,
    }

    #[derive(serde::Deserialize)]
    #[serde(rename_all = "camelCase", deny_unknown_fields)]
    struct SendChannelParams {
        handle: String,
        space_id: String,
        channel_id: String,
        body: String,
        thread_root_id: Option<String>,
        reply_to_id: Option<String>,
    }

    #[derive(serde::Deserialize)]
    #[serde(rename_all = "camelCase", deny_unknown_fields)]
    struct SendDirectParams {
        handle: String,
        recipient_slug: String,
        body: String,
        reply_to_id: Option<String>,
    }

    #[derive(serde::Deserialize)]
    #[serde(rename_all = "camelCase", deny_unknown_fields)]
    struct DevInjectChannelMessageParams {
        sender_slug: String,
        agent_slug: String,
        body: String,
    }

    fn opened_message_response(message: MessageRecord, self_slug: &str) -> OpenedMessageResponse {
        let dm = message.recipient_slug.as_deref() == Some(self_slug);
        let mentioned = body_mentions_slug(&message.body, self_slug);
        OpenedMessageResponse {
            id: message.envelope_id,
            body: message.body,
            sender_slug: Some(message.sender_slug),
            space_id: message.space_id,
            channel_id: message.channel_id,
            thread_root_id: message.thread_root_id,
            reply_to_id: message.reply_to_id,
            mentioned,
            dm,
            must_respond: dm || mentioned,
        }
    }

    fn body_mentions_slug(body: &str, slug: &str) -> bool {
        let needle = format!("@{slug}");
        let mut offset = 0;
        while let Some(index) = body[offset..].find(&needle) {
            let end = offset + index + needle.len();
            let next = body[end..].chars().next();
            if next.map(|ch| !is_slug_char(ch)).unwrap_or(true) {
                return true;
            }
            offset = end;
        }
        false
    }

    fn is_slug_char(ch: char) -> bool {
        ch.is_ascii_alphanumeric() || ch == '-'
    }

    fn session_handle(slug: &str) -> String {
        format!("session:{slug}")
    }

    fn slug_from_handle(handle: &str) -> Result<String, String> {
        handle
            .strip_prefix("session:")
            .filter(|slug| !slug.is_empty())
            .map(str::to_string)
            .ok_or_else(|| format!("unsupported session handle: {handle}"))
    }

    fn parse_params<T: DeserializeOwned>(value: Value) -> Result<T, String> {
        serde_json::from_value(value).map_err(|err| err.to_string())
    }

    fn unique_channel_route() -> (String, String) {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|duration| duration.as_nanos())
            .unwrap_or_default();
        let suffix = nanos & 0xffff_ffff_ffff;
        (
            format!("sp_00000000-0000-4000-8000-{suffix:012x}"),
            format!("ch_00000000-0000-4000-8001-{suffix:012x}"),
        )
    }

    #[cfg(test)]
    mod tests {
        use super::body_mentions_slug;

        #[test]
        fn body_mentions_slug_requires_slug_boundary() {
            assert!(body_mentions_slug("@alice-agent status?", "alice-agent"));
            assert!(body_mentions_slug(
                "ping @alice-agent, please",
                "alice-agent"
            ));
            assert!(!body_mentions_slug(
                "@alice-agent-extra status?",
                "alice-agent"
            ));
            assert!(!body_mentions_slug("@alice-agent1 status?", "alice-agent"));
            assert!(!body_mentions_slug("alice-agent status?", "alice-agent"));
        }
    }
}

use agent_native::{
    load_prod_auth_token_from_keychain, store_prod_auth_token_in_keychain, NativeCore,
    NativeProdConfig,
};
#[cfg(feature = "dev-tools")]
use dev::DevSidecar;

enum NativeProfile {
    DevMock,
    Prod,
}

impl NativeProfile {
    fn from_env() -> Result<Self, String> {
        match std::env::var("AGENT_CORE_NATIVE_PROFILE")
            .unwrap_or_else(|_| default_native_profile().to_string())
            .as_str()
        {
            "" | "dev" | "dev_mock" => Ok(Self::DevMock),
            "prod" | "production" => Ok(Self::Prod),
            other => Err(format!(
                "unsupported AGENT_CORE_NATIVE_PROFILE: {other}; expected dev_mock or prod"
            )),
        }
    }
}

#[cfg(feature = "dev-tools")]
fn default_native_profile() -> &'static str {
    "dev_mock"
}

#[cfg(not(feature = "dev-tools"))]
fn default_native_profile() -> &'static str {
    "prod"
}

struct ProdSidecar {
    config: ProdProfileConfig,
    core: Option<NativeCore>,
    sessions: HashMap<String, agent_native::NativeSession>,
}

impl ProdSidecar {
    fn from_env() -> Self {
        Self {
            config: ProdProfileConfig::from_env(),
            core: None,
            sessions: HashMap::new(),
        }
    }

    fn serve(&mut self) -> Result<(), String> {
        let stdin = std::io::stdin();
        let mut stdout = std::io::stdout();
        for line in stdin.lock().lines() {
            let line = line.map_err(|err| err.to_string())?;
            if line.trim().is_empty() {
                continue;
            }
            let response = match serde_json::from_str::<RpcRequest>(&line) {
                Ok(request) => {
                    let id = request.id;
                    match self.handle(request) {
                        Ok(result) => ok_response(id, result),
                        Err(error) => error_response(id, error),
                    }
                }
                Err(error) => error_response(0, format!("invalid request: {error}")),
            };
            write_response(&mut stdout, &response)?;
        }
        Ok(())
    }

    fn handle(&mut self, request: RpcRequest) -> Result<serde_json::Value, String> {
        match request.method.as_str() {
            "health" => Ok(self.health()),
            "startPairing" => Ok(serde_json::json!({
                "status": "unavailable",
                "reason": self.unavailable_reason(),
                "blockedBy": ["daemon_pairing_gateway"],
                "nextAction": "Start the daemon normally so /pairing/start can call the backend pairing contract; native confirmPairing only accepts a server-issued authToken after Web confirmation."
            })),
            "confirmPairing" => {
                let params: ProdConfirmPairingParams =
                    serde_json::from_value(request.params).map_err(|err| err.to_string())?;
                self.config.store_auth_token(params.auth_token)?;
                self.core = None;
                Ok(self.health())
            }
            "createAgentIdentity" => {
                let params: ProdCreateAgentIdentityParams =
                    serde_json::from_value(request.params).map_err(|err| err.to_string())?;
                let (agent, declared_operator_public_key) = {
                    let core = self.core()?;
                    let operator = core
                        .open_session(params.operator_slug.clone())
                        .map_err(|err| err.to_string())?;
                    let agent = operator
                        .create_and_publish_agent_identity(params.agent_slug)
                        .map_err(|err| err.to_string())?;
                    let declared_operator_public_key = core
                        .identity_declared_operator_public_key(agent.slug())
                        .map_err(|err| err.to_string())?
                        .ok_or_else(|| {
                            "agent identity missing declared operator public key after publication"
                                .to_string()
                        })?;
                    (agent, declared_operator_public_key)
                };
                let agent_slug = agent.slug().to_string();
                self.sessions
                    .insert(prod_session_handle(&agent_slug), agent);
                Ok(serde_json::json!({
                    "ok": true,
                    "operatorSlug": params.operator_slug,
                    "agentSlug": agent_slug,
                    "identityType": "agent",
                    "declaredOperatorPublicKey": declared_operator_public_key,
                    "published": true
                }))
            }
            "openAgentSession" => {
                let params: ProdOpenAgentSessionParams =
                    serde_json::from_value(request.params).map_err(|err| err.to_string())?;
                let handle = prod_session_handle(&params.agent_slug);
                if !self.sessions.contains_key(&handle) {
                    let session = {
                        let core = self.core()?;
                        core.open_session(params.agent_slug.clone())
                            .map_err(|err| err.to_string())?
                    };
                    self.sessions.insert(handle.clone(), session);
                }
                Ok(serde_json::json!({
                    "handle": handle,
                    "slug": params.agent_slug
                }))
            }
            other => Err(format!(
                "production native profile is not ready for {other}: {}",
                self.unavailable_reason()
            )),
        }
    }

    fn health(&self) -> serde_json::Value {
        if self.config.missing.is_empty() && self.config.validation_error().is_none() {
            serde_json::json!({
                "connected": false,
                "status": "pairing_required",
                "deviceId": "native-sidecar-prod",
                "mode": "prod",
                "version": env!("CARGO_PKG_VERSION"),
                "serverUrl": self.config.server_url,
                "authTokenSource": self.config.auth_token_source,
                "reason": PROD_PAIRING_AND_SYNC_REASON,
                "blockedBy": ["backend_pairing_contract", "space_invite_sync_contract"],
                "nextAction": PROD_PAIRING_AND_SYNC_NEXT_ACTION
            })
        } else {
            serde_json::json!({
                "connected": false,
                "status": "unavailable",
                "deviceId": "native-sidecar-prod",
                "mode": "prod",
                "version": env!("CARGO_PKG_VERSION"),
                "reason": self.unavailable_reason(),
                "missingConfig": self.config.missing,
                "blockedBy": self.config.blocked_by(),
                "nextAction": self.config.next_action()
            })
        }
    }

    fn unavailable_reason(&self) -> String {
        if let Some(error) = self.config.validation_error() {
            error
        } else if self.config.missing.is_empty() {
            PROD_PAIRING_AND_SYNC_REASON.to_string()
        } else {
            format!(
                "production native profile missing required configuration: {}",
                self.config.missing.join(", ")
            )
        }
    }

    fn core(&mut self) -> Result<&NativeCore, String> {
        if self.core.is_none() {
            self.core = Some(
                NativeCore::for_prod(self.config.to_native_config()?)
                    .map_err(|err| err.to_string())?,
            );
        }
        self.core
            .as_ref()
            .ok_or_else(|| "production native core was not initialized".to_string())
    }
}

struct ProdProfileConfig {
    server_url: Option<String>,
    database_path: Option<String>,
    auth_token: Option<String>,
    auth_token_source: Option<&'static str>,
    auth_token_error: Option<String>,
    missing: Vec<&'static str>,
}

impl ProdProfileConfig {
    fn from_env() -> Self {
        let server_url = nonempty_env("AGENT_CORE_SERVER_URL");
        let database_path = nonempty_env("AGENT_CORE_DATABASE_PATH");
        let (auth_token, auth_token_source, auth_token_error) =
            prod_auth_token_from_env_or_keychain();
        let mut missing = Vec::new();
        if server_url.is_none() {
            missing.push("AGENT_CORE_SERVER_URL");
        }
        if database_path.is_none() {
            missing.push("AGENT_CORE_DATABASE_PATH");
        }
        if auth_token.is_none() {
            missing.push("AGENT_CORE_AUTH_TOKEN");
        }
        Self {
            server_url,
            database_path,
            auth_token,
            auth_token_source,
            auth_token_error,
            missing,
        }
    }

    fn store_auth_token(&mut self, auth_token: String) -> Result<(), String> {
        let auth_token = auth_token.trim();
        if auth_token.is_empty() {
            return Err("production auth token cannot be empty".to_string());
        }
        let source = match store_prod_auth_token_in_keychain(auth_token) {
            Ok(()) => "keychain",
            Err(error) if auth_token_keychain_is_unsupported(&error.to_string()) => "memory",
            Err(error) => return Err(error.to_string()),
        };
        self.auth_token = Some(auth_token.to_string());
        self.auth_token_source = Some(source);
        self.auth_token_error = None;
        self.refresh_missing();
        Ok(())
    }

    fn to_native_config(&self) -> Result<NativeProdConfig, String> {
        let server_url = self
            .server_url
            .as_deref()
            .ok_or_else(|| self.missing_reason())?;
        let database_path = self
            .database_path
            .as_deref()
            .ok_or_else(|| self.missing_reason())?;
        let auth_token = self
            .auth_token
            .as_deref()
            .ok_or_else(|| self.missing_reason())?;
        NativeProdConfig::new(server_url, database_path, auth_token).map_err(|err| err.to_string())
    }

    fn validation_error(&self) -> Option<String> {
        if let Some(error) = &self.auth_token_error {
            return Some(error.clone());
        }
        if self.missing.is_empty() {
            self.to_native_config().err()
        } else {
            None
        }
    }

    fn missing_reason(&self) -> String {
        format!(
            "production native profile missing required configuration: {}",
            self.missing.join(", ")
        )
    }

    fn refresh_missing(&mut self) {
        let mut missing = Vec::new();
        if self.server_url.is_none() {
            missing.push("AGENT_CORE_SERVER_URL");
        }
        if self.database_path.is_none() {
            missing.push("AGENT_CORE_DATABASE_PATH");
        }
        if self.auth_token.is_none() {
            missing.push("AGENT_CORE_AUTH_TOKEN");
        }
        self.missing = missing;
    }

    fn blocked_by(&self) -> Vec<&'static str> {
        if self.auth_token_error.is_some() {
            return vec!["AGENT_CORE_AUTH_TOKEN", "keychain_auth_token_read"];
        }
        self.missing.clone()
    }

    fn next_action(&self) -> &'static str {
        if self.missing.contains(&"AGENT_CORE_AUTH_TOKEN") || self.auth_token_error.is_some() {
            "Confirm server pairing and pass the server-issued authToken to local /pairing/confirm."
        } else if !self.missing.is_empty() {
            "Start the sidecar through the daemon so AGENT_CORE_SERVER_URL and AGENT_CORE_DATABASE_PATH defaults are provided."
        } else {
            PROD_PAIRING_AND_SYNC_NEXT_ACTION
        }
    }
}

#[derive(serde::Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct ProdOpenAgentSessionParams {
    agent_slug: String,
}

#[derive(serde::Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct ProdCreateAgentIdentityParams {
    operator_slug: String,
    agent_slug: String,
}

#[derive(serde::Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct ProdConfirmPairingParams {
    auth_token: String,
}

fn prod_session_handle(slug: &str) -> String {
    format!("session:{slug}")
}

fn prod_auth_token_from_env_or_keychain() -> (Option<String>, Option<&'static str>, Option<String>)
{
    if let Some(token) = nonempty_env("AGENT_CORE_AUTH_TOKEN") {
        return (Some(token), Some("env"), None);
    }
    match load_prod_auth_token_from_keychain() {
        Ok(Some(token)) => (Some(token), Some("keychain"), None),
        Ok(None) => (None, None, None),
        Err(error) => (None, None, Some(error.to_string())),
    }
}

fn auth_token_keychain_is_unsupported(error: &str) -> bool {
    error.contains(
        "production auth token Keychain storage requires agent-native built with apple-keychain",
    )
}

fn nonempty_env(name: &str) -> Option<String> {
    std::env::var(name)
        .ok()
        .filter(|value| !value.trim().is_empty())
}

#[cfg(test)]
mod prod_profile_tests {
    use super::{ProdConfirmPairingParams, ProdCreateAgentIdentityParams, ProdProfileConfig};
    use serde_json::json;

    #[test]
    fn validation_error_reports_auth_token_keychain_read_failures() {
        let config = ProdProfileConfig {
            server_url: Some("https://api.example.test".to_string()),
            database_path: Some("/tmp/agent-core.sqlite".to_string()),
            auth_token: None,
            auth_token_source: None,
            auth_token_error: Some(
                "failed to read production auth token from Keychain: denied".to_string(),
            ),
            missing: vec!["AGENT_CORE_AUTH_TOKEN"],
        };

        assert_eq!(
            config.validation_error().as_deref(),
            Some("failed to read production auth token from Keychain: denied")
        );
    }

    #[test]
    fn prod_rpc_params_reject_unknown_fields() {
        let confirm = serde_json::from_value::<ProdConfirmPairingParams>(json!({
            "authToken": "stored-token",
            "operatorBootstrap": {
                "kind": "restore_or_enroll"
            }
        }));
        assert!(confirm.is_err());

        let identity = serde_json::from_value::<ProdCreateAgentIdentityParams>(json!({
            "operatorSlug": "alice",
            "agentSlug": "alice-agent",
            "rootSecretKey": "must-not-be-ignored"
        }));
        assert!(identity.is_err());
    }
}

fn ok_response(id: u64, result: serde_json::Value) -> RpcResponse {
    RpcResponse {
        id,
        ok: true,
        result: Some(result),
        error: None,
    }
}

fn error_response(id: u64, error: String) -> RpcResponse {
    RpcResponse {
        id,
        ok: false,
        result: None,
        error: Some(error),
    }
}

fn write_response(writer: &mut impl Write, response: &RpcResponse) -> Result<(), String> {
    serde_json::to_writer(&mut *writer, response).map_err(|err| err.to_string())?;
    writer.write_all(b"\n").map_err(|err| err.to_string())?;
    writer.flush().map_err(|err| err.to_string())
}
