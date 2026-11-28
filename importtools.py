#!/data/data/com.termux/files/usr/bin/env python
"""
Inspect and modify Python imports.

Install the CLI dependency with pip install typer. The find-py2 command also requires rapidfuzz, tree-sitter and tree-sitter-python. Run python import_tools.py --help for commands. Autofixes are heuristic: review the diff and use version control before modifying a project.

"""

from __future__ import annotations

import ast
import builtins
import importlib.metadata
import importlib.util
import os
import re
import shlex
import sys
import tokenize
import traceback
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Annotated, Any

import typer
from dh import atomic_write

app = typer.Typer(no_args_is_help=True, help="Inspect and manage Python imports.")
MAX_WORKERS: int = os.cpu_count() or 1
SKIP_DIRS = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist"}
)
STDLIB = frozenset(sys.stdlib_module_names)
BUILTINS = frozenset(dir(builtins))
IGNORED_AUTOFIX = frozenset({"imp", "cmd", "keyword", "token"})
DEFAULT_SHEBANG: str = "#!/data/data/com.termux/files/usr/bin/env python"
PACKAGE_MAPPING: dict[str, str] = {
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "sklearn": "scikit-learn",
    "yaml": "PyYAML",
    "dotenv": "python-dotenv",
    "bs4": "beautifulsoup4",
    "fitz": "pymupdf",
    "skimage": "scikit-image",
    "telegram": "python-telegram-bot",
    "dateutil": "python-dateutil",
    "git": "GitPython",
    "jwt": "PyJWT",
    "OpenGL": "PyOpenGL",
}
STDLIB_MEMBER_MAP: dict[str, set[str]] = {
    "os": {
        "path",
        "environ",
        "getenv",
        "listdir",
        "walk",
        "remove",
        "rename",
        "mkdir",
        "makedirs",
        "chdir",
        "getcwd",
        "sep",
        "linesep",
    },
    "sys": {
        "argv",
        "path",
        "stdin",
        "stdout",
        "stderr",
        "exit",
        "version",
        "platform",
        "executable",
        "modules",
    },
    "math": {
        "sqrt",
        "ceil",
        "floor",
        "sin",
        "cos",
        "tan",
        "pi",
        "e",
        "log",
        "log10",
        "exp",
        "pow",
        "fabs",
        "factorial",
    },
    "random": {
        "random",
        "randint",
        "choice",
        "shuffle",
        "sample",
        "uniform",
        "seed",
        "randrange",
    },
    "datetime": {"datetime", "date", "time", "timedelta", "timezone"},
    "json": {"dumps", "loads", "dump", "load"},
    "collections": {"defaultdict", "OrderedDict", "Counter", "deque", "namedtuple"},
    "itertools": {
        "chain",
        "cycle",
        "repeat",
        "count",
        "islice",
        "groupby",
        "combinations",
        "permutations",
        "product",
    },
    "functools": {"reduce", "partial", "lru_cache", "wraps", "cache"},
    "pathlib": {"Path", "PurePath", "PurePosixPath", "PureWindowsPath"},
    "re": {"match", "search", "findall", "sub", "compile", "split", "escape"},
    "argparse": {"ArgumentParser", "Namespace"},
    "logging": {
        "debug",
        "info",
        "warning",
        "error",
        "critical",
        "getLogger",
        "basicConfig",
    },
    "statistics": {"mean", "median", "mode", "stdev", "variance"},
    "typing": {
        "List",
        "Dict",
        "Set",
        "Tuple",
        "Optional",
        "Union",
        "Any",
        "Callable",
        "Iterator",
    },
    "decimal": {"Decimal"},
    "fractions": {"Fraction"},
    "hashlib": {"md5", "sha1", "sha256", "sha512"},
    "subprocess": {"run", "Popen", "call", "check_output"},
    "shutil": {"copy", "copy2", "move", "rmtree", "make_archive"},
    "tempfile": {"NamedTemporaryFile", "TemporaryFile", "mkdtemp"},
    "glob": {"glob"},
    "time": {"time", "sleep", "ctime", "localtime", "gmtime", "strftime", "strptime"},
}


