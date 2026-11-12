"""
تشغيل نظام إدارة المخازن والمبيعات كتطبيق سطح مكتب (نافذة مستقلة) بدل المتصفح.

هذا الملف لا يغيّر أي شيء في نظام الويب (app.py) — فقط:
1) يشغّل خادم Flask في الخلفية على المنفذ المحلي 127.0.0.1.
2) يفتح نافذة سطح مكتب حقيقية (بدون شريط عنوان متصفح) تعرض نفس واجهة النظام
   عبر مكتبة pywebview، فيبدو النظام تطبيق سطح مكتب مستقل تمامًا.

طريقة التشغيل:
    pip install -r requirements-desktop.txt
    python desktop_launcher.py

أو على ويندوز: شغّل ملف start_desktop.bat

إصلاح مشكلة الطباعة:
    يتم تجاوز window.open() في pywebview حتى تبقى صفحة الطباعة داخل
    نافذة التطبيق بدلاً من فتحها في المتصفح الافتراضي للنظام.
"""
import os
import socket
import threading
import time
import logging
import traceback
from logging.handlers import RotatingFileHandler

from config import Config
from app import app, init_db

HOST = "127.0.0.1"
PORT = int(os.getenv("PORT", "5000"))
WINDOW_TITLE = "نظام إدارة المخازن والمبيعات"

