# Herdr Workspace Autoclose

Automatically close linked Git worktree **spaces in Herdr** when:

- An agent finishes (`done`, or an observed `working` → `idle` transition).
- The checkout directory disappears, including plain directory deletion without
  pruning Git's worktree registration.
- An agent exits, its pane exits, or its tab closes.

The plugin closes Herdr state only. It never removes a Git worktree, branch, or
file. Ordinary spaces stay open. A working or
blocked agent, an agent whose state is unknown, or a foreground command in a
shell pane prevents closure. An idle agent at initial startup is not completion.

Agents can run in a shared space alongside other conversations. The Git
post-checkout hook records each workspace opened by that pane as
`worktree_space_<workspace-id>` metadata. Each value is a SHA-256 digest of the
repository key and checkout path, sized to fit Herdr's display metadata limits. These entries
accumulate, so one task can span several repositories with the same branch name
and later open a detached checkout without losing its earlier associations.
The plugin verifies the workspace ID, repository key, and checkout path before
closing.
Stale entries cannot claim a newly reopened space, and another associated agent
must finish too. The shared space stays open; any agent or foreground command in
a target protects it. Pane titles and commit hashes are never used to guess
ownership.

For older hooks that provide only `worktree_branch`, the plugin retains the
unique-branch fallback. Missing or ambiguous branch metadata leaves targets open.

After the last task worktree space in a repository closes, the plugin also closes
its default-branch space (for example, `master` or `main`) if it contains only idle shells. It checks the actual Git
branch against `origin/HEAD`, or the bare repository HEAD when unavailable,
so renaming a space cannot change this behavior. Another task space, any
agent in the default-branch space, or a foreground command keeps it open. A busy shell is
checked again after it settles. This cleanup follows an observed workspace
closure; merely starting the plugin does not close existing default-branch spaces.
Once no worktree spaces remain, the primary repository space also closes if it
contains only idle shells. Any agent or foreground command keeps it open, and
the repository group is checked again immediately before closure. This removes
the empty repository entry from the menu. All Git checkouts remain intact.

The plugin ID is `jermen.auto-close-worktrees`.

## Requirements

- Linux or macOS, Python 3.10 or newer, and Git for idle default-branch cleanup.
- Herdr 0.9.1 or newer.
- No pip packages, npm packages, external plugins, or build step.

## Install

```sh
herdr plugin install jermen/herdr-plugin-workspace-autoclose --yes
herdr plugin action invoke jermen.auto-close-worktrees.start
```

Reinstalling with `herdr plugin install` updates the checkout in place; invoke
Start again afterwards. Then install the [Git checkout hook](#git-checkout-hook).

## Develop from a checkout

Uninstall the installed copy first (`herdr plugin uninstall
jermen.auto-close-worktrees`), then run inside a Herdr pane, from the branch
checkout you want to test:

```sh
herdr plugin link "$PWD"
herdr plugin action invoke jermen.auto-close-worktrees.start
herdr plugin list --plugin jermen.auto-close-worktrees --json
```

Keep the repository in your normal development directory and link the branch
checkout you want to test. Use a persistent checkout path; Herdr executes the
linked source files directly. To test another branch, run these commands from
that branch's worktree.
Linking this ID replaces the previous local installation's registration. Keep
the old plugin directory until verification is complete if you need rollback.

The Start action is needed immediately after linking: Herdr startup hooks run
when the server starts, not when a plugin is linked or enabled. Lifecycle hooks
also ensure that one watcher is running. Calling Start repeatedly is safe.

After updating the checkout, disable and re-enable the plugin, then start it:

```sh
herdr plugin disable jermen.auto-close-worktrees
# Wait for the watcher to stop (normally within two seconds).
herdr plugin enable jermen.auto-close-worktrees
herdr plugin action invoke jermen.auto-close-worktrees.start
```

To roll back, link the previous plugin directory again. To stop automatic
cleanup, disable the plugin. A watcher also exits when its plugin is unlinked,
replaced by a different checkout, or its Herdr connection is lost. Lifecycle
hooks restart a stopped watcher on subsequent activity.

## Git checkout hook

The supplied executable `hooks/post-checkout` opens fresh linked worktrees in
Herdr without changing focus, reports the branch label plus an exact workspace
entry on the calling pane, and chains the repository-local post-checkout hook.
It requires Python 3 and the Herdr caller environment. Install it in the configured
Git hooks directory to enable associations across multiple repositories. Preserve
any additional behavior when integrating it into an existing global hook.

For example, with `core.hooksPath` set to `~/.config/git/hooks`:

```sh
curl -fsSL -o ~/.config/git/hooks/post-checkout \
  https://raw.githubusercontent.com/jermen/herdr-plugin-workspace-autoclose/master/hooks/post-checkout
chmod 755 ~/.config/git/hooks/post-checkout
```

The installed hook is a standalone copy, so reinstalling the plugin or linking
another checkout does not break Git integration.
It affects future worktree creation; it cannot recover the ownership of panes
that have already closed. Existing branch-only metadata remains supported.

## Behavior and diagnostics

The watcher subscribes to lifecycle events and reconciles a fresh session
snapshot every two seconds. Status subscriptions are registered for individual
panes and refreshed as panes appear, close, or move. A per-session file lock
prevents duplicate watchers. Named Herdr sessions are isolated from each other.

Directory removal must be visible in two consecutive checks, usually within
four seconds. The shared Git repository must remain accessible; an unavailable
disk is not treated as a worktree deletion. Permission errors and incomplete API
responses leave the space open. Fresh workspace provenance and pane activity are
checked again before closing. Busy spaces are reconsidered when they settle.

Watch logs and lock files live in Herdr's `HERDR_PLUGIN_STATE_DIR`, named
`watch-<session-and-checkout-hash>.log` and `.lock`. Hook launch logs are available
through:

```sh
herdr plugin log list --plugin jermen.auto-close-worktrees --limit 20
```

The runtime uses Herdr's documented Unix socket API. References:
[plugin lifecycle](https://herdr.dev/docs/plugins/),
[socket API](https://herdr.dev/docs/socket-api/).

## Validation

```sh
python3 -B -m unittest discover -s tests -v
```

Optional end-to-end tests require `herdr` and `git`:

```sh
python3 -B tests/integration.py
```

The integration test creates a temporary named Herdr server, separate XDG config
and plugin registry, and disposable Git worktrees. It refuses to link if the test
registry is not empty. Only that test server is stopped afterward. It exercises
real completion events, deletion without Git pruning, agent exit, agent-tab
closure with a remaining shell, busy-agent protection, and disabling the plugin.
It also checks idle `master` cleanup after automatic and manual task closure,
multiple task spaces, reopening `master`, and agents or commands in `master`.
Root-space cleanup is covered with both busy-shell and agent protection.

Shared-space completion, agent release, pane closure, and tab closure are tested
with an active neighboring agent. These cases also verify `main` and root cleanup.

Real Git hook tests cover three repositories sharing a branch plus a detached
checkout, both completion and pane exit, metadata accumulation, and chaining the
repository-local hook.
