#!/usr/bin/env bash
# ralph-gh migration stub. ralph-gh installs as a Claude Code plugin now; this script
# installs and removes nothing. It tells you the plugin install command and what to
# delete from a legacy install, then exits non-zero.
CONFIG="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

cat >&2 <<MSG
install.sh: ralph-gh now installs as a Claude Code plugin. Nothing was installed or removed.

Install it (at the Claude Code prompt):

  /plugin install ralph-gh --marketplace Simi24/ralph-gh

Update it with: claude plugin update

If you installed with the old install.sh, remove that copy so two never run side by side
(keep $CONFIG/ralph-gh/state: it holds your run state):

  rm -rf "$CONFIG/ralph-gh/conductor" "$CONFIG/ralph-gh/ralph-gh" "$CONFIG/ralph-gh/README.md" \\
         "$CONFIG/ralph-gh/example.ralph-gh.toml" "$CONFIG/ralph-gh/version.txt" "$CONFIG/ralph-gh/.installed"
  rm -rf "$CONFIG/skills/ralph-gh" "$CONFIG/agents/ralph-gate-reviewer.md" "$CONFIG/agents/ralph-ticket-gate.md"

For the terminal command (ralph-gh run / ralph-gh stop), see "Run it from a terminal" in the README.
MSG
exit 1
