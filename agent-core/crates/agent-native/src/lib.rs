//! Rust boundary for the Node local agent runtime.
//!
//! This crate intentionally exposes use-case-shaped wrappers over the existing
//! `core` client SDK. It is the place where a future N-API layer should attach.
//! Node must not call crypto primitives or handle key material directly.

use std::path::PathBuf;

#[cfg(all(
    feature = "apple-keychain",
    any(target_os = "macos", target_os = "ios")
))]
use client::api::SdkConfig;
use client::api::{
    CreateAgentIdentityCommand, CreateChannelCommand, CreateSpaceCommand,
    InvitationResponseCommand, InviteCommand, SendMessageCommand,
};
use client::{ClientSdk, ClientSession, Result, SdkError};

#[cfg(all(
    feature = "apple-keychain",
    any(target_os = "macos", target_os = "ios")
))]
use client::ports::{CryptoPort, IdentityStorePort};
#[cfg(all(
    feature = "apple-keychain",
    any(target_os = "macos", target_os = "ios")
))]
use client::providers::{
    BlockingHttpTransport, HttpBody, HttpMethod, HttpRequest, HttpResponse, HttpServerProvider,
    HttpTransport, KeychainCryptoProvider, SdkServerProvider, SdkStoreProvider,
    SqliteStoreProvider, SystemRng,
};
#[cfg(all(
    feature = "apple-keychain",
    any(target_os = "macos", target_os = "ios")
))]
use crypto::client_api::{database_dek_from_slice, DatabaseDek, HttpRequestSigningInput};
#[cfg(all(
    feature = "apple-keychain",
    any(target_os = "macos", target_os = "ios")
))]
use security_framework::passwords::{
    generic_password, set_generic_password_options, PasswordOptions,
};
#[cfg(all(
    feature = "apple-keychain",
    any(target_os = "macos", target_os = "ios")
))]
use security_framework_sys::base::errSecItemNotFound;
#[cfg(all(
    feature = "apple-keychain",
    any(target_os = "macos", target_os = "ios")
))]
use std::{cell::RefCell, rc::Rc, time::SystemTime, time::UNIX_EPOCH};

#[cfg_attr(
    not(all(
        feature = "apple-keychain",
        any(target_os = "macos", target_os = "ios")
    )),
    allow(dead_code)
)]
pub struct NativeProdConfig {
    server_url: String,
    database_path: PathBuf,
    auth_token: String,
}

#[cfg(all(
    feature = "apple-keychain",
    any(target_os = "macos", target_os = "ios")
))]
pub fn load_prod_auth_token_from_keychain() -> Result<Option<String>> {
    match generic_password(prod_auth_token_keychain_options()) {
        Ok(bytes) => String::from_utf8(bytes)
            .map(|token| Some(token.trim().to_string()).filter(|token| !token.is_empty()))
            .map_err(|err| {
                SdkError::ProviderMisconfigured(format!(
                    "failed to decode production auth token from Keychain: {err}"
                ))
            }),
        Err(error) if error.code() == errSecItemNotFound => Ok(None),
        Err(error) => Err(keychain_auth_token_error("read", error)),
    }
}

#[cfg(not(all(
    feature = "apple-keychain",
    any(target_os = "macos", target_os = "ios")
)))]
pub fn load_prod_auth_token_from_keychain() -> Result<Option<String>> {
    Ok(None)
}

#[cfg(all(
    feature = "apple-keychain",
    any(target_os = "macos", target_os = "ios")
))]
pub fn store_prod_auth_token_in_keychain(token: &str) -> Result<()> {
    let token = token.trim();
    if token.is_empty() {
        return Err(SdkError::ProviderMisconfigured(
            "production auth token cannot be empty".to_string(),
        ));
    }
    let mut options = prod_auth_token_keychain_options();
    options.set_label("Agent Core production auth token");
    options.set_description("Server-issued bearer token for the local agent core sidecar");
    set_generic_password_options(token.as_bytes(), options)
        .map_err(|error| keychain_auth_token_error("write", error))
}

#[cfg(not(all(
    feature = "apple-keychain",
    any(target_os = "macos", target_os = "ios")
)))]
pub fn store_prod_auth_token_in_keychain(_token: &str) -> Result<()> {
    Err(SdkError::ProviderMisconfigured(
        "production auth token Keychain storage requires agent-native built with apple-keychain on macOS/iOS"
            .to_string(),
    ))
}

