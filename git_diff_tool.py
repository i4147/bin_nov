#!/data/data/com.termux/files/usr/bin/env python
"""Git diff and status reports.
Subcommands: ``diff`` writes a JSON report (files, stats, optional patch) between two refs, ``status`` summarises every repository below a root (``--dirty-only`` filters) and ``added-files`` lists the files added per commit.
Usage: git_diff_tool {diff,status,added-files} [options] [repo] Positional arguments: repo older Older ref (default: HEAD~1) newer Newer ref (default: HEAD) root Subcommands: diff JSON report between two commits status Status of every repo under a root added-files Files added per commit (JSON) Options: -o, --output Default: <repo>/diff_report.json --patch Include full patch (default) --no-patch Skip the patch text --dirty-only Examples: $ git_diff_tool diff .
HEAD~3 HEAD -o report.json Command-line script: run ``python git_diff_tool.py`` or the ``git_diff_tool`` command.
The module is importable as well."""

import argparse
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
import json
from pathlib import Path
import subprocess
import sys

from dh import MAX_WORKERS


def run_git(repo: Path, *git_args: str) -> str:
    proc = subprocess.run(["git", *git_args], cwd=repo, capture_output=True, text=True)
    if proc.returncode != 0:
        msg = f"git {' '.join(git_args)} failed: {proc.stderr.strip()}"
        raise RuntimeError(msg)
    return proc.stdout


def try_git(repo: Path, *git_args: str) -> str | None:
    try:
        return run_git(repo, *git_args)
    except RuntimeError:
        return None


def last_two_commits(repo: Path) -> tuple[str, str]:
    out: list[str] = run_git(repo, "log", "-2", "--format=%H").strip().splitlines()
    if len(out) < 2:
        msg = "Repository needs at least 2 commits to diff."
        raise RuntimeError(msg)
    return out[1], out[0]


def commit_meta(repo: Path, ref: str) -> dict[str, str]:
    fmt: str = "%an%x00%ae%x00%ad%x00%s"
    line: str = run_git(repo, "show", "-s", f"--format={fmt}", ref).strip()
    author, email, date, subject = line.split("\x00")
    full: str = run_git(repo, "rev-parse", ref).strip()
    return {"hash": full, "author": author, "email": email, "date": date, "subject": subject}


def file_stats(repo: Path, older: str, newer: str) -> list[dict[str, object]]:
    numstat: dict[str, tuple[int | None, int | None]] = {}
    for line in run_git(repo, "diff", "--numstat", older, newer).splitlines():
        parts: list[str] = line.split("\t")
        if len(parts) == 3:
            added, deleted, path = parts
            numstat[path] = (None if added == "-" else int(added), None if deleted == "-" else int(deleted))
    files: list[dict[str, object]] = []
    for line in run_git(repo, "diff", "--name-status", older, newer).splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status: str = parts[0]
        path: str = parts[-1]
        old_path: str | None = parts[1] if status.startswith(("R", "C")) and len(parts) == 3 else None
        ins, dele = numstat.get(path, (None, None))
        files.append({"status": status, "path": path, "old_path": old_path, "insertions": ins, "deletions": dele})
    return files


def cmd_diff(args: argparse.Namespace) -> int:
    repo: Path = args.repo
    try:
        older, newer = (args.older, args.newer) if args.older and args.newer else last_two_commits(repo)
        report: dict[str, object] = {
            "repo": str(repo.resolve()),
            "from_commit": commit_meta(repo, older),
            "to_commit": commit_meta(repo, newer),
            "files": file_stats(repo, older, newer),
        }
        if args.patch:
            report["patch"] = run_git(repo, "diff", older, newer)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    out: Path = args.output or repo / "diff_report.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    files: object = report["files"]
    count: int = len(files) if isinstance(files, list) else 0
    print(f"Wrote diff report ({count} file(s)): {out}")
    return 0


def find_repos(root: Path) -> list[Path]:
    repos: list[Path] = []
    for git_dir in root.rglob(".git"):
        repos.append(git_dir.parent)
    return sorted(set(repos))


def repo_status(repo: Path) -> dict[str, object]:
    branch: str = (try_git(repo, "rev-parse", "--abbrev-ref", "HEAD") or "?").strip()
    porcelain: str = try_git(repo, "status", "--porcelain") or ""
    changed: list[str] = [line for line in porcelain.splitlines() if line.strip()]
    ahead_behind: str = (try_git(repo, "rev-list", "--left-right", "--count", "HEAD...@{upstream}") or "").strip()
    last: str = (try_git(repo, "log", "-1", "--format=%h %s") or "").strip()
    return {
        "repo": str(repo),
        "branch": branch,
        "dirty": bool(changed),
        "changed": len(changed),
        "upstream": ahead_behind,
        "last_commit": last,
    }


def cmd_status(args: argparse.Namespace) -> int:
    repos: list[Path] = find_repos(args.root)
    if not repos:
        print(f"No git repositories under {args.root}")
        return 0
    with ProcessPoolExecutor(max_workers=max(1, MAX_WORKERS)) as pool:
        results: list[dict[str, object]] = list(pool.map(repo_status, repos))
    shown: int = 0
    for info in sorted(results, key=lambda item: str(item["repo"])):
        if args.dirty_only and not info["dirty"]:
            continue
        shown += 1
        state: str = f"dirty ({info['changed']} changed)" if info["dirty"] else "clean"
        up: str = f" upstream:{info['upstream']}" if info["upstream"] else ""
        print(f"{info['repo']} [{info['branch']}] {state}{up} last: {info['last_commit']}")
    print(f"# {shown}/{len(results)} repo(s) shown", file=sys.stderr)
    return 0


def cmd_added_files(args: argparse.Namespace) -> int:
    out: str | None = try_git(args.repo, "log", "--diff-filter=A", "--name-only", "--pretty=format:COMMIT %h")
    if out is None:
        print("Error: not a git repository (or empty).", file=sys.stderr)
        return 1
    result: dict[str, list[str]] = {}
    current: str | None = None
    names: set[str] = set()
    for line in out.splitlines() + ["COMMIT END"]:
        if line.startswith("COMMIT "):
            if current is not None:
                result[current] = sorted(names)
            current = line.removeprefix("COMMIT ").strip()[:8]
            names = set()
        elif line.strip():
            names.add(Path(line.strip()).name)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(result)} commit(s) to {args.output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Git diff/status reporting.")
    sub = parser.add_subparsers(dest="command", required=True)
    df = sub.add_parser("diff", help="JSON report between two commits")
    df.add_argument("repo", nargs="?", type=Path, default=Path.cwd())
    df.add_argument("older", nargs="?", default=None, help="Older ref (default: HEAD~1)")
    df.add_argument("newer", nargs="?", default=None, help="Newer ref (default: HEAD)")
    df.add_argument("-o", "--output", type=Path, default=None, help="Default: <repo>/diff_report.json")
    df.add_argument("--patch", action="store_true", default=True, help="Include full patch (default)")
    df.add_argument("--no-patch", dest="patch", action="store_false", help="Skip the patch text")
    df.set_defaults(handler=cmd_diff)
    st = sub.add_parser("status", help="Status of every repo under a root")
    st.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    st.add_argument("--dirty-only", action="store_true")
    st.set_defaults(handler=cmd_status)
    af = sub.add_parser("added-files", help="Files added per commit (JSON)")
    af.add_argument("repo", nargs="?", type=Path, default=Path.cwd())
    af.add_argument("-o", "--output", type=Path, default=Path("added_files.json"))
    af.set_defaults(handler=cmd_added_files)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
