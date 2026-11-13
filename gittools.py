#!/data/data/com.termux/files/usr/bin/env python

import concurrent.futures
import csv
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Iterator, TextIO

import typer
from loguru import logger
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm
from rich.table import Table

console = Console()
err_console = Console(stderr=True)


def _init_logging(verbose: bool) -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG" if verbose else "INFO",
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | {message}",
        colorize=True,
    )


class OutputFormat(StrEnum):
    json = "json"
    jsonl = "jsonl"
    csv = "csv"
    markdown = "markdown"
    table = "table"


class ChangeMode(StrEnum):
    added = "added"
    modified = "modified"
    deleted = "deleted"
    changed = "changed"
    all = "all"


class CutMethod(StrEnum):
    squash = "squash"
    orphan = "orphan"


class CreateBackend(StrEnum):
    auto = "auto"
    gh = "gh"
    api = "api"


app = typer.Typer(no_args_is_help=True, rich_markup_mode="rich")
repo_app = typer.Typer(no_args_is_help=True)
history_app = typer.Typer(no_args_is_help=True)
stage_app = typer.Typer(no_args_is_help=True)
log_app = typer.Typer(no_args_is_help=True)
crypt_app = typer.Typer(no_args_is_help=True)
app.add_typer(repo_app, name="repo", help="Multi-repo operations")
app.add_typer(history_app, name="history", help="History rewriting / cleanup")
app.add_typer(stage_app, name="stage", help="Stage & commit pending changes")
app.add_typer(log_app, name="log", help="Inspect & export history")
app.add_typer(crypt_app, name="crypt", help="git filter integration")


@app.callback()
def _main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable debug logging")] = False,
) -> None:
    _init_logging(verbose)


