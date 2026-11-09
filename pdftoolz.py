#!/data/data/com.termux/files/usr/bin/env python
"""PDF toolkit with the same subcommands as :doc:`pdftools2`.
``outline`` resolves the mdBook ``SUMMARY.md`` anchors against the HTML ids and builds the TOC, ``merge``/``to-img``/``rename-pages``/``shrink``/``split-size``/``split-page`` cover the rest; the implementation uses PyMuPDF for the outline and the Ghostscript/qpdf engines for the compression.
Usage: pdftoolz {outline,merge,to-img,rename-pages,shrink,split-size,split-page} [options] Positional arguments: inputs ...
Subcommands: outline Add outline to a PDF using a mdBook SUMMARY.md.
merge Merge PDF files.
to-img Convert PDFs to JPG images.
rename-pages Rename PDFs appending their page count.
shrink Shrink PDF files with Ghostscript or qpdf.
split-size Split PDFs into chunks no larger than --max-mb.
split-page Split PDFs into one file per page.
Options: --html_path --pdf_path --summary_path --output_path --check-titles Base directory to check source titles against SUMMARY.md.
--sync-titles Overwrite mismatched titles when --check-titles is used.
-o, --output --directory --output-subdir --dpi ...
plus 4 more; run ``pdftoolz --help`` for the full list.
Examples: $ pdftoolz outline --summary_path SUMMARY.md --html_path docs --pdf_path book.pdf Command-line script: run ``python pdftoolz.py`` or the ``pdftoolz`` command.
The module is importable as well."""

import argparse
from concurrent.futures import ProcessPoolExecutor
import contextlib
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Iterable, Optional
import urllib.parse

from dh import MAX_WORKERS
import pymupdf


def human_size(n: float) -> str:
    sign = "-" if n < 0 else ""
    n = abs(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{sign}{n:.2f} {unit}"
        n /= 1024
    return f"{sign}{n:.2f} PB"


def run_cmd(cmd: list[str], show_output: bool = True) -> subprocess.CompletedProcess:
    if show_output:
        print(" ".join(str(c) for c in cmd))
    return subprocess.run(cmd, capture_output=not show_output, text=True)


def collect_pdfs(inputs: Iterable[str]) -> list[Path]:
    out: list[Path] = []
    for x in inputs:
        p = Path(x)
        if p.is_file() and p.suffix.lower() == ".pdf":
            out.append(p)
        elif p.is_dir():
            out.extend(p.rglob("*.pdf"))
    return out


def total_size(path: Path) -> int:
    p = Path(path)
    if p.is_file():
        return p.stat().st_size
    total = 0
    for f in p.rglob("*"):
        if f.is_file():
            with contextlib.suppress(OSError):
                total += f.stat().st_size
    return total


def find_ghostscript() -> Optional[str]:
    for name in ("gs", "gswin64c"):
        if shutil.which(name):
            return name
    return None


class TreeNode:
    def __init__(self, title: str, source_file: str, depth: int, index: int) -> None:
        self.title = title
        self.source_file = source_file
        self.depth = depth
        self.index = index
        self.parent: Optional["TreeNode"] = None
        self.children: list["TreeNode"] = []
        self.page: int = 0

    def is_root(self) -> bool:
        return self.parent is None

    def path_to_root(self) -> list[str]:
        parts: list[str] = []
        node: TreeNode = self
        while not node.is_root():
            parts.append(str(node.index + 1))
            node = node.parent
        return parts[::-1]

    def __str__(self) -> str:
        return "{}. {}".format(".".join(self.path_to_root()), self.title)


_SUMMARY_RE = re.compile(
    r"^(?P<indent> *)- \[(?P<title>[^\]]+)\]\((?P<src>[^)]+)\)",
    re.MULTILINE,
)


class _IdCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs) -> None:
        for k, v in attrs:
            if k == "id" and v:
                self.ids.add(v)

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)


def _collect_html_ids(html: str) -> set[str]:
    collector = _IdCollector()
    collector.feed(html)
    return collector.ids


