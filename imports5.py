#!/data/data/com.termux/files/usr/bin/env python
"""Generate a ``requirements.txt`` offline (local package list) or online (PyPI API).
Scans the sources (ast or line-based, notebooks included), normalises the import names through the PyPI mapping, detects the local modules and resolves the versions either from a local package list (``offline``) or from the PyPI JSON API (``online``), with a diff and a dry-run option.
Usage: imports5 {offline,online} -p PROJECT [--ast] [--print] Subcommands: offline Use a local PyPI package list (no network); mirrors imp1/imp2 online Resolve via PyPI JSON API; mirrors pipreqs/pr2 Options: -p, --path Project directory (default: current dir) -l, --pypi-list Path to the offline PyPI package list --pip-cmd Pip executable used for `pip freeze` --detect-local Enable imp2-style local-module detection --ast Parse imports via AST instead of line-by-line --encoding --ignore Additional directory basenames to ignore --no-follow-links --scan-notebooks --savepath Path for the generated requirements file ...
plus 9 more; run ``imports5 --help`` for the full list.
Examples: $ imports5 online -p .
--ast --print Command-line script: run ``python imports5.py`` or the ``imports5`` command.
The module is importable as well."""

import argparse
import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Iterable, Iterator, Optional
import urllib.error
import urllib.request

try:
    STDLIB: set[str] = set(sys.stdlib_module_names)
except AttributeError:
    STDLIB = {
        "abc",
        "aifc",
        "argparse",
        "array",
        "ast",
        "asynchat",
        "asyncio",
        "asyncore",
        "atexit",
        "audioop",
        "base64",
        "bdb",
        "binascii",
        "bisect",
        "builtins",
        "bz2",
        "calendar",
        "cgi",
        "cgitb",
        "chunk",
        "cmath",
        "cmd",
        "code",
        "codecs",
        "codeop",
        "collections",
        "colorsys",
        "compileall",
        "concurrent",
        "configparser",
        "contextlib",
        "contextvars",
        "copy",
        "copyreg",
        "crypt",
        "csv",
        "ctypes",
        "curses",
        "dataclasses",
        "datetime",
        "dbm",
        "decimal",
        "difflib",
        "dis",
        "distutils",
        "doctest",
        "email",
        "encodings",
        "ensurepip",
        "enum",
        "errno",
        "faulthandler",
        "fcntl",
        "filecmp",
        "fileinput",
        "fnmatch",
        "formatter",
        "fractions",
        "ftplib",
        "functools",
        "gc",
        "getopt",
        "getpass",
        "gettext",
        "glob",
        "graphlib",
        "grp",
        "gzip",
        "hashlib",
        "heapq",
        "hmac",
        "html",
        "http",
        "imaplib",
        "imghdr",
        "imp",
        "importlib",
        "inspect",
        "io",
        "ipaddress",
        "itertools",
        "json",
        "keyword",
        "linecache",
        "locale",
        "logging",
        "lzma",
        "mailbox",
        "mailcap",
        "marshal",
        "math",
        "mimetypes",
        "mmap",
        "modulefinder",
        "msilib",
        "msvcrt",
        "multiprocessing",
        "netrc",
        "nis",
        "nntplib",
        "nt",
        "ntpath",
        "nturl2path",
        "numbers",
        "opcode",
        "operator",
        "optparse",
        "os",
        "ossaudiodev",
        "parser",
        "pathlib",
        "pdb",
        "pickle",
        "pickletools",
        "pipes",
        "pkgutil",
        "platform",
        "plistlib",
        "poplib",
        "posix",
        "posixpath",
        "pprint",
        "profile",
        "pstats",
        "pty",
        "pwd",
        "py_compile",
        "pyclbr",
        "pydoc",
        "queue",
        "quopri",
        "random",
        "re",
        "readline",
        "reprlib",
        "resource",
        "rlcompleter",
        "runpy",
        "sched",
        "secrets",
        "select",
        "selectors",
        "shelve",
        "shlex",
        "shutil",
        "signal",
        "site",
        "smtplib",
        "sndhdr",
        "socket",
        "socketserver",
        "spwd",
        "sqlite3",
        "ssl",
        "stat",
        "statistics",
        "string",
        "stringprep",
        "struct",
        "subprocess",
        "sunau",
        "symtable",
        "sys",
        "sysconfig",
        "syslog",
        "tabnanny",
        "tarfile",
        "telnetlib",
        "tempfile",
        "termios",
        "textwrap",
        "threading",
        "time",
        "timeit",
        "tkinter",
        "token",
        "tokenize",
        "trace",
        "traceback",
        "tracemalloc",
        "tty",
        "turtle",
        "turtledemo",
        "types",
        "typing",
        "unicodedata",
        "unittest",
        "urllib",
        "uu",
        "uuid",
        "venv",
        "warnings",
        "wave",
        "weakref",
        "webbrowser",
        "winreg",
        "winsound",
        "wsgiref",
        "xdrlib",
        "xml",
        "xmlrpc",
        "zipapp",
        "zipfile",
        "zipimport",
        "zlib",
    }
