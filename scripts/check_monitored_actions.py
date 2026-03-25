#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - depends on interpreter version
    tomllib = None


PASSING_CONCLUSIONS = {"success"}
FAILING_CONCLUSIONS = {
    "action_required",
    "cancelled",
    "failure",
    "stale",
    "startup_failure",
    "timed_out",
}
NON_PASSING_CONCLUSIONS = FAILING_CONCLUSIONS | {"neutral", "skipped"}


class GitHubApiError(RuntimeError):
    pass


@dataclass
class ReportConfig:
    issue_title: str
    issue_labels: list[str]
    close_on_success: bool = True


@dataclass
class CheckConfig:
    name: str
    kind: str
    repo: str
    workflow: str
    event: str | None = None
    branch: str | None = None
    job_name: str | None = None


@dataclass
class CheckResult:
    name: str
    kind: str
    repo: str
    workflow: str
    state: str
    alert: bool
    headline: str
    latest_url: str | None = None
    lines: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind,
            "repo": self.repo,
            "workflow": self.workflow,
            "state": self.state,
            "alert": self.alert,
            "headline": self.headline,
            "latest_url": self.latest_url,
            "lines": self.lines,
        }


class GitHubClient:
    def __init__(self, token: str | None = None, api_url: str = "https://api.github.com") -> None:
        self.token = token
        self.api_url = api_url.rstrip("/")

    def _request_json(self, path: str, params: dict[str, object] | None = None) -> dict[str, object]:
        query = ""
        if params:
            filtered = {key: value for key, value in params.items() if value is not None}
            query = "?" + urllib.parse.urlencode(filtered)

        url = f"{self.api_url}{path}{query}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "gha-supervision-monitor",
                "X-GitHub-Api-Version": "2022-11-28",
                **(
                    {"Authorization": f"Bearer {self.token}"}
                    if self.token
                    else {}
                ),
            },
        )

        for attempt in range(4):
            try:
                with urllib.request.urlopen(request) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code in {429, 500, 502, 503, 504} and attempt < 3:
                    retry_after = exc.headers.get("Retry-After")
                    delay = int(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
                    time.sleep(delay)
                    continue
                raise GitHubApiError(f"{exc.code} {exc.reason} for {url}: {body}") from exc
            except urllib.error.URLError as exc:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise GitHubApiError(f"Request failed for {url}: {exc.reason}") from exc

        raise GitHubApiError(f"Request failed for {url}: exhausted retries")

    def _paginate(self, path: str, key: str, params: dict[str, object] | None = None) -> list[dict[str, object]]:
        params = dict(params or {})
        params.setdefault("per_page", 100)
        page = 1
        items: list[dict[str, object]] = []

        while True:
            payload = self._request_json(path, {**params, "page": page})
            page_items = payload.get(key, [])
            if not isinstance(page_items, list):
                raise GitHubApiError(f"Unexpected payload for {path}: missing list key {key!r}")

            items.extend(page_items)
            if len(page_items) < int(params["per_page"]):
                break
            page += 1

        return items

    def list_workflows(self, repo: str) -> list[dict[str, object]]:
        payload = self._request_json(f"/repos/{repo}/actions/workflows", {"per_page": 100})
        workflows = payload.get("workflows", [])
        if not isinstance(workflows, list):
            raise GitHubApiError(f"Unexpected workflow payload for {repo}.")
        return workflows

    def list_runs(
        self,
        repo: str,
        workflow_id: int | str,
        *,
        event: str | None = None,
        branch: str | None = None,
    ) -> list[dict[str, object]]:
        payload = self._request_json(
            f"/repos/{repo}/actions/workflows/{workflow_id}/runs",
            {"event": event, "branch": branch, "per_page": 100},
        )
        runs = payload.get("workflow_runs", [])
        if not isinstance(runs, list):
            raise GitHubApiError(f"Unexpected workflow run payload for {repo}/{workflow_id}.")
        return runs

    def list_jobs(self, repo: str, run_id: int) -> list[dict[str, object]]:
        return self._paginate(f"/repos/{repo}/actions/runs/{run_id}/jobs", "jobs")


def load_config(path: Path) -> tuple[ReportConfig, list[CheckConfig]]:
    if tomllib is None:
        raise RuntimeError("This script requires Python 3.11+ or a compatible tomllib provider.")

    with path.open("rb") as handle:
        payload = tomllib.load(handle)

    report_payload = payload.get("report")
    if not isinstance(report_payload, dict):
        raise ValueError("Missing [report] section in configuration.")

    issue_title = report_payload.get("issue_title")
    issue_labels = report_payload.get("issue_labels")
    if not isinstance(issue_title, str) or not issue_title.strip():
        raise ValueError("[report].issue_title must be a non-empty string.")
    if not isinstance(issue_labels, list) or not issue_labels or not all(
        isinstance(label, str) and label.strip() for label in issue_labels
    ):
        raise ValueError("[report].issue_labels must be a non-empty array of strings.")

    report = ReportConfig(
        issue_title=issue_title,
        issue_labels=[label.strip() for label in issue_labels],
        close_on_success=bool(report_payload.get("close_on_success", True)),
    )

    raw_checks = payload.get("checks")
    if not isinstance(raw_checks, list) or not raw_checks:
        raise ValueError("Configuration must define at least one [[checks]] entry.")

    checks: list[CheckConfig] = []
    for index, raw_check in enumerate(raw_checks, start=1):
        if not isinstance(raw_check, dict):
            raise ValueError(f"[[checks]] entry {index} must be a table.")

        missing = [
            key for key in ("name", "kind", "repo", "workflow") if not isinstance(raw_check.get(key), str)
        ]
        if missing:
            raise ValueError(f"[[checks]] entry {index} is missing required string fields: {', '.join(missing)}.")

        kind = raw_check["kind"].strip()
        if kind not in {"job", "workflow"}:
            raise ValueError(f"[[checks]] entry {index} has unsupported kind {kind!r}.")

        job_name = raw_check.get("job_name")
        if kind == "job" and (not isinstance(job_name, str) or not job_name.strip()):
            raise ValueError(f"[[checks]] entry {index} of kind 'job' requires a non-empty job_name.")

        checks.append(
            CheckConfig(
                name=raw_check["name"].strip(),
                kind=kind,
                repo=raw_check["repo"].strip(),
                workflow=raw_check["workflow"].strip(),
                event=raw_check.get("event"),
                branch=raw_check.get("branch"),
                job_name=job_name.strip() if isinstance(job_name, str) else None,
            )
        )

    return report, checks


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def markdown_link(label: str, url: str | None) -> str:
    if url:
        return f"[{label}]({url})"
    return label


def repo_actions_url(repo: str) -> str:
    return f"https://github.com/{repo}/actions"


def workflow_page_url(repo: str, workflow_path: str) -> str:
    workflow_name = workflow_path.rsplit("/", 1)[-1]
    return f"https://github.com/{repo}/actions/workflows/{workflow_name}"


def fallback_check_url(check: CheckConfig) -> str | None:
    if not check.repo or check.repo == "-":
        return None

    workflow = check.workflow.strip()
    if workflow.endswith((".yml", ".yaml")):
        return workflow_page_url(check.repo, workflow)
    return repo_actions_url(check.repo)


def state_badge(state: str) -> str:
    return {
        "success": "🟢",
        "failure": "🔴",
        "non_passing": "🟠",
        "in_progress": "🟡",
        "config_drift": "⚠️",
        "no_data": "⚪",
        "error": "🚨",
    }.get(state, "❔")


def format_run(run: dict[str, object] | None) -> str:
    if not run:
        return "none"
    run_id = run.get("id", "?")
    name = str(run.get("name", "workflow run"))
    return markdown_link(f"{name} #{run_id}", str(run.get("html_url", "")) or None)


def format_job(job: dict[str, object] | None) -> str:
    if not job:
        return "none"
    name = str(job.get("name", "workflow job"))
    return markdown_link(name, str(job.get("html_url", "")) or None)


def workflow_candidates(workflow: dict[str, object]) -> set[str]:
    path = str(workflow.get("path", ""))
    return {
        str(workflow.get("id", "")),
        str(workflow.get("name", "")),
        path,
        path.rsplit("/", 1)[-1],
    }


def resolve_workflow(
    client: GitHubClient,
    repo: str,
    selector: str,
) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
    workflows = client.list_workflows(repo)
    matches = []
    lowered = selector.casefold()

    for workflow in workflows:
        candidates = workflow_candidates(workflow)
        if any(candidate and candidate.casefold() == lowered for candidate in candidates):
            matches.append(workflow)

    if len(matches) == 1:
        return matches[0], matches
    return None, matches


def first_completed_run(runs: list[dict[str, object]]) -> dict[str, object] | None:
    return next((run for run in runs if run.get("status") == "completed"), None)


def classify_conclusion(conclusion: str | None) -> str:
    if conclusion in PASSING_CONCLUSIONS:
        return "success"
    if conclusion in FAILING_CONCLUSIONS:
        return "failure"
    if conclusion in NON_PASSING_CONCLUSIONS:
        return "non_passing"
    if conclusion is None:
        return "in_progress"
    return "unknown"


def failed_jobs(jobs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        job
        for job in jobs
        if classify_conclusion(job.get("conclusion")) in {"failure", "non_passing"}
    ]


def describe_common_metadata(result: CheckResult, *, workflow_data: dict[str, object], latest_run: dict[str, object] | None) -> None:
    result.lines.append(f"Repo: `{result.repo}`")
    result.lines.append(f"Workflow: `{workflow_data.get('path', result.workflow)}`")
    if latest_run:
        status = latest_run.get("status", "unknown")
        conclusion = latest_run.get("conclusion", "n/a")
        result.lines.append(f"Run: {format_run(latest_run)} (`{status}` / `{conclusion}`)")


def evaluate_workflow_check(client: GitHubClient, check: CheckConfig) -> CheckResult:
    workflow_data, matches = resolve_workflow(client, check.repo, check.workflow)
    if workflow_data is None:
        if matches:
            return CheckResult(
                name=check.name,
                kind=check.kind,
                repo=check.repo,
                workflow=check.workflow,
                state="config_drift",
                alert=True,
                headline=f"Workflow selector {check.workflow!r} matched multiple workflows.",
                latest_url=repo_actions_url(check.repo),
                lines=[
                    f"Repo: `{check.repo}`",
                    "Matches:",
                    *[f"- `{workflow.get('path', workflow.get('name', workflow.get('id')))!s}`" for workflow in matches],
                ],
            )

        return CheckResult(
            name=check.name,
            kind=check.kind,
            repo=check.repo,
            workflow=check.workflow,
            state="config_drift",
            alert=True,
            headline=f"Workflow {check.workflow!r} was not found.",
            latest_url=repo_actions_url(check.repo),
            lines=[f"Repo: `{check.repo}`"],
        )

    workflow_id = int(workflow_data["id"])
    runs = client.list_runs(check.repo, workflow_id, event=check.event, branch=check.branch)
    latest_run = runs[0] if runs else None
    latest_completed = first_completed_run(runs)

    if latest_run is None:
        result = CheckResult(
            name=check.name,
            kind=check.kind,
            repo=check.repo,
            workflow=check.workflow,
            state="no_data",
            alert=True,
            headline="No matching workflow runs were found.",
            latest_url=workflow_page_url(check.repo, str(workflow_data.get("path", check.workflow))),
        )
        describe_common_metadata(result, workflow_data=workflow_data, latest_run=latest_run)
        return result

    if latest_run.get("status") != "completed":
        result = CheckResult(
            name=check.name,
            kind=check.kind,
            repo=check.repo,
            workflow=check.workflow,
            state="in_progress",
            alert=latest_completed is None or classify_conclusion(latest_completed.get("conclusion")) != "success",
            headline="Latest matching workflow run is still in progress.",
            latest_url=str(latest_run.get("html_url", "")) or None,
        )
        describe_common_metadata(result, workflow_data=workflow_data, latest_run=latest_run)
        if latest_completed:
            result.lines.append(
                f"Last completed run: {format_run(latest_completed)} (`{latest_completed.get('conclusion', 'unknown')}`)"
            )
        else:
            result.lines.append("Last completed run: none")
        return result

    latest_url = str(latest_run.get("html_url", "")) or None
    conclusion = str(latest_run.get("conclusion", ""))
    classification = classify_conclusion(conclusion)

    if classification == "success":
        result = CheckResult(
            name=check.name,
            kind=check.kind,
            repo=check.repo,
            workflow=check.workflow,
            state="success",
            alert=False,
            headline="Latest matching workflow run succeeded.",
            latest_url=latest_url,
        )
        describe_common_metadata(result, workflow_data=workflow_data, latest_run=latest_run)
        return result

    jobs = client.list_jobs(check.repo, int(latest_run["id"]))
    failing = failed_jobs(jobs)

    result = CheckResult(
        name=check.name,
        kind=check.kind,
        repo=check.repo,
        workflow=check.workflow,
        state=classification,
        alert=True,
        headline=f"Latest matching workflow run concluded `{conclusion or 'unknown'}`.",
        latest_url=latest_url,
    )
    describe_common_metadata(result, workflow_data=workflow_data, latest_run=latest_run)
    if failing:
        result.lines.append("Failed jobs:")
        result.lines.extend(f"- {format_job(job)}" for job in failing)
    else:
        result.lines.append("Failed jobs: none found on the run; inspect the workflow run directly.")
    return result


def evaluate_job_check(client: GitHubClient, check: CheckConfig) -> CheckResult:
    workflow_data, matches = resolve_workflow(client, check.repo, check.workflow)
    if workflow_data is None:
        if matches:
            return CheckResult(
                name=check.name,
                kind=check.kind,
                repo=check.repo,
                workflow=check.workflow,
                state="config_drift",
                alert=True,
                headline=f"Workflow selector {check.workflow!r} matched multiple workflows.",
                latest_url=repo_actions_url(check.repo),
                lines=[
                    f"Repo: `{check.repo}`",
                    "Matches:",
                    *[f"- `{workflow.get('path', workflow.get('name', workflow.get('id')))!s}`" for workflow in matches],
                ],
            )

        return CheckResult(
            name=check.name,
            kind=check.kind,
            repo=check.repo,
            workflow=check.workflow,
            state="config_drift",
            alert=True,
            headline=f"Workflow {check.workflow!r} was not found.",
            latest_url=repo_actions_url(check.repo),
            lines=[f"Repo: `{check.repo}`"],
        )

    workflow_id = int(workflow_data["id"])
    runs = client.list_runs(check.repo, workflow_id, event=check.event, branch=check.branch)
    latest_run = runs[0] if runs else None
    latest_completed = first_completed_run(runs)

    if latest_run is None:
        result = CheckResult(
            name=check.name,
            kind=check.kind,
            repo=check.repo,
            workflow=check.workflow,
            state="no_data",
            alert=True,
            headline="No matching workflow runs were found.",
            latest_url=workflow_page_url(check.repo, str(workflow_data.get("path", check.workflow))),
        )
        describe_common_metadata(result, workflow_data=workflow_data, latest_run=latest_run)
        return result

    subject_run = latest_run if latest_run.get("status") == "completed" else latest_completed
    if subject_run is None:
        result = CheckResult(
            name=check.name,
            kind=check.kind,
            repo=check.repo,
            workflow=check.workflow,
            state="in_progress",
            alert=True,
            headline="Latest matching workflow run is in progress and there is no completed run yet.",
            latest_url=str(latest_run.get("html_url", "")) or None,
        )
        describe_common_metadata(result, workflow_data=workflow_data, latest_run=latest_run)
        return result

    jobs = client.list_jobs(check.repo, int(subject_run["id"]))
    matching_jobs = [job for job in jobs if str(job.get("name", "")) == check.job_name]

    if not matching_jobs:
        result = CheckResult(
            name=check.name,
            kind=check.kind,
            repo=check.repo,
            workflow=check.workflow,
            state="config_drift",
            alert=True,
            headline=f"Job {check.job_name!r} was not found in {format_run(subject_run)}.",
            latest_url=str(subject_run.get("html_url", "")) or None,
        )
        describe_common_metadata(result, workflow_data=workflow_data, latest_run=latest_run)
        if latest_run.get("status") != "completed":
            result.lines.append(
                f"Last completed run used for lookup: {format_run(subject_run)} (`{subject_run.get('conclusion', 'unknown')}`)"
            )
        return result

    if len(matching_jobs) > 1:
        result = CheckResult(
            name=check.name,
            kind=check.kind,
            repo=check.repo,
            workflow=check.workflow,
            state="config_drift",
            alert=True,
            headline=f"Job selector {check.job_name!r} matched multiple jobs in {format_run(subject_run)}.",
            latest_url=str(subject_run.get("html_url", "")) or None,
        )
        describe_common_metadata(result, workflow_data=workflow_data, latest_run=latest_run)
        result.lines.append("Matches:")
        result.lines.extend(f"- {format_job(job)}" for job in matching_jobs)
        return result

    job = matching_jobs[0]
    conclusion = job.get("conclusion")
    classification = classify_conclusion(conclusion)
    latest_job_url = str(job.get("html_url", "")) or None
    latest_run_completed = latest_run.get("status") == "completed"

    if latest_run_completed and classification == "success":
        result = CheckResult(
            name=check.name,
            kind=check.kind,
            repo=check.repo,
            workflow=check.workflow,
            state="success",
            alert=False,
            headline="Latest matching job succeeded.",
            latest_url=latest_job_url,
        )
        describe_common_metadata(result, workflow_data=workflow_data, latest_run=latest_run)
        result.lines.append(f"Latest matching job: {format_job(job)}")
        return result

    if not latest_run_completed and classification == "success":
        result = CheckResult(
            name=check.name,
            kind=check.kind,
            repo=check.repo,
            workflow=check.workflow,
            state="in_progress",
            alert=False,
            headline="Latest matching workflow run is still in progress; the last completed job succeeded.",
            latest_url=str(latest_run.get("html_url", "")) or None,
        )
        describe_common_metadata(result, workflow_data=workflow_data, latest_run=latest_run)
        result.lines.append(
            f"Last completed matching job: {format_job(job)} from {format_run(subject_run)}"
        )
        return result

    if not latest_run_completed:
        result = CheckResult(
            name=check.name,
            kind=check.kind,
            repo=check.repo,
            workflow=check.workflow,
            state="in_progress",
            alert=True,
            headline=(
                "Latest matching workflow run is still in progress and the last completed job did not succeed."
            ),
            latest_url=str(latest_run.get("html_url", "")) or None,
        )
        describe_common_metadata(result, workflow_data=workflow_data, latest_run=latest_run)
        result.lines.append(
            f"Last completed matching job: {format_job(job)} (`{conclusion or 'unknown'}`)"
        )
        return result

    result = CheckResult(
        name=check.name,
        kind=check.kind,
        repo=check.repo,
        workflow=check.workflow,
        state=classification,
        alert=True,
        headline=f"Latest matching job concluded `{conclusion or 'unknown'}`.",
        latest_url=latest_job_url,
    )
    describe_common_metadata(result, workflow_data=workflow_data, latest_run=latest_run)
    result.lines.append(f"Latest matching job: {format_job(job)}")
    return result


def evaluate_check(client: GitHubClient, check: CheckConfig) -> CheckResult:
    try:
        if check.kind == "workflow":
            return evaluate_workflow_check(client, check)
        if check.kind == "job":
            return evaluate_job_check(client, check)
        return CheckResult(
            name=check.name,
            kind=check.kind,
            repo=check.repo,
            workflow=check.workflow,
            state="config_drift",
            alert=True,
            headline=f"Unsupported check kind {check.kind!r}.",
            latest_url=fallback_check_url(check),
        )
    except (GitHubApiError, KeyError, ValueError) as exc:
        return CheckResult(
            name=check.name,
            kind=check.kind,
            repo=check.repo,
            workflow=check.workflow,
            state="error",
            alert=True,
            headline=f"Failed to evaluate check: {exc}",
            latest_url=fallback_check_url(check),
        )


def render_report(report: ReportConfig, config_path: Path, results: list[CheckResult], generated_at: str) -> str:
    alerting = [result for result in results if result.alert]
    healthy = [result for result in results if not result.alert]

    lines = [
        "# GitHub Actions supervision report",
        "",
        f"Generated at: `{generated_at}`",
        f"Configuration: `{config_path.as_posix()}`",
        f"Total checks: `{len(results)}`",
        f"Alerting checks: `{len(alerting)}`",
        f"Healthy or non-alerting checks: `{len(healthy)}`",
        "",
    ]

    if alerting:
        lines.extend(["## Checks requiring attention", ""])
        for result in alerting:
            lines.append(f"### {result.name}")
            lines.append(f"{state_badge(result.state)} `{result.state}` {result.headline}")
            if result.latest_url:
                lines.append(f"Link: {markdown_link('details', result.latest_url)}")
            lines.extend(result.lines)
            lines.append("")
    else:
        lines.extend(["## Checks requiring attention", "", "None.", ""])

    lines.extend(["## Healthy or non-alerting checks", ""])
    if healthy:
        for result in healthy:
            lines.append(f"### {result.name}")
            lines.append(f"{state_badge(result.state)} `{result.state}` {result.headline}")
            lines.extend(result.lines)
            lines.append("")
    else:
        lines.append("None.")
        lines.append("")

    if report.close_on_success:
        lines.append("_Policy: the issue is closed automatically when all checks are healthy._")
    else:
        lines.append("_Policy: the issue remains open until closed manually._")

    return "\n".join(lines).strip() + "\n"


def write_github_output(path: str | None, values: dict[str, str]) -> None:
    if not path:
        return

    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor public GitHub Actions runs defined in a TOML file.")
    parser.add_argument("--config", default="gha-supervision.toml", help="Path to the TOML configuration file.")
    parser.add_argument("--report", default="build/gha-supervision-report.md", help="Markdown report output path.")
    parser.add_argument("--json-out", default="build/gha-supervision-result.json", help="JSON result output path.")
    parser.add_argument(
        "--github-output",
        default=os.environ.get("GITHUB_OUTPUT"),
        help="Optional GitHub Actions output file path.",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    report_path = Path(args.report)
    json_path = Path(args.json_out)

    report_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)

    default_report_config = ReportConfig(
        issue_title="GitHub Actions supervision report",
        issue_labels=["gha-supervision"],
        close_on_success=True,
    )

    try:
        report_config, checks = load_config(config_path)
        client = GitHubClient(token=os.environ.get("ACTIONS_MONITOR_TOKEN"))
        generated_at = now_iso()
        results = [evaluate_check(client, check) for check in checks]
    except Exception as exc:  # noqa: BLE001
        report_config = default_report_config
        generated_at = now_iso()
        results = [
            CheckResult(
                name="monitor bootstrap",
                kind="internal",
                repo="-",
                workflow=config_path.as_posix(),
                state="error",
                alert=True,
                headline=f"The monitor failed before it could evaluate the configured checks: {exc}",
            )
        ]

    has_alert = any(result.alert for result in results)

    report_text = render_report(report_config, config_path, results, generated_at)
    report_path.write_text(report_text, encoding="utf-8")

    payload = {
        "generated_at": generated_at,
        "has_alert": has_alert,
        "report": {
            "issue_title": report_config.issue_title,
            "issue_labels": report_config.issue_labels,
            "close_on_success": report_config.close_on_success,
        },
        "results": [result.as_dict() for result in results],
    }
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    write_github_output(
        args.github_output,
        {
            "has_alert": "true" if has_alert else "false",
            "report_path": report_path.as_posix(),
            "result_path": json_path.as_posix(),
            "issue_title": report_config.issue_title,
            "issue_labels": ",".join(report_config.issue_labels),
            "close_on_success": "true" if report_config.close_on_success else "false",
        },
    )

    sys.stdout.write(report_text)
    return 1 if has_alert else 0


if __name__ == "__main__":
    raise SystemExit(main())
