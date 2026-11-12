#!/data/data/com.termux/files/usr/bin/env python
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal, NoReturn, Optional

import typer
from loguru import logger
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

__version__ = "1.0.0"

DEFAULT_ENV_FILE = Path.home() / ".env"
DEFAULT_GLOBAL_GITIGNORE = Path.home() / ".gitignore"
DEFAULT_USERNAME = "i4147"
DEFAULT_GIT_USER = "i4147"
DEFAULT_GIT_EMAIL = "yesnacoc@gmail.com"
DEFAULT_BRANCH = "main"
DEFAULT_DESCRIPTION = "created with python"

LOG_DIR = Path.home() / "tmp" / "apps" / "ghrepo"
LOG_DIR.mkdir(parents=True, exist_ok=True)
logger.remove()
logger.add(
    LOG_DIR / "ghrepo.log",
    rotation="1 MB",
    retention="7 days",
    level="ERROR",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
)

console = Console()
app = typer.Typer(rich_markup_mode="rich", no_args_is_help=True, add_completion=False)

TokenOpt = Annotated[Optional[str], typer.Option("--token", envvar="GITHUB_TOKEN", help="GitHub API token")]
EnvFileOpt = Annotated[Path, typer.Option("--env-file", help="Env file path")]
UsernameOpt = Annotated[str, typer.Option("--username", help="GitHub username")]


def _run(cmd: list[str], cwd: Optional[Path] = None, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, text=True, capture_output=True, check=check)


def _git(args: list[str], cwd: Path, check: bool = False) -> subprocess.CompletedProcess[str]:
    return _run(["git", *args], cwd=cwd, check=check)


def _gh_available() -> bool:
    return shutil.which("gh") is not None


def _gh_authed() -> bool:
    return _gh_available() and _run(["gh", "auth", "status"]).returncode == 0


def _is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def _init_repo(path: Path) -> None:
    _git(["init"], path)


def _config_user(path: Path, user: str, email: str) -> None:
    _git(["config", "user.name", user], path)
    _git(["config", "user.email", email], path)


def _stage_all(path: Path) -> None:
    _git(["add", "-A"], path)


def _commit(path: Path, message: str) -> bool:
    if not _git(["status", "--porcelain"], path).stdout.strip():
        return False
    return _git(["commit", "-m", message], path).returncode == 0


def _current_branch(path: Path) -> str:
    return _git(["branch", "--show-current"], path).stdout.strip() or "main"


def _rename_branch(path: Path, new_name: str) -> None:
    _git(["branch", "-M", new_name], path)


def _get_remote(path: Path, name: str) -> Optional[str]:
    r = _git(["remote", "get-url", name], path)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def _set_or_add_remote(path: Path, name: str, url: str) -> None:
    existing = _get_remote(path, name)
    if existing is None:
        _git(["remote", "add", name, url], path)
    elif existing != url:
        _git(["remote", "set-url", name, url], path)


def _push(path: Path, remote: str, branch: str) -> tuple[bool, str]:
    r = _git(["push", "-u", remote, branch], path)
    return r.returncode == 0, (r.stderr or "") + (r.stdout or "")


def _pull_rebase(path: Path, remote: str, branch: str) -> None:
    _git(["pull", remote, branch, "--rebase"], path)


def _copy_gitignore(cwd: Path, source: Path) -> bool:
    target = cwd / ".gitignore"
    if source.exists() and not target.exists():
        shutil.copy2(source, target)
        return True
    return False


def _ensure_readme(cwd: Path, name: str) -> bool:
    if any(p.name not in {".git", ".gitignore"} for p in cwd.iterdir()):
        return False
    readme = cwd / "README.md"
    if readme.exists():
        return False
    readme.write_text(f"# {name}\nRepository initialized on {datetime.now():%Y-%m-%d %H:%M:%S}\n")
    return True


def _load_token_file(env_file: Path) -> Optional[str]:
    if not env_file.exists():
        return None
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip() == "GITHUB_TOKEN":
            return val.strip().strip('"').strip("'")
    return None