def python_files(root: Path, extra_excludes: set[str] | None = None) -> list[Path]:
    """Find Python files and extensionless Python-shebang scripts without following links."""
    skip = SKIP_DIRS | (extra_excludes or set())
    if root.is_file():
        candidates = [root]
    else:
        candidates = []
        for folder, dirs, files in os.walk(root):
            dirs[:] = [
                d for d in dirs if d not in skip and not (Path(folder) / d).is_symlink()
            ]
            candidates.extend(Path(folder) / name for name in files)
    result = []
    for path in candidates:
        if path.is_symlink():
            continue
        if path.suffix == ".py":
            result.append(path)
        elif not path.suffix:
            try:
                with path.open("rb") as stream:
                    first = stream.readline().lower()
                if first.startswith(b"#!") and b"python" in first:
                    result.append(path)
            except OSError:
                continue
    return sorted(result)


def read_source(path: Path) -> str:
    """Read Python source using its declared encoding."""
    with tokenize.open(path) as stream:
        return stream.read()


def write_source(path: Path, text: str) -> None:
    """Write using the file's existing declared encoding."""
    with tokenize.open(path) as stream:
        encoding = stream.encoding
    atomic_write(path, text, encoding=encoding)


def parse_file(path: Path) -> ast.Module | None:
    """Parse a source file, reporting files that cannot be inspected."""
    try:
        return ast.parse(read_source(path), filename=str(path), type_comments=True)
    except (SyntaxError, UnicodeError, OSError, LookupError) as exc:
        typer.echo(f"Skipping {path}: {exc}", err=True)
        return None


def validate_root(root: Path, *, directory: bool = False) -> Path:
    """Resolve and validate an input file or directory."""
    root = root.resolve()
    if not root.exists() or (directory and not root.is_dir()):
        msg = f"Invalid {'directory' if directory else 'path'}: {root}"
        raise typer.BadParameter(msg)
    return root


def insertion_line(tree: ast.Module, lines: list[str]) -> int:
    """Return the line index after the docstring and future imports."""
    pos = 0
    if lines and lines[0].startswith("#!"):
        pos = 1
    for i in range(min(2, len(lines))):
        if re.match(r"^[ \t\f]*#.*?coding[:=][ \t]*[-\w.]+", lines[i]):
            pos = max(pos, i + 1)
    body = tree.body
    index = 0
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        pos = max(pos, body[0].end_lineno or body[0].lineno)
        index = 1
    for node in body[index:]:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            pos = node.end_lineno or node.lineno
        else:
            break
    return pos


def insert_imports(path: Path, imports: list[str]) -> bool:
    """Insert imports after a module docstring and future imports."""
    tree = parse_file(path)
    if tree is None:
        return False
    src = read_source(path)
    lines = src.splitlines(keepends=True)
    pos = insertion_line(tree, lines)
    if pos and not lines[pos - 1].endswith(("\n", "\r")):
        lines[pos - 1] += "\n"
    lines[pos:pos] = [item.rstrip("\n") + "\n" for item in imports]
    write_source(path, "".join(lines))
    return True


