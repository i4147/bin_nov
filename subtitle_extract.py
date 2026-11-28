#!/data/data/com.termux/files/usr/bin/env python
"""Extract subtitle tracks from Matroska (MKV) files.

Parses the EBML structure manually (segment, tracks, clusters), lists subtitle tracks and writes the selected track(s) out as SRT-like text with a progress display.
"""

import mmap
import sys
from pathlib import Path
from typing import BinaryIO, Callable, Optional, TextIO

from dh import atomic_write

SEGMENT: int = 0x18538067
TRACKS: int = 0x1654AE6B
TRACKENTRY: int = 0xAE
TRACKTYPE: int = 0x83
TRACKNUMBER: int = 0xD7
CODECID: int = 0x86
TRACKNAME: int = 0x536E
LANG_BCP47: int = 0x22B59D
LANG: int = 0x22B59C
CLUSTER: int = 0x1F43B675
TIMECODE: int = 0xE7
SIMPLEBLK: int = 0xA3
BLKGROUP: int = 0xA0
BLOCK: int = 0xA1
BLOCKDUR: int = 0x9B


def read_id(f: TextIO):
    b0 = f.read(1)
    if not b0:
        raise EOFError
    first = b0[0]
    mask, length = 0x80, 1
    while length < 4 and not (first & mask):
        mask >>= 1
        length += 1
    rest = f.read(length - 1)
    return int.from_bytes(b0 + rest, "big"), length


def read_size(f: TextIO):
    b0 = f.read(1)
    if not b0:
        raise EOFError
    first = b0[0]
    mask, length = 0x80, 1
    while length < 8 and not (first & mask):
        mask >>= 1
        length += 1
    value = first & (mask - 1)
    for _ in range(length - 1):
        b = f.read(1)
        if not b:
            raise EOFError
        value = (value << 8) | b[0]
    return value, length


def read_vint(f: TextIO):
    b0 = f.read(1)
    if not b0:
        raise EOFError
    first = b0[0]
    mask, length = 0x80, 1
    while length < 8 and not (first & mask):
        mask >>= 1
        length += 1
    value = first & (mask - 1)
    for _ in range(length - 1):
        b = f.read(1)
        if not b:
            raise EOFError
        value = (value << 8) | b[0]
    return value, length


def progress_bar(p: float) -> None:
    bar_len = 40
    filled = int(p * bar_len)
    bar = "█" * filled + "-" * (bar_len - filled)
    sys.stdout.write(f"\rExtracting: |{bar}| {p * 100:5.1f}%")
    sys.stdout.flush()


def _read_header(f: BinaryIO) -> tuple[int, int]:
    eid, _ = read_id(f)
    sz, _ = read_size(f)
    return eid, sz


def extract_subtitle_tracks(path: str) -> list[dict]:
    with open(path, "rb") as f:
        _, h = _read_header(f)
        f.seek(h, 1)
        eid, h = _read_header(f)
        if eid != SEGMENT:
            return []
        end = f.tell() + h
        while f.tell() < end:
            eid, sz = _read_header(f)
            if eid == TRACKS:
                return [t for t in _parse_tracks(f.read(sz)) if t["type"] == 0x11]
            f.seek(sz, 1)
    return []


def _parse_tracks(data: bytes) -> list[dict]:
    from io import BytesIO

    buf = BytesIO(data)
    out = []
    while True:
        try:
            eid, sz = _read_header(buf)
        except EOFError:
            break
        chunk = buf.read(sz)
        if eid == TRACKENTRY:
            out.append(_parse_track_entry(chunk))
    return out


def _parse_track_entry(data: bytes) -> dict:
    from io import BytesIO

    buf = BytesIO(data)
    info = {"type": 0, "track_number": 0, "codec_id": "", "language": "", "name": ""}
    while True:
        try:
            eid, sz = _read_header(buf)
        except EOFError:
            break
        v = buf.read(sz)
        if eid == TRACKTYPE:
            info["type"] = v[0]
        elif eid == TRACKNUMBER:
            info["track_number"] = int.from_bytes(v, "big")
        elif eid == CODECID:
            info["codec_id"] = v.decode("utf-8", "ignore")
        elif eid == LANG_BCP47:
            info["language"] = v.decode("utf-8", "ignore")
        elif eid == LANG and not info["language"]:
            info["language"] = v.decode("utf-8", "ignore")
        elif eid == TRACKNAME:
            info["name"] = v.decode("utf-8", "ignore")
    return info


def extract_subtitles(
    path: str,
    track_number: int,
    on_progress: Optional[Callable[[float], None]] = None,
) -> list[tuple[int, str] | tuple[int, str, int]]:
    subs = []
    try:
        with (
            open(path, "rb") as file,
            mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as f,
        ):
            return _extract_subtitles_from(f, track_number, on_progress)
    except (ValueError, OSError):
        with open(path, "rb") as f:
            return _extract_subtitles_from(f, track_number, on_progress)
    return subs