class GitHubClient:
    def __init__(self, token: Optional[str], username: str) -> None:
        self.token = token
        self.username = username

    def repo_exists(self, name: str) -> bool:
        if _gh_authed():
            return _run(["gh", "repo", "view", f"{self.username}/{name}"]).returncode == 0
        if self.token:
            status, _ = self._rest("GET", f"/repos/{self.username}/{name}", None)
            return status == 200
        return False

    def create(
        self,
        name: str,
        description: str = "",
        private: bool = False,
        auto_init: bool = False,
    ) -> dict[str, Any]:
        if _gh_authed():
            try:
                return self._create_gh(name, description, private, auto_init)
            except RuntimeError as exc:
                logger.warning(f"gh create failed, falling back to REST: {exc}")
        if self.token:
            return self._create_rest(name, description, private, auto_init)
        raise RuntimeError("No GitHub auth available: run `gh auth login` or provide GITHUB_TOKEN")

    def _create_gh(self, name: str, description: str, private: bool, auto_init: bool) -> dict[str, Any]:
        cmd = [
            "gh",
            "repo",
            "create",
            name,
            "--source",
            ".",
            "--remote",
            "origin",
            "--private" if private else "--public",
        ]
        if description:
            cmd.extend(["--description", description])
        if auto_init:
            cmd.append("--add-readme")
        r = _run(cmd)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or "gh repo create failed")
        return self._default_payload(name)

    def _create_rest(self, name: str, description: str, private: bool, auto_init: bool) -> dict[str, Any]:
        payload = {"name": name, "description": description, "private": private, "auto_init": auto_init}
        status, data = self._rest("POST", "/user/repos", payload)
        if status == 201:
            return data
        if status == 422:
            return self._default_payload(name)
        raise RuntimeError(f"GitHub API {status}: {data.get('message', 'unknown')}")

    def _default_payload(self, name: str) -> dict[str, Any]:
        base = f"https://github.com/{self.username}/{name}"
        return {
            "name": name,
            "html_url": base,
            "clone_url": f"{base}.git",
            "ssh_url": f"git@github.com:{self.username}/{name}.git",
        }

    def _rest(self, method: str, path: str, payload: Optional[dict[str, Any]]) -> tuple[int, Any]:
        url = f"https://api.github.com{path}"
        data = json.dumps(payload).encode() if payload else None
        req = urllib.request.Request(url, method=method, data=data)
        req.add_header("Authorization", f"token {self.token}")
        req.add_header("Accept", "application/vnd.github.v3+json")
        if payload:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = {"message": body}
            return exc.code, parsed


def _build_client(token: Optional[str], env_file: Path, username: str) -> GitHubClient:
    resolved = token or os.environ.get("GITHUB_TOKEN") or _load_token_file(env_file)
    return GitHubClient(resolved, username)


def _fail(msg: str, exc: Optional[Exception] = None) -> NoReturn:
    logger.error(f"{msg}: {exc}" if exc is not None else msg)
    console.print(f"[red]✗ {msg}[/red]")
    raise typer.Exit(1)


def _version_cb(value: bool) -> None:
    if value:
        console.print(f"ghrepo [bold cyan]{__version__}[/bold cyan]")
        raise typer.Exit()


