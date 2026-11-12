@echo off
title Z.G ERP - نظام إدارة المخازن والمبيعات (سطح المكتب)
cd /d "%~dp0"

echo التحقق من المكتبات المطلوبة...
pip show flask >nul 2>&1
if errorlevel 1 (
    echo تثبيت المكتبات الأساسية لأول مرة...
    pip install -r requirements.txt
)

pip show pywebview >nul 2>&1
if errorlevel 1 (
    echo تثبيت مكتبة تطبيق سطح المكتب لأول مرة...
    pip install -r requirements-desktop.txt
)

echo تشغيل النظام كتطبيق سطح مكتب...
python desktop_launcher.py

pause
