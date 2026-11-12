#!/data/data/com.termux/files/usr/bin/env python
"""Create a GitHub repository from the current folder and push it.
Reads the token from ``~/.env``, copies ``~/.gitignore``, initialises the repository if needed, creates it through the REST API (``-p`` for private) and commits and pushes the current branch; ``-y``/``-f`` skip the confirmations.
Usage: nr [-n NAME] [-p] [-d DESCRIPTION] [-m MESSAGE] [-y] Options: -n, --name Repository name (default: current directory name) -p, --private Create as private repo (default: public) -d, --description Repository description -m, --message -y, --yes Assume yes to prompts (non-interactive).
-f, --force Skip confirmation prompts (same as --yes).
--no-gitignore Do not copy ~/.gitignore.
Examples: $ nr -n myproject -p -y Command-line script: run ``python nr.py`` or the ``nr`` command.
The module is importable as well."""

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Optional

from loguru import logger
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ENV_FILE = Path.home() / ".env"
GITHUB_API_BASE: str = "https://api.github.com"
GIT_LOCAL_TIMEOUT: int = 30
GIT_NETWORK_TIMEOUT: int = 600
HTTP_TIMEOUT: int = 15
DEFAULT_BRANCH: str = "main"
DEFAULT_COMMIT_MSG: str = "Initial commit"
logger.remove()
logger.add(
    sys.stderr,
    format="<level>{level: <8}</level> | <level>{message}</level>",
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
)


def _build_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(500, 502, 503, 504, 429),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    return session


SESSION = _build_session()


def _gh_headers(token: str) -> dict:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "gh-create-repo/1.0",
    }


def load_github_token() -> str:
    env_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if env_token:
        logger.info("Using GITHUB_TOKEN from environment")
        return env_token
    if not ENV_FILE.exists():
        msg = f"~/.env not found at {ENV_FILE}. Create it with GITHUB_TOKEN=<token>, or export GITHUB_TOKEN."
        raise FileNotFoundError(msg)
    try:
        content = ENV_FILE.read_text(encoding="utf-8")
    except OSError as e:
        msg = f"Could not read {ENV_FILE}: {e}"
        raise RuntimeError(msg) from e
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if line.startswith("GITHUB_TOKEN="):
            token = line.split("=", 1)[1].strip().strip("\"'")
            if not token:
                msg = "GITHUB_TOKEN is empty in ~/.env"
                raise ValueError(msg)
            logger.info("Loaded GITHUB_TOKEN from ~/.env")
            return token
    msg = "GITHUB_TOKEN not found in ~/.env"
    raise ValueError(msg)


_CURRENT_USER_CACHE: Optional[str] = None


