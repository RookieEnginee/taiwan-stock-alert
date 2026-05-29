@echo off
chcp 65001 >nul
echo ================================================
echo  台股資料庫 — 每日自動更新排程設定
echo ================================================
echo.

:: 取得本腳本所在目錄（即專案根目錄）
set PROJ_DIR=%~dp0
set SCRIPT=%PROJ_DIR%stock_db.py
set TASK_NAME=TaiwanStockDB_DailyUpdate
set LOG_FILE=%PROJ_DIR%update_log.txt

:: 確認 Python 可用
python --version >nul 2>&1
if errorlevel 1 (
    echo [錯誤] 找不到 Python，請先安裝 Python 並確認已加入 PATH
    pause
    exit /b 1
)

echo [1/3] 初始化資料庫...
python "%SCRIPT%" --init
if errorlevel 1 (
    echo [錯誤] 初始化失敗
    pause
    exit /b 1
)
echo       資料庫初始化完成

echo.
echo [2/3] 執行首次資料更新（首次可能需要 2~5 分鐘）...
python "%SCRIPT%"
if errorlevel 1 (
    echo [警告] 首次更新有部分錯誤，可繼續設定排程（資料可能不完整）
)
echo       首次更新完成

echo.
echo [3/3] 設定 Windows 排程（每日 16:30 收盤後自動執行）...

:: 刪除舊排程（若存在）
schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1

:: 建立新排程：每日 16:30 執行
schtasks /create ^
  /tn "%TASK_NAME%" ^
  /tr "python \"%SCRIPT%\" >> \"%LOG_FILE%\" 2>&1" ^
  /sc daily ^
  /st 16:30 ^
  /rl highest ^
  /f

if errorlevel 1 (
    echo [錯誤] 排程建立失敗，請以「系統管理員」身分重新執行此批次檔
    pause
    exit /b 1
)

echo.
echo ================================================
echo  設定完成！
echo.
echo  排程名稱: %TASK_NAME%
echo  執行時間: 每日 16:30（台股收盤後）
echo  更新日誌: %LOG_FILE%
echo.
echo  手動執行更新: python "%SCRIPT%"
echo  查看DB狀態:   python "%SCRIPT%" --status
echo  管理排程:     工作排程器 ^> 工作排程器程式庫
echo ================================================
echo.
pause