def _parse_summary(text: str) -> TreeNode:
    root = TreeNode("root", "", 0, 0)
    levels: dict[int, list[TreeNode]] = {0: [root]}
    min_indent = 4
    for m in _SUMMARY_RE.finditer(text):
        indent = len(m.group("indent"))
        if 0 < indent < min_indent:
            min_indent = indent
        depth = indent // min_indent
        node = TreeNode(m.group("title"), m.group("src"), depth + 1, 0)
        levels.setdefault(depth + 1, []).append(node)
        parent = levels[depth][-1]
        node.parent = parent
        node.index = len(parent.children)
        parent.children.append(node)
    return root


def _outline_id(node: TreeNode) -> str:
    n = node.source_file.removeprefix("./").split(".")[0].lower()
    return n.replace("/", "-").replace(" ", "-")


def _check_titles(base_dir: Path, node: TreeNode, sync: bool) -> bool:
    for child in node.children:
        if not _check_titles(base_dir, child, sync):
            return False
    if node.is_root():
        return True
    file_path = base_dir / node.source_file
    if not file_path.exists():
        print(f"File {file_path} does not exist")
        return False
    lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
    matched = False
    for lineno, line in enumerate(lines):
        if line.startswith("# "):
            title = line[2:]
            if not title.startswith(node.title):
                print(
                    f"[ERROR] Title not matched: source_file:{file_path}, "
                    f"line num:{lineno}, title:{title!r}, "
                    f"title in `SUMMARY.md`:{node.title!r}"
                )
                break
            matched = True
            break
    if not matched and sync:
        lines.insert(0, f"# {node.title}\n")
        print(f"[Info] Overwrite title as {node.title} in {node.source_file}")
        file_path.write_text("".join(lines), encoding="utf-8")
        matched = True
    return matched


def _resolve_named_dests(doc: pymupdf.Document) -> dict:
    result: dict = {}
    try:
        names = doc.resolve_names()
        if isinstance(names, dict):
            for name, dest in names.items():
                pno: Optional[int] = None
                if isinstance(dest, (list, tuple)) and dest:
                    first = dest[0]
                    if isinstance(first, int):
                        pno = first
                elif isinstance(dest, dict) and "page" in dest:
                    try:
                        pno = int(dest["page"])
                    except (TypeError, ValueError):
                        pno = None
                if pno is not None:
                    result[str(name)] = pno
    except Exception:
        pass
    for pno in range(len(doc)):
        try:
            page = doc[pno]
            links = page.get_links()
        except Exception:
            continue
        for link in links:
            nd = link.get("nameddest")
            if nd and nd not in result:
                result[nd] = pno
    return result


def _lookup_dest(dest_map: dict, oid: str) -> Optional[int]:
    quoted = urllib.parse.quote(oid)
    for candidate in (oid, "/" + oid, quoted, "/" + quoted):
        if candidate in dest_map:
            return int(dest_map[candidate])
    return None


def _build_toc(
    html_ids: set[str],
    dest_map: dict,
    root: TreeNode,
) -> list[list]:
    toc: list[list] = []

    def walk(n: TreeNode) -> None:
        if not n.is_root():
            oid = _outline_id(n)
            if oid not in html_ids:
                print(f"[ERROR] Element not found: [{oid}]")
            else:
                pno = _lookup_dest(dest_map, oid)
                if pno is None:
                    print(f"[ERROR] Named destination not found: [{oid}]")
                else:
                    n.page = pno + 1
                    toc.append([n.depth, str(n), n.page])
        for c in n.children:
            walk(c)

    walk(root)
    return toc


def cmd_outline(args: argparse.Namespace) -> int:
    html_path = Path(args.html_path)
    pdf_path = Path(args.pdf_path)
    summary_path = Path(args.summary_path)
    output_path = Path(args.output_path)
    for p in (html_path, pdf_path, summary_path):
        if not p.exists():
            raise FileNotFoundError(str(p))
    tree = _parse_summary(summary_path.read_text(encoding="utf-8"))
    if args.check_titles:
        _check_titles(Path(args.check_titles), tree, args.sync_titles)
    html_ids = _collect_html_ids(html_path.read_text(encoding="utf-8"))
    doc = pymupdf.open(str(pdf_path))
    dest_map = _resolve_named_dests(doc)
    toc = _build_toc(html_ids, dest_map, tree)
    if toc:
        doc.set_toc(toc)
    doc.save(str(output_path))
    doc.close()
    print(f"[INFO] Write to {output_path}")
    return 0


