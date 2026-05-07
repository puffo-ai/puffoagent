#!/usr/bin/env bash
set -euo pipefail
umask 077

cleanup_tmp_dir=""
cleanup_bootstrap_tmp() {
  if [ -n "${cleanup_tmp_dir:-}" ]; then
    rm -rf "$cleanup_tmp_dir"
  fi
}
trap cleanup_bootstrap_tmp EXIT

required_node_major=20
download_node_major="${AGENT_CORE_NODE_MAJOR:-22}"
package_spec="${AGENT_CORE_PACKAGE:-@puffo-ai/agent-core}"
npm_run_scripts="${AGENT_CORE_NPM_RUN_SCRIPTS:-0}"
install_prefix_override="${AGENT_CORE_INSTALL_PREFIX:-}"
node_dir_override="${AGENT_CORE_NODE_DIR:-}"
node_dist_base_override="${AGENT_CORE_NODE_DIST_BASE:-}"

usage() {
  cat <<EOF
Usage: bootstrap-macos.sh [options]

Options:
  --package <spec>         npm package spec to install (default: @puffo-ai/agent-core)
  --run-scripts            allow npm lifecycle scripts for trusted package specs
  --no-run-scripts         install with --ignore-scripts (default)
  --install-prefix <path>  user-local npm prefix (default: \$HOME/.agent-core/npm)
  --node-dir <path>        user-local Node.js directory (default: \$HOME/.agent-core/node)
  --node-major <major>     Node.js major to download when needed (default: 22)
  --node-dist-base <url>   HTTPS Node.js distribution base URL override
  -h, --help               print this help
EOF
}

require_arg() {
  local option="$1"
  local value="${2:-}"
  if [ -z "$value" ]; then
    echo "${option} requires a value." >&2
    exit 1
  fi
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --package)
        require_arg "$1" "${2:-}"
        package_spec="$2"
        shift 2
        ;;
      --run-scripts)
        npm_run_scripts=1
        shift
        ;;
      --no-run-scripts)
        npm_run_scripts=0
        shift
        ;;
      --install-prefix)
        require_arg "$1" "${2:-}"
        install_prefix_override="$2"
        shift 2
        ;;
      --node-dir)
        require_arg "$1" "${2:-}"
        node_dir_override="$2"
        shift 2
        ;;
      --node-major)
        require_arg "$1" "${2:-}"
        download_node_major="$2"
        shift 2
        ;;
      --node-dist-base)
        require_arg "$1" "${2:-}"
        node_dist_base_override="$2"
        shift 2
        ;;
      -h | --help)
        usage
        exit 0
        ;;
      --)
        shift
        if [ "$#" -gt 0 ]; then
          echo "Unexpected positional arguments: $*" >&2
          exit 1
        fi
        ;;
      -*)
        echo "Unknown bootstrap option: $1" >&2
        usage >&2
        exit 1
        ;;
      *)
        echo "Unexpected positional argument: $1" >&2
        usage >&2
        exit 1
        ;;
    esac
  done
}

has_command() {
  command -v "$1" >/dev/null 2>&1
}

strip_trailing_slashes() {
  local value="$1"
  while [ "$value" != "/" ] && [ "${value%/}" != "$value" ]; do
    value="${value%/}"
  done
  printf '%s\n' "$value"
}

assert_no_symlink_components() {
  local value="$1"
  local label="$2"
  local normalized="$3"
  local remainder
  local component
  local current=""

  case "$normalized" in
    /*)
      ;;
    *)
      return
      ;;
  esac

  remainder="${normalized#/}"
  while [ -n "$remainder" ]; do
    component="${remainder%%/*}"
    if [ "$component" = "$remainder" ]; then
      remainder=""
    else
      remainder="${remainder#*/}"
    fi

    if [ -z "$component" ] || [ "$component" = "." ]; then
      continue
    fi
    if [ "$component" = ".." ]; then
      echo "Refusing path traversal in ${label}: ${value}" >&2
      exit 1
    fi

    if [ -z "$current" ]; then
      current="/${component}"
    else
      current="${current}/${component}"
    fi

    if [ -L "$current" ]; then
      echo "Refusing symlinked ${label}: ${value}" >&2
      exit 1
    fi
    if [ ! -e "$current" ]; then
      return
    fi
  done
}

