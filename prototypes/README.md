# Prototypes for the ralph-gh mods PRD

These are throwaway prototypes, the starting point for the "ralph-gh mods" PRD. They are not wired into anything, and this branch is never merged: the PRD's tickets copy what they need into the plugin.

- `ralph-guard/` holds the worker-session guard. It hooks `tool.call` on Bash/Write/Edit/NotebookEdit and is inert without `RALPH_GUARD=1`. It was verified with a real `claude -p --dangerously-skip-permissions` session: a push and an outside-worktree write were refused, an inside write was allowed, and the effects on disk and on the remote confirmed it.
- `ralph-dashboard/` holds the `/ralph` pane: a progress bar, in-flight pipelines, attention, up-next, collapsible waiting and done lists, toasts, status line and a Drain button. It was followed live on a two-wide parallel run. It works on demand: loading it only registers `/ralph`. `/ralph` starts one poll timer, and closing the pane or `/ralph off` cancels it. GitHub is read on open and on new log events only.

Run `claude plugin validate <dir>` and `claude plugin test <dir>` on either folder. To try one in a session, use `claude --plugin-dir <dir>`.
