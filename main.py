"""iBridge — HEVC 영상 스트리밍 & iPhone 베젤 UI"""

import asyncio
import contextlib
import struct
import sys
import threading
import logging
from typing import Optional

import av
from PyQt6.QtCore import Qt, QRect, QRectF, QSize, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath
from PyQt6.QtWidgets import (
    QApplication, QLabel, QMainWindow, QSizePolicy, QVBoxLayout, QWidget,
)

from device import USBConnector, StreamServer

logger = logging.getLogger(__name__)

# ── asyncio 백그라운드 루프 ────────────────────────────────────────────────────
_bg_loop: Optional[asyncio.AbstractEventLoop] = None

def _run_bg():
    global _bg_loop
    _bg_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_bg_loop)
    _bg_loop.run_forever()

def _start_bg():
    threading.Thread(target=_run_bg, daemon=True, name="AsyncBG").start()
    while _bg_loop is None:
        threading.Event().wait(0.01)

def schedule(coro):
    return asyncio.run_coroutine_threadsafe(coro, _bg_loop)

# ── 비디오 루프 (asyncio) ─────────────────────────────────────────────────────
async def _video_loop(bridge: "Bridge", stop_ev: asyncio.Event):
    import aiohttp
    while not stop_ev.is_set():
        try:
            codec = av.CodecContext.create("hevc", "r")
            to = aiohttp.ClientTimeout(total=None, connect=10, sock_read=30)
            async with aiohttp.ClientSession() as sess:
                async with sess.get("http://127.0.0.1:8080/stream.bin", timeout=to) as resp:
                    bridge.status_changed.emit("streaming")
                    while not stop_ev.is_set():
                        hdr    = await resp.content.readexactly(5)
                        length = struct.unpack(">I", hdr[:4])[0]
                        ftype  = hdr[4]
                        data   = await resp.content.readexactly(length - 1)
                        if ftype == 2:
                            codec = av.CodecContext.create("hevc", "r")
                        for frame in codec.decode(av.Packet(data)):
                            rgb = frame.to_ndarray(format="rgb24")
                            bridge.frame_ready.emit(bytes(rgb.data), frame.width, frame.height)
        except asyncio.CancelledError:
            return
        except aiohttp.ClientConnectionError:
            if not stop_ev.is_set():
                await asyncio.sleep(0.5)
        except Exception as e:
            if not stop_ev.is_set():
                bridge.status_changed.emit(f"reconnecting ({type(e).__name__})")
                await asyncio.sleep(1.0)

class VideoController:
    def __init__(self):
        self._stop_ev: Optional[asyncio.Event] = None
        self._task:    Optional[asyncio.Task]  = None

    async def start(self, bridge: "Bridge"):
        await self.stop()
        self._stop_ev = asyncio.Event()
        self._task = asyncio.create_task(_video_loop(bridge, self._stop_ev))

    async def stop(self):
        if self._stop_ev: self._stop_ev.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError): await self._task
        self._stop_ev = self._task = None

# ── Qt 시그널 브리지 ───────────────────────────────────────────────────────────
class Bridge(QObject):
    frame_ready         = pyqtSignal(bytes, int, int)
    device_connected    = pyqtSignal(str)
    device_disconnected = pyqtSignal()
    status_changed      = pyqtSignal(str)
    server_ready        = pyqtSignal()

