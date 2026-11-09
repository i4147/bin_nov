#!/data/data/com.termux/files/usr/bin/env python
"""Squash the last N commits with a saved patch, reset and re-apply.
Combines the patches of the last N commits into one file plus a metadata JSON, resets the branch by N commits, applies the patch again and commits the message of the newest commit; ``--dry-run`` shows the plan and ``--force`` stashes the uncommitted changes first.
Usage: git_squash_n N [--dry-run] [--force] Positional arguments: N Number of commits to squash (e.g., 3) Options: -b, --backend Backend to use (some backends will fall back to subprocess for missing features) --patch-file Path to write combined patch --meta-file Path to write metadata JSON --force Stash uncommitted changes and proceed --dry-run Show actions without making changes Examples: $ git_squash_n 3 --dry-run Command-line script: run ``python git_squash_n.py`` or the ``git_squash_n`` command.
The module is importable as well."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


def run(cmd: str, cwd=None, capture=False, check=True) -> subprocess.CompletedProcess[str]:
    if capture:
        res = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if check and res.returncode != 0:
            raise subprocess.CalledProcessError(res.returncode, cmd, output=res.stdout, stderr=res.stderr)
        return res
    else:
        res = subprocess.run(cmd, cwd=cwd)
        if check and res.returncode != 0:
            raise subprocess.CalledProcessError(res.returncode, cmd)
        return res


def is_git_repo(path=".") -> bool:
    try:
        run(["git", "rev-parse", "--git-dir"], cwd=path, capture=True)
        return True
    except Exception:
        return False


class Backend:
    def __init__(self, repo_path=".") -> None:
        self.repo_path = Path(repo_path).absolute()
        self.fallback = SubprocessBackend(self.repo_path)

    def ensure_clean_worktree(self, force=False) -> tuple[bool,]:
        return self.fallback.ensure_clean_worktree(force)

    def get_head(self) -> str:
        return self.fallback.get_head()

    def get_last_n_commits(self, n: int):
        return self.fallback.get_last_n_commits(n)

    def write_combined_diff(self, base_rev: Any, head_rev: Any, out_patch_path: Path):
        return self.fallback.write_combined_diff(base_rev, head_rev, out_patch_path)

    def write_commits_metadata(self, base_rev: Any, head_rev: Any, out_meta_path: Path):
        return self.fallback.write_commits_metadata(base_rev, head_rev, out_meta_path)

    def reset_hard(self, rev: str) -> bool:
        return self.fallback.reset_hard(rev)

    def apply_patch_index(self, patch_path: Path) -> bool:
        return self.fallback.apply_patch_index(patch_path)

    def add_all(self):
        return self.fallback.add_all()

    def commit(self, message: str, allow_empty=False):
        return self.fallback.commit(message, allow_empty)

    def rev_parse(self, rev: str) -> str:
        return self.fallback.rev_parse(rev)


class SubprocessBackend(Backend):
    def __init__(self, repo_path=".") -> None:
        self.repo_path = Path(repo_path).absolute()

    def ensure_clean_worktree(self, force=False) -> tuple[bool, bool]:
        st = run(["git", "status", "--porcelain"], cwd=self.repo_path, capture=True)
        dirty = bool(st.stdout.strip())
        stash_made = False
        if dirty and not force:
            msg = "Working tree is dirty. Commit or use --force to stash changes before running."
            raise RuntimeError(msg)
        if dirty and force:
            run(
                [
                    "git",
                    "stash",
                    "push",
                    "--include-untracked",
                    "-m",
                    "git_squash_n auto-stash",
                ],
                cwd=self.repo_path,
            )
            stash_made = True
        return (not dirty, stash_made)

    def get_head(self) -> str:
        res = run(["git", "rev-parse", "HEAD"], cwd=self.repo_path, capture=True)
        return res.stdout.strip()

    def rev_parse(self, rev: str) -> str:
        res = run(["git", "rev-parse", rev], cwd=self.repo_path, capture=True)
        return res.stdout.strip()

    def get_last_n_commits(self, n: int) -> list[str]:
        if n <= 0:
            return []
        res = run(
            ["git", "rev-list", "--max-count={}".format(n), "--reverse", "HEAD"],
            cwd=self.repo_path,
            capture=True,
        )
        shas = [s.strip() for s in res.stdout.splitlines() if s.strip()]
        if len(shas) < n:
            msg = f"Repository has fewer than {n} commits."
            raise RuntimeError(msg)
        return shas

    def write_combined_diff(self, base_rev: str, head_rev: str, out_patch_path: Path):
        with open(out_patch_path, "wb") as f:
            p = subprocess.Popen(
                ["git", "diff", "--binary", f"{base_rev}..{head_rev}"],
                cwd=self.repo_path,
                stdout=subprocess.PIPE,
            )
            out, _ = p.communicate()
            if p.returncode != 0:
                raise subprocess.CalledProcessError(
                    p.returncode, ["git", "diff", "--binary", f"{base_rev}..{head_rev}"]
                )
            f.write(out)
        return out_patch_path

    def write_commits_metadata(self, base_rev: str, head_rev: str, out_meta_path: Path):
        git_log_fmt = "---%n%H|%an|%ae|%ad|%s"
        res = run(
            [
                "git",
                "log",
                "--reverse",
                f"--pretty=format:{git_log_fmt}",
                "--name-only",
                f"{base_rev}..{head_rev}",
            ],
            cwd=self.repo_path,
            capture=True,
        )
        text = res.stdout
        commits = []
        current = None
        for line in text.splitlines():
            if line.startswith("---"):
                if current:
                    commits.append(current)
                current = {}
                continue
            if current is not None and "|" in line and current.get("sha") is None:
                parts = line.split("|", 4)
                current["sha"] = parts[0]
                current["author_name"] = parts[1]
                current["author_email"] = parts[2]
                current["date"] = parts[3]
                current["subject"] = parts[4] if len(parts) > 4 else ""
                current["files"] = []
            elif current is not None and line.strip():
                current["files"].append(line.strip())
        if current:
            commits.append(current)
        meta = {
            "base": base_rev,
            "head": head_rev,
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "commit_count": len(commits),
            "commits": commits,
        }
        with open(out_meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        return out_meta_path

    def reset_hard(self, rev: str) -> bool:
        run(["git", "reset", "--hard", rev], cwd=self.repo_path)
        return True

    def apply_patch_index(self, patch_path: Path) -> bool:
        res = run(
            ["git", "apply", "--index", patch_path],
            cwd=self.repo_path,
            capture=True,
            check=False,
        )
        if res.returncode == 0:
            return True
        res2 = run(["git", "apply", patch_path], cwd=self.repo_path, capture=True, check=False)
        if res2.returncode == 0:
            return True
        msg = f"Failed to apply patch: git apply failed. stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
        raise RuntimeError(msg)

    def add_all(self) -> None:
        run(["git", "add", "-A"], cwd=self.repo_path)

    def commit(self, message: str, allow_empty=False) -> None:
        cmd = ["git", "commit", "-m", message]
        if allow_empty:
            cmd.append("--allow-empty")
        run(cmd, cwd=self.repo_path)


class GenericBackend(Backend):
    def __init__(self, name: str, repo_path=".") -> None:
        super().__init__(repo_path)
        self.name = name
        self.repo_path = Path(repo_path).absolute()
        self.subprocess = SubprocessBackend(self.repo_path)
        self._available = {}
        if name == "gitpython":
            try:
                import git as gitpy

                self.gitpy = gitpy
                self._available["gitpython"] = True
            except Exception:
                self._available["gitpython"] = False
        elif name in {"libgit2", "pygit2"}:
            try:
                import pygit2

                self.pygit2 = pygit2
                self._available["pygit2"] = True
            except Exception:
                self._available["pygit2"] = False
        elif name == "dulwich":
            try:
                import dulwich

                self.dulwich = dulwich
                self._available["dulwich"] = True
            except Exception:
                self._available["dulwich"] = False
        elif name == "pygithub":
            self._available["pygithub"] = False
        elif name == "typer":
            self._available["typer"] = False

    def _announce_fallback(self, op: str) -> None:
        print(
            f"[{self.name}] operation '{op}' not fully supported by this backend or module not installed; falling back to git CLI (subprocess)."
        )

    def ensure_clean_worktree(self, force=False):
        if not any(self._available.values()):
            self._announce_fallback("ensure_clean_worktree")
            return self.subprocess.ensure_clean_worktree(force)
        self._announce_fallback("ensure_clean_worktree")
        return self.subprocess.ensure_clean_worktree(force)

    def get_head(self):
        if self._available.get("gitpython"):
            try:
                repo = self.gitpy.Repo(self.repo_path)
                return repo.head.commit.hexsha
            except Exception:
                self._announce_fallback("get_head")
        return self.subprocess.get_head()

    def rev_parse(self, rev: str) -> str:
        return self.subprocess.rev_parse(rev)

    def get_last_n_commits(self, n: int):
        if self._available.get("gitpython"):
            try:
                repo = self.gitpy.Repo(self.repo_path)
                commits = list(repo.iter_commits("HEAD", max_count=n))
                if len(commits) < n:
                    msg = f"Repository has fewer than {n} commits."
                    raise RuntimeError(msg)
                return [c.hexsha for c in reversed(commits)]
            except Exception:
                self._announce_fallback("get_last_n_commits")
        return self.subprocess.get_last_n_commits(n)

    def write_combined_diff(self, base_rev: Any, head_rev: Any, out_patch_path: Path):
        self._announce_fallback("write_combined_diff")
        return self.subprocess.write_combined_diff(base_rev, head_rev, out_patch_path)

    def write_commits_metadata(self, base_rev: Any, head_rev: Any, out_meta_path: Path):
        self._announce_fallback("write_commits_metadata")
        return self.subprocess.write_commits_metadata(base_rev, head_rev, out_meta_path)

    def reset_hard(self, rev: str) -> bool:
        if self._available.get("gitpython"):
            try:
                repo = self.gitpy.Repo(self.repo_path)
                repo.git.reset("--hard", rev)
                return True
            except Exception:
                self._announce_fallback("reset_hard")
        return self.subprocess.reset_hard(rev)

    def apply_patch_index(self, patch_path: Path) -> bool:
        self._announce_fallback("apply_patch_index")
        return self.subprocess.apply_patch_index(patch_path)

    def add_all(self):
        self._announce_fallback("add_all")
        return self.subprocess.add_all()

    def commit(self, message: str, allow_empty=False):
        self._announce_fallback("commit")
        return self.subprocess.commit(message, allow_empty)


def create_backend(name: str, repo_path="."):
    name = (name or "subprocess").lower()
    if name in ("subprocess", "git"):
        return SubprocessBackend(repo_path)
    else:
        return GenericBackend(name, repo_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Squash last N commits into one via saved patch + reset + apply.")
    parser.add_argument("N", type=int, help="Number of commits to squash (e.g., 3)")
    parser.add_argument(
        "-b",
        "--backend",
        default="subprocess",
        choices=["subprocess", "pygithub", "gitpython", "libgit2", "dulwich", "typer"],
        help="Backend to use (some backends will fall back to subprocess for missing features)",
    )
    parser.add_argument("--patch-file", default="saved_patch.diff", help="Path to write combined patch")
    parser.add_argument(
        "--meta-file",
        default="saved_patch.meta.json",
        help="Path to write metadata JSON",
    )
    parser.add_argument("--force", action="store_true", help="Stash uncommitted changes and proceed")
    parser.add_argument("--dry-run", action="store_true", help="Show actions without making changes")
    args = parser.parse_args()
    repo_path = "."
    if not is_git_repo(repo_path):
        print(
            "This directory doesn't look like a git repository (no .git).",
            file=sys.stderr,
        )
        sys.exit(2)
    backend = create_backend(args.backend, repo_path)
    try:
        _was_clean, stash_made = backend.ensure_clean_worktree(force=args.force)
    except Exception as e:
        print("Error: working tree check failed:", e, file=sys.stderr)
        sys.exit(1)
    orig_head = backend.get_head()
    print("Original HEAD:", orig_head)
    if args.N <= 0:
        print("N must be > 0", file=sys.stderr)
        sys.exit(2)
    try:
        commits = backend.get_last_n_commits(args.N)
    except Exception as e:
        print("Error getting last N commits:", e, file=sys.stderr)
        if stash_made:
            run(["git", "stash", "pop"], cwd=repo_path)
        sys.exit(1)
    head_rev = orig_head
    try:
        base_rev = backend.rev_parse(f"HEAD~{args.N}")
    except subprocess.CalledProcessError:
        try:
            base_rev = backend.rev_parse(f"{commits[0]}^")
        except Exception as e:
            print("Cannot identify base revision:", e, file=sys.stderr)
            if stash_made:
                run(["git", "stash", "pop"], cwd=repo_path)
            sys.exit(1)
    print(f"Base rev (state before last {args.N} commits): {base_rev}")
    print("Commits to squash (oldest->newest):")
    for c in commits:
        print("  ", c)
    patch_path = Path(args.patch_file).absolute()
    meta_path = Path(args.meta_file).absolute()
    if args.dry_run:
        print(
            "[dry-run] Would write combined diff and metadata here:",
            patch_path,
            meta_path,
        )
    else:
        try:
            backend.write_combined_diff(base_rev, head_rev, patch_path)
            backend.write_commits_metadata(base_rev, head_rev, meta_path)
            print("Wrote patch to", patch_path)
            print("Wrote metadata to", meta_path)
        except Exception as e:
            print("Error creating patch/metadata:", e, file=sys.stderr)
            if stash_made:
                run(["git", "stash", "pop"], cwd=repo_path)
            sys.exit(1)
    if args.dry_run:
        print("[dry-run] Would reset hard to", base_rev)
        print("[dry-run] Would apply patch and commit a single commit summarizing these commits")
        if stash_made:
            print("[dry-run] Would pop stash")
        return
    backup_head = orig_head
    try:
        print("Resetting repo to base_rev:", base_rev)
        backend.reset_hard(base_rev)
        print("Applying patch:", patch_path)
        backend.apply_patch_index(patch_path)
        backend.add_all()
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        commit_msgs = []
        for c in meta.get("commits", []):
            commit_msgs.append(f"{c.get('sha')[:7]} - {c.get('subject')}")
        summary_msg = f"Squash {meta.get('commit_count', args.N)} commits: " + "; ".join(commit_msgs)
        backend.commit(summary_msg, allow_empty=(len(commit_msgs) == 0))
        new_head = backend.get_head()
        print("Created new single commit:", new_head)
        if stash_made:
            print("Restoring stashed uncommitted changes (pop stash)")
            run(["git", "stash", "pop"], cwd=repo_path)
        print("Done. The last {} commits were replaced by a single commit {}.".format(args.N, new_head))
    except Exception as e:
        print("Error during apply/reset/commit:", e, file=sys.stderr)
        print("Attempting to restore original state (reset --hard {})".format(backup_head))
        try:
            run(["git", "reset", "--hard", backup_head], cwd=repo_path)
            if stash_made:
                run(["git", "stash", "pop"], cwd=repo_path)
        except Exception as e2:
            print(
                "Error while trying to restore repository. Manual recovery may be necessary.",
                e2,
                file=sys.stderr,
            )
        sys.exit(1)


if __name__ == "__main__":
    main()
