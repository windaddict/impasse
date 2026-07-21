#!/usr/bin/env bash
# Install Impasse as an OpenAI Codex skill (open Agent Skills standard), by SYMLINK.
#
# Safe by construction: it only ever creates or replaces a SYMLINK. Removing/replacing a symlink
# never touches its target, so this installer cannot delete your repo or any real files. If a
# physical file/directory already sits at the destination it REFUSES (you remove it yourself) — it
# never deletes real data: it removes ONLY a symlink it has verified, and creates without -f (so a
# file that races into the slot causes a clean failure, never a clobber). Idempotent.
# Requires bash + python3 + coreutils resolved from a TRUSTED PATH (like any script it runs `python3`,
# `ln`, `mkdir` by name — run it in your normal shell, not under an attacker-controlled PATH).
# See docs/host-detection.md and SKILL.md "Running it (host adapter)".
#
# Usage: bash scripts/install-codex.sh [--root DIR] [--dry-run]
#   --root DIR install under DIR (default: auto-detected Codex skills root)
#   --dry-run  print what would happen; change nothing
# For a stable (non-symlink) install, copy the repo into place yourself: cp -R <repo> <root>/impasse
set -euo pipefail

NAME="impasse"   # Agent Skills spec: the install dir name MUST equal SKILL.md `name:`

# Canonicalize in isolated mode (-I -S: no site/user customization, no env-driven import) so a
# hostile CWD/PYTHONPATH can't run startup code. realpath makes absolute + resolves `..` and symlinks.
canon() { python3 -I -S -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1"; }

SRC="$(canon "$(dirname -- "${BASH_SOURCE[0]}")/..")"

DRY=0; ROOT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1;;
    --root) ROOT="${2:?--root needs a directory}"; shift;;
    -h|--help) sed -n '2,17p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
  shift
done

# Detect the active Codex skills root ($CODEX_HOME authoritative; else the skills dir that EXISTS;
# both -> ambiguous, stop; neither -> ~/.codex/skills, what current alpha builds read).
detect_root() {
  if [ -n "${CODEX_HOME:-}" ]; then echo "$CODEX_HOME/skills"; return; fi
  local c="$HOME/.codex/skills" a="$HOME/.agents/skills" have=()
  [ -d "$c" ] && have+=("$c"); [ -d "$a" ] && have+=("$a")
  case "${#have[@]}" in
    1) echo "${have[0]}";;
    0) echo "$c"; echo "note: neither skills dir exists; defaulting to $c (newer builds may use" \
            "~/.agents/skills — pass --root if so)." >&2;;
    *) echo "AMBIGUOUS";;
  esac
}
[ -n "$ROOT" ] || ROOT="$(detect_root)"
if [ "$ROOT" = "AMBIGUOUS" ]; then
  echo "both ~/.codex/skills and ~/.agents/skills exist — can't tell which your Codex build reads." >&2
  echo "Re-run with --root <dir> (see docs/host-detection.md)." >&2
  exit 1
fi
ROOT="$(canon "$ROOT")"       # canonicalize the PARENT only
DEST="$ROOT/$NAME"            # lexical leaf — do NOT resolve it, so a dest symlink is seen AS a symlink

[ -f "$SRC/SKILL.md" ] || { echo "no SKILL.md at $SRC — is this the Impasse repo?" >&2; exit 1; }

# Inspect the destination with lstat semantics (-L before -e). We only ever act on a symlink or
# empty slot; a physical file/dir is never touched.
if [ -L "$DEST" ]; then
  if [ "$(canon "$DEST")" = "$SRC" ]; then
    echo "Already installed: $DEST -> $SRC (no change)."; exit 0
  fi
  echo "Replacing existing symlink at $DEST"
elif [ -e "$DEST" ]; then
  echo "refusing: $DEST exists and is not a symlink — this installer won't delete real files." >&2
  echo "Remove it yourself (or install elsewhere with --root), then re-run." >&2
  exit 1
fi

echo "Impasse -> Codex skill (symlink)"
echo "  source: $SRC"
echo "  dest:   $DEST"

if [ "$DRY" = 1 ]; then
  echo "  [dry-run] mkdir -p -- $(printf '%q' "$ROOT")"
  [ -L "$DEST" ] && echo "  [dry-run] rm -- $(printf '%q' "$DEST")   # verified symlink only"
  echo "  [dry-run] ln -s -- $(printf '%q' "$SRC") $(printf '%q' "$DEST")   # no -f: fails, never clobbers, if the slot is taken"
  exit 0
fi

# Create the symlink WITHOUT -f, so the guarantee is absolute: `ln -s` errors (never clobbers) if
# anything occupies DEST. We first remove ONLY a symlink we verified above (rm of a symlink never
# touches its target, and handles the OLD link pointing at a directory — no mv-into-symlinked-dir
# footgun). If a regular file has raced into the slot since the check, `ln -s` fails cleanly rather
# than deleting it. A physical path was already refused above, so `rm` here only ever removes a link.
mkdir -p -- "$ROOT"
[ -L "$DEST" ] && rm -- "$DEST"
ln -s -- "$SRC" "$DEST"

echo "Installed. Next:"
echo "  1. Restart Codex (skills load at startup)."
echo "  2. Confirm discovery: run /skills in Codex and look for 'impasse', or type \$impasse."
echo "  3. If it isn't listed, your build may read a different skills root — re-run with"
echo "     --root ~/.agents/skills (see docs/host-detection.md)."
