"""GUI entrypoint for PyCat."""

import os
import sys

from pycat.core.hosts.python_worker import PYTHON_EXEC_WORKER_ARG, run_python_exec_worker
from pycat.core.version import __version__


def _qt_message_handler(mode, context, message):
    """Keep known Qt font fallback noise out of the terminal."""
    text = str(message or "")
    category = str(getattr(context, "category", "") or "")
    if "OpenType support missing for" in text:
        return
    if category in {"qt.text.font.db", "qt.qpa.fonts"}:
        return
    sys.stderr.write(text + "\n")


def main() -> int:
    os.environ.setdefault(
        "QT_LOGGING_RULES",
        "qt.text.font.db=false;qt.qpa.fonts=false",
    )

    try:
        from PyQt6.QtWidgets import QApplication
    except ModuleNotFoundError as exc:
        if exc.name != "PyQt6":
            raise
        message = 'GUI requires PyQt6. Install the desktop extra: python -m pip install "pycat[gui]"'
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        elif sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "PyCat", 0x10)
        return 2
    from PyQt6.QtCore import Qt, qInstallMessageHandler
    from PyQt6.QtGui import QFont, QIcon

    from pycat.gui.main_window import MainWindow

    # Enable high DPI scaling
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeMenuBar, True)
    qInstallMessageHandler(_qt_message_handler)

    app = QApplication(sys.argv)

    # Set application info
    app.setApplicationName("PyCat Agent")
    app.setOrganizationName("PyCat")
    app.setApplicationVersion(__version__)

    # Set application icon
    icon_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "pycat.ico")
    app.setWindowIcon(QIcon(icon_path))

    # Set default font
    font = QFont("Segoe UI", 10)
    font.setStyleHint(QFont.StyleHint.SansSerif)
    app.setFont(font)

    # Create and show main window
    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == PYTHON_EXEC_WORKER_ARG:
        sys.exit(run_python_exec_worker())
    raise SystemExit(main())
