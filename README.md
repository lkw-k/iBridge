# iBridge

macOS Sequoia의 **iBridgeing**과 동일한 기능을 제공하는 크로스플랫폼 네이티브 데스크톱 앱.  
Mac과 Windows(갤럭시 북 등)에서 iPhone을 실시간으로 제어할 수 있습니다.

---

## 기능

- **실시간 화면** — HEVC/H.265 하드웨어 가속 디코딩 (30–60fps)
- **터치 제어** — 마우스 클릭/드래그 → iPhone 터치 입력
- **키보드 입력** — Mac/Windows 키보드로 iPhone에 텍스트 입력
- **하드웨어 버튼** — 키보드 단축키로 Home / 잠금 / 볼륨 / Siri 제어
- **USB 연결** — USB 케이블로 안정적인 저지연 연결
- **Wi-Fi / 블루투스 연결** — 같은 네트워크에서 무선 연결

---

## 요구 사항

| 항목 | Mac | Windows |
|------|-----|---------|
| Python | 3.11+ | 3.11+ |
| USB 드라이버 | 자동 | [Apple Devices](https://apps.microsoft.com/store/detail/apple-devices/9NP83LWLPZ9K) (Microsoft Store) |
| tunneld | macOS Ventura+ 자동 실행 | 수동 실행 필요 (아래 참고) |

---

## 설치

```bash
git clone https://github.com/[username]/[repository].git
cd [repository]
pip install -r requirements.txt
```

---

## 실행 방법

### Mac

```bash
# tunneld는 macOS Ventura 이상에서 자동으로 실행됩니다
python main.py
```

### Windows (갤럭시 북 등)

```bash
# 1. 관리자 권한 터미널에서 tunneld 실행
python -m pymobiledevice3 remote start-tunnel

# 2. 새 터미널에서 앱 실행
python main.py
```

---

## iPhone 준비

1. iPhone을 USB로 연결
2. "이 컴퓨터를 신뢰합니까?" → **신뢰** 탭
3. 앱이 자동으로 연결됩니다

---

## 키보드 단축키

| 단축키 | 기능 |
|--------|------|
| `Ctrl+H` | Home 버튼 |
| `Ctrl+L` | 잠금 / 전원 버튼 |
| `Ctrl+↑` | 볼륨 올리기 |
| `Ctrl+↓` | 볼륨 내리기 |
| `Ctrl+M` | 음소거 |
| `Ctrl+Shift+S` | Siri |

---

## 파일 구조

```
.
├── main.py        # Qt UI, 비디오 디코딩, 입력 처리
├── device.py      # iPhone 연결 (USB/Wi-Fi), 스트림 서버
└── requirements.txt
```

---

## 기술 스택

- **[pymobiledevice3](https://github.com/doronz88/pymobiledevice3)** — iPhone USB/RPC 통신, HEVC 스트림 서버
- **[PyQt6](https://pypi.org/project/PyQt6/)** — 크로스플랫폼 GUI
- **[PyAV](https://pypi.org/project/av/)** — HEVC 하드웨어 디코딩

---

## 라이선스

MIT License
