import copy
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import autoclose as app


class FakeHerdr:
    def __init__(self, root):
        self.enabled_value = True
        self.closed = []
        self.fail = set()
        self.panes = [{"pane_id": "w2:p1", "workspace_id": "w2", "tab_id": "w2:t1",
                       "terminal_id": "term1", "agent": None, "agent_status": "unknown"}]
        self.workspaces = [
            {"workspace_id": "w1", "worktree": {"is_linked_worktree": False}},
            {"workspace_id": "w2", "worktree": {"is_linked_worktree": True,
             "checkout_path": str(root / "branch"), "repo_key": str(root / ".bare")}},
        ]
        self.processes = {"shell_pid": 10, "foreground_process_group_id": 10,
                          "foreground_processes": [{"pid": 10, "name": "bash"}]}
        self.process_overrides = {}
        self.fresh_panes = None
        self.fresh_workspace = None
        self.branches = {}

    def enabled(self):
        return self.enabled_value

    def call(self, method, **params):
        if method in self.fail:
            raise app.ApiError("unavailable")
        if method == "session.snapshot":
            return copy.deepcopy({"snapshot": {"panes": self.panes, "workspaces": self.workspaces}})
        if method == "pane.process_info":
            return copy.deepcopy({"process_info": self.process_overrides.get(params["pane_id"], self.processes)})
        if method == "workspace.get":
            return copy.deepcopy({"workspace": self.fresh_workspace or next(
                (w for w in self.workspaces if w["workspace_id"] == params["workspace_id"]), None)})
        if method == "pane.list":
            return copy.deepcopy({"panes": self.fresh_panes if self.fresh_panes is not None else [
                p for p in self.panes if p["workspace_id"] == params["workspace_id"]]})
        if method == "worktree.list":
            repo = str(Path(params["cwd"]) / ".bare")
            return {"source": {"repo_key": repo}, "worktrees": [
                {"path": w["worktree"]["checkout_path"], "branch": self.branches.get(w["workspace_id"], "branch"),
                 "open_workspace_id": w["workspace_id"], "is_bare": False, "is_detached": False}
                for w in self.workspaces if app.linked(w) and app.repository_key(w) == repo]}
        if method == "workspace.close":
            self.closed.append(params["workspace_id"])
            self.workspaces = [w for w in self.workspaces if w["workspace_id"] != params["workspace_id"]]
            self.panes = [p for p in self.panes if p["workspace_id"] != params["workspace_id"]]
            return {"type": "ok"}
        raise AssertionError((method, params))


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".bare").mkdir()
        (self.root / "branch").mkdir()
        self.api = FakeHerdr(self.root)
        self.engine = app.Engine(self.api)

    def agent(self, status="working"):
        self.api.panes[0].update(agent="codex", agent_status=status)

    def event(self, event, **data):
        self.engine.event({"event": event, "data": {
            "workspace_id": "w2", "pane_id": "w2:p1", **data}})

    def add_master(self):
        (self.root / "master").mkdir()
        self.api.workspaces.append({"workspace_id": "w3", "label": "renamed master", "worktree": {
            "is_linked_worktree": True, "checkout_path": str(self.root / "master"),
            "repo_key": str(self.root / ".bare")}})
        self.api.panes.append({"pane_id": "w3:p1", "workspace_id": "w3", "tab_id": "w3:t1",
                               "terminal_id": "master-shell", "agent_status": "unknown"})
        mock = patch.object(app, "default_checkout", side_effect=lambda w: w["workspace_id"] == "w3")
        mock.start()
        self.addCleanup(mock.stop)

    def add_primary(self):
        self.api.workspaces[0]["worktree"].update(
            repo_root=str(self.root), checkout_path=str(self.root), repo_key=str(self.root / ".bare"))
        self.api.panes.append({"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1",
                               "terminal_id": "primary-shell", "agent_status": "unknown"})

    def test_last_task_closes_idle_repository_root(self):
        self.add_primary()
        self.agent("done")
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2", "w1"])
        self.assertTrue((self.root / ".bare").is_dir())
        self.assertTrue((self.root / "branch").is_dir())
        self.assertEqual(self.engine.repository_cleanup, set())

    def test_task_master_and_root_close_in_order(self):
        self.add_primary()
        self.add_master()
        self.agent("done")
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2", "w3", "w1"])
        self.assertEqual(self.api.workspaces, [])

    def test_startup_does_not_close_root_alone(self):
        self.add_primary()
        self.api.workspaces.pop(1)
        self.api.panes.pop(0)
        self.engine.refresh()
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])

    def test_any_agent_protects_repository_root(self):
        self.add_primary()
        root_pane = self.api.panes[-1]
        root_pane.update(agent="codex", agent_status="idle")
        self.agent("done")
        self.engine.refresh()
        for state in ["idle", "working", "blocked", "unknown", "done"]:
            root_pane["agent_status"] = state
            self.engine.refresh()
            self.assertEqual(self.api.closed, ["w2"])
        root_pane.update(agent=None, agent_status="unknown")
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2", "w1"])

    def test_foreground_command_protects_repository_root(self):
        self.add_primary()
        self.api.process_overrides["w1:p1"] = {"shell_pid": 10, "foreground_process_group_id": 20,
                                                "foreground_processes": [{"pid": 20, "name": "vim"}]}
        self.agent("done")
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2"])
        self.api.process_overrides.clear()
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2", "w1"])

    def test_another_task_keeps_root_open_until_its_manual_closure(self):
        self.add_primary()
        self.api.workspaces.append({**copy.deepcopy(self.api.workspaces[1]), "workspace_id": "w4"})
        self.agent("done")
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2"])
        self.api.workspaces = [w for w in self.api.workspaces if w["workspace_id"] != "w4"]
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2", "w1"])

    def test_task_opened_during_root_cleanup_is_preserved(self):
        self.add_primary()
        self.agent("done")
        call = self.api.call
        snapshots = 0

        def racing_call(method, **params):
            nonlocal snapshots
            if method == "session.snapshot":
                snapshots += 1
                if snapshots == 3:
                    self.api.workspaces.append({"workspace_id": "w4", "worktree": {
                        "is_linked_worktree": True, "repo_key": str(self.root / ".bare"),
                        "checkout_path": str(self.root / "new-task")}})
            return call(method, **params)

        with patch.object(self.api, "call", side_effect=racing_call):
            self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2"])

    def test_agent_started_during_root_cleanup_is_preserved(self):
        self.add_primary()
        self.agent("done")
        call = self.api.call
        snapshots = 0

        def racing_call(method, **params):
            nonlocal snapshots
            if method == "session.snapshot":
                snapshots += 1
                if snapshots == 3:
                    self.api.panes[0].update(agent="codex", agent_status="idle")
            return call(method, **params)

        with patch.object(self.api, "call", side_effect=racing_call):
            self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2"])

    def test_group_close_refusal_does_not_force_child_closure(self):
        self.add_primary()
        self.agent("done")
        call = self.api.call

        def reject_root_close(method, **params):
            if method == "workspace.close" and params["workspace_id"] == "w1":
                self.assertEqual(params, {"workspace_id": "w1"})
                raise app.ApiError("workspace_group_close_required")
            return call(method, **params)

        with patch.object(self.api, "call", side_effect=reject_root_close):
            self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2"])
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2", "w1"])

    def test_root_checkout_must_match_repository_root(self):
        self.add_primary()
        self.api.workspaces[0]["worktree"]["checkout_path"] = str(self.root / "elsewhere")
        self.agent("done")
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2"])

    def test_last_completed_task_also_closes_idle_master(self):
        self.add_master()
        self.agent("done")
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2", "w3"])
        self.assertTrue((self.root / "master").is_dir())
        self.assertEqual([w["workspace_id"] for w in self.api.workspaces], ["w1"])
        self.assertEqual(self.engine.repository_cleanup, set())

    def test_initial_master_alone_is_preserved(self):
        self.add_master()
        self.api.workspaces.pop(1)
        self.api.panes.pop(0)
        self.engine.refresh()
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])

    def test_manual_task_closure_also_cleans_master(self):
        self.add_master()
        self.engine.refresh()
        self.api.workspaces.pop(1)
        self.api.panes.pop(0)
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w3"])

    def test_master_waits_for_last_sibling_and_ignores_other_repositories(self):
        self.add_master()
        self.api.workspaces.extend([
            {**copy.deepcopy(self.api.workspaces[1]), "workspace_id": "w4"},
            {"workspace_id": "w5", "worktree": {"is_linked_worktree": True,
             "repo_key": str(self.root / "other-repo"), "checkout_path": str(self.root / "other")}},
        ])
        self.agent("done")
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2"])
        self.api.workspaces = [w for w in self.api.workspaces if w["workspace_id"] != "w4"]
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2", "w3"])
        self.assertIn("w5", [w["workspace_id"] for w in self.api.workspaces])

    def test_foreground_command_in_master_defers_cleanup(self):
        self.add_master()
        self.api.process_overrides["w3:p1"] = {"shell_pid": 10, "foreground_process_group_id": 20,
                                                "foreground_processes": [{"pid": 20, "name": "vim"}]}
        self.agent("done")
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2"])
        self.api.process_overrides.clear()
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2", "w3"])

    def test_agent_in_master_is_not_sibling_cleanup(self):
        self.add_master()
        master = self.api.panes[1]
        master.update(agent="codex", agent_status="idle")
        self.agent("done")
        self.engine.refresh()
        for state in ["idle", "unknown", "blocked", "working"]:
            master["agent_status"] = state
            self.engine.refresh()
            self.assertEqual(self.api.closed, ["w2"])

    def test_new_task_opened_during_master_cleanup_is_preserved(self):
        self.add_master()
        self.agent("done")
        call = self.api.call
        snapshots = 0

        def racing_call(method, **params):
            nonlocal snapshots
            if method == "session.snapshot":
                snapshots += 1
                if snapshots == 3:
                    self.api.workspaces.append({"workspace_id": "w4", "worktree": {
                        "is_linked_worktree": True, "repo_key": str(self.root / ".bare"),
                        "checkout_path": str(self.root / "new-task")}})
            return call(method, **params)

        with patch.object(self.api, "call", side_effect=racing_call):
            self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2"])

    def test_agent_started_during_master_cleanup_is_preserved(self):
        self.add_master()
        self.agent("done")
        call = self.api.call
        snapshots = 0

        def racing_call(method, **params):
            nonlocal snapshots
            if method == "session.snapshot":
                snapshots += 1
                if snapshots == 3:
                    self.api.panes[0].update(agent="codex", agent_status="idle")
            return call(method, **params)

        with patch.object(self.api, "call", side_effect=racing_call):
            self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2"])

    def test_reopening_master_after_cleanup_does_not_close_it_again(self):
        self.add_master()
        workspace, pane = copy.deepcopy(self.api.workspaces[2]), copy.deepcopy(self.api.panes[1])
        self.agent("done")
        self.engine.refresh()
        self.api.workspaces.append(workspace)
        self.api.panes.append(pane)
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2", "w3"])

    def test_failed_task_close_does_not_trigger_master_cleanup(self):
        self.add_master()
        self.agent("done")
        self.api.fail.add("workspace.close")
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])
        self.assertEqual(self.engine.repository_cleanup, set())

    def test_primary_space_is_not_master_cleanup(self):
        self.add_master()
        self.api.workspaces[2]["worktree"]["is_linked_worktree"] = False
        self.agent("done")
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2"])

    def test_done_closes_linked_space_but_preserves_checkout(self):
        self.agent("done")
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2"])
        self.assertTrue((self.root / "branch").is_dir())
        self.assertEqual([w["workspace_id"] for w in self.api.workspaces], ["w1"])

    def test_focused_working_to_idle_is_completion(self):
        self.agent()
        self.engine.refresh()
        self.agent("idle")
        self.event("pane.agent_status_changed", agent_status="idle")
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2"])

    def test_initial_idle_does_not_mean_completed(self):
        self.agent("idle")
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])

    def test_removed_directory_needs_no_agent_event_or_git_prune(self):
        (self.root / "branch").rmdir()
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2"])

    def test_transiently_missing_directory_is_preserved(self):
        (self.root / "branch").rmdir()
        self.engine.refresh()
        (self.root / "branch").mkdir()
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])

    def test_unavailable_repo_is_not_removal(self):
        (self.root / "branch").rmdir()
        (self.root / ".bare").rmdir()
        self.engine.refresh()
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])

    def test_broken_symlink_is_not_removal(self):
        (self.root / "branch").rmdir()
        (self.root / "branch").symlink_to(self.root / "missing")
        self.engine.refresh()
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])

    def test_working_and_blocked_agents_keep_deleted_space_open(self):
        (self.root / "branch").rmdir()
        for status in ["working", "blocked", "unknown"]:
            self.agent(status)
            self.engine.refresh()
            self.engine.refresh()
        self.assertEqual(self.api.closed, [])

    def test_foreground_command_keeps_deleted_space_open(self):
        (self.root / "branch").rmdir()
        self.api.processes.update(foreground_process_group_id=20,
                                  foreground_processes=[{"pid": 20, "name": "vim"}])
        self.engine.refresh()
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])

    def test_failed_process_inspection_keeps_space_open(self):
        (self.root / "branch").rmdir()
        self.api.fail.add("pane.process_info")
        self.engine.refresh()
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])

    def test_agent_exit_to_shell_closes_space(self):
        self.agent()
        self.engine.refresh()
        self.api.panes[0].update(agent=None, agent_status="unknown")
        self.event("pane.agent_detected", agent=None, released=True)
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2"])

    def test_agent_exit_detected_from_snapshot(self):
        self.agent()
        self.engine.refresh()
        self.api.panes[0].update(agent=None, agent_status="unknown")
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2"])

    def test_closed_agent_tab_closes_remaining_shell_workspace(self):
        self.agent()
        self.engine.refresh()
        self.api.panes = [{"pane_id": "w2:p2", "workspace_id": "w2", "tab_id": "w2:t2",
                           "terminal_id": "term2", "agent": None, "agent_status": "unknown"}]
        self.event("tab.closed", tab_id="w2:t1")
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2"])

    def test_closed_shell_tab_does_not_close_workspace(self):
        self.engine.refresh()
        self.event("tab.closed", tab_id="w2:t1")
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])

    def test_exited_agent_pane_can_close_empty_workspace(self):
        self.agent()
        self.engine.refresh()
        self.event("pane.exited")
        self.api.panes = []
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2"])

    def test_other_working_agent_blocks_completion_cleanup(self):
        self.agent("done")
        self.api.panes.append({**self.api.panes[0], "pane_id": "w2:p2",
                               "terminal_id": "term2", "agent_status": "working"})
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])

    def test_busy_sibling_shell_blocks_completion_cleanup(self):
        self.agent("done")
        self.api.panes.append({**self.api.panes[0], "pane_id": "w2:p2", "agent": None})
        self.api.processes["foreground_process_group_id"] = 20
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])

    def test_primary_is_not_closed_by_its_own_completion(self):
        self.agent("done")
        self.api.workspaces[1]["worktree"]["is_linked_worktree"] = False
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])

    def test_new_work_supersedes_stale_done_event(self):
        self.agent()
        self.engine.refresh()
        self.event("pane.agent_status_changed", agent_status="done")
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])

    def test_current_working_state_wins_over_snapshot(self):
        self.agent("done")
        self.api.fresh_panes = [{**self.api.panes[0], "agent_status": "working"}]
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])

    def test_changed_workspace_provenance_is_not_closed(self):
        self.agent("done")
        self.api.fresh_workspace = {"workspace_id": "w2", "worktree": {
            **self.api.workspaces[1]["worktree"], "checkout_path": str(self.root / "replacement")}}
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])

    def test_disabled_plugin_does_not_close(self):
        self.agent("done")
        self.api.enabled_value = False
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])

    def test_failed_close_does_not_claim_success(self):
        self.agent("done")
        self.api.fail.add("workspace.close")
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])

    def test_moving_agent_is_not_exiting(self):
        self.agent()
        self.engine.refresh()
        moved = {**self.api.panes[0], "pane_id": "w1:p2", "workspace_id": "w1", "tab_id": "w1:t1"}
        self.event("pane.moved", previous_pane_id="w2:p1", pane=moved)
        self.api.panes = [moved]
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])

    def test_missing_snapshot_fails_closed(self):
        self.agent("done")
        with patch.object(self.api, "call", return_value={"snapshot": {}}):
            with self.assertRaises(app.ApiError):
                self.engine.refresh()
        self.assertEqual(self.api.closed, [])


