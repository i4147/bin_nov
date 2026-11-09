#!/data/data/com.termux/files/usr/bin/env python
"""Annotate a Python file from its ``.pyi`` stub with LibCST.
Generates or reuses the stub next to the source (``same.pyi`` unless ``--stub-file`` is given), sanitises it through typeshed, applies the signatures to the source and reports with ``--diff``, ``--dry-run`` and ``--quiet``; ``--future-annotations`` adds the future import.
Usage: annotate_types FILE.py [-s STUB.pyi] [-d] [-n] Positional arguments: file Path to the .py source file to annotate (input=sys.argv[1]).
Options: -s, --stub-file Path to custom .pyi stub file (defaults to <file>.pyi beside the target file).
--no-overwrite Do not overwrite existing type annotations in the source code.
--future-annotations Enable '' support.
-d, --diff Show unified diff of modifications.
-n, --dry-run Simulate the transformation without modifying the file.
-q, --quiet Suppress output messages except errors.
Examples: $ annotate_types module.py --diff Command-line script: run ``python annotate_types.py`` or the ``annotate_types`` command.
The module is importable as well."""

import argparse
import ast
import difflib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import libcst as cst
from libcst.codemod import CodemodContext
from libcst.codemod.visitors import ApplyTypeAnnotationsVisitor


class TypeshedSanitizer(cst.CSTTransformer):
    def leave_ImportFrom(self, original_node: cst.ImportFrom, updated_node: cst.ImportFrom) -> cst.CSTNode:
        if original_node.module and cst.helpers.get_full_name_for_node(original_node.module) == "_typeshed":
            return cst.ImportFrom(
                module=cst.Name("typing"),
                names=[cst.ImportAlias(name=cst.Name("Any"))],
            )
        return updated_node

    def leave_Import(self, original_node: cst.Import, updated_node: cst.Import) -> cst.CSTNode:
        names = []
        for alias in original_node.names:
            if cst.helpers.get_full_name_for_node(alias.name) == "_typeshed":
                names.append(alias.with_changes(name=cst.Name("typing")))
            else:
                names.append(alias)
        return updated_node.with_changes(names=names)

    def leave_Attribute(self, original_node: cst.Attribute, updated_node: cst.Attribute) -> cst.CSTNode:
        if (
            isinstance(original_node.value, cst.Name) and original_node.value.value == "_typeshed"
        ) and original_node.attr.value == "Incomplete":
            return cst.Attribute(value=cst.Name("typing"), attr=cst.Name("Any"))
        return updated_node

    def leave_Name(self, original_node: cst.Name, updated_node: cst.Name) -> cst.CSTNode:
        if original_node.value == "Incomplete":
            return cst.Name("Any")
        return updated_node


def sanitize_stub_cst(stub_cst: cst.Module) -> cst.Module:
    return stub_cst.visit(TypeshedSanitizer())


