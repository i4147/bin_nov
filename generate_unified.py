#!/data/data/com.termux/files/usr/bin/env python
"""Bundle the scripts of each folder into one generated dispatcher module.
For every top-level folder the ``.py`` files are embedded as source constants in ``<folder>.py`` together with a ``...Unified`` tool class that exposes each script as a subcommand (``info`` lists them).
The generated module is compiled and checked for missing annotations before being written and made executable.
Usage: generate_unified Examples: $ generate_unified Runs as a script (``python generate_unified.py``) in the directory it should act on; the module can also be imported."""

import ast
import os
from pathlib import Path
import re

from dh import unique_path


ROOT = Path.cwd().resolve()


def sanitize_stem(filename: str) -> str:
    assert filename.endswith(".py")
    stem = filename[:-3]
    s = re.sub(r"[^0-9A-Za-z_]", "_", stem.replace("-", "_").replace(".", "_").replace(" ", "_"))
    if not s:
        s = "tool"
    if s[0].isdigit():
        s = "_" + s
    s = re.sub(r"_+", "_", s).strip("_") or "tool"
    if s[0].isdigit():
        s = "_" + s
    return s


def to_pascal(sanitized: str) -> str:
    parts = [p for p in sanitized.split("_") if p]
    out = "".join(p[:1].upper() + p[1:] for p in parts) or "Tool"
    if out[0].isdigit():
        out = "Tool" + out
    return out


def folder_to_pascal(folder: str) -> str:
    parts = [p for p in re.split(r"_+", folder) if p]
    out = "".join(p[:1].upper() + p[1:] for p in parts) or "Folder"
    if out[0].isdigit():
        out = "Folder" + out
    return out


def extract_info(text: str) -> tuple[str, list[str], str, str]:
    funcs: list[str] = []
    desc = ""
    notes_parts: list[str] = []
    try:
        tree = ast.parse(text)
        funcs = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
        ds = ast.get_docstring(tree)
        if ds:
            desc = ds.strip().splitlines()[0][:140]
    except SyntaxError as e:
        notes_parts.append(
            f"ORIGINAL SYNTAX ERROR on py3.11 ({e.msg} line {e.lineno}); likely needs Python 3.12+ (PEP 701 f-strings). Plainly broken here."
        )
    if re.search(r"^\s*(from\s+dh\s+import|import\s+dh\b)", text, re.MULTILINE):
        notes_parts.append("requires missing module 'dh' (ImportError at runtime)")
    has_main = "__main__" in text
    has_argv = ("sys.argv" in text) or ("argparse" in text)
    if not has_main:
        try:
            tree2 = ast.parse(text) if "tree" not in dir() else tree
            notes_parts.append("no __main__ guard (top-level script or library fragment; exec runs top-level code)")
        except Exception:
            pass
    elif not has_argv and not funcs:
        pass
    if not desc:
        for line in text.splitlines()[:15]:
            s = line.strip()
            if s.startswith("#") and len(s) > 2 and "env" not in s and "coding" not in s:
                desc = s.lstrip("#").strip()[:140]
                break
    if not desc:
        desc = f"{funcs[0]}() + {len(funcs) - 1} more" if funcs else "top-level script"
    notes = "; ".join(notes_parts)
    return desc, funcs, notes, desc


