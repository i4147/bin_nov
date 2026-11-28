#!/data/data/com.termux/files/usr/bin/env python
"""Bidirectional .sh <-> .zsh converter for Termux.

Supports:
- .sh and .zsh files
- Extensionless scripts with a Bash/sh or Zsh shebang
- Symlink retargeting
- Git-tracked reference rewriting
- zsh -n / bash -n syntax validation
./shell_converter.py --check
./shell_converter.py --apply
./shell_converter.py -d sh2zsh --apply ./bin
./shell_converter.py -d zsh2sh --apply ./bin
"""

from __future__ import annotations
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


PREFIX = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
ZSH_SHEBANG = f"#!{PREFIX}/bin/zsh"
SH_SHEBANG = f"#!{PREFIX}/bin/bash"

ZSH_PROLOGUE = "setopt SH_WORD_SPLIT GLOB_SUBST\nunsetopt NOMATCH\n"
ZSH_PROLOGUE_LINES = tuple(ZSH_PROLOGUE.splitlines())

SCRIPT_SUFFIXES = {".sh", ".zsh"}
MAX_FILE_SIZE = 2 * 1024 * 1024


def resolve_input(raw: str, base_dir: Path) -> Path:
    target = Path(raw).expanduser()

    if not target.is_absolute():
        target = base_dir / target

    return target


