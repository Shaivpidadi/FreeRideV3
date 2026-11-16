#!/usr/bin/env sh
# fx + FreeRide installer. Run with:
#
#   curl -sSL https://api.free-ride.xyz/fx.sh | sh
#
# What this does:
#   1. Installs fx, Vercel's open-source coding agent, with its official
#      installer (https://fx.sh/setup.sh) unless `fx` is already on PATH.
#   2. Installs the FreeRide gateway with the FreeRide installer
#      (https://api.free-ride.xyz/install.sh).
#   3. Registers the gateway as an always-on daemon (launchd / systemd)
#      and makes it fx's model connection: `freeride daemon install`,
#      `freeride bind fx`. The bind also installs the `freeride` skill so
#      the agent can diagnose its own gateway.
#
# Env knobs:
#   FX_SKIP_INSTALL=1        don't touch fx (use the one already installed)
#   FREERIDE_SKIP_GATEWAY=1  don't (re)install the gateway
#   FREERIDE_NO_DAEMON=1     bind fx but don't register the daemon
#   FREERIDE_REF / FREERIDE_TELEMETRY pass through to the gateway installer

set -e

BIN_DIR="$HOME/.local/bin"
PATH="$BIN_DIR:$PATH"
export PATH

print() { printf '%s\n' "$*"; }
err() { printf 'error: %s\n' "$*" >&2; exit 1; }

print "fx + FreeRide installer"
print ""

case "$(uname -s)" in
    Darwin|Linux) ;;
    *) err "this installer supports macOS and Linux; on Windows use install.ps1 and fx.sh/docs" ;;
esac
command -v curl >/dev/null 2>&1 || err "curl is required"

# ── 1. fx ──────────────────────────────────────────────────────────
if [ "${FX_SKIP_INSTALL:-0}" = "1" ]; then
    print "Skipping fx install (FX_SKIP_INSTALL=1)."
elif command -v fx >/dev/null 2>&1; then
    print "fx is already installed: $(fx --version 2>/dev/null || command -v fx)"
else
    print "Installing fx (https://fx.sh)..."
    curl -fsSL https://fx.sh/setup.sh | bash
fi
command -v fx >/dev/null 2>&1 || print "note: fx is not on PATH in this shell yet; open a new terminal after this finishes."

# ── 2. the gateway ─────────────────────────────────────────────────
if [ "${FREERIDE_SKIP_GATEWAY:-0}" = "1" ]; then
    print "Skipping FreeRide gateway install (FREERIDE_SKIP_GATEWAY=1)."
else
    print ""
    print "Installing the FreeRide gateway..."
    curl -sSL https://api.free-ride.xyz/install.sh | sh
fi
command -v freeride >/dev/null 2>&1 || err "freeride is not on PATH after install; add $BIN_DIR to PATH and rerun"

# ── 3. daemon + bind ───────────────────────────────────────────────
print ""
if [ "${FREERIDE_NO_DAEMON:-0}" = "1" ]; then
    print "Not registering the daemon (FREERIDE_NO_DAEMON=1); run \`freeride serve\` yourself."
else
    freeride daemon install || print "warning: daemon install failed; run \`freeride serve\` yourself."
fi
freeride bind fx

print ""
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        print "Note: $BIN_DIR is not on your PATH yet. Run:"
        print "  export PATH=\"\$HOME/.local/bin:\$PATH\""
        print "Or add that line to your ~/.zshrc / ~/.bashrc."
        ;;
esac

print "Done. Next:"
print "  freeride init     # add one free API key (optional: keyless providers work without)"
print "  fx                # the agent, running on freeride/coding"