@app.callback()
def _main(
    version: Annotated[
        bool, typer.Option("--version", callback=_version_cb, is_eager=True, help="Show version")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable debug logging to stderr")] = False,
) -> None:
    if verbose:
        logger.add(sys.stderr, level="DEBUG", format="<level>{level}</level> | {message}")


@app.command()
def create(
    name: Annotated[str, typer.Argument(help="Repository name")],
    description: Annotated[str, typer.Option("-d", "--description", help="Repo description")] = DEFAULT_DESCRIPTION,
    private: Annotated[bool, typer.Option("--private/--public", help="Visibility")] = False,
    auto_init: Annotated[bool, typer.Option("--auto-init", help="Initialize with README")] = False,
    token: TokenOpt = None,
    env_file: EnvFileOpt = DEFAULT_ENV_FILE,
    username: UsernameOpt = DEFAULT_USERNAME,
) -> None:
    client = _build_client(token, env_file, username)
    try:
        data = client.create(name, description, private, auto_init)
    except RuntimeError as exc:
        _fail("Failed to create repository", exc)
    console.print(
        Panel.fit(
            f"[bold]Name:[/bold]  {data['name']}\n"
            f"[bold]URL:[/bold]   {data.get('html_url', '')}\n"
            f"[bold]Clone:[/bold] {data.get('clone_url', data.get('ssh_url', ''))}",
            title="✓ Repository created",
            border_style="green",
        )
    )


@app.command()
def push(
    name: Annotated[Optional[str], typer.Argument(help="Repo name (default: cwd basename)")] = None,
    branch: Annotated[str, typer.Option("--branch", "-b", help="Branch name")] = DEFAULT_BRANCH,
    message: Annotated[Optional[str], typer.Option("-m", "--message", help="Commit message")] = None,
    remote: Annotated[str, typer.Option("--remote", help="Remote name")] = "origin",
    protocol: Annotated[Literal["ssh", "https"], typer.Option("--protocol", help="URL protocol")] = "ssh",
    private: Annotated[bool, typer.Option("--private/--public", help="Visibility")] = False,
    description: Annotated[Optional[str], typer.Option("-d", "--description", help="Repo description")] = None,
    existing: Annotated[
        Literal["ask", "merge", "new", "exit"],
        typer.Option("--existing", help="Behavior when a git repo already exists"),
    ] = "merge",
    no_gitignore: Annotated[bool, typer.Option("--no-gitignore", help="Skip copying ~/.gitignore")] = False,
    no_readme: Annotated[bool, typer.Option("--no-readme", help="Skip auto README creation")] = False,
    no_commit: Annotated[bool, typer.Option("--no-commit", help="Skip commit step")] = False,
    no_push: Annotated[bool, typer.Option("--no-push", help="Local-only: skip remote creation and push")] = False,
    pull_rebase: Annotated[
        bool, typer.Option("--pull-rebase/--no-pull-rebase", help="Auto-rebase on push rejection")
    ] = True,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Non-interactive mode")] = False,
    token: TokenOpt = None,
    env_file: EnvFileOpt = DEFAULT_ENV_FILE,
    username: UsernameOpt = DEFAULT_USERNAME,
    git_user: Annotated[str, typer.Option("--git-user", help="git user.name")] = DEFAULT_GIT_USER,
    git_email: Annotated[str, typer.Option("--git-email", help="git user.email")] = DEFAULT_GIT_EMAIL,
    global_gitignore: Annotated[
        Path, typer.Option("--global-gitignore", help="Path to global .gitignore")
    ] = DEFAULT_GLOBAL_GITIGNORE,
) -> None:
    if not shutil.which("git"):
        _fail("git is not installed")

    cwd = Path.cwd()
    repo_name = name or cwd.name
    msg = message or datetime.now().strftime("Auto-commit: %Y-%m-%d %H:%M:%S")
    client = _build_client(token, env_file, username)

    console.rule(f"[bold]ghrepo push → {repo_name}[/bold]")

    if _is_git_repo(cwd):
        if existing == "exit":
            console.print("[yellow]• Repository exists (--existing=exit)[/yellow]")
            return
        if existing == "new":
            if not name and yes:
                _fail("--existing=new requires --name when --yes is set")
            if not name:
                new_name = typer.prompt("New repository name").strip()
                if not new_name:
                    _fail("Repository name cannot be empty")
                repo_name = new_name
        elif existing == "ask" and not yes and sys.stdin.isatty():
            console.print("[yellow]• Git repo already exists.[/yellow]")
            choice = typer.prompt("1=merge  2=new  3=exit", default="1")
            if choice == "3":
                return
            if choice == "2":
                new_name = typer.prompt("New repository name").strip()
                if not new_name:
                    _fail("Repository name cannot be empty")
                repo_name = new_name
        console.print(f"[dim]• Using existing git repo ({repo_name})[/dim]")
    else:
        _init_repo(cwd)
        _config_user(cwd, git_user, git_email)
        console.print("[green]✓ Initialized git repository[/green]")

    if not no_gitignore and _copy_gitignore(cwd, global_gitignore.expanduser()):
        console.print("[green]✓ Copied .gitignore[/green]")

    if not no_readme and _ensure_readme(cwd, repo_name):
        console.print("[green]✓ Created README.md[/green]")

    if not no_commit:
        _stage_all(cwd)
        if _commit(cwd, msg):
            console.print(f"[green]✓ Committed: {msg}[/green]")
        else:
            console.print("[yellow]• Nothing to commit[/yellow]")

    current = _current_branch(cwd)
    if current and current != branch:
        _rename_branch(cwd, branch)
        console.print(f"[green]✓ Renamed branch {current} → {branch}[/green]")

    if no_push:
        console.print("[green]✓ Local-only mode (--no-push)[/green]")
        return

    try:
        if not client.repo_exists(repo_name):
            client.create(repo_name, description or DEFAULT_DESCRIPTION, private, auto_init=False)
            console.print(f"[green]✓ Created GitHub repo {username}/{repo_name}[/green]")
        else:
            console.print(f"[green]✓ Repo exists: {username}/{repo_name}[/green]")
    except RuntimeError as exc:
        _fail("Failed to create GitHub repository", exc)

    url = (
        f"git@github.com:{username}/{repo_name}.git"
        if protocol == "ssh"
        else f"https://github.com/{username}/{repo_name}.git"
    )
    _set_or_add_remote(cwd, remote, url)
    console.print(f"[green]✓ Remote {remote}: {url}[/green]")

    console.print(f"[dim]• Pushing {branch} → {remote}...[/dim]")
    ok, err = _push(cwd, remote, branch)
    if (
        not ok
        and pull_rebase
        and any(s in err for s in ("non-fast-forward", "fetch first", "rejected", "remote contains work"))
    ):
        console.print("[yellow]• Pulling with rebase...[/yellow]")
        _pull_rebase(cwd, remote, branch)
        ok, err = _push(cwd, remote, branch)
    if not ok:
        _fail(f"Push failed: {err.strip()[:300]}")

    console.print(
        Panel.fit(
            f"[bold]https://github.com/{username}/{repo_name}[/bold]",
            title="✓ Pushed to GitHub",
            border_style="green",
        )
    )


@app.command()
def status() -> None:
    cwd = Path.cwd()
    table = Table(title="Repository status")
    table.add_column("Key", style="cyan")
    table.add_column("Value")
    table.add_row("Path", str(cwd))
    if not _is_git_repo(cwd):
        table.add_row("Git", "[red]not initialized[/red]")
        console.print(table)
        return
    table.add_row("Git", "[green]initialized[/green]")
    table.add_row("Branch", _current_branch(cwd))
    table.add_row("Remote origin", _get_remote(cwd, "origin") or "[dim](none)[/dim]")
    dirty = bool(_git(["status", "--porcelain"], cwd).stdout.strip())
    table.add_row("Dirty", "[yellow]yes[/yellow]" if dirty else "[green]no[/green]")
    console.print(table)


@app.command(name="config")
def config_cmd(env_file: EnvFileOpt = DEFAULT_ENV_FILE) -> None:
    git_bin = shutil.which("git")
    gh_bin = shutil.which("gh")
    token = _load_token_file(env_file) or os.environ.get("GITHUB_TOKEN")
    table = Table(title="Configuration")
    table.add_column("Key", style="cyan")
    table.add_column("Value")
    table.add_row("Version", __version__)
    table.add_row("Env file", str(env_file))
    table.add_row("Token", "[green]✓ set[/green]" if token else "[red]✗ missing[/red]")
    table.add_row("gh CLI", f"[green]✓ {gh_bin}[/green]" if gh_bin else "[red]✗ not found[/red]")
    table.add_row("gh authed", "[green]✓[/green]" if _gh_authed() else "[red]✗[/red]")
    table.add_row("git", f"[green]✓ {git_bin}[/green]" if git_bin else "[red]✗ not found[/red]")
    table.add_row("Log file", str(LOG_DIR / "ghrepo.log"))
    console.print(table)


def main() -> None:
    try:
        app()
    except SystemExit:
        raise
    except Exception as exc:
        logger.exception("Unexpected failure")
        console.print(f"[red]✗ Unexpected error: {exc}[/red]")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