def read_script(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def detect_script_kind(path: Path, text: str | None = None) -> str | None:
    """Return 'sh', 'zsh', or None.

    Extensioned files are classified by suffix. Extensionless files require
    a recognizable Bash/sh or Zsh shebang.
    """
    suffix = path.suffix.lower()

    if suffix == ".sh":
        return "sh"
    if suffix == ".zsh":
        return "zsh"

    if text is None:
        try:
            text = read_script(path)
        except (OSError, UnicodeDecodeError):
            return None

    first_line = text.splitlines()[0] if text else ""

    if not first_line.startswith("#!"):
        return None

    interpreter = first_line[2:].strip()

    if "zsh" in interpreter:
        return "zsh"

    if re.search(r"(?:^|/)(?:ba)?sh(?:\s|$)", interpreter):
        return "sh"

    return None


def discover_scripts(
    paths: list[str],
    kinds: set[str],
    base_dir: Path,
) -> list[Path]:
    """Find .sh, .zsh, and eligible extensionless scripts."""
    found: list[Path] = []
    seen: set[Path] = set()

    for raw in paths:
        target = resolve_input(raw, base_dir)

        if target.is_file():
            candidates = [target]
        elif target.is_dir():
            candidates = sorted(target.rglob("*"))
        else:
            msg = f"path not found: {raw}"
            raise FileNotFoundError(msg)

        for item in candidates:
            if ".git" in item.parts:
                continue
            if not item.is_file() or item.is_symlink():
                continue

            try:
                text = read_script(item)
            except (OSError, UnicodeDecodeError):
                continue

            kind = detect_script_kind(item, text)

            if kind in kinds and item.resolve() not in seen:
                seen.add(item.resolve())
                found.append(item)

    return found


def discover_symlinks(paths: list[str], base_dir: Path) -> list[Path]:
    links: list[Path] = []
    seen: set[Path] = set()

    for raw in paths:
        target = resolve_input(raw, base_dir)

        if target.is_symlink():
            candidates = [target]
        elif target.is_dir():
            candidates = sorted(target.rglob("*"))
        else:
            continue

        for item in candidates:
            if ".git" in item.parts:
                continue
            if item.is_symlink() and item not in seen:
                seen.add(item)
                links.append(item)

    return links


def detect_newline(lines: list[str]) -> str:
    if lines and lines[0].endswith("\r\n"):
        return "\r\n"
    return "\n"


def replace_shebang(lines: list[str], shebang: str) -> list[str]:
    newline = detect_newline(lines)

    if lines and lines[0].startswith("#!"):
        lines[0] = shebang + newline
    else:
        lines.insert(0, shebang + newline)

    return lines


def rewrite_sh_to_zsh(text: str) -> str:
    replacements = {
        "shopt -s nullglob dotglob": "setopt NULL_GLOB GLOB_DOTS",
        "shopt -s nullglob nocaseglob": "setopt NULL_GLOB",
        "shopt -s nullglob": "setopt NULL_GLOB",
        "shopt -s dotglob": "setopt GLOB_DOTS",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    text = re.sub(r"\bdeclare\s+-A\b", "typeset -A", text)
    text = re.sub(r"\bdeclare\s+-a\b", "typeset -a", text)
    text = re.sub(r"\bdeclare\s+-i\b", "typeset -i", text)
    text = re.sub(r"\btype\s+-P\b", "whence -p", text)

    lines = text.splitlines(keepends=True)
    lines = replace_shebang(lines, ZSH_SHEBANG)

    has_prologue = (
        len(lines) > 2
        #        and lines.strip() == ZSH_PROLOGUE_LINES[0]
        and lines == ZSH_PROLOGUE_LINES
    )

    if not has_prologue:
        prologue = ZSH_PROLOGUE.replace("\n", detect_newline(lines))
        lines.insert(1, prologue)

    return "".join(lines)


def rewrite_zsh_to_sh(text: str) -> str:
    lines = text.splitlines(keepends=True)
    lines = replace_shebang(lines, SH_SHEBANG)

    if (
        len(lines) > 2
        #        and lines.strip() == ZSH_PROLOGUE_LINES[0]
        and lines == ZSH_PROLOGUE_LINES
    ):
        del lines[1:3]

    text = "".join(lines)

    replacements = {
        "setopt NULL_GLOB GLOB_DOTS": "shopt -s nullglob dotglob",
        "setopt NULL_GLOB": "shopt -s nullglob",
        "setopt GLOB_DOTS": "shopt -s dotglob",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    text = re.sub(r"\btypeset\s+-A\b", "declare -A", text)
    text = re.sub(r"\btypeset\s+-a\b", "declare -a", text)
    text = re.sub(r"\btypeset\s+-i\b", "declare -i", text)
    text = re.sub(r"\bwhence\s+-p\b", "type -P", text)

    return text


def find_git_root(start: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start,
            text=True,
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if result.returncode != 0:
        return None

    output = result.stdout.strip()
    return Path(output) if output else None


def rewrite_references(converted: dict[str, str], base_dir: Path) -> int:
    """Rewrite script references in git-tracked text files.

    `converted` maps a script filename stem to its new extension:
    {"install": ".zsh"}, {"deploy": ".sh"}, etc.
    """
    if not converted:
        return 0

    git_root = find_git_root(base_dir)
    if git_root is None:
        return 0

    stems = sorted(converted, key=len, reverse=True)

    if not stems:
        return 0

    alternatives = "|".join(re.escape(stem) for stem in stems)

    # Matches foo.sh, foo.zsh, ./foo.sh, and similar extensioned references.
    extensioned_pattern = re.compile(
        rf"(?<![A-Za-z0-9_.-])({alternatives})(\.(?:sh|zsh))(?![A-Za-z0-9_.-])"
    )

    # Matches bare extensionless references such as ./install or $BIN/install.
    # This is deliberately conservative: it does not alter words embedded in
    # larger identifiers.
    extensionless_pattern = re.compile(
        rf"(?<![A-Za-z0-9_.-])({alternatives})(?![A-Za-z0-9_.-])"
    )

    try:
        listed = subprocess.run(
            [
                "git",
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            cwd=git_root,
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError) as error:
        msg = f"could not enumerate repository files: {error}"
        raise RuntimeError(msg) from error

    self_path = Path(__file__).resolve()
    changed = 0

    for raw_path in listed.split(b"\0"):
        if not raw_path:
            continue

        path = git_root / os.fsdecode(raw_path)

        try:
            resolved = path.resolve()
        except OSError:
            continue

        if resolved == self_path or path.is_symlink() or not path.is_file():
            continue

        try:
            if path.stat().st_size > MAX_FILE_SIZE:
                continue

            original = path.read_bytes()

            if b"\0" in original:
                continue

            text = original.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        def replace_extensioned(match: re.Match[str]) -> str:
            stem = match.group(1)
            old_extension = match.group(2)
            new_extension = converted[stem]

            if old_extension == new_extension:
                return match.group(0)

            return f"{stem}{new_extension}"

        def replace_extensionless(match: re.Match[str]) -> str:
            stem = match.group(1)
            return f"{stem}{converted[stem]}"

        updated = extensioned_pattern.sub(replace_extensioned, text)
        updated = extensionless_pattern.sub(replace_extensionless, updated)

        if updated != text:
            try:
                path.write_text(updated, encoding="utf-8", newline="")
                changed += 1
            except OSError as error:
                msg = f"could not update {path}: {error}"
                raise RuntimeError(msg) from error

    return changed


def migration_target(
    source: Path,
    source_kind: str,
) -> Path:
    target_extension = ".zsh" if source_kind == "sh" else ".sh"
    return source.with_suffix(target_extension)


def migrate(
    paths: list[str],
    base_dir: Path,
    direction: str,
) -> tuple[int, int, int]:
    if direction == "sh2zsh":
        source_kinds = {"sh"}
    elif direction == "zsh2sh":
        source_kinds = {"zsh"}
    else:
        source_kinds = {"sh", "zsh"}

    scripts = discover_scripts(paths, source_kinds, base_dir)

    planned: list[tuple[Path, Path, str]] = []

    for source in scripts:
        try:
            text = read_script(source)
        except (OSError, UnicodeDecodeError) as error:
            msg = f"could not read {source}: {error}"
            raise RuntimeError(msg) from error

        source_kind = detect_script_kind(source, text)

        if source_kind not in source_kinds:
            continue

        destination = migration_target(source, source_kind)
        planned.append((source, destination, source_kind))

    conflicts = [destination for _, destination, _ in planned if destination.exists()]

    if conflicts:
        names = ", ".join(str(path) for path in conflicts)
        msg = f"refusing to overwrite existing destinations: {names}"
        raise RuntimeError(msg)

    converted: dict[str, str] = {}

    for source, destination, source_kind in planned:
        try:
            text = read_script(source)
            mode = source.stat().st_mode
        except (OSError, UnicodeDecodeError) as error:
            msg = f"could not read {source}: {error}"
            raise RuntimeError(msg) from error

        if source_kind == "sh":
            text = rewrite_sh_to_zsh(text)
            converted[source.stem] = ".zsh"
        else:
            text = rewrite_zsh_to_sh(text)
            converted[source.stem] = ".sh"

        try:
            destination.write_text(text, encoding="utf-8", newline="")
            destination.chmod(mode)
            source.unlink()
        except OSError as error:
            msg = f"could not convert {source} to {destination}: {error}"
            raise RuntimeError(msg) from error

    links_changed = retarget_symlinks(paths, base_dir, direction)
    references_changed = rewrite_references(converted, base_dir)

    return len(planned), links_changed, references_changed


def retarget_symlinks(
    paths: list[str],
    base_dir: Path,
    direction: str,
) -> int:
    changed = 0

    for link in discover_symlinks(paths, base_dir):
        try:
            target = os.readlink(link)
        except OSError as error:
            msg = f"could not read symlink {link}: {error}"
            raise RuntimeError(msg) from error

        new_target: str | None = None

        if direction in {"sh2zsh", "auto"} and target.endswith(".sh"):
            new_target = target[:-3] + ".zsh"
        elif direction in {"zsh2sh", "auto"} and target.endswith(".zsh"):
            new_target = target[:-4] + ".sh"

        if new_target is None or new_target == target:
            continue

        temporary = link.with_name(link.name + ".converter-tmp")

        try:
            temporary.symlink_to(new_target)
            temporary.replace(link)
            changed += 1
        except OSError as error:
            msg = f"could not retarget symlink {link}: {error}"
            raise RuntimeError(msg) from error

    return changed


def check(
    paths: list[str],
    base_dir: Path,
    direction: str,
) -> tuple[list[str], int, int]:
    problems: list[str] = []

    if direction in {"sh2zsh", "auto"}:
        leftover_sh = discover_scripts(paths, {"sh"}, base_dir)

        if leftover_sh:
            names = ", ".join(str(path) for path in leftover_sh)
            problems.append(f"{len(leftover_sh)} Bash/sh script(s) remain: {names}")

    if direction == "sh2zsh":
        target_kinds = {"zsh"}
    elif direction == "zsh2sh":
        target_kinds = {"sh"}
    else:
        target_kinds = {"sh", "zsh"}

    scripts = discover_scripts(paths, target_kinds, base_dir)
    links = discover_symlinks(paths, base_dir)

    for path in scripts:
        try:
            text = read_script(path)
        except (OSError, UnicodeDecodeError) as error:
            problems.append(f"cannot read {path}: {error}")
            continue

        kind = detect_script_kind(path, text)

        if kind is None:
            problems.append(f"{path}: cannot determine script type")
            continue

        expected_shebang = ZSH_SHEBANG if kind == "zsh" else SH_SHEBANG
        first_line = text.splitlines()[0] if text else ""

        if not first_line.startswith("#!"):
            problems.append(f"{path}: missing shebang")
        elif first_line != expected_shebang:
            problems.append(f"{path}: incorrect shebang; expected {expected_shebang}")

        if kind == "zsh":
            bash_problem = find_bash_only_syntax(path, text)

            if bash_problem:
                problems.append(bash_problem)

    broken_links = [str(link) for link in links if not link.exists()]

    if broken_links:
        problems.append("broken symlinks: " + ", ".join(broken_links))

    wrong_targets = find_wrong_symlink_targets(links, direction)

    if wrong_targets:
        problems.append(
            "symlinks still target the old extension: " + ", ".join(wrong_targets)
        )

    problems.extend(validate_syntax(scripts))

    return problems, len(scripts), len(links)


def find_bash_only_syntax(path: Path, text: str) -> str | None:
    bash_only_patterns = (
        r"\bshopt\b",
        r"\bmapfile\b",
        r"\breadarray\b",
        r"\bcompgen\b",
        r"\bexport\s+-f\b",
        r"\bdeclare\s+-",
        r"\btype\s+-P\b",
        r"\bread\s+[^\n]*\s-p\s",
        r"\$\{![^}]+\[@\]\}",
    )

    for pattern in bash_only_patterns:
        if re.search(pattern, text):
            return f"{path}: contains unconverted Bash syntax ({pattern})"

    return None


def find_wrong_symlink_targets(
    links: list[Path],
    direction: str,
) -> list[str]:
    wrong: list[str] = []

    for link in links:
        try:
            target = os.readlink(link)
        except OSError:
            continue

        if direction in {"sh2zsh", "auto"} and target.endswith(".sh"):
            wrong.append(f"{link} -> {target}")
        elif direction in {"zsh2sh", "auto"} and target.endswith(".zsh"):
            wrong.append(f"{link} -> {target}")

    return wrong


def validate_syntax(scripts: list[Path]) -> list[str]:
    problems: list[str] = []
    zsh = shutil.which("zsh")
    bash = shutil.which("bash")

    for path in scripts:
        kind = detect_script_kind(path)

        if kind == "zsh" and zsh:
            problems.extend(run_syntax_check(zsh, ["-n"], path, "Zsh"))
        elif kind == "sh" and bash:
            problems.extend(run_syntax_check(bash, ["-n"], path, "Bash"))

    return problems


def run_syntax_check(
    interpreter: str,
    arguments: list[str],
    path: Path,
    shell_name: str,
) -> list[str]:
    try:
        result = subprocess.run(
            [interpreter, *arguments, str(path)],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        return [f"{path}: could not run {shell_name} syntax check: {error}"]

    if result.returncode == 0:
        return []

    details = result.stderr.strip() or result.stdout.strip() or "unknown error"
    return [f"{path}: {shell_name} syntax check failed: {details}"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Convert .sh files to .zsh and vice versa in Termux. "
            "Extensionless scripts with Bash/sh or Zsh shebangs are supported."
        )
    )

    parser.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="Files or directories to process; defaults to the current directory.",
    )

    parser.add_argument(
        "-d",
        "--direction",
        choices=("auto", "sh2zsh", "zsh2sh"),
        default="auto",
        help="Conversion direction; auto swaps Bash/sh and Zsh scripts.",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Perform the migration.",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="Verify the migrated tree; this is the default.",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    base_dir = Path.cwd()

    try:
        if args.apply:
            scripts, links, references = migrate(
                args.paths,
                base_dir,
                args.direction,
            )

            print(
                f"Migrated {scripts} script(s), updated {links} symlink(s), "
                f"and rewrote references in {references} file(s)."
            )
            return 0

        problems, scripts, links = check(
            args.paths,
            base_dir,
            args.direction,
        )

        if problems:
            for problem in problems:
                print(f"ERROR: {problem}", file=sys.stderr)

            return 1

        print(f"OK: verified {scripts} script(s) and {links} symlink(s).")
        return 0

    except (OSError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
