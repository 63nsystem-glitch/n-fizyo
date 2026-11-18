@echo off
REM N-Fizyo Servis – Windows icin tek dosya program (.exe) olustur.
REM Gerekli: Python 3.12+ (python.org, "Add to PATH" isaretli). Bu dosyayi proje klasorunden calistir:
REM   tools\servis\build_windows.bat
REM Sonuc: dist\N-Fizyo-Servis.exe  (anahtar .exe'nin icinde DEGIL; %APPDATA%\NSystem\ altinda durur)
setlocal
cd /d "%~dp0\..\.."
py -m pip install --upgrade pip >nul
py -m pip install "cryptography>=42" "pyinstaller>=6" || goto :error
py -m PyInstaller --noconfirm --onefile --windowed --name N-Fizyo-Servis ^
  --paths . --paths tools\servis ^
  --hidden-import nfizyo.agent.servicelink --hidden-import nfizyo.agent.signing ^
  tools\servis\n_fizyo_servis.py || goto :error
echo.
echo Hazir: dist\N-Fizyo-Servis.exe
goto :eof
:error
echo HATA: .exe olusturulamadi.
exit /b 1
