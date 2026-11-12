@echo off
title Z.G ERP - نظام إدارة المخازن والمبيعات
cd /d "%~dp0"

echo التحقق من المكتبات المطلوبة...
pip show flask >nul 2>&1
if errorlevel 1 (
    echo تثبيت المكتبات المطلوبة لأول مرة...
    pip install -r requirements.txt
)

echo تشغيل الخادم...
start "" http://127.0.0.1:5000
python app.py

pause
