# Platform support

Impasse targets **macOS and conventional Linux** today. Windows is a documented roadmap, not
a claim. The key constraint: reliable process-tree termination in the supervisor uses POSIX
process groups (`start_new_session` + `os.killpg`), which don't exist natively on Windows.

Split the question two ways — *resolving* the backend vs *running* the supervisor:

| Environment | Resolve codex | Run the review (supervisor) |
|---|---|---|
| macOS | ✅ | ✅ |
| Linux | ✅ | ✅ |
| **WSL** (real Linux) | ✅ (install codex *inside* WSL) | ✅ — treat as Linux, not Windows |
| **Git-Bash / MSYS2** | ✅ (resolver finds the `%APPDATA%\npm` shim) | ⚠️ degraded — the supervisor falls back to process-level kill (no reliable tree kill); a `.cmd` shim needs `cmd.exe /c`, so prefer an extensionless shim / `.exe` / `IMPASSE_CODEX_BIN` |
| **native PowerShell / cmd** | ✅ (native shell) | ❌ — the helpers are POSIX-oriented Python; no group semantics |

**What works everywhere:** the schemas, the consent model, and the CLI *shape* are
platform-neutral. **What's POSIX-only:** the supervisor's process-group termination — on
non-POSIX it degrades to `terminate()`/`kill()` on the direct child, which can leak a
grandchild.

**Roadmap for real Windows support:** implement process containment via Windows **Job
Objects** (kill-on-close), handle `.cmd`/`.exe`/PowerShell shim invocation, and add native
Windows CI. Until then, Windows users should run Impasse under **WSL**.