def _git(
    *args: str,
    cwd: Path | None = None,
    check: bool = True,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        logger.error("git executable not found")
        raise typer.Exit(1) from exc
    except subprocess.TimeoutExpired as exc:
        logger.error("git command timed out: {}", " ".join(args))
        raise typer.Exit(1) from exc


def _is_repo(path: Path) -> bool:
    r = _git("rev-parse", "--is-inside-work-tree", cwd=path, check=False)
    return r.returncode == 0 and r.stdout.strip() == "true"


def _require_repo(path: Path) -> Path:
    path = path.resolve()
    if not _is_repo(path):
        logger.error("{} is not a git repository", path)
        raise typer.Exit(1)
    return path


def _current_branch(repo: Path) -> str | None:
    r = _git("symbolic-ref", "--short", "-q", "HEAD", cwd=repo, check=False)
    return r.stdout.strip() or None


def _is_dirty(repo: Path) -> bool:
    r = _git("status", "--porcelain", cwd=repo, check=False)
    return bool(r.stdout.strip())


def _cutoff(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _find_repos(root: Path) -> list[Path]:
    repos: list[Path] = []
    for dirpath, dirnames, _ in os.walk(root):
        p = Path(dirpath)
        if (p / ".git").is_dir():
            repos.append(p)
            dirnames[:] = []
    return repos


def _confirm(prompt: str, yes: bool) -> bool:
    if yes:
        return True
    return Confirm.ask(prompt, default=False, console=console)


@dataclass(slots=True)
class Commit:
    sha: str
    subject: str
    timestamp: int
    parents: list[str]

    @property
    def short(self) -> str:
        return self.sha[:8]

    @property
    def date(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp, tz=timezone.utc)


def _log_commits(repo: Path, ref: str = "HEAD") -> list[Commit]:
    out = _git("log", "--format=%H%x00%s%x00%ct%x00%P", ref, cwd=repo, check=False).stdout
    commits: list[Commit] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\x00")
        if len(parts) < 4:
            continue
        commits.append(
            Commit(
                sha=parts[0],
                subject=parts[1],
                timestamp=int(parts[2]),
                parents=parts[3].split() if parts[3] else [],
            )
        )
    return commits


def _deleted_files(repo: Path) -> dict[str, str]:
    out = _git("log", "--diff-filter=D", "--pretty=format:%H", "--name-only", cwd=repo, check=False).stdout
    deletions: dict[str, str] = {}
    current_sha: str | None = None
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        if len(line) == 40 and " " not in line:
            current_sha = line
        elif current_sha and line not in deletions:
            deletions[line] = current_sha
    return deletions


def _pending_historical_deletions(repo: Path) -> list[str]:
    historical = set(_deleted_files(repo).keys())
    if not historical:
        return []
    pending: list[str] = []
    for line in _git("status", "--porcelain", cwd=repo, check=False).stdout.splitlines():
        if len(line) < 4:
            continue
        code, path = line[:2], line[3:].strip()
        if "D" in code and path in historical:
            pending.append(path)
    return pending


def _path_ok(path: str, glob: str | None, exclude: str | None) -> bool:
    if glob and not fnmatch.fnmatch(path, glob):
        return False
    if exclude and fnmatch.fnmatch(path, exclude):
        return False
    return True


def _apply_mode(rec: CommitRecord, mode: ChangeMode) -> CommitRecord:
    match mode:
        case ChangeMode.added:
            rec.modified, rec.deleted, rec.renamed = [], [], []
        case ChangeMode.modified:
            rec.added, rec.deleted, rec.renamed = [], [], []
        case ChangeMode.deleted:
            rec.added, rec.modified, rec.renamed = [], [], []
        case ChangeMode.changed:
            rec.renamed = []
        case ChangeMode.all:
            pass
    return rec


@dataclass(slots=True)
class CommitRecord:
    sha: str
    short: str
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str
    authored_at: str
    committed_at: str
    message: str
    parents: list[str]
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    renamed: list[dict[str, str]] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)


CSV_FIELDS: list[str] = [
    "sha",
    "short",
    "author_name",
    "author_email",
    "committer_name",
    "committer_email",
    "authored_at",
    "committed_at",
    "message",
    "parents",
    "added",
    "modified",
    "deleted",
    "renamed",
    "files",
    "insertions",
    "deletions",
]


def _row(rec: CommitRecord) -> dict[str, Any]:
    return {
        "sha": rec.sha,
        "short": rec.short,
        "author_name": rec.author_name,
        "author_email": rec.author_email,
        "committer_name": rec.committer_name,
        "committer_email": rec.committer_email,
        "authored_at": rec.authored_at,
        "committed_at": rec.committed_at,
        "message": rec.message,
        "parents": ";".join(rec.parents),
        "added": ";".join(rec.added),
        "modified": ";".join(rec.modified),
        "deleted": ";".join(rec.deleted),
        "renamed": ";".join(f"{r['from']}->{r['to']}" for r in rec.renamed),
        "files": rec.stats.get("files", 0),
        "insertions": rec.stats.get("insertions", 0),
        "deletions": rec.stats.get("deletions", 0),
    }


def _write_json(records: Iterator[CommitRecord], out: TextIO) -> int:
    data = [asdict(r) for r in records]
    json.dump(data, out, indent=2, default=str)
    out.write("\n")
    return len(data)


def _write_jsonl(records: Iterator[CommitRecord], out: TextIO) -> int:
    n = 0
    for r in records:
        out.write(json.dumps(asdict(r), default=str) + "\n")
        n += 1
    return n


def _write_csv(records: Iterator[CommitRecord], out: TextIO) -> int:
    writer = csv.DictWriter(out, fieldnames=CSV_FIELDS)
    writer.writeheader()
    n = 0
    for r in records:
        writer.writerow(_row(r))
        n += 1
    return n


def _write_markdown(records: Iterator[CommitRecord], out: TextIO) -> int:
    cols = ["short", "author_name", "authored_at", "added", "modified", "deleted", "renamed"]
    out.write("| " + " | ".join(cols) + " |\n")
    out.write("| " + " | ".join("---" for _ in cols) + " |\n")
    n = 0
    for r in records:
        row = _row(r)
        cells = [str(row[c]).replace("|", "\\|") for c in cols]
        out.write("| " + " | ".join(cells) + " |\n")
        n += 1
    return n


def _write_table(records: Iterator[CommitRecord], out: TextIO) -> int:
    rows = [_row(r) for r in records]
    if not rows:
        return 0
    table = Table(show_header=True, header_style="bold")
    for c in CSV_FIELDS:
        table.add_column(c)
    for row in rows:
        table.add_row(*[str(row[c]) for c in CSV_FIELDS])
    Console(file=out, force_terminal=False, width=200).print(table)
    return len(rows)


WRITERS: dict[str, Any] = {
    "json": _write_json,
    "jsonl": _write_jsonl,
    "csv": _write_csv,
    "markdown": _write_markdown,
    "table": _write_table,
}


def _iter_git_log(
    repo: Path,
    *,
    ref: str = "HEAD",
    since: str | None = None,
    until: str | None = None,
    author: str | None = None,
    max_count: int | None = None,
    first_parent: bool = False,
    path_glob: str | None = None,
    exclude_glob: str | None = None,
    mode: ChangeMode = ChangeMode.changed,
    rename: bool = False,
) -> Iterator[CommitRecord]:
    args = [
        "git",
        "-C",
        str(repo),
        "log",
        "--format=%x1e%H%x1f%h%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%aI%x1f%cI%x1f%s%x1f%P",
        "--name-status",
        "-z",
    ]
    args.append("-M" if rename else "--no-renames")
    if first_parent:
        args.append("--first-parent")
    if since:
        args.append(f"--since={since}")
    if until:
        args.append(f"--until={until}")
    if author:
        args.append(f"--author={author}")
    if max_count:
        args.append(f"-n{max_count}")
    args.append(ref)

    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        if "does not have any commits" in proc.stderr:
            return
        logger.error("git log failed: {}", proc.stderr.strip())
        raise typer.Exit(1)

    for chunk in proc.stdout.split("\x1e")[1:]:
        nl = chunk.find("\n")
        if nl < 0:
            continue
        header = chunk[:nl].split("\x1f")
        if len(header) < 10:
            continue
        sha, short, an, ae, cn, ce, ai, ci, msg, parents_str = header[:10]
        parents = parents_str.split() if parents_str.strip() else []
        tokens = chunk[nl + 1 :].split("\0")
        added, modified, deleted, renamed = [], [], [], []
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if not tok:
                i += 1
                continue
            code = tok[0]
            if code in ("R", "C"):
                if i + 2 >= len(tokens):
                    break
                old, new = tokens[i + 1], tokens[i + 2]
                i += 3
                if not _path_ok(new, path_glob, exclude_glob):
                    continue
                if code == "R":
                    renamed.append({"from": old, "to": new})
                else:
                    added.append(new)
            else:
                if i + 1 >= len(tokens):
                    break
                path = tokens[i + 1]
                i += 2
                if not _path_ok(path, path_glob, exclude_glob):
                    continue
                if code == "A":
                    added.append(path)
                elif code == "D":
                    deleted.append(path)
                elif code in ("M", "T"):
                    modified.append(path)
        rec = CommitRecord(
            sha=sha,
            short=short,
            author_name=an,
            author_email=ae,
            committer_name=cn,
            committer_email=ce,
            authored_at=ai,
            committed_at=ci,
            message=msg.strip(),
            parents=parents,
            added=sorted(added),
            modified=sorted(modified),
            deleted=sorted(deleted),
            renamed=renamed,
            stats={},
        )
        yield _apply_mode(rec, mode)


def _gh_available() -> bool:
    return shutil.which("gh") is not None


def _gh_repo_create(name: str, private: bool, description: str | None, branch: str) -> None:
    cwd = Path.cwd().resolve()
    if not (cwd / ".git").is_dir():
        _git("init", "-b", branch, cwd=cwd)
    _git("add", "-A", cwd=cwd)
    if _git("status", "--porcelain", cwd=cwd).stdout.strip():
        _git("commit", "-m", "Initial commit", cwd=cwd)
    _git("branch", "-M", branch, cwd=cwd)
    args = ["gh", "repo", "create", name, "--source", ".", "--remote", "origin", "--push"]
    args.append("--private" if private else "--public")
    if description:
        args.extend(["--description", description])
    r = subprocess.run(args, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        logger.error("gh repo create failed: {}", r.stderr.strip())
        raise typer.Exit(1)
    console.print(f"[green]✓[/green] {r.stdout.strip()}")


def _github_api_request(token: str, method: str, url: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "gitool/1.0")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read().decode()
            return json.loads(content) if content else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        try:
            msg = json.loads(body).get("message", body)
        except Exception:
            msg = body
        raise RuntimeError(f"GitHub API error {exc.code}: {msg}") from exc


def _api_repo_create(token: str, name: str, private: bool, description: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": name, "private": private, "auto_init": False}
    if description:
        payload["description"] = description
    try:
        return _github_api_request(token, "POST", "https://api.github.com/user/repos", payload)
    except RuntimeError as exc:
        if "422" not in str(exc):
            raise
        user = _github_api_request(token, "GET", "https://api.github.com/user")
        login = user.get("login", "")
        return {
            "clone_url": f"https://github.com/{login}/{name}.git",
            "ssh_url": f"git@github.com:{login}/{name}.git",
        }


@repo_app.command("scan")
def repo_scan(
    path: Annotated[Path, typer.Argument(help="Root directory to scan")] = Path("."),
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
    check: Annotated[bool, typer.Option("--check", help="Exit 1 if any repo is dirty")] = False,
) -> None:
    root = path.resolve()
    repos = _find_repos(root)
    if not repos:
        console.print("[yellow]No git repositories found.[/yellow]")
        return

    def probe(repo: Path) -> tuple[Path, bool, str]:
        try:
            r = _git("status", "--porcelain", cwd=repo, check=False, timeout=10)
            if r.returncode != 0:
                return repo, False, f"git error: {r.stderr.strip()}"
            lines = [line for line in r.stdout.splitlines() if line.strip()]
            if not lines:
                return repo, False, ""
            detail = ", ".join(lines[:5])
            if len(lines) > 5:
                detail += f" (+{len(lines) - 5} more)"
            return repo, True, detail
        except Exception as exc:
            return repo, False, f"error: {exc}"

    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as prog:
        task = prog.add_task(f"Scanning {len(repos)} repo(s)...", total=len(repos))
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(repos))) as pool:
            futures = [pool.submit(probe, r) for r in repos]
            results = []
            for fut in concurrent.futures.as_completed(futures):
                results.append(fut.result())
                prog.advance(task)

    dirty = [r for r in results if r[1]]

    if json_out:
        console.print_json(
            json.dumps([
                {"repo": str(p.relative_to(root)) if p != root else ".", "dirty": d, "detail": det}
                for p, d, det in results
            ])
        )
    else:
        for p, d, detail in results:
            rel = p.relative_to(root) if p != root else Path(".")
            if d:
                console.print(f"[red]✗[/red] [bold]{rel}[/bold]")
                console.print(f"  └─ {detail}")
            else:
                console.print(f"[green]✓[/green] {rel}")

    if dirty:
        console.print(f"\n[yellow]⚠[/yellow] {len(dirty)} dirty repo(s) / {len(results)} total")
        if check:
            raise typer.Exit(1)
    else:
        console.print(f"\n[green]✓[/green] All {len(results)} repo(s) clean")


@repo_app.command("pull")
def repo_pull(
    path: Annotated[Path, typer.Argument(help="Root directory")] = Path("."),
    timeout: Annotated[int, typer.Option("--timeout", help="Per-repo timeout (s)")] = 120,
) -> None:
    root = path.resolve()
    repos = _find_repos(root)
    if not repos:
        console.print("[yellow]No repositories found.[/yellow]")
        return

    def pull(repo: Path) -> tuple[Path, bool, str]:
        try:
            branch = _current_branch(repo)
            if not branch:
                return repo, False, "detached HEAD"
            remotes = _git("remote", cwd=repo, check=False).stdout.split()
            if not remotes:
                return repo, False, "no remote"
            _git("pull", remotes[0], branch, cwd=repo, check=True, timeout=timeout)
            return repo, True, "ok"
        except subprocess.CalledProcessError as exc:
            return repo, False, (exc.stderr or "").strip() or "pull failed"
        except Exception as exc:
            return repo, False, str(exc)

    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as prog:
        task = prog.add_task(f"Pulling {len(repos)} repo(s)...", total=len(repos))
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(repos))) as pool:
            futures = [pool.submit(pull, r) for r in repos]
            results = []
            for fut in concurrent.futures.as_completed(futures):
                results.append(fut.result())
                prog.advance(task)

    ok = sum(1 for _, s, _ in results if s)
    for repo, success, msg in results:
        rel = repo.relative_to(root) if repo != root else Path(".")
        if success:
            console.print(f"[green]✓[/green] {rel}")
        else:
            console.print(f"[red]✗[/red] {rel}: {msg}")
    console.print(f"\n[green]{ok} pulled[/green], [red]{len(results) - ok} failed[/red]")