# ── 화면 위젯 (영상 표시만) ───────────────────────────────────────────────────
class ScreenWidget(QWidget):
    def __init__(self):
        super().__init__()
        self._qimg: Optional[QImage] = None
        self._img_size = QSize(0, 0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(200, 400)

    def set_frame(self, data: bytes, w: int, h: int):
        self._img_size = QSize(w, h)
        self._qimg = QImage(data, w, h, w * 3, QImage.Format.Format_RGB888).copy()
        self.update()

    def clear(self):
        self._qimg = None
        self._img_size = QSize(0, 0)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0))
        if self._qimg:
            p.drawImage(self._display_rect(), self._qimg)
        else:
            p.setPen(QColor(70, 70, 70))
            f = QFont()
            f.setPointSize(13)
            p.setFont(f)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "iPhone을 연결하세요")

    def _display_rect(self) -> QRect:
        if self._img_size.isEmpty():
            return self.rect()
        fw, fh = self._img_size.width(), self._img_size.height()
        ww, wh = self.width(), self.height()
        s = min(ww / fw, wh / fh)
        w, h = int(fw * s), int(fh * s)
        return QRect((ww - w) // 2, (wh - h) // 2, w, h)

# ── iPhone 베젤 프레임 ─────────────────────────────────────────────────────────
class PhoneFrame(QWidget):
    _SIDE   = 14
    _BOTTOM = 46
    _R      = 52.0

    def __init__(self, screen: ScreenWidget):
        super().__init__()
        self.setAutoFillBackground(False)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(self._SIDE, self._SIDE, self._SIDE, self._BOTTOM)
        layout.setSpacing(0)
        layout.addWidget(screen)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bezel = QPainterPath()
        bezel.addRoundedRect(QRectF(0, 0, self.width(), self.height()), self._R, self._R)
        p.fillPath(bezel, QColor(28, 28, 30))
        pw, ph = 134, 5
        pill = QPainterPath()
        pill.addRoundedRect(
            QRectF((self.width() - pw) / 2, self.height() - 20, pw, ph),
            ph / 2, ph / 2,
        )
        p.fillPath(pill, QColor(210, 210, 210, 170))

# ── 메인 윈도우 (기본 스트리밍) ───────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self, bridge: Bridge, server: StreamServer, video: VideoController):
        super().__init__()
        self._bridge = bridge
        self._server = server
        self._video  = video
        self._usb = USBConnector(
            on_ready =lambda name: bridge.device_connected.emit(name),
            on_lost  =lambda: bridge.device_disconnected.emit(),
            on_status=lambda msg: bridge.status_changed.emit(msg),
        )
        self._device_name = ""
        self._fps_cnt     = 0

        self.setWindowTitle("iBridge")
        self.setStyleSheet("QMainWindow { background: #000; }")

        self._screen = ScreenWidget()
        self._phone  = PhoneFrame(self._screen)
        self.setCentralWidget(self._phone)

        self._wire()
        self.resize(430, 900)

        self._fps_timer = QTimer()
        self._fps_timer.setInterval(1000)
        self._fps_timer.timeout.connect(self._tick)

    def _wire(self):
        self._bridge.frame_ready.connect(self._on_frame)
        self._bridge.device_connected.connect(self._on_up)
        self._bridge.device_disconnected.connect(self._on_down)
        self._bridge.status_changed.connect(self._on_status)
        self._bridge.server_ready.connect(self._on_srv_ready)

    def _on_frame(self, data: bytes, w: int, h: int):
        self._screen.set_frame(data, w, h)
        self._fps_cnt += 1

    def _on_up(self, name: str):
        self._device_name = name
        self.setWindowTitle(f"iBridge · {name}")
        rsd = self._usb.current_rsd
        schedule(self._server.start(rsd, on_ready=lambda: self._bridge.server_ready.emit()))

    def _on_down(self):
        self._device_name = ""
        self.setWindowTitle("iBridge")
        self._fps_timer.stop()
        self._screen.clear()
        schedule(self._teardown())

    def _on_srv_ready(self):
        self._fps_timer.start()
        schedule(self._video.start(self._bridge))

    def _on_status(self, msg: str):
        if not self._device_name:
            self.setWindowTitle(f"iBridge — {msg.splitlines()[0]}")

    def _tick(self):
        fps = self._fps_cnt
        self._fps_cnt = 0
        self.setWindowTitle(f"iBridge · {self._device_name} · {fps} fps")

    async def _teardown(self):
        await self._video.stop()
        await self._server.stop()

    def closeEvent(self, e):
        self._fps_timer.stop()
        try:
            schedule(self._teardown()).result(timeout=3.0)
        except Exception:
            pass
        e.accept()

# ── 진입점 ────────────────────────────────────────────────────────────────────
def main():
    logging.basicConfig(level=logging.WARNING)
    _start_bg()

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName("iBridge")
    app.setStyleSheet("QWidget { background:#000; color:#f0f0f0; }")

    bridge = Bridge()
    server = StreamServer()
    video  = VideoController()
    win    = MainWindow(bridge, server, video)
    win.show()

    schedule(win._usb.start())
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
