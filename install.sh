#!/usr/bin/env sh
# FreeRide installer. Run with:
#
#   curl -sSL https://free-ride.xyz/install.sh | sh
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
export PATH=\"\$HOME/.local/bin:\$PATH\" && curl -sSL https://free-ride.xyz/install.sh | sh"
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

print ""
print "Done. Next:"
print " export OPENROUTER_API_KEY=sk-or-v1-... # get a free one at https://openrouter.ai/keys"
print " freeride serve # start the gateway"
print " freeride bind aider # point your favourite agent at it"
print ""
