#!/data/data/com.termux/files/usr/bin/env python
"""Report the files added or removed in every commit of a git repository.
Walks the revision range (HEAD by default) with ``git diff-tree`` or by checking out each commit, explodes the renames into a removal plus an addition, cross-checks the result against the working tree and renders a text, JSON, CSV or Markdown ledger.
Usage: addel [repo] [--revision RANGE] [--mode diff-tree|walk] [--json PATH] Positional arguments: repo Any path inside the repository (default: cwd) Options: --revision Revision or range to walk (default: HEAD) --mode Diff-tree: one batched git call (fast); walk: check out each commit and rglob it (paranoid) --first-parent Follow only the first parent of merge commits --no-diff-merges Report no changes for merge commits at all --keep-renames Let git detect renames (they are still exploded into a removal plus an addition) --no-verify Skip the pathlib cross-check against the working tree --json Write a JSON report --csv Write a CSV report --md Write a Markdown report --max-files Paths printed per commit in the console ledger (default: 25) ...
plus 3 more; run ``addel --help`` for the full list.
Examples: $ addel .
--json ledger.json Command-line script: run ``python addel.py`` or the ``addel`` command.
The module is importable as well."""

import argparse
from collections import Counter
import contextlib
import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterable, Optional, Sequence
import re

from dh import SKIP_DIRS

UNIT_SEP: str = "\x1f"
DIFF_FILTER: str = "AD"
RESET: str = "\033[0m"
STYLES: dict[str, str] = {
    "bold": "\033[1m",
    "dim": "\033[2m",
    "green": "\033[32m",
    "red": "\033[31m",
    "amber": "\033[33m",
    "teal": "\033[36m",
}
EXAMPLES: str = """
examples:
  python addel.py                              ledger for the repo you are in
  python addel.py ~/code/api --json out.json   one machine readable report
  python addel.py --mode walk --md NOTES.md    audit by re-snapshotting trees
  python addel.py "main~25..main" --top 5      a slice of history
"""


class RepoError(RuntimeError):
    pass


def painter(enabled: bool) -> Callable[[str, str], str]:

    def paint(text: str, *styles: str) -> str:
        if not enabled or not styles:
            return text
        return "".join(STYLES[s] for s in styles if s in STYLES) + text + RESET

    return paint


def discover_repo(start: Path) -> tuple[Path, Path]:
    start = Path(start).expanduser()
    try:
        start = start.resolve()
    except OSError:
        start = start.absolute()

    for directory in (start, *start.parents):
        git_entry = directory / ".git"
        if git_entry.is_dir() or git_entry.is_file():
            return directory, git_entry
    msg = f"no git repository found at or above {start}"
    raise RepoError(msg)


def git(repo: Path, *args: str) -> str:
    command = ["git", "-C", str(repo), *args]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        msg = f"could not execute git: {exc}"
        raise RepoError(msg) from exc
    if proc.returncode != 0:
        msg = f"git {' '.join(args)} failed ({proc.returncode}): {(proc.stderr or '').strip()}"
        raise RepoError(msg)
    return proc.stdout


@dataclass(frozen=True, slots=True)
class Commit:
    sha: str
    short: str
    author: str
    date: str
    parents: tuple[str, ...]
    subject: str

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1

    @property
    def is_root(self) -> bool:
        return not self.parents

    def when(self) -> str:
        try:
            return datetime.fromisoformat(self.date).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return self.date


LOG_FORMAT = UNIT_SEP.join(["%H", "%h", "%an", "%aI", "%P", "%s"])


def parse_commit_header(record: str) -> Commit:
    sha, short, author, date, parents, subject = record.split(UNIT_SEP, 5)
    return Commit(sha, short, author, date, tuple(parents.split()), subject)


def list_commits(repo: Path, revision: str, first_parent: bool = False) -> list[Commit]:
    args = ["log", "--reverse", "--date-order", f"--format={LOG_FORMAT}"]
    if first_parent:
        args.append("--first-parent")
    args.append(revision)
    return [parse_commit_header(line) for line in git(repo, *args).splitlines() if UNIT_SEP in line]


@dataclass(frozen=True, slots=True)
class Change:
    status: str
    path: str

    @property
    def added(self) -> bool:
        return self.status == "A"


