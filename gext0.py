#!/data/data/com.termux/files/usr/bin/env python
"""Extract Python entities from files and archives into ``output/``.
Like :mod:`gexcst` but AST-based: functions, classes and constants of every scanned source (plain files, directories and archives) are written to ``output/`` with collision-free names.
Usage: gext0 [paths ...] Examples: $ gext0 src/ Runs as a script (``python gext0.py``) in the directory it should act on; the module can also be imported.
"""

import ast
from multiprocessing import Pool, cpu_count
import os
from pathlib import Path
import re
import shutil
import tarfile
from typing import Any
import zipfile

OUTPUT_DIR: Path = Path("output")
ARCHIVE_EXT: tuple[str, ...] = (
    ".whl",
    ".zip",
    ".tar.gz",
    ".tgz",
    ".tar.zst",
    ".tar.xz",
    ".tar",
    ".zst",
)
ALLOWED_PYTHON_EXT: tuple[str, str] = (".py", "")


class EntityExtractor(ast.NodeVisitor):
    def __init__(self, source_content: str, original_path: Path) -> None:
        self.entities = []
        self.source_lines = source_content.splitlines(keepends=True)
        self.original_path = original_path
        self.scope_depth = 0

    def _get_source_slice(self, node: ast.AST) -> str:
        start_line = node.lineno - 1
        end_line = node.end_lineno or node.lineno
        code_slice = self.source_lines[start_line:end_line]
        if node.col_offset is not None:
            code_slice[0] = code_slice[0][node.col_offset :]
        if node.end_col_offset is not None and node.end_col_offset > 0:
            last_line = code_slice[-1]
            code_slice[-1] = last_line[: node.end_col_offset]
        return "".join(code_slice)

    def _extract_and_save(self, node: ast.AST, entity_type: str, name: str) -> None:
        entity_code = self._get_source_slice(node)
        self.entities.append({
            "name": name,
            "full_name": name,
            "type": entity_type,
            "code": entity_code,
            "path": str(self.original_path),
            "is_constant": entity_type == "constant",
            "is_class": entity_type == "class",
            "is_function": entity_type == "function",
        })

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if self.scope_depth == 0:
            self._extract_and_save(node, "function", node.name)
        self.scope_depth += 1
        self.generic_visit(node)
        self.scope_depth -= 1

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if self.scope_depth == 0:
            self._extract_and_save(node, "function", node.name)
        self.scope_depth += 1
        self.generic_visit(node)
        self.scope_depth -= 1

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if self.scope_depth == 0:
            self._extract_and_save(node, "class", node.name)
        self.scope_depth += 1
        self.generic_visit(node)
        self.scope_depth -= 1

    def visit_Assign(self, node: ast.Assign) -> None:
        if self.scope_depth == 0 and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target_name = node.targets[0].id
            if re.match("^[A-Z_][A-Z0-9_]*$", target_name):
                self._extract_and_save(node, "constant", target_name)

    def generic_visit(self, node: ast.AST) -> None:
        super().generic_visit(node)


def get_unique_path(base_path: Path) -> Path:
    if not base_path.exists():
        return base_path
    name = base_path.stem
    suffix = base_path.suffix
    i = 1
    while True:
        new_path = base_path.with_name(f"{name}_{i}{suffix}")
        if not new_path.exists():
            return new_path
        i += 1


