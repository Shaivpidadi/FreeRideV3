# FreeRide skill for Claude Code

A skill that teaches Claude how to detect, wire, and troubleshoot FreeRide
on a user's machine. Without it, Claude treats FreeRide like any other
HTTP service and asks the user how to point an agent at it — instead of
just doing it.

## Install via plugin (recommended)

```
/plugin install https://github.com/Shaivpidadi/FreeRideV3
```

Claude Code reads `.claude-plugin/plugin.json` at the repo root and
loads the `freeride` skill from `skills/freeride/SKILL.md`.

After install, Claude will auto-invoke this skill when:

- The user mentions FreeRide
- The user has the gateway running on `localhost:11343`
- The user asks about routing across free-tier providers

## Install manually

If you'd rather not use the plugin system:

```bash
mkdir -p ~/.claude/skills/freeride
cp skills/freeride/SKILL.md ~/.claude/skills/freeride/SKILL.md
```

Or symlink for live updates:

```bash
ln -s "$(pwd)/skills/freeride" ~/.claude/skills/freeride
```

## What the skill teaches

- How to detect FreeRide is running (`curl /health`, port 11343, config)
- How to wire any OpenAI-shaped client (`OPENAI_API_BASE`, `OPENAI_API_KEY=any`)
- How to identify which provider served a given request (`X-FreeRide-Provider` header)
- How multi-key rotation and cooldown work
- The full `freeride` CLI surface
- Common 503 / quota-exhausted / "command not found" debug paths
- Failover semantics so the agent can explain *why* a request went where

## Authoring notes

- `name` is implicit from the directory (`skills/freeride/`); only
  `description` is required in frontmatter.
- The `description` is the load-bearing field — Claude uses it to decide
  whether to invoke the skill, so it must clearly state the conditions.
- Keep `SKILL.md` focused on operator-facing knowledge (detect, wire,
  diagnose). Don't inline architectural deep-dives — those belong in
  `docs/`.
