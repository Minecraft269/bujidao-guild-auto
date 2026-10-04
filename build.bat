@echo off
chcp 936 >nul
rem ============================================================
rem  布吉岛公会管理脚本 - 打包脚本(单文件模式)
rem  将 main.py 打包为单文件 .exe(尽量减小体积)
rem  产物: dist\布吉岛公会管理脚本.exe(约 18MB)
rem  依赖: Python 3.8+ + pip(脚本自动检查/安装 pyinstaller)
rem ============================================================
cd /d "%~dp0"

rem ====== 可自定义输出文件名（不含扩展名） ======
set APP_NAME=布吉岛公会管理脚本

echo === [1/4] 检查并安装打包依赖 pyinstaller ===
pip show pyinstaller >nul 2>&1 || pip install pyinstaller
if errorlevel 1 goto :err

echo === [2/4] 清理旧打包产物 ===
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist "%APP_NAME%.spec" del /f /q "%APP_NAME%.spec"

echo === [3/4] 开始打包(单文件;排除未使用的重模块 cv2/PyQt5/numpy/tkinter) ===
pyinstaller --onefile --clean --noconfirm ^
  --name "%APP_NAME%" ^
  --exclude-module tkinter ^
  --exclude-module cv2 ^
  --exclude-module numpy ^
  --exclude-module PyQt5 ^
  --exclude-module matplotlib ^
  --exclude-module scipy ^
  --exclude-module IPython ^
  --exclude-module pandas ^
  --collect-all winsdk ^
  main.py
if errorlevel 1 goto :err

echo === [4/4] 完成 ===
for %%F in ("dist\%APP_NAME%.exe") do echo 产物: dist\%APP_NAME%.exe (%%~zF 字节)
echo.
echo 使用: 双击 %APP_NAME%.exe;首次运行生成 config.json(带逐键中文注释),
echo 请先查看并修改配置,再重新启动;config.json 存在后不再提示。
echo 提示: 若首次运行被杀软拦截(报 Failed to load Python DLL),请在杀软中信任该 exe 后重试
pause
exit /b 0

:err
echo.
echo 打包失败,请检查上方错误信息(常见:缺少依赖、被杀软拦截)
pause
exit /b 1