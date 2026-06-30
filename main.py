"""iBridge — macOS iBridgeing 동일 기능 (네이티브 Qt 앱)"""

import asyncio
import contextlib
import queue
import struct
import sys
import threading
import logging
from typing import Optional

import av
import requests
from PyQt6.QtCore import Qt, QRect, QRectF, QSize, QTimer, pyqtSignal, QObject, QPoint
from PyQt6.QtGui import (
    QAction, QColor, QFont, QImage, QKeyEvent, QKeySequence,
    QMouseEvent, QPainter, QPainterPath,
)
from PyQt6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QSizePolicy, QVBoxLayout, QWidget,
)

from device import USBConnector, WiFiConnector, StreamServer, AppManager

logger = logging.getLogger(__name__)

# ── HID 키 매핑 (Qt.Key int → USB HID usage 0x07) ────────────────────────────
def _build_key_map() -> dict[int, int]:
    m: dict[int, int] = {}
    for i in range(26):                          # A–Z  → 4–29
        m[65 + i] = 4 + i
    for i in range(9):                           # 1–9  → 30–38
        m[49 + i] = 30 + i
    m[int(Qt.Key.Key_0)]           = 39
    m[int(Qt.Key.Key_Return)]      = 40
    m[int(Qt.Key.Key_Enter)]       = 40
    m[int(Qt.Key.Key_Escape)]      = 41
    m[int(Qt.Key.Key_Backspace)]   = 42
    m[int(Qt.Key.Key_Tab)]         = 43
    m[int(Qt.Key.Key_Space)]       = 44
    m[int(Qt.Key.Key_Minus)]       = 45
    m[int(Qt.Key.Key_Equal)]       = 46
    m[int(Qt.Key.Key_BracketLeft)] = 47
    m[int(Qt.Key.Key_BracketRight)]= 48
    m[int(Qt.Key.Key_Backslash)]   = 49
    m[int(Qt.Key.Key_Semicolon)]   = 51
    m[int(Qt.Key.Key_Apostrophe)]  = 52
    m[int(Qt.Key.Key_QuoteLeft)]   = 53
    m[int(Qt.Key.Key_Comma)]       = 54
    m[int(Qt.Key.Key_Period)]      = 55
    m[int(Qt.Key.Key_Slash)]       = 56
    m[int(Qt.Key.Key_CapsLock)]    = 57
    f1 = int(Qt.Key.Key_F1)
    for i in range(12):                          # F1–F12 → 58–69
        m[f1 + i] = 58 + i
    m[int(Qt.Key.Key_Delete)]   = 76
    m[int(Qt.Key.Key_Home)]     = 74
    m[int(Qt.Key.Key_End)]      = 77
    m[int(Qt.Key.Key_PageUp)]   = 75
    m[int(Qt.Key.Key_PageDown)] = 78
    m[int(Qt.Key.Key_Right)]    = 79
    m[int(Qt.Key.Key_Left)]     = 80
    m[int(Qt.Key.Key_Down)]     = 81
    m[int(Qt.Key.Key_Up)]       = 82
    m[int(Qt.Key.Key_Control)]  = 0xE0
    m[int(Qt.Key.Key_Shift)]    = 0xE1
    m[int(Qt.Key.Key_Alt)]      = 0xE2
    m[int(Qt.Key.Key_Meta)]     = 0xE3
    return m

_KEY_MAP = _build_key_map()
_MOD_MAP = {
    Qt.KeyboardModifier.ControlModifier: 0xE0,
    Qt.KeyboardModifier.ShiftModifier:   0xE1,
    Qt.KeyboardModifier.AltModifier:     0xE2,
    Qt.KeyboardModifier.MetaModifier:    0xE3,
}

# ── asyncio 백그라운드 루프 ────────────────────────────────────────────────────
_bg_loop: Optional[asyncio.AbstractEventLoop] = None

def _run_bg():
    global _bg_loop
    # Windows에서 aiohttp는 SelectorEventLoop 필요 (ProactorLoop 비호환)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    _bg_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_bg_loop)
    _bg_loop.run_forever()

