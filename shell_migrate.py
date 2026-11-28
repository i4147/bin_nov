#!/data/data/com.termux/files/usr/bin/env python
"""Migrate root-level shell scripts. Default: Zsh to Bash; --reverse: Bash to Zsh.

By default, check the tree. Use --apply to make changes. This tool performs
only a few line-based substitutions; it is not a shell-language translator.
Review changes in version control and test scripts before using them.
"""

from __future__ import annotations
import argparse
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dh import atomic_write

ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()
PREFIX = "/data/data/com.termux/files/usr"
PROLOGUE = ("setopt SH_WORD_SPLIT GLOB_SUBST", "unsetopt NOMATCH")
LOG = logging.getLogger("migration")


def scripts(ext: str, only: set[str] | None) -> list[Path]:
    return sorted(
        p
        for p in ROOT.glob(f"*.{ext}")
        if not p.is_symlink() and p.is_file() and (only is None or p.stem in only)
    )


def repo_files() -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        msg = f"cannot enumerate repository files with git: {exc}"
        raise RuntimeError(msg) from exc
    return sorted({ROOT / os.fsdecode(p) for p in result.stdout.split(b"\0") if p})


def read_utf8(path: Path) -> str | None:
    try:
        data = path.read_bytes()
        if b"\0" in data:
            return None
        return data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def transform(text: str, reverse: bool) -> str:
    lines = text.splitlines(keepends=True)
    nl = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    shell = "zsh" if reverse else "bash"
    if lines and lines[0].startswith("#!"):
        lines[0] = f"#!{PREFIX}{shell}{nl}"
    else:
        lines.insert(0, f"#!{PREFIX}{shell}{nl}")
    if reverse:
        # Avoid inserting duplicates when reprocessing manually edited scripts.
        for line in reversed(PROLOGUE):
            if not any(x.rstrip("\r\n") == line for x in lines[1:3]):
                lines.insert(1, line + nl)
    else:
        while len(lines) > 1 and lines[1].rstrip("\r\n") in PROLOGUE:
            del lines[1]

    mapping = {
        "setopt NULL_GLOB GLOB_DOTS": "shopt -s nullglob dotglob",
        "setopt NULL_GLOB": "shopt -s nullglob",
        "typeset -A": "declare -A",
        "typeset -a": "declare -a",
        "typeset -i": "declare -i",
        "whence -p": "type -P",
    }
    if reverse:
        mapping = {v: k for k, v in mapping.items()}
    # Options can be replaced as complete commands; declarations and lookup
    # commands usually take arguments, but changing them blindly is unsafe.
    for i in range(1, len(lines)):
        match = re.fullmatch(r"([ \t]*)([^\r\n]*?)(\r?\n)?", lines[i])
        if match and match.group(2) in mapping:
            lines[i] = match.group(1) + mapping[match.group(2)] + (match.group(3) or "")
    return "".join(lines)


def reference_regex(names: list[str], ext: str) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    return re.compile(rf"(?<![A-Za-z0-9_.-])({alternatives})\.{ext}(?![A-Za-z0-9_.-])")


def selected_links(names: set[str], src: str) -> list[tuple[Path, str]]:
    result = []
    for path in sorted(ROOT.iterdir()):
        if not path.is_symlink():
            continue
        target = os.readlink(path)
        # Only retarget links whose lexical target is the selected root script;
        # don't touch links to subdirectories or external files of the same name.
        resolved = Path(os.path.abspath(os.path.join(ROOT, target)))
        if resolved.parent == ROOT and resolved.name in {f"{n}.{src}" for n in names}:
            result.append((path, target))
    return result