IMPORT_MAPPING: dict[str, str] = {
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "yaml": "PyYAML",
    "PIL": "Pillow",
    "serial": "pyserial",
    "pkg_resources": "setuptools",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "jwt": "PyJWT",
    "OpenSSL": "pyOpenSSL",
    "Crypto": "pycryptodome",
    "git": "GitPython",
    "numpy": "numpy",
    "pandas": "pandas",
}
IGNORES_ONLINE: set[str] = {
    ".hg",
    ".svn",
    ".git",
    ".tox",
    "__pycache__",
    "env",
    "venv",
    ".ipynb_checkpoints",
}
IGNORES_OFFLINE_FULL: set[str] = {
    "__pycache__",
    "venv",
    ".venv",
    "env",
    ".env",
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "build",
    "dist",
    ".eggs",
}


def _norm(name: str) -> str:
    return name.lower().replace("_", "-")


def parse_imports_lines(lines: Iterable[str]) -> Iterator[str]:
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "import":
            for item in " ".join(parts[1:]).split(","):
                name = item.strip().split(" as ")[0].strip()
                if name:
                    yield name.split(".")[0]
        elif parts[0] == "from":
            if len(parts) < 2 or parts[1].startswith("."):
                continue
            name = parts[1].strip().split(".")[0]
            if name:
                yield name


def parse_imports_ast(source: str) -> set[str]:
    result: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return result
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                result.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            result.add(node.module.split(".")[0])
    return result


def read_notebook(path: str, encoding: str = "utf-8") -> str:
    with open(path, "r", encoding=encoding) as f:
        nb = json.load(f)
    chunks: list[str] = []
    for cell in nb.get("cells", []):
        src = cell.get("source", [])
        if isinstance(src, list):
            chunks.extend(src)
        else:
            chunks.append(src)
        chunks.append("\n")
    return "".join(chunks)


def scan_project(
    root: str,
    encoding: str = "utf-8",
    ignore: Optional[list[str]] = None,
    follow_links: bool = True,
    scan_notebooks: bool = False,
    use_ast: bool = False,
    ignore_set: Optional[set[str]] = None,
) -> list[str]:
    ignores: set[str] = set(ignore_set or ())
    if ignore:
        for item in ignore:
            ignores.add(Path(Path(item).resolve()).name)
    seen: set[str] = set()
    ordered: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_links):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in ignores and not d.endswith(".egg-info") and (not d.startswith(".") or ignore_set is None)
        ]
        for fname in filenames:
            full = Path(dirpath).joinpath(fname)
            found: Iterable[str] = ()
            try:
                if fname.endswith((".py", ".pyw")):
                    with open(full, "r", encoding=encoding, errors="ignore") as fh:
                        content = fh.read()
                    found = parse_imports_ast(content) if use_ast else list(parse_imports_lines(content.splitlines()))
                elif fname.endswith(".ipynb") and scan_notebooks:
                    content = read_notebook(full, encoding=encoding)
                    found = parse_imports_ast(content) if use_ast else list(parse_imports_lines(content.splitlines()))
                else:
                    continue
            except (OSError, json.JSONDecodeError):
                continue
            for name in found:
                if name and name not in seen:
                    seen.add(name)
                    ordered.append(name)
                    print(f"found {name} in {fname}")
    return ordered


def load_offline_list(path: str) -> set[str]:
    result: set[str] = set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            result.add(_norm(line))
    return result


