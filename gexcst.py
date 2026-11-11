#!/data/data/com.termux/files/usr/bin/env python
"""Extract the functions, classes and constants of Python sources into separate files.
Walks the given files, directories and archives (``.whl``, ``.zip``, ``.tar*``, ``.zst``) with a LibCST-based extractor and writes every entity to ``output/`` with a unique name, keeping the source text intact.
Files are handled in a worker pool.
Usage: gexcst [paths ...] Examples: $ gexcst pkg/ dist/pkg.whl Runs as a script (``python gexcst.py``) in the directory it should act on; the module can also be imported."""

from multiprocessing import Pool, cpu_count
import os
from pathlib import Path
import re
import shutil
import tarfile
from typing import Any
import zipfile

import libcst as cst
from libcst.metadata import MetadataWrapper, PositionProvider


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


class EntityExtractor(cst.CSTVisitor):
    METADATA_DEPENDENCIES = (PositionProvider,)

    def __init__(self, source_content: str, original_path: Path) -> None:
        self.entities: list[dict[str, Any]] = []
        self.source_lines = source_content.splitlines(keepends=True)
        self.original_path = original_path
        self.scope_depth = 0

    def _get_source_slice(self, node: cst.CSTNode) -> str:
        try:
            position = self.get_metadata(PositionProvider, node)
            start_line = position.start.line - 1
            end_line = position.end.line
            start_col = position.start.column
            end_col = position.end.column
            code_lines = self.source_lines[start_line:end_line].copy()
            if not code_lines:
                return ""
            if start_line == end_line - 1:
                return code_lines[0][start_col:end_col]
            code_lines[0] = code_lines[0][start_col:]
            if end_col > 0 and len(code_lines) > 1:
                code_lines[-1] = code_lines[-1][:end_col]
            return "".join(code_lines)
        except Exception:
            if hasattr(node, "body") and isinstance(node.body, cst.SimpleStatementSuite):
                return "".join(
                    self.source_lines[
                        self.get_metadata(PositionProvider, node).start.line - 1 : self.get_metadata(
                            PositionProvider, node
                        ).end.line
                    ]
                )
            return ""

    def _extract_and_save(self, node: cst.CSTNode, entity_type: str, name: str) -> None:
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

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool | None:
        if self.scope_depth == 0:
            self._extract_and_save(node, "function", node.name.value)
            return False
        return True

    def visit_ClassDef(self, node: cst.ClassDef) -> bool | None:
        if self.scope_depth == 0:
            self._extract_and_save(node, "class", node.name.value)
            return False
        return True

    def visit_Assign(self, node: cst.Assign) -> bool | None:
        if self.scope_depth == 0 and len(node.targets) == 1:
            target = node.targets[0].target
            if isinstance(target, cst.Name):
                target_name = target.value
                if re.match(r"^[A-Z_][A-Z0-9_]*$", target_name):
                    self._extract_and_save(node, "constant", target_name)
        return True

    def visit_AnnAssign(self, node: cst.AnnAssign) -> bool | None:
        if self.scope_depth == 0:
            target = node.target
            if isinstance(target, cst.Name):
                target_name = target.value
                if re.match(r"^[A-Z_][A-Z0-9_]*$", target_name):
                    self._extract_and_save(node, "constant", target_name)
        return True

    def visit_IndentedBlock(self, node: cst.IndentedBlock) -> bool | None:
        self.scope_depth += 1
        return True

    def leave_IndentedBlock(self, original_node: cst.IndentedBlock) -> None:
        self.scope_depth -= 1


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
        tree = cst.parse_module(content)
        wrapper = MetadataWrapper(tree)
        extractor = EntityExtractor(content, path)
        wrapper.visit(extractor)
        return extractor.entities
    except cst.ParserSyntaxError:
        return []
    except Exception as e:
        print(f"Error parsing CST for {path}: {e}")
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
    except Exception:
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
    entities: list[dict[str, Any]] = []
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


def main() -> int:
    print(f"Starting analysis in {Path.cwd()}...")
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
        print(f"Cleaned previous output directory: {OUTPUT_DIR}")
    OUTPUT_DIR.mkdir(exist_ok=True)
    files_to_process: list[str] = []
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
        return 0
    print(f"Found {len(files_to_process)} relevant files/archives. Starting multiprocessing pool...")
    num_cpus = cpu_count()
    all_entities: list[dict[str, Any]] = []
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