impl NativeProdConfig {
    pub fn new(
        server_url: impl Into<String>,
        database_path: impl Into<PathBuf>,
        auth_token: impl Into<String>,
    ) -> Result<Self> {
        let server_url = server_url.into().trim().to_string();
        let database_path = database_path.into();
        let auth_token = auth_token.into().trim().to_string();
        if server_url.is_empty() {
            return Err(SdkError::ProviderMisconfigured(
                "production native config requires server_url".to_string(),
            ));
        }
        if database_path.as_os_str().is_empty() {
            return Err(SdkError::ProviderMisconfigured(
                "production native config requires database_path".to_string(),
            ));
        }
        if auth_token.trim().is_empty() {
            return Err(SdkError::ProviderMisconfigured(
                "production native config requires auth_token".to_string(),
            ));
        }
        Ok(Self {
            server_url,
            database_path,
            auth_token,
        })
    }

    pub fn server_url(&self) -> &str {
        &self.server_url
    }
}

pub struct NativeCore {
    sdk: ClientSdk,
    #[cfg(all(
        feature = "apple-keychain",
        any(target_os = "macos", target_os = "ios")
    ))]
    http_signer_slug: Option<Rc<RefCell<Option<String>>>>,
}

impl NativeCore {
    pub fn from_sdk(sdk: ClientSdk) -> Self {
        Self {
            sdk,
            #[cfg(all(
                feature = "apple-keychain",
                any(target_os = "macos", target_os = "ios")
            ))]
            http_signer_slug: None,
        }
    }

    #[cfg(feature = "dev-tools")]
    pub fn for_dev_mock() -> Result<Self> {
        Ok(Self {
            sdk: ClientSdk::for_dev_mock()?,
            #[cfg(all(
                feature = "apple-keychain",
                any(target_os = "macos", target_os = "ios")
            ))]
            http_signer_slug: None,
        })
    }

    #[cfg(all(
        feature = "apple-keychain",
        any(target_os = "macos", target_os = "ios")
    ))]
    pub fn for_prod(config: NativeProdConfig) -> Result<Self> {
        // Pairing still stores a server-confirmed local token, but production
        // route auth now uses signed x-puffo-* headers instead of bearer auth.
        let _pairing_token = config.auth_token.as_str();
        let crypto = KeychainCryptoProvider::default_namespace()?;
        let database_dek = load_or_create_database_dek()?;
        let store =
            SqliteStoreProvider::open_sqlcipher_with_dek(&config.database_path, &database_dek)?;
        let http_signer_slug = Rc::new(RefCell::new(None));
        let transport = SignedHttpTransport::new(
            config.database_path.clone(),
            &database_dek,
            http_signer_slug.clone(),
            BlockingHttpTransport::default(),
        )?;
        let server = HttpServerProvider::with_transport(config.server_url.clone(), transport)?;
        Ok(Self {
            sdk: ClientSdk::from_providers(
                SdkConfig::prod(config.server_url),
                crypto,
                SdkServerProvider::http(server),
                SdkStoreProvider::sqlite(store),
            )?,
            http_signer_slug: Some(http_signer_slug),
        })
    }

    #[cfg(not(all(
        feature = "apple-keychain",
        any(target_os = "macos", target_os = "ios")
    )))]
    pub fn for_prod(_config: NativeProdConfig) -> Result<Self> {
        Err(SdkError::ProviderMisconfigured(
            "production native profile requires agent-native built with apple-keychain on macOS/iOS"
                .to_string(),
        ))
    }

    pub fn open_session(&self, slug: impl Into<String>) -> Result<NativeSession> {
        let slug = slug.into();
        #[cfg(all(
            feature = "apple-keychain",
            any(target_os = "macos", target_os = "ios")
        ))]
        self.set_http_signer_slug(&slug);
        let session = self
            .sdk
            .open_session(client::api::OpenSessionCommand { slug })?;
        Ok(NativeSession {
            session,
            #[cfg(all(
                feature = "apple-keychain",
                any(target_os = "macos", target_os = "ios")
            ))]
            http_signer_slug: self.http_signer_slug.clone(),
        })
    }

    #[cfg(all(
        feature = "apple-keychain",
        any(target_os = "macos", target_os = "ios")
    ))]
    fn set_http_signer_slug(&self, slug: &str) {
        if let Some(http_signer_slug) = &self.http_signer_slug {
            *http_signer_slug.borrow_mut() = Some(slug.to_string());
        }
    }

    #[cfg(feature = "dev-tools")]
    pub fn sdk_for_bridge(&self) -> &ClientSdk {
        &self.sdk
    }

    pub fn identity_declared_operator_public_key(&self, slug: &str) -> Result<Option<String>> {
        Ok(self
            .sdk
            .list_identities()?
            .into_iter()
            .find(|identity| identity.slug == slug)
            .and_then(|identity| identity.identity_cert.declared_operator_public_key))
    }
}

