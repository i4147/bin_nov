#!/data/data/com.termux/files/usr/bin/env python
"""Download a GitHub repository as a ZIP archive.
Normalises ``owner/repo`` or a full URL, resolves the default branch and the size through the API (asking before the large repositories) and downloads the codeload ZIP with subprocess, PyGithub, GitPython or Dulwich (``-b``).
Usage: ghzip owner/repo [-b BACKEND] Positional arguments: repo Repository in owner/repo or full GitHub URL form Options: -b, --backend Backend to use Examples: $ ghzip psf/requests Command-line script: run ``python ghzip.py`` or the ``ghzip`` command.
The module is importable as well."""

import argparse
import functools
import json
import operator
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Optional
from urllib.parse import urlparse


CURRENT_DIR = Path.cwd()
HOME_ENV = Path.home() / ".env"
DEFAULT_BACKEND: str = "subprocess"
SIZE_THRESHOLD: int = 5 * 1024 * 1024


def load_env_token() -> Optional[str]:
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    if HOME_ENV.exists():
        try:
            from dotenv import load_dotenv

            load_dotenv(HOME_ENV)
            return os.environ.get("GITHUB_TOKEN")
        except Exception:
            pass
    return None


def normalize_repo(spec: str) -> tuple[str, str]:
    s = spec.strip().rstrip("/")
    s = s.removesuffix(".git")
    if s.startswith(("http://", "https://")):
        p = urlparse(s)
        parts = [x for x in p.path.split("/") if x]
        if len(parts) < 2:
            msg = f"Invalid GitHub repo URL: {spec}"
            raise ValueError(msg)
        owner, repo = parts[0], parts[1]
    else:
        parts = [x for x in s.split("/") if x]
        if len(parts) != 2:
            msg = "Repo must be in owner/repo form or a GitHub URL"
            raise ValueError(msg)
        owner, repo = parts
    return owner, repo


def repo_zip_name(owner: str, repo: str, branch: str) -> Path:
    return CURRENT_DIR / f"{repo}-{branch}.zip"


def github_api_headers(token: Optional[str]) -> list[str]:
    headers = [
        "Accept: application/vnd.github+json",
        "X-GitHub-Api-Version: 2022-11-28",
    ]
    if token:
        headers.append(f"Authorization: Bearer {token}")
    return functools.reduce(operator.iadd, (["-H", h] for h in headers), [])


def fetch_default_branch_subprocess(owner: str, repo: str, token: Optional[str]) -> str:
    api = f"https://api.github.com/repos/{owner}/{repo}"
    if shutil.which("gh"):
        cmd = ["gh", "api", api, "--jq", ".default_branch"]
        env = os.environ.copy()
        if token:
            env["GITHUB_TOKEN"] = token
        out = subprocess.check_output(cmd, text=True, env=env).strip()
        return out
    curl = shutil.which("curl")
    if curl:
        cmd = [curl, "-fsSL", *github_api_headers(token), api]
        out = subprocess.check_output(cmd, text=True)
        return json.loads(out)["default_branch"]
    msg = "Neither gh nor curl is available for branch lookup"
    raise RuntimeError(msg)


def fetch_repo_size_subprocess(owner: str, repo: str, token: Optional[str]) -> Optional[int]:
    api = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        if shutil.which("gh"):
            cmd = ["gh", "api", api, "--jq", ".size"]
            env = os.environ.copy()
            if token:
                env["GITHUB_TOKEN"] = token
            out = subprocess.check_output(cmd, text=True, env=env).strip()
            return int(out) * 1024
        curl = shutil.which("curl")
        if curl:
            cmd = [curl, "-fsSL", *github_api_headers(token), api]
            out = subprocess.check_output(cmd, text=True)
            return int(json.loads(out)["size"]) * 1024
    except Exception:
        return None
    return None


def maybe_confirm(size_bytes: Optional[int]) -> bool:
    if size_bytes is None:
        return True
    if size_bytes < SIZE_THRESHOLD:
        return True
    prompt = f"Repo looks larger than 5 MB ({size_bytes / (1024 * 1024):.2f} MB). Download anyway? [y/N]: "
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            return input(prompt).strip().lower() in {"y", "yes"}
    except Exception:
        pass
    return True