@dataclass(slots=True)
class CommitEntry:
    commit: Commit
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def quiet(self) -> bool:
        return not self.added and not self.removed

    def to_dict(self) -> dict[str, object]:
        c = self.commit
        return {
            "sha": c.sha,
            "short": c.short,
            "date": c.date,
            "author": c.author,
            "subject": c.subject,
            "is_merge": c.is_merge,
            "is_root": c.is_root,
            "added": self.added,
            "removed": self.removed,
        }


def parse_name_status(tokens: Sequence[str]) -> list[Change]:
    changes: list[Change] = []
    index = 0
    total = len(tokens)
    while index < total:
        token = tokens[index]
        letter = token[:1]
        if letter in ("R", "C") and index + 2 < total:
            changes.append(Change("D", tokens[index + 1]))
            changes.append(Change("A", tokens[index + 2]))
            index += 3
        elif letter in ("A", "D") and index + 1 < total:
            changes.append(Change(letter, tokens[index + 1]))
            index += 2
        else:
            index += 1
    return changes


def collect_ledger(
    repo: Path,
    revision: str = "HEAD",
    *,
    keep_renames: bool = False,
    diff_merges: bool = True,
    first_parent: bool = False,
) -> list[CommitEntry]:
    base = [
        "log",
        "--reverse",
        "--date-order",
        "-z",
        "--root",
        f"--format={LOG_FORMAT}",
        "--name-status",
        f"--diff-filter={DIFF_FILTER}",
        "--find-renames" if keep_renames else "--no-renames",
    ]
    if first_parent:
        base.append("--first-parent")

    def run(extra: Sequence[str]) -> str:
        return git(repo, *base, *extra, revision)

    try:
        raw = run(["--diff-merges=first-parent"] if diff_merges else [])
    except RepoError as exc:
        if not diff_merges:
            raise
        sys.stderr.write(f"note: {exc}\nnote: retrying without --diff-merges\n")
        raw = run([])

    entries: list[CommitEntry] = []
    current: Optional[CommitEntry] = None
    pending: list[str] = []

    def flush() -> None:
        if current is not None and pending:
            for change in parse_name_status(pending):
                (current.added if change.added else current.removed).append(change.path)
        pending.clear()

    for token in (t for t in raw.split("\0") if t):
        if UNIT_SEP in token:
            flush()
            current = CommitEntry(parse_commit_header(token))
            entries.append(current)
        else:
            pending.append(token)
    flush()
    return entries


def snapshot_tree(root: Path, skip: Iterable[str] = SKIP_DIRS) -> set[str]:
    skip = set(skip)
    found: set[str] = set()
    root = Path(root)
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if skip.intersection(relative.parts[:-1]):
            continue
        try:
            if path.is_symlink() or path.is_file():
                found.add(relative.as_posix())
        except OSError:
            continue
    return found


def drop_worktree(repo: Path, tree: Path) -> None:
    if not tree.exists():
        return
    try:
        git(repo, "worktree", "remove", "--force", str(tree))
    except RepoError:
        shutil.rmtree(tree, ignore_errors=True)
        with contextlib.suppress(RepoError):
            git(repo, "worktree", "prune")


def walk_ledger(
    repo: Path,
    commits: Sequence[Commit],
    *,
    log: Callable[[str], None] = lambda _message: None,
) -> list[CommitEntry]:
    sandbox = Path(tempfile.mkdtemp(prefix="addel-walk-"))
    tree = sandbox / "tree"
    entries: list[CommitEntry] = []
    previous: set[str] = set()
    total = len(commits)
    try:
        for number, commit in enumerate(commits, start=1):
            drop_worktree(repo, tree)
            git(repo, "worktree", "add", "--detach", "--force", str(tree), commit.sha)
            current = snapshot_tree(tree)
            entries.append(
                CommitEntry(
                    commit,
                    sorted(current.difference(previous)),
                    sorted(previous.difference(current)),
                )
            )
            previous = current
            log(f"  [{number}/{total}] {commit.short} +{len(entries[-1].added)} -{len(entries[-1].removed)}")
    finally:
        drop_worktree(repo, tree)
        shutil.rmtree(sandbox, ignore_errors=True)
    return entries