def generate_stub(py_path: Path, output_stub_path: Path, verbose: bool = False) -> None:
    if verbose:
        print(f"[*] Generating stub for '{py_path.name}' using stubgen...")
    with tempfile.TemporaryDirectory() as tmp_out_dir:
        cmd = [
            sys.executable,
            "-c",
            "from mypy.stubgen import main; main()",
            "--include-private",
            "--include-docstrings",
            "-o",
            tmp_out_dir,
            str(py_path),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception as e:
            msg = f"Failed to execute stubgen: {e}"
            raise RuntimeError(msg) from e
        if result.returncode != 0:
            error_msg = result.stderr.strip() or result.stdout.strip()
            msg = f"stubgen failed with exit code {result.returncode}:\n{error_msg}"
            raise RuntimeError(msg)
        generated_stubs = list(Path(tmp_out_dir).rglob("*.pyi"))
        if not generated_stubs:
            msg = (
                f"stubgen finished but no .pyi file was generated in output directory. Output: {result.stdout.strip()}"
            )
            raise RuntimeError(msg)
        target_stub = next(
            (s for s in generated_stubs if s.stem == py_path.stem),
            generated_stubs[0],
        )
        output_stub_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(target_stub, output_stub_path)
    if verbose:
        print(f"[+] Created stub file at: {output_stub_path}")


def apply_type_annotations(
    source_code: str,
    stub_code: str,
    overwrite_existing: bool = True,
    use_future_annotations: bool = False,
) -> str:
    try:
        source_cst = cst.parse_module(source_code)
    except Exception as e:
        msg = f"Failed to parse source file with LibCST: {e}"
        raise ValueError(msg) from e
    try:
        stub_cst = cst.parse_module(stub_code)
    except Exception as e:
        msg = f"Failed to parse stub file with LibCST: {e}"
        raise ValueError(msg) from e
    stub_cst = sanitize_stub_cst(stub_cst)
    context = CodemodContext()
    ApplyTypeAnnotationsVisitor.store_stub_in_context(
        context=context,
        stub=stub_cst,
        overwrite_existing_annotations=overwrite_existing,
        use_future_annotations=use_future_annotations,
    )
    transformer = ApplyTypeAnnotationsVisitor(context)
    annotated_cst = transformer.transform_module(source_cst)
    return annotated_cst.code


def validate_python_code(code: str, filename: str) -> None:
    try:
        ast.parse(code, filename=filename)
    except SyntaxError as e:
        msg = (
            f"Resulting code has invalid Python syntax at line {e.lineno}, column {e.offset}: {e.msg}\n"
            f"Code snippet:\n{e.text}"
        )
        raise SyntaxError(msg) from e


def compute_diff(original: str, modified: str, filename: str) -> str:
    diff = difflib.unified_diff(
        original.splitlines(keepends=True),
        modified.splitlines(keepends=True),
        fromfile=f"{filename} (original)",
        tofile=f"{filename} (annotated)",
    )
    return "".join(diff)


def annotate_file(
    target_file: str | Path,
    stub_file: str | Path | None = None,
    overwrite_existing: bool = True,
    use_future_annotations: bool = False,
    dry_run: bool = False,
    show_diff: bool = False,
    verbose: bool = True,
) -> tuple[bool, str]:
    py_path = Path(target_file).resolve()
    if not py_path.exists():
        msg = f"Target file does not exist: {py_path}"
        raise FileNotFoundError(msg)
    if not py_path.is_file():
        msg = f"Target path is not a file: {py_path}"
        raise IsADirectoryError(msg)
    if py_path.suffix != ".py":
        msg = f"Target file must have a .py extension, got: {py_path.name}"
        raise ValueError(msg)
    if stub_file is not None:
        stub_path = Path(stub_file).resolve()
        if not stub_path.is_file():
            msg = f"Specified stub file does not exist: {stub_path}"
            raise FileNotFoundError(msg)
    else:
        stub_path = py_path.with_suffix(".pyi")
        if not stub_path.is_file():
            generate_stub(py_path, stub_path, verbose=verbose)
    original_code = py_path.read_text(encoding="utf-8")
    stub_code = stub_path.read_text(encoding="utf-8")
    validate_python_code(original_code, filename=str(py_path))
    annotated_code = apply_type_annotations(
        source_code=original_code,
        stub_code=stub_code,
        overwrite_existing=overwrite_existing,
        use_future_annotations=use_future_annotations,
    )
    validate_python_code(annotated_code, filename=str(py_path))
    is_changed = annotated_code != original_code
    if show_diff or (verbose and is_changed):
        diff_text = compute_diff(original_code, annotated_code, str(py_path))
        if diff_text and show_diff:
            print("\n" + "=" * 60)
            print("DIFF:")
            print("=" * 60)
            print(diff_text, end="")
            print("=" * 60 + "\n")
    if is_changed:
        if not dry_run:
            temp_file = py_path.with_suffix(".py.tmp")
            try:
                temp_file.write_text(annotated_code, encoding="utf-8")
                temp_file.replace(py_path)
            except Exception as e:
                if temp_file.exists():
                    temp_file.unlink()
                msg = f"Failed to write updated file: {e}"
                raise OSError(msg) from e
            if verbose:
                print(f"[+] Successfully updated '{py_path}' in-place with type annotations.")
        elif verbose:
            print(f"[*] [Dry Run] '{py_path}' would be updated in-place.")
    elif verbose:
        print(f"[*] No annotation changes needed for '{py_path}'.")
    return is_changed, annotated_code


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Add type annotations to a Python (.py) file using its .pyi stub file and LibCST.\n"
            "If the stub file does not exist beside the original file, it is created using stubgen.\n"
            "Updates the file in-place after validating syntax with ast.parse."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "file",
        help="Path to the .py source file to annotate (input=sys.argv[1]).",
    )
    parser.add_argument(
        "-s",
        "--stub-file",
        default=None,
        help="Path to custom .pyi stub file (defaults to <file>.pyi beside the target file).",
    )
    parser.add_argument(
        "--no-overwrite",
        action="store_true",
        help="Do not overwrite existing type annotations in the source code.",
    )
    parser.add_argument(
        "--future-annotations",
        action="store_true",
        help="Enable '' support.",
    )
    parser.add_argument(
        "-d",
        "--diff",
        action="store_true",
        help="Show unified diff of modifications.",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Simulate the transformation without modifying the file.",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress output messages except errors.",
    )
    args = parser.parse_args()
    try:
        annotate_file(
            target_file=args.file,
            stub_file=args.stub_file,
            overwrite_existing=not args.no_overwrite,
            use_future_annotations=args.future_annotations,
            dry_run=args.dry_run,
            show_diff=args.diff,
            verbose=not args.quiet,
        )
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
