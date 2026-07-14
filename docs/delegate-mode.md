# Delegate mode (experimental — not in the trusted review path)

**Status: planned / experimental. Not implemented in v1.** The trusted Impasse path is
read-only: the reviewer observes and argues, it does not change the artifact. Delegate mode
is a separate, opt-in capability to let a backend *propose an edit* — kept deliberately
outside the review path and off by default.

## Why it's separate

A reviewer that edits the artifact is no longer independent of the remediation it proposes.
Impasse preserves the original, read-only review; any remediation is a **distinct run** with
its own trust boundary.

## The design (when built)

- **Off by default.** Enabled per-run or via config; never implicit.
- **Isolated workspace.** Requires a git repository with a **clean worktree and index**; the
  work happens in a **temporary worktree on a throwaway branch** created from the current
  commit — never the operator's checkout.
- **Returns a patch**, plus any validation/test results. It **never** commits to the
  operator's branch, merges, pushes, or stashes.
- **Controls:** path allowlist/denylist; symlink-escape checks; no writes outside the
  temporary worktree; no network mutation, deployment, or secret rotation; caps on changed
  files/bytes; tests run with recorded commands + exit codes; the diff is inspected before
  acceptance; the patch is retained on failure.

The old "checkpoint with `git add -A && git commit`" recipe is explicitly rejected — it can
commit secrets and unrelated work. Isolation via a temporary worktree replaces it.
