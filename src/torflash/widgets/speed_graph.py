"""TorFlash: живой график скорости загрузки/раздачи (QWidget)."""

from PyQt5.QtGui import QColor, QPainter, QPen
from PyQt5.QtWidgets import QSizePolicy, QWidget

from torflash.i18n import _t
from torflash.helpers import human_bytes


class SpeedGraph(QWidget):
    """Живой график скорости загрузки/раздачи. Рисуется через QPainter."""

    HISTORY = 60  # точек (2 мин при обновлении каждые 2 сек)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(80)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._dl: list[float] = [0.0] * self.HISTORY
        self._ul: list[float] = [0.0] * self.HISTORY
        self._peak: float = 1.0  # avoid div-by-zero

    def push(self, dl_rate: float, ul_rate: float):
        self._dl.append(dl_rate)
        self._ul.append(ul_rate)
        self._dl = self._dl[-self.HISTORY:]
        self._ul = self._ul[-self.HISTORY:]
        self._peak = max(max(self._dl), max(self._ul), 1.0)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        margin_bottom = 14

        # Фон
        p.fillRect(0, 0, w, h, QColor(0, 0, 0, 15))

        draw_h = h - margin_bottom
        n = len(self._dl)
        step = w / max(n - 1, 1)

        def y(val):
            return draw_h - (val / self._peak) * (draw_h - 4)

        # Download (синий)
        p.setPen(QPen(QColor(60, 130, 240, 180), 1.5))
        for i in range(1, n):
            p.drawLine(
                int((i - 1) * step), int(y(self._dl[i - 1])),
                int(i * step), int(y(self._dl[i]))
            )
        # Upload (зелёный)
        p.setPen(QPen(QColor(80, 200, 80, 180), 1.5))
        for i in range(1, n):
            p.drawLine(
                int((i - 1) * step), int(y(self._ul[i - 1])),
                int(i * step), int(y(self._ul[i]))
            )

        # Подписи
        p.setPen(QColor(130, 130, 130))
        font = p.font()
        font.setPixelSize(10)
        p.setFont(font)
        peak_text = _t("пик: {}/с").format(human_bytes(self._peak))
        p.drawText(4, h - 2, peak_text)
        # Легенда справа
        lx = w - 120
        p.setPen(QColor(60, 130, 240))
        p.drawText(lx, h - 2, f"↓ {human_bytes(self._dl[-1])}/{_t('с')}")
        p.setPen(QColor(80, 200, 80))
        p.drawText(lx + 60, h - 2, f"↑ {human_bytes(self._ul[-1])}/{_t('с')}")
        p.end()
