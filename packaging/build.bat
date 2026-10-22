@echo off
rem 一键构建安装包：PyInstaller 打包 → 自检 → Inno Setup 生成 packaging\Output\PdfEditor-版本-Setup.exe
rem 需要：项目内 venv（已安装 requirements.txt 与 pyinstaller）、Inno Setup 6
setlocal
chcp 65001 >nul
cd /d "%~dp0.."

echo [1/3] PyInstaller 打包...
venv\python.exe -m PyInstaller packaging\pdfedit.spec --distpath packaging\dist --workpath packaging\build --noconfirm || goto :fail

echo [2/3] 打包后自检...
packaging\dist\PdfEditor\PdfEditor.exe --selftest packaging\build\selftest.txt
type packaging\build\selftest.txt
findstr /x "OK" packaging\build\selftest.txt >nul || goto :fail

echo [3/3] 生成安装程序...
set ISCC="%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist %ISCC% set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
%ISCC% packaging\installer.iss || goto :fail

echo 完成：packaging\Output
exit /b 0

:fail
echo 构建失败
exit /b 1
