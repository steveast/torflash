"""TorFlash: главное окно приложения (вкладки поиск/библиотека/флешка/настройки)."""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from PyQt5.QtCore import Qt, QSettings, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QClipboard, QFont, QGuiApplication, QIcon, QKeySequence
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QShortcut,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QStyle,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


from torflash.providers import ALL_PROVIDERS, get_provider
from torflash.providers.rutor import CATEGORIES as RUTOR_CATEGORIES
from torflash.config import (
    APP_NAME, APP_VERSION, ASSETS_DIR, STORAGE_DEFAULT, SEARCH_HISTORY_MAX, FAT32_MAX_PART, VIDEO_EXTS, _proxies, set_proxy, current_proxy,
)
from torflash.i18n import _t, set_language
from torflash.helpers import (
    human_bytes, parse_size_text, fmt_time, _result_id, group_movie_parts, MAGNET_HASH_RE,
)
from torflash.platform import backend
from torflash.session.seed_session import SeedSession
from torflash.session.download_worker import DownloadWorker
from torflash.workers.copy_worker import CopyWorker
from torflash.workers.search_worker import SearchWorker
from torflash.workers.meta_fetcher import MetaFetcher
from torflash.workers.poster_fetcher import PosterFetcher
from torflash.update.checker import UpdateChecker
from torflash.update.downloader import UpdateDownloader
from torflash.widgets.speed_graph import SpeedGraph
from torflash.widgets.sortable_item import _SortableItem
from torflash.dl_slot import _DlSlot
from torflash.runtime import themed_icon


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        self.resize(1300, 760)
        icon_path = ASSETS_DIR / "torflash.svg"
        if icon_path.exists():
            from PyQt5.QtGui import QIcon
            self.setWindowIcon(QIcon(str(icon_path)))

        # состояние
        self.results: list[dict] = []
        self.search_workers: list[SearchWorker] = []
        self._search_in_flight: int = 0
        self._search_errors: list[str] = []
        self._active_dls: dict[str, _DlSlot] = {}   # keyed by _result_id(r)
        self._flash_copy_active: CopyWorker | None = None  # one flash-copy at a time
        self._flash_copy_queue: list[tuple] = []     # pending copies
        self._pending_copy_worker: CopyWorker | None = None  # library auto-copy
        self._updating = False  # flag for update-download reusing progress_box
        # Temporary UI state for the currently displayed progress
        self.dl_phase: str = ""
        self.dl_progress: tuple = (0, "")
        flash = backend.find_flash_mount()
        if flash:
            self.dst_dir: str = str(Path(flash) / "Movies")
            self._initial_use_flash = True
        else:
            self.dst_dir = str(Path.home() / "Storage")
            self._initial_use_flash = False
        self.settings = QSettings("TorFlash", "TorFlash")
        self._restrict_settings_perms()
        set_language(self.settings.value("language", "ru", type=str))
        self.seed = SeedSession()
        self._build_ui()
        self._apply_style()
        self._build_tray()
        # Тикер для обновления вкладки «Моя раздача»
        from PyQt5.QtCore import QTimer
        self._lib_timer = QTimer(self)
        self._lib_timer.setInterval(2000)
        self._lib_timer.timeout.connect(self._refresh_library)
        self._lib_timer.start()
        # Тикер для drain_alerts/resume save
        self._alerts_timer = QTimer(self)
        self._alerts_timer.setInterval(1500)
        self._alerts_timer.timeout.connect(self.seed.drain_alerts)
        self._alerts_timer.start()
        # Периодически просим libtorrent сохранить resume_data
        self._resume_timer = QTimer(self)
        self._resume_timer.setInterval(60_000)
        self._resume_timer.timeout.connect(self.seed.request_save_resume_all)
        self._resume_timer.start()
        self._flash_timer = QTimer(self)
        self._flash_timer.setInterval(5000)
        self._flash_timer.timeout.connect(self._refresh_flash_info)
        self._flash_timer.start()
        # Авто-проверка обновлений раз в сутки
        self._auto_update_timer = QTimer(self)
        self._auto_update_timer.setInterval(24 * 60 * 60 * 1000)
        self._auto_update_timer.timeout.connect(self._maybe_check_updates)
        self._auto_update_timer.start()
        # Статистика загрузок — обновляем каждые 30 секунд
        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(30_000)
        self._stats_timer.timeout.connect(self.seed.update_stats)
        self._stats_timer.start()
        # Применяем сохранённые настройки скорости и темы
        self._apply_settings()
        self._refresh_library()
        if self.seed.listen_warning:
            self.statusBar().showMessage(self.seed.listen_warning, 10000)
        # Keyboard shortcuts
        QShortcut(QKeySequence("Ctrl+F"), self, self._focus_search)
        QShortcut(QKeySequence("Escape"), self, self._on_escape)
        # Event filter for Enter on tables
        self.table.installEventFilter(self)
        self.lib_table.installEventFilter(self)
        # Track previous flash state for notifications
        self._prev_flash_mount: str | None = backend.find_flash_mount()

    # ---------- UI building ----------

    def _build_ui(self):
        from PyQt5.QtWidgets import QTabWidget
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 6)
        root.setSpacing(8)

        tabs = QTabWidget()
        tabs.addTab(self._build_search_tab(), _t("Поиск"))
        tabs.addTab(self._build_library_tab(), _t("Моя раздача"))
        tabs.addTab(self._build_flash_tab(), _t("Флешка"))
        tabs.addTab(self._build_settings_tab(), _t("Настройки"))
        tabs.currentChanged.connect(self._on_tab_changed)
        root.addWidget(tabs, 1)
        self.tabs = tabs

        self.setStatusBar(QStatusBar())

    def _build_search_tab(self) -> QWidget:
        wrap = QWidget()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(0, 6, 0, 0)
        v.setSpacing(8)

        # Поисковая строка с категорией
        search_row = QHBoxLayout()
        self.category_combo = QComboBox()
        for cid, name in RUTOR_CATEGORIES:
            self.category_combo.addItem(name, cid)
        last_cat = self.settings.value("last_category", 0, type=int)
        for i, (cid, _) in enumerate(RUTOR_CATEGORIES):
            if cid == last_cat:
                self.category_combo.setCurrentIndex(i)
                break
        search_row.addWidget(self.category_combo)
        self.input = QLineEdit()
        self.input.setPlaceholderText(_t("Название фильма, игры, дистрибутива…"))
        self.input.returnPressed.connect(self.start_search)
        # История запросов: QCompleter
        history = self.settings.value("search_history", [], type=list) or []
        self._search_history = list(history)
        self.search_completer = QCompleter(self._search_history)
        self.search_completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.input.setCompleter(self.search_completer)
        search_row.addWidget(self.input, 1)
        self.search_btn = QPushButton(_t("Искать"))
        self.search_btn.setDefault(True)
        self.search_btn.clicked.connect(self.start_search)
        search_row.addWidget(self.search_btn)
        v.addLayout(search_row)

        # Чекбоксы провайдеров — какие источники опрашиваем
        prov_row = QHBoxLayout()
        prov_row.addWidget(QLabel(_t("Источники:")))
        enabled_names = set(
            self.settings.value(
                "enabled_providers",
                [p.name for p in ALL_PROVIDERS],
                type=list,
            ) or [p.name for p in ALL_PROVIDERS]
        )
        self.provider_checks: dict[str, QCheckBox] = {}
        for p in ALL_PROVIDERS:
            cb = QCheckBox(p.display_name)
            cb.setChecked(p.name in enabled_names)
            cb.toggled.connect(self._save_enabled_providers)
            prov_row.addWidget(cb)
            self.provider_checks[p.name] = cb
        prov_row.addStretch()
        v.addLayout(prov_row)

        # Папка назначения (только если включена флешка) + опции
        dst_row = QHBoxLayout()
        self.flash_check = QCheckBox(_t("Дублировать на флешку (Movies)"))
        self.flash_check.setChecked(self._initial_use_flash)
        self.flash_check.setToolTip(
            _t("Загрузка всегда идёт в ~/Storage. При включении дополнительно копируем на флешку в /Movies с разбиением для FAT32.")
        )
        self.flash_check.toggled.connect(self._on_flash_toggle)
        dst_row.addWidget(self.flash_check)
        self.dst_edit = QLineEdit(self.dst_dir)
        self.dst_edit.setReadOnly(True)
        self.dst_edit.setToolTip(_t("Папка, куда дополнительно копируем (флешка)"))
        dst_row.addWidget(self.dst_edit, 1)
        self.dst_btn = QToolButton()
        self.dst_btn.setText("…")
        self.dst_btn.setToolTip(_t("Выбрать папку вручную"))
        self.dst_btn.clicked.connect(self.choose_destination)
        dst_row.addWidget(self.dst_btn)
        self.flash_redetect = QToolButton()
        self.flash_redetect.setText("⟳")
        self.flash_redetect.setToolTip(_t("Найти флешку заново"))
        self.flash_redetect.clicked.connect(self.redetect_flash)
        dst_row.addWidget(self.flash_redetect)
        self.eject_btn = QToolButton()
        self.eject_btn.setText("⏏")
        self.eject_btn.setToolTip(_t("Безопасно извлечь флешку"))
        self.eject_btn.clicked.connect(self.eject_flash)
        dst_row.addWidget(self.eject_btn)
        v.addLayout(dst_row)

        # Сплиттер: список ← → детали
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_list())
        splitter.addWidget(self._build_detail())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([720, 520])
        v.addWidget(splitter, 1)
        return wrap

    def _build_library_tab(self) -> QWidget:
        wrap = QWidget()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(0, 8, 0, 0)
        v.setSpacing(8)
        info = QLabel(
            _t("Все скачанные торренты лежат в <b>{}</b> и раздаются, пока приложение открыто. Файлы не удаляются автоматически.").format(STORAGE_DEFAULT)
        )
        info.setStyleSheet("color: #888;")
        info.setWordWrap(True)
        v.addWidget(info)

        self.lib_stats_label = QLabel("")
        self.lib_stats_label.setStyleSheet("color: #888; font-size: 11px;")
        v.addWidget(self.lib_stats_label)

        self.speed_graph = SpeedGraph()
        v.addWidget(self.speed_graph)

        # Прогресс активного авто-копирования на флешку
        self.lib_copy_box = QFrame()
        self.lib_copy_box.setObjectName("progressBox")
        self.lib_copy_box.setVisible(False)
        cb = QVBoxLayout(self.lib_copy_box)
        cb.setContentsMargins(12, 8, 12, 8)
        cb.setSpacing(4)
        self.lib_copy_phase = QLabel("")
        self.lib_copy_phase.setStyleSheet("font-weight: 600;")
        cb.addWidget(self.lib_copy_phase)
        self.lib_copy_bar = QProgressBar()
        self.lib_copy_bar.setMinimum(0)
        self.lib_copy_bar.setMaximum(100)
        self.lib_copy_bar.setProperty("phase", "copy")
        cb.addWidget(self.lib_copy_bar)
        bottom = QHBoxLayout()
        self.lib_copy_status = QLabel("")
        self.lib_copy_status.setStyleSheet("color: #888;")
        self.lib_copy_status.setWordWrap(True)
        bottom.addWidget(self.lib_copy_status, 1)
        self.lib_copy_cancel = QPushButton(_t("Отмена"))
        self.lib_copy_cancel.setIcon(themed_icon("process-stop", self.style(), QStyle.SP_DialogCancelButton))
        self.lib_copy_cancel.clicked.connect(self._cancel_pending_copy)
        bottom.addWidget(self.lib_copy_cancel)
        cb.addLayout(bottom)
        v.addWidget(self.lib_copy_box)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_library_list())
        splitter.addWidget(self._build_library_detail())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([720, 520])
        v.addWidget(splitter, 1)
        return wrap

    def _build_library_list(self) -> QWidget:
        self.lib_table = QTableWidget(0, 6)
        self.lib_table.setHorizontalHeaderLabels(
            [_t("Название"), _t("Размер"), _t("Прогресс"), "↓", "↑", _t("Пиров")]
        )
        self.lib_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.lib_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.lib_table.setSelectionMode(QTableWidget.SingleSelection)
        self.lib_table.setAlternatingRowColors(True)
        self.lib_table.setSortingEnabled(True)
        self.lib_table.verticalHeader().setVisible(False)
        lh = self.lib_table.horizontalHeader()
        lh.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, 6):
            lh.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self.lib_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.lib_table.customContextMenuRequested.connect(self._lib_context_menu)
        self.lib_table.itemSelectionChanged.connect(self._on_lib_selection_changed)
        return self.lib_table

    def _build_flash_tab(self) -> QWidget:
        wrap = QWidget()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(0, 8, 0, 0)
        v.setSpacing(8)
        self.flash_summary = QLabel(_t("Флешка не подключена"))
        self.flash_summary.setObjectName("flashInfo")
        self.flash_summary.setProperty("state", "off")
        v.addWidget(self.flash_summary)

        actions = QHBoxLayout()
        self.flash_refresh_btn = QPushButton(_t("Обновить"))
        self.flash_refresh_btn.setIcon(
            themed_icon("view-refresh", self.style(), QStyle.SP_BrowserReload)
        )
        self.flash_refresh_btn.clicked.connect(self._refresh_flash_tab)
        actions.addWidget(self.flash_refresh_btn)
        self.flash_open_btn = QPushButton(_t("Открыть папку"))
        self.flash_open_btn.setIcon(
            themed_icon("folder-open", self.style(), QStyle.SP_DirOpenIcon)
        )
        self.flash_open_btn.clicked.connect(self._open_flash_folder)
        actions.addWidget(self.flash_open_btn)
        self.flash_eject_btn = QPushButton(_t("Безопасно извлечь"))
        self.flash_eject_btn.setIcon(
            themed_icon("media-eject", self.style(), QStyle.SP_DialogCancelButton)
        )
        self.flash_eject_btn.clicked.connect(self.eject_flash)
        actions.addWidget(self.flash_eject_btn)
        actions.addStretch()

        self.flash_delete_btn = QPushButton(_t("Удалить выбранное"))
        self.flash_delete_btn.setIcon(
            themed_icon("edit-delete", self.style(), QStyle.SP_TrashIcon)
        )
        self.flash_delete_btn.setEnabled(False)
        self.flash_delete_btn.clicked.connect(self._flash_delete_selected)
        actions.addWidget(self.flash_delete_btn)

        self.flash_delete_all_btn = QPushButton(_t("Удалить все фильмы"))
        self.flash_delete_all_btn.setIcon(
            themed_icon("edit-clear-all", self.style(), QStyle.SP_DialogResetButton)
        )
        self.flash_delete_all_btn.clicked.connect(self._flash_delete_all)
        actions.addWidget(self.flash_delete_all_btn)

        self.flash_format_btn = QPushButton(_t("Отформатировать"))
        self.flash_format_btn.setIcon(
            themed_icon("drive-harddisk", self.style(), QStyle.SP_DriveHDIcon)
        )
        self.flash_format_btn.clicked.connect(self._flash_format)
        actions.addWidget(self.flash_format_btn)

        v.addLayout(actions)

        self.flash_files_table = QTableWidget(0, 4)
        self.flash_files_table.setHorizontalHeaderLabels(
            [_t("Фильм"), _t("Частей"), _t("Размер"), _t("Изменён")]
        )
        self.flash_files_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.flash_files_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.flash_files_table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.flash_files_table.setAlternatingRowColors(True)
        self.flash_files_table.verticalHeader().setVisible(False)
        fh = self.flash_files_table.horizontalHeader()
        fh.setSectionResizeMode(0, QHeaderView.Stretch)
        fh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        fh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        fh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.flash_files_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.flash_files_table.customContextMenuRequested.connect(self._flash_file_menu)
        self.flash_files_table.itemSelectionChanged.connect(
            self._update_flash_buttons_state
        )
        v.addWidget(self.flash_files_table, 1)

        # Состояние подтверждения для опасных действий (кнопка → arm-таймер)
        self._flash_armed_btn: QPushButton | None = None
        self._flash_arm_timer = QTimer(self)
        self._flash_arm_timer.setSingleShot(True)
        self._flash_arm_timer.setInterval(5000)
        self._flash_arm_timer.timeout.connect(self._disarm_flash_btn)

        return wrap

    def _build_settings_tab(self) -> QWidget:
        outer = QScrollArea()
        outer.setWidgetResizable(True)
        outer.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        v = QVBoxLayout(inner)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(10)

        # Поведение окна
        v.addWidget(QLabel(_t("<b>Приложение</b>")))
        self.cb_minimize = QCheckBox(_t("Сворачивать в трей при закрытии окна"))
        self.cb_minimize.setChecked(
            self.settings.value("minimize_on_close", True, type=bool)
        )
        self.cb_minimize.toggled.connect(
            lambda c: self.settings.setValue("minimize_on_close", c)
        )
        v.addWidget(self.cb_minimize)
        self.cb_autostart = QCheckBox(_t("Запускать при входе в систему"))
        self.cb_autostart.setChecked(backend.is_autostart_enabled())
        self.cb_autostart.toggled.connect(self._apply_autostart)
        v.addWidget(self.cb_autostart)
        self.cb_hidden = QCheckBox(_t("Скрытый старт (только иконка в трее)"))
        self.cb_hidden.setChecked(
            self.settings.value("start_hidden", False, type=bool)
        )
        self.cb_hidden.toggled.connect(
            lambda c: self.settings.setValue("start_hidden", c)
        )
        v.addWidget(self.cb_hidden)
        self.cb_auto_update = QCheckBox(_t("Автоматически проверять обновления (раз в сутки)"))
        self.cb_auto_update.setChecked(
            self.settings.value("auto_check_updates", True, type=bool)
        )
        self.cb_auto_update.toggled.connect(
            lambda c: self.settings.setValue("auto_check_updates", c)
        )
        v.addWidget(self.cb_auto_update)

        # Тема
        theme_row = QHBoxLayout()
        theme_row.addWidget(QLabel(_t("Тема:")))
        self.cb_theme = QComboBox()
        for label, val in ((_t("Системная"), "auto"), (_t("Светлая"), "light"), (_t("Тёмная"), "dark")):
            self.cb_theme.addItem(label, val)
        current_theme = self.settings.value("theme", "auto", type=str)
        for i in range(self.cb_theme.count()):
            if self.cb_theme.itemData(i) == current_theme:
                self.cb_theme.setCurrentIndex(i)
                break
        self.cb_theme.currentIndexChanged.connect(self._on_theme_changed)
        theme_row.addWidget(self.cb_theme, 1)
        theme_row.addStretch()
        v.addLayout(theme_row)

        # Язык
        lang_row = QHBoxLayout()
        lang_row.addWidget(QLabel(_t("Язык:")))
        self.cb_lang = QComboBox()
        for label, val in (("Русский", "ru"), ("English", "en")):
            self.cb_lang.addItem(label, val)
        current_lang = self.settings.value("language", "ru", type=str)
        for i in range(self.cb_lang.count()):
            if self.cb_lang.itemData(i) == current_lang:
                self.cb_lang.setCurrentIndex(i)
                break
        self.cb_lang.currentIndexChanged.connect(self._on_lang_changed)
        lang_row.addWidget(self.cb_lang, 1)
        lang_row.addStretch()
        v.addLayout(lang_row)

        # Лимиты скорости
        v.addWidget(QLabel(_t("<b>Лимиты скорости</b> (КБ/с, 0 — без ограничений):")))
        rate_form = QFormLayout()
        rate_form.setHorizontalSpacing(10)
        self.sp_down = QSpinBox()
        self.sp_down.setRange(0, 1_000_000)
        self.sp_down.setSuffix(_t(" КБ/с"))
        self.sp_down.setValue(self.settings.value("rate_limit_down", 0, type=int))
        self.sp_down.valueChanged.connect(self._on_rate_changed)
        self.sp_up = QSpinBox()
        self.sp_up.setRange(0, 1_000_000)
        self.sp_up.setSuffix(_t(" КБ/с"))
        self.sp_up.setValue(self.settings.value("rate_limit_up", 0, type=int))
        self.sp_up.valueChanged.connect(self._on_rate_changed)
        rate_form.addRow(_t("Скачивание ↓:"), self.sp_down)
        rate_form.addRow(_t("Раздача ↑:"), self.sp_up)
        v.addLayout(rate_form)

        # Сеть (глобальный прокси)
        v.addWidget(QLabel(_t("<b>Сеть</b>")))
        net_form = QFormLayout()
        net_form.setHorizontalSpacing(10)
        # Миграция: если есть старый ключ rutracker_proxy — берём из него
        _saved_proxy = self.settings.value("proxy", "", type=str)
        if not _saved_proxy:
            _saved_proxy = self.settings.value("rutracker_proxy", "", type=str)
        self.proxy_edit = QLineEdit(_saved_proxy)
        self.proxy_edit.setPlaceholderText(_t("socks5://127.0.0.1:1080 или http://proxy:8080"))
        self.proxy_edit.setToolTip(_t("Используется для всех запросов (поиск, постеры, обновления)"))
        self.proxy_edit.editingFinished.connect(self._save_proxy)
        net_form.addRow(_t("Прокси:"), self.proxy_edit)
        v.addLayout(net_form)

        # RuTracker
        v.addWidget(QLabel(_t("<b>RuTracker</b> (требуется аккаунт):")))
        rt_form = QFormLayout()
        rt_form.setHorizontalSpacing(10)
        self.rt_user = QLineEdit(self.settings.value("rutracker_user", "", type=str))
        self.rt_user.setPlaceholderText(_t("логин на rutracker.org"))
        self.rt_user.editingFinished.connect(self._save_rt_credentials)
        self.rt_pass = QLineEdit(self.settings.value("rutracker_pass", "", type=str))
        self.rt_pass.setPlaceholderText(_t("пароль"))
        self.rt_pass.setEchoMode(QLineEdit.Password)
        self.rt_pass.editingFinished.connect(self._save_rt_credentials)
        rt_form.addRow(_t("Логин:"), self.rt_user)
        rt_form.addRow(_t("Пароль:"), self.rt_pass)
        v.addLayout(rt_form)

        info = QLabel(
            _t("Скачивание всегда идёт в <b>~/Storage</b>. Файлы хранятся там до ручного удаления из вкладки «Моя раздача».")
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #888; padding-top: 6px;")
        v.addWidget(info)

        v.addStretch()
        outer.setWidget(inner)
        return outer

    def _on_theme_changed(self):
        theme = self.cb_theme.currentData()
        self.settings.setValue("theme", theme)
        try:
            from torflash.themes import apply_theme
            apply_theme(QApplication.instance(), theme)
        except (ImportError, ModuleNotFoundError):
            pass

    def _on_lang_changed(self):
        lang = self.cb_lang.currentData()
        self.settings.setValue("language", lang)
        set_language(lang)
        self.statusBar().showMessage(
            "Перезапустите приложение для смены языка" if lang == "ru"
            else "Restart the app to apply the language change", 5000
        )

    def _on_rate_changed(self):
        down = self.sp_down.value()
        up = self.sp_up.value()
        self.settings.setValue("rate_limit_down", down)
        self.settings.setValue("rate_limit_up", up)
        self.seed.apply_rate_limits(down, up)

    def _apply_autostart(self, enabled: bool):
        backend.set_autostart(enabled)

    def _on_tab_changed(self, index: int):
        if index == 2:
            self._refresh_flash_tab()

    def _refresh_flash_tab(self):
        mount = backend.find_flash_mount()
        if not mount:
            self.flash_summary.setText(_t("Флешка не подключена"))
            self.flash_summary.setProperty("state", "off")
            self.flash_summary.style().unpolish(self.flash_summary)
            self.flash_summary.style().polish(self.flash_summary)
            self.flash_files_table.setRowCount(0)
            return
        try:
            usage = shutil.disk_usage(mount)
            fs = backend.flash_fstype(mount)
            label = backend.volume_label(mount) or Path(mount).name or mount
            self.flash_summary.setText(
                f"<b>{label}</b> ({mount})"
                + (f" · {fs}" if fs else "")
                + f" · {_t('свободно')} <b>{human_bytes(usage.free)}</b> / {human_bytes(usage.total)}"
            )
            free_ratio = usage.free / usage.total if usage.total else 1
            self.flash_summary.setProperty(
                "state", "warn" if free_ratio < 0.1 else "ok"
            )
            self.flash_summary.style().unpolish(self.flash_summary)
            self.flash_summary.style().polish(self.flash_summary)
        except OSError as e:
            self.flash_summary.setText(_t("Ошибка: {}").format(e))
            return
        # Перечислим содержимое /Movies (если есть) или корня
        target = Path(mount) / "Movies"
        if not target.exists():
            target = Path(mount)
        files: list[tuple[Path, int, float]] = []
        try:
            for p in target.rglob("*"):
                # Только видеофайлы, скрытые пропускаем (Android thumbnails и т.п.)
                if not p.is_file() or p.name.startswith(".") or p.suffix.lower() not in VIDEO_EXTS:
                    continue
                try:
                    st = p.stat()
                    files.append((p, st.st_size, st.st_mtime))
                except OSError:
                    continue
        except OSError as e:
            self.flash_summary.setText(_t("Ошибка чтения: {}").format(e))
            return
        groups = group_movie_parts(files)
        self.flash_files_table.setRowCount(len(groups))
        for i, g in enumerate(groups):
            it_name = QTableWidgetItem(g["title"])
            # Сохраняем список путей в UserRole — нужно для удаления группы целиком
            it_name.setData(Qt.UserRole, [str(p) for p in g["paths"]])
            count = g["count"]
            it_count = QTableWidgetItem(str(count) if count > 1 else "")
            it_count.setTextAlignment(Qt.AlignCenter)
            it_size = QTableWidgetItem(human_bytes(g["size"]))
            it_size.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            it_mtime = QTableWidgetItem(
                time.strftime("%Y-%m-%d %H:%M", time.localtime(g["mtime"]))
            )
            for col, it in enumerate((it_name, it_count, it_size, it_mtime)):
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                self.flash_files_table.setItem(i, col, it)
        self._update_flash_buttons_state()

    def _open_flash_folder(self):
        mount = backend.find_flash_mount()
        if mount and not backend.open_path(mount):
            self.statusBar().showMessage(_t("Не удалось открыть {}").format(mount), 3000)

    def _flash_file_menu(self, pos):
        item = self.flash_files_table.itemAt(pos)
        if not item:
            return
        row = item.row()
        name_item = self.flash_files_table.item(row, 0)
        if not name_item:
            return
        paths = name_item.data(Qt.UserRole) or []
        if not paths:
            return
        menu = QMenu(self)
        # Открыть — для первой части (для одиночных это и есть сам файл)
        act_open = menu.addAction(_t("Открыть"))
        first = paths[0]
        act_open.triggered.connect(lambda: backend.open_path(first))
        menu.addSeparator()
        label = _t("Удалить с флешки") if len(paths) == 1 else _t("Удалить с флешки ({} частей)").format(len(paths))
        act_del = menu.addAction(label)
        act_del.triggered.connect(lambda: self._flash_delete_paths(paths))
        menu.exec_(self.flash_files_table.viewport().mapToGlobal(pos))

    def _flash_delete_paths(self, paths: list[str]) -> tuple[int, list[str]]:
        deleted = 0
        errors = []
        for sp in paths:
            try:
                Path(sp).unlink(missing_ok=True)
                deleted += 1
            except OSError as e:
                errors.append(f"{Path(sp).name}: {e}")
        return deleted, errors

    def _selected_flash_paths(self) -> list[str]:
        paths: list[str] = []
        rows = sorted({i.row() for i in self.flash_files_table.selectedItems()})
        for row in rows:
            name_item = self.flash_files_table.item(row, 0)
            if name_item:
                paths.extend(name_item.data(Qt.UserRole) or [])
        return paths

    def _update_flash_buttons_state(self):
        has_sel = bool(self.flash_files_table.selectionModel()
                       and self.flash_files_table.selectionModel().hasSelection())
        self.flash_delete_btn.setEnabled(has_sel)
        has_rows = self.flash_files_table.rowCount() > 0
        self.flash_delete_all_btn.setEnabled(has_rows)
        # Если выбор пропал — снимаем взвод с кнопки «Удалить выбранное»
        if not has_sel and self._flash_armed_btn is self.flash_delete_btn:
            self._disarm_flash_btn()

    # ---------- arm-to-confirm ----------
    def _arm_flash_btn(self, btn: QPushButton, confirm_text: str, hint: str) -> bool:
        """Двухкликовое подтверждение. Возврат True — пора выполнять действие."""
        if self._flash_armed_btn is btn:
            self._disarm_flash_btn()
            return True
        self._disarm_flash_btn()
        self._flash_armed_btn = btn
        btn.setProperty("_orig_text", btn.text())
        btn.setText(confirm_text)
        btn.setStyleSheet("background-color: #c62828; color: white; font-weight: bold;")
        self._show_banner(hint, kind="warn")
        self._flash_arm_timer.start()
        return False

    def _disarm_flash_btn(self):
        self._flash_arm_timer.stop()
        btn = self._flash_armed_btn
        if btn is not None:
            orig = btn.property("_orig_text")
            if orig:
                btn.setText(orig)
            btn.setStyleSheet("")
        self._flash_armed_btn = None
        self._hide_banner()

    # ---------- delete actions ----------
    def _flash_delete_selected(self):
        paths = self._selected_flash_paths()
        if not paths:
            return
        rows = len({i.row() for i in self.flash_files_table.selectedItems()})
        total = sum(Path(p).stat().st_size for p in paths if Path(p).exists())
        if not self._arm_flash_btn(
            self.flash_delete_btn,
            _t("Подтвердите удаление ({})").format(rows),
            _t("Будет удалено фильмов: {} ({} файлов, {}). Нажмите ещё раз для подтверждения.").format(rows, len(paths), human_bytes(total)),
        ):
            return
        deleted, errors = self._flash_delete_paths(paths)
        self._refresh_flash_tab()
        if errors:
            self._show_banner(_t("Удалено {}, ошибок {}: ").format(deleted, len(errors)) + "; ".join(errors[:3]))
        else:
            self.statusBar().showMessage(_t("Удалено файлов: {}").format(deleted), 4000)

    def _flash_delete_all(self):
        if self._active_dls:
            self._show_banner(_t("Идёт загрузка — дождитесь завершения"))
            return
        if self._flash_copy_active and self._flash_copy_active.isRunning():
            self._show_banner(_t("Идёт копирование — дождитесь завершения"))
            return
        mount = backend.find_flash_mount()
        if not mount:
            self._show_banner(_t("Флешка не смонтирована"))
            return
        movies = Path(mount) / "Movies"
        if not movies.exists():
            self.statusBar().showMessage(_t("Папка Movies не найдена — нечего удалять"), 4000)
            return
        all_paths = [
            str(p) for p in movies.rglob("*")
            if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in VIDEO_EXTS
        ]
        if not all_paths:
            self.statusBar().showMessage(_t("Movies пуста"), 3000)
            return
        total = sum(Path(p).stat().st_size for p in all_paths)
        if not self._arm_flash_btn(
            self.flash_delete_all_btn,
            _t("Подтвердите: удалить ВСЁ"),
            _t("Будут удалены ВСЕ фильмы в {} ({} файлов, {}). Нажмите ещё раз для подтверждения.").format(movies, len(all_paths), human_bytes(total)),
        ):
            return
        deleted, errors = self._flash_delete_paths(all_paths)
        # Подчистим пустые подпапки внутри Movies
        for sub in sorted(movies.rglob("*"), reverse=True):
            if sub.is_dir():
                try:
                    sub.rmdir()
                except OSError:
                    pass
        self._refresh_flash_tab()
        if errors:
            self._show_banner(_t("Удалено {}, ошибок {}: ").format(deleted, len(errors)) + "; ".join(errors[:3]))
        else:
            self.statusBar().showMessage(_t("Удалено все фильмы ({} файлов)").format(deleted), 5000)

    def _flash_format(self):
        if self._active_dls:
            self._show_banner(_t("Идёт загрузка — дождитесь завершения перед форматированием"))
            return
        if self._flash_copy_active and self._flash_copy_active.isRunning():
            self._show_banner(_t("Идёт копирование — дождитесь завершения перед форматированием"))
            return
        mount = backend.find_flash_mount()
        if not mount:
            self._show_banner(_t("Флешка не смонтирована"))
            return
        device = backend.flash_device(mount)
        if not device:
            self._show_banner(_t("Не удалось определить устройство для {}").format(mount))
            return
        # Сохраняем текущую метку при форматировании.
        label = backend.volume_label(mount) or "KINGSTON"
        usage = shutil.disk_usage(mount)
        if not self._arm_flash_btn(
            self.flash_format_btn,
            _t("Подтвердите: форматировать {}").format(device),
            _t("Будут стёрты ВСЕ данные на {} ({}, {}). После форматирования: FAT32, метка «{}». Нажмите ещё раз для подтверждения.").format(device, label, human_bytes(usage.total), label),
        ):
            return
        res = backend.format_fat32(mount, label)
        if not res.ok:
            if res.missing_tool:
                self._show_banner(_t("Утилита не найдена: {}").format(res.message))
            elif res.step == "unmount":
                self._show_banner(_t("Не удалось размонтировать: ") + res.message)
            else:
                self._show_banner(_t("Ошибка форматирования: ") + res.message)
            return
        if res.step == "remount":
            self._show_banner(
                _t("Отформатировано, но не удалось примонтировать: ") + res.message,
                kind="info",
            )
        else:
            self._show_banner(
                _t("Флешка отформатирована (FAT32, метка «{}»)").format(label),
                kind="info",
            )
        self._refresh_flash_tab()
        self._refresh_flash_info()

    def _build_library_detail(self) -> QWidget:
        outer = QScrollArea()
        outer.setWidgetResizable(True)
        outer.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        v = QVBoxLayout(inner)
        v.setContentsMargins(16, 8, 16, 16)
        v.setSpacing(10)

        self.lib_empty = QLabel(_t("Выберите торрент в списке слева"))
        self.lib_empty.setObjectName("emptyHint")
        self.lib_empty.setAlignment(Qt.AlignCenter)
        v.addWidget(self.lib_empty)

        self.lib_detail_card = QWidget()
        self.lib_detail_card.setVisible(False)
        card = QVBoxLayout(self.lib_detail_card)
        card.setContentsMargins(0, 0, 0, 0)
        card.setSpacing(10)

        self.lib_title = QLabel()
        self.lib_title.setWordWrap(True)
        f = QFont(); f.setPointSize(13); f.setBold(True)
        self.lib_title.setFont(f)
        card.addWidget(self.lib_title)

        meta_box = QFrame()
        meta_box.setObjectName("metaBox")
        meta = QFormLayout(meta_box)
        meta.setLabelAlignment(Qt.AlignRight)
        meta.setContentsMargins(12, 12, 12, 12)
        meta.setHorizontalSpacing(14)
        meta.setVerticalSpacing(6)
        self.lib_status_val = QLabel()
        self.lib_size_val = QLabel()
        self.lib_downloaded_val = QLabel()
        self.lib_rates_val = QLabel()
        self.lib_peers_val = QLabel()
        self.lib_path_val = QLabel()
        self.lib_path_val.setWordWrap(True)
        self.lib_path_val.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lib_ratio_val = QLabel()
        self.lib_time_val = QLabel()
        self.lib_pending_val = QLabel()
        self.lib_media_val = QLabel()
        self.lib_media_val.setStyleSheet("color: #888;")
        self.lib_media_val.setWordWrap(True)
        meta.addRow(_t("Статус:"), self.lib_status_val)
        meta.addRow(_t("Размер:"), self.lib_size_val)
        meta.addRow(_t("Скачано:"), self.lib_downloaded_val)
        meta.addRow(_t("Скорости:"), self.lib_rates_val)
        meta.addRow(_t("Время:"), self.lib_time_val)
        meta.addRow(_t("Пиры:"), self.lib_peers_val)
        meta.addRow(_t("Папка:"), self.lib_path_val)
        meta.addRow(_t("Отдано:"), self.lib_ratio_val)
        meta.addRow(_t("На флешку:"), self.lib_pending_val)
        meta.addRow(_t("Медиа:"), self.lib_media_val)
        card.addWidget(meta_box)

        self.lib_progress_bar = QProgressBar()
        self.lib_progress_bar.setMinimum(0)
        self.lib_progress_bar.setMaximum(100)
        card.addWidget(self.lib_progress_bar)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        style = self.style()
        self.lib_pause_btn = QPushButton(_t("Пауза"))
        self.lib_pause_btn.setIcon(themed_icon("media-playback-pause", style, QStyle.SP_MediaPause))
        self.lib_pause_btn.clicked.connect(self._lib_pause_toggle)
        actions.addWidget(self.lib_pause_btn)
        self.lib_recheck_btn = QPushButton(_t("Проверить"))
        self.lib_recheck_btn.setIcon(themed_icon("view-refresh", style, QStyle.SP_BrowserReload))
        self.lib_recheck_btn.setToolTip(_t("Принудительная проверка пиров на диске"))
        self.lib_recheck_btn.clicked.connect(self._lib_force_recheck)
        actions.addWidget(self.lib_recheck_btn)
        self.lib_files_btn = QPushButton(_t("Файлы"))
        self.lib_files_btn.setIcon(themed_icon("document-properties", style, QStyle.SP_FileDialogDetailedView))
        self.lib_files_btn.setToolTip(_t("Выбрать файлы для скачивания (приоритеты)"))
        self.lib_files_btn.clicked.connect(self._lib_select_files)
        actions.addWidget(self.lib_files_btn)
        self.lib_open_btn = QPushButton(_t("Папка"))
        self.lib_open_btn.setIcon(themed_icon("folder-open", style, QStyle.SP_DirOpenIcon))
        self.lib_open_btn.clicked.connect(self._lib_open_current_folder)
        actions.addWidget(self.lib_open_btn)
        self.lib_flash_btn_panel = QPushButton(_t("На флешку"))
        self.lib_flash_btn_panel.setIcon(themed_icon("drive-removable-media-usb", style, QStyle.SP_DriveHDIcon))
        self.lib_flash_btn_panel.setToolTip(_t("Запланировать копирование на флешку (произойдёт при появлении флешки)"))
        self.lib_flash_btn_panel.clicked.connect(self._lib_queue_flash)
        actions.addWidget(self.lib_flash_btn_panel)
        actions.addStretch()
        self.lib_remove_btn = QPushButton(_t("Удалить"))
        self.lib_remove_btn.setIcon(themed_icon("list-remove", style, QStyle.SP_TrashIcon))
        self.lib_remove_btn.setToolTip(_t("Убрать из раздачи (файлы оставить)"))
        self.lib_remove_btn.clicked.connect(self._lib_remove_current_keep)
        actions.addWidget(self.lib_remove_btn)
        self.lib_delete_btn = QPushButton(_t("Удалить + файлы"))
        self.lib_delete_btn.setIcon(themed_icon("edit-delete", style, QStyle.SP_DialogDiscardButton))
        self.lib_delete_btn.clicked.connect(self._lib_remove_current_delete)
        actions.addWidget(self.lib_delete_btn)
        card.addLayout(actions)
        card.addStretch()

        v.addWidget(self.lib_detail_card)
        v.addStretch()
        outer.setWidget(inner)
        return outer

    def _build_list(self) -> QWidget:
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            [_t("Дата"), _t("Источник"), _t("Название"), _t("Размер"), "S", "L"]
        )
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(2, QHeaderView.Stretch)  # Название
        for i in (0, 1, 3, 4, 5):
            h.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.doubleClicked.connect(self.download_to_flash)
        return self.table

    def _build_detail(self) -> QWidget:
        outer = QScrollArea()
        outer.setWidgetResizable(True)
        outer.setFrameShape(QFrame.NoFrame)

        inner = QWidget()
        inner.setObjectName("detailPane")
        v = QVBoxLayout(inner)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(10)

        # Заглушка
        self.empty_label = QLabel(_t("Выберите торрент из списка слева"))
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setObjectName("emptyHint")
        v.addWidget(self.empty_label)

        # Карточка с деталями
        self.detail_card = QWidget()
        self.detail_card.setVisible(False)
        card_v = QVBoxLayout(self.detail_card)
        card_v.setContentsMargins(0, 0, 0, 0)
        card_v.setSpacing(10)

        self.title_label = QLabel()
        self.title_label.setWordWrap(True)
        # Заголовок — недоверенный текст с трекера: рендерим буквально,
        # чтобы остаточная разметка не интерпретировалась как rich-text.
        self.title_label.setTextFormat(Qt.PlainText)
        self.title_label.setObjectName("titleLabel")
        title_font = QFont()
        title_font.setPointSize(13)
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        card_v.addWidget(self.title_label)

        meta_box = QFrame()
        meta_box.setObjectName("metaBox")
        meta = QFormLayout(meta_box)
        meta.setLabelAlignment(Qt.AlignRight)
        meta.setContentsMargins(12, 12, 12, 12)
        meta.setHorizontalSpacing(14)
        meta.setVerticalSpacing(6)
        self.date_val = QLabel("")
        self.size_val = QLabel("")
        self.seeds_val = QLabel("")
        self.leech_val = QLabel("")
        self.hash_val = QLabel("")
        self.hash_val.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.hash_val.setStyleSheet("font-family: monospace; font-size: 11px;")
        for w in (self.date_val, self.size_val, self.seeds_val, self.leech_val):
            w.setStyleSheet("font-weight: 500;")
        meta.addRow(_t("Дата:"), self.date_val)
        meta.addRow(_t("Размер:"), self.size_val)
        meta.addRow(_t("Сиды:"), self.seeds_val)
        meta.addRow(_t("Личеры:"), self.leech_val)
        meta.addRow("Hash:", self.hash_val)
        card_v.addWidget(meta_box)

        # Постер + описание (подгружается асинхронно после выбора)
        self.poster_label = QLabel()
        self.poster_label.setAlignment(Qt.AlignCenter)
        self.poster_label.setMinimumHeight(0)
        self.poster_label.setVisible(False)
        card_v.addWidget(self.poster_label)

        # Галерея скриншотов (горизонтальная прокрутка)
        self.screenshots_scroll = QScrollArea()
        self.screenshots_scroll.setWidgetResizable(True)
        self.screenshots_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.screenshots_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.screenshots_scroll.setFrameShape(QFrame.NoFrame)
        self.screenshots_scroll.setFixedHeight(160)
        self.screenshots_scroll.setVisible(False)
        self._screenshots_widget = QWidget()
        self._screenshots_layout = QHBoxLayout(self._screenshots_widget)
        self._screenshots_layout.setContentsMargins(0, 0, 0, 0)
        self._screenshots_layout.setSpacing(6)
        self._screenshots_layout.addStretch()
        self.screenshots_scroll.setWidget(self._screenshots_widget)
        card_v.addWidget(self.screenshots_scroll)
        self._screenshot_fetchers: list = []

        self.description_view = QTextBrowser()
        self.description_view.setOpenExternalLinks(True)
        self.description_view.setMaximumHeight(200)
        self.description_view.setVisible(False)
        self.description_view.setStyleSheet(
            "QTextBrowser { background: rgba(127,127,127,0.05); border: none; padding: 8px; }"
        )
        card_v.addWidget(self.description_view)

        # Информация о флешке + помещается ли торрент
        self.flash_info = QLabel("")
        self.flash_info.setObjectName("flashInfo")
        self.flash_info.setWordWrap(True)
        card_v.addWidget(self.flash_info)

        # Действия
        actions = QHBoxLayout()
        actions.setSpacing(6)
        style = self.style()
        self.copy_btn = QPushButton("Magnet")
        self.copy_btn.setIcon(themed_icon("edit-copy", style, QStyle.SP_DialogSaveButton))
        self.copy_btn.setToolTip(_t("Скопировать magnet-ссылку в буфер обмена"))
        self.copy_btn.clicked.connect(self.copy_magnet)
        actions.addWidget(self.copy_btn)
        self.ktorrent_btn = QPushButton("KTorrent")
        self.ktorrent_btn.setIcon(themed_icon("ktorrent", style, QStyle.SP_MediaPlay))
        self.ktorrent_btn.clicked.connect(self.open_in_ktorrent)
        actions.addWidget(self.ktorrent_btn)
        self.page_btn = QPushButton(_t("Страница"))
        self.page_btn.setIcon(themed_icon("internet-web-browser", style, QStyle.SP_DirLinkIcon))
        self.page_btn.clicked.connect(self.open_page)
        actions.addWidget(self.page_btn)
        actions.addStretch()
        self.flash_btn = QPushButton(_t("Скачать → на флешку"))
        self.flash_btn.setObjectName("primaryBtn")
        self.flash_btn.setIcon(themed_icon("drive-removable-media-usb", style, QStyle.SP_DriveHDIcon))
        self.flash_btn.clicked.connect(self.download_to_flash)
        actions.addWidget(self.flash_btn)
        card_v.addLayout(actions)

        # Inline-баннер (ошибки/важные сообщения)
        self.banner = QLabel("")
        self.banner.setVisible(False)
        self.banner.setWordWrap(True)
        self.banner.setObjectName("banner")
        card_v.addWidget(self.banner)

        # Прогресс-секция
        self.progress_box = QFrame()
        self.progress_box.setObjectName("progressBox")
        self.progress_box.setVisible(False)
        pv = QVBoxLayout(self.progress_box)
        pv.setContentsMargins(12, 10, 12, 10)
        pv.setSpacing(6)
        self.progress_phase = QLabel("")
        self.progress_phase.setStyleSheet("font-weight: 600;")
        pv.addWidget(self.progress_phase)
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(100)
        pv.addWidget(self.progress_bar)
        self.progress_status = QLabel("")
        self.progress_status.setWordWrap(True)
        self.progress_status.setStyleSheet("color: #888;")
        pv.addWidget(self.progress_status)
        cancel_row = QHBoxLayout()
        cancel_row.addStretch()
        self.cancel_btn = QPushButton(_t("Отмена"))
        self.cancel_btn.setIcon(themed_icon("process-stop", style, QStyle.SP_DialogCancelButton))
        self.cancel_btn.clicked.connect(self._on_cancel)
        cancel_row.addWidget(self.cancel_btn)
        pv.addLayout(cancel_row)
        card_v.addWidget(self.progress_box)

        card_v.addStretch()
        v.addWidget(self.detail_card)
        v.addStretch()

        outer.setWidget(inner)
        return outer

    def _install_tray_icons(self):
        """Копируем иконки трея в ~/.local/share/icons/hicolor/ чтобы KDE Plasma
        находила их по имени, а не по временному пути из PyInstaller."""
        icon_dir = Path.home() / ".local" / "share" / "icons" / "hicolor"
        mapping = {
            "torflash-tray-22.png": "22x22/apps/torflash-tray.png",
            "torflash-tray-32.png": "32x32/apps/torflash-tray.png",
            "torflash-tray-48.png": "48x48/apps/torflash-tray.png",
            "torflash-tray.svg": "scalable/apps/torflash-tray.svg",
            "torflash.svg": "scalable/apps/torflash.svg",
        }
        for src_name, dst_rel in mapping.items():
            src = ASSETS_DIR / src_name
            if not src.exists():
                continue
            dst = icon_dir / dst_rel
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                if not dst.exists() or dst.stat().st_size != src.stat().st_size:
                    shutil.copy2(src, dst)
            except OSError:
                pass

    def _build_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = None
            return
        self._install_tray_icons()
        # KDE Plasma ищет по имени в hicolor; Qt тоже пробует fromTheme
        icon = QIcon.fromTheme("torflash-tray")
        if icon.isNull() or not icon.availableSizes():
            # Фолбэк: из ASSETS_DIR напрямую
            tray_path = ASSETS_DIR / "torflash-tray.svg"
            icon = QIcon(str(tray_path)) if tray_path.exists() else self.windowIcon()
            for size in (22, 32, 48):
                png = ASSETS_DIR / f"torflash-tray-{size}.png"
                if png.exists():
                    icon.addFile(str(png))

        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip(APP_NAME)

        menu = QMenu(self)
        self.act_show = QAction(_t("Показать"), self)
        self.act_show.triggered.connect(self._tray_show)
        menu.addAction(self.act_show)
        menu.addSeparator()
        act_settings = QAction(_t("Настройки…"), self)
        act_settings.triggered.connect(self.open_settings)
        menu.addAction(act_settings)
        self.act_update = QAction(_t("Проверить обновление… (v{})").format(APP_VERSION), self)
        self.act_update.triggered.connect(self.check_for_updates)
        menu.addAction(self.act_update)
        menu.addSeparator()
        act_quit = QAction(_t("Выход"), self)
        act_quit.triggered.connect(self._tray_quit)
        menu.addAction(act_quit)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:  # ЛКМ
            if self.isVisible():
                self.hide()
            else:
                self._tray_show()

    def _tray_show(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def open_settings(self):
        self._tray_show()
        self.tabs.setCurrentIndex(3)  # вкладка «Настройки»

    def _apply_settings(self):
        down = self.settings.value("rate_limit_down", 0, type=int)
        up = self.settings.value("rate_limit_up", 0, type=int)
        self.seed.apply_rate_limits(down, up)
        try:
            from torflash.themes import apply_theme
            theme = self.settings.value("theme", "auto", type=str)
            apply_theme(QApplication.instance(), theme)
        except (ImportError, ModuleNotFoundError) as e:
            print(f"[main] theme module unavailable: {e}", flush=True)
        # Глобальный прокси (миграция со старого ключа rutracker_proxy)
        proxy = self.settings.value("proxy", "", type=str)
        if not proxy:
            proxy = self.settings.value("rutracker_proxy", "", type=str)
            if proxy:
                self.settings.setValue("proxy", proxy)
        set_proxy(proxy)
        # RuTracker credentials
        rt = get_provider("rutracker")
        if rt and hasattr(rt, "set_credentials"):
            rt.set_credentials(
                self.settings.value("rutracker_user", "", type=str),
                self.settings.value("rutracker_pass", "", type=str),
                proxy,
            )

    def _maybe_check_updates(self):
        if not self.settings.value("auto_check_updates", True, type=bool):
            return
        # Скрытая проверка: не показываем «уже свежая»
        self.check_for_updates(silent=True)

    def _tray_quit(self):
        if self.tray:
            self.tray.hide()
        QApplication.quit()

    def closeEvent(self, event):
        if (
            self.tray
            and self.tray.isVisible()
            and self.settings.value("minimize_on_close", True, type=bool)
        ):
            self.hide()
            event.ignore()
            if not self.settings.value("tray_hint_shown", False, type=bool):
                self.tray.showMessage(
                    APP_NAME,
                    _t("Свёрнуто в трей. Правый клик по иконке — настройки и выход."),
                    QSystemTrayIcon.Information,
                    4000,
                )
                self.settings.setValue("tray_hint_shown", True)
            return
        event.accept()

    def _apply_style(self):
        self.setStyleSheet("""
            QLabel#emptyHint {
                color: #888;
                font-size: 13px;
                padding: 40px;
            }
            QLabel#titleLabel {
                padding: 4px 0;
            }
            QFrame#metaBox, QFrame#progressBox {
                background: rgba(127, 127, 127, 0.08);
                border-radius: 6px;
            }
            QPushButton#primaryBtn {
                font-weight: 600;
                padding: 6px 14px;
            }
            QProgressBar {
                text-align: center;
                border: 1px solid rgba(127,127,127,0.3);
                border-radius: 4px;
                height: 20px;
            }
            QProgressBar::chunk {
                background-color: #2980b9;
                border-radius: 3px;
            }
            QProgressBar[phase="copy"]::chunk {
                background-color: #27ae60;
            }
            QLabel#banner {
                background: #c0392b;
                color: white;
                padding: 8px 12px;
                border-radius: 4px;
            }
            QLabel#banner[kind="info"] {
                background: #2980b9;
            }
            QLabel#banner[kind="warn"] {
                background: #d35400;
            }
            QLabel#flashInfo {
                padding: 6px 10px;
                border-radius: 4px;
                background: rgba(40, 167, 69, 0.12);
                color: #2d7a3f;
            }
            QLabel#flashInfo[state="warn"] {
                background: rgba(192, 57, 43, 0.15);
                color: #c0392b;
            }
            QLabel#flashInfo[state="off"] {
                background: rgba(127, 127, 127, 0.08);
                color: #888;
            }
        """)

    # ---------- helpers ----------

    def _show_banner(self, text: str, kind: str = "error"):
        self.banner.setText(text)
        self.banner.setProperty("kind", kind)
        self.banner.style().unpolish(self.banner)
        self.banner.style().polish(self.banner)
        self.banner.setVisible(True)

    def _hide_banner(self):
        self.banner.setVisible(False)

    def current_result(self) -> dict | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        it = self.table.item(row, 0)
        if it is None:
            return None
        return it.data(Qt.UserRole)

    def _on_selection_changed(self):
        r = self.current_result()
        if not r:
            self.detail_card.setVisible(False)
            self.empty_label.setVisible(True)
            return
        self.empty_label.setVisible(False)
        self.detail_card.setVisible(True)
        self.title_label.setText(r["title"])
        self.date_val.setText(r["date"])
        self.size_val.setText(r["size"])
        self.seeds_val.setText(r["seeds"])
        self.leech_val.setText(r["leech"])
        m = MAGNET_HASH_RE.search(r["magnet"])
        self.hash_val.setText(m.group(1) if m else "—")
        self._refresh_flash_info()
        # Сбрасываем постер/описание, запускаем фоновое получение деталей
        self.poster_label.setVisible(False)
        self.poster_label.clear()
        self.screenshots_scroll.setVisible(False)
        self._clear_screenshots()
        self.description_view.setVisible(False)
        self.description_view.clear()
        self._current_meta_url = r["page"]
        # Обновляем кнопку/прогресс при смене выделенного результата
        self._sync_detail_buttons(r)
        if r["page"]:
            self._meta_fetcher = MetaFetcher(r["page"])
            self._meta_fetcher.fetched.connect(self._on_meta_fetched)
            self._track_thread(self._meta_fetcher)

    def _track_thread(self, t):
        """Удерживаем ссылку на фоновый QThread до завершения и стартуем его.
        Иначе перезапись self._meta_fetcher/_poster_fetcher роняет ещё
        работающий поток в GC → ~QThread('Destroyed while running') → abort."""
        if not hasattr(self, "_bg_threads"):
            self._bg_threads = set()
        self._bg_threads.add(t)
        t.finished.connect(lambda: self._bg_threads.discard(t))
        t.start()

    def _on_meta_fetched(self, url: str, data: dict):
        # Игнорируем если пользователь уже выбрал другой торрент
        if getattr(self, "_current_meta_url", None) != url:
            return
        # Сохраняем добытый magnet в результат, чтобы «Копировать magnet»
        # работал для источников без magnet в листинге (NNM/RuTracker).
        m = (data.get("magnet") or "").strip()
        if m:
            cur = self.current_result()
            if cur and cur.get("page") == url and not (cur.get("magnet") or "").strip():
                cur["magnet"] = m
        desc = data.get("description") or ""
        if desc:
            self.description_view.setPlainText(desc.strip())
            self.description_view.setVisible(True)
        poster_url = data.get("poster_url") or ""
        if poster_url:
            self._poster_fetcher = PosterFetcher(poster_url, referer=url)
            self._poster_fetcher.loaded.connect(self._on_poster_loaded)
            self._track_thread(self._poster_fetcher)
        # Скриншоты
        screenshots = data.get("screenshots") or []
        if screenshots:
            self._screenshot_fetchers = []
            for surl in screenshots[:12]:
                f = PosterFetcher(surl, referer=url)
                f.loaded.connect(self._on_screenshot_loaded)
                self._screenshot_fetchers.append(f)
                self._track_thread(f)

    def _on_poster_loaded(self, url: str, data: bytes):
        if not data:
            return
        from PyQt5.QtGui import QPixmap
        pix = QPixmap()
        pix.loadFromData(data)
        if pix.isNull():
            return
        pix = pix.scaledToWidth(280, Qt.SmoothTransformation)
        self.poster_label.setPixmap(pix)
        self.poster_label.setVisible(True)

    def _clear_screenshots(self):
        while self._screenshots_layout.count() > 1:
            item = self._screenshots_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._screenshot_fetchers = []

    def _on_screenshot_loaded(self, url: str, data: bytes):
        if not data:
            return
        from PyQt5.QtGui import QPixmap
        pix = QPixmap()
        pix.loadFromData(data)
        if pix.isNull():
            return
        pix = pix.scaledToHeight(140, Qt.SmoothTransformation)
        lbl = QLabel()
        lbl.setPixmap(pix)
        lbl.setCursor(Qt.PointingHandCursor)
        lbl.setToolTip(_t("Клик для увеличения"))
        lbl.mousePressEvent = lambda e, p=pix, u=url: self._show_screenshot_full(p, u)
        # Вставляем перед stretch
        self._screenshots_layout.insertWidget(self._screenshots_layout.count() - 1, lbl)
        self.screenshots_scroll.setVisible(True)

    def _show_screenshot_full(self, thumb_pix, url: str):
        """Показать скриншот в полном размере в отдельном окне."""
        from PyQt5.QtGui import QPixmap
        from PyQt5.QtWidgets import QDialog, QLabel, QVBoxLayout, QScrollArea
        dlg = QDialog(self)
        dlg.setWindowTitle(_t("Скриншот"))
        dlg.resize(900, 600)
        v = QVBoxLayout(dlg)
        v.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        lbl = QLabel()
        lbl.setAlignment(Qt.AlignCenter)
        # Загрузим полноразмерную версию (fastpic: заменяем /thumb/ на /big/)
        full_url = url.replace("/thumb/", "/big/")
        try:
            import requests
            r = requests.get(full_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10, proxies=_proxies())
            if r.status_code == 200:
                full_pix = QPixmap()
                full_pix.loadFromData(r.content)
                if not full_pix.isNull():
                    lbl.setPixmap(full_pix)
                else:
                    lbl.setPixmap(thumb_pix)
            else:
                lbl.setPixmap(thumb_pix)
        except Exception:
            lbl.setPixmap(thumb_pix)
        scroll.setWidget(lbl)
        v.addWidget(scroll)
        dlg.exec_()

    def _sync_detail_buttons(self, r: dict | None = None):
        """Синхронизирует кнопку загрузки и прогресс-бар для выбранного результата."""
        if r is None:
            r = self.current_result()
        if r is None:
            return
        # Don't hide progress_box if update download is using it
        if self._updating:
            return
        rid = _result_id(r)
        slot = self._active_dls.get(rid)
        if slot:
            self.dl_progress = slot.progress
            self.dl_phase = slot.phase
            self._refresh_progress_widget()
            self.progress_box.setVisible(True)
            self.flash_btn.setEnabled(False)
        else:
            self.progress_box.setVisible(False)
            self.flash_btn.setEnabled(True)

    def _refresh_progress_widget(self):
        pct, status = self.dl_progress
        self.progress_bar.setValue(pct)
        self.progress_status.setText(status)
        if self.dl_phase == "dl":
            self.progress_phase.setText(_t("Скачивание торрента"))
        elif self.dl_phase == "copy":
            self.progress_phase.setText(_t("Копирование → {}").format(self.dst_dir))
        # тот же прогресс-бар, но зелёная заливка для фазы copy
        if self.progress_bar.property("phase") != self.dl_phase:
            self.progress_bar.setProperty("phase", self.dl_phase)
            self.progress_bar.style().unpolish(self.progress_bar)
            self.progress_bar.style().polish(self.progress_bar)

    # ---------- search ----------

    def _save_enabled_providers(self):
        enabled = [n for n, cb in self.provider_checks.items() if cb.isChecked()]
        self.settings.setValue("enabled_providers", enabled)

    def _save_proxy(self):
        proxy = self.proxy_edit.text().strip()
        set_proxy(proxy)
        self.settings.setValue("proxy", proxy)
        # Обновляем прокси и для RuTracker-сессии
        rt = get_provider("rutracker")
        if rt and hasattr(rt, "set_credentials"):
            rt.set_credentials(
                self.settings.value("rutracker_user", "", type=str),
                self.settings.value("rutracker_pass", "", type=str),
                proxy,
            )

    def _save_rt_credentials(self):
        user = self.rt_user.text().strip()
        pwd = self.rt_pass.text()
        self.settings.setValue("rutracker_user", user)
        self.settings.setValue("rutracker_pass", pwd)
        self.settings.sync()
        self._restrict_settings_perms()
        rt = get_provider("rutracker")
        if rt and hasattr(rt, "set_credentials"):
            rt.set_credentials(user, pwd, current_proxy())

    def _restrict_settings_perms(self):
        """Конфиг QSettings (~/.config/TorFlash/TorFlash.conf) хранит пароль
        rutracker в открытом виде — закрываем доступ всем, кроме владельца (0600).
        Не замена системному keyring, но убирает world-readable утечку."""
        try:
            path = self.settings.fileName()
            if path and os.path.exists(path):
                os.chmod(path, 0o600)
        except OSError as e:
            print(f"[settings] chmod failed: {e}", flush=True)

    def _enabled_providers(self) -> list:
        return [p for p in ALL_PROVIDERS if self.provider_checks[p.name].isChecked()]

    def start_search(self):
        query = self.input.text().strip()
        if not query:
            return
        if self._search_in_flight > 0:
            return
        providers = self._enabled_providers()
        if not providers:
            self._show_banner(_t("Не выбран ни один источник"))
            return
        category = self.category_combo.currentData() or 0
        self.settings.setValue("last_category", int(category))
        self._push_history(query)
        self.search_btn.setEnabled(False)
        self.statusBar().showMessage(
            _t("Поиск в {} источниках…").format(len(providers))
        )
        self._hide_banner()
        # Очищаем таблицу и буфер результатов
        self.results = []
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        self._search_errors = []
        self.search_workers = []
        self._search_in_flight = len(providers)
        for p in providers:
            w = SearchWorker(p, query, category=int(category))
            w.done.connect(self._on_search_done)
            w.failed.connect(self._on_search_failed)
            self.search_workers.append(w)
            w.start()

    def _push_history(self, query: str):
        q = query.strip()
        if not q:
            return
        if q in self._search_history:
            self._search_history.remove(q)
        self._search_history.insert(0, q)
        self._search_history = self._search_history[:SEARCH_HISTORY_MAX]
        self.settings.setValue("search_history", self._search_history)
        # Обновляем completer
        from PyQt5.QtCore import QStringListModel
        model = self.search_completer.model()
        if isinstance(model, QStringListModel):
            model.setStringList(self._search_history)
        else:
            self.search_completer = QCompleter(self._search_history)
            self.search_completer.setCaseSensitivity(Qt.CaseInsensitive)
            self.input.setCompleter(self.search_completer)

    def _provider_display(self, name: str) -> str:
        p = get_provider(name)
        return p.display_name if p else name

    def _on_search_done(self, provider_name: str, results: list):
        # Дописываем результаты в таблицу. Сортировка делается после
        # завершения всех воркеров — чтобы пользователь не видел дёрганий.
        before = len(self.results)
        self.results.extend(results)
        self.table.setRowCount(len(self.results))
        display = self._provider_display(provider_name)
        for offset, r in enumerate(results):
            i = before + offset
            cells = (
                ("date", r["date"]),
                ("provider", display),
                ("title", r["title"]),
                ("size", r["size"]),
                ("seeds", r["seeds"]),
                ("leech", r["leech"]),
            )
            for j, (key, text) in enumerate(cells):
                if key in ("seeds", "leech"):
                    item = _SortableItem(text, int(text) if text.isdigit() else 0)
                    item.setTextAlignment(Qt.AlignCenter)
                elif key == "size":
                    item = _SortableItem(text, parse_size_text(text))
                else:
                    item = QTableWidgetItem(text)
                if j == 0:
                    item.setData(Qt.UserRole, r)
                self.table.setItem(i, j, item)
        self._search_in_flight -= 1
        if self._search_in_flight <= 0:
            self._finalize_search()

    def _on_search_failed(self, provider_name: str, err: str):
        self._search_errors.append(f"{self._provider_display(provider_name)}: {err}")
        self._search_in_flight -= 1
        if self._search_in_flight <= 0:
            self._finalize_search()

    def _finalize_search(self):
        self.search_btn.setEnabled(True)
        self.table.setSortingEnabled(True)
        # По умолчанию сортируем по сидам (по убыванию), чтобы лучшее всплыло
        self.table.sortItems(4, Qt.DescendingOrder)
        total = len(self.results)
        if self._search_errors:
            msg = _t("Найдено: {} · ошибок: {}").format(total, len(self._search_errors))
        else:
            msg = _t("Найдено: {}").format(total)
        self.statusBar().showMessage(msg, 6000)
        if self._search_errors and total == 0:
            self._show_banner(_t("Поиск не удался: ") + "; ".join(self._search_errors))
        elif self._search_errors:
            # Часть источников упала, часть отдала — мягкое уведомление
            print(f"[search] partial failures: {self._search_errors}", flush=True)
        if total:
            self.table.selectRow(0)

    # ---------- destination ----------

    def _on_flash_toggle(self, checked: bool):
        if checked:
            flash = backend.find_flash_mount()
            if not flash:
                self.statusBar().showMessage(_t("Флешка не обнаружена — выключаю"), 4000)
                self.flash_check.blockSignals(True)
                self.flash_check.setChecked(False)
                self.flash_check.blockSignals(False)
                self.dst_dir = str(Path.home() / "Storage")
            else:
                self.dst_dir = str(Path(flash) / "Movies")
        else:
            self.dst_dir = str(Path.home() / "Storage")
        self.dst_edit.setText(self.dst_dir)

    def choose_destination(self):
        d = QFileDialog.getExistingDirectory(self, _t("Куда копировать"), self.dst_dir)
        if d:
            self.dst_dir = d
            self.dst_edit.setText(d)
            # Ручной выбор — снимаем галочку флешки
            self.flash_check.blockSignals(True)
            self.flash_check.setChecked(False)
            self.flash_check.blockSignals(False)
            self.statusBar().showMessage(_t("Папка: {}").format(d), 3000)

    def redetect_flash(self):
        flash = backend.find_flash_mount()
        if flash:
            if self.flash_check.isChecked():
                self.dst_dir = str(Path(flash) / "Movies")
                self.dst_edit.setText(self.dst_dir)
            self.statusBar().showMessage(_t("Найдена флешка: {}").format(flash), 4000)
        else:
            self.statusBar().showMessage(_t("Флешка не обнаружена"), 4000)

    def eject_flash(self):
        if getattr(self, "_ejecting", False):
            return
        if self._active_dls:
            self._show_banner(_t("Идёт загрузка — дождитесь завершения перед извлечением"))
            return
        if self._flash_copy_active and self._flash_copy_active.isRunning():
            self._show_banner(_t("Идёт копирование на флешку — дождитесь завершения"))
            return
        mount = backend.find_flash_mount()
        if not mount:
            self._show_banner(_t("Флешка не смонтирована"))
            return

        # Блокируем кнопки на время операции
        self._ejecting = True
        self.eject_btn.setEnabled(False)
        self.flash_eject_btn.setEnabled(False)
        app = QApplication.instance()

        status_text = {
            "sync": _t("⏏ Синхронизация буферов…"),
            "unmount": _t("⏏ Размонтирование…"),
            "poweroff": _t("⏏ Отключение питания устройства…"),
        }

        def on_status(step: str):
            self.statusBar().showMessage(status_text.get(step, step))
            if app:
                app.processEvents()

        try:
            res = backend.eject(mount, on_status=on_status)
            if not res.ok:
                if res.step == "detect":
                    self._show_banner(_t("Не удалось определить устройство для {}").format(mount))
                elif res.missing_tool:
                    self._show_banner(_t("Утилита не найдена: {}").format(res.message))
                elif res.step == "unmount":
                    msg = _t("Не удалось размонтировать: {}").format(res.message)
                    if res.busy:
                        msg += "\n" + _t("Держат: {}").format(res.busy)
                    self._show_banner(msg)
                else:
                    self._show_banner(_t("Ошибка: {}").format(res.message))
                return
            if res.step == "poweroff":
                self._show_banner(
                    _t("Размонтировано, но power-off не сработал: {}. Можно вынимать.").format(res.message),
                    kind="info",
                )
            else:
                self._show_banner(
                    _t("Флешка извлечена ({}) — можно вынимать").format(res.device or mount), kind="info"
                )
            # Переходим в режим ~/Storage
            self.flash_check.blockSignals(True)
            self.flash_check.setChecked(False)
            self.flash_check.blockSignals(False)
            self.dst_dir = str(Path.home() / "Storage")
            self.dst_edit.setText(self.dst_dir)
            self.statusBar().showMessage(_t("Флешка безопасно извлечена"), 5000)
        finally:
            self._ejecting = False
            self.eject_btn.setEnabled(True)
            self.flash_eject_btn.setEnabled(True)

    def stop_all_workers(self, timeout_ms: int = 8000):
        """Останавливает все фоновые потоки перед выходом. DownloadWorker
        останавливаем мягко (stop — без удаления частичной закачки), CopyWorker
        отменяем (cancel). Затем ждём завершения, чтобы интерпретатор не рвал
        запись на флешку/в БД на полуслове."""
        dls = [s.worker for s in list(self._active_dls.values()) if getattr(s, "worker", None)]
        copies = [s.copy_worker for s in list(self._active_dls.values()) if getattr(s, "copy_worker", None)]
        copies += [c for c in (self._flash_copy_active, self._pending_copy_worker) if c]
        for w in dls:
            try:
                w.stop()
            except Exception:
                pass
        for c in copies:
            try:
                c.cancel()
            except Exception:
                pass
        for t in dls + copies:
            try:
                if t.isRunning():
                    t.wait(timeout_ms)
            except Exception:
                pass

    # ---------- actions ----------

    def _set_clipboard(self, text: str):
        """Кладём и в основной буфер, и в X11 PRIMARY (paste средней кнопкой) —
        иначе на части окружений «иногда не вставляется»."""
        cb = QGuiApplication.clipboard()
        cb.setText(text, QClipboard.Clipboard)
        if cb.supportsSelection():
            cb.setText(text, QClipboard.Selection)

    def copy_magnet(self):
        r = self.current_result()
        if not r:
            return
        magnet = (r.get("magnet") or "").strip()
        if magnet:
            self._set_clipboard(magnet)
            self.statusBar().showMessage(_t("Magnet скопирован"), 3000)
            return
        # У NNM/RuTracker magnet нет в листинге — достаём со страницы раздачи.
        page = r.get("page") or ""
        if not page:
            self._show_banner(_t("У этого источника нет magnet — используйте «Скачать»"))
            return
        self.statusBar().showMessage(_t("Получаю magnet…"), 5000)
        fetcher = MetaFetcher(page)

        def _on_magnet(url, data, _r=r):
            m = (data.get("magnet") or "").strip()
            if m:
                _r["magnet"] = m
                self._set_clipboard(m)
                self.statusBar().showMessage(_t("Magnet скопирован"), 3000)
            else:
                self._show_banner(_t("У этого источника нет magnet — используйте «Скачать»"))
        fetcher.fetched.connect(_on_magnet)
        self._track_thread(fetcher)

    def open_page(self):
        r = self.current_result()
        if not r or not r["page"]:
            return
        import webbrowser
        webbrowser.open(r["page"])

    def open_in_ktorrent(self):
        r = self.current_result()
        if not r:
            return
        exe = shutil.which("ktorrent")
        if not exe:
            self._show_banner(_t("KTorrent не найден в PATH"))
            return
        try:
            subprocess.Popen([exe, r["magnet"]])
            self.statusBar().showMessage(_t("Открыто в KTorrent"), 3000)
        except OSError as e:
            self._show_banner(_t("Ошибка запуска KTorrent: {}").format(e))

    def download_to_flash(self):
        r = self.current_result()
        if not r:
            return
        rid = _result_id(r)
        if rid in self._active_dls:
            self.statusBar().showMessage(_t("Уже скачивается"), 3000)
            return
        try:
            STORAGE_DEFAULT.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._show_banner(_t("Не удалось создать {}: {}").format(STORAGE_DEFAULT, e))
            return
        self._hide_banner()
        slot = _DlSlot(result=r, use_flash=self.flash_check.isChecked())
        self._active_dls[rid] = slot
        self._sync_detail_buttons()
        # Для провайдеров с авторизацией (RuTracker) передаём cookies сессии
        cookies = None
        prov = get_provider(r.get("provider", ""))
        if prov and hasattr(prov, "_session") and prov._session:
            cookies = prov._session.cookies
        worker = DownloadWorker(
            self.seed, r["magnet"], str(STORAGE_DEFAULT),
            r.get("torrent_url", ""),
            mark_pending_flash=slot.use_flash,
            cookies=cookies,
        )
        slot.worker = worker
        worker.progress.connect(lambda pct, st, sid=rid: self._on_dl_progress(sid, pct, st))
        worker.done.connect(lambda sd, rp, ih, sid=rid: self._on_dl_done(sid, sd, rp, ih))
        worker.failed.connect(lambda err, sid=rid: self._on_dl_failed(sid, err))
        worker.start()

    def _on_cancel(self):
        cur = self.current_result()
        if not cur:
            return
        rid = _result_id(cur)
        slot = self._active_dls.get(rid)
        if not slot:
            return
        if slot.worker and slot.worker.isRunning():
            slot.worker.cancel()
        if slot.copy_worker and slot.copy_worker.isRunning():
            slot.copy_worker.cancel()

    def _on_dl_progress(self, slot_id: str, pct: int, status: str):
        slot = self._active_dls.get(slot_id)
        if not slot:
            return
        slot.progress = (pct, status)
        # Update UI only if viewing this result
        cur = self.current_result()
        if cur and _result_id(cur) == slot_id:
            self.dl_progress = slot.progress
            self.dl_phase = slot.phase
            self._refresh_progress_widget()

    def _on_dl_failed(self, slot_id: str, err: str):
        slot = self._active_dls.pop(slot_id, None)
        title = slot.result.get("title", "?")[:60] if slot else "?"
        print(f"[dl] failed slot={slot_id[:20]} err={err!r}", flush=True)
        if err == _t("Отменено"):
            self.statusBar().showMessage(_t("Загрузка отменена"), 2500)
        else:
            self._show_banner(_t("Ошибка загрузки: {}").format(err))
            self._notify(_t("Ошибка загрузки"), f"{title}: {err}")
        self._sync_detail_buttons()

    def _on_dl_done(self, slot_id: str, save_dir: str, rel_paths: list, info_hash: str):
        slot = self._active_dls.get(slot_id)
        if not slot:
            return
        title = slot.result.get("title", "?")[:60]
        slot.info_hash = info_hash
        self._notify(_t("Загрузка завершена"), title)
        if not slot.use_flash:
            self._active_dls.pop(slot_id, None)
            self.statusBar().showMessage(
                _t("Скачано в {}, продолжаю раздачу").format(save_dir), 8000
            )
            self._show_banner(
                _t("Готово: файлы в {}, раздаются. Управление — на вкладке «Моя раздача».").format(save_dir),
                kind="info",
            )
            self._sync_detail_buttons()
            return
        # Queue flash copy
        self._flash_copy_queue.append((slot_id, save_dir, rel_paths, info_hash))
        self._start_next_flash_copy()

    def _on_copy_progress(self, slot_id: str, pct: int, status: str):
        slot = self._active_dls.get(slot_id)
        if not slot:
            return
        slot.progress = (pct, status)
        cur = self.current_result()
        if cur and _result_id(cur) == slot_id:
            self.dl_progress = slot.progress
            self.dl_phase = slot.phase
            self._refresh_progress_widget()

    def _on_copy_done(self, slot_id: str, report: list):
        slot = self._active_dls.pop(slot_id, None)
        title = slot.result.get("title", "?")[:60] if slot else "?"
        summary = " · ".join(line for line in report)
        # Clear pending_flash_copy for this torrent
        if slot and slot.info_hash:
            self.seed.clear_pending_flash(slot.info_hash)
        self._flash_copy_active = None
        self.statusBar().showMessage(f"{_t('Готово')}: {summary}", 8000)
        self._show_banner(
            _t("Скопировано на флешку. Оригинал в ~/Storage, раздаётся."),
            kind="info",
        )
        self._notify(_t("Скопировано на флешку"), title)
        self._sync_detail_buttons()
        # Start next queued flash copy if any
        self._start_next_flash_copy()

    def _on_copy_failed(self, slot_id: str, err: str):
        slot = self._active_dls.pop(slot_id, None)
        print(f"[copy] failed slot={slot_id[:20]} err={err!r}", flush=True)
        self._flash_copy_active = None
        if err == _t("Отменено"):
            self.statusBar().showMessage(_t("Копирование отменено"), 2500)
        else:
            self._show_banner(
                _t("Ошибка копирования: {}. Файлы скачаны в ~/Storage, раздача идёт.").format(err)
            )
        self._sync_detail_buttons()
        # Start next queued flash copy if any
        self._start_next_flash_copy()

    def _start_next_flash_copy(self):
        """Start the next queued flash copy, one at a time."""
        if self._flash_copy_active and self._flash_copy_active.isRunning():
            return
        if not self._flash_copy_queue:
            return
        slot_id, save_dir, rel_paths, info_hash = self._flash_copy_queue.pop(0)
        slot = self._active_dls.get(slot_id)
        if not slot:
            # Slot was cancelled/removed — try next
            self._start_next_flash_copy()
            return
        dst_dir = self.dst_dir
        try:
            Path(dst_dir).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._active_dls.pop(slot_id, None)
            self._show_banner(_t("Не удалось создать {}: {}").format(dst_dir, e))
            self._sync_detail_buttons()
            self._start_next_flash_copy()
            return
        if not os.access(dst_dir, os.W_OK):
            self._active_dls.pop(slot_id, None)
            self._show_banner(_t("Нет прав на запись в {}").format(dst_dir))
            self._sync_detail_buttons()
            self._start_next_flash_copy()
            return
        slot.phase = "copy"
        slot.progress = (0, _t("Подготовка…"))
        cw = CopyWorker(save_dir, rel_paths, dst_dir, FAT32_MAX_PART)
        slot.copy_worker = cw
        self._flash_copy_active = cw
        cw.progress.connect(lambda pct, st, sid=slot_id: self._on_copy_progress(sid, pct, st))
        cw.done.connect(lambda rpt, sid=slot_id: self._on_copy_done(sid, rpt))
        cw.failed.connect(lambda err, sid=slot_id: self._on_copy_failed(sid, err))
        cw.start()
        self._sync_detail_buttons()

    # ---------- flash info ----------

    def _refresh_flash_info(self):
        if not hasattr(self, "flash_info"):
            return
        mount = backend.find_flash_mount()
        # Notify on flash connect/disconnect
        prev = getattr(self, "_prev_flash_mount", None)
        if mount and not prev:
            self._notify(_t("Флешка подключена"), mount)
        self._prev_flash_mount = mount
        r = self.current_result()
        torrent_size = parse_size_text(r["size"]) if r else 0

        def set_state(state: str):
            self.flash_info.setProperty("state", state)
            self.flash_info.style().unpolish(self.flash_info)
            self.flash_info.style().polish(self.flash_info)

        if not mount:
            self.flash_info.setText(_t("Флешка не подключена — копирование пропустим"))
            set_state("off")
            return
        try:
            usage = shutil.disk_usage(mount)
        except OSError as e:
            self.flash_info.setText(_t("Ошибка чтения {}: {}").format(mount, e))
            set_state("warn")
            return
        fs = backend.flash_fstype(mount)
        label = backend.volume_label(mount) or Path(mount).name or mount
        text = (
            f"<b>{label}</b> ({mount})"
            + (f" · {fs}" if fs else "")
            + f" · {_t('свободно')} <b>{human_bytes(usage.free)}</b> / {human_bytes(usage.total)}"
        )
        if torrent_size > 0:
            text += "<br/>" + _t("Размер торрента: <b>{}</b>").format(human_bytes(torrent_size))
            if torrent_size > usage.free:
                need = torrent_size - usage.free
                text += _t(" — <b>не помещается</b>, не хватает {}").format(human_bytes(need))
                set_state("warn")
            else:
                left = usage.free - torrent_size
                text += _t(" — после копирования останется {}").format(human_bytes(left))
                set_state("ok")
        else:
            set_state("ok")
        self.flash_info.setText(text)

    # ---------- library / seeding ----------

    def _refresh_library(self):
        rows = self.seed.all_statuses()
        # Remember selection and sort state
        prev_hash = self._selected_lib_hash()
        self.lib_table.setSortingEnabled(False)
        self.lib_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self._set_lib_row(i, r)
        self.lib_table.setSortingEnabled(True)
        # Restore selection
        if prev_hash:
            for i in range(self.lib_table.rowCount()):
                it = self.lib_table.item(i, 0)
                if it and it.data(Qt.UserRole) == prev_hash:
                    self.lib_table.selectRow(i)
                    break
        self._check_pending_flash_copies(rows)
        self._refresh_lib_detail()
        # Суммарная скорость для графика
        total_dl_rate = sum(r.get("download_rate", 0) for r in rows)
        total_ul_rate = sum(r.get("upload_rate", 0) for r in rows)
        if hasattr(self, "speed_graph"):
            self.speed_graph.push(total_dl_rate, total_ul_rate)
        # Update stats display
        stats = self.seed.stats
        if hasattr(self, "lib_stats_label"):
            total_dl = stats.get("total_downloaded", 0)
            total_ul = stats.get("total_uploaded", 0)
            today = time.strftime("%Y-%m-%d")
            daily = stats.get("daily", {})
            today_dl = daily.get(today, {}).get("dl", 0)
            today_ul = daily.get(today, {}).get("ul", 0)
            self.lib_stats_label.setText(
                _t("Всего: ↓{} ↑{} · Сегодня: ↓{} ↑{}").format(
                    human_bytes(total_dl), human_bytes(total_ul),
                    human_bytes(today_dl), human_bytes(today_ul),
                )
            )

    def _check_pending_flash_copies(self, rows: list):
        """Если торрент завершён и помечен pending_flash_copy — копируем на флешку.

        Работает и после перезапуска: флаг хранится в library.json."""
        if self._pending_copy_worker and self._pending_copy_worker.isRunning():
            return
        if self._flash_copy_active and self._flash_copy_active.isRunning():
            return
        mount = backend.find_flash_mount()
        if not mount:
            return
        for r in rows:
            if not r["is_seeding"] and r["progress"] < 1.0:
                continue
            meta = self.seed.library.get(r["hash"], {})
            if not meta.get("pending_flash_copy"):
                continue
            handle = self.seed.handles.get(r["hash"])
            if not handle:
                continue
            info = handle.torrent_file()
            if not info:
                continue
            files = info.files()
            rel_paths = [files.file_path(i) for i in range(files.num_files())]
            target = str(Path(mount) / "Movies")
            try:
                Path(target).mkdir(parents=True, exist_ok=True)
            except OSError as e:
                print(f"[flash] mkdir failed: {e}", flush=True)
                return
            print(f"[flash] auto-copy {r['title'][:60]} → {target}", flush=True)
            self._pending_copy_hash = r["hash"]
            self._pending_copy_title = r["title"]
            self._pending_copy_worker = CopyWorker(meta["save_path"], rel_paths, target, FAT32_MAX_PART)
            self._pending_copy_worker.progress.connect(self._on_pending_copy_progress)
            self._pending_copy_worker.done.connect(self._on_pending_copy_done)
            self._pending_copy_worker.failed.connect(self._on_pending_copy_failed)
            self.lib_copy_phase.setText(_t("Копирую на флешку: {}").format(r['title'][:80]))
            self.lib_copy_bar.setValue(0)
            self.lib_copy_status.setText(_t("Подготовка…"))
            self.lib_copy_box.setVisible(True)
            self._pending_copy_worker.start()
            self.statusBar().showMessage(
                _t("Копирую на флешку: {}").format(r['title'][:60]), 5000
            )
            return  # одна копия за раз

    def _on_pending_copy_progress(self, pct: int, status: str):
        if hasattr(self, "lib_copy_bar"):
            self.lib_copy_bar.setValue(pct)
            self.lib_copy_status.setText(status)
        self.statusBar().showMessage(_t("Флешка: {}").format(status), 2500)

    def _on_pending_copy_done(self, report: list):
        hid = getattr(self, "_pending_copy_hash", None)
        title = getattr(self, "_pending_copy_title", "")[:80]
        if hid:
            self.seed.clear_pending_flash(hid)
        self._pending_copy_hash = None
        self._pending_copy_worker = None
        if hasattr(self, "lib_copy_box"):
            self.lib_copy_box.setVisible(False)
        self.statusBar().showMessage(_t("Скопировано на флешку"), 5000)
        self._notify(_t("Скопировано на флешку"), title)

    def _on_pending_copy_failed(self, err: str):
        self._pending_copy_hash = None
        self._pending_copy_worker = None
        if hasattr(self, "lib_copy_box"):
            self.lib_copy_box.setVisible(False)
        if err != _t("Отменено"):
            self.statusBar().showMessage(_t("Не удалось скопировать на флешку: {}").format(err), 5000)

    def _cancel_pending_copy(self):
        if self._pending_copy_worker and self._pending_copy_worker.isRunning():
            self._pending_copy_worker.cancel()

    def _set_lib_row(self, i: int, r: dict):
        title_item = QTableWidgetItem(r["title"] or _t("(метаданные…)"))
        title_item.setData(Qt.UserRole, r["hash"])

        size_item = _SortableItem(human_bytes(r["size"]) if r["size"] else "?", r["size"])

        pct = int(r["progress"] * 100)
        if r["is_seeding"] or pct == 100:
            prog_text = _t("раздача")
        elif r["has_metadata"]:
            prog_text = f"{r['state']} {pct}%"
        else:
            prog_text = _t("метаданные…")
        prog_item = _SortableItem(prog_text, r["progress"])
        prog_item.setTextAlignment(Qt.AlignCenter)

        down_item = _SortableItem(f"{human_bytes(r['download_rate'])}/s", r["download_rate"])
        up_item = _SortableItem(f"{human_bytes(r['upload_rate'])}/s", r["upload_rate"])
        if r["upload_rate"] > 0:
            up_item.setForeground(Qt.green)

        peers_item = _SortableItem(f"{r['num_peers']} ({r['num_seeds']}↑)", r["num_peers"])
        peers_item.setTextAlignment(Qt.AlignCenter)

        for col, item in enumerate(
            (title_item, size_item, prog_item, down_item, up_item, peers_item)
        ):
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self.lib_table.setItem(i, col, item)

    def _lib_context_menu(self, pos):
        item = self.lib_table.itemAt(pos)
        if not item:
            return
        row = item.row()
        title_item = self.lib_table.item(row, 0)
        info_hash = title_item.data(Qt.UserRole) if title_item else None
        if not info_hash:
            return
        menu = QMenu(self)
        act_open = menu.addAction(_t("Открыть папку"))
        act_open.triggered.connect(lambda: self._lib_open_folder(info_hash))
        menu.addSeparator()
        act_rm = menu.addAction(_t("Убрать из раздачи (файлы оставить)"))
        act_rm.triggered.connect(lambda: self._lib_remove(info_hash, False))
        act_del = menu.addAction(_t("Удалить вместе с файлами"))
        act_del.triggered.connect(lambda: self._lib_remove(info_hash, True))
        menu.exec_(self.lib_table.viewport().mapToGlobal(pos))

    def _lib_open_folder(self, info_hash: str):
        meta = self.seed.library.get(info_hash)
        if not meta:
            return
        save = Path(meta.get("save_path", STORAGE_DEFAULT))
        if not backend.open_path(str(save)):
            self._show_banner(_t("Не удалось открыть {}").format(save))

    def _lib_remove(self, info_hash: str, delete_files: bool):
        self.seed.remove(info_hash, delete_files=delete_files)
        self._refresh_library()
        msg = _t("Удалено вместе с файлами") if delete_files else _t("Убрано из раздачи")
        self.statusBar().showMessage(msg, 3000)

    def _selected_lib_hash(self):
        row = self.lib_table.currentRow()
        if row < 0:
            return None
        item = self.lib_table.item(row, 0)
        return item.data(Qt.UserRole) if item else None

    def _on_lib_selection_changed(self):
        if not self._selected_lib_hash():
            self.lib_detail_card.setVisible(False)
            self.lib_empty.setVisible(True)
            return
        self._refresh_lib_detail()

    def _refresh_lib_detail(self):
        hid = self._selected_lib_hash()
        if not hid:
            return
        s = self.seed.get_status(hid)
        h = self.seed.handles.get(hid)
        if not s or not h:
            self.lib_detail_card.setVisible(False)
            self.lib_empty.setVisible(True)
            return
        self.lib_empty.setVisible(False)
        self.lib_detail_card.setVisible(True)

        st = h.status()
        paused = bool(getattr(st, "paused", False)) or bool(getattr(st, "auto_managed", True)) is False and bool(getattr(st, "paused", False))
        try:
            paused = bool(st.paused)
        except AttributeError:
            paused = False

        self.lib_title.setText(s["title"])
        if paused:
            status = _t("На паузе")
        elif s["is_seeding"]:
            status = _t("Раздаётся")
        elif s["has_metadata"]:
            status = f"{s['state']} {int(s['progress'] * 100)}%"
        else:
            status = _t("Получение метаданных…")
        self.lib_status_val.setText(status)
        self.lib_size_val.setText(human_bytes(s["size"]) if s["size"] else "?")
        if s["size"]:
            downloaded = s["progress"] * s["size"]
            self.lib_downloaded_val.setText(
                f"{human_bytes(downloaded)} ({int(s['progress'] * 100)}%)"
            )
        else:
            self.lib_downloaded_val.setText(f"{int(s['progress'] * 100)}%")
        self.lib_rates_val.setText(
            f"↓ {human_bytes(s['download_rate'])}/s · ↑ {human_bytes(s['upload_rate'])}/s"
        )
        # Время: прошло с добавления + ETA
        elapsed_parts = []
        if s["added_at"]:
            elapsed_s = time.time() - s["added_at"]
            elapsed_parts.append(_t("прошло {}").format(fmt_time(elapsed_s)))
        if s["completed_at"] and s["added_at"]:
            dl_time = s["completed_at"] - s["added_at"]
            elapsed_parts.append(_t("скачано за {}").format(fmt_time(dl_time)))
        elif not s["is_seeding"] and s["progress"] < 1.0 and s["download_rate"] > 1024 and s["size"]:
            remaining = (1.0 - s["progress"]) * s["size"] / s["download_rate"]
            elapsed_parts.append(_t("осталось ~{}").format(fmt_time(remaining)))
        if s["seeding_time"] and s["is_seeding"]:
            elapsed_parts.append(_t("раздача {}").format(fmt_time(s['seeding_time'])))
        self.lib_time_val.setText(" · ".join(elapsed_parts) if elapsed_parts else "—")
        self.lib_peers_val.setText(f"{s['num_peers']} ({_t('сидов: {}').format(s['num_seeds'])})")
        self.lib_path_val.setText(s["save_path"])
        total_up = getattr(st, "total_payload_upload", 0) or 0
        total_done = max(1, int(s["progress"] * s["size"])) if s["size"] else 1
        ratio = total_up / total_done if total_done else 0
        self.lib_ratio_val.setText(f"{human_bytes(total_up)} (ratio {ratio:.2f})")
        pending = self.seed.library.get(hid, {}).get("pending_flash_copy", False)
        self.lib_pending_val.setText(_t("✓ запланировано") if pending else "—")
        # Медиа-инфо для самого большого .mkv в папке торрента (один раз, кэшируем)
        self._update_media_info(hid, h, s)
        self.lib_progress_bar.setValue(int(s["progress"] * 100))
        self.lib_progress_bar.setProperty(
            "phase", "copy" if s["is_seeding"] else "dl"
        )
        self.lib_progress_bar.style().unpolish(self.lib_progress_bar)
        self.lib_progress_bar.style().polish(self.lib_progress_bar)

        style = self.style()
        if paused:
            self.lib_pause_btn.setText(_t("Возобновить"))
            self.lib_pause_btn.setIcon(themed_icon("media-playback-start", style, QStyle.SP_MediaPlay))
        else:
            self.lib_pause_btn.setText(_t("Пауза"))
            self.lib_pause_btn.setIcon(themed_icon("media-playback-pause", style, QStyle.SP_MediaPause))
        self.lib_flash_btn_panel.setEnabled(not pending)

    def _update_media_info(self, hid: str, handle, s: dict):
        if not s.get("has_metadata"):
            self.lib_media_val.setText("—")
            return
        if not hasattr(self, "_media_cache"):
            self._media_cache = {}
        if hid in self._media_cache:
            self.lib_media_val.setText(self._media_cache[hid])
            return
        # Ищем самый большой видеофайл
        try:
            info = handle.torrent_file()
            files = info.files()
            best = None
            best_size = 0
            for i in range(files.num_files()):
                path = files.file_path(i)
                if path.lower().endswith((".mkv", ".mp4", ".avi", ".m4v", ".mov")):
                    fs = files.file_size(i)
                    if fs > best_size:
                        best_size = fs
                        best = path
            if not best:
                self.lib_media_val.setText("—")
                self._media_cache[hid] = "—"
                return
            full = Path(s["save_path"]) / best
            if not full.exists():
                self.lib_media_val.setText(_t("(файл недоступен)"))
                return
        except Exception as e:
            self.lib_media_val.setText(f"{_t('ошибка')}: {e}")
            return
        # Запускаем фоновую проверку
        self._media_cache[hid] = _t("загружаю…")
        self.lib_media_val.setText(_t("загружаю…"))

        class _MediaWorker(QThread):
            done = pyqtSignal(str, str)

            def __init__(self, hid, path):
                super().__init__()
                self.hid = hid
                self.path = path

            def run(self):
                try:
                    from torflash.mediainfo import file_info
                    data = file_info(str(self.path))
                    summary = data.get("human_summary", "") or "—"
                except Exception as e:
                    summary = f"({_t('ошибка')}: {e})"
                self.done.emit(self.hid, summary)

        w = _MediaWorker(hid, full)
        w.done.connect(self._on_media_done)
        w.start()
        # Сохраняем ссылку чтобы не GC'нулся
        self._media_worker = w

    def _on_media_done(self, hid: str, summary: str):
        self._media_cache[hid] = summary
        if self._selected_lib_hash() == hid:
            self.lib_media_val.setText(summary)

    def _lib_pause_toggle(self):
        hid = self._selected_lib_hash()
        if not hid:
            return
        h = self.seed.handles.get(hid)
        if not h:
            return
        try:
            if h.status().paused:
                h.resume()
            else:
                h.pause()
        except AttributeError:
            pass
        self._refresh_lib_detail()

    def _lib_force_recheck(self):
        hid = self._selected_lib_hash()
        if not hid:
            return
        h = self.seed.handles.get(hid)
        if h:
            try:
                h.force_recheck()
                self.statusBar().showMessage(_t("Перепроверка пиров запущена"), 3000)
            except AttributeError as e:
                self.statusBar().showMessage(_t("recheck недоступен: {}").format(e), 3000)

    def _lib_open_current_folder(self):
        hid = self._selected_lib_hash()
        if hid:
            self._lib_open_folder(hid)

    def _lib_queue_flash(self):
        hid = self._selected_lib_hash()
        if not hid:
            return
        if hid not in self.seed.library:
            return
        self.seed.set_pending_flash(hid, True)
        self.statusBar().showMessage(
            _t("Запланировано — скопируем при появлении флешки"), 4000
        )
        self._check_pending_flash_copies(self.seed.all_statuses())
        self._refresh_lib_detail()

    def _lib_remove_current_keep(self):
        hid = self._selected_lib_hash()
        if hid:
            self._lib_remove(hid, delete_files=False)

    def _lib_remove_current_delete(self):
        hid = self._selected_lib_hash()
        if hid:
            self._lib_remove(hid, delete_files=True)

    # ---------- updater ----------

    def check_for_updates(self, silent: bool = False):
        if getattr(self, "update_checker", None) and self.update_checker.isRunning():
            return
        self._update_silent = silent
        if not silent:
            self.statusBar().showMessage(_t("Проверяю обновление…"), 3000)
        self.update_checker = UpdateChecker()
        self.update_checker.found.connect(self._on_update_found)
        self.update_checker.up_to_date.connect(self._on_up_to_date)
        self.update_checker.failed.connect(self._on_update_check_failed)
        self.update_checker.start()

    def _on_update_found(self, version: str, url: str, asset_name: str,
                         sha256_url: str = "", minisig_url: str = ""):
        self._pending_update = (version, url, asset_name, sha256_url, minisig_url)
        self._show_banner(
            _t("Доступна версия v{} (сейчас v{}). Нажмите ⏏ для обновления → автозамена бинарника и перезапуск.").format(version, APP_VERSION),
            kind="info",
        )
        # Используем кнопку eject_btn временно? Лучше отдельную. Покажем уведомление трея.
        if self.tray:
            self.tray.showMessage(
                APP_NAME,
                _t("Доступна версия v{}. Кликните в меню «Установить обновление».").format(version),
                QSystemTrayIcon.Information,
                6000,
            )
        # Меняем пункт меню на «Установить обновление v…»
        self.act_update.setText(_t("Установить обновление v{}").format(version))
        try:
            self.act_update.triggered.disconnect()
        except TypeError:
            pass
        self.act_update.triggered.connect(self._install_pending_update)

    def _install_pending_update(self):
        if not getattr(self, "_pending_update", None):
            return
        if not getattr(sys, "frozen", False):
            self._show_banner(
                _t("Запущена python-версия — обновление возможно только для бинарника. Запустите через ярлык TorFlash и попробуйте снова.")
            )
            return
        version, url, _, sha256_url, minisig_url = self._pending_update
        binary_dir = str(Path(sys.executable).parent)
        self._update_dl = UpdateDownloader(url, binary_dir, sha256_url, minisig_url)
        self._updating = True
        self.progress_phase.setText(_t("Обновление до v{}").format(version))
        self.progress_bar.setValue(0)
        self.progress_status.setText(_t("Скачивание обновления…"))
        self.progress_bar.setProperty("phase", "copy")
        self.progress_bar.style().unpolish(self.progress_bar)
        self.progress_bar.style().polish(self.progress_bar)
        self.progress_box.setVisible(True)
        self._update_dl.progress.connect(self._on_update_dl_progress)
        self._update_dl.done.connect(self._on_update_dl_done)
        self._update_dl.failed.connect(self._on_update_dl_failed)
        self._update_dl.start()

    def _on_update_dl_progress(self, pct: int, status: str):
        self.progress_bar.setValue(pct)
        self.progress_status.setText(status)

    def _on_update_dl_done(self, new_path: str):
        self._updating = False
        self.progress_box.setVisible(False)
        current = str(Path(sys.executable))
        self._show_banner(
            _t("Обновление установлено. Перезапуск…"),
            kind="info",
        )
        app = QApplication.instance()
        if app:
            app.processEvents()
        if self.tray:
            self.tray.hide()
        try:
            # Linux/macOS: заменяет бинарник и делает execv (сюда не вернётся).
            # Windows: запускает отложенный хелпер замены и возвращается.
            backend.install_update(new_path, current, sys.argv[1:])
        except OSError as e:
            self._show_banner(_t("Не удалось заменить бинарник: {}").format(e))
            return
        # Достижимо только на Windows: завершаем процесс, чтобы хелпер заменил .exe.
        if app:
            app.quit()

    def _on_update_dl_failed(self, err: str):
        self._updating = False
        self.progress_box.setVisible(False)
        self._show_banner(_t("Не удалось скачать обновление: {}").format(err))

    def _on_up_to_date(self, version: str):
        if not getattr(self, "_update_silent", False):
            self.statusBar().showMessage(_t("Установлена последняя версия (v{})").format(version), 5000)

    def _on_update_check_failed(self, err: str):
        if not getattr(self, "_update_silent", False):
            self.statusBar().showMessage(_t("Проверка обновлений не удалась: {}").format(err), 5000)

    # ---------- notifications ----------

    def _notify(self, title: str, body: str):
        """Системное уведомление. Linux — notify-send (как раньше); на Windows/
        macOS (и Linux без notify-send) — через системный трей."""
        if sys.platform.startswith("linux"):
            try:
                icon_path = str(ASSETS_DIR / "torflash.svg")
                cmd = ["notify-send", "-a", APP_NAME, "-i", icon_path, title, body]
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except (FileNotFoundError, OSError):
                pass  # notify-send недоступен — упадём в трей ниже
        tray = getattr(self, "tray", None)
        if tray is not None and tray.supportsMessages():
            tray.showMessage(title, body, self.windowIcon())

    # ---------- keyboard shortcuts ----------

    def _focus_search(self):
        self.tabs.setCurrentIndex(0)
        self.input.setFocus()
        self.input.selectAll()

    def _on_escape(self):
        if self.input.hasFocus() and self.input.text():
            self.input.clear()
        else:
            cur = self.current_result()
            if cur:
                rid = _result_id(cur)
                slot = self._active_dls.get(rid)
                if slot:
                    self._on_cancel()

    def eventFilter(self, obj, event):
        if event.type() == event.KeyPress:
            if obj is self.table and event.key() in (Qt.Key_Return, Qt.Key_Enter):
                self.download_to_flash()
                return True
            if obj is self.lib_table and event.key() == Qt.Key_Delete:
                hid = self._selected_lib_hash()
                if hid:
                    self._lib_remove(hid, delete_files=False)
                return True
        return super().eventFilter(obj, event)

    # ---------- selective file download ----------

    def _lib_select_files(self):
        hid = self._selected_lib_hash()
        if not hid:
            return
        h = self.seed.handles.get(hid)
        if not h or not h.status().has_metadata:
            self.statusBar().showMessage(_t("Метаданные ещё не получены"), 3000)
            return
        info = h.torrent_file()
        files = info.files()
        current_prio = h.file_priorities()

        dlg = QDialog(self)
        dlg.setWindowTitle(_t("Выбор файлов"))
        dlg.resize(600, 400)
        layout = QVBoxLayout(dlg)

        # Select all / none
        top = QHBoxLayout()
        btn_all = QPushButton(_t("Выбрать все"))
        btn_none = QPushButton(_t("Снять все"))
        top.addWidget(btn_all)
        top.addWidget(btn_none)
        top.addStretch()
        layout.addLayout(top)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        form = QVBoxLayout(inner)

        checks = []
        for i in range(files.num_files()):
            path = files.file_path(i)
            size = files.file_size(i)
            cb = QCheckBox(f"{path}  ({human_bytes(size)})")
            cb.setChecked(current_prio[i] > 0)
            form.addWidget(cb)
            checks.append(cb)

        btn_all.clicked.connect(lambda: [c.setChecked(True) for c in checks])
        btn_none.clicked.connect(lambda: [c.setChecked(False) for c in checks])

        scroll.setWidget(inner)
        layout.addWidget(scroll)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        if dlg.exec_() == QDialog.Accepted:
            prio = [4 if c.isChecked() else 0 for c in checks]
            h.prioritize_files(prio)
            self.statusBar().showMessage(_t("Приоритеты файлов обновлены"), 3000)
