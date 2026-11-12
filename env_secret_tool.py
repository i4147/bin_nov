#!/data/data/com.termux/files/usr/bin/env python
"""Environment-variable inventory, secret-leak scan, and token harvesting.
Analysis / deduplication ------------------------ - ``collect_env_vars.py`` and ``xenv_vars.py`` are near-identical top-level scripts collecting ``^NAME=`` lines into ``env_vars.txt``: one ``collect-vars`` command (now also matching ``os.environ[...]`` and ``${VAR}`` uses with ``--also-code``).
- ``secretleak.py`` -> ``scan-secrets`` (stdlib only; the loguru reporting became plain output, the magic "skip signature" hack is an explicit ``--allow`` substring list instead).
- ``to.env.py`` (harvest ``sk-…`` tokens from a markdown file, map models to variable names, append to ``~/.env`` files) -> ``extract-tokens``.
Matches are *never printed in full* by default in ``scan-secrets`` (first 12 chars shown).
Examples -------- python3 env_secret_tool.py collect-vars ~/project python3 env_secret_tool.py scan-secrets ~/project python3 env_secret_tool.py extract-tokens README.md
"""

import argparse
from collections import defaultdict
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
import json
from pathlib import Path
import re
import sys

ENV_ASSIGN: re.Pattern[str] = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")
ENV_CODE: re.Pattern[str] = re.compile(
    r"""os\.(?:environ(?:\.get)?\[?\(?|getenv\()\s*["']([A-Z_][A-Z0-9_]*)""" r"""|\$\{?([A-Z_][A-Z0-9_]*)\}?"""
)
SKIP_DIRS: frozenset[str] = frozenset({".git", ".venv", "venv", "__pycache__", "node_modules"})
SKIP_SUFFIXES: frozenset[str] = frozenset({
    ".pyc",
    ".so",
    ".o",
    ".a",
    ".exe",
    ".dll",
    ".dylib",
    ".jpg",
    ".png",
    ".gif",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".svg",
    ".lock",
    ".bin",
    ".class",
})
SECRET_PATTERNS: dict[str, str] = {
    "AWS Key": r"AKIA[0-9A-Z]{16}",
    "Private Key": r"-----BEGIN (?:RSA|DSA|EC|OPENSSH) PRIVATE KEY-----",
    "GitHub Token": r"ghp_[A-Za-z0-9_]{36,255}",
    "OpenAI-style Key": r"sk-[A-Za-z0-9_-]{20,}",
    "Generic API Key": r"api[_-]?key['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9\-_]{20,}",
    "Database URL": r"(?:mysql|postgresql|mongodb)://[^\s]+",
    "Slack Token": r"xox[baprs]-[0-9]{10,13}-[0-9]{10,13}[A-Za-z0-9-]*",
    "Generic Password": r"password['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9\-_!@#$%]{8,}",
    "AWS Secret": r"aws_secret_access_key['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{40}",
    "Google API Key": r"AIza[0-9A-Za-z\-_]{35}",
    "JWT Token": r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
}
MODEL_TO_VAR: dict[str, str] = {
    "baidu/cobuddy:free": "COBUDDY_TOKEN",
    "claude-opus-4-7": "CLAUDE_TOKEN",
    "deepseek-chat": "DEEPSEEK_CHAT_TOKEN",
    "deepseek-v4-flash": "DEEPSEEK_V4F_TOKEN",
    "deepseek-v4-pro": "DEEPSEEK_V4P_TOKEN",
    "gemini-2.5-flash": "GEMINI_F_TOKEN",
    "kimi-k2.5": "KIMI_TOKEN",
    "openrouter/owl-alpha": "OPENROUTER_TOKEN",
    "openai/gpt-5.5": "OPENAI_GPT55_TOKEN",
    "openai/gpt-5.5-pro": "OPENAI_GPT55P_TOKEN",
    "text-embedding-3-small": "TEXT_EMBEDDING_TOKEN",
    "x-ai/grok-4.3": "GROK_TOKEN",
}


