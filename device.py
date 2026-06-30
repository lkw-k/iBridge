"""iPhone 디바이스 연결 및 스트림 서버 관리"""
import asyncio
import contextlib
import logging

logger = logging.getLogger(__name__)

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8080
TUNNELD_ADDR = ("127.0.0.1", 49151)
RSD_PORT = 58783
POLL_SECS = 2.0


async def _safe_close(rsd):
    with contextlib.suppress(Exception):
        if hasattr(rsd, "aclose"):
            await rsd.aclose()
        elif hasattr(rsd, "close"):
            r = rsd.close()
            if asyncio.iscoroutine(r):
                await r
        elif hasattr(rsd, "__aexit__"):
            await rsd.__aexit__(None, None, None)


def _device_name(rsd) -> str:
    try:
        return rsd.all_values.get("DeviceName", rsd.product_type)
    except Exception:
        return "iPhone"


# ── USB 연결 (tunneld 기반) ─────────────────────────────────────────────────────
class USBConnector:
    def __init__(self, on_ready, on_lost, on_status):
        self._on_ready = on_ready    # (device_name: str)
        self._on_lost = on_lost      # ()
        self._on_status = on_status  # (msg: str)
        self.current_rsd = None
        self._udid = None
        self._task = None

    async def start(self):
        self._task = asyncio.create_task(self._poll())

    async def stop(self):
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._drop()

    async def _drop(self):
        rsd, self.current_rsd, self._udid = self.current_rsd, None, None
        if rsd:
            await _safe_close(rsd)

    async def _poll(self):
        try:
            from pymobiledevice3.tunneld import get_tunneld_devices
        except ImportError:
            from pymobiledevice3.tunneld.api import get_tunneld_devices
        try:
            from pymobiledevice3.exceptions import TunneldConnectionError
        except ImportError:
            TunneldConnectionError = ConnectionRefusedError

        while True:
            try:
                rsds = await get_tunneld_devices(TUNNELD_ADDR)
            except TunneldConnectionError:
                self._on_status(
                    "tunneld 미실행\n"
                    "Mac: python -m pymobiledevice3 remote start-tunnel\n"
                    "Windows: 관리자 권한으로 동일 명령 실행"
                )
                if self._udid:
                    await self._drop()
                    self._on_lost()
                await asyncio.sleep(POLL_SECS)
                continue
            except Exception as e:
                logger.debug("USB poll: %s", e)
                await asyncio.sleep(POLL_SECS)
                continue

            if not rsds:
                if self._udid:
                    await self._drop()
                    self._on_lost()
                    self._on_status("iPhone을 USB로 연결하세요")
                await asyncio.sleep(POLL_SECS)
                continue

            rsd = rsds[0]
            for extra in rsds[1:]:
                await _safe_close(extra)

            if rsd.udid == self._udid:
                await _safe_close(rsd)
                await asyncio.sleep(POLL_SECS)
                continue

            if self._udid:
                await self._drop()
                self._on_lost()

            self.current_rsd = rsd
            self._udid = rsd.udid
            self._on_ready(_device_name(rsd))
            await asyncio.sleep(POLL_SECS)


# ── Wi-Fi / 블루투스 직접 연결 ────────────────────────────────────────────────────
class WiFiConnector:
    def __init__(self, on_ready, on_lost, on_status):
        self._on_ready = on_ready
        self._on_lost = on_lost
        self._on_status = on_status
        self.current_rsd = None
        self._task = None

    async def connect(self, host: str, port: int = RSD_PORT):
        await self.stop()
        self._task = asyncio.create_task(self._try(host, port))

    async def stop(self):
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self.current_rsd:
            await _safe_close(self.current_rsd)
            self.current_rsd = None

    async def _try(self, host: str, port: int):
        from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
        try:
            self._on_status(f"{host} 연결 중...")
            rsd = RemoteServiceDiscoveryService((host, port))
            await rsd.connect()
            self.current_rsd = rsd
            self._on_ready(_device_name(rsd))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self._on_status(f"연결 실패: {e}")
            self._on_lost()


# ── ScreenStreamServer 래퍼 ────────────────────────────────────────────────────
class StreamServer:
    def __init__(self):
        self._task = None

    async def start(self, rsd, on_ready):
        await self.stop()
        from pymobiledevice3.remote.core_device.screen_stream import ScreenStreamServer
        server = ScreenStreamServer(
            rsd,
            bind=SERVER_HOST,
            http_port=SERVER_PORT,
            display_id=1,
            audio_default_on=False,
        )
        self._task = asyncio.create_task(self._run(server))
        await asyncio.sleep(0.3)
        on_ready()

    async def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None

    async def _run(self, server):
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await server.serve()

    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done())
