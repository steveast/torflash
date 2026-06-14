#!/usr/bin/env python3
"""TorFlash headless CLI — search rutor, manage library, download torrents.

No Qt; reuses pure-Python helpers from rutor_search.py.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rutor_search import (  # noqa: E402
    EXTRA_TRACKERS,
    HEADERS,
    _safe_join,
)
from providers import get_provider  # noqa: E402

LIBRARY_DIR = Path.home() / ".local" / "share" / "TorFlash"
TORRENTS_CACHE_DIR = LIBRARY_DIR / "torrents"
RESUME_DIR = LIBRARY_DIR / "resume"
LIBRARY_FILE = LIBRARY_DIR / "library.json"
STORAGE_DEFAULT = Path.home() / "Storage"

MAGNET_BTIH_RE = re.compile(r"btih:([a-fA-F0-9]{40})", re.I)


def human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


def load_library() -> dict:
    if LIBRARY_FILE.exists():
        try:
            return json.loads(LIBRARY_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_library(lib: dict) -> None:
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LIBRARY_FILE.with_name(LIBRARY_FILE.name + ".tmp")
    tmp.write_text(json.dumps(lib, ensure_ascii=False, indent=2))
    os.replace(tmp, LIBRARY_FILE)


def truncate(s: str, n: int) -> str:
    s = s.replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def magnet_hash(magnet: str) -> str:
    m = MAGNET_BTIH_RE.search(magnet or "")
    return m.group(1).lower() if m else ""


def print_table(headers: list[str], rows: list[list[str]]) -> None:
    if not rows:
        print("(no rows)")
        return
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if len(cell) > widths[i]:
                widths[i] = len(cell)
    fmt = "  ".join("{:<" + str(w) + "}" for w in widths)
    print(fmt.format(*headers))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(fmt.format(*row))


# ---------- search ----------

def cmd_search(args: argparse.Namespace) -> int:
    prov = get_provider("rutor")
    if prov is None:
        print("rutor provider unavailable", file=sys.stderr)
        return 1
    try:
        results = prov.search(args.query, 0)
    except requests.RequestException as e:
        print(f"No results. {e}", file=sys.stderr)
        return 1
    if not results:
        print("No results.", file=sys.stderr)
        return 1
    rows = [[it.get("date", ""), truncate(it.get("title", ""), 70),
             it.get("size", ""), it.get("seeds", "0"), it.get("leech", "0"),
             magnet_hash(it.get("magnet", ""))[:8]] for it in results]
    print_table(["Date", "Title", "Size", "S", "L", "Hash"], rows)
    return 0


# ---------- list ----------

def cmd_list(_: argparse.Namespace) -> int:
    lib = load_library()
    if not lib:
        print("Library is empty.")
        return 0
    rows = []
    for hid, meta in lib.items():
        try:
            size_h = human_bytes(int(meta.get("size") or 0)) if meta.get("size") else "—"
        except (TypeError, ValueError):
            size_h = "—"
        rows.append([hid[:8], truncate(meta.get("title", ""), 70), size_h,
                     "✓" if meta.get("completed_at") else "—",
                     meta.get("save_path", "")])
    print_table(["Hash", "Title", "Size", "Done", "Save path"], rows)
    return 0


# ---------- download ----------

def _make_session(lt):
    return lt.session({
        "listen_interfaces": "0.0.0.0:6881",
        "alert_mask": (
            lt.alert.category_t.error_notification
            | lt.alert.category_t.status_notification
            | lt.alert.category_t.storage_notification
        ),
        "enable_dht": True,
        "enable_lsd": False,
        "enable_upnp": False,
        "enable_natpmp": False,
        "announce_to_all_trackers": True,
        "announce_to_all_tiers": True,
        "enable_outgoing_utp": True,
        "enable_incoming_utp": True,
        "dht_bootstrap_nodes": (
            "router.bittorrent.com:6881,"
            "router.utorrent.com:6881,"
            "dht.transmissionbt.com:6881"
        ),
    })


def _add_params(lt, source: str, save_path: Path):
    if source.startswith("magnet:"):
        parsed = lt.parse_magnet_uri(source)
        parsed.save_path = str(save_path)
        for t in EXTRA_TRACKERS:
            parsed.trackers.append(t)
        return parsed
    r = requests.get(source, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return {
        "save_path": str(save_path),
        "trackers": list(EXTRA_TRACKERS),
        "ti": lt.torrent_info(lt.bdecode(r.content)),
    }


def _handle_hash(handle) -> str:
    try:
        ih = handle.info_hashes()
        v1 = str(ih.v1)
        return v1 if v1 != "0" * 40 else str(ih.v2)
    except AttributeError:
        return str(handle.info_hash())


def cmd_download(args: argparse.Namespace) -> int:
    import libtorrent as lt

    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    TORRENTS_CACHE_DIR.mkdir(exist_ok=True)
    RESUME_DIR.mkdir(exist_ok=True)
    STORAGE_DEFAULT.mkdir(parents=True, exist_ok=True)

    source = args.source.strip()
    ses = _make_session(lt)
    print(f"[lt] listening on {ses.listen_port()}", flush=True)

    params = _add_params(lt, source, STORAGE_DEFAULT)
    handle = ses.add_torrent(params)

    # Wait briefly for metadata if magnet
    if source.startswith("magnet:"):
        print("[lt] fetching metadata…", flush=True)
        while not handle.status().has_metadata:
            time.sleep(0.5)

    hid = _handle_hash(handle)
    tf = handle.torrent_file()
    title = tf.name() if tf else hid
    total = tf.total_size() if tf else 0
    print(f"[lt] {hid}  {title}  ({human_bytes(total)})", flush=True)

    # Persist .torrent
    try:
        if tf:
            ct = lt.create_torrent(tf)
            (TORRENTS_CACHE_DIR / f"{hid}.torrent").write_bytes(
                lt.bencode(ct.generate())
            )
    except Exception as e:  # noqa: BLE001
        print(f"[lt] warn: cannot persist .torrent: {e}", flush=True)

    # Add to library (don't overwrite)
    lib = load_library()
    if hid not in lib:
        lib[hid] = {
            "hash": hid,
            "title": title,
            "size": total,
            "magnet": source if source.startswith("magnet:") else "",
            "torrent_url": source if not source.startswith("magnet:") else "",
            "save_path": str(STORAGE_DEFAULT),
            "added_at": time.time(),
            "completed_at": None,
        }
        save_library(lib)

    if args.no_wait:
        print("[lt] --no-wait: detaching.", flush=True)
        return 0

    start = time.time()
    last_bytes = 0
    last_t = start
    try:
        while True:
            st = handle.status()
            now = time.time()
            dt = max(now - last_t, 1e-6)
            rate = (st.total_done - last_bytes) / dt
            elapsed = int(now - start)
            h, rem = divmod(elapsed, 3600)
            m, s = divmod(rem, 60)
            print(
                f"[progress] {h:d}:{m:02d}:{s:02d}  "
                f"{human_bytes(st.total_done)}/{human_bytes(st.total_wanted or total or 1)}  "
                f"↓ {human_bytes(max(rate, 0))}/s  "
                f"peers {st.num_peers}",
                flush=True,
            )
            last_bytes = st.total_done
            last_t = now
            if st.is_seeding or st.progress >= 1.0:
                break
            time.sleep(5)
    except KeyboardInterrupt:
        print("\n[lt] interrupted; torrent remains in library.", flush=True)
        return 130

    lib = load_library()
    if hid in lib:
        lib[hid]["completed_at"] = time.time()
        save_library(lib)
    print("[lt] download complete.", flush=True)
    return 0


# ---------- remove ----------

def cmd_remove(args: argparse.Namespace) -> int:
    lib = load_library()
    hid = args.hash.lower()
    candidates = [k for k in lib if k.startswith(hid)]
    if not candidates:
        print(f"No entry for {args.hash}", file=sys.stderr)
        return 1
    if len(candidates) > 1:
        print(f"Ambiguous prefix: {candidates}", file=sys.stderr)
        return 1
    key = candidates[0]
    meta = lib.pop(key)
    save_library(lib)
    print(f"Removed {key} from library.")
    if args.delete_files:
        save_path = meta.get("save_path", "")
        target = _safe_join(save_path, meta.get("title", "")) if save_path else None
        if target is None:
            print("Refusing to delete: unsafe path", file=sys.stderr)
        elif target.exists():
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                try:
                    target.unlink()
                except OSError as e:
                    print(f"warn: {e}", file=sys.stderr)
            print(f"Deleted {target}")
        else:
            print(f"Path not found: {target}")
    for p in (TORRENTS_CACHE_DIR / f"{key}.torrent", RESUME_DIR / f"{key}.dat"):
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
    return 0


# ---------- main ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="torflash_cli",
        description="Headless CLI for TorFlash (rutor search + libtorrent).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("search", help="Search rutor mirrors.")
    sp.add_argument("query", help="Search query.")
    sp.set_defaults(func=cmd_search)

    lp = sub.add_parser("list", help="List entries in library.json.")
    lp.set_defaults(func=cmd_list)

    dp = sub.add_parser("download", help="Download a magnet or .torrent URL.")
    dp.add_argument("source", help="magnet:?... or https://.../*.torrent")
    dp.add_argument("--no-wait", action="store_true",
                    help="Return immediately after adding the torrent.")
    dp.set_defaults(func=cmd_download)

    rp = sub.add_parser("remove", help="Remove an entry from library.json.")
    rp.add_argument("hash", help="Info-hash or prefix (>=4 hex chars).")
    rp.add_argument("--delete-files", action="store_true",
                    help="Also delete the downloaded files.")
    rp.set_defaults(func=cmd_remove)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