@dataclass(slots=True)
class Verification:
    tracked_now: int
    ghosts: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "files_on_disk": self.tracked_now,
            "ghosts": self.ghosts,
            "missing": self.missing,
        }


def verify_against_disk(repo: Path, entries: Sequence[CommitEntry]) -> Verification:
    on_disk = snapshot_tree(repo)
    last_event: dict[str, str] = {}
    for entry in entries:
        for path in entry.added:
            last_event[path] = "A"
        for path in entry.removed:
            last_event[path] = "D"

    expected = {p for p, status in last_event.items() if status == "A"}
    ghosts = sorted(p for p in on_disk if last_event.get(p) == "D")
    missing = sorted(expected.difference(on_disk))
    return Verification(len(on_disk), ghosts, missing)


def directory_of(path: str, depth: int = 2) -> str:
    parts = Path(path).parts
    if len(parts) == 1:
        return "(root)"
    return "/".join(parts[:depth]) if len(parts) > depth else "/".join(parts[:-1])


@dataclass(slots=True)
class Summary:
    commits: int
    active_commits: int
    merges: int
    added: int
    removed: int
    unique_added: int
    unique_removed: int
    span: tuple[str, str]
    hot_directories: list[tuple[str, int]]
    churn: list[tuple[str, int]]

    @property
    def net(self) -> int:
        return self.added - self.removed


def summarize(entries: Sequence[CommitEntry], top: int = 8) -> Summary:
    added_paths = [p for e in entries for p in e.added]
    removed_paths = [p for e in entries for p in e.removed]

    directories: Counter = Counter()
    for path in added_paths + removed_paths:
        directories[directory_of(path)] += 1

    churn: Counter = Counter(added_paths)
    churn.update(removed_paths)

    dated = [e for e in entries if e.commit.date]
    span = (dated[0].commit.when(), dated[-1].commit.when()) if dated else ("-", "-")
    return Summary(
        commits=len(entries),
        active_commits=sum(1 for e in entries if not e.quiet),
        merges=sum(1 for e in entries if e.commit.is_merge),
        added=len(added_paths),
        removed=len(removed_paths),
        unique_added=len(set(added_paths)),
        unique_removed=len(set(removed_paths)),
        span=span,
        hot_directories=directories.most_common(top),
        churn=churn.most_common(top),
    )


