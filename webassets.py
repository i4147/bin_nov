#!/data/data/com.termux/files/usr/bin/env python
"""Unified web-asset minifier and Meson JSON-doc validator."""

import argparse
from concurrent.futures import ProcessPoolExecutor
import contextlib
from dataclasses import dataclass
from functools import partial
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterable, Optional, Sequence

from dh import MAX_WORKERS


try:
    from rcssmin import cssmin as _rcssmin
except ImportError:
    _rcssmin = None
try:
    from rjsmin import jsmin as _rjsmin
except ImportError:
    _rjsmin = None
_NONE_TYPE = type(None)
_ANSI: dict[str, str] = {
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "cyan": "\033[36m",
    "white": "\033[37m",
    "grey": "\033[90m",
    "reset": "\033[0m",
}


def cprint(msg: str, color: str = "white") -> None:
    print(f"{_ANSI.get(color, '')}{msg}{_ANSI['reset']}")


def file_size(p: Path | str) -> int:
    try:
        return Path(p).stat().st_size
    except OSError:
        return 0


def dir_size(p: Path | str) -> int:
    p = Path(p)
    if p.is_file():
        return file_size(p)
    if not p.is_dir():
        return 0
    return sum(file_size(f) for f in p.rglob("*") if f.is_file())


def human_size(n: float) -> str:
    sign = "-" if n < 0 else ""
    n = abs(float(n))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{sign}{n:.2f}{unit}"
        n /= 1024
    return f"{sign}{n:.2f}TB"


def find_files(
    root: Path | str,
    extensions: Sequence[str],
    exclude_parts: Sequence[str] = (),
) -> list[Path]:
    root = Path(root)
    exts = tuple(e.lower() for e in extensions)
    out: list[Path] = []
    if root.is_file():
        if any(root.name.lower().endswith(e) for e in exts):
            out.append(root)
        return out
    if not root.exists():
        return []
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        if exclude_parts and any(p in f.parts for p in exclude_parts):
            continue
        if any(f.name.lower().endswith(e) for e in exts):
            out.append(f)
    return sorted(set(out))


def parallel_map(
    func: Callable[[Any], Any],
    items: list[Any],
    workers: Optional[int] = None,
) -> list[Any]:
    if not items:
        return []
    if workers is None:
        workers = os.cpu_count() or 1
    if workers <= 1 or len(items) == 1:
        return [func(i) for i in items]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(func, items))


def require_tool(tool: str, hint: str = "") -> None:
    if shutil.which(tool) is None:
        msg = f"'{tool}' is not installed."
        if hint:
            msg += " " + hint
        raise RuntimeError(msg)


@dataclass(slots=True)
class Result:
    path: Path
    original_size: int
    minified_size: int
    success: bool
    error: Optional[str] = None
    no_change: bool = False
    dry_run: bool = False

    @property
    def saved(self) -> int:
        return self.original_size - self.minified_size

    @property
    def ratio(self) -> float:
        if self.original_size == 0:
            return 0.0
        return (1 - self.minified_size / self.original_size) * 100


def _relative(path: Path) -> Path | str:
    try:
        return path.relative_to(Path.cwd())
    except ValueError:
        return path


def print_file_result(r: Result) -> None:
    rel = _relative(r.path)
    if not r.success:
        cprint(f"✗ {rel}: {r.error}", "red")
    elif r.no_change:
        cprint(f"= {rel}: (no change)", "yellow")
    elif r.dry_run:
        cprint(f"~ {rel}: would save {human_size(r.saved)} ({r.ratio:.1f}%)", "cyan")
    else:
        cprint(
            f"✓ {rel}: {human_size(r.original_size)} → "
            f"{human_size(r.minified_size)} "
            f"(-{human_size(r.saved)}, {r.ratio:.1f}%)",
            "green",
        )


def print_summary(results: Sequence[Result], title: str = "Summary") -> None:
    total = len(results)
    ok = sum(1 for r in results if r.success and not r.no_change)
    nc = sum(1 for r in results if r.success and r.no_change)
    fail = total - ok - nc
    orig = sum(r.original_size for r in results)
    new = sum(r.minified_size for r in results)
    saved = orig - new
    ratio = (saved / orig * 100) if orig else 0.0
    print("=" * 40)
    print(title)
    print("=" * 40)
    cprint(f"Files processed: {total}", "white")
    cprint(f"✓ Successful:    {ok}", "green")
    if nc:
        cprint(f"= Unchanged:     {nc}", "yellow")
    if fail:
        cprint(f"✗ Failed:        {fail}", "red")
    cprint(f"Original size:   {human_size(orig)}", "white")
    cprint(f"Minified size:   {human_size(new)}", "white")
    cprint(f"Total saved:     {human_size(saved)} ({ratio:.1f}%)", "green")
    print("=" * 40)


