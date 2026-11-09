#!/data/data/com.termux/files/usr/bin/env python
"""Browse a Persian↔English dictionary interactively in the terminal.
Loads the JSON mapping from ``/sdcard/dic/dic.json`` into both directions, then offers a prompt with lookup, ``:list``, ``:stats``, ``:export`` (CSV), ``:random`` and fuzzy suggestions for near misses.
New or edited entries are written back to the same JSON file.
Usage: faen [DICTIONARY.json] Examples: $ faen Runs as a script (``python faen.py``) in the directory it should act on; the module can also be imported."""

import json
import os
from pathlib import Path
import sys


class Bidirectionaldictionary:
    def __init__(self, json_file: str = "/sdcard/dic/dic.json") -> None:
        self.json_file = Path(json_file)
        self.persian_to_english: dict[str, str] = {}
        self.english_to_persian: dict[str, str] = {}
        self.load_dictionary()

    def load_dictionary(self) -> None:
        try:
            if not self.json_file.exists():
                print(f"❌ Error: {self.json_file} not found")
                sys.exit(1)
            with self.json_file.open("r", encoding="utf-8") as file:
                data = json.load(file)
            self.persian_to_english = {}
            self.english_to_persian = {}
            for persian, english in data.items():
                self.persian_to_english[persian] = english
                self.english_to_persian[english.lower()] = persian
            print(f"✅ Loaded {len(self.persian_to_english)} entries from {self.json_file}")
        except json.JSONDecodeError as e:
            print(f"❌ Error: Invalid JSON format in {self.json_file}")
            print(f"   {e}")
            sys.exit(1)
        except Exception as e:
            print(f"❌ Error loading dictionary: {e}")
            sys.exit(1)

    def save_dictionary(self) -> None:
        try:
            with open(self.json_file, "w", encoding="utf-8") as file:
                json.dump(self.persian_to_english, file, ensure_ascii=False, indent=2)
            print(f"💾 dictionary saved to {self.json_file}")
        except Exception as e:
            print(f"❌ Error saving dictionary: {e}")

    def search(self, query: str) -> str | None:
        query = query.strip()
        if not query:
            return None
        if query in self.persian_to_english:
            return f"📖 {query} → {self.persian_to_english[query]}"
        query_lower = query.lower()
        if query_lower in self.english_to_persian:
            return f"📖 {query} → {self.english_to_persian[query_lower]}"
        suggestions = self.get_suggestions(query)
        if suggestions:
            result = "🔍 Did you mean:\n"
            for match in suggestions[:5]:
                if match in self.persian_to_english:
                    result += f"  • {match} → {self.persian_to_english[match]}\n"
                else:
                    persian = self.english_to_persian.get(match.lower())
                    if persian:
                        result += f"  • {persian} → {match}\n"
            return result.strip()
        return None

    def get_suggestions(self, query: str) -> list[str]:
        query_lower = query.lower()
        suggestions = []
        for persian in self.persian_to_english:
            if query in persian:
                suggestions.append(persian)
        for english in self.english_to_persian:
            if query_lower in english:
                suggestions.append(english)
        return suggestions

    def add_word(self, persian: str, english: str) -> None:
        persian = persian.strip()
        english = english.strip()
        if not persian or not english:
            print("❌ Error: Both Persian and English words are required")
            return
        if persian in self.persian_to_english:
            print(f"⚠️  Word '{persian}' already exists. Updating...")
        self.persian_to_english[persian] = english
        self.english_to_persian[english.lower()] = persian
        self.save_dictionary()
        print(f"✅ Added: '{persian}' ↔ '{english}'")

    def delete_word(self, word: str) -> None:
        word = word.strip()
        if word in self.persian_to_english:
            english = self.persian_to_english[word]
            del self.persian_to_english[word]
            del self.english_to_persian[english.lower()]
            self.save_dictionary()
            print(f"✅ Deleted: '{word}' ↔ '{english}'")
        elif word.lower() in self.english_to_persian:
            persian = self.english_to_persian[word.lower()]
            del self.english_to_persian[word.lower()]
            del self.persian_to_english[persian]
            self.save_dictionary()
            print(f"✅ Deleted: '{persian}' ↔ '{word}'")
        else:
            print(f"❌ Error: '{word}' not found in dictionary")

    def list_all(self, page: int = 1, per_page: int = 10) -> None:
        if not self.persian_to_english:
            print("📭 dictionary is empty")
            return
        sorted_items = sorted(self.persian_to_english.items())
        total = len(sorted_items)
        total_pages = (total + per_page - 1) // per_page
        if page < 1 or page > total_pages:
            print(f"❌ Invalid page. Total pages: {total_pages}")
            return
        start = (page - 1) * per_page
        end = min(start + per_page, total)
        print(f"\n📚 dictionary (Page {page}/{total_pages}):")
        print("-" * 40)
        for i, (persian, english) in enumerate(sorted_items[start:end], start + 1):
            print(f"{i:3}. {persian:15} → {english}")
        print("-" * 40)
        print(f"Showing {start + 1}-{end} of {total} entries")

    def list_all_full(self) -> None:
        if not self.persian_to_english:
            print("📭 dictionary is empty")
            return
        sorted_items = sorted(self.persian_to_english.items())
        print(f"\n📚 dictionary ({len(sorted_items)} entries):")
        print("-" * 40)
        for i, (persian, english) in enumerate(sorted_items, 1):
            print(f"{i:3}. {persian:15} → {english}")
        print("-" * 40)

    def stats(self) -> dict[str, int]:
        return {
            "total": len(self.persian_to_english),
            "persian": len(self.persian_to_english),
            "english": len(self.english_to_persian),
        }

    def export_csv(self, filename: str = "dictionary_export.csv") -> None:
        try:
            with open(filename, "w", encoding="utf-8") as file:
                file.write("Persian,English\n")
                file.writelines(
                    f"{persian},{english}\n" for persian, english in sorted(self.persian_to_english.items())
                )
            print(f"✅ Exported to {filename}")
        except Exception as e:
            print(f"❌ Error exporting to CSV: {e}")

    def random_word(self) -> None:
        import random

        if not self.persian_to_english:
            print("📭 dictionary is empty")
            return
        persian = random.choice(list(self.persian_to_english.keys()))
        english = self.persian_to_english[persian]
        print(f"🎲 Random: {persian} → {english}")


