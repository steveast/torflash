"""TorFlash: ячейка таблицы с числовой сортировкой (QTableWidgetItem)."""

from PyQt5.QtWidgets import QTableWidgetItem


class _SortableItem(QTableWidgetItem):
    """QTableWidgetItem, который сортируется по числовому значению, а не по тексту."""
    def __init__(self, text: str, sort_value):
        super().__init__(text)
        self._sort_value = sort_value

    def __lt__(self, other):
        if isinstance(other, _SortableItem):
            return self._sort_value < other._sort_value
        return super().__lt__(other)
