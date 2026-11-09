#!/data/data/com.termux/files/usr/bin/env python
"""Extract URLs/links from a Telegram channel using Telethon."""

import argparse
import asyncio
import os
from pathlib import Path
import re
import sys
from typing import Iterator

from dotenv import load_dotenv
from telethon import TelegramClient


PATTERN_PRESETS: dict[str, str] = {
    "coc": r"https://link\.clashofclans\.com/[a-zA-Z0-9\?\=\&_]+",
    "general": r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+",
}


def load_credentials(env_path: Path) -> tuple[str | None, str | None]:
    if env_path.exists():
        load_dotenv(env_path)
    api_id = os.environ.get("API_ID")
    api_hash = os.environ.get("API_HASH")
    return api_id, api_hash


def extract_matches(text: str, pattern: str) -> Iterator[str]:
    for match in re.findall(pattern, text):
        yield match


async def run_extractor(args: argparse.Namespace) -> None:
    env_file = Path(args.env).expanduser()
    env_api_id, env_api_hash = load_credentials(env_file)
    api_id = args.api_id or env_api_id
    api_hash = args.api_hash or env_api_hash
    if not api_id or not api_hash:
        print(
            "Error: API_ID and API_HASH must be provided via CLI arguments or environment variables.",
            file=sys.stderr,
        )
        sys.exit(1)
    pattern = args.pattern or PATTERN_PRESETS[args.pattern_preset]
    client = TelegramClient(args.session, api_id, api_hash)
    if args.phone:
        await client.start(phone=args.phone)
    else:
        await client.start()
    try:
        entity = await client.get_entity(args.channel)
        channel_name = getattr(entity, "title", args.channel)
        print(f"Searching for links in '{channel_name}'...")
        iter_kwargs: dict = {}
        if args.limit is not None:
            iter_kwargs["limit"] = args.limit
        if args.search:
            iter_kwargs["search"] = args.search
        found_count = 0
        output_file_handle = open(args.output, "a", encoding="utf-8") if args.output else None
        try:
            async for message in client.iter_messages(entity, **iter_kwargs):
                if not message.text:
                    continue
                links = list(extract_matches(message.text, pattern))
                if links:
                    for link in links:
                        found_count += 1
                        print(f"Found link [Msg ID {message.id}]: {link}")
                        if output_file_handle:
                            output_file_handle.write(link + "\n")
        finally:
            if output_file_handle:
                output_file_handle.close()
        print(f"\nDone. Extracted {found_count} link(s).")
    finally:
        await client.disconnect()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract URLs/links from a Telegram channel using Telethon.")
    parser.add_argument(
        "channel",
        type=str,
        help="Channel username, link, or ID (e.g., 'username_of_the_channel' or 'https://t.me/pycode_hubb')",
    )
    parser.add_argument(
        "-s",
        "--search",
        type=str,
        default=None,
        help="Optional text search query to filter messages on Telegram's servers.",
    )
    parser.add_argument(
        "-l",
        "--limit",
        type=int,
        default=None,
        help="Maximum number of recent messages to fetch.",
    )
    parser.add_argument(
        "-p",
        "--pattern-preset",
        choices=list(PATTERN_PRESETS.keys()),
        default="general",
        help="Predefined regex pattern preset (default: 'general').",
    )
    parser.add_argument(
        "--pattern",
        type=str,
        default=None,
        help="Custom regex pattern (overrides --pattern-preset).",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="File path to append extracted links (e.g., 'links.txt').",
    )
    parser.add_argument(
        "--session",
        type=str,
        default="session_name",
        help="Telethon session name (default: 'session_name').",
    )
    parser.add_argument(
        "--env",
        type=str,
        default="~/.env",
        help="Path to .env file for credentials (default: '~/.env').",
    )
    parser.add_argument(
        "--phone",
        type=str,
        default=None,
        help="Phone number for Telegram client authentication.",
    )
    parser.add_argument(
        "--api-id",
        type=str,
        default=None,
        help="Telegram API ID (overrides .env).",
    )
    parser.add_argument(
        "--api-hash",
        type=str,
        default=None,
        help="Telegram API Hash (overrides .env).",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    asyncio.run(run_extractor(args))


if __name__ == "__main__":
    main()
