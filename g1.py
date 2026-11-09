#!/data/data/com.termux/files/usr/bin/env python
"""Clone a GitHub repository through a pluggable backend.
Accepts ``owner/repo``, HTTPS or SSH identifiers, resolves the default branch and the size through the GitHub API (``--token`` raises the rate limit), warns about the large repositories, can clone shallowly (``-d``) and initialises the submodules; ``--backend`` picks subprocess, Dulwich, GitPython, libgit2 or typer.
Usage: g1 REPOSITORY [-d] [--backend NAME] [--token TOKEN] Positional arguments: repository_url Repository identifier or URL (owner/repo, HTTPS, or SSH).
Options: --token GitHub personal access token (increases rate limit).
-d, --depth Perform a shallow clone with depth 1.
Without this flag the full history is cloned.
-b, --backend Examples: $ g1 psf/requests -d Command-line script: run ``python g1.py`` or the ``g1`` command.
The module is importable as well."""

import argparse
from pathlib import Path
import shutil
import subprocess
from typing import TYPE_CHECKING, Final, Optional, Protocol

from github import Github
from github.GithubException import GithubException, UnknownObjectException
from github.Repository import Repository
from loguru import logger


LARGE_REPO_THRESHOLD_MB: Final[float] = 5.0
DEFAULT_BRANCH_FALLBACK: Final[str] = "master"
GITHUB_SSH_PREFIX: Final[str] = "git@github.com:"
GITHUB_HTTP_PREFIXES: Final[tuple[str, ...]] = ("http://", "https://")
GITHUB_HOST: Final[str] = "github.com/"
DEFAULT_CLONE_DEPTH: Final[int] = 1
GITMODULES_FILENAME: Final[str] = ".gitmodules"
BACKEND_GH: Final[str] = "gh"
BACKEND_GIT: Final[str] = "git"
BACKEND_DULWICH: Final[str] = "dulwich"
BACKEND_GITPYTHON: Final[str] = "gitpython"
BACKEND_LIBGIT2: Final[str] = "libgit2"
BACKEND_TYPER: Final[str] = "typer"
KNOWN_BACKENDS: Final[tuple[str, ...]] = (
    BACKEND_GH,
    BACKEND_GIT,
    BACKEND_DULWICH,
    BACKEND_GITPYTHON,
    BACKEND_LIBGIT2,
    BACKEND_TYPER,
)
DEFAULT_BACKEND: Final[str] = BACKEND_GH


class CloneBackend(Protocol):
    name: str

    def clone(
        self,
        clone_url: str,
        target: Path,
        branch: str,
        depth: Optional[int],
    ) -> None: ...
    def update_submodules(self, repo_root: Path) -> None: ...


def _run_subprocess(
    cmd: list[str],
    cwd: Optional[Path] = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    logger.debug(f"Running: {' '.join(cmd)} (cwd={cwd})")
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=check,
        capture_output=True,
        text=True,
    )


def _git_available() -> bool:
    return shutil.which("git") is not None


def _gh_available() -> bool:
    return shutil.which("gh") is not None


class SubprocessBackend:
    name: str = BACKEND_GH

    def __init__(self, prefer_gh: bool = True) -> None:
        self.prefer_gh = prefer_gh and _gh_available()

    def clone(
        self,
        clone_url: str,
        target: Path,
        branch: str,
        depth: Optional[int],
    ) -> None:
        if target.exists() and any(target.iterdir()):
            msg = f"Target directory already exists and is not empty: {target}"
            raise Exception(msg)
        if self.prefer_gh:
            gh_cmd: list[str] = [
                "gh",
                "repo",
                "clone",
                clone_url,
                str(target),
                "--",
                "--branch",
                branch,
            ]
            if depth is not None:
                gh_cmd.extend(["--depth", str(depth)])
            try:
                _run_subprocess(gh_cmd)
                return
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                logger.warning(f"gh clone failed, falling back to git: {e}")
        if not _git_available():
            msg = "Neither 'gh' nor 'git' is available on PATH."
            raise Exception(msg)
        git_cmd: list[str] = ["git", "clone", clone_url, str(target)]
        git_cmd.extend(["--branch", branch])
        if depth is not None:
            git_cmd.extend(["--depth", str(depth)])
        _run_subprocess(git_cmd)

    def update_submodules(self, repo_root: Path) -> None:
        if not _git_available():
            msg = "'git' is not available on PATH."
            raise Exception(msg)
        _run_subprocess(
            ["git", "submodule", "update", "--init", "--recursive"],
            cwd=repo_root,
        )


