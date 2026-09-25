import os
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from scripts.github_ci_gate import GateError, resolve_ready_sha


ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40


WORKFLOW = ".github/workflows/ci.yml"


def _github_responses(
    *, ref="refs/heads/Main_test", status="completed", conclusion="success", **run_changes
):
    run = {
        "id": 10,
        "name": "CI",
        "path": WORKFLOW,
        "head_branch": "Main_test",
        "head_sha": SHA,
        "event": "push",
        "status": status,
        "conclusion": conclusion,
    }
    run.update(run_changes)
    return [
        {"ref": ref, "object": {"sha": SHA}},
        {"id": 77, "name": "CI", "path": WORKFLOW},
        {"workflow_runs": [run]},
    ]


def test_ci_gate_accepts_only_success_for_exact_branch_and_sha():
    with patch("scripts.github_ci_gate._request_json", side_effect=_github_responses()):
        assert resolve_ready_sha("owner/repo", "Main_test", WORKFLOW) == SHA


@pytest.mark.parametrize(
    ("status", "conclusion"),
    [("in_progress", None), ("completed", "failure"), ("completed", "cancelled")],
)
def test_ci_gate_rejects_non_successful_check(status, conclusion):
    with patch(
        "scripts.github_ci_gate._request_json",
        side_effect=_github_responses(status=status, conclusion=conclusion),
    ):
        assert resolve_ready_sha("owner/repo", "Main_test", WORKFLOW) is None


def test_ci_gate_rejects_case_changed_branch_response():
    with patch(
        "scripts.github_ci_gate._request_json",
        side_effect=_github_responses(ref="refs/heads/main_test"),
    ):
        with pytest.raises(GateError, match="different casing"):
            resolve_ready_sha("owner/repo", "Main_test", WORKFLOW)


@pytest.mark.parametrize(
    "changes",
    [
        {"head_sha": "b" * 40},
        {"event": "pull_request"},
        {"path": ".github/workflows/deceptive.yml"},
        {"name": "Tests and quality checks"},
        {"head_branch": "main_test"},
    ],
)
def test_ci_gate_rejects_deceptive_workflow_runs(changes):
    responses = _github_responses(**changes)
    with patch("scripts.github_ci_gate._request_json", side_effect=responses):
        assert resolve_ready_sha("owner/repo", "Main_test", WORKFLOW) is None


def test_ci_gate_uses_latest_rerun_result():
    responses = _github_responses()
    responses[2]["workflow_runs"].append(
        {
            "id": 20,
            "name": "CI",
            "path": WORKFLOW,
            "head_branch": "Main_test",
            "head_sha": SHA,
            "event": "push",
            "status": "completed",
            "conclusion": "failure",
        }
    )
    with patch("scripts.github_ci_gate._request_json", side_effect=responses):
        assert resolve_ready_sha("owner/repo", "Main_test", WORKFLOW) is None


def test_auto_deploy_modes_are_fixed_and_share_one_lock():
    script = (ROOT / "scripts" / "auto-deploy.sh").read_text(encoding="utf-8")
    assert "branch=Main_test\n    stack=test\n    project=rso-max-test" in script
    assert "branch=main\n    stack=prod\n    project=rso-max-prod" in script
    assert 'lock_file="$state_dir/deploy.lock"' in script
    assert 'workflow_path=.github/workflows/ci.yml' in script
    assert "git -C \"$repo_cache\" archive" in script
    assert "branch-advanced-during-poll" in script
    assert "reason=already-applied" in script
    assert script.count('--profile bot stop bot web api') == 1
    assert '--profile bot stop bot web' in script
    assert '--profile bot stop api' in script
    assert "--no-build api web bot" in script
    assert "wait_healthy \"$previous_image_id\"" in script
    assert 'bot_id" == "$expected_bot_id' in script
    assert 'bot_username" == "$expected_bot_username' in script
    assert script.index('bot_id" == "$expected_bot_id') < script.index("rollback_needed=true")
    assert script.index("rollback_needed=true") < script.rindex("--profile bot stop bot web")
    assert script.rindex("stop bot web") < script.index("./scripts/backup.sh") < script.index("stop api")
    assert "failed-$(date -u" in script
    assert "database.sqlite-wal" in script and "database.sqlite-shm" in script
    assert "printenv" not in script
    assert "set -x" not in script


def test_release_smoke_is_read_only_and_modes_are_isolated():
    script = (ROOT / "scripts" / "release-smoke.sh").read_text(encoding="utf-8")
    assert "mode=ro" in script
    assert "sqlite_pragma=quick_check" in script
    assert "sqlite_pragma=integrity_check" in script
    assert "web_port=5001" in script
    assert "web_port=5000" in script
    for forbidden in ("docker compose up", "docker compose down", "docker restart", "curl -X"):
        assert forbidden not in script


