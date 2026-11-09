#!/data/data/com.termux/files/usr/bin/env python
"""Convert Windows ``.bat`` scripts into executable bash scripts.
Translates the common batch commands (``echo``, ``cd``, ``dir``, ``del``, ``copy``, ``move``, ``ren``, ``type``, ``findstr``, ``if exist`` ...) and ``%VAR%`` variables into their shell equivalents.
Every ``*.bat`` below the given directory becomes a ``*.sh`` with mode 755 and the original is removed.
Usage: bat2bash [DIRECTORY] Examples: $ bat2bash ./scripts Runs as a script (``python bat2bash.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import re
import sys


class BatToShConverter:
    COMMAND_MAP: dict[str, str] = {
        r"^echo\s+off\s*$": "# Echo off",
        r"^@echo\s+off\s*$": "# Echo off",
        r"^pause\s*$": 'read -p "Press enter to continue..."',
        r"^cls\s*$": "clear",
        r"^exit\s*$": "exit 0",
        r"^cd\s+": "cd ",
        r"^dir\s*$": "ls -la",
        r"^dir\s+": "ls -la ",
        r"^del\s+": "rm ",
        r"^copy\s+": "cp ",
        r"^move\s+": "mv ",
        r"^ren\s+": "mv ",
        r"^type\s+": "cat ",
        r"^findstr\s+": "grep ",
        r"^if\s+exist\s+": "if [ -f ",
        r"^if\s+not\s+exist\s+": "if [ ! -f ",
    }

    def __init__(self):
        self.converted_count = 0
        self.error_count = 0

    def convert_line(self, line: str) -> str:
        line = line.rstrip("\r\n")
        if not line.strip() or line.strip().startswith("REM"):
            if line.strip().startswith("REM"):
                return line.replace("REM", "#", 1)
            return line
        line = re.sub(r"%([A-Za-z_][A-Za-z0-9_]*)%", r"$\1", line)
        line = re.sub(r"^set\s+", "", line, flags=re.IGNORECASE)
        line = re.sub(
            r'if\s+"?([^"]*?)"?\s+==\s+"?([^"]*?)"?',
            r'if [ "$\1" = "$\2" ]',
            line,
            flags=re.IGNORECASE,
        )
        for pattern, replacement in self.COMMAND_MAP.items():
            if re.match(pattern, line, re.IGNORECASE):
                line = re.sub(pattern, replacement, line, flags=re.IGNORECASE)
                break
        line = re.sub(r"^:\w+\s*$", lambda m: f"# {m.group(0)}", line)
        line = re.sub(r"goto\s+(\w+)", r"# TODO: goto \1", line, flags=re.IGNORECASE)
        return line

    def convert_file(self, bat_file: Path) -> str:
        try:
            with open(bat_file, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except Exception as e:
            print(f"❌ Error reading {bat_file}: {e}")
            self.error_count += 1
            return ""
        converted_lines = [
            "#!/bin/bash\n",
            "# Converted from " + bat_file.name + "\n",
            "\n",
        ]
        for line in lines:
            converted_lines.append(self.convert_line(line) + "\n")
        return "".join(converted_lines)

    def process_directory(self, directory: Path | None = None) -> None:
        if directory is None:
            directory = Path.cwd()
        if not directory.exists():
            print(f"❌ Directory not found: {directory}")
            return
        bat_files = list(directory.rglob("*.bat"))
        if not bat_files:
            print(f"ℹ️  No .bat files found in {directory}")
            return
        print(f"🔍 Found {len(bat_files)} .bat file(s)\n")
        for bat_file in bat_files:
            sh_file = bat_file.with_suffix(".sh")
            print(f"📝 Converting: {bat_file.name} → {sh_file.name}")
            converted_content = self.convert_file(bat_file)
            if converted_content:
                try:
                    with open(sh_file, "w", encoding="utf-8") as f:
                        f.write(converted_content)
                    sh_file.chmod(0o755)
                    self.converted_count += 1
                    bat_file.unlink()
                    print(f"   ✅ Converted successfully\n")
                except Exception as e:
                    print(f"   ❌ Error writing {sh_file}: {e}\n")
                    self.error_count += 1

    def print_summary(self) -> None:
        print("=" * 40)
        print(f"📊 Conversion Summary")
        print(f"   ✅ Successful: {self.converted_count}")
        print(f"   ❌ Errors: {self.error_count}")
        print("=" * 40)


def main():
    target_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    converter = BatToShConverter()
    converter.process_directory(target_dir)
    converter.print_summary()


if __name__ == "__main__":
    raise SystemExit(main())
