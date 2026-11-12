#!/data/data/com.termux/files/usr/bin/env python
"""Convert an ``.m4a`` audio file to MP3 with ffmpeg.
Transcodes the input with ``libmp3lame`` at 64 kbit/s (video streams dropped), writes ``<name>.mp3`` next to it and prints the input/output sizes and their ratio.
Usage: m4a2mp3 SONG.m4a Examples: $ m4a2mp3 podcast.m4a Runs as a script (``python m4a2mp3.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
import subprocess
import sys

from dh import runcmd


def convert_m4a_to_mp3(input_file: Path, bitrate="64k") -> None:
    if not Path(input_file).exists():
        print(f"Error: Input file '{input_file}' not found.")
        sys.exit(1)
    if not input_file.lower().endswith(".m4a"):
        print("Warning: Input file doesn't have .m4a extension. Proceeding anyway...")
    input_path = Path(input_file)
    output_file = str(input_path.with_suffix(".mp3"))
    print(f"Converting: {input_file}")
    print(f"Output: {output_file}")
    print(f"Bitrate: {bitrate}")
    cmd = [
        "ffmpeg",
        "-i",
        input_file,
        "-codec:a",
        "libmp3lame",
        "-b:a",
        bitrate,
        "-vn",
        "-y",
        output_file,
    ]
    try:
        ret, _txt, _err = runcmd(cmd, show_output=True)
        if not ret:
            print(f"Successfully converted to: {output_file}")
        input_size = Path(input_file).stat().st_size / (1024 * 1024)
        output_size = Path(output_file).stat().st_size / (1024 * 1024)
        print(f"Input size: {input_size:.2f} MB")
        print(f"Output size: {output_size:.2f} MB")
        print(f"Size ratio: {output_size / input_size:.1%}")
    except subprocess.CalledProcessError as e:
        print("Error during conversion:")
        print(e.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print("Error: ffmpeg not found. Please install ffmpeg first.")
        print("Ubuntu/Debian: sudo apt install ffmpeg")
        print("macOS: brew install ffmpeg")
        print("Windows: Download from https://ffmpeg.org/download.html")
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python script.py <input_file.m4a>")
        print("Example: python script.py song.m4a")
        sys.exit(1)
    input_file = sys.argv[1]
    convert_m4a_to_mp3(input_file, bitrate="64k")
