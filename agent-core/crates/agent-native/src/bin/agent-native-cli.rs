use std::env;

#[cfg(feature = "dev-tools")]
use agent_native::{NativeCore, NativeSession};
#[cfg(feature = "dev-tools")]
use client::api::SignupCommand;
#[cfg(feature = "dev-tools")]
use client::domain::IdentityRecord;
#[cfg(feature = "dev-tools")]
use types::IdentityType;

#[derive(serde::Serialize)]
struct ErrorResponse {
    ok: bool,
    error: String,
}

#[derive(serde::Serialize)]
struct HealthResponse {
    ok: bool,
    mode: &'static str,
    version: &'static str,
}

#[cfg(feature = "dev-tools")]
#[derive(serde::Serialize)]
struct AgentIdentityResponse {
    ok: bool,
    operator_slug: String,
    agent_slug: String,
    identity_type: String,
    declared_operator_public_key: Option<String>,
}

#[cfg(feature = "dev-tools")]
#[derive(serde::Serialize)]
struct SessionResponse {
    ok: bool,
    handle: String,
    slug: String,
}

#[cfg(feature = "dev-tools")]
#[derive(serde::Serialize)]
struct SyncResponse {
    ok: bool,
    certs_updated: usize,
    invitations_updated: usize,
    spaces_updated: usize,
    messages_processed: usize,
}

#[cfg(feature = "dev-tools")]
#[derive(serde::Serialize)]
struct PendingMessagesResponse {
    ok: bool,
    messages: Vec<OpenedMessageResponse>,
}

#[cfg(feature = "dev-tools")]
#[derive(serde::Serialize)]
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

#[cfg(feature = "dev-tools")]
#[derive(serde::Serialize)]
struct MessageRefResponse {
    ok: bool,
    message_id: String,
}

#[cfg(feature = "dev-tools")]
#[derive(serde::Serialize)]
struct SnapshotResponse {
    ok: bool,
    handle: String,
    slug: String,
}

fn main() {
    if let Err(error) = run() {
        print_json(&ErrorResponse { ok: false, error });
        std::process::exit(1);
    }
}

fn run() -> Result<(), String> {
    let mut args = env::args().skip(1);
    let command = args.next().unwrap_or_else(|| "help".to_string());
    match command.as_str() {
        "health" => {
            print_json(&HealthResponse {
                ok: true,
                mode: if cfg!(feature = "dev-tools") {
                    "dev_mock"
                } else {
                    "release"
                },
                version: env!("CARGO_PKG_VERSION"),
            });
            Ok(())
        }
        #[cfg(feature = "dev-tools")]
        "dev-create-agent-identity" => {
            let operator_slug = read_flag(&mut args, "--operator")?;
            let agent_slug = read_flag(&mut args, "--agent")?;
            let core = NativeCore::for_dev_mock().map_err(|err| err.to_string())?;
            let operator = NativeSession::from_session_for_bridge(
                core.sdk_for_bridge()
                    .signup(SignupCommand {
                        slug: operator_slug.clone(),
                        identity_type: IdentityType::Human,
                    })
                    .map_err(|err| err.to_string())?,
            );
            let agent = operator
                .create_agent_identity(agent_slug.clone())
                .map_err(|err| err.to_string())?;
            let identities = core
                .sdk_for_bridge()
                .list_identities()
                .map_err(|err| err.to_string())?;
            let record = identities
                .iter()
                .find(|identity: &&IdentityRecord| identity.slug == agent.slug())
                .ok_or_else(|| "agent identity not found after create".to_string())?;
            print_json(&AgentIdentityResponse {
                ok: true,
                operator_slug,
                agent_slug: agent.slug().to_string(),
                identity_type: format!("{:?}", record.identity_type).to_lowercase(),
                declared_operator_public_key: record
                    .identity_cert
                    .declared_operator_public_key
                    .clone(),
            });
            Ok(())
        }
        #[cfg(feature = "dev-tools")]
        "dev-open-agent-session" => {
            let slug = read_flag(&mut args, "--slug")?;
            let session = dev_signup_session(&slug, IdentityType::Agent)?;
            print_json(&SessionResponse {
                ok: true,
                handle: format!("dev:{}", session.slug()),
                slug: session.slug().to_string(),
            });
            Ok(())
        }
        #[cfg(feature = "dev-tools")]
        "dev-sync-once" => {
            let handle = read_flag(&mut args, "--handle")?;
            let slug = slug_from_handle(&handle)?;
            let session = dev_signup_session(&slug, IdentityType::Agent)?;
            let report = session.sync_once().map_err(|err| err.to_string())?;
            print_json(&SyncResponse {
                ok: true,
                certs_updated: report.certs_updated,
                invitations_updated: report.invitations_updated,
                spaces_updated: report.spaces_updated,
                messages_processed: report.messages_processed,
            });
            Ok(())
        }
        #[cfg(feature = "dev-tools")]
        "dev-process-pending" => {
            let handle = read_flag(&mut args, "--handle")?;
            let slug = slug_from_handle(&handle)?;
            let session = dev_signup_session(&slug, IdentityType::Agent)?;
            let _processed = session
                .process_pending_messages()
                .map_err(|err| err.to_string())?;
            print_json(&PendingMessagesResponse {
                ok: true,
                messages: Vec::new(),
            });
            Ok(())
        }
        #[cfg(feature = "dev-tools")]
        "dev-send-channel-text" => {
            let handle = read_flag(&mut args, "--handle")?;
            let _space_id = read_flag(&mut args, "--space")?;
            let _channel_id = read_flag(&mut args, "--channel")?;
            let _body = read_flag(&mut args, "--body")?;
            let rest: Vec<String> = args.collect();
            let _thread_root_id = read_optional_flag(&rest, "--thread-root");
            let _reply_to_id = read_optional_flag(&rest, "--reply-to");
            let _slug = slug_from_handle(&handle)?;
            print_json(&MessageRefResponse {
                ok: true,
                message_id: dev_message_id(),
            });
            Ok(())
        }
        #[cfg(feature = "dev-tools")]
        "dev-send-direct-text" => {
            let handle = read_flag(&mut args, "--handle")?;
            let _recipient = read_flag(&mut args, "--recipient")?;
            let _body = read_flag(&mut args, "--body")?;
            let rest: Vec<String> = args.collect();
            let _reply_to_id = read_optional_flag(&rest, "--reply-to");
            let _slug = slug_from_handle(&handle)?;
            print_json(&MessageRefResponse {
                ok: true,
                message_id: dev_message_id(),
            });
            Ok(())
        }
        #[cfg(feature = "dev-tools")]
        "dev-snapshot" => {
            let handle = read_flag(&mut args, "--handle")?;
            let slug = slug_from_handle(&handle)?;
            print_json(&SnapshotResponse {
                ok: true,
                handle,
                slug,
            });
            Ok(())
        }
        "help" | "--help" | "-h" => {
            eprintln!(
                "Usage:\n  agent-native-cli health\n  agent-native-cli dev-create-agent-identity --operator <slug> --agent <slug>\n  agent-native-cli dev-open-agent-session --slug <slug>\n  agent-native-cli dev-sync-once --handle <handle>\n  agent-native-cli dev-process-pending --handle <handle>"
            );
            Ok(())
        }
        other => Err(format!("unknown command: {other}")),
    }
}

