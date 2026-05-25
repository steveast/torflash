#!/usr/bin/env python3
"""Lightweight media file inspector using mediainfo or ffprobe.

Exposes file_info(path) -> dict with normalized keys. Pure stdlib only.
Never raises — returns {} on any failure.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys


_EMPTY: dict = {
    "container": "",
    "codec_video": "",
    "width": 0,
    "height": 0,
    "duration_seconds": 0.0,
    "bitrate_kbps": 0,
    "audio_tracks": [],
    "subtitles": [],
    "size_bytes": 0,
    "human_summary": "",
}


# --- codec/language normalization helpers -----------------------------------

_VIDEO_CODEC_MAP = {
    "avc": "h264", "h264": "h264", "x264": "h264",
    "hevc": "hevc", "h265": "hevc", "x265": "hevc",
    "av1": "av1", "vp9": "vp9", "vp8": "vp8",
    "mpeg-4 visual": "mpeg4", "mpeg4": "mpeg4", "xvid": "mpeg4", "divx": "mpeg4",
    "mpeg video": "mpeg2", "mpeg2video": "mpeg2", "mpeg-2 video": "mpeg2",
}
_AUDIO_CODEC_MAP = {
    "ac-3": "ac3", "ac3": "ac3", "e-ac-3": "eac3", "eac3": "eac3",
    "dts": "dts", "dts-hd": "dts", "aac": "aac", "aac lc": "aac",
    "mp3": "mp3", "mpeg audio": "mp3", "flac": "flac", "opus": "opus",
    "vorbis": "vorbis", "pcm": "pcm", "truehd": "truehd", "mlp fba": "truehd",
}
_LANG_ALIASES = {
    "ru": "rus", "russian": "rus", "en": "eng", "english": "eng",
    "uk": "ukr", "ukrainian": "ukr", "de": "ger", "german": "ger",
    "fr": "fre", "french": "fre", "es": "spa", "spanish": "spa",
    "ja": "jpn", "japanese": "jpn",
}


def _norm_video(s: str) -> str:
    if not s:
        return ""
    s = s.strip().lower()
    return _VIDEO_CODEC_MAP.get(s, s.split()[0] if s else "")


def _norm_audio(s: str) -> str:
    if not s:
        return ""
    s = s.strip().lower()
    return _AUDIO_CODEC_MAP.get(s, s.split()[0] if s else "")


def _norm_lang(s: str) -> str:
    if not s:
        return ""
    s = s.strip().lower()
    return _LANG_ALIASES.get(s, s[:3])


def _fmt_duration(secs: float) -> str:
    if secs <= 0:
        return ""
    total = int(secs)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _channels_label(ch: int) -> str:
    return {1: "mono", 2: "2.0", 6: "5.1", 8: "7.1"}.get(ch, f"{ch}ch" if ch else "")


def _human_summary(info: dict) -> str:
    parts = []
    if info["height"]:
        parts.append(f"{info['height']}p {info['codec_video'] or '?'}".strip())
    elif info["codec_video"]:
        parts.append(info["codec_video"])
    if info["audio_tracks"]:
        langs = "/".join(dict.fromkeys(a["language"][:2] for a in info["audio_tracks"] if a["language"]))
        first = info["audio_tracks"][0]
        acodec = (first["codec"] or "").upper()
        chlabel = _channels_label(first["channels"])
        audio_str = " ".join(p for p in [langs, acodec, chlabel] if p)
        if audio_str:
            parts.append(audio_str)
    dur = _fmt_duration(info["duration_seconds"])
    if dur:
        parts.append(dur)
    return " · ".join(parts)


# --- mediainfo backend ------------------------------------------------------

def _run(cmd: list[str], timeout: int = 20) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if out.returncode != 0:
            return None
        return out.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _safe_int(v) -> int:
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return 0


def _safe_float(v) -> float:
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


def _parse_mediainfo(raw: str, path: str) -> dict:
    data = json.loads(raw)
    tracks = ((data.get("media") or {}).get("track")) or []
    info = dict(_EMPTY)
    info["audio_tracks"] = []
    info["subtitles"] = []
    for t in tracks:
        ttype = (t.get("@type") or "").lower()
        if ttype == "general":
            ext = os.path.splitext(path)[1].lstrip(".").lower()
            info["container"] = ext or (t.get("Format") or "").lower()
            info["duration_seconds"] = _safe_float(t.get("Duration"))
            info["bitrate_kbps"] = _safe_int(t.get("OverallBitRate")) // 1000
            info["size_bytes"] = _safe_int(t.get("FileSize"))
        elif ttype == "video" and not info["codec_video"]:
            info["codec_video"] = _norm_video(t.get("Format") or "")
            info["width"] = _safe_int(t.get("Width"))
            info["height"] = _safe_int(t.get("Height"))
        elif ttype == "audio":
            info["audio_tracks"].append({
                "language": _norm_lang(t.get("Language") or ""),
                "codec": _norm_audio(t.get("Format") or ""),
                "channels": _safe_int(t.get("Channels")),
            })
        elif ttype == "text":
            info["subtitles"].append(_norm_lang(t.get("Language") or ""))
    return info


# --- ffprobe backend --------------------------------------------------------

def _parse_ffprobe(raw: str, path: str) -> dict:
    data = json.loads(raw)
    fmt = data.get("format") or {}
    info = dict(_EMPTY)
    info["audio_tracks"] = []
    info["subtitles"] = []
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    info["container"] = ext or (fmt.get("format_name") or "").split(",")[0]
    info["duration_seconds"] = _safe_float(fmt.get("duration"))
    info["bitrate_kbps"] = _safe_int(fmt.get("bit_rate")) // 1000
    info["size_bytes"] = _safe_int(fmt.get("size"))
    for s in data.get("streams") or []:
        ctype = (s.get("codec_type") or "").lower()
        cname = (s.get("codec_name") or "").lower()
        lang = _norm_lang((s.get("tags") or {}).get("language") or "")
        if ctype == "video" and not info["codec_video"]:
            info["codec_video"] = _norm_video(cname)
            info["width"] = _safe_int(s.get("width"))
            info["height"] = _safe_int(s.get("height"))
        elif ctype == "audio":
            info["audio_tracks"].append({
                "language": lang, "codec": _norm_audio(cname),
                "channels": _safe_int(s.get("channels")),
            })
        elif ctype == "subtitle":
            info["subtitles"].append(lang)
    return info


# --- public entry -----------------------------------------------------------

def file_info(path: str) -> dict:
    try:
        if not path or not os.path.isfile(path):
            return {}
        info: dict | None = None
        if shutil.which("mediainfo"):
            raw = _run(["mediainfo", "--Output=JSON", path])
            if raw:
                try:
                    info = _parse_mediainfo(raw, path)
                except (ValueError, KeyError, TypeError):
                    info = None
        if info is None and shutil.which("ffprobe"):
            raw = _run(["ffprobe", "-v", "quiet", "-print_format", "json",
                        "-show_format", "-show_streams", path])
            if raw:
                try:
                    info = _parse_ffprobe(raw, path)
                except (ValueError, KeyError, TypeError):
                    info = None
        if info is None:
            return {}
        if not info["size_bytes"]:
            try:
                info["size_bytes"] = os.path.getsize(path)
            except OSError:
                pass
        info["human_summary"] = _human_summary(info)
        return info
    except Exception:
        return {}


def _find_sample() -> str | None:
    storage = os.path.expanduser("~/Storage")
    if not os.path.isdir(storage):
        return None
    for name in sorted(os.listdir(storage)):
        if name.lower().endswith(".mkv"):
            return os.path.join(storage, name)
    return None


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else _find_sample()
    if not target:
        print("no path given and no .mkv found in ~/Storage")
        sys.exit(1)
    print(f"# {target}")
    print(json.dumps(file_info(target), indent=2, ensure_ascii=False))
