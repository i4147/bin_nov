#!/data/data/com.termux/files/usr/bin/env python
"""Create a GitHub repository from a local project and push it.
Subcommands: ``api`` creates the repository through the REST API, ``api-push`` also initialises, commits and pushes, ``gh-create`` mixes PyGithub and the ``gh`` CLI, ``gh-cli`` is a pure ``gh``/git workflow and ``gh-managed`` an interactive manager.
The token comes from ``~/.env`` and ``--backend`` picks the git implementation.
Usage: gh_repo {api,api-push,gh-create,gh-cli,gh-managed} [options] Positional arguments: repo_name Name of the new repository.
description Subcommands: api Create a repo via API (mkghrepo.py).
api-push Init/commit + API create + push (new_repo.py).
gh-create PyGithub/gh-CLI hybrid create+push (new_repo2.py).
gh-cli Pure gh CLI + git subprocess workflow (newrepo.py).
gh-managed Interactive manager with prompts & README (pynewrepo.py).
Options: -b, --backend Backend for git and GitHub operations.
Default: subprocess.
--env-file --github-username --private Make the repo private.
-n, --name Repository name.
Default: current directory name.
--branch --commit-message --global-gitignore -m, --message Custom commit message.
Default: auto-generated with timestamp.
--git-email ...
plus 1 more; run ``gh_repo --help`` for the full list.
Examples: $ gh_repo api-push myproject --private Command-line script: run ``python gh_repo.py`` or the ``gh_repo`` command.
The module is importable as well."""

from abc import ABC, abstractmethod
import argparse
import contextlib
from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import shutil
import subprocess
import sys
import traceback
from typing import Any, Optional, Sequence


DEFAULT_GITHUB_USERNAME: str = "i4147"
DEFAULT_GIT_EMAIL: str = "adnanonagh@gmail.com"
DEFAULT_GIT_USER: str = "i4147"
DEFAULT_ENV_FILE = Path.home() / ".env"
DEFAULT_GLOBAL_GITIGNORE = Path.home() / ".gitignore"
DEFAULT_DESCRIPTION: str = "created with python"
DEFAULT_BRANCH: str = "main"
DEFAULT_API_PUSH_COMMIT_MESSAGE: str = "Update files"
DEFAULT_GH_CREATE_COMMIT_MESSAGE: str = "initial"
DEFAULT_GH_CLI_COMMIT_MESSAGE: str = "initial"
BACKEND_CHOICES: list[str] = [
    "subprocess",
    "gitpython",
    "rest",
    "pygithub",
    "githubpython",
    "dulwich",
    "libgit2",
]


def parse_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    key, _, value = line.partition("=")
                    env[key.strip()] = value.strip()
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"Error reading {path}: {exc}")
    return env


def get_github_token(env_file: Path | None = None) -> Optional[str]:
    env_file = env_file or DEFAULT_ENV_FILE
    env = parse_env_file(env_file)
    return env.get("GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")


def run_cli(
    cmd: Sequence[str],
    cwd: Path | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        check=check,
        capture_output=True,
        text=True,
    )


class GitBackend(ABC):
    @abstractmethod
    def is_git_repo(self, path: Path) -> bool: ...
    @abstractmethod
    def init_repo(self, path: Path) -> None: ...
    @abstractmethod
    def config_user(self, path: Path, name: str, email: str) -> None: ...
    @abstractmethod
    def add_all(self, path: Path) -> None: ...
    @abstractmethod
    def commit(self, path: Path, message: str) -> bool: ...
    @abstractmethod
    def is_dirty(self, path: Path) -> bool: ...
    @abstractmethod
    def get_remote_url(self, path: Path, name: str = "origin") -> Optional[str]: ...
    @abstractmethod
    def add_remote(self, path: Path, name: str, url: str) -> None: ...
    @abstractmethod
    def set_remote_url(self, path: Path, name: str, url: str) -> None: ...
    @abstractmethod
    def remove_remote(self, path: Path, name: str) -> None: ...
    @abstractmethod
    def push(self, path: Path, remote: str, branch: str, set_upstream: bool = True) -> None: ...
    @abstractmethod
    def current_branch(self, path: Path) -> str: ...
    @abstractmethod
    def rename_branch(self, path: Path, new_name: str) -> None: ...


