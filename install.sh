#!/usr/bin/env sh
# FreeRide installer. Run with:
#
#   curl -sSL https://api.free-ride.xyz/install.sh | sh
#
# What this does:
#   1. Installs `uv` (Astral's Rust-based Python package manager) if it isn't already.
#   2. Uses `uv tool install` to install freeride-gateway into an isolated venv
#      and symlink the `freeride` binary into ~/.local/bin (which uv puts on PATH).
#   3. Verifies `freeride --version` works.
#
# Result: `freeride` works in every terminal, no venv juggling, no PATH dance.
#
# Why uv: modern, fast, handles the venv + PATH correctly out of the box.
# Same install pattern as bun.sh, astral.sh/uv, aider.chat.

set -e

print() { printf '%s\n' "$*"; }
err() { printf 'error: %s\n' "$*" >&2; exit 1; }

print "FreeRide installer"
print ""

# 1. Make sure we have uv. If we don't, install it via the official one-liner.
if ! command -v uv >/dev/null 2>&1; then
    print "uv (Python package manager) not found — installing it first..."
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        err "Need either curl or wget to install uv. Install one first."
    fi

    # uv installs to ~/.local/bin and ~/.cargo/bin (varies); load its env if available.
    if [ -f "$HOME/.local/bin/env" ]; then
        # shellcheck source=/dev/null
        . "$HOME/.local/bin/env"
    fi

    if ! command -v uv >/dev/null 2>&1; then
        # uv install just dropped a binary somewhere; try common paths.
        for cand in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
            if [ -x "$cand" ]; then
                PATH="$(dirname "$cand"):$PATH"
                export PATH
                break
            fi
        done
    fi

    if ! command -v uv >/dev/null 2>&1; then
        err "uv installed but not on PATH. Restart your shell and re-run this installer, or: \
export PATH=\"\$HOME/.local/bin:\$PATH\" && curl -sSL https://api.free-ride.xyz/install.sh | sh"
    fi
fi

print ""
print "Installing freeride-gateway..."
# --prerelease=allow because we ship 0.3.0a* alphas pre-stable; once 0.3.0
# final lands you can drop this flag and it'll still pick up the latest.
uv tool install --prerelease=allow freeride-gateway

print ""
print "Verifying..."
if command -v freeride >/dev/null 2>&1; then
    freeride --version
elif [ -x "$HOME/.local/bin/freeride" ]; then
    "$HOME/.local/bin/freeride" --version
    print ""
    print "Note: $HOME/.local/bin is not on your PATH yet. Run:"
    print "  export PATH=\"\$HOME/.local/bin:\$PATH\""
    print "Or add that line to your ~/.zshrc / ~/.bashrc."
else
    err "Install completed but the freeride binary couldn't be located. Try restarting your shell."
fi

# ---------------------------------------------------------------------------
# Install-event beacon — fires once per installation, before the user has
# even run `freeride serve`. Closes the gap where the existing hourly
# beacon only sees CLIs that ran serve >1h with telemetry on.
#
# Best-effort: any failure is silent and never breaks the install.
# Skipped entirely when --no-telemetry is passed or FREERIDE_TELEMETRY=off.
# Reuses ~/.freeride/installation_id if present so re-installs don't
# generate a new id (the gateway reads the same file at runtime, so
# install events and beacons share the same UUID and can be correlated).
# ---------------------------------------------------------------------------
if [ "${FREERIDE_TELEMETRY:-on}" = "off" ] || [ "${1:-}" = "--no-telemetry" ]; then
    :  # opted out — skip install-event
else
    INSTALL_ID_FILE="$HOME/.freeride/installation_id"
    mkdir -p "$HOME/.freeride" 2>/dev/null || true

    if [ -s "$INSTALL_ID_FILE" ]; then
        INSTALL_ID="$(cat "$INSTALL_ID_FILE" 2>/dev/null | tr -d '[:space:]')"
    else
        if command -v uuidgen >/dev/null 2>&1; then
            INSTALL_ID="$(uuidgen | tr 'A-Z' 'a-z')"
        elif [ -r /proc/sys/kernel/random/uuid ]; then
            INSTALL_ID="$(cat /proc/sys/kernel/random/uuid)"
        else
            INSTALL_ID="$(python3 -c "import uuid; print(uuid.uuid4())" 2>/dev/null || true)"
        fi
        if [ -n "$INSTALL_ID" ]; then
            printf '%s' "$INSTALL_ID" > "$INSTALL_ID_FILE" 2>/dev/null || true
            chmod 600 "$INSTALL_ID_FILE" 2>/dev/null || true
        fi
    fi

    case "$(uname -s 2>/dev/null)" in
        Darwin) OS_KIND="darwin" ;;
        Linux)  OS_KIND="linux" ;;
        *)      OS_KIND="other" ;;
    esac

    INSTALLED_VERSION="$(freeride --version 2>/dev/null \
        | grep -oE '[0-9]+\.[0-9]+\.[0-9]+[a-zA-Z0-9.+-]*' \
        | head -1)"
    INSTALLED_VERSION="${INSTALLED_VERSION:-unknown}"

    if [ -n "$INSTALL_ID" ] && command -v curl >/dev/null 2>&1; then
        curl -sS -m 5 -X POST https://api.free-ride.xyz/v1/install-event \
            -H "content-type: application/json" \
            -d "{\"installation_id\":\"$INSTALL_ID\",\"version\":\"$INSTALLED_VERSION\",\"os\":\"$OS_KIND\",\"install_method\":\"curl-sh\"}" \
            >/dev/null 2>&1 || true
    fi
fi

print ""
print "Done. Next:"
print " export OPENROUTER_API_KEY=sk-or-v1-... # get a free one at https://openrouter.ai/keys"
print " freeride serve # start the gateway"
print " freeride bind aider # point your favourite agent at it"
print ""