def main() -> None:
    dict_app = Bidirectionaldictionary("dic.json")
    search_history = []
    print("\n" + "=" * 40)
    print("📖 PERSIAN-ENGLISH BIDIRECTIONAL DICTIONARY")
    print("-" * 40)
    print("Commands:")
    print("  :add <fa> <en>    - Add a new word")
    print("  :del <word>       - Delete a word")
    print("  :list [page]      - list words (page number optional)")
    print("  :list all         - list all words")
    print("  :stats            - Show dictionary statistics")
    print("  :export           - Export to CSV")
    print("  :random           - Show random word")
    print("  :clear            - Clear screen")
    print("  :help             - Show this help")
    print("  :exit/:q          - Exit the application")
    print("-" * 40)
    print("💡 Just type a word to search (supports Persian & English)")
    print("-" * 40)
    while True:
        try:
            user_input = input(": ").strip()
            if not user_input:
                continue
            if user_input.startswith(":"):
                parts = user_input[1:].split(maxsplit=2)
                command = parts[0].lower() if parts else ""
                if command in ["exit", "q", "quit"]:
                    print("👋 Goodbye!")
                    break
                if command == "help":
                    print("\nCommands:")
                    print("  :add <fa> <en>    - Add a new Persian-English word pair")
                    print("  :del <word>       - Delete a word from dictionary")
                    print("  :list [page]      - list words (page number optional)")
                    print("  :list all         - list all words")
                    print("  :stats            - Show dictionary statistics")
                    print("  :export           - Export dictionary to CSV file")
                    print("  :random           - Show a random word")
                    print("  :clear            - Clear the screen")
                    print("  :help             - Show this help")
                    print("  :exit/:q          - Exit the application")
                    print("\n💡 Just type a word to search (works both directions)")
                    continue
                elif command == "add":
                    if len(parts) < 3:
                        print("❌ Usage: :add <persian_word> <english_word>")
                        print("   Example: :add سلام hello")
                        continue
                    persian_word = parts[1]
                    english_word = parts[2]
                    dict_app.add_word(persian_word, english_word)
                    continue
                elif command in ["del", "delete"]:
                    if len(parts) < 2:
                        print("❌ Usage: :del <word>")
                        print("   Example: :del سلام")
                        continue
                    word_to_delete = parts[1]
                    dict_app.delete_word(word_to_delete)
                    continue
                elif command == "list":
                    if len(parts) > 1 and parts[1] == "all":
                        dict_app.list_all_full()
                    else:
                        page = int(parts[1]) if len(parts) > 1 else 1
                        dict_app.list_all(page)
                    continue
                elif command == "stats":
                    stats = dict_app.stats()
                    print("\n📊 dictionary Statistics:")
                    print("-" * 40)
                    print(f"  Total entries:   {stats['total']}")
                    print(f"  Persian words:   {stats['persian']}")
                    print(f"  English words:   {stats['english']}")
                    print("-" * 40)
                    continue
                elif command == "export":
                    dict_app.export_csv()
                    continue
                elif command == "random":
                    dict_app.random_word()
                    continue
                elif command == "clear":
                    os.system("clear" if os.name == "posix" else "cls")
                    continue
                else:
                    print(f"❌ Unknown command: :{command}")
                    print("💡 Type :help for available commands")
                    continue
            else:
                query = user_input
                search_history.append(query)
                result = dict_app.search(query)
                if result:
                    print(result)
                else:
                    print(f"❌ '{query}' not found in dictionary")
                    print("💡 Use :list to see all words or try a different search")
        except KeyboardInterrupt:
            print("\n\n👋 Goodbye!")
            break
        except EOFError:
            print("\n👋 Goodbye!")
            break
        except ValueError as e:
            print(f"❌ Invalid input: {e}")
        except Exception as e:
            print(f"❌ Error: {e}")


if __name__ == "__main__":
    raise SystemExit(main())