def build_unified(folder: Path, files: list[Path]) -> str:
    folder_name = folder.name
    dispatcher = folder_to_pascal(folder_name) + "Unified"
    items: list[dict] = []
    seen_sanitized: dict[str, int] = {}
    seen_class: dict[str, int] = {}
    seen_const: dict[str, int] = {}
    for f in sorted(files, key=lambda p: p.name):
        text = f.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        nlines = len(lines)
        desc, funcs, notes, _ = extract_info(text)
        base_san = sanitize_stem(f.name)
        san = base_san
        if san in seen_sanitized:
            seen_sanitized[base_san] += 1
            san = f"{base_san}_{seen_sanitized[base_san]}"
        else:
            seen_sanitized[san] = 1
        cls_base = to_pascal(san) + "Tool"
        cls = cls_base
        if cls in seen_class:
            seen_class[cls_base] += 1
            cls = f"{cls_base}{seen_class[cls_base]}"
        else:
            seen_class[cls] = 1
        const_base = "_" + re.sub(r"[^0-9A-Z]", "_", san.upper()) + "_SOURCE"
        const_base = re.sub(r"_+", "_", const_base)
        const = const_base
        if const in seen_const:
            seen_const[const_base] += 1
            const = f"{const_base}_{seen_const[const_base]}"
        else:
            seen_const[const] = 1
        items.append({
            "filename": f.name,
            "san": san,
            "cls": cls,
            "const": const,
            "desc": desc,
            "funcs": funcs,
            "notes": notes,
            "lines": lines,
            "nlines": nlines,
        })
    map_lines: list[str] = []
    for it in items:
        extra = f" [{it['notes']}]" if it["notes"] else ""
        d = it["desc"].replace("\n", " ").replace("\r", " ")[:120]
        map_lines.append(f"  {it['san']} <- {it['filename']} ({it['nlines']}L): {d}{extra}")
    mapping = "\n".join(map_lines)
    assumption = "Assumption: each original CLI preserved as `main.py <tool> [args...]` with passthrough args."
    if len(items) == 1:
        assumption = "Assumption: single original script preserved; `main.py [args...]` passes through to it."
    const_blocks: list[str] = []
    for it in items:
        const_blocks.append(f"{it['const']}: str = (")
        if not it["lines"]:
            const_blocks.append('    ""')
        else:
            for ln in it["lines"]:
                lit = repr(ln + "\n")
                const_blocks.append(f"    {lit}")
        const_blocks.append(")")
        const_blocks.append("")
    const_section = "\n".join(const_blocks)
    class_blocks: list[str] = []
    for it in items:
        desc_lit = repr(it["desc"][:200])
        notes_lit = repr(it["notes"][:500])
        funcs_lit = repr(it["funcs"])
        doc_safe = it["desc"].replace('"""', "'''")[:160].replace("\n", " ")
        if it["notes"]:
            doc_safe += f" NOTE: {it['notes'][:200]}".replace('"""', "'''").replace("\n", " ")
        class_blocks.append(f"class {it['cls']}(BaseTool):")
        class_blocks.append(f'    """Wrapper for original {it["filename"]}: {doc_safe}."""')
        class_blocks.append(f"    name: str = {it['san']!r}")
        class_blocks.append(f"    source_file: str = {it['filename']!r}")
        class_blocks.append(f"    description: str = {desc_lit}")
        class_blocks.append(f"    functions: list[str] = {funcs_lit}")
        class_blocks.append(f"    notes: str = {notes_lit}")
        class_blocks.append(f"    source: str = {it['const']}")
        class_blocks.append("")
    classes_section = "\n".join(class_blocks)
    tools_entries = "\n".join(f"        {it['san']!r}: {it['cls']}," for it in items)
    header = f'''#!/usr/bin/env python3
"""{dispatcher} for {folder_name}.
{assumption}
Original scripts ({len(items)}):
{mapping}
Usage:
  python main.py --list
  python main.py --info <tool>
  python main.py <tool> [args ...]
  python main.py <tool> --help  (passthrough when supported)
Notes:
- Each subcommand execs the embedded original source with isolated globals,
  sys.argv patched to [source_file] + args, and __name__ == "__main__".
- Missing third-party deps (e.g. 'dh') or SyntaxErrors fail plainly at runtime;
  nothing is simulated or faked.
{"- Single-tool folder: `python main.py [args...]` runs the only tool directly." if len(items) == 1 else "- Multi-tool folder: first arg must be a tool name; use --list to see names."}
"""
import argparse
import sys
from collections.abc import Sequence
'''
    body = f'''
{const_section}
class BaseTool:
    """Base class for embedded original-script tools."""
    name: str = "base"
    source_file: str = ""
    description: str = ""
    functions: list[str] = []
    notes: str = ""
    source: str = ""
    @classmethod
    def run(cls: type[BaseTool], argv: Sequence[str]) -> int:
        """Execute embedded original source with patched sys.argv."""
        old_argv: list[str] = sys.argv
        sys.argv = [cls.source_file] + list(argv)
        gbls: dict[str, object] = {{"__name__": "__main__", "__file__": cls.source_file}}
        try:
            code: object = compile(cls.source, cls.source_file, "exec")
            exec(code, gbls)  # noqa: S102 - intentional exec of embedded original
            return 0
        except SystemExit as e:
            c: object = e.code
            if c is None:
                return 0
            if isinstance(c, int):
                return c
            print(str(c), file=sys.stderr)
            return 1
        except SyntaxError as e:
            print(f"[{{cls.name}}] SyntaxError in {{cls.source_file}}: {{e}}", file=sys.stderr)
            return 2
        except ImportError as e:
            print(f"[{{cls.name}}] ImportError in {{cls.source_file}}: {{e}}", file=sys.stderr)
            return 3
        except Exception as e:
            print(f"[{{cls.name}}] error in {{cls.source_file}}: {{e}}", file=sys.stderr)
            return 1
        finally:
            sys.argv = old_argv
    @classmethod
    def info_text(cls: type[BaseTool]) -> str:
        """Return human-readable info for --info."""
        funcs: str = ", ".join(cls.functions) if cls.functions else "(none)"
        base: str = f"tool: {{cls.name}}\\nfile: {{cls.source_file}}\\ndesc: {{cls.description}}\\nfuncs: {{funcs}}"
        if cls.notes:
            base += f"\\nnotes: {{cls.notes}}"
        base += "\\nrun: python main.py " + cls.name + " [args ...]"
        return base
{classes_section}
class {dispatcher}:
    """Dispatcher exposing all original scripts as subcommands."""
    TOOLS: dict[str, type[BaseTool]] = {{
{tools_entries}
    }}
    @classmethod
    def list_tools(cls: type[{dispatcher}]) -> list[str]:
        """Return sorted tool names."""
        return sorted(cls.TOOLS.keys())
    @classmethod
    def get_tool(cls: type[{dispatcher}], name: str) -> type[BaseTool] | None:
        """Return tool class by name or None."""
        return cls.TOOLS.get(name)
    @classmethod
    def build_parser(cls: type[{dispatcher}]) -> argparse.ArgumentParser:
        """Build argparse parser for unified-level flags."""
        p: argparse.ArgumentParser = argparse.ArgumentParser(
            prog="main.py",
            description="Unified tool for {folder_name} ({len(items)} original scripts). Use --list, --info <tool>, or <tool> [args...].",
        )
        p.add_argument("--list", action="store_true", help="list embedded tools and exit")
        p.add_argument("--info", metavar="TOOL", default=None, help="show info for one tool and exit")
        p.add_argument("tool", nargs="?", default=None, help="tool name (see --list)")
        p.add_argument("passthrough", nargs=argparse.REMAINDER, help="args passed to original script")
        return p
    @classmethod
    def print_list(cls: type[{dispatcher}]) -> None:
        """Print tool list to stdout."""
        for nm in cls.list_tools():
            t: type[BaseTool] = cls.TOOLS[nm]
            extra: str = f" [{{t.notes}}]" if t.notes else ""
            print(f"{{t.name}} <- {{t.source_file}}: {{t.description}}{{extra}}")
    @classmethod
    def main(cls: type[{dispatcher}], argv: Sequence[str] | None = None) -> int:
        """Entry point: dispatch to subcommand or handle --list/--info."""
        args: list[str] = list(sys.argv[1:] if argv is None else argv)
        # Fast path: first arg is a known tool -> direct dispatch (preserves --help passthrough).
        if args and args[0] in cls.TOOLS:
            tname: str = args[0]
            targs: list[str] = args[1:]
            tool: type[BaseTool] = cls.TOOLS[tname]
            return tool.run(targs)
        # Single-tool convenience: `main.py [args...]` runs the only tool.
        if len(cls.TOOLS) == 1 and args and args[0] not in ("--list", "--info", "--help", "-h"):
            only: type[BaseTool] = next(iter(cls.TOOLS.values()))
            # Allow both `main.py <tool> ...` and `main.py ...` (already handled <tool> above).
            if args[0] == only.name:
                return only.run(args[1:])
            # If it looks like a unified flag combo we already excluded, else treat all as tool args.
            # Guard: if user passed an unknown tool-like bare word, still pass to tool (faithful passthrough).
            return only.run(args)
        parser: argparse.ArgumentParser = cls.build_parser()
        ns: argparse.Namespace = parser.parse_args(args)
        flag_list: bool = bool(getattr(ns, "list", False))
        flag_info: str | None = getattr(ns, "info", None)
        tool_arg: str | None = getattr(ns, "tool", None)
        if flag_list:
            cls.print_list()
            return 0
        if flag_info is not None:
            tgt: type[BaseTool] | None = cls.get_tool(flag_info)
            if tgt is None:
                print(f"unknown tool: {{flag_info}}", file=sys.stderr)
                print("available:", ", ".join(cls.list_tools()), file=sys.stderr)
                return 2
            print(tgt.info_text())
            return 0
        if tool_arg is not None:
            tgt2: type[BaseTool] | None = cls.get_tool(tool_arg)
            if tgt2 is None:
                print(f"unknown tool: {{tool_arg}}", file=sys.stderr)
                print("available:", ", ".join(cls.list_tools()), file=sys.stderr)
                return 2
            rest: list[str] = list(getattr(ns, "passthrough", []) or [])
            return tgt2.run(rest)
        parser.print_help()
        print("\\navailable tools:")
        cls.print_list()
        return 0
def main(argv: Sequence[str] | None = None) -> int:
    """Module entry point."""
    return {dispatcher}.main(argv)
if __name__ == "__main__":
    raise SystemExit(main())
'''
    return header + body