HTML_HMIN_CONFIG: dict[str, Any] = {
    "collapseBooleanAttributes": True,
    "collapseInlineTagWhitespace": True,
    "collapseWhitespace": True,
    "conservativeCollapse": False,
    "decodeEntities": True,
    "html5": True,
    "includeAutoGeneratedTags": False,
    "keepClosingSlash": False,
    "minifyCSS": True,
    "minifyJS": True,
    "minifyURLs": True,
    "preserveLineBreaks": False,
    "preventAttributesEscaping": False,
    "processConditionalComments": True,
    "processScripts": ["text/html"],
    "quoteCharacter": '"',
    "removeComments": True,
    "removeEmptyAttributes": True,
    "removeEmptyElements": True,
    "removeRedundantAttributes": True,
    "removeScriptTypeAttributes": True,
    "removeStyleLinkTypeAttributes": True,
    "removeTagWhitespace": True,
    "sortAttributes": True,
    "sortClassName": True,
    "trimCustomFragments": True,
    "useShortDoctype": True,
}
HTML_HTMIN_FLAGS: list[str] = [
    "--collapse-whitespace",
    "--remove-comments",
    "--remove-optional-tags",
    "--remove-redundant-attributes",
    "--remove-attribute-quotes",
    "--minify-css",
    "--minify-js",
    "--minify-urls",
    "--use-short-doctype",
    "--remove-empty-attributes",
    "--remove-empty-elements",
    "--sort-attributes",
    "--sort-class-name",
    "--remove-script-type-attributes",
    "--remove-style-link-type-attributes",
    "--collapse-inline-tag-whitespace",
    "--remove-tag-whitespace",
    "--decode-entities",
]
_INLINE_TAGS: str = "span|a|strong|em|b|i|code|label"
_RE_DOCTYPE_LOWER = re.compile(r"<!(doctype)(html)", re.IGNORECASE)
_RE_DOCTYPE_UPPER = re.compile(r"<!(DOCTYPE)(HTML)")
_RE_INLINE_WS = re.compile(f"(</(?:{_INLINE_TAGS})>)(<(?:{_INLINE_TAGS}))")


def _fix_doctype(s: str) -> str:
    s = _RE_DOCTYPE_LOWER.sub(r"<!\1 \2", s)
    s = _RE_DOCTYPE_UPPER.sub(r"<!\1 \2", s)
    return s


def _post_process_html(s: str) -> str:
    s = _fix_doctype(s)
    s = _RE_INLINE_WS.sub(r"\1 \2", s)
    return s


