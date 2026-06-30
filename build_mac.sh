#!/bin/bash
set -e

echo "📦 의존성 설치 중..."
pip install pyinstaller pyinstaller-hooks-contrib --quiet

echo "🔨 앱 빌드 중 (수 분 소요)..."
pyinstaller \
  --windowed \
  --name "iBridge" \
  --noconfirm \
  --clean \
  --collect-all pymobiledevice3 \
  --collect-all av \
  --collect-all aiohttp \
  --hidden-import requests \
  --hidden-import numpy \
  main.py

echo "🖥  바탕화면에 복사 중..."
cp -r "dist/iBridge.app" ~/Desktop/

echo "✅ 완료! 바탕화면의 'iBridge.app'을 더블클릭해서 실행하세요."
