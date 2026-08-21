#!/usr/bin/env bash
# Install Impasse as a Cursor skill (open Agent Skills standard), by SYMLINK.
#
# Safe by construction: it only ever creates or replaces a SYMLINK. Removing/replacing a symlink
# never touches its target, so this installer cannot delete your repo or any real files. If a
# physical file/directory already sits at the destination it REFUSES (you remove it yourself) — it
# never deletes real data: it removes ONLY a symlink it has verified, and creates without -f (so a
# file racing into the slot causes a clean failure, never a clobber). The checks are not atomic, so
# the exact guarantee is "never deletes a path this script observed to be a non-symlink" — and the
# install is post-verified, turning any raced outcome into a loud failure. Idempotent.
# Requires bash + python3 + coreutils resolved from a TRUSTED PATH (like any script it runs `python3`,
# `ln`, `mkdir` by name — run it in your normal shell, not under an attacker-controlled PATH).
#
# WHY A CURSOR-NATIVE INSTALL AT ALL: Cursor's docs say it also discovers skills from the Claude and
# Codex locations, so a machine that already has Impasse under Claude Code MAY load it in Cursor with
# no install. That compat path is a CANDIDATE, not a verified one — it has not been dogfooded here —
# so this installer exists to give you a path that does not depend on it. See
# docs/host-detection.md and SKILL.md "Running it (host adapter)".
#
# INDEPENDENCE UNDER CURSOR — read before you rely on a review from here. Cursor's host model is
# whatever you picked in its model picker, and no environment marker reveals which lab it came from.
# Impasse therefore reports `undetermined` under Cursor unless YOU assert the host with IMPASSE_HOST
# (see SKILL.md). Installing this script changes nothing about that.
#
# Usage: bash scripts/install-cursor.sh [--root DIR] [--dry-run]
#   --root DIR install under DIR (default: auto-detected Cursor skills root)
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
    -h|--help) sed -n '2,25p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
  shift
done

# Detect the active Cursor skills root. Project-local (.cursor/skills) is deliberately NOT
# auto-detected: it would install into whatever directory you happened to run this from, which is a
# surprising place to leave a symlink. Pass --root .cursor/skills if you want that.
detect_root() {
  local c="$HOME/.cursor/skills" a="$HOME/.agents/skills" have=()
  [ -d "$c" ] && have+=("$c"); [ -d "$a" ] && have+=("$a")
  case "${#have[@]}" in
    1) echo "${have[0]}";;
    0) echo "$c"; echo "note: neither skills dir exists; defaulting to $c (some builds read" \
            "~/.agents/skills — pass --root if so)." >&2;;
    *) echo "AMBIGUOUS";;
  esac
}
[ -n "$ROOT" ] || ROOT="$(detect_root)"
if [ "$ROOT" = "AMBIGUOUS" ]; then
  echo "both ~/.cursor/skills and ~/.agents/skills exist — can't tell which your Cursor build reads." >&2
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

echo "Impasse -> Cursor skill (symlink)"
echo "  source: $SRC"
echo "  dest:   $DEST"

if [ "$DRY" = 1 ]; then
  echo "  [dry-run] mkdir -p -- $(printf '%q' "$ROOT")"
  [ -L "$DEST" ] && echo "  [dry-run] rm -- $(printf '%q' "$DEST")   # verified symlink only"
  echo "  [dry-run] ln -s -- $(printf '%q' "$SRC") $(printf '%q' "$DEST")   # no -f: refuses to overwrite a file; result is post-verified"
  exit 0
fi

# Create the symlink WITHOUT -f: `ln -s` then refuses to overwrite an existing FILE rather than
# clobbering it. We first remove ONLY a symlink we verified above (rm of a symlink never touches its
# target, and handles the OLD link pointing at a directory — no mv-into-symlinked-dir footgun).
#
# The honest limit: these checks are NOT atomic. Between the test and the `rm`, or between the `rm`
# and the `ln`, a concurrent process could substitute the destination — so the guarantee is "never
# deletes a path this script observed to be a non-symlink", not "immune to a racing attacker". Such
# an attacker already needs write access to your skills directory. The post-verify below converts
# every raced outcome into a loud failure rather than a silent wrong install.
mkdir -p -- "$ROOT"
[ -L "$DEST" ] && rm -- "$DEST"
ln -s -- "$SRC" "$DEST"

# POST-VERIFY, because the checks above are not atomic and `ln -s` is not as strict as it looks:
# if a DIRECTORY races into DEST after the rm, `ln -s SRC DEST` does NOT fail — it silently creates
# the link INSIDE it (DEST/impasse) and exits 0. Re-checking the end state is what turns that
# outcome, and any other raced substitution, into a loud failure instead of a false success.
if [ ! -L "$DEST" ] || [ "$(canon "$DEST")" != "$SRC" ]; then
  echo "refusing to report success: $DEST is not a symlink to $SRC after install." >&2
  echo "Something occupied the destination while installing (a directory racing into place makes" >&2
  echo "'ln -s' create a link inside it rather than failing). Inspect it and remove it yourself." >&2
  exit 1
fi

echo "Installed. Next:"
echo "  1. Restart Cursor (skills load at startup)."
echo "  2. Confirm discovery: look for 'impasse' in Cursor's skills list, or invoke it by name."
echo "  3. If it isn't listed, your build may read a different skills root — re-run with"
echo "     --root ~/.agents/skills (see docs/host-detection.md)."
echo "  4. BEFORE trusting a review from Cursor, assert which model drives this session:"
echo "     export IMPASSE_HOST=claude|codex|gemini|grok   # or leave unset to stay 'undetermined'"
echo "     Impasse cannot detect it, and an unasserted Cursor session never claims cross-provider."