def get_current_user_login(token: str) -> str:
    global _CURRENT_USER_CACHE
    if _CURRENT_USER_CACHE:
        return _CURRENT_USER_CACHE
    try:
        r = SESSION.get(
            f"{GITHUB_API_BASE}/user",
            headers=_gh_headers(token),
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as e:
        msg = f"Could not reach GitHub API: {e}"
        raise RuntimeError(msg) from e
    if r.status_code == 401:
        msg = "GitHub token is invalid or expired (401)."
        raise ValueError(msg)
    if r.status_code != 200:
        msg = f"GitHub API error {r.status_code}: {r.text[:200]}"
        raise requests.RequestException(msg)
    login = r.json().get("login")
    if not login:
        msg = "Could not determine GitHub login from /user"
        raise ValueError(msg)
    logger.info(f"Authenticated as GitHub user: {login}")
    _CURRENT_USER_CACHE = login
    return login


def create_github_repo(
    repo_name: str,
    token: str,
    user_login: str,
    private: bool = False,
    description: Optional[str] = None,
) -> str:
    payload: dict = {"name": repo_name, "private": private}
    if description:
        payload["description"] = description
    r = SESSION.post(
        f"{GITHUB_API_BASE}/user/repos",
        headers=_gh_headers(token),
        json=payload,
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code == 201:
        data = r.json()
        logger.info(f"✓ Created GitHub repo: {data['full_name']}")
        return data.get("ssh_url") or data["clone_url"]
    if r.status_code == 422:
        logger.warning(f"Repo '{user_login}/{repo_name}' already exists on GitHub — reusing it.")
        return f"git@github.com:{user_login}/{repo_name}.git"
    if r.status_code == 401:
        msg = "Invalid GitHub token (401 Unauthorized)."
        raise ValueError(msg)
    if r.status_code == 403:
        msg = f"GitHub API forbidden (rate limit or insufficient scopes): {r.text[:200]}"
        raise ValueError(msg)
    msg = f"GitHub API error {r.status_code}: {r.text[:200]}"
    raise requests.RequestException(msg)


def run_git_command(
    cmd: list,
    cwd: Optional[Path] = None,
    check: bool = True,
    timeout: Optional[int] = None,
) -> tuple[str, int]:
    timeout = timeout or GIT_LOCAL_TIMEOUT
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd or Path.cwd(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        msg = f"Command not found: {cmd[0]} (is git installed and on PATH?)"
        raise RuntimeError(msg) from e
    except subprocess.TimeoutExpired as e:
        msg = f"Command timed out after {timeout}s: {' '.join(cmd)}"
        raise RuntimeError(msg) from e
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if check and result.returncode != 0:
        detail = stderr or stdout or "(no output)"
        msg = f"Command failed ({result.returncode}): {' '.join(cmd)}\n{detail}"
        raise RuntimeError(msg)
    return stdout, result.returncode


def is_git_repo(cwd: Path) -> bool:
    return (cwd / ".git").exists()


def get_remote_url(cwd: Path, remote: str = "origin") -> Optional[str]:
    url, rc = run_git_command(["git", "config", "--get", f"remote.{remote}.url"], cwd, check=False)
    return url if rc == 0 and url else None


_GH_SSH_RE = re.compile(r"^git@github\.com:([^/]+)/(.+?)(?:\.git)?/?$")
_GH_HTTPS_RE = re.compile(r"^https?://(?:[^@/]+@)?github\.com/([^/]+)/(.+?)(?:\.git)?/?$")


def parse_github_url(url: str) -> tuple[str, str]:
    for rx in (_GH_SSH_RE, _GH_HTTPS_RE):
        m = rx.match(url)
        if m:
            return m.group(1), m.group(2)
    msg = f"Not a GitHub remote URL: {url}"
    raise ValueError(msg)


def validate_repo_name(name: str) -> None:
    if not name:
        msg = "Repository name is empty"
        raise ValueError(msg)
    if len(name) > 100:
        msg = f"Repository name too long ({len(name)} > 100)"
        raise ValueError(msg)
    if name in (".", ".."):
        msg = "Repository name cannot be '.' or '..'"
        raise ValueError(msg)
    if name.startswith("."):
        msg = "Repository name cannot start with '.'"
        raise ValueError(msg)
    if name.endswith(".git"):
        msg = "Repository name cannot end with '.git'"
        raise ValueError(msg)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        msg = f"Invalid repo name '{name}': use only letters, digits, '.', '-', '_'"
        raise ValueError(msg)


def copy_gitignore(cwd: Path) -> None:
    source = Path.home() / ".gitignore"
    target = cwd / ".gitignore"
    if not source.exists():
        logger.warning("~/.gitignore not found, skipping copy")
        return
    if target.exists():
        logger.info(".gitignore already exists locally, preserving")
        return
    try:
        shutil.copy2(source, target)
        logger.info(f"✓ Copied .gitignore from ~ → {cwd}")
    except OSError as e:
        msg = f"Failed to copy .gitignore: {e}"
        raise RuntimeError(msg) from e


def _warn_if_missing_identity(cwd: Path) -> None:
    _, rc_name = run_git_command(["git", "config", "user.name"], cwd, check=False)
    _, rc_mail = run_git_command(["git", "config", "user.email"], cwd, check=False)
    if rc_name != 0 or rc_mail != 0:
        logger.warning(
            "git user.name/user.email not configured — commit may fail.\n"
            "Fix with:\n"
            "  git config --global user.name  'Your Name'\n"
            "  git config --global user.email 'you@example.com'"
        )


def initialize_git_repo(cwd: Path, remote_url: str) -> None:
    if not is_git_repo(cwd):
        logger.info("Initializing git repository...")
        _, rc = run_git_command(["git", "init", "-b", DEFAULT_BRANCH], cwd, check=False)
        if rc != 0:
            run_git_command(["git", "init"], cwd)
            run_git_command(
                ["git", "symbolic-ref", "HEAD", f"refs/heads/{DEFAULT_BRANCH}"],
                cwd,
            )
    _warn_if_missing_identity(cwd)
    existing = get_remote_url(cwd)
    if existing == remote_url:
        logger.info(f"Remote already set to {remote_url}")
    elif existing:
        logger.info(f"Updating remote: {existing} → {remote_url}")
        run_git_command(["git", "remote", "set-url", "origin", remote_url], cwd)
    else:
        run_git_command(["git", "remote", "add", "origin", remote_url], cwd)
    logger.info(f"✓ Remote configured: {remote_url}")


def commit_and_push(cwd: Path, commit_message: str = DEFAULT_COMMIT_MSG) -> None:
    _, has_head = run_git_command(["git", "rev-parse", "--verify", "HEAD"], cwd, check=False)
    if has_head != 0:
        run_git_command(
            ["git", "symbolic-ref", "HEAD", f"refs/heads/{DEFAULT_BRANCH}"],
            cwd,
            check=False,
        )
    logger.info("Staging files...")
    run_git_command(["git", "add", "-A"], cwd)
    status, _ = run_git_command(["git", "status", "--porcelain"], cwd)
    _, has_head = run_git_command(["git", "rev-parse", "--verify", "HEAD"], cwd, check=False)
    if not status and has_head != 0:
        msg = "Nothing to commit and no prior commit exists (empty repository)."
        raise RuntimeError(msg)
    if status:
        logger.info("Creating commit...")
        run_git_command(["git", "commit", "-m", commit_message], cwd)
    else:
        logger.info("No changes to commit (working tree clean)")
    current, _ = run_git_command(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd)
    if current != DEFAULT_BRANCH:
        logger.info(f"Renaming branch '{current}' → '{DEFAULT_BRANCH}'")
        run_git_command(["git", "branch", "-M", DEFAULT_BRANCH], cwd)
    logger.info("Pushing to origin...")
    try:
        run_git_command(
            ["git", "push", "-u", "origin", DEFAULT_BRANCH],
            cwd,
            timeout=GIT_NETWORK_TIMEOUT,
        )
    except RuntimeError as e:
        logger.error(f"Push failed: {e}")
        logger.info("Troubleshoot: ensure your SSH key is registered on GitHub, or run 'gh auth login'.")
        raise
    logger.info(f"✓ Pushed to origin/{DEFAULT_BRANCH}")


def _sanity_check_environment(cwd: Path) -> Optional[int]:
    if shutil.which("git") is None:
        logger.error("`git` not found on PATH. Install git and retry.")
        return 1
    if cwd == Path.home():
        logger.error("Refusing to run in home directory. cd into your project first.")
        return 1
    if cwd == Path(cwd.anchor):
        logger.error("Refusing to run at filesystem root.")
        return 1
    if not os.access(cwd, os.W_OK):
        logger.error(f"No write permission in {cwd}")
        return 1
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create and push a new GitHub repo from current folder contents.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                          # repo name = current folder name
  %(prog)s -n my-project            # repo name = my-project
  %(prog)s -n my-project --private  # create private repo
  %(prog)s -y                       # non-interactive (assume yes)
""",
    )
    parser.add_argument(
        "-n",
        "--name",
        default=None,
        help="Repository name (default: current directory name)",
    )
    parser.add_argument(
        "-p",
        "--private",
        action="store_true",
        help="Create as private repo (default: public)",
    )
    parser.add_argument(
        "-d",
        "--description",
        default=None,
        help="Repository description",
    )
    parser.add_argument(
        "-m",
        "--message",
        default=DEFAULT_COMMIT_MSG,
        help=f"Initial commit message (default: '{DEFAULT_COMMIT_MSG}')",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Assume yes to prompts (non-interactive).",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Skip confirmation prompts (same as --yes).",
    )
    parser.add_argument(
        "--no-gitignore",
        action="store_true",
        help="Do not copy ~/.gitignore.",
    )
    args = parser.parse_args()
    non_interactive = args.yes or args.force or not sys.stdin.isatty()
    cwd = Path.cwd().resolve()
    rc = _sanity_check_environment(cwd)
    if rc is not None:
        return rc
    try:
        token = load_github_token()
        current_user = get_current_user_login(token)
        repo_name = args.name or cwd.name
        validate_repo_name(repo_name)
        logger.info(f"Target repo name: {repo_name}")
        if not args.no_gitignore:
            copy_gitignore(cwd)
        remote_url: Optional[str] = None
        if is_git_repo(cwd):
            logger.info("Existing git repo detected")
            remote_url = get_remote_url(cwd)
            if remote_url:
                logger.info(f"Remote URL: {remote_url}")
                try:
                    owner, _ = parse_github_url(remote_url)
                except ValueError:
                    logger.warning(f"Remote is not a GitHub URL; leaving it alone: {remote_url}")
                    owner = None
                if owner and owner.lower() == current_user.lower():
                    logger.info("✓ You own this remote — proceeding")
                elif owner:
                    logger.warning(f"Remote belongs to '{owner}', not you ('{current_user}')")
                    if non_interactive:
                        logger.info("Non-interactive mode: keeping existing remote.")
                    else:
                        resp = input("Remove remote and create a new repo in your account? [y/N]: ").strip().lower()
                        if resp != "y":
                            logger.info("Aborted")
                            return 1
                        run_git_command(["git", "remote", "remove", "origin"], cwd)
                        remote_url = None
            else:
                logger.info("Git repo exists but no remote configured")
        if not remote_url:
            logger.info(f"Creating GitHub repo: {current_user}/{repo_name}")
            remote_url = create_github_repo(
                repo_name,
                token,
                current_user,
                private=args.private,
                description=args.description,
            )
            initialize_git_repo(cwd, remote_url)
        commit_and_push(cwd, args.message)
        try:
            owner, repo = parse_github_url(remote_url)
        except ValueError:
            owner, repo = current_user, repo_name
        logger.info(f"✅ Success! https://github.com/{owner}/{repo}")
        return 0
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        return 130
    except (FileNotFoundError, ValueError) as e:
        logger.error(f"Configuration error: {e}")
        return 1
    except requests.RequestException as e:
        logger.error(f"Network/GitHub API error: {e}")
        return 1
    except RuntimeError as e:
        logger.error(str(e))
        return 1
    except Exception as e:
        logger.error(f"Unexpected error: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