class DulwichBackend:
    name: str = BACKEND_DULWICH

    def clone(
        self,
        clone_url: str,
        target: Path,
        branch: str,
        depth: Optional[int],
    ) -> None:
        from dulwich import porcelain

        porcelain.clone(
            source=clone_url,
            target=str(target),
            branch=branch.encode("utf-8"),
            depth=depth,
        )

    def update_submodules(self, repo_root: Path) -> None:
        from dulwich import porcelain

        porcelain.submodule_update(root=str(repo_root), recursive=True)


class GitPythonBackend:
    name: str = BACKEND_GITPYTHON

    def clone(
        self,
        clone_url: str,
        target: Path,
        branch: str,
        depth: Optional[int],
    ) -> None:
        from git import Repo

        kwargs: dict[str, object] = {"branch": branch}
        if depth is not None:
            kwargs["depth"] = depth
            kwargs["single_branch"] = True
        Repo.clone_from(clone_url, str(target), **kwargs)

    def update_submodules(self, repo_root: Path) -> None:
        from git import Repo

        repo = Repo(str(repo_root))
        for submodule in repo.submodules:
            submodule.update(init=True, recursive=True)


class Libgit2Backend:
    name: str = BACKEND_LIBGIT2

    def clone(
        self,
        clone_url: str,
        target: Path,
        branch: str,
        depth: Optional[int],
    ) -> None:
        if depth is not None:
            msg = "pygit2/libgit2 does not support shallow clones; falling back to subprocess git."
            raise NotImplementedError(msg)
        import pygit2

        pygit2.clone_repository(clone_url, str(target), checkout_branch=branch)

    def update_submodules(self, repo_root: Path) -> None:
        msg = "pygit2 does not expose recursive submodule update; falling back to subprocess git."
        raise NotImplementedError(msg)


class TyperBackend:
    name: str = BACKEND_TYPER

    def __init__(self) -> None:
        self._fallback = SubprocessBackend(prefer_gh=False)

    def clone(
        self,
        clone_url: str,
        target: Path,
        branch: str,
        depth: Optional[int],
    ) -> None:
        self._fallback.clone(clone_url, target, branch, depth)

    def update_submodules(self, repo_root: Path) -> None:
        self._fallback.update_submodules(repo_root)


def create_backend(name: str) -> CloneBackend:
    name = name.lower()
    if name == BACKEND_GH:
        return SubprocessBackend(prefer_gh=True)
    if name == BACKEND_GIT:
        return SubprocessBackend(prefer_gh=False)
    if name == BACKEND_DULWICH:
        return DulwichBackend()
    if name == BACKEND_GITPYTHON:
        return GitPythonBackend()
    if name == BACKEND_LIBGIT2:
        return Libgit2Backend()
    if name == BACKEND_TYPER:
        return TyperBackend()
    msg = f"Unknown backend: {name}"
    raise ValueError(msg)


def get_github_client(token: Optional[str] = None) -> Github:
    if token:
        return Github(token)
    return Github()


def parse_repo_url(txt: str) -> tuple[str, str]:
    txt = txt.strip()
    txt = txt.removesuffix(".git")
    if txt.startswith(GITHUB_SSH_PREFIX):
        txt = txt.replace(GITHUB_SSH_PREFIX, "")
    if txt.startswith(GITHUB_HTTP_PREFIXES):
        txt = txt.split(GITHUB_HOST, 1)[-1]
    parts = txt.split("/")
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    msg = f"Invalid repository format: {txt}"
    raise ValueError(msg)