#[cfg(feature = "dev-tools")]
fn dev_signup_session(slug: &str, identity_type: IdentityType) -> Result<NativeSession, String> {
    let core = NativeCore::for_dev_mock().map_err(|err| err.to_string())?;
    dev_signup_session_for_core(&core, slug, identity_type)
}

#[cfg(feature = "dev-tools")]
fn dev_signup_session_for_core(
    core: &NativeCore,
    slug: &str,
    identity_type: IdentityType,
) -> Result<NativeSession, String> {
    if identity_type == IdentityType::Agent {
        let operator_slug = format!("dev-operator-{slug}");
        let operator = NativeSession::from_session_for_bridge(
            core.sdk_for_bridge()
                .signup(SignupCommand {
                    slug: operator_slug,
                    identity_type: IdentityType::Human,
                })
                .map_err(|err| err.to_string())?,
        );
        return operator
            .create_agent_identity(slug.to_string())
            .map_err(|err| err.to_string());
    }
    let session = core
        .sdk_for_bridge()
        .signup(SignupCommand {
            slug: slug.to_string(),
            identity_type,
        })
        .map_err(|err| err.to_string())?;
    Ok(NativeSession::from_session_for_bridge(session))
}

#[cfg(feature = "dev-tools")]
fn slug_from_handle(handle: &str) -> Result<String, String> {
    handle
        .strip_prefix("dev:")
        .filter(|slug| !slug.is_empty())
        .map(str::to_string)
        .ok_or_else(|| format!("unsupported session handle: {handle}"))
}

#[cfg(feature = "dev-tools")]
fn dev_message_id() -> String {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or_default();
    format!("dev_msg_{nanos}")
}

#[cfg(feature = "dev-tools")]
fn read_flag(args: &mut impl Iterator<Item = String>, name: &str) -> Result<String, String> {
    while let Some(arg) = args.next() {
        if arg == name {
            return args
                .next()
                .ok_or_else(|| format!("missing value for {name}"));
        }
    }
    Err(format!("missing required flag {name}"))
}

#[cfg(feature = "dev-tools")]
fn read_optional_flag(args: &[String], name: &str) -> Option<String> {
    let mut args = args.iter();
    while let Some(arg) = args.next() {
        if arg == name {
            return args.next().cloned();
        }
    }
    None
}

fn print_json(value: &impl serde::Serialize) {
    println!(
        "{}",
        serde_json::to_string(value).expect("JSON serialization should not fail")
    );
}
