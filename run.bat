@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ОШИБКА] Бот ещё не установлен.
    echo Сначала запустите setup.bat, затем снова run.bat.
    echo.
    pause
    exit /b 1
)

if not exist ".env" (
    echo [ОШИБКА] Нет файла .env с настройками.
    echo Запустите setup.bat — он создаст .env из образца.
    echo.
    pause
    exit /b 1
)

echo Запуск бота. Чтобы остановить — нажмите Ctrl+C.
echo Логи пишутся в logs\bot.log
echo.

".venv\Scripts\python.exe" -m bot.main
set "CODE=%ERRORLEVEL%"

if not "%CODE%"=="0" (
    echo.
    echo Бот завершился с кодом %CODE%.
    echo Причина выше или в logs\bot.log
    echo.
    pause
)

exit /b %CODE%