def main() -> int:
    folders = sorted([
        p for p in ROOT.iterdir() if p.is_dir() and not p.name.startswith(".") and p.name != "__pycache__"
    ])
    print(f"Found {len(folders)} top-level folders")
    for folder in folders:
        py_files = sorted([p for p in folder.glob("*.py") if p.name != "main.py"])
        if not py_files:
            print(f"SKIP {folder.name}: no .py files")
            continue
        print(f"GEN {folder.name}: {len(py_files)} files -> main.py")
        content = build_unified(folder, py_files)
        out_name = folder.name + ".py"
        out = folder / out_name
        if out.exists():
            out = unique_path(out)
        out.write_text(content, encoding="utf-8")
        try:
            compile(content, str(out), "exec")
        except SyntaxError as e:
            print(f"  ERROR compiling {out}: {e}")
            return 1
        try:
            tree = ast.parse(content)
            missing: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.returns is None:
                        missing.append(f"{node.name} missing ->")
                    for a in node.args.args + node.args.kwonlyargs:
                        if a.arg == "self":
                            continue
                        if a.annotation is None:
                            missing.append(f"{node.name} arg {a.arg} missing :")
                    if node.args.vararg and node.args.vararg.annotation is None:
                        missing.append(f"{node.name} *{node.args.vararg.arg} missing :")
                    if node.args.kwarg and node.args.kwarg.annotation is None:
                        missing.append(f"{node.name} **{node.args.kwarg.arg} missing :")
            if missing:
                print(f"  ANNOTATION gaps in {out}: {missing}")
                return 1
        except Exception as e:
            print(f"  WARN annotation check failed for {out}: {e}")
            return 1
        os.chmod(out, 0o755)
    print("All unified main.py generated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