def test_auto_deploy_units_are_hardened_and_use_stable_modes():
    fast = (ROOT / "deploy/systemd/rso-max-auto-deploy-fast.service").read_text()
    full = (ROOT / "deploy/systemd/rso-max-auto-deploy-full.service").read_text()
    for unit in (fast, full):
        assert "User=botadmin" in unit
        assert "NoNewPrivileges=true" in unit
        assert "PrivateTmp=true" in unit
        assert "ProtectSystem=strict" in unit
        assert "ProtectHome=true" in unit
        assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" in unit
        assert "EnvironmentFile=/etc/rso-max-deploy/github.conf" in unit
        assert "--kill-after=1000s 1800s" in unit
        assert "TimeoutStartSec=2850" in unit
        assert "TimeoutStopSec=1050" in unit
    assert "/usr/local/libexec/rso-max-auto-deploy fast" in fast
    assert "/usr/local/libexec/rso-max-auto-deploy full" in full


def test_installer_pins_distinct_public_bot_identities():
    installer = (ROOT / "scripts/install-auto-deploy-systemd.sh").read_text(encoding="utf-8")
    assert "EXPECTED_TEST_BOT_ID" in installer
    assert "EXPECTED_TEST_BOT_USERNAME" in installer
    assert "EXPECTED_PROD_BOT_ID" in installer
    assert "EXPECTED_PROD_BOT_USERNAME" in installer
    assert '"$test_bot_id" == "$prod_bot_id"' in installer
    assert '"${test_bot_username,,}" == "${prod_bot_username,,}"' in installer
    assert "printenv" not in installer
    assert 'test -e "$release/.git"' in installer
    assert "status --porcelain --untracked-files=normal" in installer
    assert "org.opencontainers.image.revision" in installer
    assert "/usr/local/libexec/rso-max-release-smoke fast" in installer
    assert "/usr/local/libexec/rso-max-release-smoke full" in installer
    assert "BOOTSTRAP_ADMIN_PASSWORD must be empty" in installer


def test_ci_has_fast_full_jobs_and_stable_gate_name():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "      - Main_test" in workflow
    assert "name: Fast tests" in workflow
    assert "name: Full tests and security checks" in workflow
    assert "name: Tests and quality checks" in workflow
    fast_section, full_section = workflow.split("  full:", 1)
    assert "pip-audit" not in fast_section
    assert "bandit" not in fast_section
    assert "pip-audit" in full_section
    assert "bandit" in full_section


@pytest.mark.parametrize(
    ("mode", "stack", "project", "app_env", "runtime_env", "forbidden_project"),
    [
        ("fast", "test", "rso-max-test", ".env.test", ".env.test.runtime", "rso-max-prod"),
        ("full", "prod", "rso-max-prod", ".env", ".env.prod.runtime", "rso-max-test"),
    ],
)
@pytest.mark.skipif(os.name == "nt", reason="behavioral shell test runs in Linux CI")
def test_release_smoke_behavior_is_stack_isolated(
    tmp_path, mode, stack, project, app_env, runtime_env, forbidden_project
):
    work = tmp_path / "release"
    data = work / "runtime" / stack / "data"
    fakebin = tmp_path / "fakebin"
    data.mkdir(parents=True)
    fakebin.mkdir()
    (work / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (work / app_env).write_text("TOKEN=\n", encoding="utf-8")
    (work / runtime_env).write_text("", encoding="utf-8")
    (work / ".release-sha").write_text(f"{SHA}\n", encoding="ascii")
    (data / "database.sqlite").write_bytes(b"sqlite-placeholder")
    (data / "bot-heartbeat").write_text("ok\n", encoding="ascii")

    commands = {
        "docker": """#!/usr/bin/env bash
echo "$*" >> "$FAKE_COMMAND_LOG"
if [[ "$1" == compose ]]; then
  echo "cid-${@: -1}"
elif [[ "$1" == inspect ]]; then
  cid="${@: -1}"
  service="${cid#cid-}"
  if [[ "$*" == *RestartCount* && "$*" != *State.Status* ]]; then
    echo 0
  else
    echo "running|healthy|0|$EXPECTED_PROJECT|$service|sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  fi
elif [[ "$1" == exec ]]; then
  echo ok
fi
""",
        "curl": "#!/usr/bin/env bash\necho '{\"status\":\"ok\"}'\n",
        "df": "#!/usr/bin/env bash\nprintf 'Filesystem 1K-blocks Used Available Use%% Mounted on\\nfake 100 50 50 50%% /\\n'\n",
    }
    for name, body in commands.items():
        path = fakebin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    command_log = tmp_path / "commands.log"
    env = os.environ.copy()
    env.update(
        PATH=f"{fakebin}{os.pathsep}{env['PATH']}",
        EXPECTED_PROJECT=project,
        FAKE_COMMAND_LOG=str(command_log),
        SMOKE_RESTART_INTERVAL_SECONDS="0",
    )
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/release-smoke.sh"), mode],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"SUMMARY mode={mode} stack={stack} result=PASS" in result.stdout
    assert forbidden_project not in command_log.read_text(encoding="utf-8")