#[cfg(all(
    feature = "apple-keychain",
    any(target_os = "macos", target_os = "ios")
))]
struct SignedHttpTransport<T> {
    crypto: KeychainCryptoProvider,
    store: SqliteStoreProvider,
    http_signer_slug: Rc<RefCell<Option<String>>>,
    inner: T,
}

#[cfg(all(
    feature = "apple-keychain",
    any(target_os = "macos", target_os = "ios")
))]
impl<T> SignedHttpTransport<T> {
    fn new(
        database_path: PathBuf,
        database_dek: &DatabaseDek,
        http_signer_slug: Rc<RefCell<Option<String>>>,
        inner: T,
    ) -> Result<Self> {
        Ok(Self {
            crypto: KeychainCryptoProvider::default_namespace()?,
            store: SqliteStoreProvider::open_sqlcipher_with_dek(database_path, database_dek)?,
            http_signer_slug,
            inner,
        })
    }
}

#[cfg(all(
    feature = "apple-keychain",
    any(target_os = "macos", target_os = "ios")
))]
impl<T: HttpTransport> HttpTransport for SignedHttpTransport<T> {
    fn send(&self, mut request: HttpRequest) -> Result<HttpResponse> {
        let signer_slug = self.http_signer_slug.borrow().clone().ok_or_else(|| {
            SdkError::ProviderMisconfigured(
                "signed HTTP transport requires an active native session signer".to_string(),
            )
        })?;
        let session = self
            .store
            .load_identity(&signer_slug)?
            .ok_or_else(|| SdkError::LocalIdentityMissing(signer_slug.clone()))?;
        let timestamp_ms = now_ms()?;
        self.crypto.activate_identity(&session, timestamp_ms)?;
        let signing_path = http_request_signing_path(&request);
        let body = http_request_body_bytes(&request)?;
        let headers = self.crypto.sign_http_as_subkey(
            &signer_slug,
            HttpRequestSigningInput {
                method: http_method_name(request.method),
                path: &signing_path,
                timestamp_ms,
                body: &body,
            },
        )?;
        request.headers.retain(|(key, _)| {
            !key.eq_ignore_ascii_case("authorization") && !is_signed_http_header(key)
        });
        request.headers.push((
            "x-puffo-slug".to_string(),
            headers.slug().as_str().to_string(),
        ));
        request.headers.push((
            "x-puffo-signer-id".to_string(),
            headers.signer_id().to_string(),
        ));
        request.headers.push((
            "x-puffo-timestamp".to_string(),
            headers.timestamp_ms().to_string(),
        ));
        request.headers.push((
            "x-puffo-nonce".to_string(),
            headers.nonce().as_str().to_string(),
        ));
        request
            .headers
            .push(("x-puffo-signature".to_string(), headers.signature_b64u()));
        self.inner.send(request)
    }
}

#[cfg(all(
    feature = "apple-keychain",
    any(target_os = "macos", target_os = "ios")
))]
fn now_ms() -> Result<u64> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis() as u64)
        .map_err(|error| {
            SdkError::ProviderMisconfigured(format!("system clock before UNIX epoch: {error}"))
        })
}

#[cfg(all(
    feature = "apple-keychain",
    any(target_os = "macos", target_os = "ios")
))]
fn http_method_name(method: HttpMethod) -> &'static str {
    match method {
        HttpMethod::Delete => "DELETE",
        HttpMethod::Get => "GET",
        HttpMethod::Patch => "PATCH",
        HttpMethod::Post => "POST",
        HttpMethod::Put => "PUT",
    }
}

