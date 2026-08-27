#!/usr/bin/env bash
# ralph-gh installer — copies the orchestrator to ~/.claude/ralph-gh and the
# companion agents to ~/.claude/agents (user-level, available in every repo).
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.claude/ralph-gh"
AGENTS="$HOME/.claude/agents"

mkdir -p "$DEST" "$AGENTS"

for f in ralph-gh.sh CLAUDE.md example.ralph-gh.config; do
  if [[ -f "$DEST/$f" ]] && ! cmp -s "$SRC/$f" "$DEST/$f"; then
    cp "$DEST/$f" "$DEST/$f.bak"
    echo "existing $f differs — backed up to $f.bak"
  fi
  cp "$SRC/$f" "$DEST/$f"
done
chmod +x "$DEST/ralph-gh.sh"

for f in "$SRC"/agents/*.md; do
  cp "$f" "$AGENTS/"
done

echo ""
echo "Installed:"
echo "  orchestrator -> $DEST"
echo "  agents       -> $AGENTS (ralph-refactorer, ralph-gate-reviewer)"
echo ""
echo "Optional alias:"
echo "  echo 'alias ralph-gh=\"\$HOME/.claude/ralph-gh/ralph-gh.sh\"' >> ~/.zshrc"
