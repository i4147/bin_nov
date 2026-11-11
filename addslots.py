#!/data/data/com.termux/files/usr/bin/env python
import sys
from pathlib import Path
import libcst as cst
from libcst import matchers as m
from dh import get_pyfiles, mpf, cprint


class DataclassSlotsTransformer(cst.CSTTransformer):
    """Mutates @dataclass to include slots=True while preserving formatting."""

    def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> cst.ClassDef:
        new_decorators = []
        modified = False

        # LibCST uses node.decorators (not decorator_list)
        for decorator in updated_node.decorators:
            # Case 1: Simple identifier -> @dataclass
            if m.matches(decorator.decorator, m.Name(value="dataclass")):
                modified = True
                # Construct @dataclass(slots=True)
                new_decorator = decorator.with_changes(
                    decorator=cst.Call(
                        func=cst.Name(value="dataclass"),
                        args=[
                            cst.Arg(
                                keyword=cst.Name(value="slots"),
                                equal=cst.AssignEqual(
                                    whitespace_before=cst.SimpleWhitespace(""),
                                    whitespace_after=cst.SimpleWhitespace(""),
                                ),
                                value=cst.Name(value="True"),
                            )
                        ],
                    )
                )
                new_decorators.append(new_decorator)

            # Case 2: Already a function call -> @dataclass(...)
            elif m.matches(decorator.decorator, m.Call(func=m.Name(value="dataclass"))):
                call_node = decorator.decorator

                # Check if slots is already explicitly provided
                has_slots = any(arg.keyword and arg.keyword.value == "slots" for arg in call_node.args)

                if not has_slots:
                    modified = True
                    # Create the slots=True argument
                    slots_arg = cst.Arg(
                        keyword=cst.Name(value="slots"),
                        equal=cst.AssignEqual(
                            whitespace_before=cst.SimpleWhitespace(""), whitespace_after=cst.SimpleWhitespace("")
                        ),
                        value=cst.Name(value="True"),
                    )

                    # Append it gracefully to existing arguments
                    new_args = list(call_node.args)
                    if new_args:
                        # Ensure there's a proper comma separating them
                        new_args[-1] = new_args[-1].with_changes(
                            comma=cst.Comma(whitespace_after=cst.SimpleWhitespace(" "))
                        )
                    new_args.append(slots_arg)

                    new_decorator = decorator.with_changes(decorator=call_node.with_changes(args=new_args))
                    new_decorators.append(new_decorator)
                else:
                    new_decorators.append(decorator)
            else:
                new_decorators.append(decorator)

        if modified:
            return updated_node.with_changes(decorators=tuple(new_decorators))

        return updated_node


def process_file(file_path: Path) -> None:
    """Modifies dataclass decorators in a single file inplace."""
    try:
        source_code = file_path.read_text(encoding="utf-8")

        # Parse the code into a Concrete Syntax Tree
        cst_tree = cst.parse_module(source_code)

        # Transform the tree
        transformer = DataclassSlotsTransformer()
        modified_tree = cst_tree.visit(transformer)

        # Only rewrite the file if changes were applied
        if cst_tree != modified_tree:
            file_path.write_text(modified_tree.code, encoding="utf-8")
            cprint(f"Updated: {file_path.name}")
        else:
            print(f"No changes: {file_path.name}")

    except Exception as e:
        print(f"Error processing {file_path}: {e}", file=sys.stderr)


def main(target_dir: str = ".") -> None:
    """Finds all Python files recursively in the target directory and processes them."""
    root_path = Path(target_dir).resolve()
    print(f"Scanning directory: {root_path}")

    # Discover target files while avoiding virtual envs and standard hidden directories
    py_files = get_pyfiles(root_path)

    mpf(process_file, py_files)


if __name__ == "__main__":
    directory = sys.argv[1] if len(sys.argv) > 1 else "."
    main(directory)
