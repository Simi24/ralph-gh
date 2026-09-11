#!/usr/bin/env bash
# ralph-gh installer — copies the orchestrator to ~/.claude/ralph-gh and the
# companion agents to ~/.claude/agents (user-level, available in every repo).
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.claude/ralph-gh"
AGENTS="$HOME/.claude/agents"

mkdir -p "$DEST" "$AGENTS"

for f in ralph-gh.sh CLAUDE.md example.ralph-gh.config README.md; do
  if [[ -f "$DEST/$f" ]] && ! cmp -s "$SRC/$f" "$DEST/$f"; then
    cp "$DEST/$f" "$DEST/$f.bak"
    echo "existing $f differs — backed up to $f.bak"
  fi
  cp "$SRC/$f" "$DEST/$f"
done
chmod +x "$DEST/ralph-gh.sh"

# version.txt only exists once release-please has cut a first release —
# best-effort, not an install failure if this is a pre-release clone.
if [[ -f "$SRC/version.txt" ]]; then
  cp "$SRC/version.txt" "$DEST/version.txt"
fi

for f in "$SRC"/agents/*.md; do
  base="$(basename "$f")"
  if [[ -f "$AGENTS/$base" ]] && ! cmp -s "$f" "$AGENTS/$base"; then
    cp "$AGENTS/$base" "$AGENTS/$base.bak"
    echo "existing $base differs — backed up to $base.bak"
  fi
  cp "$f" "$AGENTS/"
done

SKILLS="$HOME/.claude/skills"
mkdir -p "$SKILLS/ralph-gh"
cp "$SRC/skills/ralph-gh/SKILL.md" "$SKILLS/ralph-gh/SKILL.md"

echo ""
echo "Installed:"
echo "  orchestrator -> $DEST"
echo "  agents       -> $AGENTS (ralph-refactorer, ralph-gate-reviewer)"
echo "  skill        -> $SKILLS/ralph-gh (/ralph-gh inside Claude Code sessions)"
echo ""
echo "Optional alias:"
echo "  echo 'alias ralph-gh=\"\$HOME/.claude/ralph-gh/ralph-gh.sh\"' >> ~/.zshrc"