def collect_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Collect bound and loaded identifiers; this is not scope-aware."""
    bound: set[str] = set()
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bound.update(a.asname or a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            bound.update(a.asname or a.name for a in node.names if a.name != "*")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                used.add(node.id)
            else:
                bound.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
    return bound, used


def detect_missing(path: Path, strategy: str) -> list[str]:
    """Suggest possible imports for unbound names using a selected heuristic."""
    tree = parse_file(path)
    if tree is None:
        return []
    bound, used = collect_names(tree)
    suggestions: set[str] = set()
    for name in sorted(used - bound - BUILTINS - IGNORED_AUTOFIX):
        if name.startswith("_"):
            continue
        if strategy == "spec":
            try:
                if importlib.util.find_spec(name) is not None:
                    suggestions.add(f"import {name}")
            except (ImportError, ModuleNotFoundError, ValueError, AttributeError):
                pass
        elif name in STDLIB:
            suggestions.add(f"import {name}")
        elif strategy == "mapped":
            for module, members in STDLIB_MEMBER_MAP.items():
                if name in members:
                    suggestions.add(f"from {module} import {name}")
                    break
    return sorted(suggestions)


@app.command("check-missing")
def check_missing(
    directory: Annotated[Path, typer.Option("--directory", "-d")] = Path(),
    strategy: Annotated[str, typer.Option(help="spec, stdlib, or mapped")] = "spec",
    autofix: Annotated[bool, typer.Option("--autofix", "-a")] = False,
    exclude: Annotated[list[str] | None, typer.Option("--exclude", "-e")] = None,
) -> None:
    """Find possible missing imports; optionally insert suggestions."""
    if strategy not in {"spec", "stdlib", "mapped"}:
        msg = "Must be spec, stdlib, or mapped"
        raise typer.BadParameter(msg, param_hint="--strategy")
    root = validate_root(directory, directory=True)
    files = python_files(root, set(exclude or []))
    total = failed = fixed = 0
    for path in files:
        missing = detect_missing(path, strategy)
        if missing:
            total += len(missing)
            typer.echo(
                f"\n{path.relative_to(root)}:\n"
                + "\n".join(f"  - {s}" for s in missing)
            )
            if autofix:
                if insert_imports(path, missing):
                    fixed += 1
                else:
                    failed += 1
    typer.echo(f"Total possible missing imports: {total}; files fixed: {fixed}")
    if failed or (total and not autofix):
        raise typer.Exit(1)


def misplaced_imports(tree: ast.Module) -> list[ast.Import | ast.ImportFrom]:
    """Return module-level imports occurring after executable statements."""
    seen_code = False
    result: list[ast.Import | ast.ImportFrom] = []
    for index, node in enumerate(tree.body):
        if (
            index == 0
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if seen_code:
                result.append(node)
        else:
            seen_code = True
    return result


@app.command("check-position")
def check_position(
    directory: Annotated[Path, typer.Option("--directory", "-d")] = Path(),
    deep: Annotated[
        bool, typer.Option(help="Enable moving misplaced module-level imports.")
    ] = False,
    autofix: Annotated[bool, typer.Option("--autofix", "-a")] = False,
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
) -> None:
    """Report module-level imports after code; nested imports are left alone."""
    if autofix and not deep:
        msg = "--autofix requires --deep"
        raise typer.BadParameter(msg)
    root = validate_root(directory)
    reports: list[str] = []
    failed = False
    for path in python_files(root):
        tree = parse_file(path)
        if tree is None:
            continue
        offenders = misplaced_imports(tree)
        if not offenders:
            continue
        label = str(path.relative_to(root)) if root.is_dir() else path.name
        reports.append(
            f"{label}:\n"
            + "\n".join(f"  Line {n.lineno}: import after code" for n in offenders)
        )
        if autofix:
            # Refuse ambiguous physical lines rather than accidentally deleting code or comments.
            source = read_source(path)
            lines = source.splitlines(keepends=True)
            if any(
                n.col_offset != 0
                or n.end_col_offset
                != len(lines[(n.end_lineno or n.lineno) - 1].rstrip("\r\n").encode())
                for n in offenders
            ):
                typer.echo(
                    f"Cannot safely move imports in {path}; fix manually.", err=True
                )
                failed = True
                continue
            chunks = ["".join(lines[n.lineno - 1 : n.end_lineno]) for n in offenders]
            for node in reversed(offenders):
                del lines[node.lineno - 1 : node.end_lineno]
            remaining = "".join(lines)
            try:
                new_tree = ast.parse(remaining)
                pos = insertion_line(new_tree, lines)
                lines[pos:pos] = [
                    chunk if chunk.endswith("\n") else chunk + "\n" for chunk in chunks
                ]
                ast.parse("".join(lines))
                write_source(path, "".join(lines))
            except SyntaxError:
                typer.echo(
                    f"Cannot safely move imports in {path}; fix manually.", err=True
                )
                failed = True
    report = "\n\n".join(reports) if reports else "No misplaced imports found!"
    typer.echo(report)
    if output:
        atomic_write(output, report + "\n", encoding="utf-8")
    if failed or (reports and not autofix):
        raise typer.Exit(1)


@app.command("add-import")
def add_import(
    name: str,
    directory: Annotated[Path, typer.Option("--directory", "-d")] = Path(),
    shebang: Annotated[
        str,
        typer.Option(help="Shebang for files without one; empty disables insertion."),
    ] = DEFAULT_SHEBANG,
) -> None:
    """Add an import to each Python file, preserving docstrings and future imports."""
    if not all(part.isidentifier() for part in name.split(".")):
        msg = "Expected a dotted Python module name."
        raise typer.BadParameter(msg)
    root = validate_root(directory)
    failures = 0
    for path in python_files(root):
        if not insert_imports(path, [f"import {name}"]):
            failures += 1
            continue
        src = read_source(path)
        if shebang and not src.startswith("#!"):
            write_source(path, shebang.rstrip("\r\n") + "\n" + src)
        typer.echo(f"Processed {path}")
    if failures:
        raise typer.Exit(1)


@app.command("check-load")
def check_load(
    files: Annotated[list[Path], typer.Argument(help="Files to execute as modules.")],
) -> None:
    """Execute each file as a module to detect load errors (executes arbitrary code)."""
    failed = False
    for index, path in enumerate(files):
        try:
            spec = importlib.util.spec_from_file_location(
                f"_import_check_{index}", path
            )
            if spec is None or spec.loader is None:
                msg = f"Cannot load {path}"
                raise ImportError(msg)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            try:
                spec.loader.exec_module(module)
            finally:
                sys.modules.pop(spec.name, None)
        except Exception:  # noqa: BLE001 - executing arbitrary user modules
            failed = True
            typer.echo(str(path), err=True)
            traceback.print_exc()
    if failed:
        raise typer.Exit(1)


def extract_module_name(text: str) -> str:
    """Extract the first absolute module from an import statement."""
    match = re.match(r"\s*(?:from|import)\s+([\w.]+)", text)
    if not match or match.group(1).startswith("."):
        return ""
    return match.group(1).split(".")[0]


@app.command("find-py2")
def find_py2(
    directory: Annotated[Path, typer.Option("--directory", "-d")] = Path(),
    threshold: Annotated[int, typer.Option(min=0, max=100)] = 85,
    no_color: Annotated[bool, typer.Option("--no-color")] = False,
) -> None:
    """Find stdlib-like top-level imports using tree-sitter and fuzzy matching.

    This is a broad heuristic, not a reliable Python 2 detector.
    """
    try:
        import tree_sitter_python as tsp
        from rapidfuzz import fuzz
        from tree_sitter import Language, Parser
    except ImportError as exc:
        typer.echo(
            f"Install rapidfuzz, tree-sitter and tree-sitter-python: {exc}", err=True
        )
        raise typer.Exit(2) from exc
    root = validate_root(directory)
    parser = Parser(Language(tsp.language()))
    stdlib = sorted(m.lower() for m in STDLIB)
    excluded = {
        "io",
        "os",
        "pathlib",
        "urllib",
        "tkinter",
        "pickle",
        "string",
        "queue",
        "urllib3",
        "configparser",
        "copyreg",
        "httplib2",
    }
    for path in python_files(root):
        src = path.read_bytes()
        tree = parser.parse(src)
        names = {
            extract_module_name(src[n.start_byte : n.end_byte].decode(errors="replace"))
            for n in tree.root_node.children
            if n.type in {"import_statement", "import_from_statement"}
        }
        for name in sorted(names - {""}):
            low = name.lower()
            label = str(path.relative_to(root)) if root.is_dir() else path.name
            if low in stdlib and low not in {"io", "os", "pathlib", "ast", "urllib"}:
                typer.secho(
                    f"{label}: {name}", fg=None if no_color else typer.colors.CYAN
                )
            elif low not in excluded and len(low) > 3:
                for candidate in stdlib:
                    score = fuzz.ratio(low, candidate)
                    if len(candidate) > 3 and score > threshold:
                        typer.secho(
                            f"{label}: {low} / {candidate} / {score:.1f}",
                            fg=None if no_color else typer.colors.YELLOW,
                        )
                        break


class ImportTransformer(ast.NodeTransformer):
    """Convert simple module-qualified accesses to from-imports.

    This transformation is intentionally conservative, but may still alter
    semantics when modules are mutated dynamically or names are shadowed.
    """

    def __init__(self, tree: ast.Module) -> None:
        """Identify unaliased modules used only as attribute bases."""
        self.names: dict[str, set[str]] = {}
        self.modified = False
        imports = [
            a.name
            for n in tree.body
            if isinstance(n, ast.Import)
            for a in n.names
            if not a.asname and "." not in a.name
        ]
        parents = {
            child: node
            for node in ast.walk(tree)
            for child in ast.iter_child_nodes(node)
        }
        for module in imports:
            references = [
                n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == module
            ]
            attributes: set[str] = set()
            for reference in references:
                parent = parents.get(reference)
                if (
                    not isinstance(parent, ast.Attribute)
                    or parent.value is not reference
                    or not isinstance(reference.ctx, ast.Load)
                    or not isinstance(parent.ctx, ast.Load)
                ):
                    break
                attributes.add(parent.attr)
            else:
                bound, _ = collect_names(tree)
                if attributes and not (attributes & bound):
                    self.names[module] = attributes

    def visit_Import(self, node: ast.Import) -> ast.AST | list[ast.AST]:
        """Replace eligible imports while retaining unrelated aliases."""
        result: list[ast.AST] = []
        for alias in node.names:
            if not alias.asname and alias.name in self.names:
                self.modified = True
                result.append(
                    ast.copy_location(
                        ast.ImportFrom(
                            module=alias.name,
                            names=[
                                ast.alias(name=n)
                                for n in sorted(self.names[alias.name])
                            ],
                            level=0,
                        ),
                        node,
                    )
                )
            else:
                result.append(ast.copy_location(ast.Import(names=[alias]), node))
        return result

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        """Replace eligible attribute expressions with imported names."""
        if isinstance(node.value, ast.Name) and node.value.id in self.names:
            self.modified = True
            return ast.copy_location(ast.Name(id=node.attr, ctx=node.ctx), node)
        return self.generic_visit(node)


@app.command("transform")
def transform(file: Path) -> None:
    """Rewrite simple imports using AST; comments and formatting will be lost."""
    if not file.is_file() or file.suffix != ".py":
        msg = "Expected an existing .py file."
        raise typer.BadParameter(msg)
    tree = parse_file(file)
    if tree is None:
        raise typer.Exit(1)
    transformer = ImportTransformer(tree)
    new_tree = transformer.visit(tree)
    if transformer.modified:
        ast.fix_missing_locations(new_tree)
        # Preserve shebang and coding cookie; unparse removes all comments.
        original = read_source(file).splitlines(keepends=True)
        prefix = "".join(
            line
            for i, line in enumerate(original[:2])
            if (i == 0 and line.startswith("#!"))
            or re.match(r"^[ \t\f]*#.*?coding[:=]", line)
        )
        write_source(file, prefix + ast.unparse(new_tree) + "\n")
        typer.echo(f"Transformed {file}; review changes for semantic differences.")
    else:
        typer.echo(f"No transformations needed for {file}.")


def import_lines_of(path: Path) -> tuple[Path, list[str]]:
    """Extract source text for all import statements in a file."""
    tree = parse_file(path)
    if tree is None:
        return path, []
    source_lines = read_source(path).splitlines()
    imports = sorted(
        (n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))),
        key=lambda n: (n.lineno, n.col_offset),
    )
    return path, ["\n".join(source_lines[n.lineno - 1 : n.end_lineno]) for n in imports]


def top_level_modules_of(path: Path) -> tuple[Path, set[str]]:
    """Return imported top-level module names, including nested imports."""
    tree = parse_file(path)
    names: set[str] = set()
    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return path, names


def module_contexts(path: Path) -> list[str]:
    """Describe imports and their nearest function or class context."""
    tree = parse_file(path)
    if tree is None:
        return []
    notes: list[str] = []

    def visit(node: ast.AST, context: str) -> None:
        for child in ast.iter_child_nodes(node):
            sub = context
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                sub = f"in {child.name}()"
            elif isinstance(child, ast.ClassDef):
                sub = f"in class {child.name}"
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                notes.append(f"{ast.unparse(child)} [{sub}] (line {child.lineno})")
            visit(child, sub)

    visit(tree, "top-level")
    return notes


def local_module_names(root: Path) -> set[str]:
    """Get local top-level modules and regular packages for dependency filtering."""
    base = root if root.is_dir() else root.parent
    names = {p.stem for p in base.glob("*.py")}
    names.update(
        p.name for p in base.iterdir() if p.is_dir() and (p / "__init__.py").exists()
    )
    return names


def all_modules(files: list[Path]) -> set[str]:
    """Collect imported names across files using worker processes."""
    names: set[str] = set()
    if files:
        with ProcessPoolExecutor(max_workers=min(MAX_WORKERS, len(files))) as pool:
            for _, found in pool.map(top_level_modules_of, files):
                names.update(found)
    return names


@app.command("lines")
def lines(
    root: Annotated[Path, typer.Argument()] = Path(),
    out_dir: Annotated[Path, typer.Option("--out-dir", "-o")] = Path("output"),
) -> None:
    """Save each source file's import statements in a parallel output tree."""
    root = validate_root(root)
    files = python_files(root)
    written = 0
    for path in files:
        _, imports = import_lines_of(path)
        if imports:
            relative = path.relative_to(root) if root.is_dir() else Path(path.name)
            dest = out_dir / relative.with_suffix(".imports.txt")
            dest.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(dest, "\n".join(imports) + "\n", encoding="utf-8")
            written += 1
    typer.echo(f"Wrote imports for {written} file(s) under {out_dir}")