def syntax_check(path: Path, shell: str, problems: list[str]) -> None:
    executable = shutil.which(shell)
    if not executable:
        return
    result = subprocess.run(
        [executable, "-n", str(path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        errors="replace",
    )
    if result.returncode:
        problems.append(
            f"{path.name}: {shell} -n failed: {result.stderr.strip() or result.stdout.strip()}"
        )


def check(only: set[str] | None, reverse: bool) -> list[str]:
    src, dst = ("sh", "zsh") if reverse else ("zsh", "sh")
    problems = []
    remaining = scripts(src, only)
    if remaining:
        problems.append(
            f"source scripts remain: {', '.join(p.name for p in remaining)}"
        )
    targets = scripts(dst, only)
    if only:
        found = {p.stem for p in targets} | {p.stem for p in remaining}
        for name in sorted(only - found):
            problems.append(f"selected script not found: {name}")
    names = [p.stem for p in targets]
    shell = "zsh" if reverse else "bash"
    for path in targets:
        text = read_utf8(path)
        if text is None:
            problems.append(f"{path.name}: cannot read as UTF-8 text")
            continue
        first = text.splitlines()[0] if text else ""
        if first != f"#!{PREFIX}{shell}":
            problems.append(f"{path.name}: incorrect or missing {shell} shebang")
        syntax_check(path, shell, problems)
    if names:
        pattern = reference_regex(names, src)
        for path in repo_files():
            if path == SELF or path.is_symlink() or not path.is_file():
                continue
            text = read_utf8(path)
            if text is not None and pattern.search(text):
                problems.append(
                    f"{path.relative_to(ROOT)}: possible stale .{src} reference"
                )
        for path, target in selected_links(set(names), src):
            problems.append(f"{path.name}: stale symlink target {target}")
    if not shutil.which(shell):
        LOG.warning("%s not found; syntax checks skipped", shell)
    return problems


def migrate(only: set[str] | None, reverse: bool) -> None:
    src, dst = ("sh", "zsh") if reverse else ("zsh", "sh")
    sources = scripts(src, only)
    if only:
        missing = only - {p.stem for p in sources}
        if missing:
            raise RuntimeError(
                "selected source scripts not found: " + ", ".join(sorted(missing))
            )
    destinations = [p.with_suffix(f".{dst}") for p in sources]
    conflicts = [p.name for p in destinations if os.path.lexists(p)]
    if conflicts:
        raise RuntimeError("destination already exists: " + ", ".join(conflicts))
    names = [p.stem for p in sources]
    if not names:
        LOG.info("No source scripts to migrate")
        return
    # Preflight all inputs and enumerate git before modifying anything.
    files = repo_files()
    links = selected_links(set(names), src)
    pattern = reference_regex(names, src)
    changes: dict[Path, tuple[str, int]] = {}
    for path in files:
        if path == SELF or path.is_symlink() or not path.is_file():
            continue
        text = read_utf8(path)
        if text is not None:
            updated = pattern.sub(lambda m: f"{m.group(1)}.{dst}", text)
            if updated != text:
                changes[path] = (updated, path.stat().st_mode)
    converted = []
    for source, destination in zip(sources, destinations):
        text = read_utf8(source)
        if text is None:
            msg = f"cannot read {source.name} as UTF-8 text"
            raise RuntimeError(msg)
        text = pattern.sub(lambda m: f"{m.group(1)}.{dst}", text)
        converted.append(
            (source, destination, transform(text, reverse), source.stat().st_mode)
        )
        changes.pop(source, None)
    # Each file replacement is atomic, but the whole migration is not a
    # transaction. Use version control or a backup before applying.
    for source, destination, text, mode in converted:
        # Exclusive creation avoids silently overwriting a newly created file.
        fd = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IMODE(mode)
        )
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(text.encode("utf-8"))
            os.chmod(destination, stat.S_IMODE(mode))
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        source.unlink()
        LOG.info("%s -> %s", source.name, destination.name)
    for path, (text, mode) in changes.items():
        atomic_write(path, text, mode)
    for path, target in links:
        # Replace the symlink atomically using a temporary name.
        fd, tmp = tempfile.mkstemp(prefix=".shell-link-", dir=path.parent)
        os.close(fd)
        os.unlink(tmp)
        try:
            os.symlink(target[: -(len(src) + 1)] + "." + dst, tmp)
            os.replace(tmp, path)
        finally:
            if os.path.lexists(tmp):
                os.unlink(tmp)
    LOG.info(
        "Migrated %d scripts, updated %d reference files and %d symlinks",
        len(sources),
        len(changes),
        len(links),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply", action="store_true", help="perform migration and then check"
    )
    mode.add_argument("--check", action="store_true", help="check only (default)")
    parser.add_argument(
        "--reverse", action="store_true", help="migrate Bash to Zsh instead"
    )
    parser.add_argument("--only", help="comma-separated script stems")
    args = parser.parse_args()
    only = None
    if args.only is not None:
        parts = [s.strip() for s in args.only.split(",")]
        if any(not s or s in {".", ".."} or "/" in s or "\\" in s for s in parts):
            parser.error("--only requires nonempty script stems, not paths")
        only = set(parts)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if args.apply:
            migrate(only, args.reverse)
        problems = check(only, args.reverse)
    except (OSError, RuntimeError) as exc:
        LOG.error("%s", exc)
        return 1
    if problems:
        for problem in problems:
            LOG.error("%s", problem)
        return 1
    LOG.info("Checks passed (this does not guarantee semantic compatibility)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