def _extract_subtitles_from(
    f: BinaryIO,
    track_number: int,
    on_progress: Optional[Callable[[float], None]] = None,
) -> list[tuple[int, str] | tuple[int, str, int]]:
    subs = []
    _, h = _read_header(f)
    f.seek(h, 1)
    eid, h = _read_header(f)
    if eid != SEGMENT:
        return subs
    segment_start = f.tell()
    segment_end = segment_start + h
    last_percent = -1
    while f.tell() < segment_end:
        eid, sz = _read_header(f)
        if eid == CLUSTER:
            subs += _parse_cluster(f, sz, track_number)
        else:
            f.seek(sz, 1)
        if on_progress:
            pos = f.tell() - segment_start
            percent = int(100 * pos / (segment_end - segment_start))
            if percent != last_percent and percent % 1 == 0:
                last_percent = percent
                on_progress(percent / 100)
    return subs


def _parse_cluster(
    f: BinaryIO, size: int, track_number: int
) -> list[tuple[int, str] | tuple[int, str, int]]:
    end = f.tell() + size
    base = _read_cluster_time(f, end)
    out = []
    while f.tell() < end:
        eid, sz = _read_header(f)
        if eid == SIMPLEBLK:
            out += _handle_block(f, sz, base, track_number)
        elif eid == BLKGROUP:
            out += _handle_group(f, sz, base, track_number)
        else:
            f.seek(sz, 1)
    return out


def _read_cluster_time(f: BinaryIO, end: int) -> int:
    while f.tell() < end:
        eid, sz = _read_header(f)
        if eid == TIMECODE:
            return int.from_bytes(f.read(sz), "big")
        f.seek(sz, 1)
    return 0


def _handle_block(
    f: BinaryIO, sz: int, base: int, track_number: int
) -> list[tuple[int, str]]:
    trk, vlen = read_vint(f)
    t = int.from_bytes(f.read(2), "big", signed=True)
    f.read(1)
    plen = sz - vlen - 3
    if trk == track_number:
        txt = f.read(plen).decode("utf-8", "ignore").strip()
        return [(base + t, txt)]
    f.seek(plen, 1)
    return []


def _handle_group(
    f: BinaryIO, sz: int, base: int, track_number: int
) -> list[tuple[int, str] | tuple[int, str, int]]:
    end = f.tell() + sz
    txt = None
    t_rel = None
    duration = None
    while f.tell() < end:
        eid, s = _read_header(f)
        if eid == BLOCK:
            pos = f.tell()
            trk, vlen = read_vint(f)
            t_rel = int.from_bytes(f.read(2), "big", signed=True)
            f.read(1)
            plen = s - vlen - 3
            if trk == track_number:
                txt = f.read(plen).decode("utf-8", "ignore").strip()
            else:
                f.seek(pos + s)
        elif eid == BLOCKDUR:
            duration = int.from_bytes(f.read(s), "big")
        else:
            f.seek(s, 1)
    if txt and t_rel is not None:
        start = base + t_rel
        if duration:
            return [(start, txt, start + duration)]
        return [(start, txt)]
    return []


def format_timestamp(ms: int) -> str:
    h = ms // 3600000
    m = (ms % 3600000) // 60000
    s = (ms % 60000) // 1000
    ms = ms % 1000
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def extract_subtitles_as_srt(
    path: str, track_number: int, on_progress: Optional[Callable[[float], None]] = None
) -> str:
    entries = extract_subtitles(path, track_number, on_progress)
    result = []
    DEFAULT_DURATION = 3000
    for i, entry in enumerate(entries):
        if len(entry) == 2:
            start, text = entry
            next_start = (
                entries[i + 1][0] if i + 1 < len(entries) else start + DEFAULT_DURATION
            )
            end = next_start if next_start > start else start + DEFAULT_DURATION
        else:
            start, text, end = entry
        result.append(
            f"{i + 1}\n{format_timestamp(start)} --> {format_timestamp(end)}\n{text.strip()}\n\n"
        )
    return "".join(result)


def get_output_srt_path(input_path: Path, index: int, total: int, lang: str) -> Path:
    if total == 1:
        return input_path.with_suffix(".srt")
    lang_tag = f".{lang}" if lang else ""
    return input_path.with_suffix(f".{index}{lang_tag}.srt")


def main() -> None:
    if len(sys.argv) < 2:
        sys.stderr.write("Usage: subtitle_extract <input_file>\n")
        sys.exit(1)

    input_path = Path(sys.argv[1])
    tracks = extract_subtitle_tracks(str(input_path))

    if not tracks:
        print("No subtitle tracks found.")
        return

    for i, t in enumerate(tracks, 1):
        track_num = t["track_number"]
        lang = t.get("language", "und") or "und"
        print(f"Found track#{track_num} ({t['codec_id']} [{lang}])")

        srt_text = extract_subtitles_as_srt(
            str(input_path), track_num, on_progress=progress_bar
        )
        print()

        out_path = get_output_srt_path(input_path, i, len(tracks), lang)
        atomic_write(out_path, srt_text, encoding="utf-8")
        print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