def iter_text_files(root: Path, max_bytes: int) -> list[Path]:
    """Return scannable files under *root* (no symlinks/junk/binary)."""
    self_path: Path = Path(__file__).resolve()
    files: list[Path] = []
    candidates = root.rglob("*") if root.is_dir() else iter((root,))
    for path in candidates:
        if not path.is_file() or path.is_symlink():
            continue
        if SKIP_DIRS.intersection(path.parts) or path.suffix.lower() in SKIP_SUFFIXES:
            continue
        if path.resolve() == self_path:
            continue
        try:
            if path.stat().st_size > max_bytes:
                continue
            if b"\x00" in path.read_bytes()[:8192]:
                continue
        except OSError:
            continue
        files.append(path)
    return sorted(files)


def read_text(path: Path) -> str | None:
    """Read a file as text, returning None on failure."""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


# ----------------------------------------------------------- collect-vars
def cmd_collect_vars(args: argparse.Namespace) -> int:
    """Collect environment-variable names from assignments (and code)."""
    found: set[str] = set()
    for path in iter_text_files(args.root, args.max_kb * 1024):
        text: str | None = read_text(path)
        if text is None:
            continue
        for line in text.splitlines():
            match: re.Match[str] | None = ENV_ASSIGN.match(line)
            if match:
                found.add(match.group(1))
            if args.also_code:
                for code_match in ENV_CODE.finditer(line):
                    found.add(next(g for g in code_match.groups() if g))
    names: list[str] = sorted(found)
    if args.output is not None:
        args.output.write_text("".join(name + "\n" for name in names), encoding="utf-8")
        print(f"Found {len(names)} variable name(s); saved to {args.output}")
    else:
        print("\n".join(names))
        print(f"# {len(names)} variable name(s)", file=sys.stderr)
    return 0


# ----------------------------------------------------------- scan-secrets
def scan_one(job: tuple[Path, tuple[str, ...]]) -> tuple[Path, list[tuple[str, int, str]]]:
    """Scan one file; return (path, [(kind, line, redacted-match), ...])."""
    path, allow = job
    text: str | None = read_text(path)
    findings: list[tuple[str, int, str]] = []
    if text is None:
        return path, findings
    lines: list[str] = text.splitlines()
    for kind, pattern in SECRET_PATTERNS.items():
        for match in re.finditer(pattern, text, re.IGNORECASE):
            line_no: int = text.count("\n", 0, match.start()) + 1
            line_text: str = lines[line_no - 1] if line_no <= len(lines) else ""
            if any(token in line_text for token in allow):
                continue
            raw: str = match.group(0)
            findings.append((kind, line_no, raw[:12] + ("..." if len(raw) > 12 else "")))
    return path, findings


def cmd_scan_secrets(args: argparse.Namespace) -> int:
    """Scan a tree for leaked secrets (exit 1 when any are found)."""
    files: list[Path] = iter_text_files(args.root, args.max_kb * 1024)
    print(f"Scanning {len(files)} file(s) for secrets...")
    allow: tuple[str, ...] = tuple(args.allow)
    jobs: list[tuple[Path, tuple[str, ...]]] = [(p, allow) for p in files]
    total: int = 0
    dirty_files: int = 0
    report: dict[str, list[dict[str, object]]] = {}
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for path, findings in pool.map(scan_one, jobs):
            if not findings:
                continue
            dirty_files += 1
            total += len(findings)
            print(f"\n{path} ({len(findings)} finding(s))")
            entries: list[dict[str, object]] = []
            for kind, line_no, snippet in findings:
                print(f"  line {line_no}: {kind}: {snippet}")
                entries.append({"kind": kind, "line": line_no, "match": snippet})
            report[str(path)] = entries
    print(f"\nFiles scanned: {len(files)} | leaks: {total} | files with leaks: {dirty_files}")
    if args.json_out is not None:
        args.json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if total:
        print("SECRETS DETECTED - remove them before committing/pushing.", file=sys.stderr)
        return 1
    print("No secrets detected.")
    return 0