def render_text(
    repo: Path,
    revision: str,
    mode: str,
    entries: Sequence[CommitEntry],
    summary: Summary,
    verification: Optional[Verification],
    *,
    max_files: int = 25,
    paint: Callable[[str, str], str],
) -> str:
    out: list[str] = []
    width = 74
    out.append(paint("addel", "bold", "teal") + paint(" · ledger of added & removed files", "dim"))
    out.append(paint("repository ", "dim") + str(repo))
    out.append(paint("revision   ", "dim") + f"{revision}  ({mode} mode)")
    out.append(paint("span       ", "dim") + f"{summary.span[0]} → {summary.span[1]}")
    out.append(paint("─" * width, "dim"))

    if not entries:
        out.append(paint("no commits matched that revision", "amber"))
        return "\n".join(out)

    for number, entry in enumerate(entries, start=1):
        commit = entry.commit
        head = f"{number}/{summary.commits} "
        out.append("")
        out.append(paint("─" * 3 + " " + head, "dim") + paint("─" * max(0, width - len(head) - 3), "dim"))
        flags = " merge" if commit.is_merge else (" root" if commit.is_root else "")
        out.append(
            paint(commit.short, "bold", "amber")
            + "  "
            + paint(commit.when(), "dim")
            + "  "
            + commit.author
            + paint(flags, "dim")
        )
        out.append("  " + paint(f'"{commit.subject}"', "dim"))
        if entry.quiet:
            out.append("  " + paint("no files added or removed", "dim"))
            continue

        shown = 0
        for path in entry.added:
            if shown >= max_files:
                break
            out.append("  " + paint("+ ", "green") + path)
            shown += 1
        for path in entry.removed:
            if shown >= max_files:
                break
            out.append("  " + paint("- ", "red") + path)
            shown += 1
        hidden = len(entry.added) + len(entry.removed) - shown
        if hidden > 0:
            out.append("  " + paint(f"… {hidden} more path(s) — raise --max-files", "dim"))
        out.append(
            "  " + paint(f"+{len(entry.added)}", "green") + paint(" / ", "dim") + paint(f"-{len(entry.removed)}", "red")
        )

    out.append("")
    out.append(paint("═" * width, "dim"))
    out.append(paint("summary", "bold", "teal"))
    rows = [
        ("commits scanned", f"{summary.commits}  ({summary.active_commits} with activity, {summary.merges} merges)"),
        ("files added", f"{summary.added}  ({summary.unique_added} distinct paths)"),
        ("files removed", f"{summary.removed}  ({summary.unique_removed} distinct paths)"),
        ("net", f"{summary.net:+d}"),
    ]
    label_width = max(len(label) for label, _ in rows)
    for label, value in rows:
        out.append("  " + paint(label.ljust(label_width), "dim") + "  " + value)

    if summary.hot_directories:
        out.append("")
        out.append("  " + paint("hottest directories", "dim"))
        peak = summary.hot_directories[0][1] or 1
        for name, count in summary.hot_directories:
            bar = "█" * max(1, round(count / peak * 28))
            out.append("    " + name.ljust(28)[:28] + paint(bar, "teal") + paint(f" {count}", "dim"))

    if summary.churn:
        out.append("")
        out.append("  " + paint("most churned paths", "dim"))
        for name, count in summary.churn:
            out.append("    " + paint(f"{count:>3}", "amber") + "  " + name)

    if verification is not None:
        out.append("")
        out.append(
            "  "
            + paint("working tree cross-check", "dim")
            + paint(f"  ({verification.tracked_now} files on disk)", "dim")
        )
        if verification.missing:
            out.append(
                "    "
                + paint("missing", "red")
                + ": "
                + ", ".join(verification.missing[:6])
                + (" …" if len(verification.missing) > 6 else "")
            )
        if verification.ghosts:
            out.append(
                "    "
                + paint("ghosts", "amber")
                + ": "
                + ", ".join(verification.ghosts[:6])
                + (" …" if len(verification.ghosts) > 6 else "")
            )
        if not verification.missing and not verification.ghosts:
            out.append("    " + paint("history and disk agree", "green"))

    return "\n".join(out)


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_csv(path: Path, entries: Sequence[CommitEntry]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["sha", "short", "date", "author", "subject", "status", "path"])
        for entry in entries:
            for status, paths in (("added", entry.added), ("removed", entry.removed)):
                for item in paths:
                    writer.writerow([
                        entry.commit.sha,
                        entry.commit.short,
                        entry.commit.date,
                        entry.commit.author,
                        entry.commit.subject,
                        status,
                        item,
                    ])


