"""Theme management for the rutor-search application.

Exposes:
    apply_theme(app, name) -> None
    available_themes() -> list[str]

Supported themes: "auto", "light", "dark".
"""

from PyQt5.QtGui import QPalette, QColor
from PyQt5.QtCore import Qt  # noqa: F401  (kept for parity with Qt enums use)


_THEMES = ["auto", "light", "dark"]


def available_themes():
    return list(_THEMES)


def _light_palette():
    p = QPalette()
    p.setColor(QPalette.Window, QColor("#f5f5f5"))
    p.setColor(QPalette.WindowText, QColor("#1f2937"))
    p.setColor(QPalette.Base, QColor("#ffffff"))
    p.setColor(QPalette.AlternateBase, QColor("#f0f0f0"))
    p.setColor(QPalette.Text, QColor("#1f2937"))
    p.setColor(QPalette.ButtonText, QColor("#1f2937"))
    p.setColor(QPalette.Highlight, QColor("#2980b9"))
    p.setColor(QPalette.HighlightedText, QColor("white"))
    p.setColor(QPalette.Button, QColor("#e5e7eb"))
    return p


def _dark_palette():
    p = QPalette()
    p.setColor(QPalette.Window, QColor("#1f2937"))
    p.setColor(QPalette.WindowText, QColor("#e5e7eb"))
    p.setColor(QPalette.Base, QColor("#111827"))
    p.setColor(QPalette.AlternateBase, QColor("#1b2433"))
    p.setColor(QPalette.Text, QColor("#e5e7eb"))
    p.setColor(QPalette.ButtonText, QColor("#e5e7eb"))
    p.setColor(QPalette.Highlight, QColor("#2980b9"))
    p.setColor(QPalette.HighlightedText, QColor("white"))
    p.setColor(QPalette.Button, QColor("#374151"))
    p.setColor(QPalette.ToolTipBase, QColor("#1f2937"))
    p.setColor(QPalette.ToolTipText, QColor("#e5e7eb"))
    return p


def apply_theme(app, name):
    """Apply a named theme to the QApplication."""
    if name not in _THEMES:
        raise ValueError(
            "Unknown theme {!r}; expected one of {}".format(name, _THEMES)
        )

    if name == "auto":
        # Reset to default: let Qt follow the desktop environment.
        app.setPalette(app.style().standardPalette())
        # No-op restyle clears any prior style-sheet/palette overrides.
        app.setStyle(app.style().objectName())
        return

    if name == "light":
        app.setPalette(_light_palette())
        return

    if name == "dark":
        app.setPalette(_dark_palette())
        return


if __name__ == "__main__":
    import sys
    from PyQt5.QtWidgets import QApplication

    app = QApplication(sys.argv)

    roles = [
        ("Window", QPalette.Window),
        ("WindowText", QPalette.WindowText),
        ("Base", QPalette.Base),
        ("AlternateBase", QPalette.AlternateBase),
        ("Text", QPalette.Text),
        ("ButtonText", QPalette.ButtonText),
        ("Highlight", QPalette.Highlight),
        ("HighlightedText", QPalette.HighlightedText),
        ("Button", QPalette.Button),
        ("ToolTipBase", QPalette.ToolTipBase),
        ("ToolTipText", QPalette.ToolTipText),
    ]

    print("available_themes() ->", available_themes())
    for theme in available_themes():
        apply_theme(app, theme)
        pal = app.palette()
        print("--- theme: {} ---".format(theme))
        for label, role in roles:
            print("  {:<16} {}".format(label, pal.color(role).name()))