#[cfg(all(
    feature = "apple-keychain",
    any(target_os = "macos", target_os = "ios")
))]
fn http_request_body_bytes(request: &HttpRequest) -> Result<Vec<u8>> {
    match &request.body {
        Some(HttpBody::Json(value)) => serde_json::to_vec(value).map_err(SdkError::from),
        Some(HttpBody::Bytes(value)) => Ok(value.clone()),
        None => Ok(Vec::new()),
    }
}

#[cfg(all(
    feature = "apple-keychain",
    any(target_os = "macos", target_os = "ios")
))]
fn http_request_signing_path(request: &HttpRequest) -> String {
    let mut path = request.path.clone();
    if !request.query.is_empty() {
        path.push('?');
        for (index, (key, value)) in request.query.iter().enumerate() {
            if index > 0 {
                path.push('&');
            }
            path.push_str(&percent_encode(key));
            path.push('=');
            path.push_str(&percent_encode(value));
        }
    }
    path
}

#[cfg(all(
    feature = "apple-keychain",
    any(target_os = "macos", target_os = "ios")
))]
fn percent_encode(value: &str) -> String {
    let mut out = String::new();
    for byte in value.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' => {
                out.push(byte as char);
            }
            _ => {
                const HEX: &[u8; 16] = b"0123456789ABCDEF";
                out.push('%');
                out.push(HEX[(byte >> 4) as usize] as char);
                out.push(HEX[(byte & 0x0f) as usize] as char);
            }
        }
    }
    out
}

#[cfg(all(
    feature = "apple-keychain",
    any(target_os = "macos", target_os = "ios")
))]
fn is_signed_http_header(key: &str) -> bool {
    key.eq_ignore_ascii_case("x-puffo-slug")
        || key.eq_ignore_ascii_case("x-puffo-signer-id")
        || key.eq_ignore_ascii_case("x-puffo-timestamp")
        || key.eq_ignore_ascii_case("x-puffo-nonce")
        || key.eq_ignore_ascii_case("x-puffo-signature")
}

#[cfg(all(
    feature = "apple-keychain",
    any(target_os = "macos", target_os = "ios")
))]
const DATABASE_DEK_KEYCHAIN_SERVICE: &str = "agent-core.native";
#[cfg(all(
    feature = "apple-keychain",
    any(target_os = "macos", target_os = "ios")
))]
const DATABASE_DEK_KEYCHAIN_ACCOUNT: &str = "sqlcipher-dek-v1";
#[cfg(all(
    feature = "apple-keychain",
    any(target_os = "macos", target_os = "ios")
))]
const PROD_AUTH_TOKEN_KEYCHAIN_ACCOUNT: &str = "prod-auth-token-v1";

#[cfg(all(
    feature = "apple-keychain",
    any(target_os = "macos", target_os = "ios")
))]
fn load_or_create_database_dek() -> Result<DatabaseDek> {
    match generic_password(database_dek_keychain_options()) {
        Ok(bytes) => database_dek_from_slice(&bytes).map_err(SdkError::from),
        Err(error) if error.code() == errSecItemNotFound => create_database_dek(),
        Err(error) => Err(keychain_dek_error("read", error)),
    }
}

#[cfg(all(
    feature = "apple-keychain",
    any(target_os = "macos", target_os = "ios")
))]
fn create_database_dek() -> Result<DatabaseDek> {
    let mut rng = SystemRng;
    let database_dek = DatabaseDek::generate(&mut rng);
    let mut options = database_dek_keychain_options();
    options.set_label("Agent Core SQLCipher database key");
    options.set_description("Local agent core SQLCipher database encryption key");
    set_generic_password_options(database_dek.as_sqlcipher_key_bytes(), options)
        .map_err(|error| keychain_dek_error("write", error))?;
    Ok(database_dek)
}

#[cfg(all(
    feature = "apple-keychain",
    any(target_os = "macos", target_os = "ios")
))]
fn database_dek_keychain_options() -> PasswordOptions {
    let mut options = PasswordOptions::new_generic_password(
        DATABASE_DEK_KEYCHAIN_SERVICE,
        DATABASE_DEK_KEYCHAIN_ACCOUNT,
    );
    options.set_access_synchronized(Some(false));
    options
}

#[cfg(all(
    feature = "apple-keychain",
    any(target_os = "macos", target_os = "ios")
))]
fn prod_auth_token_keychain_options() -> PasswordOptions {
    let mut options = PasswordOptions::new_generic_password(
        DATABASE_DEK_KEYCHAIN_SERVICE,
        PROD_AUTH_TOKEN_KEYCHAIN_ACCOUNT,
    );
    options.set_access_synchronized(Some(false));
    options
}

