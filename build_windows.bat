@echo off
chcp 65001 >nul
title iBridge 빌드

echo =============================================
echo  iBridge Windows 앱 빌드
echo =============================================
echo.

:: Python 확인
python --version >nul 2>&1
if errorlevel 1 (
    echo [오류] Python이 설치되지 않았습니다.
    echo https://python.org 에서 Python 3.11 이상을 설치하세요.
    pause & exit /b 1
)

echo [1/3] 의존성 설치 중...
pip install -r requirements.txt --quiet
pip install pyinstaller pyinstaller-hooks-contrib --quiet

echo [2/3] 앱 빌드 중 (3~5분 소요)...
pyinstaller ^
  --windowed ^
  --name "iBridge" ^
  --noconfirm ^
  --clean ^
  --collect-all pymobiledevice3 ^
  --collect-all av ^
  --collect-all aiohttp ^
  --hidden-import requests ^
  --hidden-import numpy ^
  --exclude-module PyQt5 ^
  --exclude-module PySide6 ^
  --exclude-module PySide2 ^
  main.py

if errorlevel 1 (
    echo.
    echo [오류] 빌드 실패. 위 오류 메시지를 확인하세요.
    pause & exit /b 1
)

echo [3/3] 바탕화면에 복사 중...
if exist "%USERPROFILE%\Desktop\iBridge.exe" del "%USERPROFILE%\Desktop\iBridge.exe"
copy "dist\iBridge\iBridge.exe" "%USERPROFILE%\Desktop\iBridge.exe" >nul

echo.
echo =============================================
echo  완료! 바탕화면의 iBridge.exe 를 실행하세요.
echo =============================================
echo.
echo [주의] 처음 실행 전 반드시 아래를 먼저 실행하세요:
echo   관리자 권한 터미널에서:
echo   python -m pymobiledevice3 remote start-tunnel
echo.
pause