def minify_html_hmin(path: Path, timeout: Optional[int] = None) -> Result:
    path = Path(path)
    orig = file_size(path)
    if not path.exists():
        return Result(path, 0, 0, False, "file not found")
    try:
        require_tool(
            "html-minifier-terser",
            "Install it with: npm install -g html-minifier-terser",
        )
    except RuntimeError as e:
        return Result(path, orig, orig, False, str(e))
    tmp_cfg: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(HTML_HMIN_CONFIG, f)
            tmp_cfg = Path(f.name)
        content = path.read_text(encoding="utf-8")
        proc = subprocess.run(
            ["html-minifier-terser", "--config-file", str(tmp_cfg)],
            input=content,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
        if proc.returncode != 0:
            return Result(
                path,
                orig,
                orig,
                False,
                f"minification failed: {proc.stderr.strip()}",
            )
        out = _post_process_html(proc.stdout)
        path.write_text(out, encoding="utf-8")
        return Result(path, orig, file_size(path), True)
    except Exception as exc:
        return Result(path, orig, orig, False, str(exc))
    finally:
        if tmp_cfg is not None and tmp_cfg.exists():
            with contextlib.suppress(OSError):
                tmp_cfg.unlink()


def minify_html_htmin(path: Path, timeout: int = 30) -> Result:
    path = Path(path)
    orig = file_size(path)
    cmd = [
        "html-minifier-terser",
        *HTML_HTMIN_FLAGS,
        "--output",
        str(path),
        str(path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            err = proc.stderr.strip() or f"exit code {proc.returncode}"
            return Result(path, orig, orig, False, err)
        return Result(path, orig, file_size(path), True)
    except FileNotFoundError:
        return Result(path, orig, orig, False, "html-minifier-terser not found")
    except subprocess.TimeoutExpired:
        return Result(path, orig, orig, False, f"timeout ({timeout}s exceeded)")
    except Exception as exc:
        return Result(path, orig, orig, False, str(exc))


def minify_css_csso(path: Path) -> Result:
    path = Path(path)
    orig = file_size(path)
    if not path.exists():
        return Result(path, 0, 0, False, "file not found")
    try:
        proc = subprocess.run(
            ["csso", "-i", str(path), "-o", str(path)],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return Result(path, orig, orig, False, proc.stderr.strip() or "csso error")
        new = file_size(path)
        return Result(path, orig, new, True, no_change=(new == orig))
    except FileNotFoundError:
        return Result(path, orig, orig, False, "csso not found")
    except Exception as exc:
        return Result(path, orig, orig, False, str(exc))


def minify_css_rcssmin(path: Path) -> Result:
    path = Path(path)
    orig = file_size(path)
    if _rcssmin is None:
        return Result(path, orig, orig, False, "rcssmin not installed (pip install rcssmin)")
    try:
        content = path.read_text(encoding="utf-8")
        out = _rcssmin(content)
        if out == content:
            return Result(path, orig, orig, True, no_change=True)
        path.write_text(out, encoding="utf-8")
        return Result(path, orig, file_size(path), True)
    except Exception as exc:
        return Result(path, orig, orig, False, str(exc))


def minify_js_rjsmin(path: Path) -> Result:
    path = Path(path)
    orig = file_size(path)
    if _rjsmin is None:
        return Result(path, orig, orig, False, "rjsmin not installed (pip install rjsmin)")
    try:
        content = path.read_text(encoding="utf-8")
        out = _rjsmin(content)
        if out == content:
            return Result(path, orig, orig, True, no_change=True)
        path.write_text(out, encoding="utf-8")
        return Result(path, orig, file_size(path), True)
    except Exception as exc:
        return Result(path, orig, orig, False, str(exc))


def minify_json(path: Path, dry: bool = False, spaced: bool = False) -> Result:
    path = Path(path)
    orig = file_size(path)
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as exc:
        return Result(path, orig, orig, False, f"cannot read: {exc}")
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        return Result(path, orig, orig, False, f"invalid JSON: {exc}")
    if spaced:
        out = json.dumps(data, ensure_ascii=False, indent=None)
    else:
        out = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    if content.strip() == out.strip():
        return Result(path, orig, orig, True, no_change=True)
    if dry:
        return Result(path, orig, len(out.encode("utf-8")), True, dry_run=True)
    try:
        path.write_text(out, encoding="utf-8")
        return Result(path, orig, file_size(path), True)
    except Exception as exc:
        return Result(path, orig, orig, False, f"cannot write: {exc}")


def minify_svg_svgcleaner(path: Path, skip_parts: Sequence[str] = ("lazy",)) -> Result:
    path = Path(path)
    if any(s in path.parts for s in skip_parts) or not path.exists():
        return Result(path, 0, 0, True, no_change=True)
    orig = file_size(path)
    tmp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as f:
            tmp_path = Path(f.name)
        proc = subprocess.run(
            ["svgcleaner", str(path), str(tmp_path)],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return Result(path, orig, orig, False, proc.stderr.strip() or "svgcleaner failed")
        new = file_size(tmp_path)
        if not new:
            return Result(path, orig, orig, False, "svgcleaner produced empty output")
        tmp_path.replace(path)
        return Result(path, orig, new, True, no_change=(new == orig))
    except FileNotFoundError:
        return Result(path, orig, orig, False, "svgcleaner not found")
    except Exception as exc:
        return Result(path, orig, orig, False, str(exc))
    finally:
        if tmp_path is not None and tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()


DEFAULT_TRUNCATE_TAGS: tuple[str, ...] = (
    "</svg>",
    "</html>",
    "</body>",
    "</script>",
    "</div>",
)


def truncate_at_last_tag(
    path: Path,
    tags: Sequence[str] = DEFAULT_TRUNCATE_TAGS,
) -> Result:
    path = Path(path)
    if not path.exists():
        return Result(path, 0, 0, False, "file not found")
    orig = file_size(path)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    cut_at: int = -1
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        for tag in tags:
            m = line.rfind(tag)
            if m != -1:
                cut_at = sum(len(lines[j]) for j in range(i)) + m + len(tag)
                break
        if cut_at != -1:
            break
    if cut_at == -1:
        return Result(path, orig, orig, True, no_change=True)
    new_content = "".join(lines)[:cut_at]
    path.write_text(new_content, encoding="utf-8")
    return Result(path, orig, file_size(path), True)


_RE_HTML_TAG_GAP = re.compile(r">\s+<")
_RE_HTML_MULTI_WS = re.compile(r"\s{2,}")


def _collapse_html_ws(s: str) -> str:
    s = _RE_HTML_TAG_GAP.sub("><", s)
    s = _RE_HTML_MULTI_WS.sub(" ", s)
    return s.strip()


def minify_mixed(path: Path) -> tuple[Path, str]:
    path = Path(path)
    ext = path.suffix.lower()
    try:
        content = path.read_text(encoding="utf-8")
        if ext == ".css":
            if _rcssmin is None:
                return path, f"ERR ({path}): rcssmin not installed"
            out = _rcssmin(content)
        elif ext == ".json":
            out = json.dumps(json.loads(content), separators=(",", ":"))
        elif ext in (".html", ".htm"):
            out = _collapse_html_ws(content)
        else:
            return path, f"SKIP → {path}"
        path.write_text(out, encoding="utf-8")
        return path, f"OK → {path}"
    except Exception as exc:
        return path, f"ERR ({path}): {exc}"


_MESON_DB: dict[str, Any] = {}


def _v_keys(prefix: str, actual: dict, expected: dict) -> dict:
    assert set(actual.keys()).issuperset(expected.keys()), (
        f"{prefix}:DIFF:{set(actual.keys()).difference(expected.keys())}"
    )
    kept: dict[str, Any] = {}
    for k, t in expected.items():
        v = actual.pop(k)
        assert isinstance(v, t), f"{prefix}:type({k}:{v})!={t}"
        kept[k] = v
    return kept


def _v_base(prefix: str, name: str, d: dict) -> None:
    schema = {
        "name": str,
        "description": str,
        "since": (str, _NONE_TYPE),
        "deprecated": (str, _NONE_TYPE),
        "notes": list,
        "warnings": list,
    }
    r = _v_keys(f"{prefix}.{name}", d, schema)
    assert r["name"], f"{prefix}.{name}"
    assert r["description"], f"{prefix}.{name}"
    assert r["name"] == name, f"{prefix}.{name}"
    assert all(isinstance(x, str) and x for x in r["notes"]), f"{prefix}.{name}"
    assert all(isinstance(x, str) and x for x in r["warnings"]), f"{prefix}.{name}"


def _v_obj(prefix: str, d: dict) -> None:
    schema = {"obj": str, "holds": list}
    r = _v_keys(prefix, d, schema)
    assert not d, f"{prefix} has extra keys: {d.keys()}"
    assert r["obj"] in _MESON_DB["objects"], prefix
    for i in r["holds"]:
        _v_obj(prefix, i)


def _v_arg(prefix: str, name: str, d: dict) -> None:
    _v_base(prefix, name, d)
    schema = {
        "type": list,
        "type_str": str,
        "required": bool,
        "default": (str, _NONE_TYPE),
        "min_varargs": (int, _NONE_TYPE),
        "max_varargs": (int, _NONE_TYPE),
    }
    r = _v_keys(f"{prefix}.{name}", d, schema)
    assert not d, f"{prefix}.{name} has extra keys: {d.keys()}"
    assert r["type"], f"{prefix}.{name}"
    assert r["type_str"], f"{prefix}.{name}"
    for i in r["type"]:
        _v_obj(f"{prefix}.{name}", i)
    if r["min_varargs"] is not None:
        assert r["min_varargs"] > 0, f"{prefix}.{name}"
    if r["max_varargs"] is not None:
        assert r["max_varargs"] > 0, f"{prefix}.{name}"


def _v_func(prefix: str, name: str, d: dict) -> None:
    _v_base(prefix, name, d)
    schema = {
        "returns": list,
        "returns_str": str,
        "example": (str, _NONE_TYPE),
        "posargs": dict,
        "optargs": dict,
        "kwargs": dict,
        "varargs": (dict, _NONE_TYPE),
        "arg_flattening": bool,
    }
    r = _v_keys(f"{prefix}.{name}", d, schema)
    assert not d, f"{prefix}.{name} has extra keys: {d.keys()}"
    assert r["returns"], f"{prefix}.{name}"
    assert r["returns_str"], f"{prefix}.{name}"
    for i in r["returns"]:
        _v_obj(f"{prefix}.{name}", i)
    for k, v in r["posargs"].items():
        _v_arg(f"{prefix}.{name}", k, v)
    for k, v in r["optargs"].items():
        _v_arg(f"{prefix}.{name}", k, v)
    for k, v in r["kwargs"].items():
        _v_arg(f"{prefix}.{name}", k, v)
    if r["varargs"]:
        _v_arg(f"{prefix}.{name}", r["varargs"]["name"], r["varargs"])


def _v_object(prefix: str, name: str, d: dict) -> None:
    _v_base(prefix, name, d)
    schema = {
        "example": (str, _NONE_TYPE),
        "object_type": str,
        "methods": dict,
        "is_container": bool,
        "extends": (str, _NONE_TYPE),
        "returned_by": list,
        "extended_by": list,
        "defined_by_module": (str, _NONE_TYPE),
    }
    r = _v_keys(f"{prefix}.{name}", d, schema)
    assert not d, f"{prefix}.{name} has extra keys: {d.keys()}"
    for mk, mv in r["methods"].items():
        _v_func(f"{prefix}.{name}", mk, mv)
    if r["extends"] is not None:
        assert r["extends"] in _MESON_DB["objects"], f"{prefix}.{name}"
    assert all(isinstance(x, str) for x in r["returned_by"]), f"{prefix}.{name}"
    assert all(isinstance(x, str) for x in r["extended_by"]), f"{prefix}.{name}"
    assert all(x in _MESON_DB["objects"] for x in r["extended_by"]), f"{prefix}.{name}"
    if r["defined_by_module"] is not None:
        assert r["defined_by_module"] in _MESON_DB["objects"], f"{prefix}.{name}"
        assert r["object_type"] == "RETURNED", f"{prefix}.{name}"
        assert _MESON_DB["objects"][r["defined_by_module"]]["object_type"] == "MODULE", f"{prefix}.{name}"
        assert name in _MESON_DB["objects_by_type"]["modules"][r["defined_by_module"]], f"{prefix}.{name}"
        return
    assert r["object_type"] in {
        "ELEMENTARY",
        "BUILTIN",
        "MODULE",
        "RETURNED",
    }, f"{prefix}.{name}"
    if r["object_type"] == "ELEMENTARY":
        assert name in _MESON_DB["objects_by_type"]["elementary"], f"{prefix}.{name}"
    if r["object_type"] == "BUILTIN":
        assert name in _MESON_DB["objects_by_type"]["builtins"], f"{prefix}.{name}"
    if r["object_type"] == "RETURNED":
        assert name in _MESON_DB["objects_by_type"]["returned"], f"{prefix}.{name}"
    if r["object_type"] == "MODULE":
        assert name in _MESON_DB["objects_by_type"]["modules"], f"{prefix}.{name}"


def validate_meson_json(doc_file: Path) -> int:
    global _MESON_DB
    _MESON_DB = json.loads(Path(doc_file).read_text(encoding="utf-8"))
    assert isinstance(_MESON_DB, dict)
    root_schema = {
        "version_major": int,
        "version_minor": int,
        "meson_version": str,
        "functions": dict,
        "objects": dict,
        "objects_by_type": dict,
    }
    r = _v_keys("root", _MESON_DB, root_schema)
    assert not _MESON_DB, f"root has extra keys: {_MESON_DB.keys()}"
    obt = r["objects_by_type"]
    _v_keys(
        "root.objects_by_type",
        obt,
        {"elementary": list, "builtins": list, "returned": list, "modules": dict},
    )
    assert not obt, f"root.objects_by_type has extra keys: {obt.keys()}"
    db = _MESON_DB
    for kind in ("elementary", "builtins", "returned"):
        assert all(isinstance(x, str) for x in db["objects_by_type"][kind])
        assert all(x in db["objects"] for x in db["objects_by_type"][kind])
    assert all(isinstance(x, str) for x in db["objects_by_type"]["modules"])
    assert all(x in db["objects"] for x in db["objects_by_type"]["modules"])
    assert all(db["objects"][x]["object_type"] == "ELEMENTARY" for x in db["objects_by_type"]["elementary"])
    assert all(db["objects"][x]["object_type"] == "BUILTIN" for x in db["objects_by_type"]["builtins"])
    assert all(db["objects"][x]["object_type"] == "RETURNED" for x in db["objects_by_type"]["returned"])
    assert all(db["objects"][x]["object_type"] == "MODULE" for x in db["objects_by_type"]["modules"])
    assert all(all(isinstance(x, str) for x in v) for v in db["objects_by_type"]["modules"].values())
    assert all(all(x in db["objects"] for x in v) for v in db["objects_by_type"]["modules"].values())
    assert all(
        all(db["objects"][x]["defined_by_module"] == k for x in v) for k, v in db["objects_by_type"]["modules"].items()
    )
    for name, body in r["functions"].items():
        _v_func("root", name, body)
    for name, body in r["objects"].items():
        _v_object("root", name, body)
    return 0


def _collect_files(paths: Sequence[str], extensions: Sequence[str]) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        files.extend(find_files(Path(p).resolve(), extensions))
    return sorted(set(files))


def _ext_list(s: str) -> list[str]:
    return [e.strip() for e in s.split(",") if e.strip()]


def cmd_html(args: argparse.Namespace) -> int:
    exts = _ext_list(args.ext)
    files = _collect_files(args.paths, exts)
    if not files:
        print("No HTML files found.")
        return 1
    if args.backend == "hmin":
        fn: Callable[[Path], Result] = minify_html_hmin
    else:
        fn = partial(minify_html_htmin, timeout=args.timeout)
    results = parallel_map(fn, files, workers=MAX_WORKERS)
    for r in results:
        print_file_result(r)
    print_summary(results, "HTML Summary")
    return 0 if all(r.success or r.no_change for r in results) else 1


def cmd_css(args: argparse.Namespace) -> int:
    exts = _ext_list(args.ext)
    files = _collect_files(args.paths, exts)
    if not files:
        print("No CSS files found.")
        return 1
    fn: Callable[[Path], Result]
    if args.backend == "csso":
        fn = minify_css_csso
    else:
        fn = minify_css_rcssmin
    results = parallel_map(fn, files, workers=MAX_WORKERS)
    for r in results:
        print_file_result(r)
    print_summary(results, "CSS Summary")
    return 0 if all(r.success or r.no_change for r in results) else 1


def cmd_js(args: argparse.Namespace) -> int:
    exts = _ext_list(args.ext)
    files = _collect_files(args.paths, exts)
    if not files:
        print("No JS files found.")
        return 1
    results = parallel_map(minify_js_rjsmin, files, workers=MAX_WORKERS)
    for r in results:
        print_file_result(r)
    print_summary(results, "JS Summary")
    return 0 if all(r.success or r.no_change for r in results) else 1


def cmd_json(args: argparse.Namespace) -> int:
    exts = _ext_list(args.ext)
    files = _collect_files(args.paths, exts)
    if not files:
        print("No JSON files found.")
        return 1
    fn = partial(minify_json, dry=args.dry, spaced=args.spaced)
    results = parallel_map(fn, files, workers=MAX_WORKERS)
    for r in results:
        print_file_result(r)
    print_summary(results, "JSON Summary")
    return 0 if all(r.success or r.no_change for r in results) else 1


def cmd_svg(args: argparse.Namespace) -> int:
    exts = _ext_list(args.ext)
    files = _collect_files(args.paths, exts)
    if not files:
        print("No SVG files found.")
        return 1
    fn = partial(minify_svg_svgcleaner, skip_parts=tuple(args.skip_part))
    results = parallel_map(fn, files, workers=MAX_WORKERS)
    for r in results:
        print_file_result(r)
    print_summary(results, "SVG Summary")
    return 0 if all(r.success or r.no_change for r in results) else 1


def cmd_truncate(args: argparse.Namespace) -> int:
    exts = _ext_list(args.ext)
    files = _collect_files(args.paths, exts)
    if not files:
        print("No matching files found.")
        return 1
    tags = tuple(args.tag) if args.tag else DEFAULT_TRUNCATE_TAGS
    fn = partial(truncate_at_last_tag, tags=tags)
    results = parallel_map(fn, files, workers=MAX_WORKERS)
    for r in results:
        print_file_result(r)
    print_summary(results, "Truncate Summary")
    return 0 if all(r.success or r.no_change for r in results) else 1


def cmd_mixed(args: argparse.Namespace) -> int:
    exts = _ext_list(args.ext)
    files = _collect_files(args.paths, exts)
    if not files:
        print("No supported files found.")
        return 1
    results = parallel_map(minify_mixed, files, workers=MAX_WORKERS)
    for _, msg in results:
        print(msg)
    ok = sum(1 for _, m in results if m.startswith(("OK", "SKIP")))
    print(f"\n{ok}/{len(results)} file(s) processed successfully.")
    return 0 if ok == len(results) else 1


def cmd_validate_meson_json(args: argparse.Namespace) -> int:
    try:
        validate_meson_json(args.doc_file)
    except AssertionError as exc:
        print(f"Validation failed: {exc}", file=sys.stderr)
        return 1
    print(f"OK: {args.doc_file} is valid.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="webassets.py",
        description="Unified web-asset minifier + Meson JSON-doc validator.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")
    ph = sub.add_parser("html", help="Minify HTML files.")
    ph.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="Files/directories to process (default: .).",
    )
    ph.add_argument(
        "-b",
        "--backend",
        choices=("hmin", "htmin"),
        default="hmin",
        help="hmin = JSON-config + stdin/stdout (default); htmin = CLI-flags + in-place write.",
    )
    ph.add_argument(
        "-t",
        "--timeout",
        type=int,
        default=30,
        help="Subprocess timeout in seconds (htmin backend only; default: 30).",
    )
    ph.add_argument(
        "--ext",
        default=".html,.htm",
        help="Comma-separated extensions (default: .html,.htm).",
    )
    ph.set_defaults(func=cmd_html)
    pc = sub.add_parser("css", help="Minify CSS files.")
    pc.add_argument("paths", nargs="*", default=["."])
    pc.add_argument(
        "-b",
        "--backend",
        choices=("rcssmin", "csso"),
        default="rcssmin",
        help="rcssmin = pure Python (default); csso = external CLI.",
    )
    pc.add_argument("--ext", default=".css", help="Comma-separated extensions (default: .css).")
    pc.set_defaults(func=cmd_css)
    pj = sub.add_parser("js", help="Minify JS files (rjsmin).")
    pj.add_argument("paths", nargs="*", default=["."])
    pj.add_argument("--ext", default=".js", help="Comma-separated extensions (default: .js).")
    pj.set_defaults(func=cmd_js)
    pq = sub.add_parser("json", help="Minify JSON files.")
    pq.add_argument("paths", nargs="*", default=["."])
    pq.add_argument("--dry", action="store_true", help="Report what would change without writing.")
    pq.add_argument(
        "--spaced",
        action="store_true",
        help="Use default separators (jm2.py style) instead of compact separators (mjb.py style).",
    )
    pq.add_argument("--ext", default=".json")
    pq.set_defaults(func=cmd_json)
    ps = sub.add_parser("svg", help="Optimise SVG files (svgcleaner).")
    ps.add_argument("paths", nargs="*", default=["."])
    ps.add_argument(
        "--skip-part",
        action="append",
        default=["lazy"],
        help="Path part to skip; repeatable (default: lazy).",
    )
    ps.add_argument("--ext", default=".svg")
    ps.set_defaults(func=cmd_svg)
    pt = sub.add_parser("truncate", help="Truncate files at their last closing tag (fixsvg.py).")
    pt.add_argument("paths", nargs="*", default=["."])
    pt.add_argument(
        "--ext",
        default=".html,.htm,.svg,.xml",
        help="Extensions to process (default: .html,.htm,.svg,.xml).",
    )
    pt.add_argument(
        "--tag",
        action="append",
        default=None,
        help="Closing tag to search for; repeatable. Defaults to "
        "</svg> </html> </body> </script> </div> in that order.",
    )
    pt.set_defaults(func=cmd_truncate)
    pm = sub.add_parser("mixed", help="Dispatch CSS/JSON/HTML by extension (minjch.py).")
    pm.add_argument("paths", nargs="*", default=["."])
    pm.add_argument("--ext", default=".css,.json,.html,.htm")
    pm.set_defaults(func=cmd_mixed)
    pv = sub.add_parser("validate-meson-json", help="Validate a Meson JSON-docs file.")
    pv.add_argument("doc_file", type=Path, help="Path to the JSON docs file to validate.")
    pv.set_defaults(func=cmd_validate_meson_json)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