#[cfg(all(
    feature = "apple-keychain",
    any(target_os = "macos", target_os = "ios")
))]
fn keychain_dek_error(operation: &str, error: security_framework::base::Error) -> SdkError {
    SdkError::ProviderMisconfigured(format!(
        "failed to {operation} SQLCipher database key from Keychain: {error}"
    ))
}

#[cfg(all(
    feature = "apple-keychain",
    any(target_os = "macos", target_os = "ios")
))]
fn keychain_auth_token_error(operation: &str, error: security_framework::base::Error) -> SdkError {
    SdkError::ProviderMisconfigured(format!(
        "failed to {operation} production auth token from Keychain: {error}"
    ))
}

#[cfg(test)]
mod prod_config_tests {
    use super::*;

    #[test]
    fn prod_config_trims_inputs() {
        let config = NativeProdConfig::new(
            " https://api.example.test/ ",
            "/tmp/agent-core.sqlite",
            " token ",
        )
        .expect("prod config");

        assert_eq!(config.server_url(), "https://api.example.test/");
        assert_eq!(config.auth_token, "token");
    }

    #[test]
    fn prod_config_rejects_empty_auth_token() {
        let error = match NativeProdConfig::new(
            "https://api.example.test",
            "/tmp/agent-core.sqlite",
            " ",
        ) {
            Ok(_) => panic!("empty auth token should be rejected"),
            Err(error) => error,
        };

        assert!(error
            .to_string()
            .contains("production native config requires auth_token"));
    }
}

pub struct NativeSession {
    session: ClientSession,
    #[cfg(all(
        feature = "apple-keychain",
        any(target_os = "macos", target_os = "ios")
    ))]
    http_signer_slug: Option<Rc<RefCell<Option<String>>>>,
}

