#!/data/data/com.termux/files/usr/bin/env python
"""Create a GitHub repository and push the current project into it.
Two backends: ``api`` talks to the REST API with a token from ``--token``/``--env-file``, ``gh`` shells out to the GitHub CLI.
Both initialise git when needed, optionally add a README and ``.gitignore``, stage and commit, set the remote and push the chosen branch.
Usage: git_repo_creator {api,gh} NAME [options] Positional arguments: name Subcommands: api gh Options: -d, --description --private --auto-init --push --token --env-file --api-url --remote --protocol --branch ...
plus 7 more; run ``git_repo_creator --help`` for the full list.
Examples: $ git_repo_creator gh myproject --private $ git_repo_creator api myproject --push Command-line script: run ``python git_repo_creator.py`` or the ``git_repo_creator`` command.
The module is importable as well."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Optional, Sequence
import urllib.error
import urllib.request


def run(
    cmd: Sequence[str],
    cwd: Optional[Path] = None,
    check: bool = False,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        check=check,
        text=True,
        capture_output=capture,
    )


def load_env(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def get_token(args: argparse.Namespace) -> str:
    token = args.token or os.environ.get("GITHUB_TOKEN")
    if not token:
        env_path = Path(args.env_file).expanduser()
        token = load_env(env_path).get("GITHUB_TOKEN")
    if not token:
        print("Error: GITHUB_TOKEN not found in ~/.env", file=sys.stderr)
        sys.exit(1)
    return token


def api_request(method: str, url: str, token: str, payload: Optional[dict[str, Any]] = None) -> tuple[int, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, method=method, data=data)
    request.add_header("Authorization", f"token {token}")
    request.add_header("Accept", "application/vnd.github.v3+json")
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {"message": body}
        return exc.code, parsed


def create_repo_api(
    token: str,
    name: str,
    description: str,
    private: bool,
    auto_init: bool,
    api_url: str,
) -> Optional[dict[str, Any]]:
    payload = {
        "name": name,
        "description": description,
        "private": private,
        "auto_init": auto_init,
    }
    status, data = api_request("POST", api_url, token, payload)
    if status == 201:
        print("✅ Repository created successfully!")
        print(f"📁 Name:{data['name']}")
        print(f"🔗 URL:{data['html_url']}")
        print(f"📝 Clone URL:{data['clone_url']}")
        return data
    print(f"❌ Failed to create repository:{status}")
    print(f"Error:{data.get('message', 'Unknown error')}")
    return None


def git_repo_exists(cwd: Path) -> bool:
    return (cwd / ".git").exists()


def init_git(cwd: Path, git_user: Optional[str] = None, git_email: Optional[str] = None) -> None:
    if not git_repo_exists(cwd):
        print("Initializing git repository...")
        run(["git", "init"], cwd, check=True)
    else:
        print("Git repository already initialized.")
    if git_user:
        run(["git", "config", "user.name", git_user], cwd)
    if git_email:
        run(["git", "config", "user.email", git_email], cwd)


def stage_all(cwd: Path) -> None:
    print("Staging all changes...")
    run(["git", "add", "-A"], cwd, check=True)


def commit(cwd: Path, message: str) -> bool:
    status = run(["git", "status", "--porcelain"], cwd)
    if status.stdout.strip():
        print("Committing changes...")
        run(["git", "commit", "-m", message], cwd, check=True)
        return True
    print("No changes to commit.")
    return False


def rename_branch(cwd: Path, branch: str) -> None:
    current = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd)
    if current.returncode == 0 and current.stdout.strip() != branch:
        print(f"Renaming branch from '{current.stdout.strip()}' to '{branch}'...")
        run(["git", "branch", "-M", branch], cwd)


def add_or_set_remote(cwd: Path, url: str, remote: str) -> None:
    current = run(["git", "remote", "get-url", remote], cwd)
    if current.returncode == 0:
        existing = current.stdout.strip()
        if existing != url:
            print(f"Updating remote {remote} from {existing} to {url}")
            run(["git", "remote", "set-url", remote, url], cwd)
        else:
            print(f"Remote '{remote}' already configured correctly")
    else:
        print(f"Adding remote {remote}: {url}")
        run(["git", "remote", "add", remote, url], cwd, check=True)


def push(cwd: Path, remote: str, branch: str, pull_rebase: bool) -> None:
    print(f"Pushing branch '{branch}' to {remote}...")
    result = run(["git", "push", "-u", remote, branch], cwd)
    if result.returncode == 0:
        print("Successfully pushed.")
        return
    error = (result.stderr or "") + (result.stdout or "")
    if pull_rebase and (
        "non-fast-forward" in error or "fetch first" in error or "remote contains work" in error or "rejected" in error
    ):
        print("Remote has changes. Pulling with rebase...")
        run(["git", "pull", remote, branch, "--rebase"], cwd, check=True)
        run(["git", "push", "-u", remote, branch], cwd, check=True)
        print("Successfully pushed after rebase.")
        return
    print(error, file=sys.stderr)
    sys.exit(1)


def gh_available() -> bool:
    return shutil.which("gh") is not None


def gh_authenticated() -> bool:
    return run(["gh", "auth", "status"]).returncode == 0


def gh_repo_exists(username: Optional[str], name: str) -> bool:
    full = f"{username}/{name}" if username else name
    return run(["gh", "repo", "view", full]).returncode == 0


def gh_create_repo(name: str, source: Path, remote: str, public: bool, description: Optional[str]) -> None:
    cmd = ["gh", "repo", "create", name, "--source", str(source), "--remote", remote]
    cmd.append("--public" if public else "--private")
    if description:
        cmd.extend(["--description", description])
    print(f"Running:{' '.join(cmd)}")
    result = run(cmd)
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        sys.exit(1)


def ensure_readme(cwd: Path, name: str) -> None:
    files = [path for path in cwd.iterdir() if path.name != ".git"]
    if not files:
        readme = cwd / "README.md"
        if not readme.exists():
            readme.write_text(f"# {name}\nRepository initialized on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            print("Created README.md")


def copy_gitignore(cwd: Path) -> None:
    global_gitignore = Path.home() / ".gitignore"
    local_gitignore = cwd / ".gitignore"
    if global_gitignore.exists() and not local_gitignore.exists():
        shutil.copy2(global_gitignore, local_gitignore)
        print(f"Copied {global_gitignore}->{local_gitignore}")
    elif local_gitignore.exists():
        print(".gitignore already exists in current directory.")
    else:
        print(f"No global .gitignore found at {global_gitignore}")


def add_bool_argument(parser: argparse.ArgumentParser, name: str, default: bool) -> None:
    dest = name.replace("-", "_")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(f"--{name}", dest=dest, action="store_true")
    group.add_argument(f"--no-{name}", dest=dest, action="store_false")
    parser.set_defaults(**{dest: default})


def cmd_api(args: argparse.Namespace) -> int:
    token = get_token(args)
    name = args.name or Path.cwd().name
    data = create_repo_api(token, name, args.description, args.private, args.auto_init, args.api_url)
    if not data:
        return 1
    if args.push:
        cwd = Path.cwd()
        init_git(cwd, args.git_user, args.git_email)
        stage_all(cwd)
        commit(cwd, args.commit_message)
        rename_branch(cwd, args.branch)
        url = data["clone_url"] if args.protocol == "https" else data["ssh_url"]
        add_or_set_remote(cwd, url, args.remote)
        push(cwd, args.remote, args.branch, args.pull_rebase)
        print(f"\n✨ Repository ready at:{data['html_url']}")
    return 0


def cmd_gh(args: argparse.Namespace) -> int:
    cwd = Path(args.source).expanduser().resolve()
    os.chdir(cwd)
    if not gh_available():
        print("Error: GitHub CLI (gh) is not installed.", file=sys.stderr)
        return 1
    if not gh_authenticated():
        print(
            "Error: GitHub CLI is not authenticated. Run: gh auth login",
            file=sys.stderr,
        )
        return 1
    repo_name = args.name or cwd.name
    if args.copy_global_gitignore:
        copy_gitignore(cwd)
    if not git_repo_exists(cwd):
        if args.init:
            init_git(cwd, args.git_user, args.git_email)
        else:
            print("Error: not a git repository and --no-init", file=sys.stderr)
            return 1
    else:
        if args.existing == "exit":
            return 0
        if args.existing == "ask":
            if sys.stdin.isatty():
                print(f"Git repository already exists in {cwd}")
                print("1. Merge changes")
                print("2. Create new repository")
                print("3. Exit")
                choice = input("Select option (1-3): ").strip()
                if choice == "3":
                    return 0
                if choice == "2":
                    new_name = input("Enter new repository name: ").strip()
                    if not new_name:
                        print("Error: Repository name cannot be empty.", file=sys.stderr)
                        return 1
                    repo_name = new_name
                    run(["git", "remote", "remove", args.remote], cwd)
        elif args.existing == "new":
            if args.name:
                repo_name = args.name
            else:
                new_name = input("Enter new repository name: ").strip()
                if not new_name:
                    print("Error: Repository name cannot be empty.", file=sys.stderr)
                    return 1
                repo_name = new_name
            run(["git", "remote", "remove", args.remote], cwd)
    if args.create_readme:
        ensure_readme(cwd, repo_name)
    if args.commit:
        stage_all(cwd)
        message = args.commit_message or datetime.now().strftime("Auto-commit:%Y-%m-%d:%H:%M:%S")
        commit(cwd, message)
    if args.rename_branch:
        rename_branch(cwd, args.branch)
    if args.push:
        if not gh_repo_exists(args.github_username, repo_name):
            gh_create_repo(repo_name, cwd, args.remote, not args.private, args.description)
        else:
            print(f"✓ Repository {repo_name} already exists on GitHub")
        if args.github_username:
            url = (
                f"git@github.com:{args.github_username}/{repo_name}.git"
                if args.protocol == "ssh"
                else f"https://github.com/{args.github_username}/{repo_name}.git"
            )
            add_or_set_remote(cwd, url, args.remote)
        push(cwd, args.remote, args.branch, args.pull_rebase)
        print(f"\n✅ Success! Repository '{repo_name}' is now on GitHub.")
        if args.github_username:
            print(f"View it at:https://github.com/{args.github_username}/{repo_name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mkghrepo")
    subparsers = parser.add_subparsers(dest="command", required=True)
    api = subparsers.add_parser("api")
    api.add_argument("name", nargs="?", default=None)
    api.add_argument("-d", "--description", default="created with python")
    api.add_argument("--private", action="store_true")
    api.add_argument("--auto-init", action="store_true")
    api.add_argument("--push", action="store_true")
    api.add_argument("--token")
    api.add_argument("--env-file", default="~/.env")
    api.add_argument("--api-url", default="https://api.github.com/user/repos")
    api.add_argument("--remote", default="origin")
    api.add_argument("--protocol", choices=["https", "ssh"], default="ssh")
    api.add_argument("--branch", default="main")
    api.add_argument("--commit-message", default="Update files")
    api.add_argument("--git-user")
    api.add_argument("--git-email")
    api.add_argument("--pull-rebase", action="store_true")
    gh = subparsers.add_parser("gh")
    gh.add_argument("name", nargs="?", default=None)
    gh.add_argument("-d", "--description")
    gh.add_argument("--private", action="store_true")
    gh.add_argument("--source", default=".")
    gh.add_argument("--remote", default="origin")
    gh.add_argument("--branch", default="main")
    gh.add_argument("--commit-message")
    gh.add_argument("--git-user", default="i4147")
    gh.add_argument("--git-email", default="adnanonagh@gmail.com")
    gh.add_argument("--github-username", default="i4147")
    gh.add_argument("--protocol", choices=["https", "ssh"], default="https")
    gh.add_argument("--existing", choices=["ask", "merge", "new", "exit"], default="ask")
    add_bool_argument(gh, "copy-global-gitignore", True)
    add_bool_argument(gh, "create-readme", True)
    add_bool_argument(gh, "init", True)
    add_bool_argument(gh, "commit", True)
    add_bool_argument(gh, "push", True)
    add_bool_argument(gh, "pull-rebase", True)
    add_bool_argument(gh, "rename-branch", True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "api":
        return cmd_api(args)
    if args.command == "gh":
        return cmd_gh(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