def download_zip_subprocess(owner: str, repo: str, branch: str, out_path: Path, token: Optional[str]) -> None:
    url = f"https://github.com/{owner}/{repo}/archive/refs/heads/{branch}.zip"
    if shutil.which("gh"):
        cmd = [
            "gh",
            "api",
            f"/repos/{owner}/{repo}/zipball/{branch}",
            "-H",
            "Accept: application/vnd.github+json",
        ]
        env = os.environ.copy()
        if token:
            env["GITHUB_TOKEN"] = token
        with open(out_path, "wb") as f:
            subprocess.run(cmd, check=True, env=env, stdout=f)
        return
    headers = []
    if token:
        headers = ["-H", f"Authorization: Bearer {token}"]
    if shutil.which("curl"):
        cmd = ["curl", "-fL", *headers, "-o", str(out_path), url]
        env = os.environ.copy()
        if token:
            env["GITHUB_TOKEN"] = token
        subprocess.run(cmd, check=True, env=env)
        return
    if shutil.which("wget"):
        cmd = ["wget", "-O", str(out_path), url]
        env = os.environ.copy()
        if token:
            env["GITHUB_TOKEN"] = token
        subprocess.run(cmd, check=True, env=env)
        return
    msg = "No supported subprocess downloader found (gh/curl/wget)"
    raise RuntimeError(msg)


def download_zip_pygithub(owner: str, repo: str, branch: str, out_path: Path, token: Optional[str]) -> bool:
    try:
        from github import Github
    except Exception:
        return False
    gh = Github(token) if token else Github()
    r = gh.get_repo(f"{owner}/{repo}")
    url = r.get_archive_link("zipball", ref=branch)
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "python"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req) as resp, open(out_path, "wb") as f:
        shutil.copyfileobj(resp, f)
    return True


def download_zip_gitpython(owner: str, repo: str, branch: str, out_path: Path, token: Optional[str]) -> bool:
    try:
        from git import Repo
    except Exception:
        return False
    base = f"https://github.com/{owner}/{repo}.git"
    tmp = CURRENT_DIR / f".tmp-{repo}-{branch}"
    if tmp.exists():
        shutil.rmtree(tmp)
    Repo.clone_from(base, tmp, branch=branch, depth=1)
    shutil.make_archive(str(out_path.with_suffix("")), "zip", tmp)
    shutil.rmtree(tmp, ignore_errors=True)
    return True


def download_zip_dulwich(owner: str, repo: str, branch: str, out_path: Path, token: Optional[str]) -> bool:
    try:
        from dulwich import porcelain
    except Exception:
        return False
    tmp = CURRENT_DIR / f".tmp-{repo}-{branch}-dulwich"
    if tmp.exists():
        shutil.rmtree(tmp)
    porcelain.clone(f"https://github.com/{owner}/{repo}.git", str(tmp), checkout=True)
    shutil.make_archive(str(out_path.with_suffix("")), "zip", tmp)
    shutil.rmtree(tmp, ignore_errors=True)
    return True


def resolve_backend(name: str) -> str:
    name = (name or DEFAULT_BACKEND).lower()
    allowed = {"subprocess", "pygithub", "gitpython", "dulwich", "typer"}
    if name not in allowed:
        msg = f"Unsupported backend: {name}"
        raise ValueError(msg)
    return name


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Download a GitHub repository as a ZIP archive")
    p.add_argument("repo", help="Repository in owner/repo or full GitHub URL form")
    p.add_argument(
        "-b",
        "--backend",
        default=DEFAULT_BACKEND,
        choices=["subprocess", "pygithub", "gitpython", "dulwich", "typer"],
        help="Backend to use",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    token = load_env_token()
    owner, repo = normalize_repo(args.repo)
    backend = resolve_backend(args.backend)
    try:
        size = fetch_repo_size_subprocess(owner, repo, token)
    except Exception:
        size = None
    if not maybe_confirm(size):
        print("Skipped by user choice.")
        return 0
    try:
        branch = fetch_default_branch_subprocess(owner, repo, token)
    except Exception:
        branch = "main"
    out_path = repo_zip_name(owner, repo, branch)
    if out_path.exists():
        out_path.unlink()
    downloaded = False
    if backend == "pygithub":
        downloaded = download_zip_pygithub(owner, repo, branch, out_path, token)
    elif backend == "gitpython":
        downloaded = download_zip_gitpython(owner, repo, branch, out_path, token)
    elif backend == "dulwich":
        downloaded = download_zip_dulwich(owner, repo, branch, out_path, token)
    elif backend == "typer":
        downloaded = False
    if not downloaded:
        download_zip_subprocess(owner, repo, branch, out_path, token)
    print(str(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