class SharedPaneTests(unittest.TestCase):
    setUp = EngineTests.setUp
    agent = EngineTests.agent
    def shared_agent(self, status="working", branch="branch"):
        pane = {"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1",
                "terminal_id": "shared-agent", "agent": "codex", "agent_status": status,
                "tokens": {"worktree_branch": branch}}
        self.api.panes.append(pane)
        return pane

    def test_shared_completion_preserves_shared_space_and_other_agent(self):
        source = self.shared_agent()
        self.api.panes.append({**source, "pane_id": "w1:p2", "terminal_id": "other", "tokens": {}})
        self.engine.refresh()
        source["agent_status"] = "idle"
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2"])
        self.assertEqual(len(self.api.panes), 2)
        self.assertTrue((self.root / "branch").is_dir())

    def test_shared_release_and_closed_pane_or_tab(self):
        for event in [None, "pane.agent_detected", "pane.exited", "pane.closed", "tab.closed"]:
            with self.subTest(event=event):
                self.api = FakeHerdr(self.root)
                self.engine = app.Engine(self.api)
                source = self.shared_agent()
                self.engine.refresh()
                if event:
                    self.engine.event({"event": event, "data": {
                        "pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1",
                        "agent": None, "released": True}})
                if event == "pane.agent_detected":
                    source.update(agent=None, agent_status="unknown")
                else:
                    self.api.panes.remove(source)
                self.engine.refresh()
                self.assertEqual(self.api.closed, ["w2"])

    def test_shared_initial_idle_and_title_only_are_not_associations(self):
        source = self.shared_agent("idle")
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])
        source.update(agent_status="working", tokens={}, title="branch")
        self.engine.refresh()
        source["agent_status"] = "idle"
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])

    def test_duplicate_branch_across_repositories_is_ambiguous(self):
        self.api.workspaces.append({"workspace_id": "w4", "worktree": {
            "is_linked_worktree": True, "repo_key": str(self.root / "other/.bare"),
            "checkout_path": str(self.root / "other/branch")}})
        source = self.shared_agent()
        self.engine.refresh()
        self.api.panes.remove(source)
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])

    def test_second_associated_agent_must_finish(self):
        source = self.shared_agent()
        second = {**source, "pane_id": "w1:p2", "terminal_id": "second", "agent_status": "idle"}
        self.api.panes.append(second)
        self.engine.refresh()
        self.api.panes.remove(source)
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])
        second["agent_status"] = "working"
        self.engine.refresh()
        second["agent_status"] = "idle"
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2"])

    def test_agent_in_target_protects_it_from_shared_cleanup(self):
        source = self.shared_agent()
        self.agent("idle")
        self.engine.refresh()
        self.api.panes.remove(source)
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])

    def test_source_shell_command_defers_cleanup(self):
        source = self.shared_agent()
        self.engine.refresh()
        source.update(agent=None, agent_status="unknown")
        self.api.process_overrides[source["pane_id"]] = {"shell_pid": 10, "foreground_process_group_id": 20}
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])
        self.api.process_overrides.clear()
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2"])

    def test_git_lookup_failure_retries_departure(self):
        source = self.shared_agent()
        self.engine.refresh()
        self.api.panes.remove(source)
        self.api.fail.add("worktree.list")
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])
        self.api.fail.clear()
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2"])

    def test_reopening_target_after_shared_completion_stays_open(self):
        source = self.shared_agent()
        target = copy.deepcopy(self.api.workspaces[1])
        shell = copy.deepcopy(self.api.panes[0])
        self.engine.refresh()
        source["agent_status"] = "idle"
        self.engine.refresh()
        self.api.workspaces.append(target)
        self.api.panes.append(shell)
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2"])

    def test_shared_agent_move_is_not_exit(self):
        source = self.shared_agent()
        self.engine.refresh()
        source.update(pane_id="w1:p2", tab_id="w1:t2")
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])

    def test_closed_linked_space_does_not_become_a_shared_source(self):
        self.agent()
        self.api.panes[0]["tokens"] = {"worktree_branch": "branch"}
        self.api.workspaces.append({"workspace_id": "w4", "worktree": {
            "is_linked_worktree": True, "repo_key": str(self.root / "other/.bare"),
            "checkout_path": str(self.root / "other/branch")}})
        self.engine.refresh()
        self.api.workspaces.pop(1)
        self.api.panes.clear()
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])

    def add_other_repository(self):
        (self.root / "other/.bare").mkdir(parents=True)
        (self.root / "other/branch").mkdir()
        target = {"workspace_id": "w4", "worktree": {
            "is_linked_worktree": True, "repo_key": str(self.root / "other/.bare"),
            "checkout_path": str(self.root / "other/branch")}}
        self.api.workspaces.append(target)
        return target

    def own(self, source, target):
        source["tokens"][app.SPACE_TOKEN_PREFIX + target["workspace_id"]] = app.workspace_token_value(target)

    def test_exact_tokens_cover_multiple_repos_despite_later_detached_checkout(self):
        other = self.add_other_repository()
        source = self.shared_agent()
        self.own(source, self.api.workspaces[1])
        self.own(source, other)
        source["tokens"]["worktree_branch"] = "f939ef8"
        self.api.branches["w4"] = None
        self.engine.refresh()
        self.api.panes.remove(source)
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2", "w4"])

    def test_exact_token_does_not_close_unowned_duplicate_branch(self):
        self.add_other_repository()
        source = self.shared_agent()
        self.own(source, self.api.workspaces[1])
        self.engine.refresh()
        source["agent_status"] = "idle"
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2"])

    def test_wrong_path_token_does_not_fall_back_to_branch(self):
        source = self.shared_agent()
        source["tokens"][app.SPACE_TOKEN_PREFIX + "w2"] = str(self.root / "wrong-path")
        self.engine.refresh()
        self.api.panes.remove(source)
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])

    def test_exact_owner_protects_target_after_branch_token_changes(self):
        source = self.shared_agent()
        self.own(source, self.api.workspaces[1])
        other = {**source, "pane_id": "w1:p2", "terminal_id": "other", "tokens": dict(source["tokens"])}
        other["tokens"]["worktree_branch"] = "unrelated"
        self.api.panes.append(other)
        self.engine.refresh()
        self.api.panes.remove(source)
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])
        self.api.panes.remove(other)
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2"])

    def test_ambiguous_legacy_owner_protects_explicit_target(self):
        self.add_other_repository()
        source = self.shared_agent()
        self.own(source, self.api.workspaces[1])
        other = {**source, "pane_id": "w1:p2", "terminal_id": "other", "tokens": {"worktree_branch": "branch"}}
        self.api.panes.append(other)
        self.engine.refresh()
        self.api.panes.remove(source)
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])

    def test_stale_token_cannot_claim_reopened_workspace(self):
        source = self.shared_agent()
        self.own(source, self.api.workspaces[1])
        self.engine.refresh()
        self.api.workspaces[1]["workspace_id"] = "w5"
        self.api.panes[0].update(workspace_id="w5", pane_id="w5:p1")
        self.api.panes.remove(source)
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])

    def test_exact_tokens_do_not_need_git_branch_lookup(self):
        source = self.shared_agent()
        self.own(source, self.api.workspaces[1])
        self.engine.refresh()
        self.api.fail.add("worktree.list")
        self.api.panes.remove(source)
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2"])

    def test_explicit_tokens_override_last_branch_value(self):
        other = self.add_other_repository()
        source = self.shared_agent()
        self.own(source, other)
        self.engine.refresh()
        self.api.panes.remove(source)
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w4"])

    def test_explicit_default_waits_for_other_task_spaces(self):
        EngineTests.add_master(self)
        source = self.shared_agent()
        self.own(source, self.api.workspaces[1])
        self.own(source, self.api.workspaces[2])
        self.api.process_overrides["w2:p1"] = {"shell_pid": 10, "foreground_process_group_id": 20}
        self.engine.refresh()
        self.api.panes.remove(source)
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])
        self.api.process_overrides.clear()
        self.engine.refresh()
        self.assertEqual(self.api.closed, ["w2", "w3"])

    def test_fresh_associated_agent_blocks_closure(self):
        source = self.shared_agent()
        self.engine.refresh()
        self.api.panes.remove(source)
        real_call = self.api.call
        snapshots = 0
        def call(method, **params):
            nonlocal snapshots
            result = real_call(method, **params)
            if method == "session.snapshot":
                snapshots += 1
                if snapshots > 1:
                    result["snapshot"]["panes"].append(source)
            return result
        with patch.object(self.api, "call", side_effect=call):
            self.engine.refresh()
        self.assertEqual(self.api.closed, [])

    def test_changed_target_is_preserved(self):
        source = self.shared_agent()
        self.engine.refresh()
        self.api.panes.remove(source)
        self.api.fresh_workspace = {"workspace_id": "w2", "worktree": {
            **self.api.workspaces[1]["worktree"], "checkout_path": str(self.root / "replacement")}}
        self.engine.refresh()
        self.assertEqual(self.api.closed, [])