assert_safe_user_dir() {
  local value="$1"
  local label="$2"
  local normalized
  local home_dir
  local agent_dir

  normalized="$(strip_trailing_slashes "$value")"
  case "$normalized" in
    "" | "." | ".." | "/" | "/Applications" | "/Library" | "/System" | "/bin" | "/sbin" | "/usr" | "/usr/bin" | "/usr/local" | "/opt" | "/opt/homebrew" | "/private" | "/var" | "/tmp")
      echo "Refusing unsafe ${label}: ${value}" >&2
      exit 1
      ;;
  esac
  case "$normalized" in
    /*)
      ;;
    *)
      echo "Refusing relative ${label}: ${value}" >&2
      exit 1
      ;;
  esac
  assert_no_symlink_components "$value" "$label" "$normalized"

  if [ -n "${HOME:-}" ]; then
    home_dir="$(strip_trailing_slashes "$HOME")"
    agent_dir="${home_dir}/.agent-core"
    case "$normalized" in
      "$home_dir" | "$agent_dir")
        echo "Refusing unsafe ${label}: ${value}" >&2
        exit 1
        ;;
    esac
  fi
}

assert_safe_node_dir() {
  local value="$1"
  local normalized
  local home_dir
  local agent_dir

  assert_safe_user_dir "$value" "Node.js install directory"
  if [ -n "${HOME:-}" ]; then
    normalized="$(strip_trailing_slashes "$value")"
    home_dir="$(strip_trailing_slashes "$HOME")"
    agent_dir="${home_dir}/.agent-core"
    case "$normalized" in
      "$agent_dir/npm" | "$agent_dir/tmp")
        echo "Refusing unsafe Node.js install directory: ${value}" >&2
        exit 1
        ;;
    esac
  fi
}

ensure_macos() {
  if [ "$(uname -s)" = "Darwin" ]; then
    return
  fi

  echo "This bootstrap is for macOS. Install @puffo-ai/agent-core with npm on this system instead." >&2
  exit 1
}

default_install_prefix() {
  if [ -n "$install_prefix_override" ]; then
    printf '%s\n' "$install_prefix_override"
    return
  fi

  if [ -z "${HOME:-}" ]; then
    echo "HOME is required to choose a user-local agent-core install directory." >&2
    exit 1
  fi

  printf '%s\n' "${HOME}/.agent-core/npm"
}

default_node_dir() {
  if [ -n "$node_dir_override" ]; then
    printf '%s\n' "$node_dir_override"
    return
  fi

  if [ -z "${HOME:-}" ]; then
    echo "HOME is required to choose a user-local Node.js install directory." >&2
    exit 1
  fi

  printf '%s\n' "${HOME}/.agent-core/node"
}

node_major() {
  node -p 'Number(process.versions.node.split(".")[0])' 2>/dev/null || true
}

verify_node_version() {
  if has_command node && [ "$(node_major)" -ge "$required_node_major" ] 2>/dev/null; then
    return
  fi

  cat >&2 <<EOF
Node.js ${required_node_major}+ is required before installing agent-core.
Install Node.js from https://nodejs.org/ or let this script download a supported macOS tarball.
EOF
  exit 1
}

validate_download_node_major() {
  case "$download_node_major" in
    "" | *[!0-9]*)
      echo "AGENT_CORE_NODE_MAJOR must be an integer >= ${required_node_major}." >&2
      exit 1
      ;;
  esac
  if [ "$download_node_major" -lt "$required_node_major" ]; then
    echo "AGENT_CORE_NODE_MAJOR must be >= ${required_node_major}." >&2
    exit 1
  fi
}

validate_package_spec() {
  case "$package_spec" in
    "" | *[[:space:]]*)
      echo "AGENT_CORE_PACKAGE must be a non-empty npm package spec without whitespace." >&2
      exit 1
      ;;
    -*)
      echo "AGENT_CORE_PACKAGE must be a package spec, not an npm option." >&2
      exit 1
      ;;
  esac
}

validate_npm_run_scripts() {
  case "$npm_run_scripts" in
    0 | 1)
      ;;
    *)
      echo "AGENT_CORE_NPM_RUN_SCRIPTS must be 0 or 1." >&2
      exit 1
      ;;
  esac
}

ensure_node() {
  if has_command node && [ "$(node_major)" -ge "$required_node_major" ] 2>/dev/null; then
    return
  fi

  local node_dir
  node_dir="$(default_node_dir)"
  if [ -x "${node_dir}/bin/node" ]; then
    export PATH="${node_dir}/bin:${PATH}"
    if has_command node && [ "$(node_major)" -ge "$required_node_major" ] 2>/dev/null; then
      return
    fi
  fi

  install_user_node "$node_dir"
}

node_platform() {
  case "$(uname -m)" in
    arm64)
      printf '%s\n' "darwin-arm64"
      ;;
    x86_64)
      printf '%s\n' "darwin-x64"
      ;;
    *)
      echo "Unsupported macOS CPU architecture for automatic Node.js install: $(uname -m)" >&2
      exit 1
      ;;
  esac
}

node_dist_base_url() {
  if [ -n "$node_dist_base_override" ]; then
    local value
    value="$(strip_trailing_slashes "$node_dist_base_override")"
    case "$value" in
      *[[:space:]]*)
        echo "AGENT_CORE_NODE_DIST_BASE must be an https:// URL without whitespace." >&2
        exit 1
        ;;
      https://?*)
        printf '%s\n' "$value"
        ;;
      *)
        echo "AGENT_CORE_NODE_DIST_BASE must be an https:// URL." >&2
        exit 1
        ;;
    esac
    return
  fi

  printf 'https://nodejs.org/dist/latest-v%s.x\n' "$download_node_major"
}

install_user_node() {
  local node_dir="$1"
  local platform
  local base_url
  local temp_root
  local shasums
  local archive_name
  local expected_sha
  local actual_sha
  local tmp_dir
  local archive
  local stage

  assert_safe_node_dir "$node_dir"
  if [ -z "${HOME:-}" ]; then
    echo "HOME is required to choose a user-local temporary download directory." >&2
    exit 1
  fi
  temp_root="${HOME}/.agent-core/tmp"
  assert_safe_user_dir "$temp_root" "temporary download directory"

  platform="$(node_platform)"
  base_url="$(node_dist_base_url)"

  if ! has_command curl || ! has_command tar || ! has_command shasum; then
    cat >&2 <<EOF
Automatic Node.js install requires curl, tar, and shasum.
Install Node.js ${required_node_major}+ from https://nodejs.org/ and rerun this script.
EOF
    exit 1
  fi

  echo "Installing Node.js ${download_node_major}.x for ${platform} into ${node_dir}..."
  shasums="$(curl -fsSL --proto '=https' --proto-redir '=https' "${base_url}/SHASUMS256.txt")"
  archive_name="$(
    printf '%s\n' "$shasums" |
      awk -v platform="$platform" '$2 ~ ("^node-v[0-9.]+-" platform "\\.tar\\.gz$") { print $2; exit }'
  )"
  if [ -z "$archive_name" ]; then
    echo "Could not find a Node.js ${download_node_major}.x ${platform} tarball in ${base_url}/SHASUMS256.txt." >&2
    exit 1
  fi
  expected_sha="$(
    printf '%s\n' "$shasums" |
      awk -v archive="$archive_name" '$2 == archive { print $1; exit }'
  )"
  if [ -z "$expected_sha" ]; then
    echo "Could not find a checksum for ${archive_name}." >&2
    exit 1
  fi

  mkdir -p "$temp_root"
  assert_safe_user_dir "$temp_root" "temporary download directory"
  tmp_dir="$(mktemp -d "${temp_root}/node.XXXXXX")"
  cleanup_tmp_dir="$tmp_dir"
  archive="${tmp_dir}/${archive_name}"
  stage="${tmp_dir}/node"
  curl -fsSL --proto '=https' --proto-redir '=https' -o "$archive" "${base_url}/${archive_name}"
  actual_sha="$(shasum -a 256 "$archive" | awk '{ print $1 }')"
  if [ "$actual_sha" != "$expected_sha" ]; then
    rm -rf "$tmp_dir"
    echo "Node.js checksum verification failed for ${archive_name}." >&2
    exit 1
  fi

  mkdir -p "$stage"
  tar -xzf "$archive" -C "$stage" --strip-components=1
  if [ ! -x "${stage}/bin/node" ] || [ ! -x "${stage}/bin/npm" ]; then
    rm -rf "$tmp_dir"
    echo "Downloaded Node.js archive did not contain node and npm binaries." >&2
    exit 1
  fi

  mkdir -p "$(dirname "$node_dir")"
  assert_safe_node_dir "$node_dir"
  rm -rf "$node_dir"
  mv "$stage" "$node_dir"
  rm -rf "$tmp_dir"
  cleanup_tmp_dir=""
  export PATH="${node_dir}/bin:${PATH}"
  verify_node_version
}

ensure_npm() {
  if has_command npm; then
    return
  fi
  echo "npm was not found after Node.js setup." >&2
  exit 1
}

resolve_agent_command() {
  local install_prefix="${1:-}"

  if [ -n "$install_prefix" ] && [ -x "${install_prefix}/bin/agent" ]; then
    printf '%s\n' "${install_prefix}/bin/agent"
    return
  fi

  cat >&2 <<EOF
agent-core was installed, but the expected agent binary was not found:

  ${install_prefix}/bin/agent

The bootstrap will not start a different agent binary from PATH. Inspect the npm
install output, then run:

  ${install_prefix}/bin/agent start
EOF
  exit 1
}

parse_args "$@"
ensure_macos
validate_download_node_major
validate_package_spec
validate_npm_run_scripts
install_prefix="$(default_install_prefix)"
assert_safe_user_dir "$install_prefix" "agent-core install prefix"
ensure_node
ensure_npm

echo "Installing ${package_spec} into ${install_prefix}..."
mkdir -p "$install_prefix"
assert_safe_user_dir "$install_prefix" "agent-core install prefix"
npm_install_args=(-g --prefix "$install_prefix")
if [ "$npm_run_scripts" = "0" ]; then
  npm_install_args+=(--ignore-scripts)
fi
npm install "${npm_install_args[@]}" "${package_spec}"

echo "Starting local agent daemon..."
agent_cmd="$(resolve_agent_command "$install_prefix")"
exec "$agent_cmd" start
