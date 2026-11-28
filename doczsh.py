#!/data/data/com.termux/files/usr/bin/env python
"""
Add short end-of-line explanations to zsh scripts for beginners.

"""

from __future__ import annotations
import argparse
from pathlib import Path
import re
import sys
from typing import TYPE_CHECKING

from dh import atomic_write
from loguru import logger


if TYPE_CHECKING:
    from collections.abc import Iterator


try:
    from concurrent.futures import InterpreterPoolExecutor as Pool  # Python 3.14+
except ImportError:  # fallback for older interpreters
    from concurrent.futures import ProcessPoolExecutor as Pool

# (pattern, note). Order matters: specific rules must come before generic ones.
RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(pattern), note)
    for pattern, note in [
        (
            r"^if\b",
            "start of a conditional: runs the block below only if the test passes",
        ),
        (r"^elif\b", "otherwise-if branch, checked when the previous test failed"),
        (r"^else\b", "fallback branch when no earlier test matched"),
        (r"^then\b", "begins the block that runs when the test passed"),
        (r"^fi\b", "end of the if block"),
        (
            r"^(for|while|until)\b",
            "loop: repeats the block below until its condition or list is finished",
        ),
        (r"^do\b", "begins the loop body"),
        (r"^done\b", "end of the loop body"),
        (r"^case\b", "matches a value against several patterns"),
        (r"^esac\b", "end of the case statement"),
        (r"^(function\s+\S+|\S+\s*\(\)\s*\{?)", "defines a reusable function"),
        (r"^\}", "end of a function or group block"),
        (r"^\[\[", "conditional test (string or file checks, safer than single [)"),
        (r"^print\s+-P\b", "prints formatted text; %F{n} sets colour, %f resets it"),
        (r"^(print|echo)\b", "prints text to the terminal"),
        (
            r"^command\b",
            "runs the real command, skipping any alias or function with the same name",
        ),
        (r"^mkdir\b", "creates a folder; -p also creates missing parent folders"),
        (r"^chmod\b", "changes file permissions"),
        (r"^git\s+clone\b", "downloads a git repository into a local folder"),
        (r"^(source|\.)\s", "loads and runs another script in the current shell"),
        (r"^eval\b", "runs the given text as shell code"),
        (r"^alias\b", "creates a shortcut name for a command"),
        (r"^setopt\b", "turns shell options on"),
        (r"^unsetopt\b", "turns shell options off"),
        (r"^autoload\b", "loads a zsh function on first use"),
        (r"^zstyle\b", "configures zsh completion or behaviour settings"),
        (r"^bindkey\b", "maps a key to a line-editor action"),
        (r"^(zinit|zplugin)\b", "zinit plugin manager command"),
        (
            r"^(local|typeset|declare)\b",
            "declares a variable, optionally limited to the current function",
        ),
        (r"^export\b", "makes a variable visible to child processes"),
        (r"^read\b", "reads user input into a variable"),
        (r"^return\b", "leaves the current function with a status code"),
        (r"^exit\b", "stops the script with a status code"),
        (r"^cd\b", "changes the current directory"),
        (r"^rm\b", "deletes files; -r is recursive, -f skips prompts"),
        (r"^(cp|mv)\b", "copies or moves files"),
        (r".*\|\|", "if the left part fails, the right part runs"),
        (r".*&&", "the right part runs only if the left part succeeds"),
    ]
]

# Variable assignments such as FOO=bar, export FOO=bar, local -a arr=(...)
ASSIGNMENT = re.compile(
    r"^(?:(?:export|local|typeset|readonly|declare)\s+(?:-\w+\s+)*)?"
    r"[A-Za-z_]\w*(?:\[[^\]]*\])?\+?="
)


def has_comment(code: str) -> bool:
    """Return True if the line already has a real (unquoted) # comment."""
    quote: str | None = None
    prev = ""
    for ch in code:
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and prev in ("", " ", "\t", ";", "&", "|", "("):
            return True
        prev = ch
    return False


def explain(line: str) -> str | None:
    """Return a beginner note for a line, or None if it should be skipped."""
    code = line.strip()
    if not code or code.startswith("#"):
        return None
    if code.endswith("\\") or ASSIGNMENT.match(code) or has_comment(code):
        return None
    for pattern, note in RULES:
        if pattern.search(code):
            return note
    return None


def annotate_text(text: str) -> tuple[str, int]:
    """Return the annotated text and the number of comments added."""
    out: list[str] = []
    count = 0
    for line in text.splitlines():
        note = explain(line)
        if note:
            out.append(f"{line.rstrip()}    # {note}")
            count += 1
        else:
            out.append(line)
    return "\n".join(out) + "\n", count


def process(src: str, dst: str) -> tuple[str, int, str | None]:
    """Worker: annotate one file. Errors are returned, not logged, so the main process logs them."""
    try:
        text = Path(src).read_text(encoding="utf-8")
        result, count = annotate_text(text)
        atomic_write(Path(dst), result, encoding="utf-8")
        return src, count, None
    except Exception as exc:  # reported via loguru in the main process
        return src, 0, f"{type(exc).__name__}: {exc}"


def collect(target: Path) -> Iterator[Path]:
    """Yield the target file, or every .zsh file below a directory."""
    if target.is_dir():
        yield from (p for p in target.rglob("*.zsh") if p.is_file())
    else:
        yield target


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Annotate zsh scripts with beginner-friendly comments."
    )
    parser.add_argument("target", type=Path, help="a .zsh file or a directory to scan")
    parser.add_argument(
        "-i", "--in-place", action="store_true", help="overwrite the original files"
    )
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="INFO")

    files = list(collect(args.target))
    if not files:
        logger.error("No .zsh files found at {}", args.target)
        return 1

    jobs = [
        (
            str(src),
            str(
                src
                if args.in_place
                else src.with_name(f"{src.stem}.annotated{src.suffix}")
            ),
        )
        for src in files
    ]

    failures = 0
    with Pool() as pool:
        futures = [pool.submit(process, src, dst) for src, dst in jobs]
        for (src, _), future in zip(jobs, futures):
            try:
                _, count, error = future.result()
            except Exception:
                failures += 1
                logger.exception("Worker crashed while processing {}", src)
                continue
            if error:
                failures += 1
                logger.error("Failed to annotate {}: {}", src, error)
            else:
                logger.success("{}: added {} comment(s)", src, count)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
