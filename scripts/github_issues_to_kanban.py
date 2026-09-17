#!/usr/bin/env python3
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone

LOG = pathlib.Path.home() / ".hermes/logs/github-kanban-sync.log"
E_LEARNING_ROOT = pathlib.Path.home() / "workspace"
LEGACY_ROOTS = [
    pathlib.Path.home() / "e-learning" / "workspace",
    pathlib.Path.home() / "e-learning",
]
PROFILE = "builder"
GITHUB_BOT_LOGIN = "sariagentbot"
ACTIVE_REPO_ROOT = None


def log(message):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now(timezone.utc).isoformat()} {message}\n")


def run(*args):
    hermes = shutil.which("hermes") or "/home/rog/.local/bin/hermes"
    result = subprocess.run(
        [hermes, *args],
        cwd=str(ACTIVE_REPO_ROOT or pathlib.Path.home()),
        text=True,
        capture_output=True,
        timeout=90,
    )
    if result.returncode:
        raise RuntimeError(f"{args[0:3]}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def github_assignees(repo, number):
    url = f"https://api.github.com/repos/{repo}/issues/{number}"
    try:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "Hermes-Kanban"}
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.load(response)
        return {
            str(item.get("login", "")).lower()
            for item in (data.get("assignees") or [])
            if isinstance(item, dict)
        }
    except Exception as exc:
        log(f"assignee lookup failed issue={repo}#{number}: {type(exc).__name__}")
        return set()


def clone_repository(repo, path):
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    env = os.environ.copy()
    askpass_path = None
    if token:
        askpass = tempfile.NamedTemporaryFile("w", prefix="hermes-git-askpass-", delete=False)
        askpass.write("#!/bin/sh\ncase \"$1\" in\n  *Username*) printf '%s' 'x-access-token' ;;\n  *) printf '%s' \"$GITHUB_TOKEN\" ;;\nesac\n")
        askpass.close()
        pathlib.Path(askpass.name).chmod(0o700)
        askpass_path = askpass.name
        env["GIT_ASKPASS"] = askpass_path
        env["GITHUB_TOKEN"] = token
    env["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(
        ["git", "clone", f"https://github.com/{repo}.git", str(path)],
        cwd=str(E_LEARNING_ROOT), text=True, capture_output=True, timeout=300, env=env,
    )
    if askpass_path:
        pathlib.Path(askpass_path).unlink(missing_ok=True)
    if result.returncode:
        raise RuntimeError(f"clone failed for {repo}: {result.stderr.strip()}")
    log(f"cloned repository={repo} path={path}")


def repo_root_for(repo):
    parts = repo.split("/", 1)
    if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts):
        raise RuntimeError(f"invalid GitHub repository name: {repo}")
    path = E_LEARNING_ROOT / parts[1]
    if (path / ".git").exists():
        return path
    for legacy_root in LEGACY_ROOTS:
        legacy_path = legacy_root / parts[1]
        if (legacy_path / ".git").exists():
            log(f"using legacy repository path={legacy_path}; new tasks should use {path}")
            return legacy_path
    if path.exists():
        raise RuntimeError(f"repository path exists but is not a git repository: {path}")
    E_LEARNING_ROOT.mkdir(parents=True, exist_ok=True)
    clone_repository(repo, path)
    return path


def ensure_worktree(branch, path, repo_root):
    path = pathlib.Path(path)
    if path.exists():
        return str(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    local_branch = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=str(repo_root),
    ).returncode == 0
    command = ["git", "worktree", "add"]
    if not local_branch:
        command += ["-b", branch]
    command += [str(path), branch if local_branch else "HEAD"]
    result = subprocess.run(command, cwd=str(repo_root), text=True, capture_output=True, timeout=60)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return str(path)


def workflow(number, repo):
    return (
        "\n\nWORKFLOW WAJIB SETELAH IMPLEMENTASI:\n"
        "1. Kerjakan perubahan pada branch yang sudah disiapkan.\n"
        "2. Jalankan test/check yang relevan dan catat hasilnya.\n"
        "3. Commit perubahan dengan pesan yang jelas.\n"
        "4. Push branch ke origin.\n"
        f"5. Tambahkan history pengerjaan ke GitHub Issue #{number} dengan \\\"gh issue comment {number} --repo {repo}\\\"; komentar wajib memuat ringkasan perubahan, file yang berubah, test yang dijalankan, hasil test, commit SHA, dan nama branch.\n"
        "6. Hanya setelah commit, push, dan komentar berhasil, selesaikan task Kanban.\n"
        "7. Format komentar history GitHub wajib Markdown yang rapi dan ringkas:\n"
        "   ## Ringkasan\n"
        "   Jelaskan perubahan utama.\n"
        "   ## File yang Diubah\n"
        "   Gunakan daftar bullet dengan path dalam `backtick`.\n"
        "   ## Test\n"
        "   Tulis perintah test dan hasilnya.\n"
        "   ## Commit\n"
        "   Tulis SHA commit dan branch dalam `backtick`.\n"
        "   Jangan menulis komentar sebagai paragraf polos.\n"
    )


