@echo off
REM Nexus review bot — runs only while this PC is on (by design).
REM Auto-restarts on crash; close the window to stop the bot.
cd /d D:\Projects\Nexus-Think-Tank
:restart
python eval\nexus_bot.py
echo Bot exited, restarting in 10s... (Ctrl+C to stop)
timeout /t 10 >nul
goto restart