@app.command("modules")
def modules(
    root: Annotated[Path, typer.Argument()] = Path(),
    filter: Annotated[str, typer.Option(help="all, stdlib, or third-party")] = "all",
    details: bool = False,
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
) -> None:
    """List imported modules or display imports with their nesting context."""
    if filter not in {"all", "stdlib", "third-party"}:
        msg = "Must be all, stdlib, or third-party"
        raise typer.BadParameter(msg, param_hint="--filter")
    root = validate_root(root)
    files = python_files(root)
    if details:
        if output is not None:
            msg = "--output is not supported with --details"
            raise typer.BadParameter(msg)
        for path in files:
            notes = module_contexts(path)
            if notes:
                typer.echo(f"\n{path}:\n" + "\n".join(f"  {n}" for n in notes))
        return
    names = all_modules(files)
    local = local_module_names(root)
    if filter == "third-party":
        names -= STDLIB | local | {"__future__"}
    elif filter == "stdlib":
        names &= STDLIB
    text = "\n".join(sorted(names))
    if output:
        atomic_write(output, text + ("\n" if text else ""), encoding="utf-8")
        typer.echo(f"Saved {len(names)} module(s) to {output}")
    else:
        if text:
            typer.echo(text)
        typer.echo(f"# {len(names)} module(s) from {len(files)} file(s)", err=True)


