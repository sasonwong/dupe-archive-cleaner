#!/usr/bin/env python3
"""
dupe-archive-cleaner — find zip/rar/7z archives that have already been extracted.

Requires: rarfile, py7zr  (zipfile is stdlib)

Usage:
  dupe-archive-cleaner.py --scan [PATH]    Scan directory (default: .)
  dupe-archive-cleaner.py --scan PATH -o REPORT  Save report to custom path
  dupe-archive-cleaner.py --read REPORT    Read and display existing report
  dupe-archive-cleaner.py -q ...           Suppress progress on stderr
  dupe-archive-cleaner.py --help
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
import zipfile
from typing import Any

VERSION = 1
DEFAULT_REPORT = "duplicate-archive-report.json"
CHUNK_SIZE = 65536

try:
    import rarfile
except ImportError:
    rarfile = None  # type: ignore[assignment]

try:
    import py7zr
except ImportError:
    py7zr = None  # type: ignore[assignment]


# ── Progress (stderr) ──────────────────────────────────────────────

class Progress:
    def __init__(self, quiet: bool = False) -> None:
        self.quiet = quiet
        self.total = 0
        self.current = 0
        self.matched = 0
        self.skipped = 0
        self.start_time = time.time()

    def _w(self, text: str, end: str = "\n") -> None:
        if not self.quiet:
            print(text, file=sys.stderr, end=end, flush=True)

    def header(self, path: str) -> None:
        self._w(f"Scanning: {os.path.abspath(path)}")

    def found(self, name: str) -> None:
        self._w(f"  found {name}")

    def none_found(self) -> None:
        self._w("  (no archives found)")

    def match_ok(self, archive: str, mode: str) -> None:
        self.matched += 1
        self._w(f"  [{self.current}/{self.total}] {Path(archive).name} ... CRC matched ({mode})")

    def match_fail(self, archive: str, reason: str) -> None:
        self._w(f"  [{self.current}/{self.total}] {Path(archive).name} ... not matched ({reason})")

    def skip(self, archive: str, reason: str) -> None:
        self.skipped += 1
        self._w(f"  [{self.current}/{self.total}] {Path(archive).name} ... skipped ({reason})")

    def summary(self, report_path: str) -> None:
        elapsed = time.time() - self.start_time
        unmatched = self.total - self.matched - self.skipped
        self._w(
            f"Done. {self.matched}/{self.total} archives matched "
            f"({unmatched} unmatched, {self.skipped} skipped)"
            f"  \u2192 {report_path}  [{elapsed:.1f}s]"
        )

    def reading(self, path: str) -> None:
        self._w(f"Reading report: {os.path.abspath(path)}")

    def report_info(self, matched: int, total: int, report_file: str) -> None:
        self._w(f"Report: {matched}/{total} archives matched  \u2192 {report_file}")


# ── CRC32 helper ───────────────────────────────────────────────────

def crc32_of_file(path: str | os.PathLike) -> int:
    crc = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            crc = zlib.crc32(chunk, crc)
    return crc & 0xFFFFFFFF


# ── Archive readers (return list[(rel_path, crc32)] or None) ────────

def _read_zip(path: str) -> list[tuple[str, int]] | None:
    try:
        with zipfile.ZipFile(path, "r") as zf:
            out: list[tuple[str, int]] = []
            for info in zf.infolist():
                if info.is_dir():
                    continue
                out.append((info.filename, info.CRC))
            return out
    except Exception:
        return None


def _read_rar(path: str) -> list[tuple[str, int]] | None:
    if rarfile is None:
        return None
    try:
        with rarfile.RarFile(path, "r") as rf:
            out = []
            for info in rf.infolist():
                if info.is_dir():
                    continue
                if info.CRC is None:
                    return None
                out.append((info.filename, info.CRC))
            return out
    except Exception:
        return None


def _read_7z(path: str) -> list[tuple[str, int]] | None:
    if py7zr is None:
        return None
    try:
        with py7zr.SevenZipFile(path, "r") as szf:
            out = []
            for name in szf.getnames():
                entry = szf.getinfo(name)
                if entry.is_directory:
                    continue
                if entry.crc32 is None:
                    return None
                out.append((name, entry.crc32))
            return out
    except Exception:
        return None


ARCHIVE_READERS: dict[str, tuple[str, Any]] = {
    ".zip": ("zipfile", _read_zip),
    ".rar": ("rarfile", _read_rar),
    ".7z": ("py7zr", _read_7z),
}

LIB_HINT: dict[str, str] = {
    ".rar": "rarfile (pip install rarfile or uv pip install rarfile)",
    ".7z": "py7zr (pip install py7zr or uv pip install py7zr)",
}


# ── Matching ───────────────────────────────────────────────────────

def _try_match(
    archive_path: str,
    files: list[tuple[str, int]],
    base_dir: str,
    mode: str,
) -> list[str] | None:
    if mode == "subdir":
        base_dir = os.path.join(base_dir, Path(archive_path).stem)

    matched: list[str] = []
    for rel_path, expected_crc in files:
        disk_path = os.path.join(base_dir, rel_path)
        if not os.path.isfile(disk_path):
            return None
        try:
            actual_crc = crc32_of_file(disk_path)
        except (OSError, PermissionError):
            return None
        if actual_crc != expected_crc:
            return None
        matched.append(disk_path)
    return matched


def match_archive(
    archive_path: str,
    progress: Progress | None = None,
) -> dict[str, Any] | None:
    ext = Path(archive_path).suffix.lower()
    entry = ARCHIVE_READERS.get(ext)
    if entry is None:
        if progress:
            progress.skip(archive_path, "unsupported format")
        return None

    lib_name, reader_fn = entry
    files = reader_fn(archive_path)

    if files is None:
        if progress:
            hint = LIB_HINT.get(ext)
            if hint:
                progress.skip(archive_path, f"requires {hint}")
            else:
                progress.skip(archive_path, "corrupted or encrypted")
        return None

    if not files:
        if progress:
            progress.skip(archive_path, "empty archive")
        return None

    base_dir = os.path.dirname(archive_path)

    matched_paths = _try_match(archive_path, files, base_dir, "direct")
    mode = "direct"
    if matched_paths is None:
        matched_paths = _try_match(archive_path, files, base_dir, "subdir")
        mode = "subdir"

    if matched_paths is None:
        first_rel = files[0][0]
        stem = Path(archive_path).stem
        dc = os.path.join(base_dir, first_rel)
        sc = os.path.join(base_dir, stem, first_rel)
        if os.path.isfile(dc) or os.path.isfile(sc):
            reason = "CRC mismatch"
        else:
            reason = "missing files on disk"
        if progress:
            progress.match_fail(archive_path, reason)
        return None

    mtime_epoch = os.path.getmtime(archive_path)
    mtime_iso = datetime.fromtimestamp(mtime_epoch, tz=timezone.utc).isoformat()

    extracted_total_size = 0
    for p in matched_paths:
        try:
            extracted_total_size += os.path.getsize(p)
        except OSError:
            pass

    if progress:
        progress.match_ok(archive_path, mode)

    return {
        "archive": os.path.abspath(archive_path),
        "archive_size": os.path.getsize(archive_path),
        "archive_modified": mtime_iso,
        "match_mode": mode,
        "total_files": len(files),
        "extracted_paths": matched_paths,
        "extracted_total_size": extracted_total_size,
    }


def find_archives(root: str) -> list[str]:
    root_path = Path(root).resolve()
    out: list[str] = []
    for entry in root_path.rglob("*"):
        if entry.is_file() and entry.suffix.lower() in ARCHIVE_READERS:
            out.append(str(entry))
    return sorted(out)


# ── Commands ──────────────────────────────────────────────────────

def cmd_scan(scan_path: str, output: str | None, quiet: bool) -> None:
    progress = Progress(quiet=quiet)
    progress.header(scan_path)

    archives = find_archives(scan_path)
    progress.total = len(archives)

    if not archives:
        progress.none_found()
    else:
        progress.found(f"{len(archives)} archive(s)")

    matches: list[dict[str, Any]] = []
    for ap in archives:
        progress.current += 1
        result = match_archive(ap, progress)
        if result is not None:
            matches.append(result)

    report_path = os.path.abspath(output or DEFAULT_REPORT)

    result = {
        "version": VERSION,
        "args": {
            "mode": "scan",
            "scan_path": os.path.abspath(scan_path),
            "report_file": report_path,
        },
        "summary": {
            "total_archives": len(archives),
            "matched_archives": len(matches),
            "total_archive_size": sum(m["archive_size"] for m in matches),
            "total_extracted_size": sum(m["extracted_total_size"] for m in matches),
            "report_file": report_path,
        },
        "matches": matches,
    }

    with open(report_path, "w") as f:
        json.dump(result, f, indent=2)

    print(json.dumps(result, indent=2))

    progress.summary(report_path)


def cmd_read(report_path: str, quiet: bool) -> None:
    progress = Progress(quiet=quiet)
    progress.reading(report_path)

    with open(report_path, "r") as f:
        data = json.load(f)

    print(json.dumps(data, indent=2))

    summary = data.get("summary", {})
    total = summary.get("total_archives", "?")
    matched = summary.get("matched_archives", "?")
    rfile = summary.get("report_file", report_path)
    progress.report_info(matched, total, rfile)


# ── CLI ────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dupe-archive-cleaner",
        description=(
            "Find zip/rar/7z archives whose contents have already been extracted "
            "to disk (100% filename + CRC32 match)."
        ),
    )
    parser.add_argument("--scan", metavar="PATH", nargs="?", const=".", default=None,
                        help="Scan a directory (default: current directory)")
    parser.add_argument("--read", metavar="REPORT", default=None,
                        help="Read and display an existing report")
    parser.add_argument("-o", "--output", default=None,
                        help=f"Report output path (default: {DEFAULT_REPORT})")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="Suppress progress output on stderr")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.scan is not None and args.read is not None:
        print("error: --scan and --read are mutually exclusive", file=sys.stderr)
        sys.exit(1)

    if args.scan is not None:
        cmd_scan(args.scan, args.output, args.quiet)
    elif args.read is not None:
        cmd_read(args.read, args.quiet)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