def get_repo(repo_url: str, github_client: Github) -> Repository:
    try:
        owner, repo_name = parse_repo_url(repo_url)
        print(f"Fetching repository: {owner}/{repo_name}")
        repo = github_client.get_user(owner).get_repo(repo_name)
        _ = repo.size
        print(f"Repository found: {repo.full_name}")
        return repo
    except UnknownObjectException:
        msg = f"Repository not found: {repo_url}"
        raise ValueError(msg)
    except GithubException as e:
        msg = f"GitHub API error: {e.status} {e.data}"
        raise Exception(msg)


def get_repo_size(repo: Repository) -> float:
    try:
        size_kb = repo.size
        size_mb = size_kb / 1024
        print(f"Repository size: {size_mb:.2f} MB")
        return size_mb
    except Exception as e:
        logger.error(f"Could not fetch repo size: {e}")
        return 0.0


def get_default_branch(repo: Repository) -> str:
    try:
        default_branch = repo.default_branch
        print(f"Default branch: {default_branch}")
        return default_branch
    except Exception as e:
        logger.warning(f"Could not determine default branch: {e}")
        return "main"


def build_clone_url(repo: Repository) -> str:
    return repo.clone_url


def resolve_clone_target(clone_url: str) -> Path:
    name = Path(clone_url.rstrip("/").removesuffix(".git")).name
    return Path.cwd() / name


def clone_repo(
    clone_url: str,
    branch: str,
    depth: Optional[int],
    backend: CloneBackend,
) -> Path:
    depth_msg = f"depth={depth}" if depth is not None else "full history"
    print(f"Cloning repository from {clone_url} (branch: {branch}, {depth_msg}, backend: {backend.name})")
    target_path = resolve_clone_target(clone_url)
    try:
        backend.clone(clone_url, target_path, branch, depth)
        print(f"Clone completed successfully at {target_path}.")
        return target_path
    except NotImplementedError as e:
        logger.warning(f"Backend '{backend.name}' cannot clone shallow: {e}")
    except Exception as e:
        logger.warning(f"Backend '{backend.name}' clone failed: {e}")
    if not _git_available():
        msg = f"Backend '{backend.name}' failed and 'git' is not available for fallback."
        raise Exception(msg)
    logger.info("Falling back to subprocess git for clone.")
    fallback = SubprocessBackend(prefer_gh=False)
    try:
        fallback.clone(clone_url, target_path, branch, depth)
        print(f"Clone completed via fallback git at {target_path}.")
        return target_path
    except Exception as e:
        msg = f"[ERROR] Clone failed: {e}"
        raise Exception(msg)


def has_submodules(repo_path: Path) -> bool:
    if (repo_path / GITMODULES_FILENAME).is_file():
        return True
    try:
        for candidate in repo_path.rglob(GITMODULES_FILENAME):
            if candidate.is_file():
                return True
    except OSError as e:
        logger.warning(f"Error scanning for submodules: {e}")
    return False


def _subprocess_update_submodules(repo_root: Path) -> None:
    if not _git_available():
        msg = "'git' is not available for submodule update."
        raise Exception(msg)
    try:
        _run_subprocess(
            ["git", "submodule", "update", "--init", "--recursive"],
            cwd=repo_root,
        )
    except subprocess.CalledProcessError as e:
        msg = f"Submodule update failed in {repo_root}: {e.stderr or e}"
        raise Exception(msg)


def _update_submodules_recursive(repo_root: Path, backend: CloneBackend) -> None:
    processed: set[Path] = set()
    pending: list[Path] = [repo_root]
    while pending:
        current_root = pending.pop()
        if current_root in processed:
            continue
        processed.add(current_root)
        if not has_submodules(current_root):
            continue
        print(f"Updating submodules in {current_root}...")
        try:
            backend.update_submodules(current_root)
            print(f"Submodules updated in {current_root} via {backend.name}.")
        except NotImplementedError as e:
            logger.warning(f"Backend '{backend.name}' cannot update submodules: {e}. Using subprocess git.")
            _subprocess_update_submodules(current_root)
            print(f"Submodules updated in {current_root} via fallback git.")
        except Exception as e:
            logger.warning(f"Backend '{backend.name}' submodule update failed: {e}. Trying subprocess git.")
            try:
                _subprocess_update_submodules(current_root)
                print(f"Submodules updated in {current_root} via fallback git.")
            except Exception as e2:
                msg = f"Submodule update failed in {current_root}: {e2}"
                raise Exception(msg)
        for sub in current_root.iterdir():
            if not sub.is_dir():
                continue
            if sub in processed:
                continue
            if has_submodules(sub):
                pending.append(sub)