def write_markdown(path: Path, repo: Path, revision: str, entries: Sequence[CommitEntry], summary: Summary) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        "# Added & removed files",
        "",
        (
            f"`{repo}` @ `{revision}` — {summary.commits} commits, "
            f"**+{summary.added}** / **-{summary.removed}** (net {summary.net:+d})."
        ),
        "",
        "| # | commit | date | author | subject | + | - |",
        "|--:|--------|------|--------|---------|--:|--:|",
    ]
    for number, entry in enumerate(entries, start=1):
        commit = entry.commit
        subject = commit.subject.replace("|", "\\|") or "(no subject)"
        lines.append(
            f"| {number} | `{commit.short}` | {commit.when()} | {commit.author} "
            f"| {subject} | {len(entry.added)} | {len(entry.removed)} |"
        )
    lines.append("")
    lines.append("## Per-commit detail")
    for entry in entries:
        if entry.quiet:
            continue
        commit = entry.commit
        lines.append("")
        lines.append(
            f"<details><summary><code>{commit.short}</code> "
            f"+{len(entry.added)} -{len(entry.removed)} — {commit.subject}</summary>"
        )
        lines.append("")
        for path_item in entry.added:
            lines.append(f"- `+` {path_item}")
        for path_item in entry.removed:
            lines.append(f"- `-` {path_item}")
        lines.append("")
        lines.append("</details>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="addel.py",
        description="Report the files added or removed in every commit of a git "
        "repository. Modified files are ignored on purpose.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "repo", nargs="?", type=Path, default=Path.cwd(), help="any path inside the repository (default: cwd)"
    )
    parser.add_argument("--revision", default="HEAD", help="revision or range to walk (default: HEAD)")
    parser.add_argument(
        "--mode",
        choices=("diff-tree", "walk"),
        default="diff-tree",
        help="diff-tree: one batched git call (fast); walk: check out each commit and rglob it (paranoid)",
    )
    parser.add_argument("--first-parent", action="store_true", help="follow only the first parent of merge commits")
    parser.add_argument("--no-diff-merges", action="store_true", help="report no changes for merge commits at all")
    parser.add_argument(
        "--keep-renames",
        action="store_true",
        help="let git detect renames (they are still exploded into a removal plus an addition)",
    )
    parser.add_argument(
        "--no-verify", action="store_true", help="skip the pathlib cross-check against the working tree"
    )
    parser.add_argument("--json", type=Path, metavar="FILE", help="write a JSON report")
    parser.add_argument("--csv", type=Path, metavar="FILE", help="write a CSV report")
    parser.add_argument("--md", type=Path, metavar="FILE", help="write a Markdown report")
    parser.add_argument(
        "--max-files",
        type=int,
        default=25,
        metavar="N",
        help="paths printed per commit in the console ledger (default: 25)",
    )
    parser.add_argument(
        "--top", type=int, default=8, metavar="N", help="rows in the hot-directory and churn tables (default: 8)"
    )
    parser.add_argument("--no-color", action="store_true", help="never emit ANSI escapes")
    parser.add_argument("--quiet", action="store_true", help="no progress on stderr")
    return parser.parse_args(list(sys.argv[1:] if argv is None else argv))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    if os.name == "nt":
        os.system("")
    use_color = not args.no_color and os.environ.get("NO_COLOR") is None and sys.stdout.isatty()
    paint = painter(use_color)
    log: Callable[[str], None] = (lambda _m: None) if args.quiet else (lambda m: print(m, file=sys.stderr))

    if shutil.which("git") is None:
        print("error: the `git` executable was not found on PATH", file=sys.stderr)
        return 3

    try:
        repo, git_entry = discover_repo(args.repo)
    except RepoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    log(f"repo   {repo}")
    log(f".git   {git_entry} ({'directory' if git_entry.is_dir() else 'gitfile'})")
    log(f"range  {args.revision} · mode {args.mode}")

    try:
        if args.mode == "walk":
            commits = list_commits(repo, args.revision, args.first_parent)
            log(f"walk   snapshotting {len(commits)} commit tree(s) with Path.rglob()")
            entries = walk_ledger(repo, commits, log=log)
        else:
            entries = collect_ledger(
                repo,
                args.revision,
                keep_renames=args.keep_renames,
                diff_merges=not args.no_diff_merges,
                first_parent=args.first_parent,
            )
    except RepoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not entries:
        log("nothing to report for that revision")

    summary = summarize(entries, top=args.top)
    verification = None
    if entries and not args.no_verify:
        try:
            verification = verify_against_disk(repo, entries)
        except OSError as exc:
            log(f"note: could not snapshot the working tree ({exc})")

    print(
        render_text(
            repo, args.revision, args.mode, entries, summary, verification, max_files=args.max_files, paint=paint
        )
    )

    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if args.json:
        write_json(
            args.json,
            {
                "repository": str(repo),
                "revision": args.revision,
                "mode": args.mode,
                "generated_at": generated,
                "summary": {
                    "commits": summary.commits,
                    "active_commits": summary.active_commits,
                    "merges": summary.merges,
                    "added": summary.added,
                    "removed": summary.removed,
                    "unique_added": summary.unique_added,
                    "unique_removed": summary.unique_removed,
                    "net": summary.net,
                    "span": list(summary.span),
                    "hot_directories": [list(x) for x in summary.hot_directories],
                    "churn": [list(x) for x in summary.churn],
                },
                "verification": verification.to_dict() if verification else None,
                "commits": [entry.to_dict() for entry in entries],
            },
        )
        log(f"wrote  {args.json}")
    if args.csv:
        write_csv(args.csv, entries)
        log(f"wrote  {args.csv}")
    if args.md:
        write_markdown(args.md, repo, args.revision, entries, summary)
        log(f"wrote  {args.md}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