@repo_app.command("create")
def repo_create(
    name: Annotated[str | None, typer.Option("--name", "-n", help="Repository name")] = None,
    private: Annotated[bool, typer.Option("--private/--public")] = False,
    description: Annotated[str | None, typer.Option("--description", "-d")] = None,
    branch: Annotated[str, typer.Option("--branch", "-b")] = "main",
    message: Annotated[str, typer.Option("-m", "--message")] = "Initial commit",
    ssh: Annotated[bool, typer.Option("--ssh", help="Use SSH remote URL")] = False,
    backend: Annotated[CreateBackend, typer.Option("--backend", help="auto|gh|api")] = CreateBackend.auto,
) -> None:
    cwd = Path.cwd().resolve()
    repo_name = name or cwd.name

    if backend in (CreateBackend.auto, CreateBackend.gh) and _gh_available():
        _gh_repo_create(repo_name, private, description, branch)
        return

    if backend == CreateBackend.gh:
        logger.error("gh CLI is not available")
        raise typer.Exit(1)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        logger.error("GITHUB_TOKEN / GH_TOKEN not set and gh CLI unavailable")
        raise typer.Exit(1)

    info = _api_repo_create(token, repo_name, private, description)
    url = info.get("ssh_url" if ssh else "clone_url") or info.get("clone_url")
    if not url:
        logger.error("Could not determine remote URL")
        raise typer.Exit(1)

    if not (cwd / ".git").is_dir():
        _git("init", "-b", branch, cwd=cwd)
        logger.info("Initialized git repository in {}", cwd)

    local_ignore = cwd / ".gitignore"
    global_ignore = Path.home() / ".gitignore"
    if not local_ignore.exists() and global_ignore.exists():
        shutil.copy2(global_ignore, local_ignore)
        logger.info("Copied global .gitignore")

    _git("add", "-A", cwd=cwd)
    if _git("status", "--porcelain", cwd=cwd).stdout.strip():
        _git("commit", "-m", message, cwd=cwd)
    _git("branch", "-M", branch, cwd=cwd)

    remotes = _git("remote", cwd=cwd, check=False).stdout.split()
    if "origin" in remotes:
        _git("remote", "set-url", "origin", url, cwd=cwd)
    else:
        _git("remote", "add", "origin", url, cwd=cwd)

    _git("push", "-u", "origin", branch, cwd=cwd)
    console.print(f"[green]✓[/green] Created and pushed → [link={url}]{url}[/link]")


