@echo off
rem One-click refresh: scrape everything (forum included — works from home,
rem where the forum doesn't block us), rebuild the app, push it live.
cd /d "%~dp0"
python kvbl_app\build.py
if errorlevel 1 (echo BUILD FAILED & pause & exit /b 1)
git add -A docs kvbl_app/forum_cache.json kvbl_app/ratings_history.json
git commit -m "Refresh data (local)"
git pull --rebase origin main
if errorlevel 1 (
  git checkout --ours docs/index.html
  git add docs/index.html
  git rebase --continue
)
git push
echo.
echo Done — app updates at https://mgarcell.github.io/jsb/ in ~1 minute.
pause