def get_installed(pip_cmd: str = "pip3") -> tuple[list[str], set[str]]:
    try:
        proc = subprocess.Popen(
            [pip_cmd, "freeze"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        out, _ = proc.communicate()
    except FileNotFoundError:
        print(f"[!] '{pip_cmd}' not found, skipping installed-package check")
        return [], set()
    raw: list[str] = []
    names: set[str] = set()
    for line in out.decode("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-e")):
            continue
        raw.append(line)
        name = line.split("==")[0].split("@")[0].strip()
        names.add(_norm(name))
    return raw, names


def detect_local_modules(root: str) -> set[str]:
    local: set[str] = set()
    root_abs = Path(root).resolve()
    for dirpath, dirnames, filenames in os.walk(root_abs):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in IGNORES_OFFLINE_FULL and not d.endswith(".egg-info") and not d.startswith(".")
        ]
        if "__init__.py" in filenames:
            local.add(_norm(Path(dirpath).name))
        for fname in filenames:
            if fname.endswith(".py") and fname not in {
                "setup.py",
                "conftest.py",
                "__init__.py",
            }:
                local.add(_norm(fname[:-3]))
    base = Path(root_abs).name
    if base and base not in {".", "/"}:
        local.add(_norm(base))
    return local


def detect_local_names(root: str) -> set[str]:
    names: set[str] = set()
    for dirpath, _dirnames, filenames in os.walk(root):
        names.add(Path(dirpath).name)
        for fname in filenames:
            if (str(Path(fname).with_suffix("")), Path(fname).suffix)[1] in (".py", ".pyw"):
                names.add((str(Path(fname).with_suffix("")), Path(fname).suffix)[0])
    return names


def map_imports_to_pypi(names: Iterable[str]) -> list[str]:
    mapped: set[str] = set()
    for name in names:
        mapped.add(IMPORT_MAPPING.get(name, name))
    return sorted(mapped, key=lambda s: s.lower())


def get_local_packages(encoding: str = "utf-8") -> dict[str, dict[str, Optional[str]]]:
    result: dict[str, dict[str, Optional[str]]] = {}
    skip = {"tests", "_tests", "egg", "EGG", "info"}
    for p in sys.path:
        if not p or not Path(p).is_dir():
            continue
        for dirpath, _dirnames, filenames in os.walk(p):
            for fname in filenames:
                if "top_level" not in fname:
                    continue
                full = Path(dirpath).joinpath(fname)
                parts = Path(dirpath).name.split("-")
                try:
                    with open(full, "r", encoding=encoding) as f:
                        contents = f.read().strip().split("\n")
                except OSError:
                    continue
                version: Optional[str] = None
                if len(parts) > 1:
                    version = parts[1].replace(".dist", "").replace(".egg", "")
                for name in contents:
                    if name and name not in skip and parts[0] not in skip:
                        result[name.lower()] = {"name": parts[0], "version": version}
    return result


def match_local(
    pkgs: Iterable[str],
    local: dict[str, dict[str, Optional[str]]],
) -> list[dict[str, Optional[str]]]:
    out: list[dict[str, Optional[str]]] = []
    for p in pkgs:
        entry = local.get(p.lower())
        if entry is not None:
            out.append(entry)
    uniq: list[dict[str, Optional[str]]] = []
    seen: set[tuple[str, Optional[str]]] = set()
    for entry in out:
        key = (entry["name"], entry["version"])
        if key not in seen:
            seen.add(key)
            uniq.append(entry)
    return uniq


def resolve_from_pypi(
    names: list[str],
    server: str = "https://pypi.python.org/pypi/",
    proxy: Optional[str] = None,
) -> list[dict[str, Optional[str]]]:
    results: list[dict[str, Optional[str]]] = []
    opener = urllib.request.build_opener()
    if proxy:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    for name in names:
        url = f"{server}{name}/json"
        try:
            with opener.open(url, timeout=15) as resp:
                if resp.status >= 300:
                    continue
                data = json.loads(resp.read().decode("utf-8"))
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            json.JSONDecodeError,
            OSError,
        ):
            print(f'[!] Package "{name}" does not exist or network problem')
            continue
        version = data.get("info", {}).get("version")
        results.append({"name": name, "version": version})
        print(f'[i] Resolved "{name}" -> {name}:{version}')
    return results