@repo_app.command("ignore")
def repo_ignore(
    line: Annotated[str, typer.Argument(help="Line to append to .gitignore")],
    paths: Annotated[list[Path] | None, typer.Argument(help="Files or directories to scan")] = None,
) -> None:
    needle = line.strip()
    if not needle:
        logger.error("line cannot be empty")
        raise typer.Exit(1)
    targets = [p.resolve() for p in (paths or [Path(".")])]
    gi_files: list[Path] = []
    for t in targets:
        if t.is_file() and t.name == ".gitignore":
            gi_files.append(t)
        elif t.is_dir():
            gi_files.extend(f for f in t.rglob(".gitignore") if f.is_file())

    changed = 0
    for gi in gi_files:
        try:
            content = gi.read_text(encoding="utf-8", errors="ignore")
            if needle in content.splitlines():
                continue
            if content and not content.endswith("\n"):
                content += "\n"
            content += needle + "\n"
            gi.write_text(content, encoding="utf-8")
            changed += 1
            console.print(f"[green]✓[/green] {gi}")
        except Exception as exc:
            logger.error("failed to update {}: {}", gi, exc)
    console.print(f"Updated {changed} file(s)")


@history_app.command("checkout-previous")
def history_checkout_previous(
    root: Annotated[Path, typer.Argument(help="Directory containing repositories")] = Path("."),
    prefix: Annotated[str, typer.Option("--prefix", "-p", help="Latest commit subject prefix")] = "2026-08-29",
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    root = root.resolve()
    if not root.is_dir():
        logger.error("not a directory: {}", root)
        raise typer.Exit(1)
    candidates = [root, *[p for p in root.iterdir() if p.is_dir() and not p.is_symlink()]]
    found = False
    for candidate in candidates:
        if not _is_repo(candidate):
            continue
        found = True
        commits = _log_commits(candidate, "HEAD")
        if not commits:
            continue
        head = commits[0]
        if not head.subject.startswith(prefix):
            console.print(f"[dim]skip[/dim] {candidate.name}: subject doesn't start with {prefix!r}")
            continue
        if not head.parents:
            console.print(f"[dim]skip[/dim] {candidate.name}: no parent commit")
            continue
        target = head.parents[0]
        console.print(
            Panel.fit(
                f"[bold]Repository:[/bold] {candidate.name}\n"
                f"[bold]Path:[/bold]       {candidate}\n"
                f"[bold]Current:[/bold]    {head.short} {head.subject}\n"
                f"[bold]Target:[/bold]     {target[:8]}",
                title="match",
                border_style="cyan",
            )
        )
        if not _confirm(f"Checkout {target[:8]} in {candidate.name}?", yes):
            console.print(f"[dim]skip[/dim] {candidate.name}: cancelled")
            continue
        _git("checkout", target, cwd=candidate)
        console.print(f"[green]✓[/green] {candidate.name} → {target[:8]}")
    if not found:
        console.print("[yellow]No git repositories found.[/yellow]")


@history_app.command("prune")
def history_prune(
    repo: Annotated[Path, typer.Option("--repo", "-C")] = Path("."),
    days: Annotated[int, typer.Option("--days", help="Drop commits older than N days")] = 30,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview only")] = False,
) -> None:
    repo = _require_repo(repo)
    branch = _current_branch(repo)
    if branch is None:
        logger.error("HEAD is detached — checkout a branch first")
        raise typer.Exit(1)
    if _is_dirty(repo):
        logger.error("working directory is not clean")
        raise typer.Exit(1)

    cutoff = _cutoff(days)
    commits = _log_commits(repo, branch)
    if not commits:
        console.print("[yellow]No commits found.[/yellow]")
        return
    keep = [c for c in commits if c.date > cutoff]
    old = [c for c in commits if c.date <= cutoff]
    if not old:
        console.print(f"[green]No commits older than {days} days.[/green]")
        return
    if not keep:
        logger.error("all commits would be deleted — aborting")
        raise typer.Exit(1)

    console.print(f"Branch: [bold]{branch}[/bold]")
    console.print(f"Cutoff: {cutoff:%Y-%m-%d %H:%M:%S} UTC")
    console.print(f"Deleting [red]{len(old)}[/red], keeping [green]{len(keep)}[/green]")
    console.print(f"New HEAD → [bold]{keep[0].short}[/bold] {keep[0].subject}")

    if dry_run:
        console.print("[cyan]dry-run: no changes made[/cyan]")
        return
    if not _confirm("This will PERMANENTLY DELETE those commits. Continue?", yes):
        console.print("Cancelled.")
        return

    backup = f"backup-{branch}-{datetime.now():%Y%m%d%H%M%S}"
    _git("branch", backup, cwd=repo)
    _git("reset", "--hard", keep[0].sha, cwd=repo)
    console.print(
        Panel.fit(
            f"[green]✓[/green] Removed {len(old)} commit(s)\n"
            f"Backup branch: [bold]{backup}[/bold]\n"
            f"Restore: git reset --hard {backup}",
            border_style="green",
        )
    )


@history_app.command("cut")
def history_cut(
    repo: Annotated[Path, typer.Option("--repo", "-C")] = Path("."),
    days: Annotated[int, typer.Option("--days", help="Drop commits older than N days")] = 30,
    method: Annotated[CutMethod, typer.Option("--method")] = CutMethod.squash,
    backup: Annotated[bool, typer.Option("--backup", help="Create backup branch")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    repo = _require_repo(repo)
    cutoff = _cutoff(days)
    commits = _log_commits(repo, "HEAD")
    if not commits:
        console.print("[yellow]No commits found.[/yellow]")
        return
    keep = [c for c in commits if c.date > cutoff]
    old = [c for c in commits if c.date <= cutoff]
    console.print(f"Cutoff:       {cutoff:%Y-%m-%d %H:%M:%S} UTC")
    console.print(f"Keep:         [green]{len(keep)}[/green]")
    console.print(f"Remove:       [red]{len(old)}[/red]")
    if not keep:
        logger.error("no commits to keep — aborting")
        raise typer.Exit(1)
    if not old:
        console.print("[green]No old commits to remove.[/green]")
        return

    oldest_kept = keep[-1]
    console.print(f"Oldest kept: [bold]{oldest_kept.short}[/bold] {oldest_kept.subject}")
    if dry_run:
        console.print("[cyan]dry-run: no changes made[/cyan]")
        return
    if not _confirm("Rewrite history?", yes):
        console.print("Cancelled.")
        return

    branch = _current_branch(repo)
    if backup and branch:
        bname = f"backup-{branch}-{datetime.now():%Y%m%d%H%M%S}"
        _git("branch", bname, cwd=repo)
        console.print(f"[green]✓[/green] Backup branch: {bname}")

    if method == CutMethod.squash:
        _git("reset", "--hard", oldest_kept.sha, cwd=repo)
        console.print(f"[green]✓[/green] Reset to {oldest_kept.short}")
        if branch:
            console.print(f"Force-push with: git push --force origin {branch}")
        return

    if branch is None:
        logger.error("cannot use orphan method on detached HEAD")
        raise typer.Exit(1)
    new_branch = f"cleaned_{branch}"
    _git("checkout", "--orphan", new_branch, cwd=repo)
    failed = 0
    for c in reversed(keep):
        r = _git("cherry-pick", "--allow-empty", c.sha, cwd=repo, check=False)
        if r.returncode != 0:
            logger.warning("cherry-pick {} failed: {}", c.short, r.stderr.strip())
            failed += 1
    console.print(
        Panel.fit(
            f"[green]✓[/green] Created orphan branch [bold]{new_branch}[/bold] "
            f"with {len(keep) - failed} commit(s) ({failed} failed)\n\n"
            f"To replace original:\n"
            f"  git checkout {branch}\n"
            f"  git reset --hard {new_branch}\n"
            f"  git branch -D {new_branch}",
            border_style="green",
        )
    )


@history_app.command("remote-prune")
def history_remote_prune(
    repo: Annotated[Path, typer.Option("--repo", "-C")] = Path("."),
    branch: Annotated[str, typer.Option("--branch", help="Branch to clean")] = "master",
    days: Annotated[int, typer.Option("--days", help="Keep commits within N days")] = 7,
    no_push: Annotated[bool, typer.Option("--no-push", help="Only reset locally")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    repo = _require_repo(repo)
    refs = _git("for-each-ref", "--format=%(refname:short)", "refs/remotes/origin", cwd=repo, check=False).stdout
    if branch == "master" and "origin/main" in refs and "origin/master" not in refs:
        branch = "main"

    cutoff = _cutoff(days)
    commits = _log_commits(repo, branch)
    if not commits:
        logger.error("no commits on {}", branch)
        raise typer.Exit(1)
    keep = [c for c in commits if c.date >= cutoff]
    if not keep:
        logger.error("no commits within {} days — aborting", days)
        raise typer.Exit(1)
    new_head = keep[0]
    console.print(f"Branch:   [bold]{branch}[/bold]")
    console.print(f"Cutoff:   {cutoff:%Y-%m-%d %H:%M:%S} UTC")
    console.print(f"New HEAD: [bold]{new_head.short}[/bold] {new_head.subject}")
    if dry_run:
        console.print("[cyan]dry-run: no changes made[/cyan]")
        return
    if not _confirm("Proceed with local reset and force-push?", yes):
        console.print("Cancelled.")
        return

    if _current_branch(repo) != branch:
        _git("checkout", branch, cwd=repo)
    _git("reset", "--hard", new_head.sha, cwd=repo)
    console.print(f"[green]✓[/green] Local {branch} reset to {new_head.short}")

    if no_push:
        console.print("[yellow]Skipping push (--no-push).[/yellow]")
        return
    r = _git("push", "--force", "origin", f"{branch}:{branch}", cwd=repo, check=False)
    if r.returncode != 0:
        logger.error("push failed: {}", r.stderr.strip())
        raise typer.Exit(1)
    console.print(f"[green]✓[/green] Force-pushed {branch} to origin")


@history_app.command("restore")
def history_restore(
    repo: Annotated[Path, typer.Option("--repo", "-C")] = Path("."),
    message: Annotated[str, typer.Option("-m", "--message")] = "removed files",
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
) -> None:
    repo = _require_repo(repo)
    logger.info("analyzing history for deleted files...")
    deletions = _deleted_files(repo)
    if not deletions:
        console.print("[green]🎉 No deleted files found in history.[/green]")
        return

    missing = [(p, sha) for p, sha in deletions.items() if not (repo / p).exists()]
    if not missing:
        console.print("[green]ℹ️  All historically deleted files are present.[/green]")
        return

    table = Table(title=f"Found {len(missing)} missing file(s)", title_style="bold yellow")
    table.add_column("File")
    table.add_column("Deleted in")
    for p, sha in missing:
        table.add_row(p, sha[:8])
    console.print(table)

    if dry_run:
        console.print("[cyan]dry-run: no changes made[/cyan]")
        return
    if not _confirm("Restore these files and create a commit?", yes):
        console.print("Cancelled.")
        return

    restored = 0
    for p, sha in missing:
        r = _git("checkout", f"{sha}^", "--", p, cwd=repo, check=False)
        if r.returncode != 0:
            r = _git("checkout", sha, "--", p, cwd=repo, check=False)
        if r.returncode == 0:
            _git("add", "--", p, cwd=repo)
            restored += 1
            console.print(f"[green]✓[/green] {p}")
        else:
            logger.error("could not restore {}: {}", p, r.stderr.strip())

    if restored:
        _git("commit", "-m", message, cwd=repo)
        console.print(f"[green]✓[/green] Committed {restored} restored file(s)")
    else:
        logger.error("no files restored")


@stage_app.command("deleted")
def stage_deleted(
    repo: Annotated[Path, typer.Option("--repo", "-C")] = Path("."),
    message: Annotated[str, typer.Option("-m", "--message")] = "removed files",
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    repo = _require_repo(repo)
    pending = _pending_historical_deletions(repo)
    if not pending:
        console.print("[green]🎉 No pending historical deletions need staging.[/green]")
        return
    for p in pending:
        console.print(f"  [yellow]D[/yellow] {p}")
    if dry_run:
        console.print("[cyan]dry-run: no changes made[/cyan]")
        return
    for p in pending:
        _git("add", "--", p, cwd=repo)
    _git("commit", "-m", message, cwd=repo)
    console.print(f"[green]✓[/green] Committed {len(pending)} deletion(s): {message!r}")


@stage_app.command("squash")
def stage_squash(
    repo: Annotated[Path, typer.Option("--repo", "-C")] = Path("."),
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    repo = _require_repo(repo)
    pending = _pending_historical_deletions(repo)
    if not pending:
        console.print("[green]🎉 No pending historical deletions to squash.[/green]")
        return
    for p in pending:
        console.print(f"  [yellow]D[/yellow] {p}")
    if dry_run:
        console.print("[cyan]dry-run: no changes made[/cyan]")
        return
    for p in pending:
        _git("add", "--", p, cwd=repo)
    _git("commit", "--amend", "--no-edit", cwd=repo)
    console.print(f"[green]✓[/green] Amended last commit with {len(pending)} deletion(s)")


@log_app.command("added")
def log_added(
    repo: Annotated[Path, typer.Option("--repo", "-C")] = Path("."),
    n: Annotated[int, typer.Option("-n", help="Commits to look back")] = 10,
) -> None:
    repo = _require_repo(repo)
    out = _git(
        "log",
        f"-n{n}",
        "--pretty=format:",
        "--name-status",
        "--diff-filter=A",
        cwd=repo,
        check=False,
    ).stdout
    files: list[Path] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            p = repo / parts[1]
            if not p.is_symlink():
                files.append(p)
    if not files:
        console.print(f"[yellow]No files added in last {n} commits.[/yellow]")
        return
    for f in files:
        try:
            print(f.relative_to(repo))
        except ValueError:
            print(f)


@log_app.command("export")
def log_export(
    repo: Annotated[Path, typer.Option("--repo", "-C")] = Path("."),
    output: Annotated[Path | None, typer.Option("-o", "--output", help="Output file (- for stdout)")] = None,
    fmt: Annotated[OutputFormat, typer.Option("--format", "-f")] = OutputFormat.json,
    ref: Annotated[str, typer.Option("--ref", help="Ref to walk")] = "HEAD",
    since: Annotated[str | None, typer.Option("--since", help="ISO 8601 or git-style")] = None,
    until: Annotated[str | None, typer.Option("--until")] = None,
    author: Annotated[str | None, typer.Option("--author")] = None,
    max_count: Annotated[int | None, typer.Option("--max-count")] = None,
    first_parent: Annotated[bool, typer.Option("--first-parent")] = False,
    path_glob: Annotated[str | None, typer.Option("--path", help="Glob filter")] = None,
    exclude: Annotated[str | None, typer.Option("--exclude", help="Glob exclusion")] = None,
    mode: Annotated[ChangeMode, typer.Option("--mode")] = ChangeMode.changed,
    rename: Annotated[bool, typer.Option("--rename", help="Enable rename detection")] = False,
) -> None:
    repo = _require_repo(repo)
    records = _iter_git_log(
        repo,
        ref=ref,
        since=since,
        until=until,
        author=author,
        max_count=max_count,
        first_parent=first_parent,
        path_glob=path_glob,
        exclude_glob=exclude,
        mode=mode,
        rename=rename,
    )
    writer = WRITERS[fmt]
    if output in (None, Path("-")):
        n = writer(records, sys.stdout)
    else:
        with output.open("w", newline="", encoding="utf-8") as fh:
            n = writer(records, fh)
    logger.info("wrote {} commit(s) as {}", n, fmt.value)


@crypt_app.command("age-filter")
def crypt_age_filter(
    mode: Annotated[str, typer.Argument(help="clean (encrypt) or smudge (decrypt)")],
    age_bin: Annotated[str | None, typer.Option("--age-bin", help="Path to age binary")] = None,
    public_key: Annotated[Path, typer.Option("--public-key")] = Path.home() / ".config" / "age" / "public.key",
    private_key: Annotated[Path, typer.Option("--private-key")] = Path.home() / ".config" / "age" / "keys.txt",
) -> None:
    if mode not in ("clean", "smudge"):
        logger.error("mode must be 'clean' or 'smudge'")
        raise typer.Exit(1)
    bin_path = age_bin or ("age" if shutil.which("age") else str(Path.home() / ".." / "usr" / "bin" / "age"))
    data = sys.stdin.buffer.read()

    if mode == "clean":
        pk = public_key.expanduser()
        if not pk.exists():
            logger.error("missing public key file: {}", pk)
            raise typer.Exit(1)
        recipient = pk.read_text(encoding="utf-8").strip()
        if not recipient:
            logger.error("public key file is empty")
            raise typer.Exit(1)
        r = subprocess.run([bin_path, "-r", recipient, "-a"], input=data, capture_output=True)
        if r.returncode != 0:
            logger.error("age encrypt failed: {}", r.stderr.decode(errors="replace"))
            raise typer.Exit(1)
        sys.stdout.buffer.write(r.stdout)
        return

    if not data.lstrip().startswith(b"-----BEGIN AGE ENCRYPTED FILE-----"):
        sys.stdout.buffer.write(data)
        return
    priv = private_key.expanduser()
    if not priv.exists():
        logger.warning("no private key; leaving ciphertext")
        sys.stdout.buffer.write(data)
        return
    r = subprocess.run([bin_path, "-d", "-i", str(priv)], input=data, capture_output=True)
    if r.returncode != 0:
        logger.error("age decrypt failed: {}", r.stderr.decode(errors="replace"))
        raise typer.Exit(1)
    sys.stdout.buffer.write(r.stdout)


@app.command("version")
def version_cmd() -> None:
    console.print("[bold]gitool[/bold] 1.0.0")


if __name__ == "__main__":
    try:
        app()
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(130)