def _start_bg():
    threading.Thread(target=_run_bg, daemon=True, name="AsyncBG").start()
    while _bg_loop is None:
        threading.Event().wait(0.01)

def schedule(coro):
    return asyncio.run_coroutine_threadsafe(coro, _bg_loop)

# ── 터치 HTTP 큐 ──────────────────────────────────────────────────────────────
_touch_q: queue.Queue = queue.Queue(maxsize=5)

def _touch_worker():
    while True:
        data = _touch_q.get()
        try:
            requests.post("http://127.0.0.1:8080/touch", json=data, timeout=0.3)
        except Exception:
            pass

def _send_touch(data: dict):
    if _touch_q.full():
        try: _touch_q.get_nowait()
        except queue.Empty: pass
    try: _touch_q.put_nowait(data)
    except queue.Full: pass

def _fire(path: str, data: dict):
    threading.Thread(
        target=lambda: requests.post(
            f"http://127.0.0.1:8080{path}", json=data, timeout=1
        ), daemon=True,
    ).start()

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
                        hdr  = await resp.content.readexactly(5)
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
    apps_loaded         = pyqtSignal(list)
    launch_error        = pyqtSignal(str)

# ── 화면 위젯 ─────────────────────────────────────────────────────────────────
class ScreenWidget(QWidget):
    def __init__(self):
        super().__init__()
        self._qimg: Optional[QImage] = None
        self._img_size = QSize(0, 0)
        self._pressing = False
        self._held: set[int] = set()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
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
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       "iPhone을 연결하세요")

    def _display_rect(self) -> QRect:
        if self._img_size.isEmpty():
            return self.rect()
        fw, fh = self._img_size.width(), self._img_size.height()
        ww, wh = self.width(), self.height()
        s = min(ww / fw, wh / fh)
        w, h = int(fw * s), int(fh * s)
        return QRect((ww - w) // 2, (wh - h) // 2, w, h)

    def _to_hid(self, pos: QPoint) -> tuple[int, int]:
        r = self._display_rect()
        if r.width() <= 0 or r.height() <= 0:
            return 0, 0
        x = max(0.0, min(1.0, (pos.x() - r.x()) / r.width()))
        y = max(0.0, min(1.0, (pos.y() - r.y()) / r.height()))
        return int(x * 65535), int(y * 65535)

    def mousePressEvent(self, e: QMouseEvent):
        if e.button() == Qt.MouseButton.LeftButton:
            self._pressing = True
            self.setFocus()
            x, y = self._to_hid(e.pos())
            _send_touch({"type": "contact", "x": x, "y": y})

    def mouseMoveEvent(self, e: QMouseEvent):
        if self._pressing:
            x, y = self._to_hid(e.pos())
            _send_touch({"type": "contact", "x": x, "y": y})

    def mouseReleaseEvent(self, e: QMouseEvent):
        if e.button() == Qt.MouseButton.LeftButton and self._pressing:
            self._pressing = False
            x, y = self._to_hid(e.pos())
            _send_touch({"type": "release", "x": x, "y": y})

    def mouseDoubleClickEvent(self, e: QMouseEvent):
        if e.button() == Qt.MouseButton.LeftButton:
            x, y = self._to_hid(e.pos())
            _send_touch({"type": "tap", "x": x, "y": y})

    def keyPressEvent(self, e: QKeyEvent):
        hid = _KEY_MAP.get(e.key())
        if hid and hid not in self._held:
            self._held.add(hid)
            self._flush_keys(e.modifiers())

    def keyReleaseEvent(self, e: QKeyEvent):
        hid = _KEY_MAP.get(e.key())
        if hid:
            self._held.discard(hid)
            self._flush_keys(e.modifiers())

    def _flush_keys(self, mods):
        mcs = [v for k, v in _MOD_MAP.items() if mods & k]
        _fire("/key", {"usages": mcs + list(self._held)})

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

# ── Wi-Fi 연결 다이얼로그 ──────────────────────────────────────────────────────
class WiFiDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Wi-Fi / 블루투스 연결")
        self.setModal(True)
        self.setMinimumWidth(340)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.addWidget(QLabel("iPhone IP 주소를 입력하세요.\n(iPhone: 설정 → Wi-Fi → 연결된 네트워크 → IP 주소)"))
        self._ip = QLineEdit()
        self._ip.setPlaceholderText("예) 192.168.0.5")
        layout.addWidget(self._ip)
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def ip(self) -> str:
        return self._ip.text().strip()

# ── 앱 목록 다이얼로그 ────────────────────────────────────────────────────────
class AppLauncherDialog(QDialog):
    launch_requested = pyqtSignal(str, list)   # bundle_id, url_schemes

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("앱 실행")
        self.setMinimumSize(380, 560)
        self._apps: list[dict] = []

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        self._search = QLineEdit()
        self._search.setPlaceholderText("앱 검색 (이름 또는 Bundle ID)...")
        self._search.textChanged.connect(self._filter)
        layout.addWidget(self._search)

        self._status = QLabel("앱 목록 로딩 중...")
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status.setStyleSheet("color:#888; padding:20px;")
        layout.addWidget(self._status)

        self._list = QListWidget()
        self._list.setStyleSheet(
            "QListWidget{background:#111;border:1px solid #333;border-radius:6px;}"
            "QListWidget::item{padding:8px 12px;border-bottom:1px solid #222;}"
            "QListWidget::item:selected{background:#0a84ff;color:#fff;}"
            "QListWidget::item:hover{background:#1c1c1e;}"
        )
        self._list.setHidden(True)
        self._list.itemDoubleClicked.connect(self._on_launch)
        layout.addWidget(self._list)

        hint = QLabel("더블클릭으로 실행  |  Developer Mode가 활성화되어 있어야 합니다")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet("color:#555;font-size:11px;padding-top:4px;")
        layout.addWidget(hint)

    def set_apps(self, apps: list[dict]):
        self._apps = apps
        n = len(apps)
        self._status.setText(f"앱 {n}개" if n else "설치된 앱 없음")
        self._list.setHidden(False)
        self._filter(self._search.text())

    def set_error(self, msg: str):
        self._status.setText(f"오류: {msg}")

    def _filter(self, text: str):
        self._list.clear()
        t = text.lower()
        for app in self._apps:
            name = app.get('CFBundleDisplayName') or app.get('CFBundleName') or '알 수 없음'
            bid  = app.get('CFBundleIdentifier', '')
            if not t or t in name.lower() or t in bid.lower():
                item = QListWidgetItem(f"{name}\n{bid}")
                item.setData(Qt.ItemDataRole.UserRole, app)
                self._list.addItem(item)

    def _on_launch(self, item: QListWidgetItem):
        app = item.data(Qt.ItemDataRole.UserRole)
        bid = app.get('CFBundleIdentifier', '')
        schemes: list[str] = []
        for ut in (app.get('CFBundleURLTypes') or []):
            schemes.extend(ut.get('CFBundleURLSchemes', []))
        if bid:
            self.launch_requested.emit(bid, schemes)

# ── 메인 윈도우 ───────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self, bridge: Bridge, server: StreamServer, video: VideoController):
        super().__init__()
        self._bridge = bridge
        self._server = server
        self._video  = video
        self._usb  = USBConnector(
            on_ready =lambda name: bridge.device_connected.emit(name),
            on_lost  =lambda: bridge.device_disconnected.emit(),
            on_status=lambda msg: bridge.status_changed.emit(msg),
        )
        self._wifi = WiFiConnector(
            on_ready =lambda name: bridge.device_connected.emit(name),
            on_lost  =lambda: bridge.device_disconnected.emit(),
            on_status=lambda msg: bridge.status_changed.emit(msg),
        )
        self._active: USBConnector | WiFiConnector = self._usb
        self._device_name = ""
        self._conn_mode   = "USB"
        self._fps_cnt     = 0
        self._app_mgr     = AppManager()
        self._app_dlg: Optional[AppLauncherDialog] = None

        self.setWindowTitle("iBridge")
        self.setStyleSheet("QMainWindow { background: #000; }")

        self._screen = ScreenWidget()
        self._phone  = PhoneFrame(self._screen)
        self.setCentralWidget(self._phone)

        self._build_menu()
        self._wire()

        self.resize(430, 900)

        self._fps_timer = QTimer()
        self._fps_timer.setInterval(1000)
        self._fps_timer.timeout.connect(self._tick)

    # ── 메뉴 바 ───────────────────────────────────────────────────────────────
    def _build_menu(self):
        mb = self.menuBar()
        mb.setStyleSheet(
            "QMenuBar { background:#111; color:#ddd; }"
            "QMenuBar::item:selected { background:#333; }"
            "QMenu { background:#1c1c1e; color:#ddd; border:1px solid #444; }"
            "QMenu::item:selected { background:#0a84ff; }"
        )

        # 연결
        m_conn = mb.addMenu("연결")
        self._act_usb = QAction("USB 자동 연결", self, checkable=True, checked=True)
        self._act_usb.triggered.connect(self._use_usb)
        m_conn.addAction(self._act_usb)

        act_wifi = QAction("Wi-Fi / 블루투스...", self)
        act_wifi.triggered.connect(self._show_wifi_dlg)
        m_conn.addAction(act_wifi)

        m_conn.addSeparator()
        act_disc = QAction("연결 끊기", self)
        act_disc.triggered.connect(lambda: schedule(self._disconnect()))
        m_conn.addAction(act_disc)

        # 앱 실행
        m_app = mb.addMenu("앱")
        act_apps = QAction("앱 목록...", self)
        act_apps.setShortcut(QKeySequence("Ctrl+A"))
        act_apps.triggered.connect(self._show_app_list)
        m_app.addAction(act_apps)

        # 버튼 (키보드 단축키)
        m_btn = mb.addMenu("버튼")
        _BTN_SHORTCUTS = [
            ("Home",       "home",         "Ctrl+H"),
            ("잠금",       "lock",         "Ctrl+L"),
            ("볼륨 올리기","volume-up",    "Ctrl+Up"),
            ("볼륨 내리기","volume-down",  "Ctrl+Down"),
            ("음소거",     "mute",         "Ctrl+M"),
            ("Siri",       "siri",         "Ctrl+Shift+S"),
        ]
        for label, name, sc in _BTN_SHORTCUTS:
            act = QAction(label, self)
            act.setShortcut(QKeySequence(sc))
            act.triggered.connect(
                lambda _, n=name: _fire("/button", {"name": n, "state": "press"})
            )
            m_btn.addAction(act)

    # ── 시그널 연결 ───────────────────────────────────────────────────────────
    def _wire(self):
        self._bridge.frame_ready.connect(self._on_frame)
        self._bridge.device_connected.connect(self._on_up)
        self._bridge.device_disconnected.connect(self._on_down)
        self._bridge.status_changed.connect(self._on_status)
        self._bridge.server_ready.connect(self._on_srv_ready)
        self._bridge.apps_loaded.connect(self._on_apps_loaded)
        self._bridge.launch_error.connect(self._on_launch_error)

    # ── 이벤트 핸들러 ─────────────────────────────────────────────────────────
    def _on_frame(self, data: bytes, w: int, h: int):
        self._screen.set_frame(data, w, h)
        self._fps_cnt += 1

    def _on_up(self, name: str):
        self._device_name = name
        self._update_title()
        rsd = self._active.current_rsd
        self._app_mgr.set_rsd(rsd)
        schedule(self._server.start(rsd, on_ready=lambda: self._bridge.server_ready.emit()))

    def _on_down(self):
        self._device_name = ""
        self.setWindowTitle("iBridge")
        self._fps_timer.stop()
        self._screen.clear()
        self._app_mgr.clear()
        if self._app_dlg:
            self._app_dlg.set_error("기기 연결이 끊어졌습니다")
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
        self._update_title(fps)

    def _update_title(self, fps: int = 0):
        parts = ["iBridge"]
        if self._device_name:
            parts.append(self._device_name)
            parts.append(self._conn_mode)
        if fps:
            parts.append(f"{fps} fps")
        self.setWindowTitle(" · ".join(parts))

    # ── 앱 목록 ───────────────────────────────────────────────────────────────
    def _show_app_list(self):
        if not self._app_mgr.available:
            QMessageBox.information(self, "iBridge", "iPhone을 먼저 연결하세요.")
            return
        if self._app_dlg is None:
            self._app_dlg = AppLauncherDialog(self)
            self._app_dlg.launch_requested.connect(self._on_launch_app)
        self._app_dlg.set_apps([])
        self._app_dlg._status.setText("앱 목록 로딩 중...")
        self._app_dlg._list.setHidden(True)
        self._app_dlg.show()
        self._app_dlg.raise_()
        schedule(self._async_load_apps())

    async def _async_load_apps(self):
        loop = asyncio.get_event_loop()
        try:
            apps = await loop.run_in_executor(None, self._app_mgr.list_apps)
            self._bridge.apps_loaded.emit(apps)
        except Exception as e:
            self._bridge.status_changed.emit(f"앱 목록 로드 실패: {e}")
            self._bridge.apps_loaded.emit([])

    def _on_apps_loaded(self, apps: list):
        if self._app_dlg:
            self._app_dlg.set_apps(apps)

    def _on_launch_app(self, bundle_id: str, url_schemes: list):
        schedule(self._async_launch(bundle_id, url_schemes))

    async def _async_launch(self, bundle_id: str, url_schemes: list):
        loop = asyncio.get_event_loop()
        err = await loop.run_in_executor(
            None, self._app_mgr.launch_app, bundle_id, url_schemes
        )
        if err:
            self._bridge.launch_error.emit(err)

    def _on_launch_error(self, msg: str):
        QMessageBox.warning(self, "앱 실행 실패", msg)

    # ── 연결 전환 ─────────────────────────────────────────────────────────────
    def _use_usb(self):
        self._act_usb.setChecked(True)
        self._conn_mode = "USB"
        schedule(self._switch(self._usb))

    def _show_wifi_dlg(self):
        dlg = WiFiDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.ip():
            self._act_usb.setChecked(False)
            self._conn_mode = "Wi-Fi"
            ip = dlg.ip()
            schedule(self._switch_wifi(ip))

    async def _switch(self, connector):
        await self._teardown()
        await self._active.stop()
        self._active = connector
        await connector.start()

    async def _switch_wifi(self, ip: str):
        await self._teardown()
        await self._active.stop()
        self._active = self._wifi
        await self._wifi.connect(ip)

    async def _disconnect(self):
        await self._teardown()
        await self._active.stop()

    async def _teardown(self):
        await self._video.stop()
        await self._server.stop()

    async def _shutdown(self):
        await self._teardown()
        await self._usb.stop()
        await self._wifi.stop()

    def closeEvent(self, e):
        self._fps_timer.stop()
        try:
            schedule(self._shutdown()).result(timeout=3.0)
        except Exception:
            pass
        e.accept()

# ── 진입점 ────────────────────────────────────────────────────────────────────
def main():
    logging.basicConfig(level=logging.WARNING)

    # Windows: 콘솔 창 숨기기 (PyInstaller --windowed와 동일 효과)
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)

    _start_bg()
    threading.Thread(target=_touch_worker, daemon=True, name="TouchHTTP").start()

    # Windows High-DPI 스케일링 활성화
    if sys.platform == "win32":
        from PyQt6.QtCore import Qt as _Qt2
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            _Qt2.HighDpiScaleFactorRoundingPolicy.PassThrough
        )

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