# --------------------------------------------------------- extract-tokens
def var_name_for_model(model: str) -> str:
    """Map a model name/heading to an env var name."""
    cleaned: str = model.strip().strip("`")
    if cleaned in MODEL_TO_VAR:
        return MODEL_TO_VAR[cleaned]
    lowered: str = cleaned.lower()
    for key, var in MODEL_TO_VAR.items():
        if key.lower() in lowered:
            return var
    safe: str = re.sub(r"[^A-Z0-9_]", "_", cleaned.upper().replace(" ", "_").replace("-", "_").replace(".", "_"))
    return f"{safe}_TOKEN"


def extract_model_tokens(text: str) -> list[tuple[str, str]]:
    """Pair ``### model`` markdown sections with ``sk-…`` tokens inside."""
    pairs: list[tuple[str, str]] = []
    for section in re.split(r"###\s+", text):
        lines: list[str] = section.strip().splitlines()
        if not lines:
            continue
        heading: str = re.sub(r"`\d{2}-\d{2} \d{2}:\d{2}`", "", lines[0]).strip().split("|")[0].strip()
        for token in re.findall(r"sk-[A-Za-z0-9]+", section):
            pairs.append((heading or "unknown", token))
    return pairs


def cmd_extract_tokens(args: argparse.Namespace) -> int:
    """Harvest model tokens from markdown into a table + env file."""
    text: str = args.source.read_text(encoding="utf-8", errors="ignore")
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for model, token in extract_model_tokens(text):
        if token not in seen:
            seen.add(token)
            unique.append((model, token))
    if not unique:
        print(f"No tokens found in {args.source}.")
        return 1
    rows: list[str] = [f"{'Model':<40} Token", "-" * 60]
    rows += [f"{model[:40]:<40} {token}" for model, token in unique]
    args.table_out.write_text("\n".join(rows) + "\n", encoding="utf-8")
    by_model: dict[str, list[str]] = defaultdict(list)
    for model, token in unique:
        by_model[model].append(token)
    mode: str = "a" if args.append else "w"
    with args.env_out.open(mode, encoding="utf-8") as handle:
        for model, tokens in by_model.items():
            var: str = var_name_for_model(model)
            for index, token in enumerate(tokens, start=1):
                suffix: str = f"_{index}" if len(tokens) > 1 else ""
                handle.write(f"{var}{suffix}={token}\n")
    print(f"Saved {len(unique)} token(s) to {args.table_out} and {args.env_out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(description="Env-var inventory, secret scan, token harvest.")
    parser.add_argument("--max-kb", type=int, default=2048, help="Skip files larger than this (default: 2048 KiB)")
    sub = parser.add_subparsers(dest="command", required=True)

    cv = sub.add_parser("collect-vars", help="List env var names found in a tree")
    cv.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    cv.add_argument("--also-code", action="store_true", help="Also match os.environ / ${VAR} uses")
    cv.add_argument("-o", "--output", type=Path, default=None, help="Write names here (original: env_vars.txt)")
    cv.set_defaults(handler=cmd_collect_vars)

    ss = sub.add_parser("scan-secrets", help="Scan a tree for leaked secrets")
    ss.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    ss.add_argument(
        "--allow", nargs="*", default=[], metavar="TEXT", help="Ignore lines containing any of these strings"
    )
    ss.add_argument("--json-out", type=Path, default=None, help="Write findings as JSON")
    ss.add_argument("-j", "--workers", type=int, default=8)
    ss.set_defaults(handler=cmd_scan_secrets)

    et = sub.add_parser("extract-tokens", help="Harvest sk- tokens from model markdown")
    et.add_argument("source", nargs="?", type=Path, default=Path("README.md"))
    et.add_argument("--env-out", type=Path, default=Path.home() / ".env_dynamic")
    et.add_argument("--table-out", type=Path, default=Path("tokens.txt"))
    et.add_argument("--append", action="store_true", default=True, help="Append to env file (original behaviour)")
    et.set_defaults(handler=cmd_extract_tokens)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
