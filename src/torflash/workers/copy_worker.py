"""TorFlash: копирование фильмов на флешку с разбиением для FAT32 (QThread)."""

import re
import shutil
import subprocess
import time
import traceback
from pathlib import Path

from PyQt5.QtCore import QThread, pyqtSignal

from torflash.i18n import _t
from torflash.helpers import human_bytes, fmt_time, _safe_join, _split_copy_part_name


class CopyWorker(QThread):
    progress = pyqtSignal(int, str)
    done = pyqtSignal(list)  # список сообщений (что разбито, что скопировано целиком)
    failed = pyqtSignal(str)

    def __init__(self, src_dir: str, rel_paths: list[str], dst_dir: str, chunk_size: int):
        super().__init__()
        self.src_dir = src_dir
        self.rel_paths = rel_paths
        self.dst_dir = dst_dir
        self.chunk_size = chunk_size
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            sources = [Path(self.src_dir) / p for p in self.rel_paths]
            sources = [p for p in sources if p.is_file()]
            total_bytes = sum(p.stat().st_size for p in sources)
            if total_bytes == 0:
                self.failed.emit(_t("Нет файлов для копирования"))
                return
            self._start = time.monotonic()
            self._total = total_bytes
            copied = 0
            report = []
            has_mkvmerge = shutil.which("mkvmerge") is not None
            for src in sources:
                rel = src.relative_to(self.src_dir)
                # Имена из недоверенного .torrent: не даём rel с ../ вырваться
                # за пределы целевой папки (перезапись чужих файлов).
                dst = _safe_join(self.dst_dir, rel)
                if dst is None:
                    report.append(f"⚠ {rel} — {_t('небезопасный путь, пропущено')}")
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                size = src.stat().st_size
                # Incremental: skip files already on destination with same size
                if size <= self.chunk_size and dst.exists() and dst.stat().st_size == size:
                    copied += size
                    report.append(f"≡ {rel} ({human_bytes(size)}) — {_t('уже на флешке')}")
                    self.progress.emit(
                        int(copied * 100 / total_bytes),
                        self._stat_line(copied, f"{_t('пропускаю')} {rel.name}"),
                    )
                    continue
                if size <= self.chunk_size:
                    copied = self._stream_copy(src, dst, copied, total_bytes, f"{_t('копирую')} {rel.name}")
                    report.append(f"✓ {rel} ({human_bytes(size)})")
                elif src.suffix.lower() == ".mkv" and has_mkvmerge:
                    parts = self._mkvmerge_split(src, dst, copied, total_bytes)
                    copied += size
                    report.append(
                        f"M {rel} → {parts} {_t('проигрываемых MKV-частей через mkvmerge')}"
                    )
                else:
                    parts = self._split_copy(src, dst, copied, total_bytes)
                    copied += size
                    report.append(f"✂ {rel} → {parts} {_t('частей по')} ≤ {human_bytes(self.chunk_size)}")
                if self._cancel:
                    self.failed.emit(_t("Отменено"))
                    return
            self.done.emit(report)
        except OSError as e:
            print(f"[copy] FAILED dst={self.dst_dir}\n{traceback.format_exc()}", flush=True)
            self.failed.emit(_t("Ошибка ввода-вывода: {}").format(e))
        except Exception as e:
            print(f"[copy] CRASH\n{traceback.format_exc()}", flush=True)
            self.failed.emit(_t("Ошибка: {}").format(e))

    def _stream_copy(self, src: Path, dst: Path, copied: int, total: int, label: str) -> int:
        buf_size = 4 * 1024 * 1024
        with open(src, "rb") as fin, open(dst, "wb") as fout:
            while True:
                if self._cancel:
                    return copied
                buf = fin.read(buf_size)
                if not buf:
                    break
                fout.write(buf)
                copied += len(buf)
                self.progress.emit(int(copied * 100 / total), self._stat_line(copied, label))
        return copied

    def _stat_line(self, copied: int, label: str) -> str:
        elapsed = time.monotonic() - self._start
        rate = copied / elapsed if elapsed > 0.2 else 0
        eta = (self._total - copied) / rate if rate > 1024 else None
        return (
            f"{label} · ↑ {human_bytes(rate)}/s "
            f"· ETA {fmt_time(eta)} · {_t('прошло')} {fmt_time(elapsed)}"
        )

    def _mkvmerge_split(self, src: Path, dst: Path, copied: int, total: int) -> int:
        """Режет MKV по keyframe'ам через mkvmerge — каждая часть валидный MKV.

        Имена частей: name-001.mkv, name-002.mkv, ..."""
        chunk_mb = max(64, self.chunk_size // (1024 * 1024))
        cmd = [
            "mkvmerge",
            "--gui-mode",
            "-o", str(dst),
            "--split", f"size:{chunk_mb}M",
            str(src),
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        src_size = src.stat().st_size
        for line in proc.stdout or []:
            if self._cancel:
                proc.terminate()
                break
            m = re.search(r"#GUI#progress\s+(\d+)%", line) or re.search(r"Progress:\s*(\d+)%", line)
            if m:
                local_pct = int(m.group(1))
                global_done = copied + (local_pct / 100.0) * src_size
                global_pct = int(global_done * 100 / total) if total else 0
                self.progress.emit(
                    global_pct,
                    self._stat_line(int(global_done), f"mkvmerge {src.name} {local_pct}%"),
                )
        proc.wait()
        if proc.returncode not in (0, 1):  # 1 = warnings, still produces output
            raise OSError(f"mkvmerge exit {proc.returncode}")
        # Подсчёт получившихся частей
        produced = sorted(dst.parent.glob(f"{dst.stem}-*{dst.suffix}"))
        return len(produced)

    def _split_copy(self, src: Path, dst: Path, copied: int, total: int) -> int:
        buf_size = 4 * 1024 * 1024
        part_idx = 0
        # Расширение сохраняется в конце: name.part001.mkv (а не name.mkv.part001)
        stem, ext = dst.stem, dst.suffix
        with open(src, "rb") as fin:
            while True:
                if self._cancel:
                    return part_idx
                part_name = _split_copy_part_name(stem, ext, part_idx)
                part_path = dst.with_name(part_name)
                written = 0
                with open(part_path, "wb") as fout:
                    while written < self.chunk_size:
                        if self._cancel:
                            return part_idx
                        to_read = min(buf_size, self.chunk_size - written)
                        buf = fin.read(to_read)
                        if not buf:
                            break
                        fout.write(buf)
                        written += len(buf)
                        copied_now = copied + part_idx * self.chunk_size + written
                        self.progress.emit(
                            int(copied_now * 100 / total),
                            self._stat_line(
                                copied_now,
                                f"{_t('режу')} {dst.name} · {_t('часть')} {part_idx + 1}",
                            ),
                        )
                if written == 0:
                    part_path.unlink(missing_ok=True)
                    break
                part_idx += 1
                if written < self.chunk_size:
                    break
        return part_idx