impl NativeSession {
    pub fn from_session_for_bridge(session: ClientSession) -> Self {
        Self {
            session,
            #[cfg(all(
                feature = "apple-keychain",
                any(target_os = "macos", target_os = "ios")
            ))]
            http_signer_slug: None,
        }
    }

    pub fn slug(&self) -> &str {
        self.session.slug()
    }

    pub fn sync_once(&self) -> Result<client::domain::SyncReport> {
        self.set_http_signer_slug();
        self.session.sync_once()
    }

    pub fn process_pending_messages(&self) -> Result<usize> {
        self.set_http_signer_slug();
        self.session.process_pending_messages()
    }

    pub fn create_agent_identity(&self, slug: impl Into<String>) -> Result<NativeSession> {
        self.set_http_signer_slug();
        let session = self
            .session
            .create_agent_identity(CreateAgentIdentityCommand { slug: slug.into() })?;
        Ok(NativeSession {
            session,
            #[cfg(all(
                feature = "apple-keychain",
                any(target_os = "macos", target_os = "ios")
            ))]
            http_signer_slug: self.http_signer_slug.clone(),
        })
    }

    pub fn create_and_publish_agent_identity(
        &self,
        slug: impl Into<String>,
    ) -> Result<NativeSession> {
        self.set_http_signer_slug();
        let session = self
            .session
            .create_and_publish_agent_identity(CreateAgentIdentityCommand { slug: slug.into() })?;
        Ok(NativeSession {
            session,
            #[cfg(all(
                feature = "apple-keychain",
                any(target_os = "macos", target_os = "ios")
            ))]
            http_signer_slug: self.http_signer_slug.clone(),
        })
    }

    pub fn create_space(
        &self,
        space_id: impl Into<String>,
        name: impl Into<String>,
    ) -> Result<client::domain::SpaceProjection> {
        self.set_http_signer_slug();
        self.session.create_space(CreateSpaceCommand {
            space_id: space_id.into(),
            name: name.into(),
        })
    }

    pub fn create_channel(
        &self,
        space_id: impl Into<String>,
        channel_id: impl Into<String>,
        name: impl Into<String>,
        initial_member_slugs: Vec<String>,
    ) -> Result<client::domain::ChannelProjection> {
        self.set_http_signer_slug();
        self.session.create_channel(CreateChannelCommand {
            space_id: space_id.into(),
            channel_id: channel_id.into(),
            name: name.into(),
            initial_member_slugs,
        })
    }

    pub fn invite_to_channel(
        &self,
        space_id: impl Into<String>,
        channel_id: impl Into<String>,
        invitee_slug: impl Into<String>,
    ) -> Result<client::domain::InvitationRecord> {
        self.set_http_signer_slug();
        let mut invitations = self.session.invite(InviteCommand {
            space_id: space_id.into(),
            channel_id: Some(channel_id.into()),
            invitee_slug: invitee_slug.into(),
            role_on_accept: None,
            initial_channel_ids: Vec::new(),
        })?;
        invitations.pop().ok_or_else(|| {
            client::api::SdkError::SchemaMismatch(
                "channel invite returned no invitation".to_string(),
            )
        })
    }

    pub fn accept_channel_invite(
        &self,
        space_id: impl Into<String>,
        channel_id: impl Into<String>,
        invitation_event_id: impl Into<String>,
    ) -> Result<client::domain::InvitationRecord> {
        self.set_http_signer_slug();
        self.session.accept_invite(InvitationResponseCommand {
            space_id: space_id.into(),
            channel_id: Some(channel_id.into()),
            invitation_event_id: invitation_event_id.into(),
        })
    }

    pub fn list_messages(&self) -> Result<Vec<client::domain::MessageRecord>> {
        self.set_http_signer_slug();
        self.session.list_messages()
    }

    pub fn send_channel_text(
        &self,
        space_id: impl Into<String>,
        channel_id: impl Into<String>,
        body: impl Into<String>,
        thread_root_id: Option<String>,
        reply_to_id: Option<String>,
    ) -> Result<client::domain::MessageRecord> {
        self.set_http_signer_slug();
        let mut command = SendMessageCommand::channel_text(space_id, channel_id, body);
        if let Some(thread_root_id) = thread_root_id {
            command = command.with_thread(thread_root_id);
        }
        if let Some(reply_to_id) = reply_to_id {
            command = command.with_reply_to(reply_to_id);
        }
        self.session.send_channel_message(command)
    }

    pub fn send_direct_text(
        &self,
        recipient_slug: impl Into<String>,
        body: impl Into<String>,
        reply_to_id: Option<String>,
    ) -> Result<client::domain::MessageRecord> {
        self.set_http_signer_slug();
        let mut command = SendMessageCommand::direct_text(recipient_slug, body);
        if let Some(reply_to_id) = reply_to_id {
            command = command.with_reply_to(reply_to_id);
        }
        self.session.send_direct_message(command)
    }

    fn set_http_signer_slug(&self) {
        #[cfg(all(
            feature = "apple-keychain",
            any(target_os = "macos", target_os = "ios")
        ))]
        if let Some(http_signer_slug) = &self.http_signer_slug {
            *http_signer_slug.borrow_mut() = Some(self.slug().to_string());
        }
    }
}

#[cfg(all(test, feature = "dev-tools"))]
mod tests {
    use super::*;
    use client::api::SignupCommand;
    use client::domain::IdentityRecord;
    use types::IdentityType;

    #[test]
    fn dev_mock_can_construct_native_core() {
        let _core = NativeCore::for_dev_mock().expect("dev mock core");
    }

    #[test]
    fn dev_mock_can_create_agent_identity_from_operator_session() {
        let core = NativeCore::for_dev_mock().expect("dev mock core");
        let operator = NativeSession {
            session: core
                .sdk
                .signup(SignupCommand {
                    slug: "alice".to_string(),
                    identity_type: IdentityType::Human,
                })
                .expect("operator signup"),
        };
        let agent = operator
            .create_agent_identity("alice-agent")
            .expect("agent identity");
        assert_eq!(agent.slug(), "alice-agent");
        let identities = core.sdk.list_identities().expect("identities");
        let agent_record = identities
            .iter()
            .find(|record: &&IdentityRecord| record.slug == "alice-agent")
            .expect("agent record");
        assert_eq!(agent_record.identity_type, IdentityType::Agent);
        assert!(agent_record
            .identity_cert
            .declared_operator_public_key
            .is_some());
        assert_eq!(
            core.identity_declared_operator_public_key("alice-agent")
                .expect("declared operator key"),
            agent_record.identity_cert.declared_operator_public_key
        );
        assert_eq!(
            core.identity_declared_operator_public_key("missing-agent")
                .expect("missing identity lookup"),
            None
        );
    }
}
