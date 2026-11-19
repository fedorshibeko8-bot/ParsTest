@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================
echo   Установка бота ParsBot
echo ============================================
echo.

rem --- Ищем Python 3.11+ -------------------------------------------------
set "PY="
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    where python >nul 2>nul
    if not errorlevel 1 (
        python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
        if not errorlevel 1 set "PY=python"
    )
)
if not defined PY (
    echo [ОШИБКА] Не найден Python 3.11 или новее.
    echo.
    echo Скачайте Python с https://www.python.org/downloads/
    echo При установке обязательно отметьте галочку "Add Python to PATH".
    echo После установки запустите setup.bat ещё раз.
    echo.
    pause
    exit /b 1
)
echo Python найден: %PY%
echo.

rem --- Виртуальное окружение --------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo Создаю виртуальное окружение .venv ...
    %PY% -m venv .venv
    if errorlevel 1 goto fail
) else (
    echo Виртуальное окружение уже есть.
)

echo Обновляю pip ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto fail

echo Устанавливаю зависимости ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail

rem --- Конфигурация и каталоги ------------------------------------------
if not exist ".env" (
    copy /Y ".env.example" ".env" >nul
    echo.
    echo Создан файл .env. Откройте его Блокнотом и заполните:
    echo   BOT_TOKEN, CHANNEL_ID, ADMIN_IDS
    echo Пока там стоит DRY_RUN=1, бот работает безопасно: без публикаций в канал.
) else (
    echo Файл .env уже существует — оставляю без изменений.
)

if not exist "data" mkdir "data"
if not exist "logs" mkdir "logs"

echo.
echo ============================================
echo   Готово!
echo   1. Заполните .env (см. ЗАПУСК.md).
echo   2. Запустите run.bat.
echo ============================================
echo.
pause
exit /b 0

:fail
echo.
echo [ОШИБКА] Установка не завершена. Скопируйте текст выше,
echo чтобы понять причину. Чаще всего помогает проверить интернет
echo и повторить запуск setup.bat.
echo.
pause
exit /b 1