def is_installed(module_name: str) -> bool:
    """Check whether a distribution or importable module appears installed."""
    distribution = PACKAGE_MAPPING.get(module_name, module_name)
    try:
        importlib.metadata.distribution(distribution)
        return True
    except importlib.metadata.PackageNotFoundError:
        try:
            return importlib.util.find_spec(module_name) is not None
        except (ImportError, ModuleNotFoundError, ValueError, AttributeError):
            return False


@app.command("requirements")
def requirements(
    root: Annotated[Path, typer.Argument()] = Path(),
    out: Annotated[Path, typer.Option("--out", "-o")] = Path("requirements.txt"),
) -> None:
    """Generate unpinned, heuristic requirements and a missing-dependency script."""
    root = validate_root(root)
    deps = sorted(
        all_modules(python_files(root))
        - STDLIB
        - local_module_names(root)
        - {"__future__"}
    )
    packages = sorted({PACKAGE_MAPPING.get(d, d) for d in deps})
    atomic_write(
        out, "\n".join(packages) + ("\n" if packages else ""), encoding="utf-8"
    )
    installed = [d for d in deps if is_installed(d)]
    missing = sorted({PACKAGE_MAPPING.get(d, d) for d in deps if d not in installed})
    typer.echo(
        f"Wrote {len(packages)} unpinned requirement(s) to {out}; review package mappings."
    )
    if installed:
        typer.echo(f"Already installed: {', '.join(installed)}")
    if missing:
        script = Path("install_deps.sh")
        atomic_write(
            script,
            "#!/bin/sh\npython -m pip install "
            + " ".join(shlex.quote(x) for x in missing)
            + "\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        typer.echo(f"Missing: {', '.join(missing)} - review and run ./{script.name}")


if __name__ == "__main__":
    app()