# ---- سجل أخطاء مخصص لمرحلة الإقلاع نفسها (قبل حتى أن يجهز سجل Flask) ----
_LOG_DIR = os.path.join(Config.BASE_DIR, "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
_launcher_logger = logging.getLogger("desktop_launcher")
_launcher_logger.setLevel(logging.INFO)
_handler = RotatingFileHandler(os.path.join(_LOG_DIR, "erp.log"), maxBytes=2_000_000,
                                backupCount=3, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_launcher_logger.addHandler(_handler)


def _show_error_dialog(title, message):
    """يعرض رسالة خطأ في نافذة حقيقية بدل الاختفاء الصامت (لا توجد نافذة طرفية
    في نسخة .exe المُجمَّعة بخيار console=False)."""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
    except Exception:
        # كملاذ أخير إن تعذر حتى فتح tkinter
        _launcher_logger.error("تعذر عرض نافذة الخطأ أيضًا: %s", message)

# ---- JavaScript مُحقَن في كل صفحة ----
# يُعيد توجيه window.open() داخل نافذة pywebview بدل فتح المتصفح،
# ويُضيف window.print() مباشرةً بعد تحميل صفحة الطباعة.
_INJECT_JS = """
(function() {
    // تجاوز window.open حتى لا يفتح المتصفح الخارجي
    var _originalOpen = window.open;
    window.open = function(url, name, features) {
        if (url && url !== '' && url !== 'about:blank') {
            // انتقل داخل نافذة pywebview بدلاً من المتصفح
            window.location.href = url;
            return window;
        }
        return _originalOpen.call(window, url, name, features);
    };

    // إذا كانت هذه صفحة طباعة (invoice_print) افتح نافذة الطباعة تلقائياً
    if (window.location.pathname.indexOf('/print') !== -1 ||
        window.location.pathname.indexOf('/invoice_print') !== -1) {
        window.addEventListener('load', function() {
            setTimeout(function() { window.print(); }, 600);
        });
    }
})();
"""


def _wait_for_server(host, port, timeout=15):
    """ينتظر حتى يصبح الخادم جاهزًا لاستقبال الاتصالات قبل فتح النافذة."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def main():
    os.makedirs(os.path.join(Config.BASE_DIR, "database"), exist_ok=True)
    _launcher_logger.info("=== بدء تشغيل التطبيق ===")

    try:
        init_db()
    except Exception:
        tb = traceback.format_exc()
        _launcher_logger.error("فشل تهيئة قاعدة البيانات:\n%s", tb)
        _show_error_dialog("خطأ في تشغيل البرنامج",
                            "تعذر تهيئة قاعدة البيانات عند بدء التشغيل.\n"
                            "التفاصيل الكاملة محفوظة في ملف logs/erp.log بجانب البرنامج.")
        return

    try:
        from backup_service import start_auto_backup
        start_auto_backup(app, interval_minutes=int(os.getenv("BACKUP_INTERVAL_MINUTES", "60")))
    except Exception as exc:
        _launcher_logger.warning("تعذر تشغيل النسخ الاحتياطي التلقائي: %s", exc)

    server_error = {}

    def _run_server_safe():
        try:
            app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)
        except Exception:
            server_error["trace"] = traceback.format_exc()
            _launcher_logger.error("انهار خادم Flask أثناء التشغيل:\n%s", server_error["trace"])

    server_thread = threading.Thread(target=_run_server_safe, daemon=True)
    server_thread.start()

    if not _wait_for_server(HOST, PORT):
        detail = server_error.get("trace", "لم يستجب الخادم المحلي خلال المهلة المحددة "
                                             "(قد يكون المنفذ 5000 مستخدمًا من برنامج آخر، "
                                             "أو برنامج حماية/جدار حماية يمنع الاتصال).")
        _launcher_logger.error("تعذر تشغيل الخادم المحلي على %s:%s\n%s", HOST, PORT, detail)
        _show_error_dialog("تعذر تشغيل الخادم المحلي",
                            "لم يتمكن البرنامج من تشغيل الخادم الداخلي على المنفذ "
                            f"{PORT}.\nتحقق من:\n"
                            "- عدم وجود نسخة أخرى من البرنامج شغّالة (تحقّق من مدير المهام).\n"
                            "- عدم حجب برنامج الحماية/جدار الحماية للاتصال على localhost.\n\n"
                            "التفاصيل الكاملة محفوظة في ملف logs/erp.log بجانب البرنامج.")
        return

    try:
        import webview
    except ImportError:
        _launcher_logger.error("مكتبة pywebview غير مثبتة.")
        _show_error_dialog("مكتبة مفقودة",
                            "مكتبة pywebview غير مثبتة. الرجاء تشغيل:\n"
                            "pip install -r requirements-desktop.txt")
        return

    # ---- إصلاح جوهري: تفعيل السماح بتنزيل الملفات داخل نافذة سطح المكتب ----
    # افتراضيًا pywebview يمنع أي تنزيل (Excel/PDF) بصمت دون أي خطأ ظاهر، ودون فتح
    # نافذة "حفظ باسم" — وهو سبب عدم عمل أزرار التصدير في نسخة سطح المكتب سابقًا.
    try:
        webview.settings["ALLOW_DOWNLOADS"] = True
    except Exception as exc:
        _launcher_logger.warning("تعذر تفعيل إعداد ALLOW_DOWNLOADS (%s). قد لا تعمل أزرار التصدير.", exc)

    win = webview.create_window(
        WINDOW_TITLE,
        url=f"http://{HOST}:{PORT}",
        width=1280,
        height=800,
        min_size=(1000, 650),
        confirm_close=True,
    )

    # ---- إصلاح الطباعة: حقن JS في كل صفحة بعد تحميلها ----
    def on_loaded():
        try:
            win.evaluate_js(_INJECT_JS)
        except Exception as exc:
            # لا نوقف التطبيق إذا فشل الحقن في صفحة معينة
            _launcher_logger.warning("تحذير حقن JS: %s", exc)

    win.events.loaded += on_loaded

    try:
        # edgechromium صراحةً: بدونه قد يرجع pywebview بصمت لمحرك Internet Explorer القديم على جهاز
        # ليس عليه WebView2، فتظهر الواجهة مكسورة. فإن لم يتوفر، نفتح المتصفح بدلها (انظر except).
        _launcher_logger.info("تم فتح نافذة سطح المكتب بنجاح.")
        webview.start(gui="edgechromium")
    except Exception:
        # جهاز جديد بلا WebView2 Runtime: البرنامج يبقى يعمل من المتصفح الافتراضي بدل أن يختفي
        _launcher_logger.error("تعذر فتح نافذة البرنامج، سيُفتح في المتصفح:\n%s", traceback.format_exc())
        import webbrowser
        webbrowser.open(f"http://{HOST}:{PORT}")
        _show_error_dialog("تنبيه",
                            "تعذر فتح نافذة البرنامج على هذا الجهاز (ينقصه Microsoft Edge WebView2 Runtime).\n"
                            "فُتح البرنامج في المتصفح بدلًا منها ويعمل بشكل كامل.\n"
                            "اترك هذه الرسالة مفتوحة طوال العمل؛ إغلاقها يغلق البرنامج.\n\n"
                            "لتثبيت WebView2 (مجاني من مايكروسوفت): ابحث عن WebView2 Runtime وثبّته.")


if __name__ == "__main__":
    main()