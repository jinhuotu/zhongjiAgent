# Windows 下 poetry.exe 可能被 AppLocker / 应用程序控制策略拦截。
# 本脚本通过 python -m poetry 启动本地双进程（API + chat-worker）。
Set-Location $PSScriptRoot\..
python -m poetry run zhongji-dev @args