_NUM_SUFFIX_RE = re.compile(r"_(\d+)\.pdf$", re.IGNORECASE)


def _merge_sort_key(name: str):
    m = _NUM_SUFFIX_RE.search(name)
    if m:
        return (int(m.group(1)),)
    return (float("inf"),)


def cmd_merge(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    if args.inputs:
        files: list[Path] = []
        for raw in args.inputs:
            p = Path(raw)
            if p.is_file() and p.suffix.lower() == ".pdf":
                files.append(p)
            elif p.is_dir():
                files.extend(p.glob("*.pdf"))
        files = sorted(files, key=lambda p: _merge_sort_key(p.name))
    else:
        files = sorted(cwd.glob("*.pdf"), key=lambda p: _merge_sort_key(p.name))
    if not files:
        print("No PDF files found.")
        return 1
    out_doc = pymupdf.open()
    for f in files:
        with pymupdf.open(str(f)) as src:
            out_doc.insert_pdf(src)
    out_path = cwd / args.output
    out_doc.save(str(out_path))
    out_doc.close()
    print(f"Merged {len(files)} files into: {out_path}")
    return 0


def _convert_one_pdf(
    pdf_path: Path,
    out_root: Path,
    dpi: int,
) -> bool:
    target = out_root / pdf_path.stem
    try:
        print(f"Converting '{pdf_path.name}'...")
        target.mkdir(parents=True, exist_ok=True)
        doc = pymupdf.open(str(pdf_path))
        zoom = dpi / 72.0
        mat = pymupdf.Matrix(zoom, zoom)
        count = 0
        for i in range(len(doc)):
            page = doc[i]
            pix = page.get_pixmap(matrix=mat)
            out = target / f"{pdf_path.stem}_page_{i + 1}.jpg"
            pix.save(str(out))
            count += 1
        doc.close()
        print(f"Successfully converted '{pdf_path.name}' to {count} JPG files in '{target}'.")
        return True
    except Exception as e:
        print(f"Error converting '{pdf_path.name}': {e}")
        if target.exists():
            try:
                shutil.rmtree(target)
            except Exception as cleanup_e:
                print(f"Error during cleanup of '{target}': {cleanup_e}")
        return False


def cmd_to_img(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    out_root = root / args.output_subdir
    out_root.mkdir(exist_ok=True)
    print(f"Starting PDF to JPG conversion in directory: {root}")
    print(f"Output will be saved in: {out_root}")
    succeeded = 0
    failed = 0
    for pdf in root.rglob("*"):
        if not (pdf.is_file() and pdf.suffix.lower() == ".pdf"):
            continue
        if out_root in pdf.parents:
            print(f"Skipping PDF '{pdf.name}' as it's within the output directory.")
            continue
        if _convert_one_pdf(pdf, out_root, args.dpi):
            if not args.keep:
                try:
                    pdf.unlink()
                    print(f"Removed original PDF: '{pdf.name}'")
                except OSError as e:
                    print(f"Error removing original PDF '{pdf.name}': {e}")
                    failed += 1
                    continue
            succeeded += 1
        else:
            failed += 1
    print("\n---Conversion Summary---")
    print(f"Successfully converted and removed: {succeeded} PDF files.")
    print(f"Failed to convert: {failed} PDF files.")
    print("------------------------")
    return 0 if failed == 0 else 1


def _rename_one(path_str: str) -> None:
    path = Path(path_str)
    if not path.exists() or path.is_symlink():
        return
    with pymupdf.open(str(path)) as doc:
        count = len(doc)
    new_name = f"{path.stem}{count}.pdf"
    print(new_name)
    new_path = path.parent / new_name
    if str(count) in path.stem:
        return
    if not new_path.exists():
        path.rename(new_path)
        print(f"{path.name} --> {new_path.name}")
    else:
        print(f"{new_path.name} exists.")


def cmd_rename_pages(args: argparse.Namespace) -> int:
    root = Path.cwd()
    files = [str(p) for p in root.rglob("*") if p.is_file() and p.suffix == ".pdf"]
    if not files:
        print("No PDF files found.")
        return 0
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for _ in ex.map(_rename_one, files):
            pass
    return 0


_GS_BASIC: list[str] = [
    "-sDEVICE=pdfwrite",
    "-dCompatibilityLevel=1.4",
    "-dDownsampleColorImages=true",
    "-dDownsampleGrayImages=true",
    "-dDownsampleMonoImages=true",
    "-dColorImageResolution=65",
    "-dGrayImageResolution=65",
    "-dMonoImageResolution=65",
    "-dColorImageDownsampleType=/Bicubic",
    "-dGrayImageDownsampleType=/Bicubic",
    "-dMonoImageDownsampleType=/Subsample",
    "-dNOPAUSE",
    "-dBATCH",
]
_GS_SCREEN: list[str] = [
    "-dBATCH",
    "-dColorConversionStrategy=/sRGB",
    "-dColorImageDownsampleType=/Bicubic",
    "-dColorImageResolution=85",
    "-dCompatibilityLevel=1.4",
    "-dDownsampleColorImages=true",
    "-dDownsampleGrayImages=true",
    "-dDownsampleMonoImages=true",
    "-dEmbedAllFonts=false",
    "-dGrayImageDownsampleType=/Bicubic",
    "-dGrayImageResolution=85",
    "-dHaveTransparency=false",
    "-dMonoImageDownsampleType=/Bicubic",
    "-dMonoImageResolution=85",
    "-dNOPAUSE",
    "-dOptimize=true",
    "-dPDFSETTINGS=/screen",
    "-dSubsetFonts=true",
    "-sDEVICE=pdfwrite",
]
_GS_MOBILE: list[str] = [
    "-sDEVICE=pdfwrite",
    "-dCompatibilityLevel=1.4",
    "-dPDFSETTINGS=/screen",
    "-dFastWebView=true",
    "-dNOPAUSE",
    "-dQUIET",
    "-dBATCH",
]


def _shrink_gs_basic(pdf: Path) -> None:
    gs = find_ghostscript()
    if not gs:
        print("[ERROR] Ghostscript not found in PATH.")
        return
    tmp = pdf.with_name(f"temp_gs_{pdf.name}")
    before = pdf.stat().st_size
    print(f"{pdf.name} Before : {human_size(before)}")
    cmd = [gs, *_GS_BASIC, f"-sOutputFile={tmp}", str(pdf)]
    run_cmd(cmd, show_output=False)
    if tmp.exists() and tmp.stat().st_size:
        after = tmp.stat().st_size
        print(f"{pdf.name} After  : {human_size(after)}")
        if after < before:
            tmp.replace(pdf)
            print(f"Saved  : {human_size(before - after)}")
        else:
            print("original file is smaller")
            tmp.unlink(missing_ok=True)


def _shrink_gs_screen(pdf: Path) -> None:
    gs = find_ghostscript()
    if not gs:
        print("[ERROR] Ghostscript not found in PATH.")
        return
    tmp = pdf.with_name(f"temp_gs_{pdf.name}")
    before = pdf.stat().st_size
    print(f"{pdf.name} Before : {human_size(before)}")
    cmd = [gs, *_GS_SCREEN, f"-sOutputFile={tmp}", str(pdf)]
    run_cmd(cmd, show_output=True)
    if tmp.exists() and tmp.stat().st_size:
        after = tmp.stat().st_size
        print(f"{pdf.name} After  : {human_size(after)}")
        if after < before:
            tmp.replace(pdf)
            print(f"Saved  : {human_size(before - after)}")
        else:
            print("original file is smaller")
            tmp.unlink(missing_ok=True)


def _shrink_gs_mobile(pdf: Path) -> None:
    gs = find_ghostscript()
    if not gs:
        print("[ERROR] Ghostscript ('gs' or 'gswin64c') is not installed or not in your PATH.")
        return
    before = pdf.stat().st_size
    print(f"Original size: {human_size(before)}")
    print("Compressing for mobile viewing... (72 DPI + Linearization)")
    tmp = pdf.with_suffix(pdf.suffix + ".tmp")
    cmd = [gs, *_GS_MOBILE, f"-sOutputFile={tmp}", str(pdf)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"Ghostscript Error:\n{r.stderr}")
            if tmp.exists():
                tmp.unlink()
            return
        after = tmp.stat().st_size
        if after < before:
            tmp.replace(pdf)
            print("Success! Inplace update complete.")
            print(f"New mobile-optimized size: {human_size(after)}")
            print(f"Saved: {(before - after) / before * 100:.1f}% space")
        else:
            print("Compression did not reduce file size. Original file kept intact.")
            if tmp.exists():
                tmp.unlink()
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        if tmp.exists():
            tmp.unlink()


def _shrink_qpdf(pdf: Path) -> None:
    if not shutil.which("qpdf"):
        print("[ERROR] qpdf not found in PATH.")
        return
    if not pdf.exists():
        print("Input file not found.", file=sys.stderr)
        return
    tmp = pdf.with_name(f"temp_qpdf_{pdf.name}")
    before = pdf.stat().st_size
    print(f"{pdf.name} Before : {human_size(before)}")
    cmd = ["qpdf", "--linearize", "--object-streams=generate", str(pdf), str(tmp)]
    run_cmd(cmd, show_output=True)
    if tmp.exists():
        after = tmp.stat().st_size
        print(f"{pdf.name} After  : {human_size(after)}")
        if after < before:
            tmp.replace(pdf)
            print(f"Saved  : {human_size(before - after)}")
        else:
            print("original file is smaller")
            tmp.unlink(missing_ok=True)


_SHRINK_ENGINES = {
    "gs-basic": _shrink_gs_basic,
    "gs-screen": _shrink_gs_screen,
    "gs-mobile": _shrink_gs_mobile,
    "qpdf": _shrink_qpdf,
}


def _shrink_dispatch(path_str: str, engine: str) -> None:
    _SHRINK_ENGINES[engine](Path(path_str))


def cmd_shrink(args: argparse.Namespace) -> int:
    files = collect_pdfs(args.inputs) if args.inputs else collect_pdfs([str(Path.cwd())])
    if not files:
        print("No PDF files found.")
        return 1
    fn = _SHRINK_ENGINES[args.engine]
    if len(files) == 1 or MAX_WORKERS <= 1:
        before = total_size(Path.cwd())
        for f in files:
            fn(f)
        if len(files) > 1:
            freed = before - total_size(Path.cwd())
            if freed > 0:
                print(f"space freed: {human_size(freed)}")
        return 0
    before = total_size(Path.cwd())
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as ex:
        list(
            ex.map(
                _shrink_dispatch,
                [str(f) for f in files],
                [args.engine] * len(files),
            )
        )
    freed = before - total_size(Path.cwd())
    if freed > 0:
        print(f"space freed: {human_size(freed)}")
    return 0


def _split_by_size(input_path: Path, output_dir: Path, max_mb: int) -> None:
    max_bytes = max_mb * 1024 * 1024
    src = pymupdf.open(str(input_path))
    stem = input_path.stem
    writer = pymupdf.open()
    part = 1
    for pno in range(len(src)):
        writer.insert_pdf(src, from_page=pno, to_page=pno)
        data = writer.tobytes()
        if len(data) > max_bytes and len(writer) > 1:
            writer.delete_page(len(writer) - 1)
            data = writer.tobytes()
            out = output_dir / f"{stem}_{part}.pdf"
            out.write_bytes(data)
            writer.close()
            writer = pymupdf.open()
            writer.insert_pdf(src, from_page=pno, to_page=pno)
            part += 1
    if len(writer) > 0:
        out = output_dir / f"{stem}_{part}.pdf"
        out.write_bytes(writer.tobytes())
    writer.close()
    src.close()


def cmd_split_size(args: argparse.Namespace) -> int:
    out_dir = Path(args.output).resolve()
    out_dir.mkdir(exist_ok=True)
    files = collect_pdfs(args.inputs) if args.inputs else list(Path.cwd().rglob("*.pdf"))
    if not files:
        print("No PDF files found.")
        return 1
    with ProcessPoolExecutor() as ex:
        futures = [ex.submit(_split_by_size, f, out_dir, args.max_mb) for f in files]
        for fut in futures:
            fut.result()
    print(f"Processing complete. Output files in: {out_dir}")
    return 0


def _split_by_page(input_path: Path, output_dir: Path) -> None:
    src = pymupdf.open(str(input_path))
    stem = input_path.stem
    total = len(src)
    pad = len(str(total))
    for idx in range(total):
        w = pymupdf.open()
        w.insert_pdf(src, from_page=idx, to_page=idx)
        name = f"{stem}_{str(idx + 1).zfill(pad)}.pdf"
        w.save(str(output_dir / name))
        w.close()
    src.close()


def cmd_split_page(args: argparse.Namespace) -> int:
    out_dir = Path(args.output).resolve()
    out_dir.mkdir(exist_ok=True)
    files = collect_pdfs(args.inputs) if args.inputs else list(Path.cwd().rglob("*.pdf"))
    if not files:
        print("No PDF files found.")
        return 1
    with ProcessPoolExecutor() as ex:
        futures = [ex.submit(_split_by_page, f, out_dir) for f in files]
        for fut in futures:
            fut.result()
    print(f"Processing complete. Output files in: {out_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="merged.py", description="Unified PDF toolkit.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("outline", help="Add outline to a PDF using a mdBook SUMMARY.md.")
    p.add_argument("--html_path", default="print.html")
    p.add_argument("--pdf_path", default="output.pdf")
    p.add_argument("--summary_path", default="src/SUMMARY.md")
    p.add_argument("--output_path", default="output_with_outline.pdf")
    p.add_argument(
        "--check-titles",
        default=None,
        help="Base directory to check source titles against SUMMARY.md.",
    )
    p.add_argument(
        "--sync-titles",
        action="store_true",
        help="Overwrite mismatched titles when --check-titles is used.",
    )
    p.set_defaults(func=cmd_outline)
    p = sub.add_parser("merge", help="Merge PDF files.")
    p.add_argument("-o", "--output", default="merged.pdf")
    p.add_argument("inputs", nargs="*")
    p.set_defaults(func=cmd_merge)
    p = sub.add_parser("to-img", help="Convert PDFs to JPG images.")
    p.add_argument("--directory", default=".")
    p.add_argument("--output-subdir", default="output_jpgs")
    p.add_argument("--dpi", type=int, default=300)
    p.add_argument(
        "--keep",
        action="store_true",
        help="Keep original PDFs after conversion (original deletes them).",
    )
    p.set_defaults(func=cmd_to_img)
    p = sub.add_parser("rename-pages", help="Rename PDFs appending their page count.")
    p.set_defaults(func=cmd_rename_pages)
    p = sub.add_parser("shrink", help="Shrink PDF files with Ghostscript or qpdf.")
    p.add_argument("--engine", choices=sorted(_SHRINK_ENGINES.keys()), required=True)
    p.add_argument("inputs", nargs="*")
    p.set_defaults(func=cmd_shrink)
    p = sub.add_parser("split-size", help="Split PDFs into chunks no larger than --max-mb.")
    p.add_argument("--max-mb", type=int, default=5)
    p.add_argument("--output", default="output")
    p.add_argument("inputs", nargs="*")
    p.set_defaults(func=cmd_split_size)
    p = sub.add_parser("split-page", help="Split PDFs into one file per page.")
    p.add_argument("--output", default="output")
    p.add_argument("inputs", nargs="*")
    p.set_defaults(func=cmd_split_page)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
