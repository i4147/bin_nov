#!/data/data/com.termux/files/usr/bin/env python
"""Translate text files in place with ChatGPT (Gemini fallback).
Loads the API key from ``.env``, collects the files of ``-e`` extensions (``txt``/``md`` by default), translates each of them into ``-l`` language and overwrites the original.
Usage: inplace_translator [-e txt,md] [-l LANGUAGE] Options: -e, --extensions Comma-separated file extensions to look for (default: txt,md) -l, --lang Target translation destination language (default: English) Examples: $ inplace_translator -e md -l English Needs an OpenAI or Gemini API key and replaces the content of the source files.
"""

import argparse
import glob
import os
from pathlib import Path
import sys
from typing import Any

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None
try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None


def load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip("'\"")
            os.environ[key] = val


class UniversalTranslator:
    def __init__(self) -> None:
        home_env = Path.home() / ".env"
        load_env_file(home_env)
        self.openai_key = os.getenv("OPENAI_API_KEY")
        self.gemini_key = os.getenv("GEMINI_API_KEY")
        self.openai_client = None
        self.gemini_client = None
        if self.openai_key and OpenAI:
            self.openai_client = OpenAI(api_key=self.openai_key)
        elif self.gemini_key and genai:
            self.gemini_client = genai.Client(api_key=self.gemini_key)

    def is_configured(self) -> bool:
        return self.openai_client is not None or self.gemini_client is not None

    def translate_text(self, text: str, target_lang="English"):
        if not text.strip():
            return text
        prompt = (
            f"You are a professional, high-fidelity translation engine.\n"
            f"Translate the following text into fluent {target_lang}.\n"
            f"Preserve all original formatting, paragraphs, code blocks, syntax markers, placeholders, and structure.\n"
            f"Only output the translated text. Do not include any explanations, preambles, notes, or markdown wrappers unless they were present in the source text.\n\n"
            f"Source Text:\n{text}"
        )
        if self.openai_client:
            try:
                response = self.openai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {
                            "role": "system",
                            "content": "You are a helpful translation assistant.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.3,
                )
                return response.choices[0].message.content.strip()
            except Exception as e:
                print(
                    f"⚠️ OpenAI translation error: {e}. Falling back to alternative methods...",
                    file=sys.stderr,
                )
                if self.gemini_key and genai and not self.gemini_client:
                    self.gemini_client = genai.Client(api_key=self.gemini_key)
        if self.gemini_client:
            try:
                response = self.gemini_client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                    config=types.GenerateContentConfig(temperature=0.3),
                )
                return response.text.strip()
            except Exception as e:
                print(f"❌ Gemini translation error: {e}", file=sys.stderr)
        msg = "No operational translation client or fallback API routes available."
        raise RuntimeError(msg)


def process_file(file_path: Path, translator: Any, target_lang: str) -> None:
    print(f"📄 Processing: {file_path}...")
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        if not content.strip():
            print(f"   ℹ️ Skipping empty file: {file_path}")
            return
        translated_content = translator.translate_text(content, target_lang=target_lang)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(translated_content)
        print(f"   ✅ Done.")
    except Exception as e:
        print(f"   ❌ Error processing {file_path}: {e}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Inplace file translator using ChatGPT (with Gemini fallback).")
    parser.add_argument(
        "-e",
        "--extensions",
        default="txt,md",
        help="Comma-separated file extensions to look for (default: txt,md)",
    )
    parser.add_argument(
        "-l",
        "--lang",
        default="English",
        help="Target translation destination language (default: English)",
    )
    args = parser.parse_args()
    if OpenAI is None and genai is None:
        print(
            "❌ Error: Missing required packages. Run: pip install openai google-genai",
            file=sys.stderr,
        )
        sys.exit(1)
    translator = UniversalTranslator()
    if not translator.is_configured():
        print(
            "❌ Error: Valid credentials missing. Ensure OPENAI_API_KEY or GEMINI_API_KEY is defined in ~/.env",
            file=sys.stderr,
        )
        sys.exit(1)
    if translator.openai_client:
        print("🚀 Primary Translation Engine Active: ChatGPT (gpt-4o-mini)")
    else:
        print("🚀 Fallback Translation Engine Active: Gemini (gemini-2.5-flash)")
    extensions = [ext.strip().lstrip(".") for ext in args.extensions.split(",")]
    files_to_translate = []
    for ext in extensions:
        files_to_translate.extend(glob.glob(f"*.{ext}"))
    if not files_to_translate:
        print(f"No files found matching extensions ({', '.join(extensions)}) in the current directory.")
        return
    print(f"Found {len(files_to_translate)} target file(s) for inplace translation.")
    for path in files_to_translate:
        process_file(path, translator, args.lang)


if __name__ == "__main__":
    main()