def main():
    payload = json.load(sys.stdin)
    wrapped_payload = payload.get("payload")
    if isinstance(wrapped_payload, str):
        payload = json.loads(wrapped_payload)
    elif isinstance(wrapped_payload, dict):
        payload = wrapped_payload
    issue = payload.get("issue") or {}
    repo = (payload.get("repository") or {}).get("full_name", "unknown/unknown")
    number = issue.get("number")
    action = payload.get("action", "")
    comment = payload.get("comment") or {}
    is_comment_event = action == "created" and bool(comment)
    log(f"received action={action or 'none'} issue={repo}#{number or 'none'} keys={','.join(sorted(payload.keys()))}")
    valid_issue_action = action in {"assigned", "opened", "reopened", "edited", "closed"}
    if not number or (not valid_issue_action and not is_comment_event):
        print("[SILENT]")
        return

    global ACTIVE_REPO_ROOT
    ACTIVE_REPO_ROOT = repo_root_for(repo)

    comment_author = ((comment.get("user") or {}).get("login") or "").lower()
    if is_comment_event and comment_author == GITHUB_BOT_LOGIN:
        log(f"ignored bot comment issue={repo}#{number} author={comment_author}")
        print("[SILENT]")
        return

    issue_url = issue.get("html_url", "")
    assignee = (payload.get("assignee") or {}).get("login", "")
    assignees = {
        str(item.get("login", "")).lower()
        for item in (issue.get("assignees") or [])
        if isinstance(item, dict)
    }
    if not assignees:
        assignees = github_assignees(repo, number)
    assigned_to_bot = assignee.lower() == GITHUB_BOT_LOGIN or GITHUB_BOT_LOGIN in assignees
    if action == "assigned" and assignee:
        assigned_to_bot = assignee.lower() == GITHUB_BOT_LOGIN
    if not assigned_to_bot:
        log(f"ignored issue={repo}#{number} action={action} assignee={assignee or 'none'}")
        print("[SILENT]")
        return

    marker = f"github_issue_key={repo}#{number}"
    title = issue.get("title") or f"GitHub issue #{number}"
    body = issue.get("body") or ""
    tasks = json.loads(run("kanban", "list", "--json"))
    existing = next((task for task in tasks if marker in (task.get("body") or "")), None)

    if is_comment_event:
        comment_id = comment.get("id", "unknown")
        comment_body = comment.get("body") or ""
        update = (
            f"GitHub Issue comment #{comment_id} meminta pengerjaan ulang.\n"
            f"Repository: {repo}\nIssue: #{number}\nURL: {issue_url}\n"
            f"Author: {comment.get('user', {}).get('login', 'unknown')}\n\n{comment_body}"
        )
        if existing and existing.get("status") in {"ready", "running", "scheduled", "blocked"}:
            run("kanban", "comment", existing["id"], update, "--author", "github-webhook")
            if existing.get("status") in {"blocked", "scheduled"}:
                run("kanban", "unblock", existing["id"], "Komentar GitHub meminta pengerjaan ulang")
            elif existing.get("status") == "running":
                run("kanban", "reclaim", existing["id"], "--reason", "Komentar GitHub meminta pengerjaan ulang")
            log(f"updated active {existing['id']} issue={repo}#{number} comment={comment_id}")
        else:
            branch = (existing or {}).get("branch_name") or f"features/issue-{number}"
            worktree = pathlib.Path(ACTIVE_REPO_ROOT) / ".worktrees" / f"issue-{number}"
            if existing and existing.get("workspace_path"):
                candidate = pathlib.Path(existing["workspace_path"])
                if candidate.parent.exists():
                    worktree = candidate
            worktree = ensure_worktree(branch, worktree, ACTIVE_REPO_ROOT)
            task_body = (
                f"{marker}\nGitHub Issue: {issue_url}\n\n{body}\n\n"
                f"COMMENT REQUEST:\n{comment_body}{workflow(number, repo)}"
            )
            output = run(
                "kanban", "create", f"Rework: {title}",
                "--body", task_body,
                "--assignee", PROFILE,
                "--workspace", f"worktree:{worktree}",
                "--branch", branch,
                "--idempotency-key", f"github-{repo}-{number}-comment-{comment_id}",
                "--created-by", "github-webhook",
                "--json",
            )
            created = json.loads(output)
            log(f"created rework {created.get('id')} issue={repo}#{number} comment={comment_id} branch={branch}")
        print("[SILENT]")
        return

    update = (
        f"GitHub Issue update ({action})\n"
        f"Repository: {repo}\nIssue: #{number}\nURL: {issue_url}\n\n{body}"
    )
    if existing:
        run("kanban", "comment", existing["id"], update, "--author", "github-webhook")
        log(f"updated {existing['id']} issue={repo}#{number} action={action}")
    elif action in {"assigned", "opened", "reopened", "edited"}:
        branch = f"features/issue-{number}"
        worktree = ensure_worktree(branch, pathlib.Path(ACTIVE_REPO_ROOT) / ".worktrees" / f"issue-{number}", ACTIVE_REPO_ROOT)
        task_body = f"{marker}\nGitHub Issue: {issue_url}\n\n{body}{workflow(number, repo)}"
        output = run(
            "kanban", "create", title,
            "--body", task_body,
            "--assignee", PROFILE,
            "--workspace", f"worktree:{worktree}",
            "--branch", branch,
            "--idempotency-key", f"github-{repo}-{number}",
            "--created-by", "github-webhook",
            "--json",
        )
        created = json.loads(output)
        log(f"created {created.get('id')} issue={repo}#{number} branch={branch}")
    elif action == "closed":
        log(f"closed issue without matching task issue={repo}#{number}")

    print("[SILENT]")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"ERROR {type(exc).__name__}: {exc}")
        print("[SILENT]")
