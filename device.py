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


# ── 앱 목록 조회 및 실행 ───────────────────────────────────────────────────────
class AppManager:
    """iPhone 설치 앱 조회 및 실행 (InstallationProxy + DVT)"""

    def __init__(self):
        self._rsd = None

    def set_rsd(self, rsd):
        self._rsd = rsd

    def clear(self):
        self._rsd = None

    @property
    def available(self) -> bool:
        return self._rsd is not None

    def _ld(self):
        from pymobiledevice3.lockdown import create_using_remote
        return create_using_remote(self._rsd)

    def list_apps(self) -> list[dict]:
        """사용자 앱 목록 반환 (동기). run_in_executor로 호출할 것."""
        from pymobiledevice3.services.installation_proxy import InstallationProxyService
        ld = self._ld()
        proxy = InstallationProxyService(lockdown=ld)
        apps = list(proxy.browse(
            application_type='User',
            attributes=[
                'CFBundleDisplayName',
                'CFBundleIdentifier',
                'CFBundleName',
                'CFBundleURLTypes',
            ],
        ))
        return sorted(
            apps,
            key=lambda a: (a.get('CFBundleDisplayName') or a.get('CFBundleName') or '').lower(),
        )

    def launch_app(self, bundle_id: str, url_schemes: list = None) -> str:
        """앱 실행. 성공 시 '' 반환, 실패 시 오류 메시지 반환."""
        # 1차: DVT ProcessControl (Developer Mode 필요)
        try:
            ld = self._ld()
            from pymobiledevice3.services.dvt.dvt_secure_socket_proxy import DvtSecureSocketProxyService
            from pymobiledevice3.services.dvt.instruments.process_control import ProcessControl
            with DvtSecureSocketProxyService(lockdown=ld) as dvt:
                pc = ProcessControl(dvt)
                pc.launch(bundle_id=bundle_id, kill_existing=True)
            return ""
        except Exception as e:
            logger.debug("DVT launch failed for %s: %s", bundle_id, e)

        # 2차: URL 스킴 (SpringBoard open_url)
        if url_schemes:
            try:
                ld = self._ld()
                from pymobiledevice3.services.springboard import SpringBoardServicesService
                sbs = SpringBoardServicesService(lockdown=ld)
                if hasattr(sbs, 'open_url'):
                    sbs.open_url(f"{url_schemes[0]}://")
                    return ""
            except Exception as e:
                logger.debug("URL scheme launch failed: %s", e)

        return (
            "앱 실행에 실패했습니다.\n"
            "iPhone: 설정 → 개인정보 보호 및 보안 → 개발자 모드 활성화 후 재시도하세요."
        )