class DefaultIdentityTests(unittest.TestCase):
    workspace = {"label": "feature", "worktree": {"is_linked_worktree": True,
                 "checkout_path": "/repo/renamed", "repo_key": "/repo/.bare"}}

    def test_git_identity_not_directory_or_label_determines_default(self):
        for branch in ["master", "main", "develop"]:
            with self.subTest(branch=branch), patch.object(app.subprocess, "check_output", side_effect=[
                f"/repo/renamed\n/repo/.bare\nrefs/heads/{branch}\n", f"refs/remotes/origin/{branch}\n"]):
                self.assertTrue(app.default_checkout(self.workspace))
        for output in ["/repo/renamed\n/repo/.bare\nrefs/heads/feature\n",
                       "/repo/renamed\n/other/.bare\nrefs/heads/main\n",
                       "/repo/other\n/repo/.bare\nrefs/heads/main\n",
                       "/repo/renamed\n/repo/.bare\nHEAD\n", "malformed"]:
            with self.subTest(output=output), patch.object(app.subprocess, "check_output", side_effect=[
                output, "refs/remotes/origin/main\n"]):
                self.assertFalse(app.default_checkout(self.workspace))

    def test_bare_head_fallback(self):
        with patch.object(app.subprocess, "check_output", side_effect=[
            "/repo/renamed\n/repo/.bare\nrefs/heads/main\n",
            subprocess.CalledProcessError(1, "git"), "true\n", "refs/heads/main\n"]):
            self.assertTrue(app.default_checkout(self.workspace))

    def test_nonbare_head_is_not_default_evidence(self):
        with patch.object(app.subprocess, "check_output", side_effect=[
            "/repo/renamed\n/repo/.bare\nrefs/heads/main\n",
            subprocess.CalledProcessError(1, "git"), "false\n"]):
            self.assertFalse(app.default_checkout(self.workspace))

    def test_git_failure_preserves_default(self):
        for error in [FileNotFoundError(), subprocess.CalledProcessError(1, "git"),
                      subprocess.TimeoutExpired("git", app.TIMEOUT)]:
            with self.subTest(error=error), patch.object(app.subprocess, "check_output", side_effect=error):
                self.assertFalse(app.default_checkout(self.workspace))


class TransportTests(unittest.TestCase):
    def test_fragmented_event_and_multiple_lines(self):
        receiver, sender = socket.socketpair()
        self.addCleanup(receiver.close)
        self.addCleanup(sender.close)
        stream = app.Stream(receiver)
        sender.sendall(b'{"event":')
        self.assertEqual(stream.read(0.1), [])
        sender.sendall(b'"pane.closed"}\n{"result":{}}\n')
        self.assertEqual(stream.read(0.1), [{"event": "pane.closed"}, {"result": {}}])

    def test_disconnect_does_not_spin(self):
        receiver, sender = socket.socketpair()
        self.addCleanup(receiver.close)
        sender.close()
        with self.assertRaises(app.ApiError):
            app.Stream(receiver).read(0.1)


if __name__ == "__main__":
    unittest.main()
