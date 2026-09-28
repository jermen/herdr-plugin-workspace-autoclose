#!/usr/bin/env python3
"""Close finished Herdr worktree spaces. Python standard library only."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import time

PLUGIN_ID = "jermen.auto-close-worktrees"
ROOT = Path(__file__).resolve().parent
INTERVAL = 2.0
TIMEOUT = 5.0
EVENTS = ["pane.created", "pane.agent_detected", "pane.closed",
          "pane.exited", "tab.closed", "pane.moved", "workspace.closed",
          "worktree.created", "worktree.opened"]
SPACE_TOKEN_PREFIX = "worktree_space_"


def log(message):
    print(f"[{PLUGIN_ID}] {message}", file=sys.stderr, flush=True)


class ApiError(RuntimeError):
    pass


class Stream:
    def __init__(self, sock):
        self.sock = sock
        self.buffer = b""
        self.pending = []
        self.pane_ids = set()

    def read(self, timeout):
        if self.pending:
            messages, self.pending = self.pending, []
            return messages
        if b"\n" not in self.buffer:
            if not select.select([self.sock], [], [], timeout)[0]:
                return []
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ApiError("Herdr event stream disconnected")
            self.buffer += chunk
            if len(self.buffer) > 16 * 1024 * 1024:
                raise ApiError("oversized Herdr event")
        messages = []
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            if line:
                messages.append(json.loads(line))
        return messages


class Client:
    def __init__(self, socket_path):
        self.socket_path = socket_path

    def connect(self, method, params):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(TIMEOUT)
            sock.connect(self.socket_path)
            sock.sendall(json.dumps({"id": "autoclose", "method": method,
                                     "params": params}).encode() + b"\n")
        except BaseException:
            sock.close()
            raise
        return sock

    def call(self, method, **params):
        with self.connect(method, params) as sock:
            with sock.makefile("rb") as reader:
                response = json.loads(reader.readline(16 * 1024 * 1024))
        if "error" in response:
            raise ApiError(f"{method}: {response['error']}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise ApiError(f"{method}: missing result")
        return result

    def subscribe(self):
        # Status subscriptions require an explicit pane ID on Herdr 0.9.1.
        snapshot = self.call("session.snapshot")["snapshot"]
        pane_ids = {p["pane_id"] for p in snapshot["panes"]}
        subscriptions = [{"type": event} for event in EVENTS]
        subscriptions += [{"type": "pane.agent_status_changed", "pane_id": pane_id}
                          for pane_id in sorted(pane_ids)]
        stream = Stream(self.connect("events.subscribe", {"subscriptions": subscriptions}))
        stream.pane_ids = pane_ids
        deadline = time.monotonic() + TIMEOUT
        try:
            while time.monotonic() < deadline:
                messages = stream.read(max(0, deadline - time.monotonic()))
                for message in messages:
                    if "error" in message:
                        raise ApiError(str(message["error"]))
                    if message.get("result", {}).get("type") == "subscription_started":
                        stream.pending = [m for m in messages if "event" in m]
                        return stream
            raise ApiError("Herdr did not acknowledge the event subscription")
        except BaseException:
            stream.sock.close()
            raise

    def enabled(self):
        plugins = self.call("plugin.list", plugin_id=PLUGIN_ID).get("plugins", [])
        return any(p.get("plugin_id") == PLUGIN_ID and p.get("enabled") is True
                   and Path(p.get("plugin_root", "")).resolve() == ROOT for p in plugins)


def linked(workspace):
    worktree = workspace.get("worktree")
    return isinstance(worktree, dict) and worktree.get("is_linked_worktree") is True


def repository_key(workspace):
    worktree = workspace.get("worktree")
    if isinstance(worktree, dict):
        repo = worktree.get("repo_key")
        if isinstance(repo, str) and os.path.isabs(repo):
            return repo
    return None


def primary(workspace):
    worktree = workspace.get("worktree")
    if not isinstance(worktree, dict) or worktree.get("is_linked_worktree") is not False:
        return False
    root, repo = worktree.get("repo_root"), repository_key(workspace)
    return (isinstance(root, str) and os.path.isabs(root)
            and worktree.get("checkout_path") == root and bool(repo)
            and Path(root).is_dir() and Path(repo).is_dir())


def default_checkout(workspace):
    """Identify the actual Git branch and repository, never the space label."""
    if not linked(workspace):
        return False
    repo = repository_key(workspace)
    checkout = workspace.get("worktree", {}).get("checkout_path")
    if not repo or not isinstance(checkout, str) or not os.path.isabs(checkout):
        return False
    env = {k: v for k, v in os.environ.items()
           if k not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"}}
    try:
        values = subprocess.check_output(
            ["git", "-C", checkout, "rev-parse", "--show-toplevel",
             "--git-common-dir", "--symbolic-full-name", "HEAD"],
            env=env, stderr=subprocess.DEVNULL, text=True, timeout=TIMEOUT,
        ).splitlines()
        if (len(values) != 3 or Path(values[0]).resolve() != Path(checkout).resolve()
                or (Path(checkout) / values[1]).resolve() != Path(repo).resolve()):
            return False
        try:
            default = subprocess.check_output(
                ["git", "--git-dir", repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
                env=env, stderr=subprocess.DEVNULL, text=True, timeout=TIMEOUT,
            ).strip()
            prefix = "refs/remotes/origin/"
            if not default.startswith(prefix):
                return False
            default = "refs/heads/" + default[len(prefix):]
        except subprocess.CalledProcessError:
            if subprocess.check_output(
                ["git", "--git-dir", repo, "rev-parse", "--is-bare-repository"],
                env=env, stderr=subprocess.DEVNULL, text=True, timeout=TIMEOUT,
            ).strip() != "true":
                return False
            default = subprocess.check_output(
                ["git", "--git-dir", repo, "symbolic-ref", "--quiet", "HEAD"],
                env=env, stderr=subprocess.DEVNULL, text=True, timeout=TIMEOUT,
            ).strip()
        return default.startswith("refs/heads/") and values[2] == default
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def missing_checkout(workspace):
    """A removed directory qualifies even if Git still has a prunable record."""
    if not linked(workspace):
        return False
    worktree = workspace["worktree"]
    checkout, repo = worktree.get("checkout_path"), worktree.get("repo_key")
    if not all(isinstance(p, str) and os.path.isabs(p) for p in (checkout, repo)):
        return False
    try:
        Path(checkout).lstat()
    except FileNotFoundError:
        # Do not interpret an unavailable disk/repository as a removed worktree.
        return Path(repo).is_dir()
    except OSError:
        return False
    return False


def space_tokens(pane):
    return {key[len(SPACE_TOKEN_PREFIX):]: value
            for key, value in pane.get("tokens", {}).items()
            if key.startswith(SPACE_TOKEN_PREFIX)}


def workspace_token_value(workspace):
    repo = repository_key(workspace)
    checkout = workspace.get("worktree", {}).get("checkout_path")
    if not repo or not isinstance(checkout, str) or not os.path.isabs(checkout):
        return None
    # Herdr truncates display metadata values. A digest fits even for long paths.
    return hashlib.sha256((repo + "\0" + checkout).encode()).hexdigest()


def association_identity(pane):
    tokens = pane.get("tokens", {})
    return (pane.get("terminal_id"), tokens.get("worktree_branch"),
            tuple(sorted(space_tokens(pane).items())))


class Engine:
    def __init__(self, client):
        self.client = client
        self.previous = {}
        self.completed = set()
        self.departed = set()
        self.missing = set()
        self.previous_workspaces = {}
        self.repository_cleanup = set()
        self.exited_panes = {}
        self.external_cleanup = {}
        self.associated_completions = {}

    def record_exit(self, pane):
        if pane.get("agent"):
            self.exited_panes[pane["pane_id"]] = pane.copy()

    def event(self, message):
        event, data = message.get("event", ""), message.get("data", {})
        if "." not in event:
            event = event.replace("_", ".", 1)
        if not isinstance(data, dict):
            return
        pane_id, workspace_id = data.get("pane_id"), data.get("workspace_id")
        previous = self.previous.get(pane_id, {})
        if event == "pane.agent_status_changed":
            status = data.get("agent_status")
            if status == "done" or (status == "idle" and previous.get("agent_status") == "working"):
                self.completed.add(pane_id)
            elif status in {"working", "blocked", "unknown"}:
                self.completed.discard(pane_id)
            if previous:
                previous["agent_status"] = status
        elif event == "pane.agent_detected":
            if not data.get("agent") and (previous.get("agent") or data.get("released")):
                self.departed.add(workspace_id)
                self.record_exit(previous)
            elif data.get("agent"):
                self.previous[pane_id] = {**previous, **data}
        elif event in {"pane.closed", "pane.exited"} and previous.get("agent"):
            self.departed.add(workspace_id)
            self.record_exit(previous)
        elif event == "tab.closed":
            for pane in self.previous.values():
                if pane.get("tab_id") == data.get("tab_id") and pane.get("agent"):
                    self.departed.add(workspace_id)
                    self.record_exit(pane)
        elif event == "pane.moved":
            # Moving an agent is not exiting it.
            old = data.get("previous_pane_id")
            self.previous.pop(old, None)
            self.completed.discard(old)
            pane = data.get("pane")
            if isinstance(pane, dict):
                self.previous[pane["pane_id"]] = pane

    def safe_panes(self, panes):
        """Allow settled agents and bare shells; fail closed on unknown/busy panes."""
        for pane in panes:
            status = pane.get("agent_status")
            if status in {"working", "blocked"}:
                return False
            if pane.get("agent"):
                if status not in {"idle", "done"}:
                    return False
                continue
            try:
                info = self.client.call("pane.process_info", pane_id=pane["pane_id"])["process_info"]
            except (KeyError, ApiError, OSError, ValueError):
                return False
            shell, processes = info.get("shell_pid"), info.get("foreground_processes")
            if (not isinstance(shell, int) or shell <= 0
                    or info.get("foreground_process_group_id") != shell
                    or not isinstance(processes, list) or not processes
                    or any(not isinstance(p, dict) or p.get("pid") != shell for p in processes)):
                return False
        return True

    def snapshot(self):
        snapshot = self.client.call("session.snapshot").get("snapshot")
        if (not isinstance(snapshot, dict)
                or not all(isinstance(snapshot.get(k), list) for k in ("panes", "workspaces"))):
            raise ApiError("incomplete session snapshot")
        return snapshot

    def branch_workspaces(self, workspaces):
        """Resolve hook metadata against Git, rejecting incomplete/ambiguous lookup."""
        linked_spaces = {w["workspace_id"]: w for w in workspaces if linked(w)}
        repositories = {repository_key(w): w["worktree"].get("repo_root")
                        or str(Path(repository_key(w)).parent)
                        for w in linked_spaces.values() if repository_key(w)}
        branches = {}
        try:
            for repo, root in repositories.items():
                result = self.client.call("worktree.list", cwd=root)
                if (result.get("source", {}).get("repo_key") != repo
                        or not isinstance(result.get("worktrees"), list)):
                    return None
                for entry in result["worktrees"]:
                    workspace = linked_spaces.get(entry.get("open_workspace_id"))
                    branch = entry.get("branch")
                    if (workspace and isinstance(branch, str) and branch
                            and not entry.get("is_bare") and not entry.get("is_detached")
                            and repository_key(workspace) == repo
                            and entry.get("path") == workspace["worktree"].get("checkout_path")):
                        branches.setdefault(branch, []).append(workspace)
        except (ApiError, OSError, ValueError):
            return None
        return branches

    def associate_finished_agents(self, panes, workspaces):
        sources = list(self.exited_panes.values())
        self.associated_completions = {
            pid: identity for pid, identity in self.associated_completions.items()
            if pid in self.completed and pid in panes and panes[pid].get("agent")}
        sources += [p for p in panes.values() if p.get("agent") and p["pane_id"] in self.completed
                    and self.associated_completions.get(p["pane_id"]) != association_identity(p)]
        sources = [p for p in sources if (space_tokens(p) or p.get("tokens", {}).get("worktree_branch"))
                   and not linked(workspaces.get(p.get("workspace_id"),
                                  self.previous_workspaces.get(p.get("workspace_id"), {})))]
        if not sources:
            self.exited_panes.clear()
            return
        branches = (self.branch_workspaces(workspaces.values())
                    if any(not space_tokens(p) for p in sources) else {})
        self.exited_panes.clear()
        for pane in sources:
            exact = space_tokens(pane)
            branch = pane.get("tokens", {}).get("worktree_branch")
            if exact:
                targets = [w for wid, w in workspaces.items() if linked(w) and wid in exact
                           and exact.get(wid) == workspace_token_value(w)]
            else:
                if branches is None:
                    self.record_exit(pane)
                    continue
                targets = branches.get(branch, [])
                if len(targets) != 1:
                    targets = []
            self.associated_completions[pane["pane_id"]] = association_identity(pane)
            for target in targets:
                self.external_cleanup[target["workspace_id"]] = {
                    "branch": None if exact else branch, "worktree": target["worktree"]}

    def external_ready(self, workspace, snapshot):
        association = self.external_cleanup.get(workspace["workspace_id"])
        if not association or association["worktree"] != workspace.get("worktree"):
            return False
        branch, wid = association["branch"], workspace["workspace_id"]
        legacy_owners = [p for p in snapshot["panes"]
                         if p.get("tokens", {}).get("worktree_branch") and not space_tokens(p)]
        branches = self.branch_workspaces(snapshot["workspaces"]) if branch or legacy_owners else {}
        if branches is None:
            return False
        targets = (branches.get(branch, []) if branch else
                   [w for w in snapshot["workspaces"] if w["workspace_id"] == wid])
        if (len(targets) != 1 or targets[0]["workspace_id"] != workspace["workspace_id"]
                or targets[0].get("worktree") != workspace.get("worktree")):
            return False
        if default_checkout(workspace) and any(
            linked(w) and w["workspace_id"] != wid and repository_key(w) == repository_key(workspace)
            for w in snapshot["workspaces"]
        ):
            return False
        # A second agent using the same branch must finish too. Never close its
        # shared workspace, or an unrelated agent occupying the target space.
        for pane in snapshot["panes"]:
            if pane.get("workspace_id") == workspace["workspace_id"] and pane.get("agent"):
                return False
            exact = space_tokens(pane)
            owns_target = (exact.get(wid) == workspace_token_value(workspace) if exact else
                           any(w["workspace_id"] == wid for w in branches.get(
                               pane.get("tokens", {}).get("worktree_branch"), [])))
            if not owns_target:
                continue
            if pane.get("agent") and (
                pane["pane_id"] not in self.completed
                or pane.get("terminal_id") != self.previous.get(pane["pane_id"], {}).get("terminal_id")
            ):
                return False
            if not self.safe_panes([pane]):
                return False
        return True

    def refresh(self):
        snapshot = self.snapshot()
        panes = {p["pane_id"]: p for p in snapshot["panes"]}
        workspaces = {w["workspace_id"]: w for w in snapshot["workspaces"]}
        for wid in self.previous_workspaces.keys() - workspaces.keys():
            repo = repository_key(self.previous_workspaces[wid])
            if repo and linked(self.previous_workspaces[wid]) and not default_checkout(self.previous_workspaces[wid]):
                self.repository_cleanup.add(repo)
        closed = set()
        terminals = {p.get("terminal_id") for p in panes.values()}
        for pane_id, old in self.previous.items():
            current = panes.get(pane_id)
            if old.get("agent") and (
                (current is None and old.get("terminal_id") not in terminals)
                or (current is not None and not current.get("agent"))
            ):
                self.departed.add(old.get("workspace_id"))
                self.record_exit(old)
            if current and (current.get("terminal_id") != old.get("terminal_id")
                            or current.get("agent") != old.get("agent")):
                self.completed.discard(pane_id)
        for pane_id, pane in panes.items():
            old = self.previous.get(pane_id, {})
            status = pane.get("agent_status")
            if pane.get("agent") and (status == "done" or (
                status == "idle" and old.get("agent_status") == "working"
                and pane.get("terminal_id") == old.get("terminal_id")
            )):
                self.completed.add(pane_id)
            elif status in {"working", "blocked", "unknown"}:
                self.completed.discard(pane_id)
        self.completed.intersection_update(panes)
        self.departed.intersection_update(workspaces)
        self.external_cleanup = {wid: a for wid, a in self.external_cleanup.items()
                                 if wid in workspaces and a["worktree"] == workspaces[wid].get("worktree")}
        self.associate_finished_agents(panes, workspaces)
        missing = {wid for wid, ws in workspaces.items() if missing_checkout(ws)}
        for workspace_id, workspace in workspaces.items():
            if not linked(workspace):
                continue
            members = [p for p in panes.values() if p.get("workspace_id") == workspace_id]
            reason = None
            # Two observations avoid closing during a transient checkout rename.
            if workspace_id in missing & self.missing:
                reason = "worktree directory removed"
            elif any(p["pane_id"] in self.completed for p in members):
                reason = "agent completed"
            elif workspace_id in self.departed:
                reason = "agent or its tab exited"
            elif workspace_id in self.external_cleanup:
                reason = "associated agent finished or exited"
            if reason and self.safe_panes(members):
                if self.close_if_current(workspace, reason):
                    closed.add(workspace_id)
                    repo = repository_key(workspace)
                    if repo and not default_checkout(workspace):
                        self.repository_cleanup.add(repo)
        self.previous = panes
        self.previous_workspaces = {wid: w for wid, w in workspaces.items() if wid not in closed}
        self.missing = missing
        self.close_idle_repository_spaces()

    def close_idle_repository_spaces(self):
        if not self.repository_cleanup:
            return
        snapshot = self.snapshot()
        for repo in list(self.repository_cleanup):
            group = [w for w in snapshot["workspaces"] if repository_key(w) == repo]
            children = [w for w in group if linked(w)]
            if children:
                if len(children) != 1 or not self.close_if_current(children[0], "last task workspace closed"):
                    continue
                self.previous_workspaces.pop(children[0]["workspace_id"], None)
                snapshot = self.snapshot()
                group = [w for w in snapshot["workspaces"] if repository_key(w) == repo]
            if not group:
                self.repository_cleanup.discard(repo)
            elif len(group) == 1 and primary(group[0]):
                if self.close_if_current(group[0], "repository workspaces closed"):
                    self.repository_cleanup.discard(repo)
                    self.previous_workspaces.pop(group[0]["workspace_id"], None)

    def close_if_current(self, workspace, reason):
        workspace_id = workspace["workspace_id"]
        primary_cleanup = reason == "repository workspaces closed"
        try:
            current = self.client.call("workspace.get", workspace_id=workspace_id).get("workspace")
            if not current or current.get("worktree") != workspace.get("worktree"):
                return
            if not (primary(current) if primary_cleanup else linked(current)):
                return
            panes = self.client.call("pane.list", workspace_id=workspace_id).get("panes")
            if not isinstance(panes, list) or not self.safe_panes(panes):
                return
            if primary_cleanup or reason == "last task workspace closed":
                if any(p.get("agent") for p in panes) or (not primary_cleanup and not default_checkout(current)):
                    return
                # Recheck the repository group in case another task space opened.
                snapshot = self.snapshot()
                remaining = [w for w in snapshot["workspaces"]
                             if repository_key(w) == repository_key(current)
                             and (primary_cleanup or linked(w))]
                if (len(remaining) != 1 or remaining[0]["workspace_id"] != workspace_id
                        or remaining[0].get("worktree") != current.get("worktree")):
                    return
                members = [p for p in snapshot["panes"] if p.get("workspace_id") == workspace_id]
                if any(p.get("agent") for p in members) or not self.safe_panes(members):
                    return
            if reason == "associated agent finished or exited":
                snapshot = self.snapshot()
                if not self.external_ready(current, snapshot):
                    return
                members = [p for p in snapshot["panes"] if p.get("workspace_id") == workspace_id]
                if not self.safe_panes(members):
                    return
            if reason == "worktree directory removed" and not missing_checkout(current):
                return
            if reason == "agent completed" and not any(
                p.get("pane_id") in self.completed and p.get("agent")
                and p.get("agent_status") in {"idle", "done"}
                and p.get("terminal_id") == self.previous.get(p.get("pane_id"), p).get("terminal_id")
                for p in panes
            ):
                return
            if not self.client.enabled():
                return
            self.client.call("workspace.close", workspace_id=workspace_id)
            self.departed.discard(workspace_id)
            self.external_cleanup.pop(workspace_id, None)
            log(f"closed {workspace_id}: {reason}")
            return True
        except (ApiError, OSError, ValueError) as exc:
            log(f"skip {workspace_id}: {exc}")


def watch(client, lock_fd):
    # Keep the inherited flock descriptor alive for the entire watcher lifetime.
    stream = None
    try:
        if not client.enabled():
            return
        stream = client.subscribe()
        engine = Engine(client)
        next_scan = time.monotonic()
        log("watcher started")
        while True:
            topology_changed = False
            for message in stream.read(max(0, next_scan - time.monotonic())):
                if "error" in message:
                    raise ApiError(str(message["error"]))
                if "event" in message:
                    engine.event(message)
                    topology_changed |= message["event"] in {
                        "pane.created", "pane_created", "pane.closed", "pane_closed",
                        "pane.moved", "pane_moved", "pane.agent_detected", "pane_agent_detected"}
            if topology_changed:
                replacement = client.subscribe()
                stream.sock.close()
                stream = replacement
                next_scan = time.monotonic()
            if time.monotonic() >= next_scan:
                if not client.enabled():
                    log("watcher stopped: plugin disabled, unlinked, or replaced")
                    return
                engine.refresh()
                if set(engine.previous) != stream.pane_ids:
                    replacement = client.subscribe()
                    stream.sock.close()
                    stream = replacement
                next_scan = time.monotonic() + INTERVAL
    finally:
        if stream:
            stream.sock.close()
        os.close(lock_fd)


def start(client, state_dir):
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Separate named sessions and replacements at a different checkout path.
    key = hashlib.sha256(f"{client.socket_path}:{ROOT}".encode()).hexdigest()[:16]
    lock = state_dir / f"watch-{key}.lock"
    with lock.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        with (state_dir / f"watch-{key}.log").open("a") as output:
            proc = subprocess.Popen(
                [sys.executable, str(ROOT / "autoclose.py"), "watch", "--lock-fd", str(handle.fileno())],
                stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                start_new_session=True, pass_fds=(handle.fileno(),),
            )
        handle.seek(0)
        handle.truncate()
        handle.write(str(proc.pid) + "\n")
        handle.flush()
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["start", "watch"], nargs="?", default="start")
    parser.add_argument("--lock-fd", type=int)
    args = parser.parse_args()
    socket_path = os.environ.get("HERDR_SOCKET_PATH")
    state_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if os.environ.get("HERDR_ENV") != "1" or not socket_path or not state_dir:
        log("missing Herdr runtime context; use the plugin Start action")
        return 1
    client = Client(socket_path)
    try:
        if args.command == "start":
            return start(client, Path(state_dir))
        if args.lock_fd is None:
            parser.error("watch requires a lock inherited from start")
        watch(client, args.lock_fd)
    except (ApiError, OSError, ValueError) as exc:
        log(f"watcher stopped: {exc}; the next lifecycle hook will restart it")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
