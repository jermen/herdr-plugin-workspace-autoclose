"""Opt-in live tests in an isolated Herdr server; never use the user's session."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import autoclose as app


def run():
    with tempfile.TemporaryDirectory(prefix="workspace-autoclose-integration-") as tmp:
        root = Path(tmp)
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
        env.update(XDG_CONFIG_HOME=str(root / "config"), XDG_DATA_HOME=str(root / "data"),
                   XDG_STATE_HOME=str(root / "state"), HERDR_CONFIG_PATH=str(root / "config.toml"))
        (root / "config.toml").write_text("")
        socket_path = root / "config/herdr/sessions/autoclose-integration/herdr.sock"
        client = app.Client(str(socket_path))

        def git(*args):
            subprocess.run(["git", *map(str, args)], env=env, text=True,
                           capture_output=True, check=True)

        def cli(*args):
            return subprocess.run(["herdr", "--session", "autoclose-integration", *args],
                                  env=env, text=True, capture_output=True, check=True)

        seed = root / "seed"
        git("init", "--initial-branch=fixture", seed)
        git("-C", seed, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "commit", "--allow-empty", "-m", "Fixture")
        repo = root / "repo with spaces"
        repo.mkdir()
        git("clone", "--bare", seed, repo / ".bare")
        (repo / ".git").write_text("gitdir: ./.bare\n")

        def wait_for(predicate, description, timeout=12):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    if predicate():
                        return
                except (OSError, app.ApiError, ValueError):
                    pass
                time.sleep(0.1)
            raise AssertionError(description)

        def spaces():
            return {w["workspace_id"] for w in client.call("workspace.list")["workspaces"]}

        def repository_spaces():
            return [w for w in client.call("workspace.list")["workspaces"]
                    if w.get("worktree", {}).get("repo_key") == str(repo / ".bare")]

        def root_pane():
            primary = next(w for w in repository_spaces() if not w["worktree"]["is_linked_worktree"])
            return primary["workspace_id"], client.call("pane.list", workspace_id=primary["workspace_id"])["panes"][0]

        def open_checkout(checkout):
            result = client.call("worktree.open", cwd=str(repo), path=str(checkout),
                                 label=checkout.name, focus=False)
            workspace = result["workspace"]
            wid = workspace["workspace_id"]
            pane = client.call("pane.list", workspace_id=wid)["panes"][0]
            return wid, pane, checkout

        def open_worktree(name):
            checkout = repo / name
            git("-C", repo, "worktree", "add", "-b", name, checkout, "fixture")
            return open_checkout(checkout)

        def report(pane, state):
            client.call("pane.report_agent", pane_id=pane["pane_id"], source="autoclose-test",
                        agent="autoclose-fixture", state=state)

        with (root / "server.log").open("w") as output:
            server = subprocess.Popen(["herdr", "--session", "autoclose-integration", "server"],
                                      env=env, stdout=output, stderr=output)
        try:
            wait_for(lambda: socket_path.exists() and client.call("ping"), "test server did not start")
            assert client.call("plugin.list")["plugins"] == [], "plugin registry was not isolated"
            client.call("plugin.link", path=str(app.ROOT), enabled=True)
            client.call("plugin.action.invoke", plugin_id=app.PLUGIN_ID, action_id="start")

            wid, pane, checkout = open_worktree("removed-directory")
            time.sleep(2.5)
            # Intentionally leave Git's prunable record in place.
            shutil.rmtree(checkout)
            wait_for(lambda: wid not in spaces(), "plain directory removal left the space open")
            assert "removed-directory" in subprocess.check_output(
                ["git", "-C", str(repo), "branch", "--list"], env=env, text=True)
            print("PASS: directory removal without an agent event or Git prune", flush=True)

            wid, pane, checkout = open_worktree("completed")
            report(pane, "working")
            time.sleep(2.5)
            report(pane, "idle")
            wait_for(lambda: wid not in spaces(), "agent completion left the space open")
            assert checkout.is_dir()
            print("PASS: agent completion preserves the Git checkout", flush=True)

            wid, pane, checkout = open_worktree("agent-exited")
            report(pane, "working")
            time.sleep(2.5)
            client.call("pane.release_agent", pane_id=pane["pane_id"],
                        source="autoclose-test", agent="autoclose-fixture")
            wait_for(lambda: wid not in spaces(), "agent exit left the space open")
            assert checkout.is_dir()
            print("PASS: agent exit to shell", flush=True)

            wid, pane, checkout = open_worktree("tab-closed")
            report(pane, "working")
            client.call("tab.create", workspace_id=wid, cwd=str(checkout), focus=False)
            time.sleep(2.5)
            client.call("tab.close", tab_id=pane["tab_id"])
            wait_for(lambda: wid not in spaces(), "closed agent tab left a shell space open")
            print("PASS: agent tab closed while another shell tab remains", flush=True)

            wid, pane, checkout = open_worktree("busy")
            report(pane, "blocked")
            time.sleep(2.5)
            shutil.rmtree(checkout)
            time.sleep(4.5)
            assert wid in spaces(), "blocked agent workspace was closed"
            client.call("pane.release_agent", pane_id=pane["pane_id"],
                        source="autoclose-test", agent="autoclose-fixture")
            wait_for(lambda: wid not in spaces(), "cleanup did not resume after agent exit")
            print("PASS: blocked agent preserved until exit", flush=True)

            master_id, master_pane, master_checkout = open_worktree("master")
            git("--git-dir", repo / ".bare", "symbolic-ref", "HEAD", "refs/heads/master")
            first_id, first_pane, _ = open_worktree("first-task")
            last_id, last_pane, _ = open_worktree("last-task")
            report(first_pane, "working")
            report(last_pane, "working")
            time.sleep(2.5)
            report(first_pane, "idle")
            wait_for(lambda: first_id not in spaces(), "first task did not close")
            assert master_id in spaces(), "master closed before the last task"
            report(last_pane, "idle")
            wait_for(lambda: last_id not in spaces() and master_id not in spaces(),
                     "idle master remained after the last task completed")
            assert master_checkout.is_dir()
            wait_for(lambda: not repository_spaces(), "empty primary repository space stayed in the menu")
            assert (repo / ".bare").is_dir()
            print("PASS: last completed task closes idle master and the empty repository root", flush=True)

            master_id, master_pane, _ = open_checkout(master_checkout)
            time.sleep(4.5)
            assert master_id in spaces(), "reopening master alone immediately closed it again"
            task_id, _, _ = open_worktree("manually-closed")
            time.sleep(2.5)
            client.call("workspace.close", workspace_id=task_id)
            wait_for(lambda: master_id not in spaces(), "manual task closure left idle master open")
            print("PASS: manual last-task closure cleans master; reopening master alone is safe", flush=True)

            master_id, master_pane, _ = open_checkout(master_checkout)
            cli("pane", "run", master_pane["pane_id"], "sleep 60")

            def master_busy():
                info = client.call("pane.process_info", pane_id=master_pane["pane_id"])["process_info"]
                return info["foreground_process_group_id"] != info["shell_pid"]

            wait_for(master_busy, "master's foreground command did not start")
            task_id, task_pane, _ = open_worktree("busy-master-task")
            report(task_pane, "working")
            time.sleep(2.5)
            report(task_pane, "idle")
            wait_for(lambda: task_id not in spaces(), "task with busy master did not close")
            time.sleep(2.5)
            assert master_id in spaces(), "master's foreground command was closed"
            cli("pane", "send-keys", master_pane["pane_id"], "ctrl+c")
            wait_for(lambda: master_id not in spaces(), "master did not close after its command stopped")
            print("PASS: foreground command in master defers cleanup until the shell is idle", flush=True)

            master_id, master_pane, _ = open_checkout(master_checkout)
            report(master_pane, "idle")
            task_id, _, _ = open_worktree("idle-agent-master-task")
            time.sleep(2.5)
            client.call("workspace.close", workspace_id=task_id)
            time.sleep(2.5)
            assert master_id in spaces(), "an idle agent in master was closed by sibling cleanup"
            client.call("pane.release_agent", pane_id=master_pane["pane_id"],
                        source="autoclose-test", agent="autoclose-fixture")
            wait_for(lambda: master_id not in spaces(), "master did not close after its own agent exited")
            print("PASS: any agent in master prevents sibling cleanup", flush=True)

            task_id, task_pane, _ = open_worktree("busy-root-task")
            primary_id, primary_pane = root_pane()
            cli("pane", "run", primary_pane["pane_id"], "sleep 60")

            def root_busy():
                info = client.call("pane.process_info", pane_id=primary_pane["pane_id"])["process_info"]
                return info["foreground_process_group_id"] != info["shell_pid"]

            wait_for(root_busy, "root's foreground command did not start")
            report(task_pane, "working")
            time.sleep(2.5)
            report(task_pane, "idle")
            wait_for(lambda: task_id not in spaces(), "task with busy root did not close")
            assert primary_id in spaces(), "root's foreground command was closed"
            next_id, next_pane, _ = open_worktree("new-task-before-root-cleanup")
            report(next_pane, "working")
            cli("pane", "send-keys", primary_pane["pane_id"], "ctrl+c")
            time.sleep(2.5)
            assert primary_id in spaces(), "root closed while a new task space existed"
            report(next_pane, "idle")
            wait_for(lambda: not repository_spaces(), "root did not close after the new task finished")
            print("PASS: busy root and a newly opened task prevent root cleanup", flush=True)

            task_id, task_pane, _ = open_worktree("agent-root-task")
            primary_id, primary_pane = root_pane()
            report(primary_pane, "idle")
            report(task_pane, "working")
            time.sleep(2.5)
            report(task_pane, "idle")
            wait_for(lambda: task_id not in spaces(), "task with root agent did not close")
            time.sleep(2.5)
            assert primary_id in spaces(), "idle agent in root was closed by cleanup"
            client.call("pane.release_agent", pane_id=primary_pane["pane_id"],
                        source="autoclose-test", agent="autoclose-fixture")
            wait_for(lambda: not repository_spaces(), "root did not close after its agent exited")
            print("PASS: root agent is preserved until it exits", flush=True)

            shared = client.call("workspace.create", cwd=str(root), label="shared", focus=False)["workspace"]
            shared_id = shared["workspace_id"]
            neighbor = client.call("pane.list", workspace_id=shared_id)["panes"][0]
            report(neighbor, "working")
            main_id, _, main_checkout = open_worktree("main")
            git("--git-dir", repo / ".bare", "symbolic-ref", "HEAD", "refs/heads/main")

            for action in ["complete", "release", "close-pane", "close-tab"]:
                if action != "complete":
                    main_id, _, _ = open_checkout(main_checkout)
                task_id, _, task_checkout = open_worktree("shared-" + action)
                if action == "close-tab":
                    tab = client.call("tab.create", workspace_id=shared_id, cwd=str(root), focus=False)["tab"]
                    source = next(p for p in client.call("pane.list", workspace_id=shared_id)["panes"]
                                  if p["tab_id"] == tab["tab_id"])
                else:
                    source = client.call("pane.split", target_pane_id=neighbor["pane_id"],
                                         direction="right", cwd=str(root), focus=False)["pane"]
                report(source, "working")
                client.call("pane.report_metadata", pane_id=source["pane_id"], source="git-worktree-add",
                            tokens={"worktree_branch": "shared-" + action})
                time.sleep(2.5)
                if action == "complete":
                    report(source, "idle")
                elif action == "release":
                    client.call("pane.release_agent", pane_id=source["pane_id"],
                                source="autoclose-test", agent="autoclose-fixture")
                elif action == "close-pane":
                    client.call("pane.close", pane_id=source["pane_id"])
                else:
                    client.call("tab.close", tab_id=source["tab_id"])
                wait_for(lambda: not repository_spaces(), "shared agent " + action + " left repository spaces open")
                assert shared_id in spaces()
                assert any(p["pane_id"] == neighbor["pane_id"] and p["agent_status"] == "working"
                           for p in client.call("pane.list", workspace_id=shared_id)["panes"])
                assert task_checkout.is_dir() and main_checkout.is_dir()
                print("PASS: shared agent " + action + " closes task, main and root; active neighbor survives", flush=True)

            for ending in ["complete", "close-pane"]:
                source = client.call("pane.split", target_pane_id=neighbor["pane_id"],
                                     direction="right", cwd=str(root), focus=False)["pane"]
                report(source, "working")
                hook_env = {**env, "HERDR_ENV": "1", "HERDR_SOCKET_PATH": str(socket_path),
                            "HERDR_WORKSPACE_ID": shared_id, "HERDR_PANE_ID": source["pane_id"]}
                owned = []
                for index in range(4):
                    project = root / (ending + " project " + str(index))
                    project.mkdir()
                    git("clone", "--bare", seed, project / ".bare")
                    (project / ".git").write_text("gitdir: ./.bare\n")
                    local_hook = project / ".bare/hooks/post-checkout"
                    local_hook.write_text("#!/bin/sh\ntouch hook-chained\n")
                    local_hook.chmod(0o755)
                    checkout = project / ("shared-ticket" if index < 3 else "temporary-chart")
                    args = ["-b", "shared-ticket"] if index < 3 else ["--detach"]
                    result = subprocess.run(
                        ["git", "-C", str(project), "-c", "core.hooksPath=" + str(app.ROOT / "hooks"),
                         "worktree", "add", *args, str(checkout), "fixture"],
                        env=hook_env, text=True, capture_output=True, check=True)
                    assert (checkout / "hook-chained").is_file(), result.stderr
                    workspace = next(w for w in client.call("workspace.list")["workspaces"]
                                     if w.get("worktree", {}).get("checkout_path") == str(checkout))
                    owned.append((workspace["workspace_id"], checkout))
                current = next(p for p in client.call("pane.list", workspace_id=shared_id)["panes"]
                               if p["pane_id"] == source["pane_id"])
                expected = {w["workspace_id"]: app.workspace_token_value(w)
                            for w in client.call("workspace.list")["workspaces"]
                            if w["workspace_id"] in {wid for wid, _ in owned}}
                assert app.space_tokens(current) == expected, (app.space_tokens(current), expected)
                assert current["tokens"]["worktree_branch"] != "shared-ticket"
                time.sleep(2.5)
                if ending == "complete":
                    report(source, "idle")
                else:
                    client.call("pane.close", pane_id=source["pane_id"])
                repos = {str(path.parent / ".bare") for _, path in owned}
                wait_for(lambda: not any(app.repository_key(w) in repos
                         for w in client.call("workspace.list")["workspaces"]),
                         "multi-repository " + ending + " left a workspace open")
                assert all(path.is_dir() for _, path in owned)
                assert shared_id in spaces()
                print("PASS: Git hook retains three same-branch repos and detached checkout on " + ending, flush=True)

            client.call("plugin.disable", plugin_id=app.PLUGIN_ID)
            wid, pane, checkout = open_worktree("disabled")
            shutil.rmtree(checkout)
            time.sleep(4.5)
            assert wid in spaces(), "disabled plugin still closed a workspace"
            print("PASS: disabling stops the watcher", flush=True)
        except BaseException:
            print((root / "server.log").read_text()[-4000:], file=sys.stderr)
            for path in root.rglob("watch-*.log"):
                print(str(path), path.read_text()[-6000:], file=sys.stderr)
            raise
        finally:
            try:
                client.call("server.stop")
            except (OSError, ValueError, app.ApiError):
                server.terminate()
            server.wait(timeout=10)


if __name__ == "__main__":
    run()