def init_submodules(repo_path: Path, backend: CloneBackend) -> None:
    if not has_submodules(repo_path):
        print("No submodules found.")
        return
    print("Submodules found. Initialize and update? (y/n)")
    if input().lower() != "y":
        print("Submodule initialization skipped.")
        return
    try:
        _update_submodules_recursive(repo_path, backend)
    except Exception as e:
        msg = f"Submodule update failed: {e}"
        raise Exception(msg)


def confirm_large_repo(size_mb: float) -> bool:
    if size_mb > LARGE_REPO_THRESHOLD_MB:
        logger.warning(f"Repository size is {size_mb:.2f} MB. Continue? (y/n)")
        return input().lower() == "y"
    return True


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="script.py",
        description=("Clone a GitHub repository using a pluggable backend."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  script.py owner/repo\n"
            "  script.py https://github.com/owner/repo\n"
            "  script.py git@github.com:owner/repo.git -d\n"
            "  script.py owner/repo -b dulwich\n"
            "  script.py owner/repo -b gitpython --token YOUR_TOKEN\n"
            "\n"
            f"Backends: {', '.join(KNOWN_BACKENDS)} "
            f"(default: {DEFAULT_BACKEND})\n"
        ),
    )
    parser.add_argument(
        "repository_url",
        help="Repository identifier or URL (owner/repo, HTTPS, or SSH).",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="GitHub personal access token (increases rate limit).",
    )
    parser.add_argument(
        "-d",
        "--depth",
        action="store_true",
        help=("Perform a shallow clone with depth 1. Without this flag the full history is cloned."),
    )
    parser.add_argument(
        "-b",
        "--backend",
        default=DEFAULT_BACKEND,
        choices=KNOWN_BACKENDS,
        help=(f"Clone backend to use. Choices: {', '.join(KNOWN_BACKENDS)}. Default: {DEFAULT_BACKEND}."),
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    repo_url: str = args.repository_url.strip()
    token: Optional[str] = args.token
    depth: Optional[int] = DEFAULT_CLONE_DEPTH if args.depth else None
    backend_name: str = args.backend
    try:
        backend = create_backend(backend_name)
    except ValueError as e:
        logger.error(f"{e}")
        return 1
    print(f"Using backend: {backend.name}")
    try:
        github_client = get_github_client(token)
        if token:
            print(f"Authenticated as: {github_client.get_user().login}")
    except GithubException as e:
        logger.error(f"Authentication failed: {e}")
        return 1
    try:
        repo = get_repo(repo_url, github_client)
    except (ValueError, Exception) as e:
        logger.error(f"{e}")
        return 1
    size_mb = get_repo_size(repo)
    if not confirm_large_repo(size_mb):
        print("Aborted by user.")
        return 0
    default_branch = get_default_branch(repo)
    clone_url = build_clone_url(repo)
    try:
        repo_path = clone_repo(clone_url, default_branch, depth, backend)
    except Exception as e:
        if "not found" in str(e).lower() or "fatal:" in str(e):
            alt_branch = DEFAULT_BRANCH_FALLBACK if default_branch == "main" else "main"
            logger.warning(f"Branch '{default_branch}' failed, trying '{alt_branch}'...")
            try:
                repo_path = clone_repo(clone_url, alt_branch, depth, backend)
            except Exception as e2:
                logger.error(f"Clone with both branches failed: {e2}")
                return 1
        else:
            logger.error(f"{e}")
            return 1
    try:
        init_submodules(repo_path, backend)
    except Exception as e:
        logger.warning(f"Submodule handling failed: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
