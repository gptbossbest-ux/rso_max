#!/usr/bin/env python3
"""Resolve a branch head only when its exact GitHub Actions check succeeded."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API_ROOT = "https://api.github.com"
TIMEOUT_SECONDS = 15


class GateError(RuntimeError):
    pass


def _request_json(path: str, token: str | None = None) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "rso-max-server-deployer/1",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{API_ROOT}{path}", headers=headers)
    try:
        # API_ROOT is a module constant using HTTPS; caller input is only the path.
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # nosec B310
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in {403, 429}:
            raise GateError(
                "GitHub API rate limited the poll; wait for the next timer run or configure a read-only token"
            ) from exc
        raise GateError(f"GitHub API returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise GateError("GitHub API request failed") from exc


def resolve_ready_sha(repo: str, branch: str, workflow_path: str, token: str | None = None) -> str | None:
    owner, separator, name = repo.partition("/")
    if not separator or not owner or not name or "/" in name:
        raise GateError("repository must use owner/name format")
    if not branch or branch.startswith("-") or any(char.isspace() for char in branch):
        raise GateError("invalid branch name")

    encoded_repo = f"{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"
    encoded_branch = urllib.parse.quote(branch, safe="")
    ref = _request_json(f"/repos/{encoded_repo}/git/ref/heads/{encoded_branch}", token)
    if ref.get("ref") != f"refs/heads/{branch}":
        raise GateError("GitHub returned a branch with different casing")
    sha = ref.get("object", {}).get("sha", "")
    if len(sha) != 40 or any(char not in "0123456789abcdef" for char in sha):
        raise GateError("GitHub returned an invalid commit SHA")

    encoded_workflow = urllib.parse.quote(workflow_path, safe="")
    workflow = _request_json(f"/repos/{encoded_repo}/actions/workflows/{encoded_workflow}", token)
    if workflow.get("path") != workflow_path or workflow.get("name") != "CI":
        raise GateError("GitHub returned an unexpected workflow identity")
    runs = _request_json(
        f"/repos/{encoded_repo}/actions/workflows/{workflow['id']}/runs"
        f"?branch={encoded_branch}&head_sha={sha}&event=push&status=completed&per_page=100",
        token,
    ).get("workflow_runs", [])
    matching = [
        run
        for run in runs
        if run.get("path") == workflow_path
        and run.get("name") == "CI"
        and run.get("head_branch") == branch
        and run.get("head_sha") == sha
        and run.get("event") == "push"
        and run.get("status") == "completed"
    ]
    if not matching:
        return None
    latest = max(matching, key=lambda run: run.get("id", 0))
    if latest.get("conclusion") == "success":
        return sha
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--workflow", default=".github/workflows/ci.yml")
    args = parser.parse_args()
    try:
        sha = resolve_ready_sha(
            args.repo,
            args.branch,
            args.workflow,
            os.environ.get("GITHUB_TOKEN", ""),
        )
    except GateError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if sha is None:
        return 3
    print(sha)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
