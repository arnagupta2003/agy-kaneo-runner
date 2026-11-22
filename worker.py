#!/usr/bin/env python3
"""Stateless Kaneo <-> Antigravity CLI (agy) worker for Dagu.

Subcommands:
  check  - Verify Kaneo API connectivity, print board column slugs, and check `agy`.
  reap   - Move orphaned cards stuck in investigating/in-progress to blocked.
  run    - Process the next card in `approved` (priority) or `investigate`.
  fail   - Called by Dagu `handlerOn.failure` to mark the active card as blocked.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

STATE_FILE = pathlib.Path("/tmp/current_task.json")
PROJECTS_CONFIG = pathlib.Path("/app/config/projects.json")
RUNS_DIR = pathlib.Path("/workspaces/_runs")
SESSION_RE = re.compile(r"agy_session:([0-9a-fA-F-]+)")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def require_env(name: str) -> str:
    val = env(name)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


class KaneoClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _req(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Kaneo {method} {path} failed ({e.code}): {err_body}") from e

    def get_board(self, project_id: str) -> dict[str, Any]:
        res = self._req("GET", f"/api/task/tasks/{project_id}")
        return res.get("data", res) if isinstance(res, dict) else res

    def update_status(self, task_id: str, status_slug: str) -> Any:
        return self._req("PUT", f"/api/task/status/{task_id}", {"status": status_slug})

    def update_description(self, task: dict[str, Any], new_description: str) -> Any:
        payload: dict[str, Any] = {
            "title": task.get("title", ""),
            "description": new_description,
            "status": task.get("status", ""),
            "priority": task.get("priority") or "no-priority",
            "position": task.get("position", 0),
            "projectId": task.get("projectId", ""),
        }
        for opt_key in ("userId", "startDate", "dueDate"):
            if task.get(opt_key) is not None:
                payload[opt_key] = task[opt_key]
        return self._req("PUT", f"/api/task/{task['id']}", payload)

    def add_comment(self, task_id: str, comment: str) -> Any:
        return self._req("POST", "/api/activity/comment", {"taskId": task_id, "comment": comment})

    def get_activity(self, task_id: str) -> list[dict[str, Any]]:
        res = self._req("GET", f"/api/activity/{task_id}")
        if isinstance(res, dict):
            res = res.get("data", res)
        return res if isinstance(res, list) else res.get("activities", [])


def load_project_config(project_id: str) -> dict[str, Any]:
    if PROJECTS_CONFIG.exists():
        cfg = json.loads(PROJECTS_CONFIG.read_text(encoding="utf-8"))
        return cfg.get("projects", {}).get(project_id, cfg.get("default", {}))
    return {"workspace": "/workspaces/sandbox", "use_worktree": False}


def extract_session_id(description: str) -> str | None:
    m = SESSION_RE.search(description or "")
    return m.group(1) if m else None


def upsert_session_id(description: str, session_id: str) -> str:
    base = (description or "").rstrip()
    marker = f"agy_session:{session_id}"
    if SESSION_RE.search(base):
        return SESSION_RE.sub(marker, base)
    return f"{base}\n\n---\n{marker}\n" if base else f"{marker}\n"


def ensure_workspace(project_cfg: dict[str, Any], task_id: str, phase: str) -> pathlib.Path:
    ws = pathlib.Path(project_cfg.get("workspace", "/workspaces/sandbox"))
    ws.mkdir(parents=True, exist_ok=True)
    if phase == "execute" and project_cfg.get("use_worktree") and (ws / ".git").exists():
        wt_root = pathlib.Path("/workspaces/_worktrees")
        wt_root.mkdir(parents=True, exist_ok=True)
        wt_path = wt_root / task_id
        branch = f"agent/{task_id}"
        if not wt_path.exists():
            subprocess.run(
                ["git", "-C", str(ws), "worktree", "add", "-B", branch, str(wt_path)],
                check=True,
            )
        return wt_path
    return ws


def ensure_agy_settings() -> None:
    """If GEMINI_API_KEY is set, ensure agy uses Gemini API provider cleanly."""
    if not env("GEMINI_API_KEY"):
        return
    cfg_dir = pathlib.Path.home() / ".gemini" / "antigravity-cli"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    settings_path = cfg_dir / "settings.json"
    data: dict[str, Any] = {}
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    if data.get("modelProvider") != "gemini":
        data["modelProvider"] = "gemini"
        settings_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def run_agy(prompt: str, cwd: pathlib.Path, phase: str, session_id: str | None) -> dict[str, Any]:
    ensure_agy_settings()
    cmd = ["agy", "-p", prompt, "--output-format", "json"]

    if session_id:
        cmd.extend(["--conversation", session_id])

    model = env("AGY_MODEL")
    if model:
        cmd.extend(["--model", model])

    effort = env("AGY_EFFORT")
    if effort:
        cmd.extend(["--effort", effort])

    timeout = env("AGY_INVESTIGATE_TIMEOUT", "10m") if phase == "investigate" else env("AGY_EXECUTE_TIMEOUT", "20m")
    cmd.extend(["--print-timeout", timeout])

    if phase == "investigate":
        # Plan mode keeps Phase 1 read-only while allowing inspection/web commands inside Docker.
        cmd.extend(["--mode", "plan", "--dangerously-skip-permissions"])
    else:
        cmd.extend(["--mode", "accept-edits", "--dangerously-skip-permissions"])

    print(f"[worker] Running ({phase}) in {cwd}: {' '.join(cmd[:6])} ...", flush=True)
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)

    if proc.stderr:
        print(f"[agy stderr]\n{proc.stderr}", file=sys.stderr, flush=True)

    stdout = proc.stdout.strip()
    if not stdout:
        raise RuntimeError(f"agy exited with code {proc.returncode} and empty stdout")

    try:
        envelope = json.loads(stdout.splitlines()[-1])
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse agy JSON output (exit {proc.returncode}): {stdout[:500]}") from e

    if proc.returncode != 0 or envelope.get("status") != "SUCCESS":
        raise RuntimeError(f"agy failed ({envelope.get('status')}): {envelope.get('error') or stdout[:500]}")

    return envelope


def format_comments(activities: list[dict[str, Any]]) -> str:
    comments = [
        a for a in activities if a.get("type") == "comment" or a.get("content") or a.get("comment")
    ]
    if not comments:
        return "(none)"
    lines = []
    for item in comments[:10]:
        text = item.get("content") or item.get("comment") or ""
        if text:
            lines.append(f"- {text.strip()}")
    return "\n".join(lines) if lines else "(none)"


def build_prompt(task: dict[str, Any], comments_text: str, phase: str, is_revision: bool = False) -> str:
    title = task.get("title", "Untitled")
    desc = task.get("description") or "(no description)"
    formatting_rule = (
        "Formatting: Write self-contained Markdown suitable for a Kaneo comment. "
        "Do NOT include local container file:// links (such as file:///home/runner/.gemini/...)."
    )
    if phase == "investigate" and is_revision:
        return (
            f"The human sent Kaneo task '{title}' (ID: {task['id']}) back for a revised investigation.\n\n"
            f"Latest comments (focus on the human's newest feedback/corrections):\n{comments_text}\n\n"
            "Instructions (READ-ONLY REVISED INVESTIGATION):\n"
            "1. Do NOT modify files or run destructive commands.\n"
            "2. Directly address the human's latest feedback/corrections and update your findings and plan.\n"
            "3. Return a concise Markdown update with:\n"
            "   - **Updated Findings / Context**\n"
            "   - **Revised Plan** (numbered steps)\n"
            "   - **Files / Systems Affected**\n"
            "   - **Risks or Questions** (if anything still needs clarification)\n"
            f"4. {formatting_rule}\n"
        )
    if phase == "investigate":
        return (
            f"You are investigating Kaneo task '{title}' (ID: {task['id']}).\n\n"
            f"Description:\n{desc}\n\n"
            f"Recent comments:\n{comments_text}\n\n"
            "Instructions (READ-ONLY INVESTIGATION):\n"
            "1. Do NOT modify files or run destructive commands.\n"
            "2. Inspect the workspace and gather all context needed.\n"
            "3. Return a concise Markdown investigation with:\n"
            "   - **Findings / Context**\n"
            "   - **Proposed Plan** (numbered steps)\n"
            "   - **Files / Systems Affected**\n"
            "   - **Risks or Questions** (if anything needs human clarification)\n"
            f"4. {formatting_rule}\n"
        )
    return (
        f"The human approved Kaneo task '{title}' (ID: {task['id']}).\n\n"
        f"Latest comments (check for any feedback/adjustments):\n{comments_text}\n\n"
        "Instructions (EXECUTION):\n"
        "1. Execute the approved plan cleanly in this workspace.\n"
        "2. Verify your work (run tests/checks where applicable).\n"
        "3. Return a concise Markdown completion report with:\n"
        "   - **What Changed**\n"
        "   - **Verification / Results**\n"
        "   - **Follow-ups / Notes**\n"
        f"4. {formatting_rule}\n"
    )


def get_tasks_by_slug(board: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    by_slug: dict[str, list[dict[str, Any]]] = {}
    for col in board.get("columns", []):
        slug = col.get("slug") or col.get("id") or ""
        by_slug[slug] = col.get("tasks", [])
    return by_slug


def cmd_check() -> None:
    client = KaneoClient(require_env("KANEO_URL"), require_env("KANEO_API_KEY"))
    project_id = require_env("KANEO_PROJECT_ID")
    board = client.get_board(project_id)
    print("Connected to Kaneo! Columns found:")
    for col in board.get("columns", []):
        print(f"  - {col.get('name')} (slug: {col.get('slug')}, tasks: {len(col.get('tasks', []))})")
    res = subprocess.run(["agy", "--version"], capture_output=True, text=True)
    print(f"agy version: {res.stdout.strip() or res.stderr.strip()}")


def cmd_reap() -> None:
    client = KaneoClient(require_env("KANEO_URL"), require_env("KANEO_API_KEY"))
    project_id = require_env("KANEO_PROJECT_ID")
    by_slug = get_tasks_by_slug(client.get_board(project_id))

    col_investigating = env("COL_INVESTIGATING", "investigating")
    col_in_progress = env("COL_IN_PROGRESS", "in-progress")
    col_blocked = env("COL_BLOCKED", "blocked")

    orphans = by_slug.get(col_investigating, []) + by_slug.get(col_in_progress, [])
    for task in orphans:
        print(f"[reap] Moving orphaned task {task['id']} ({task.get('title')}) -> {col_blocked}", flush=True)
        client.add_comment(
            task["id"],
            "Runner restarted while this task was active. Moved to Blocked automatically—move back to Investigate or Approved to retry.",
        )
        client.update_status(task["id"], col_blocked)


def cmd_run() -> None:
    client = KaneoClient(require_env("KANEO_URL"), require_env("KANEO_API_KEY"))
    project_id = require_env("KANEO_PROJECT_ID")
    by_slug = get_tasks_by_slug(client.get_board(project_id))

    col_investigate = env("COL_INVESTIGATE", "investigate")
    col_investigating = env("COL_INVESTIGATING", "investigating")
    col_ready = env("COL_READY_FOR_APPROVAL", "ready-for-approval")
    col_approved = env("COL_APPROVED", "approved")
    col_in_progress = env("COL_IN_PROGRESS", "in-progress")
    col_in_review = env("COL_IN_REVIEW", "in-review")

    # Prioritize Approved (execution) over Investigate
    task: dict[str, Any] | None = None
    phase = ""
    if by_slug.get(col_approved):
        task = by_slug[col_approved][0]
        phase = "execute"
        active_col, done_col = col_in_progress, col_in_review
    elif by_slug.get(col_investigate):
        task = by_slug[col_investigate][0]
        phase = "investigate"
        active_col, done_col = col_investigating, col_ready
    else:
        print("[worker] No pending Investigate or Approved tasks.", flush=True)
        return

    task_id = task["id"]
    print(f"[worker] Claiming task {task_id} ('{task.get('title')}') for phase={phase}", flush=True)
    STATE_FILE.write_text(json.dumps({"task_id": task_id, "phase": phase, "title": task.get("title")}), encoding="utf-8")

    # 1. Claim card
    client.update_status(task_id, active_col)

    # 2. Prepare context & workspace
    project_cfg = load_project_config(project_id)
    cwd = ensure_workspace(project_cfg, task_id, phase)
    activities = client.get_activity(task_id)
    comments_text = format_comments(activities)
    existing_session = extract_session_id(task.get("description", ""))
    prompt = build_prompt(task, comments_text, phase, is_revision=bool(existing_session and phase == "investigate"))

    # 3. Execute agy
    envelope = run_agy(prompt, cwd, phase, existing_session)

    # 4. Archive run output locally
    run_dir = RUNS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_{task_id}_{phase}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "envelope.json").write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    (run_dir / "response.md").write_text(envelope.get("response", ""), encoding="utf-8")

    # 5. Persist conversation_id on the Kaneo card
    conv_id = envelope.get("conversation_id", "")
    if conv_id and conv_id != existing_session:
        task["status"] = active_col
        new_desc = upsert_session_id(task.get("description", ""), conv_id)
        client.update_description(task, new_desc)

    # 6. Post report & advance column
    duration = envelope.get("duration_seconds", 0)
    tokens = envelope.get("usage", {}).get("total_tokens", 0)
    header = (
        f"### AGY {'Investigation & Plan' if phase == 'investigate' else 'Execution Report'}\n"
        f"_Duration: {duration:.1f}s | Tokens: {tokens} | Session: `{conv_id}`_\n\n"
    )
    client.add_comment(task_id, header + (envelope.get("response") or "(empty response)"))
    client.update_status(task_id, done_col)
    STATE_FILE.unlink(missing_ok=True)
    print(f"[worker] Completed task {task_id} -> {done_col}", flush=True)


def cmd_fail() -> None:
    if not STATE_FILE.exists():
        print("[fail] No active task state file found.", flush=True)
        return
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    task_id = state["task_id"]
    col_blocked = env("COL_BLOCKED", "blocked")
    client = KaneoClient(require_env("KANEO_URL"), require_env("KANEO_API_KEY"))
    client.add_comment(
        task_id,
        f"AGY run failed or timed out during **{state.get('phase')}**. Check Dagu logs for details, then move back to Investigate/Approved to retry.",
    )
    client.update_status(task_id, col_blocked)
    STATE_FILE.unlink(missing_ok=True)
    print(f"[fail] Marked task {task_id} -> {col_blocked}", flush=True)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: worker.py [check|reap|run|fail]", file=sys.stderr)
        sys.exit(2)
    cmd = sys.argv[1]
    {"check": cmd_check, "reap": cmd_reap, "run": cmd_run, "fail": cmd_fail}[cmd]()


if __name__ == "__main__":
    main()
