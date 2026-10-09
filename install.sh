#!/usr/bin/env bash
# ralph-gh installer — deploys the conductor package and its `ralph-gh` launcher
# to <claude config dir>/ralph-gh, the companion agents to <claude config
# dir>/agents and the /ralph-gh skill to <claude config dir>/skills (user-level,
# available in every repo). The claude config dir is $CLAUDE_CONFIG_DIR, or
# ~/.claude when that is unset.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
DEST="$CONFIG/ralph-gh"
AGENTS="$CONFIG/agents"
SKILLS="$CONFIG/skills"

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
  echo "install.sh: ralph-gh needs Python 3.12 or newer (python3 on PATH is missing or older)" >&2
  exit 1
fi

mkdir -p "$DEST/conductor" "$AGENTS" "$SKILLS/ralph-gh"

# install_file SRC_FILE DEST_FILE: copy, backing up a differing installed file first.
install_file() {
  local src="$1" dest="$2" name
  name="$(basename "$dest")"
  if [[ -f "$dest" ]] && ! cmp -s "$src" "$dest"; then
    cp "$dest" "$dest.bak"
    echo "existing $name differs — backed up to $name.bak"
  fi
  cp "$src" "$dest"
}

# retire FILE: move an installed file that this version no longer ships out of
# the way (kept as FILE.bak, which nothing imports or loads).
retire() {
  local file="$1"
  if [[ -f "$file" ]]; then
    mv "$file" "$file.bak"
    echo "retired $(basename "$file") — kept as $(basename "$file").bak"
  fi
}

# The package: every module, plus the launcher that runs it from next to itself.
for f in "$SRC"/conductor/*.py; do
  install_file "$f" "$DEST/conductor/$(basename "$f")"
done
for f in "$DEST"/conductor/*.py; do
  [[ -e "$f" ]] || continue
  [[ -f "$SRC/conductor/$(basename "$f")" ]] || retire "$f"
done
install_file "$SRC/ralph-gh" "$DEST/ralph-gh"
chmod +x "$DEST/ralph-gh"

install_file "$SRC/example.ralph-gh.toml" "$DEST/example.ralph-gh.toml"
install_file "$SRC/README.md" "$DEST/README.md"

# What the bash orchestrator installed and the conductor replaced.
retire "$DEST/ralph-gh.sh"
retire "$DEST/CLAUDE.md"
retire "$DEST/example.ralph-gh.config"
retire "$AGENTS/ralph-refactorer.md"

# version.txt only exists once release-please has cut a first release —
# best-effort, not an install failure if this is a pre-release clone. Also
# clear a stale installed copy so a reinstall from a pre-release clone can
# never leave the banner reporting a version this clone doesn't have.
if [[ -f "$SRC/version.txt" ]]; then
  cp "$SRC/version.txt" "$DEST/version.txt"
else
  rm -f "$DEST/version.txt"
fi

for f in "$SRC"/worker-bundle/agents/*.md; do
  install_file "$f" "$AGENTS/$(basename "$f")"
done

install_file "$SRC/skills/ralph-gh/SKILL.md" "$SKILLS/ralph-gh/SKILL.md"

# Stamp which clone this install came from and at what commit, so a stale
# installed copy can warn about itself at startup (conductor/drift.py). Best
# effort: SRC may not be a git checkout at all (e.g. a downloaded tarball),
# in which case the SHA is recorded as "unknown" and the drift check fails
# open. Refreshed on every re-run, so reinstalling always re-syncs it.
SRC_SHA="$(git -C "$SRC" rev-parse HEAD 2>/dev/null || echo unknown)"
{
  echo "source_path=$SRC"
  echo "source_sha=$SRC_SHA"
} > "$DEST/.installed"

echo ""
echo "Installed:"
echo "  conductor -> $DEST (launcher: $DEST/ralph-gh)"
echo "  agents    -> $AGENTS (ralph-ticket-gate, ralph-gate-reviewer)"
echo "  skill     -> $SKILLS/ralph-gh (/ralph-gh inside Claude Code sessions)"
echo ""
echo "Optional alias:"
echo "  echo 'alias ralph-gh=\"$DEST/ralph-gh\"' >> ~/.zshrc"
