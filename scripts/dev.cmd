@echo off
REM Windows 下 poetry.exe 可能被「应用程序控制策略」拦截。
REM 用 python -m poetry 走模块入口，不依赖被拦截的 poetry.exe。
cd /d "%~dp0"
python -m poetry run zhongji-dev %*
