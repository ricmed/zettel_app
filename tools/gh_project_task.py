# -*- coding: utf-8 -*-
"""Add one GitHub issue to the Zettel Project (Tarefas - Agent Skills).

Requires `gh` authenticated with scopes: repo, project, read:project.

Examples:
  python tools/gh_project_task.py add --title "Corrigir paging MD" --priority p4 --area harvest
  python tools/gh_project_task.py add --title "..." --body-file body.md --parent 10 --label enhancement
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_REPO = os.environ.get("ZETTEL_GH_REPO", "ricmed/zettel_app")
DEFAULT_OWNER = os.environ.get("ZETTEL_GH_OWNER", "ricmed")
DEFAULT_PROJECT_NUMBER = int(os.environ.get("ZETTEL_GH_PROJECT", "3"))
DEFAULT_MILESTONE = os.environ.get(
    "ZETTEL_GH_MILESTONE", "Agent Skills & progressive disclosure"
)
DEFAULT_PARENT = os.environ.get("ZETTEL_GH_PARENT", "10")
DEFAULT_STATUS = "Todo"

PRIORITY_LABELS = {f"p{i}": f"priority:p{i}" for i in range(6)}
AREA_LABELS = {
    "cli": "area:cli",
    "extract": "area:extract",
    "harvest": "area:harvest",
    "ask": "area:ask",
    "evals": "area:evals",
    "web": "area:web",
}


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        args, check=False, text=True, encoding="utf-8", capture_output=True,
    )
    if check and proc.returncode != 0:
        sys.stderr.write(f"CMD FAILED: {' '.join(args)}\n{proc.stderr}\n{proc.stdout}\n")
        proc.check_returncode()
    return proc


def graphql(query: str, variables: dict) -> dict:
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        if isinstance(value, bool):
            args.extend(["-F", f"{key}={str(value).lower()}"])
        else:
            args.extend(["-f", f"{key}={value}"])
    data = json.loads(run(args).stdout)
    if data.get("errors"):
        raise RuntimeError(json.dumps(data["errors"], indent=2))
    return data["data"]


def issue_payload(repo: str, number: int) -> dict:
    view = json.loads(
        run(
            ["gh", "issue", "view", str(number), "--repo", repo, "--json", "number,url,title"]
        ).stdout
    )
    rest = json.loads(run(["gh", "api", f"repos/{repo}/issues/{number}"]).stdout)
    view["databaseId"] = rest["id"]
    return view


def find_issue_by_title(repo: str, title: str) -> dict | None:
    listing = json.loads(
        run(
            [
                "gh", "issue", "list", "--repo", repo,
                "--state", "open", "--limit", "100",
                "--json", "number,title,url",
            ]
        ).stdout
    )
    for item in listing:
        if item["title"] == title:
            return issue_payload(repo, item["number"])
    return None


def create_issue(
    repo: str,
    title: str,
    body: str,
    labels: list[str],
    milestone: str | None,
) -> dict:
    existing = find_issue_by_title(repo, title)
    if existing:
        print(f"issue exists: {existing['url']}", file=sys.stderr)
        return existing
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".md", delete=False
    ) as fh:
        fh.write(body)
        path = fh.name
    args = ["gh", "issue", "create", "--repo", repo, "--title", title, "--body-file", path]
    if milestone:
        args.extend(["--milestone", milestone])
    for lab in labels:
        args.extend(["--label", lab])
    proc = run(args)
    Path(path).unlink(missing_ok=True)
    url = proc.stdout.strip()
    number = int(url.rstrip("/").split("/")[-1])
    print(f"issue created: {url}", file=sys.stderr)
    return issue_payload(repo, number)


def link_sub_issue(repo: str, parent: int, child_database_id: int) -> None:
    proc = run(
        [
            "gh", "api", "--method", "POST",
            f"repos/{repo}/issues/{parent}/sub_issues",
            "-F", f"sub_issue_id={child_database_id}",
        ],
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or "") + (proc.stdout or "")
        if "already" in err.lower() or "duplicate" in err.lower():
            print(f"sub-issue already linked to #{parent}", file=sys.stderr)
            return
        sys.stderr.write(err)
        proc.check_returncode()
    print(f"sub-issue linked -> #{parent}", file=sys.stderr)


def project_view(owner: str, number: int) -> dict:
    return json.loads(
        run(
            ["gh", "project", "view", str(number), "--owner", owner, "--format", "json"]
        ).stdout
    )


def status_field(owner: str, project_number: int, status_name: str) -> tuple[str, str]:
    fields = json.loads(
        run(
            [
                "gh", "project", "field-list", str(project_number),
                "--owner", owner, "--format", "json",
            ]
        ).stdout
    )
    status = next(f for f in fields["fields"] if f["name"] == "Status")
    option = next(o for o in status["options"] if o["name"] == status_name)
    return status["id"], option["id"]


def issue_already_in_project(owner: str, project_number: int, issue_number: int) -> bool:
    items = json.loads(
        run(
            [
                "gh", "project", "item-list", str(project_number),
                "--owner", owner, "--limit", "100", "--format", "json",
            ]
        ).stdout
    )
    for item in items.get("items", []):
        content = item.get("content") or {}
        if content.get("number") == issue_number:
            return True
    return False


def add_to_project(owner: str, project_number: int, issue_url: str) -> str:
    data = json.loads(
        run(
            [
                "gh", "project", "item-add", str(project_number),
                "--owner", owner, "--url", issue_url, "--format", "json",
            ]
        ).stdout
    )
    item_id = data.get("id") or data.get("itemId")
    if not item_id:
        raise RuntimeError(f"project item-add returned no id: {data}")
    print(f"project item: {item_id}", file=sys.stderr)
    return item_id


def set_status(project_id: str, item_id: str, field_id: str, option_id: str) -> None:
    query = """
    mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
      updateProjectV2ItemFieldValue(input: {
        projectId: $projectId
        itemId: $itemId
        fieldId: $fieldId
        value: { singleSelectOptionId: $optionId }
      }) { projectV2Item { id } }
    }
    """
    graphql(query, {
        "projectId": project_id,
        "itemId": item_id,
        "fieldId": field_id,
        "optionId": option_id,
    })


def collect_labels(args: argparse.Namespace) -> list[str]:
    labels: list[str] = []
    if args.priority:
        labels.append(PRIORITY_LABELS[args.priority])
    if args.area:
        labels.append(AREA_LABELS[args.area])
    labels.extend(args.label or [])
    if args.type_label:
        labels.append(args.type_label)
    seen: set[str] = set()
    out: list[str] = []
    for lab in labels:
        if lab not in seen:
            seen.add(lab)
            out.append(lab)
    return out


def read_body(args: argparse.Namespace) -> str:
    if args.body_file:
        return Path(args.body_file).read_text(encoding="utf-8")
    if args.body:
        return args.body
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return args.title


def cmd_add(args: argparse.Namespace) -> int:
    body = read_body(args).strip() or args.title
    labels = collect_labels(args)
    milestone = None if args.no_milestone else args.milestone
    issue = create_issue(args.repo, args.title, body, labels, milestone)

    parent = None if args.no_parent else args.parent
    if parent:
        link_sub_issue(args.repo, int(parent), int(issue["databaseId"]))

    if not issue_already_in_project(args.owner, args.project, issue["number"]):
        item_id = add_to_project(args.owner, args.project, issue["url"])
        proj = project_view(args.owner, args.project)
        field_id, option_id = status_field(args.owner, args.project, args.status)
        set_status(proj["id"], item_id, field_id, option_id)
        print(f"status {args.status}: #{issue['number']}", file=sys.stderr)
    else:
        print(f"already in project: #{issue['number']}", file=sys.stderr)

    result = {
        "number": issue["number"],
        "url": issue["url"],
        "title": issue["title"],
        "project": f"https://github.com/users/{args.owner}/projects/{args.project}",
        "parent": int(parent) if parent else None,
        "labels": labels,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a GitHub issue and add it to the Zettel Project board.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="Create issue (idempotent by title) and pin to the Project")
    add.add_argument("--title", required=True, help="Issue title (imperative PT-BR)")
    add.add_argument("--body", default="", help="Markdown body (or pipe stdin / --body-file)")
    add.add_argument("--body-file", help="Path to markdown body")
    add.add_argument(
        "--priority", choices=sorted(PRIORITY_LABELS),
        help="Adds label priority:pN",
    )
    add.add_argument(
        "--area", choices=sorted(AREA_LABELS),
        help="Adds label area:<name>",
    )
    add.add_argument(
        "--label", action="append", default=[],
        help="Extra label (repeatable)",
    )
    add.add_argument(
        "--type-label", default="enhancement",
        help="Type label (default: enhancement). Use '' to skip.",
    )
    add.add_argument("--repo", default=DEFAULT_REPO)
    add.add_argument("--owner", default=DEFAULT_OWNER)
    add.add_argument("--project", type=int, default=DEFAULT_PROJECT_NUMBER)
    add.add_argument("--milestone", default=DEFAULT_MILESTONE)
    add.add_argument("--no-milestone", action="store_true")
    add.add_argument(
        "--parent", default=DEFAULT_PARENT,
        help="Parent epic issue number (default: 10). Empty / --no-parent to skip.",
    )
    add.add_argument("--no-parent", action="store_true")
    add.add_argument("--status", default=DEFAULT_STATUS, help="Project Status option")
    add.set_defaults(func=cmd_add)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.type_label == "":
        args.type_label = None
    if args.parent == "":
        args.no_parent = True
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
