#!/data/data/com.termux/files/usr/bin/env python
"""Collect the emoji used by the files of the current directory into ``emojis.json``.
Scans the files (20 MB limit, UTF-8 only) with a regex that covers the pictographic blocks, the variation selectors, the skin tones, the ZWJ sequences and the regional indicators and writes the sorted ``U+XXXX`` keys with their glyph to ``emojis.json``.
Usage: xemoji Examples: $ xemoji Runs as a script (``python xemoji.py``) in the directory it should act on; the module can also be imported.
"""

import json
from multiprocessing import Pool
from pathlib import Path
import re

_BASE: str = (
    "\U0001f300-\U0001f5ff"
    "\U0001f600-\U0001f64f"
    "\U0001f680-\U0001f6ff"
    "\U0001f700-\U0001f77f"
    "\U0001f780-\U0001f7ff"
    "\U0001f800-\U0001f8ff"
    "\U0001f900-\U0001f9ff"
    "\U0001fa00-\U0001faff"
    "\U00002600-\U000026ff"
    "\U00002700-\U000027bf"
    "\U0001f000-\U0001f0ff"
    "\U00002b00-\U00002bff"
    "\U00002190-\U000021ff"
    "\U00002300-\U000023ff"
    "\U000025a0-\U000025ff"
    "\U00002500-\U0000257f"
)
_VS: str = "\ufe0e\ufe0f"
_SKIN: str = "\U0001f3fb-\U0001f3ff"
_ZWJ: str = "\u200d"
_RI: str = "\U0001f1e6-\U0001f1ff"
EMOJI_RE = re.compile(
    f"(?:[{_BASE}][{_VS}]?[{_SKIN}]?(?:{_ZWJ}[{_BASE}][{_VS}]?[{_SKIN}]?)*|[{_RI}]{{2}})",
    flags=re.UNICODE,
)


def scan_file(path: Path) -> dict[str, str]:
    try:
        if path.stat().st_size > 20 * 1024 * 1024:
            return {}
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    result: dict[str, str] = {}
    for match in EMOJI_RE.finditer(text):
        emoji = match.group(0)
        key = " ".join(f"U+{ord(ch):04X}" for ch in emoji)
        result[key] = emoji
    return result


def main() -> None:
    here = Path.cwd()
    out_path = here / "emojis.json"
    out_resolved = out_path.resolve()
    paths = [p for p in here.rglob("*") if p.is_file() and p.resolve() != out_resolved]
    found: dict[str, str] = {}
    with Pool() as pool:
        for partial in pool.imap_unordered(scan_file, paths):
            found.update(partial)
    out_path.write_text(
        json.dumps(found, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(f"Found {len(found)} unique emojis. Wrote to {out_path}")


if __name__ == "__main__":
    main()
