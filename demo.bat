@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   银枢 YinShu - 可信银行智能体
echo   首次运行会自动生成模拟数据，约需 5 秒
echo ============================================
where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 python，请先安装 Python 3.10+ 并加入 PATH
  pause
  exit /b 1
)
python -c "import fastapi" >nul 2>nul
if errorlevel 1 (
  echo [提示] 首次运行需要安装依赖，正在安装...
  python -m pip install -r requirements.txt
)
python start.py
pause