def parse_requirements_file(path: str) -> list[dict[str, Optional[str]]]:
    try:
        with open(path, "r") as f:
            lines = [line.strip() for line in f if line.strip()]
    except OSError as exc:
        print(f"Error opening {path}: {exc}")
        raise
    result: list[dict[str, Optional[str]]] = []
    delims = ["<", ">", "=", "!", "~"]
    for line in lines:
        if not line or not line[0].isalpha():
            continue
        matched = False
        for d in delims:
            if d in line:
                parts = line.split(d)
                entry = {"name": parts[0], "version": parts[-1].replace("=", "")}
                if entry not in result:
                    result.append(entry)
                matched = True
                break
        if not matched:
            entry = {"name": line, "version": None}
            if entry not in result:
                result.append(entry)
    return result


def show_diff(req_file: str, resolved: list[dict[str, Optional[str]]]) -> None:
    existing = {r["name"] for r in parse_requirements_file(req_file)}
    imported = {r["name"] for r in resolved}
    diff = existing - imported
    print(f"[i] The following modules are in {req_file} but do not seem to be imported: {','.join(diff)}")


def clean_file(req_file: str, resolved: list[dict[str, Optional[str]]]) -> None:
    existing = {r["name"] for r in parse_requirements_file(req_file)}
    imported = {r["name"] for r in resolved}
    to_remove = existing - imported
    if not to_remove:
        print(f"[i] Nothing to clean in {req_file}")
        return
    pat = re.compile("|".join(re.escape(n) for n in to_remove))
    try:
        with open(req_file, "r+") as f:
            kept = [line for line in f if not pat.match(line)]
            f.seek(0)
            f.truncate()
            f.writelines(kept)
    except OSError:
        print(f"[!] Failed on file: {req_file}")
        raise
    print(f"[i] Successfully cleaned up requirements in {req_file}")


def write_requirements(
    out_path: str,
    pkgs: list[dict[str, Optional[str]]],
    delimiter: str,
) -> None:
    lines: list[str] = []
    for entry in pkgs:
        if entry["version"]:
            lines.append(f"{entry['name']}{delimiter}{entry['version']}")
        else:
            lines.append(entry["name"])
    content = "\n".join(lines) + ("\n" if lines else "")
    if out_path == "-":
        sys.stdout.write(content)
    else:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(content)


def run_offline(args: argparse.Namespace) -> int:
    path = args.path or os.curdir
    known = load_offline_list(args.pypi_list)
    print(f"[i] Loaded {len(known)} packages from {args.pypi_list}")
    local: set[str] = set()
    if args.detect_local:
        local = detect_local_modules(path)
        print(f"[i] Detected {len(local)} local modules/packages")
    imports = scan_project(
        root=path,
        encoding=args.encoding,
        ignore=args.ignore,
        follow_links=not args.no_follow_links,
        scan_notebooks=args.scan_notebooks,
        use_ast=args.ast,
        ignore_set=IGNORES_OFFLINE_FULL if args.detect_local else None,
    )
    print(f"[i] Found {len(imports)} unique imports in source")
    _raw, installed = get_installed(args.pip_cmd)
    print(f"[i] {len(installed)} packages installed locally")
    stdlib = {_norm(m) for m in STDLIB}
    skip_std: list[str] = []
    skip_inst: list[str] = []
    skip_local: list[str] = []
    keep: list[str] = []
    unknown: list[str] = []
    for name in imports:
        n = _norm(name)
        if n in stdlib:
            skip_std.append(name)
            continue
        if n in local:
            skip_local.append(name)
            continue
        if n in installed:
            skip_inst.append(name)
            continue
        if n in known:
            keep.append(name)
        else:
            unknown.append(name)
    print(f"[i] Skipped {len(skip_std)} stdlib modules")
    if args.detect_local:
        print(f"[i] Skipped {len(skip_local)} local modules:{','.join(sorted(skip_local)) or '-'}")
    print(f"[i] Skipped {len(skip_inst)} already-installed modules")
    if unknown:
        label = "unknown" if args.detect_local else "local/unknown"
        print(f"[i] Skipped {len(unknown)} {label} modules:{','.join(unknown)}")
    out_path = Path(path).joinpath("requirements.txt")
    final = sorted(set(keep))
    with open(out_path, "w", encoding="utf-8") as f:
        if final:
            f.write("\n".join(final) + "\n")
    print(f"\n[\u2713] Wrote {len(final)} packages to {out_path}")
    return 0


