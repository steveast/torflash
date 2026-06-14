"""TorFlash: слот активной загрузки/копирования в UI (dataclass)."""

from dataclasses import dataclass


@dataclass
class _DlSlot:
    result: dict
    phase: str = "dl"           # "dl" | "copy"
    progress: tuple = (0, "")
    worker: object = None       # DownloadWorker | None
    copy_worker: object = None  # CopyWorker | None
    use_flash: bool = False
    info_hash: str = ""