def save_entity(entity: dict[str, Any]) -> None:
    filename_base = f"{entity['full_name']}.py"
    output_path_base = OUTPUT_DIR / entity["type"] / filename_base
    output_path_base.parent.mkdir(parents=True, exist_ok=True)
    comment = f"# Original path: {entity['path']}\n"
    content = comment + entity["code"]
    final_py_path = get_unique_path(output_path_base)
    try:
        with open(final_py_path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        print(f"Error saving {final_py_path}: {e}")


def extract_entities_from_content(content: str, path: Path) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(content)
        extractor = EntityExtractor(content, path)
        extractor.visit(tree)
        return extractor.entities
    except SyntaxError:
        return []
    except Exception as e:
        print(f"Error parsing AST for {path}: {e}")
        return []


def is_python_file_no_extension(path: Path) -> bool:
    if path.suffix:
        return False
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            first_lines = "".join(f.readlines(1024))
            if re.match(r"#!\s*/.*python", first_lines):
                return True
            if "def " in first_lines or "class " in first_lines or "import " in first_lines:
                return True
    except:
        pass
    return False


def process_single_file(path: Path) -> list[dict[str, Any]]:
    try:
        if path.suffix == ".py" or is_python_file_no_extension(path):
            content = path.read_text(encoding="utf-8", errors="ignore")
            return extract_entities_from_content(content, path)
        return []
    except Exception as e:
        print(f"Error reading file {path}: {e}")
        return []


def process_archive(path: Path) -> list[dict[str, Any]]:
    entities = []
    if path.suffix in (".zip", ".whl"):
        try:
            with zipfile.ZipFile(path, "r") as zf:
                for member in zf.namelist():
                    member_path = Path(member)
                    if member_path.suffix == ".py":
                        with zf.open(member) as member_file:
                            content = member_file.read().decode("utf-8", errors="ignore")
                            virtual_path = Path(f"{path}/{member}")
                            entities.extend(extract_entities_from_content(content, virtual_path))
        except Exception as e:
            print(f"Error processing ZIP/WHL archive {path}: {e}")
    elif any(path.name.endswith(ext) for ext in [".tar", ".tar.gz", ".tgz", ".tar.zst", ".tar.xz"]):
        mode_map = {
            ".tar.gz": "r:gz",
            ".tgz": "r:gz",
            ".tar.zst": "r:zst",
            ".tar.xz": "r:xz",
            ".tar": "r",
        }
        mode = next((mode_map[ext] for ext in mode_map if path.name.endswith(ext)), "r")
        try:
            with tarfile.open(path, mode) as tf:
                for member in tf.getmembers():
                    member_path = Path(member.name)
                    if member.isfile() and member_path.suffix == ".py":
                        member_file = tf.extractfile(member)
                        if member_file:
                            content = member_file.read().decode("utf-8", errors="ignore")
                            virtual_path = Path(f"{path}/{member.name}")
                            entities.extend(extract_entities_from_content(content, virtual_path))
        except tarfile.ReadError:
            pass
        except Exception as e:
            print(f"Error processing TAR archive {path}: {e}")
    return entities


def worker_process(path_str: str) -> list[dict[str, Any]]:
    path = Path(path_str)
    if path.name.endswith(ARCHIVE_EXT):
        return process_archive(path)
    return process_single_file(path)


def main() -> None:
    print(f"Starting analysis in {Path.cwd()}...")
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
        print(f"Cleaned previous output directory: {OUTPUT_DIR}")
    OUTPUT_DIR.mkdir(exist_ok=True)
    files_to_process = []
    current_dir = Path()
    for root, _, filenames in os.walk(current_dir):
        for name in filenames:
            path = Path(root) / name
            if path.is_relative_to(OUTPUT_DIR):
                continue
            is_archive = path.suffix in ARCHIVE_EXT or any(path.name.endswith(ext) for ext in ARCHIVE_EXT)
            is_py = path.suffix in ALLOWED_PYTHON_EXT or is_python_file_no_extension(path)
            if is_archive or is_py:
                files_to_process.append(str(path))
    if not files_to_process:
        print("No Python files or archives found to process.")
        return
    print(f"Found {len(files_to_process)} relevant files/archives. Starting multiprocessing pool...")
    num_cpus = cpu_count()
    all_entities = []
    with Pool(processes=num_cpus) as pool:
        results_list = pool.map(worker_process, files_to_process)
        for result in results_list:
            all_entities.extend(result)
    print(f"Processing complete. Extracted {len(all_entities)} entities.")
    print(f"Saving entities to {OUTPUT_DIR}...")
    for entity in all_entities:
        save_entity(entity)
    print("\n\nAll tasks finished successfully!")
    print(f"Results are saved in the '{OUTPUT_DIR}' folder, organized by entity type (class, function, constant).")


if __name__ == "__main__":
    raise SystemExit(main())