def run_online(args: argparse.Namespace) -> int:
    path = Path(args.path or os.curdir).absolute()
    imports = scan_project(
        root=path,
        encoding=args.encoding,
        ignore=args.ignore,
        follow_links=not args.no_follow_links,
        scan_notebooks=args.scan_notebooks,
        use_ast=True,
        ignore_set=IGNORES_ONLINE,
    )
    local_names = detect_local_names(path)
    imports = [i for i in imports if i not in local_names]
    print(f"[i] Found {len(imports)} unique imports in source")
    mapped = map_imports_to_pypi(imports)
    if args.debug:
        print(f"[debug] Mapped imports: {mapped}")
    local_pkgs = get_local_packages(args.encoding)
    local_resolved = match_local(mapped, local_pkgs)
    if args.use_local:
        resolved = local_resolved
    else:
        covered = {r["name"].lower() for r in local_resolved}
        to_lookup = [p for p in mapped if p.lower() not in covered]
        resolved = local_resolved + resolve_from_pypi(
            to_lookup,
            server=args.pypi_server,
            proxy=args.proxy,
        )
    resolved = sorted(resolved, key=lambda x: x["name"].lower())
    if args.diff:
        show_diff(args.diff, resolved)
        return 0
    if args.clean:
        clean_file(args.clean, resolved)
        return 0
    if args.mode == "no-pin":
        for r in resolved:
            r["version"] = ""
        delim = ""
    elif args.mode == "gt":
        delim = ">="
    elif args.mode == "compat":
        delim = "~="
    else:
        delim = "=="
    out_path = args.savepath or Path(path).joinpath("requirements.txt")
    if not args.print_only and not args.savepath and not args.force and Path(out_path).exists():
        print("[!] requirements.txt already exists, use --force to overwrite it")
        return 1
    if args.print_only:
        write_requirements("-", resolved, delim)
        print("[i] Successfully output requirements")
    else:
        write_requirements(out_path, resolved, delim)
        print(f"[i] Successfully saved requirements file in {out_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="merged.py",
        description="Merged requirements.txt generator (imp1+imp2+pipreqs+pr2).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="mode", required=True)
    off = sub.add_parser(
        "offline",
        help="use a local PyPI package list (no network); mirrors imp1/imp2",
    )
    off.add_argument("-p", "--path", default=None, help="project directory (default: current dir)")
    off.add_argument(
        "-l",
        "--pypi-list",
        default="/sdcard/data/pip.txt",
        help="path to the offline PyPI package list",
    )
    off.add_argument("--pip-cmd", default="pip3", help="pip executable used for `pip freeze`")
    off.add_argument(
        "--detect-local",
        action="store_true",
        help="enable imp2-style local-module detection",
    )
    off.add_argument(
        "--ast",
        action="store_true",
        help="parse imports via AST instead of line-by-line",
    )
    off.add_argument("--encoding", default="utf-8")
    off.add_argument(
        "--ignore",
        action="append",
        default=[],
        help="additional directory basenames to ignore",
    )
    off.add_argument("--no-follow-links", action="store_true")
    off.add_argument("--scan-notebooks", action="store_true")
    off.set_defaults(func=run_offline)
    on = sub.add_parser(
        "online",
        help="resolve via PyPI JSON API; mirrors pipreqs/pr2",
    )
    on.add_argument("-p", "--path", default=None)
    on.add_argument("--savepath", default=None, help="path for the generated requirements file")
    on.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="print requirements to stdout instead of writing",
    )
    on.add_argument("--force", action="store_true", help="overwrite existing requirements.txt")
    on.add_argument("--use-local", action="store_true", help="use local installation metadata only")
    on.add_argument(
        "--pypi-server",
        default="https://pypi.python.org/pypi/",
        help="PyPI JSON base URL (pr2 used a Tsinghua mirror)",
    )
    on.add_argument("--proxy", default=None)
    on.add_argument(
        "--mode",
        choices=["compat", "gt", "no-pin"],
        default=None,
        help="version scheme: compat '~=', gt '>=', no-pin ''",
    )
    on.add_argument("--diff", default=None, help="show entries in FILE not imported by the project")
    on.add_argument("--clean", default=None, help="remove unused entries from FILE")
    on.add_argument("--encoding", default="utf-8")
    on.add_argument("--ignore", action="append", default=[])
    on.add_argument("--no-follow-links", action="store_true")
    on.add_argument("--scan-notebooks", action="store_true")
    on.add_argument("--debug", action="store_true")
    on.set_defaults(func=run_online)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
