import os
import sys


def get_app_dir():
    """المجلد الذي توضع فيه البيانات الدائمة (قاعدة البيانات، النسخ الاحتياطية).
    عند التشغيل كملف .exe (مُجمَّع عبر PyInstaller)، نستخدم مجلد الملف
    التنفيذي نفسه بدل المجلد المؤقت الذي يُحذف بعد إغلاق البرنامج، حتى لا
    تُفقد قاعدة البيانات بين مرات التشغيل."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.abspath(os.path.dirname(__file__))


def get_resource_dir():
    """المجلد الذي توجد فيه الملفات الثابتة المرفقة (templates/static/fonts).
    عند التشغيل كـ .exe بصيغة onefile، تُستخرج هذه الملفات إلى مجلد مؤقت
    يشير إليه PyInstaller عبر sys._MEIPASS."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.abspath(os.path.dirname(__file__))


BASE_DIR = get_app_dir()
RESOURCE_DIR = get_resource_dir()


class Config:
    BASE_DIR = BASE_DIR
    RESOURCE_DIR = RESOURCE_DIR
    SECRET_KEY = os.getenv("ERP_SECRET_KEY", "change-this-secret-key-before-production")
    SQLALCHEMY_DATABASE_URI = "sqlite:///" + os.path.join(BASE_DIR, "database", "erp.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    CURRENCY = "د.ل"          # الدينار الليبي
    COMPANY_NAME = "Z.G ERP"
    LOW_STOCK_THRESHOLD = 5