class SubprocessGit(GitBackend):
    def _run(self, cmd: Sequence[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
        return run_cli(cmd, cwd=cwd, check=check)

    def is_git_repo(self, path: Path) -> bool:
        return self._run(["git", "rev-parse", "--git-dir"], path, check=False).returncode == 0

    def init_repo(self, path: Path) -> None:
        self._run(["git", "init"], path)

    def config_user(self, path: Path, name: str, email: str) -> None:
        self._run(["git", "config", "user.name", name], path, check=False)
        self._run(["git", "config", "user.email", email], path, check=False)

    def add_all(self, path: Path) -> None:
        self._run(["git", "add", "-A"], path)

    def commit(self, path: Path, message: str) -> bool:
        r = self._run(["git", "commit", "-m", message], path, check=False)
        return r.returncode == 0

    def is_dirty(self, path: Path) -> bool:
        r = self._run(["git", "status", "--porcelain"], path, check=False)
        return bool(r.stdout.strip())

    def get_remote_url(self, path: Path, name: str = "origin") -> Optional[str]:
        r = self._run(["git", "remote", "get-url", name], path, check=False)
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None

    def add_remote(self, path: Path, name: str, url: str) -> None:
        self._run(["git", "remote", "add", name, url], path)

    def set_remote_url(self, path: Path, name: str, url: str) -> None:
        self._run(["git", "remote", "set-url", name, url], path)

    def remove_remote(self, path: Path, name: str) -> None:
        self._run(["git", "remote", "remove", name], path, check=False)

    def push(self, path: Path, remote: str, branch: str, set_upstream: bool = True) -> None:
        cmd = ["git", "push"]
        if set_upstream:
            cmd.append("-u")
        cmd += [remote, branch]
        r = self._run(cmd, path, check=False)
        if r.returncode != 0:
            msg = f"Push failed: {r.stderr.strip() or r.stdout.strip()}"
            raise RuntimeError(msg)

    def current_branch(self, path: Path) -> str:
        r = self._run(["git", "branch", "--show-current"], path, check=False)
        return r.stdout.strip() or "main"

    def rename_branch(self, path: Path, new_name: str) -> None:
        self._run(["git", "branch", "-M", new_name], path, check=False)


class GitPythonGit(GitBackend):
    def _repo(self, path: Path):
        from git import InvalidGitRepositoryError, Repo

        try:
            return Repo(str(path))
        except InvalidGitRepositoryError:
            return None

    def is_git_repo(self, path: Path) -> bool:
        return self._repo(path) is not None

    def init_repo(self, path: Path) -> None:
        from git import Repo

        Repo.init(str(path))

    def config_user(self, path: Path, name: str, email: str) -> None:
        repo = self._repo(path)
        if repo:
            with repo.config_writer() as cw:
                cw.set_value("user", "name", name)
                cw.set_value("user", "email", email)

    def add_all(self, path: Path) -> None:
        repo = self._repo(path)
        if repo:
            repo.git.add(all=True)

    def commit(self, path: Path, message: str) -> bool:
        repo = self._repo(path)
        if not repo or not repo.is_dirty(untracked_files=True):
            return False
        repo.index.commit(message)
        return True

    def is_dirty(self, path: Path) -> bool:
        repo = self._repo(path)
        return bool(repo and repo.is_dirty(untracked_files=True))

    def get_remote_url(self, path: Path, name: str = "origin") -> Optional[str]:
        repo = self._repo(path)
        if not repo:
            return None
        try:
            return repo.remote(name).url
        except ValueError:
            return None

    def add_remote(self, path: Path, name: str, url: str) -> None:
        repo = self._repo(path)
        if repo:
            repo.create_remote(name, url)

    def set_remote_url(self, path: Path, name: str, url: str) -> None:
        repo = self._repo(path)
        if repo:
            remote = repo.remote(name)
            with remote.config_writer as cw:
                cw.set_value("url", url)

    def remove_remote(self, path: Path, name: str) -> None:
        repo = self._repo(path)
        if repo:
            with contextlib.suppress(Exception):
                repo.delete_remote(name)

    def push(self, path: Path, remote: str, branch: str, set_upstream: bool = True) -> None:
        repo = self._repo(path)
        if not repo:
            msg = "Not a git repository"
            raise RuntimeError(msg)
        kwargs = {"set_upstream": True} if set_upstream else {}
        repo.remote(remote).push(refspec=f"{branch}:{branch}", **kwargs)

    def current_branch(self, path: Path) -> str:
        repo = self._repo(path)
        if not repo or repo.head.is_detached:
            return "main"
        return repo.active_branch.name

    def rename_branch(self, path: Path, new_name: str) -> None:
        repo = self._repo(path)
        if repo:
            repo.git.branch("-M", new_name)


class DulwichGit(GitBackend):
    @staticmethod
    def _dulwich():
        try:
            from dulwich import porcelain
            from dulwich.repo import Repo

            return porcelain, Repo
        except ImportError as exc:
            msg = "dulwich backend requires `pip install dulwich`"
            raise SystemExit(msg) from exc

    def is_git_repo(self, path: Path) -> bool:
        _, Repo = self._dulwich()
        try:
            Repo(str(path))
            return True
        except Exception:
            return False

    def init_repo(self, path: Path) -> None:
        porcelain, _ = self._dulwich()
        porcelain.init(str(path))

    def config_user(self, path: Path, name: str, email: str) -> None:
        _, Repo = self._dulwich()
        repo = Repo(str(path))
        cfg = repo.get_config()
        cfg.set((b"user",), b"name", name.encode())
        cfg.set((b"user",), b"email", email.encode())
        cfg.write_to_path()

    def add_all(self, path: Path) -> None:
        porcelain, _ = self._dulwich()
        porcelain.add(str(path))

    def commit(self, path: Path, message: str) -> bool:
        porcelain, Repo = self._dulwich()
        repo = Repo(str(path))
        status = porcelain.status(repo)
        if not (status.untracked or status.unstaged or status.staged):
            return False
        porcelain.commit(str(path), message=message.encode(), author=None, committer=None)
        return True

    def is_dirty(self, path: Path) -> bool:
        porcelain, Repo = self._dulwich()
        repo = Repo(str(path))
        s = porcelain.status(repo)
        return bool(s.untracked or s.unstaged or s.staged)

    def get_remote_url(self, path: Path, name: str = "origin") -> Optional[str]:
        _, Repo = self._dulwich()
        repo = Repo(str(path))
        try:
            return repo.get_config().get((b"remote", name.encode()), b"url").decode()
        except KeyError:
            return None

    def add_remote(self, path: Path, name: str, url: str) -> None:
        _, Repo = self._dulwich()
        repo = Repo(str(path))
        cfg = repo.get_config()
        cfg.set((b"remote", name.encode()), b"url", url.encode())
        cfg.set(
            (b"remote", name.encode()),
            b"fetch",
            f"+refs/heads/*:refs/remotes/{name}/*".encode(),
        )
        cfg.write_to_path()

    def set_remote_url(self, path: Path, name: str, url: str) -> None:
        _, Repo = self._dulwich()
        repo = Repo(str(path))
        cfg = repo.get_config()
        cfg.set((b"remote", name.encode()), b"url", url.encode())
        cfg.write_to_path()

    def remove_remote(self, path: Path, name: str) -> None:
        _, Repo = self._dulwich()
        repo = Repo(str(path))
        cfg = repo.get_config()
        try:
            cfg.remove_section((b"remote", name.encode()))
            cfg.write_to_path()
        except KeyError:
            pass

    def push(self, path: Path, remote: str, branch: str, set_upstream: bool = True) -> None:
        porcelain, _ = self._dulwich()
        porcelain.push(str(path), remote, f"refs/heads/{branch}:refs/heads/{branch}")

    def current_branch(self, path: Path) -> str:
        _, Repo = self._dulwich()
        repo = Repo(str(path))
        try:
            ref = repo.refs.read_ref(b"HEAD")
            prefix = b"ref: refs/heads/"
            if ref and ref.startswith(prefix):
                return ref[len(prefix) :].decode()
        except Exception:
            pass
        return "main"

    def rename_branch(self, path: Path, new_name: str) -> None:
        _, Repo = self._dulwich()
        repo = Repo(str(path))
        current = self.current_branch(path)
        if current == new_name:
            return
        src = f"refs/heads/{current}".encode()
        dst = f"refs/heads/{new_name}".encode()
        sha = repo.refs.read_ref(src)
        if sha is None:
            return
        repo.refs[dst] = sha
        with contextlib.suppress(Exception):
            repo.refs.remove_if_equals(src, sha)
        repo.refs.set_symbolic_ref(b"HEAD", dst)


class Pygit2Git(GitBackend):
    @staticmethod
    def _pg():
        try:
            import pygit2

            return pygit2
        except ImportError as exc:
            msg = "libgit2 backend requires `pip install pygit2`"
            raise SystemExit(msg) from exc

    def _repo(self, path: Path):
        pg = self._pg()
        try:
            return pg.Repository(str(path))
        except Exception:
            return None

    def is_git_repo(self, path: Path) -> bool:
        return self._repo(path) is not None

    def init_repo(self, path: Path) -> None:
        pg = self._pg()
        pg.init_repository(str(path))

    def config_user(self, path: Path, name: str, email: str) -> None:
        repo = self._repo(path)
        if repo:
            repo.config["user.name"] = name
            repo.config["user.email"] = email

    def add_all(self, path: Path) -> None:
        repo = self._repo(path)
        if repo:
            repo.index.add_all()
            repo.index.write()

    def commit(self, path: Path, message: str) -> bool:
        repo = self._repo(path)
        if not repo:
            return False
        if not repo.status():
            return False
        tree = repo.index.write_tree()
        parents = [repo.head.target] if not repo.head_is_unborn else []
        sig = repo.default_signature
        repo.create_commit("HEAD", sig, sig, message, tree, parents)
        return True

    def is_dirty(self, path: Path) -> bool:
        repo = self._repo(path)
        return bool(repo and repo.status())

    def get_remote_url(self, path: Path, name: str = "origin") -> Optional[str]:
        repo = self._repo(path)
        if not repo:
            return None
        try:
            return repo.remotes[name].url
        except KeyError:
            return None

    def add_remote(self, path: Path, name: str, url: str) -> None:
        repo = self._repo(path)
        if repo:
            try:
                repo.remotes.create(name, url)
            except Exception:
                repo.remotes[name].url = url

    def set_remote_url(self, path: Path, name: str, url: str) -> None:
        repo = self._repo(path)
        if repo:
            try:
                repo.remotes[name].url = url
            except KeyError:
                repo.remotes.create(name, url)

    def remove_remote(self, path: Path, name: str) -> None:
        repo = self._repo(path)
        if repo:
            with contextlib.suppress(Exception):
                repo.remotes.delete(name)

    def push(self, path: Path, remote: str, branch: str, set_upstream: bool = True) -> None:
        msg = "push via pygit2 requires credential callbacks. Use the 'subprocess' backend for authenticated pushes."
        raise NotImplementedError(msg)

    def current_branch(self, path: Path) -> str:
        repo = self._repo(path)
        if not repo:
            return "main"
        try:
            return repo.head.shorthand
        except Exception:
            return "main"

    def rename_branch(self, path: Path, new_name: str) -> None:
        repo = self._repo(path)
        if not repo:
            return
        current = self.current_branch(path)
        if current == new_name:
            return
        branch = repo.branches.get(current)
        if branch:
            branch.rename(new_name)


class GitHubBackend(ABC):
    @abstractmethod
    def repo_exists(self, owner: str, name: str) -> bool: ...
    @abstractmethod
    def create_repo(
        self,
        name: str,
        description: str = "",
        private: bool = False,
        auto_init: bool = False,
        owner: Optional[str] = None,
    ) -> Optional[dict]: ...


class GhCliGitHub(GitHubBackend):
    def __init__(self, username: Optional[str] = None) -> None:
        self.username = username

    def repo_exists(self, owner: str, name: str) -> bool:
        r = run_cli(["gh", "repo", "view", f"{owner}/{name}"])
        return r.returncode == 0

    def create_repo(
        self, name: str, description="", private=False, auto_init=False, owner=None
    ) -> Optional[dict[str, str]]:
        cmd = [
            "gh",
            "repo",
            "create",
            name,
            "--source=.",
            "--remote=origin",
            "--private" if private else "--public",
        ]
        if description:
            cmd += ["--description", description]
        r = run_cli(cmd)
        if r.returncode != 0:
            if owner and self.repo_exists(owner, name):
                return {"ssh_url": f"git@github.com:{owner}/{name}.git"}
            return None
        return {"name": name, "ssh_url": f"git@github.com:{owner}/{name}.git"}


class RestGitHub(GitHubBackend):
    def __init__(self, token: str, username: Optional[str] = None) -> None:
        self.token = token
        self.username = username

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github.v3+json",
        }

    def repo_exists(self, owner: str, name: str) -> bool:
        import requests

        r = requests.get(
            f"https://api.github.com/repos/{owner}/{name}",
            headers=self._headers(),
        )
        return r.status_code == 200

    def create_repo(self, name: str, description="", private=False, auto_init=False, owner=None):
        import requests

        payload = {
            "name": name,
            "description": description,
            "private": private,
            "auto_init": auto_init,
        }
        r = requests.post(
            "https://api.github.com/user/repos",
            headers=self._headers(),
            json=payload,
        )
        if r.status_code == 201:
            return r.json()
        if r.status_code == 422 and owner:
            return {"ssh_url": f"git@github.com:{owner}/{name}.git"}
        with contextlib.suppress(Exception):
            print(f"REST create failed ({r.status_code}): {r.json().get('message')}")
        return None


class PyGithubGitHub(GitHubBackend):
    def __init__(self, token: str, username: Optional[str] = None) -> None:
        try:
            from github import Github
            from github.Auth import Token
        except ImportError as exc:
            msg = "pygithub backend requires `pip install PyGithub`"
            raise SystemExit(msg) from exc
        self.client = Github(auth=Token(token))
        self.username = username

    def repo_exists(self, owner: str, name: str) -> bool:
        try:
            user = self.client.get_user(owner) if owner else self.client.get_user()
            user.get_repo(name)
            return True
        except Exception:
            return False

    def create_repo(
        self, name: str, description="", private=False, auto_init=False, owner=None
    ) -> Optional[dict[str, str]]:
        try:
            user = self.client.get_user(owner) if owner else self.client.get_user()
            repo = user.create_repo(
                name=name,
                description=description,
                private=private,
                auto_init=auto_init,
            )
            return {
                "name": repo.name,
                "html_url": repo.html_url,
                "clone_url": repo.clone_url,
                "ssh_url": repo.ssh_url,
            }
        except Exception as exc:
            if "already exists" in str(exc).lower() and owner:
                return {"ssh_url": f"git@github.com:{owner}/{name}.git"}
            print(f"PyGithub create failed: {exc}")
            return None


class Github3GitHub(GitHubBackend):
    def __init__(self, token: str, username: Optional[str] = None) -> None:
        try:
            import github3
        except ImportError as exc:
            msg = "githubpython backend requires `pip install github3.py`"
            raise SystemExit(msg) from exc
        self.client = github3.login(token=token)
        self.username = username

    def repo_exists(self, owner: str, name: str) -> bool:
        try:
            return self.client.repository(owner, name) is not None
        except Exception:
            return False

    def create_repo(
        self, name: str, description="", private=False, auto_init=False, owner=None
    ) -> Optional[dict[str, str]]:
        try:
            repo = self.client.create_repo(
                name,
                description=description or None,
                private=private,
                auto_init=auto_init,
            )
            return {
                "name": repo.name,
                "html_url": repo.html_url,
                "clone_url": repo.clone_url,
                "ssh_url": repo.ssh_url,
            }
        except Exception as exc:
            if "already exists" in str(exc).lower() and owner:
                return {"ssh_url": f"git@github.com:{owner}/{name}.git"}
            print(f"github3.py create failed: {exc}")
            return None


@dataclass(slots=True)
class Backend:
    name: str
    git: GitBackend
    github: GitHubBackend


def make_backend(
    name: str,
    token: Optional[str],
    username: Optional[str],
) -> Backend:
    if name == "subprocess":
        return Backend(name, SubprocessGit(), GhCliGitHub(username))
    if name == "gitpython":
        return Backend(name, GitPythonGit(), GhCliGitHub(username))
    if name == "rest":
        if not token:
            msg = "Backend 'rest' requires GITHUB_TOKEN."
            raise SystemExit(msg)
        return Backend(name, SubprocessGit(), RestGitHub(token, username))
    if name == "pygithub":
        if not token:
            msg = "Backend 'pygithub' requires GITHUB_TOKEN."
            raise SystemExit(msg)
        return Backend(name, SubprocessGit(), PyGithubGitHub(token, username))
    if name == "githubpython":
        if not token:
            msg = "Backend 'githubpython' requires GITHUB_TOKEN."
            raise SystemExit(msg)
        return Backend(name, SubprocessGit(), Github3GitHub(token, username))
    if name == "dulwich":
        return Backend(name, DulwichGit(), GhCliGitHub(username))
    if name == "libgit2":
        return Backend(name, Pygit2Git(), GhCliGitHub(username))
    msg = f"Unknown backend: {name}"
    raise SystemExit(msg)


def resolve_backend(args: argparse.Namespace) -> Backend:
    env_file = Path(args.env_file).expanduser()
    token = get_github_token(env_file)
    return make_backend(args.backend, token, getattr(args, "github_username", None))


def cmd_api(args: argparse.Namespace) -> int:
    backend = resolve_backend(args)
    owner = args.github_username
    data = backend.github.create_repo(
        name=args.repo_name,
        description=args.description,
        private=args.private,
        auto_init=True,
        owner=owner,
    )
    if data is None:
        return 1
    print("✅ Repository created successfully!")
    print(f"📁 Name: {data.get('name', args.repo_name)}")
    print(f"🔗 URL: {data.get('html_url', f'https://github.com/{owner}/{args.repo_name}')}")
    print(f"📝 Clone URL: {data.get('clone_url', data.get('ssh_url', ''))}")
    return 0


def cmd_api_push(args: argparse.Namespace) -> int:
    backend = resolve_backend(args)
    cwd = Path.cwd()
    repo_name = args.name or cwd.name
    branch = args.branch
    username = args.github_username
    if not backend.git.is_git_repo(cwd):
        print("No git repository found. Creating new one...")
        backend.git.init_repo(cwd)
        print("Git repository initialized.")
    else:
        print("Existing git repository found.")
    if backend.git.is_dirty(cwd):
        backend.git.add_all(cwd)
        backend.git.commit(cwd, args.commit_message)
        print("Changes committed.")
    else:
        print("No changes to commit.")
    if backend.git.get_remote_url(cwd) is None:
        print("No remote 'origin' found. Creating GitHub repository...")
        data = backend.github.create_repo(
            name=repo_name,
            private=False,
            auto_init=False,
            owner=username,
        )
        if data is None:
            return 1
        ssh_url = data.get("ssh_url") or f"git@github.com:{username}/{repo_name}.git"
        backend.git.add_remote(cwd, "origin", ssh_url)
        print(f"Remote 'origin' created: {ssh_url}")
    else:
        print(f"Remote 'origin' already exists: {backend.git.get_remote_url(cwd)}")
    try:
        backend.git.push(cwd, "origin", branch, set_upstream=True)
        print(f"Successfully pushed to origin/{branch}")
    except Exception as exc:
        print(f"Push failed: {exc}")
        return 1
    print(f"✅ Repository '{repo_name}' is now on GitHub!")
    return 0


def cmd_gh_create(args: argparse.Namespace) -> int:
    backend = resolve_backend(args)
    cwd = Path.cwd()
    repo_name = args.name or cwd.name
    username = args.github_username
    global_gitignore = Path(args.global_gitignore).expanduser()
    local_gitignore = cwd / ".gitignore"
    if global_gitignore.exists() and not local_gitignore.exists():
        shutil.copy2(global_gitignore, local_gitignore)
        print(f"Copied {global_gitignore} -> {local_gitignore}")
    elif local_gitignore.exists():
        print(".gitignore already exists in current directory.")
    else:
        print(f"No global .gitignore found at {global_gitignore}")
    if backend.git.is_git_repo(cwd):
        print("Git repository already initialized.")
    else:
        print("Initializing git repository...")
        backend.git.init_repo(cwd)
    if backend.git.get_remote_url(cwd) is not None:
        print("Remote 'origin' already exists.")
        return 0
    exists = backend.github.repo_exists(username, repo_name)
    if exists:
        print(f"GitHub repo '{repo_name}' already exists on your account.")
        ssh_url = f"git@github.com:{username}/{repo_name}.git"
        backend.git.add_remote(cwd, "origin", ssh_url)
        print(f"Added remote origin: {ssh_url}")
    else:
        print(f"GitHub repo '{repo_name}' does not exist yet.")
        data = backend.github.create_repo(
            name=repo_name,
            private=False,
            auto_init=False,
            owner=username,
        )
        if data is None:
            return 1
        ssh_url = data.get("ssh_url") or f"git@github.com:{username}/{repo_name}.git"
        backend.git.add_remote(cwd, "origin", ssh_url)
        print(f"Added remote origin: {ssh_url}")
    backend.git.add_all(cwd)
    if backend.git.commit(cwd, args.commit_message):
        print("Committing changes...")
    else:
        print("No changes to commit.")
    branch = backend.git.current_branch(cwd)
    print(f"Pushing branch '{branch}'...")
    try:
        backend.git.push(cwd, "origin", branch, set_upstream=True)
    except Exception as exc:
        print(f"Push failed: {exc}")
        return 1
    print(f"\n✅ Success! Repository '{repo_name}' is on GitHub or updated there.")
    print(f"View it at: https://github.com/{repo_name}")
    return 0


def cmd_gh_cli(args: argparse.Namespace) -> int:
    backend = resolve_backend(args)
    cwd = Path.cwd()
    repo_name = args.name or cwd.name
    print(f"Repository name: {repo_name}")
    if not backend.git.is_git_repo(cwd):
        print("Initializing git repository...")
        backend.git.init_repo(cwd)
    else:
        print("Git repository already initialized.")
    origin_url = backend.git.get_remote_url(cwd)
    if origin_url is None:
        print(f"Creating GitHub repository '{repo_name}'...")
        data = backend.github.create_repo(
            name=repo_name,
            private=False,
            auto_init=False,
            owner=args.github_username,
        )
        if data is None:
            return 1
    else:
        print("Remote 'origin' already exists. Checking if repo exists on GitHub...")
        fetch = run_cli(["git", "fetch", "origin"], cwd=cwd)
        if fetch.returncode == 0:
            print("GitHub repository exists. Will push changes.")
        else:
            print("Remote exists but seems inaccessible. You might need to authenticate.")
            print(f"Remote URL: {origin_url}")
    print("Adding all files...")
    backend.git.add_all(cwd)
    if backend.git.is_dirty(cwd):
        print("Committing changes...")
        if not backend.git.commit(cwd, args.commit_message):
            print("Nothing to commit after staging.")
    else:
        print("No changes to commit.")
    print("Pushing to GitHub...")
    branch = backend.git.current_branch(cwd) or args.branch
    try:
        backend.git.push(cwd, "origin", branch, set_upstream=True)
    except Exception as exc:
        print(f"Push failed: {exc}")
        return 1
    print(f"\n✅ Success! Repository '{repo_name}' is now on GitHub.")
    print(f"View it at: https://github.com/{repo_name}")
    return 0


class GitHubRepoManager:
    def __init__(
        self,
        backend: Backend,
        repo_name: Optional[str],
        github_username: str,
        git_email: str,
        git_user: str,
        branch: str,
    ) -> None:
        self.backend = backend
        self.cwd = Path.cwd()
        self.repo_name = repo_name or self.cwd.name
        self.github_username = github_username
        self.git_email = git_email
        self.git_user = git_user
        self.branch = branch
        self.repo_url = f"https://github.com/{github_username}/{self.repo_name}.git"

    def _check_gh_cli_installed(self) -> bool:
        return run_cli(["gh", "--version"]).returncode == 0

    def _check_gh_authenticated(self) -> bool:
        return run_cli(["gh", "auth", "status"]).returncode == 0

    def _init_local_repo(self) -> None:
        print(f"\n📦 Initializing local git repository in {self.cwd}...")
        self.backend.git.init_repo(self.cwd)
        self.backend.git.config_user(self.cwd, self.git_user, self.git_email)
        print("✓ Local repository initialized")

    def _create_github_repo(self) -> bool:
        print(f"\n🌐 Creating repository on GitHub: {self.repo_name}...")
        if self.backend.github.repo_exists(self.github_username, self.repo_name):
            print(f"✓ Repository {self.repo_name} already exists on GitHub")
            return True
        data = self.backend.github.create_repo(
            name=self.repo_name,
            private=False,
            auto_init=False,
            owner=self.github_username,
        )
        if data is None:
            print("Error creating repository on GitHub")
            return False
        print("✓ Repository created on GitHub")
        return True

    def _ensure_content(self) -> bool:
        visible = [f for f in self.cwd.glob("*") if f.name != ".git"]
        hidden = [f for f in self.cwd.glob(".*") if f.name not in {".git", ".", ".."}]
        has_content = bool(visible or hidden)
        if not has_content:
            print("📄 No files found, creating initial README.md...")
            readme = self.cwd / "README.md"
            if not readme.exists():
                readme.write_text(f"# {self.repo_name}\nRepository initialized on {datetime.now():%Y-%m-%d %H:%M:%S}\n")
                print("✓ Created README.md")
                return True
        return has_content

    def _generate_commit_message(self) -> str:
        return datetime.now().strftime("Auto-commit: %Y-%m-%d %H:%M:%S")

    def _commit_changes(self, message: Optional[str] = None) -> bool:
        message = message or self._generate_commit_message()
        print(f"\n💾 Committing changes with message: '{message}'")
        ok = self.backend.git.commit(self.cwd, message)
        if not ok:
            print("⚠️  Nothing to commit")
            return False
        print("✓ Changes committed")
        return True

    def _add_remote(self) -> None:
        print(f"\n🔗 Adding remote: {self.repo_url}")
        current = self.backend.git.get_remote_url(self.cwd)
        if current:
            if current == self.repo_url:
                print("✓ Remote 'origin' already configured correctly")
                return
            print(f"Updating remote URL from {current} to {self.repo_url}")
            self.backend.git.set_remote_url(self.cwd, "origin", self.repo_url)
            return
        self.backend.git.add_remote(self.cwd, "origin", self.repo_url)
        print("✓ Remote added")

    def _push_to_github(self, branch: Optional[str] = None) -> None:
        branch = branch or self.branch
        print(f"\n🚀 Pushing to GitHub ({branch} branch)...")
        try:
            self.backend.git.push(self.cwd, "origin", branch, set_upstream=True)
            print("✓ Successfully pushed to GitHub")
        except NotImplementedError as exc:
            print(f"Backend does not support push: {exc}")
            raise
        except Exception as exc:
            msg = str(exc).lower()
            if "permission denied" in msg:
                print("Error: Permission denied. Check your GitHub credentials.")
                sys.exit(1)
            print(f"Warning: Push encountered an issue: {exc}")

    def _rename_branch_to_main(self) -> None:
        current = self.backend.git.current_branch(self.cwd)
        if current and current != self.branch:
            print(f"\n🔄 Renaming branch from '{current}' to '{self.branch}'...")
            self.backend.git.rename_branch(self.cwd, self.branch)

    def handle_existing_repo(self) -> bool:
        print(f"\n⚠️  Git repository already exists in {self.cwd}")
        current = self.backend.git.get_remote_url(self.cwd)
        print(f"Current remote: {current or 'None'}")
        while True:
            print("\nOptions:")
            print("1. Merge changes (stage and commit to current repo)")
            print("2. Create new repository (enter new name)")
            print("3. Exit")
            choice = input("\nSelect option (1-3): ").strip()
            if choice == "1":
                print("✓ Using existing repository")
                return True
            if choice == "2":
                new_name = input("Enter new repository name: ").strip()
                if not new_name:
                    print("Error: Repository name cannot be empty.")
                    continue
                self.repo_name = new_name
                self.repo_url = f"https://github.com/{self.github_username}/{self.repo_name}.git"
                print(f"✓ New repository name set: {self.repo_name}")
                return False
            if choice == "3":
                print("Exiting...")
                sys.exit(0)
            print("Invalid choice. Please select 1, 2, or 3.")

    def run(self, commit_message: Optional[str] = None) -> int:
        print("-" * 40)
        print(f"GitHub Repository Manager (backend: {self.backend.name})")
        print("-" * 40)
        print(f"Directory: {self.cwd}")
        print(f"Repository: {self.repo_name}")
        print(f"GitHub User: {self.github_username}")
        print(f"Email: {self.git_email}")
        print("-" * 40)
        if self.backend.name == "subprocess":
            if not self._check_gh_cli_installed():
                print("\n❌ Error: GitHub CLI (gh) is not installed.")
                return 1
            if not self._check_gh_authenticated():
                print("\n❌ Error: GitHub CLI is not authenticated.")
                return 1
            print("\n✓ GitHub CLI is installed and authenticated\n")
        if self.backend.git.is_git_repo(self.cwd):
            keep = self.handle_existing_repo()
            if not keep:
                self.backend.git.remove_remote(self.cwd, "origin")
        else:
            self._init_local_repo()
        self._ensure_content()
        print("\n📝 Staging all changes...")
        self.backend.git.add_all(self.cwd)
        print("✓ Changes staged")
        if not self._commit_changes(commit_message):
            print("\n⚠️  Could not commit changes.")
            return 1
        self._rename_branch_to_main()
        if not self._create_github_repo():
            print("\n❌ Failed to create repository on GitHub")
            return 1
        self._add_remote()
        self._push_to_github()
        print("\n" + "=" * 40)
        print("✅ Success! Repository created and pushed to GitHub")
        print(f"Repository URL: https://github.com/{self.github_username}/{self.repo_name}")
        print("-" * 40)
        return 0


def cmd_gh_managed(args: argparse.Namespace) -> int:
    try:
        backend = resolve_backend(args)
        manager = GitHubRepoManager(
            backend=backend,
            repo_name=args.name,
            github_username=args.github_username,
            git_email=args.git_email,
            git_user=args.git_user,
            branch=args.branch,
        )
        return manager.run(commit_message=args.message)
    except KeyboardInterrupt:
        print("\n\nExiting...")
        return 0
    except Exception as exc:
        print(f"\nUnexpected error: {exc}")
        traceback.print_exc()
        return 1


def add_common_backend_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "-b",
        "--backend",
        choices=BACKEND_CHOICES,
        default="subprocess",
        help="Backend for git and GitHub operations. Default: subprocess.",
    )
    p.add_argument(
        "--env-file",
        default=str(DEFAULT_ENV_FILE),
        help=f"Path to env file with GITHUB_TOKEN. Default: {DEFAULT_ENV_FILE}",
    )
    p.add_argument(
        "--github-username",
        default=DEFAULT_GITHUB_USERNAME,
        help=f"GitHub username. Default: {DEFAULT_GITHUB_USERNAME}.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gh_repo.py",
        description="Create a GitHub repository from a local project and push code.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("api", help="Create a repo via API (mkghrepo.py).")
    p.add_argument("repo_name", help="Name of the new repository.")
    p.add_argument(
        "description",
        nargs="?",
        default=DEFAULT_DESCRIPTION,
        help=f"Repository description. Default: '{DEFAULT_DESCRIPTION}'.",
    )
    p.add_argument("--private", action="store_true", help="Make the repo private.")
    add_common_backend_args(p)
    p.set_defaults(func=cmd_api)
    p = sub.add_parser("api-push", help="Init/commit + API create + push (new_repo.py).")
    p.add_argument("-n", "--name", help="Repository name. Default: current directory name.")
    p.add_argument(
        "--branch",
        default=DEFAULT_BRANCH,
        help=f"Branch to push. Default: {DEFAULT_BRANCH}.",
    )
    p.add_argument(
        "--commit-message",
        default=DEFAULT_API_PUSH_COMMIT_MESSAGE,
        help=f"Commit message. Default: '{DEFAULT_API_PUSH_COMMIT_MESSAGE}'.",
    )
    add_common_backend_args(p)
    p.set_defaults(func=cmd_api_push)
    p = sub.add_parser("gh-create", help="PyGithub/gh-CLI hybrid create+push (new_repo2.py).")
    p.add_argument("-n", "--name", help="Repository name. Default: current directory name.")
    p.add_argument(
        "--global-gitignore",
        default=str(DEFAULT_GLOBAL_GITIGNORE),
        help=f"Path to global .gitignore. Default: {DEFAULT_GLOBAL_GITIGNORE}.",
    )
    p.add_argument(
        "--commit-message",
        default=DEFAULT_GH_CREATE_COMMIT_MESSAGE,
        help=f"Commit message. Default: '{DEFAULT_GH_CREATE_COMMIT_MESSAGE}'.",
    )
    add_common_backend_args(p)
    p.set_defaults(func=cmd_gh_create)
    p = sub.add_parser("gh-cli", help="Pure gh CLI + git subprocess workflow (newrepo.py).")
    p.add_argument("-n", "--name", help="Repository name. Default: current directory name.")
    p.add_argument(
        "--branch",
        default=DEFAULT_BRANCH,
        help=f"Fallback branch. Default: {DEFAULT_BRANCH}.",
    )
    p.add_argument(
        "--commit-message",
        default=DEFAULT_GH_CLI_COMMIT_MESSAGE,
        help=f"Commit message. Default: '{DEFAULT_GH_CLI_COMMIT_MESSAGE}'.",
    )
    add_common_backend_args(p)
    p.set_defaults(func=cmd_gh_cli)
    p = sub.add_parser("gh-managed", help="Interactive manager with prompts & README (pynewrepo.py).")
    p.add_argument("-n", "--name", help="Repository name. Default: current directory name.")
    p.add_argument(
        "-m",
        "--message",
        help="Custom commit message. Default: auto-generated with timestamp.",
    )
    p.add_argument(
        "--git-email",
        default=DEFAULT_GIT_EMAIL,
        help=f"Git user email. Default: {DEFAULT_GIT_EMAIL}.",
    )
    p.add_argument(
        "--git-user",
        default=DEFAULT_GIT_USER,
        help=f"Git user name. Default: {DEFAULT_GIT_USER}.",
    )
    p.add_argument(
        "--branch",
        default=DEFAULT_BRANCH,
        help=f"Main branch name. Default: {DEFAULT_BRANCH}.",
    )
    add_common_backend_args(p)
    p.set_defaults(func=cmd_gh_managed)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
