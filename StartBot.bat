@echo off

cd /d C:\Users\abhia\btc-ai-trading

start "" cmd /c "python -m app"

start "" cmd /c "cd /d C:\Users\abhia\btc-ai-trading\dashboard && npm run dev"

timeout /t 8 > nul

start http://localhost:3000