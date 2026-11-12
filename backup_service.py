"""النسخ الاحتياطي لـ Z.G ERP:
1) نسخة محلية مضغوطة على نفس الجهاز كل ساعة (لا تحتاج إنترنت)، مع الاحتفاظ بآخر 48 نسخة،
   ونسخة إضافية اختيارية إلى مجلد آخر (فلاشة مثلًا).
2) رفع إلى Google Drive الخاص بالمالك مرة يوميًا، وبعد إغلاق كل وردية، مع الاحتفاظ بآخر 30 نسخة هناك.

الربط بـ Google يتم من متصفح المالك نفسه (OAuth للتطبيقات المكتبية مع PKCE). الإذن المطلوب drive.file فقط:
يرى النظام الملفات التي أنشأها هو فقط، لا باقي ملفات المالك. لا توجد أي مفاتيح أو حسابات داخل الكود.

كل الحالة (مفاتيح الربط، أوقات آخر نسخة، آخر خطأ) محفوظة في جدول الإعدادات settings.
"""
import base64
import hashlib
import os
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timedelta
from urllib.parse import urlencode

import requests

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
DRIVE_ABOUT_URL = "https://www.googleapis.com/drive/v3/about"
DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
DRIVE_FOLDER_NAME = "ZG_ERP_Backups"

LOCAL_KEEP = 48          # عدد النسخ المحلية المحتفظ بها
DRIVE_KEEP = 30          # عدد النسخ المحتفظ بها في Google Drive
DRIVE_EVERY_HOURS = 24   # الرفع التلقائي إلى Drive مرة كل يوم
CHECK_EVERY_SECONDS = 300

_lock = threading.Lock()          # نسخة واحدة في كل مرة (يدوي/تلقائي/بعد إغلاق وردية)
_wake = threading.Event()         # لإيقاظ الخيط فورًا بعد إغلاق وردية


# ---------------------------------------------------------------------------
# الإعدادات (جدول settings) - تُستدعى داخل app.app_context()
# ---------------------------------------------------------------------------
def _get(key, default=""):
    from models import Setting
    return Setting.get(key, default)


def _set(**values):
    from models import Setting, db
    for k, v in values.items():
        Setting.set(k, "" if v is None else str(v))
    db.session.commit()


def _parse_time(raw):
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def backup_dir(app):
    path = os.path.join(app.config["BASE_DIR"], "backups")
    os.makedirs(path, exist_ok=True)
    return path


def _db_path(app):
    return os.path.join(app.config["BASE_DIR"], "database", "erp.db")


def _local_now():
    # اسم الملف بتوقيت الجهاز ليسهل على المالك معرفة تاريخ النسخة
    return datetime.now()


# ---------------------------------------------------------------------------
# النسخة المحلية
# ---------------------------------------------------------------------------
def make_local_backup(app):
    """لقطة متسقة من قاعدة البيانات (SQLite Online Backup، تعمل والنظام شغّال) تُفحص سلامتها ثم تُضغط
    في ملف zip داخل مجلد backups. يعيد (مسار الملف، sha256)."""
    src = _db_path(app)
    if not os.path.exists(src):
        raise RuntimeError("قاعدة البيانات غير موجودة في مجلد database.")
    fd, snap = tempfile.mkstemp(prefix="zg_snap_", suffix=".db")
    os.close(fd)
    try:
        source = sqlite3.connect(src)
        dest = sqlite3.connect(snap)
        try:
            source.backup(dest)
            ok = dest.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            dest.close()
            source.close()
        if ok != "ok":
            raise RuntimeError(f"فحص سلامة النسخة فشل: {ok}")
        name = f"ZG_ERP_{_local_now():%Y-%m-%d_%H-%M}.zip"
        target = os.path.join(backup_dir(app), name)
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(snap, arcname="erp.db")
    finally:
        try:
            os.remove(snap)
        except OSError:
            pass
    with open(target, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    _prune_local(app)
    return target, digest


def _prune_local(app):
    files = sorted(list_local_backups(app), key=lambda f: f["mtime"], reverse=True)
    for f in files[LOCAL_KEEP:]:
        try:
            os.remove(f["path"])
        except OSError:
            pass


def list_local_backups(app):
    folder = backup_dir(app)
    out = []
    for name in os.listdir(folder):
        if name.startswith("ZG_ERP_") and name.endswith(".zip"):
            path = os.path.join(folder, name)
            st = os.stat(path)
            out.append({"name": name, "path": path, "size": st.st_size, "mtime": datetime.fromtimestamp(st.st_mtime)})
    return sorted(out, key=lambda f: f["mtime"], reverse=True)


def _copy_to_extra_folder(path):
    """نسخة إضافية إلى مجلد يحدده المالك (فلاشة/قرص آخر). فشلها لا يُفشل النسخة المحلية."""
    extra = (_get("backup_extra_dir") or "").strip()
    if not extra:
        return None
    if not os.path.isdir(extra):
        return f"المجلد الإضافي غير متاح ({extra}) - هل الفلاشة موصولة؟"
    try:
        shutil.copy2(path, os.path.join(extra, os.path.basename(path)))
        return None
    except OSError as exc:
        return f"تعذر النسخ إلى المجلد الإضافي: {exc}"


# ---------------------------------------------------------------------------
# Google Drive: الربط
# ---------------------------------------------------------------------------
def drive_status():
    return {
        "client_id": _get("gdrive_client_id"),
        "has_secret": bool(_get("gdrive_client_secret")),
        "connected": bool(_get("gdrive_refresh_token")),
        "account": _get("gdrive_account"),
        "folder_id": _get("gdrive_folder_id"),
    }


def build_auth_url(redirect_uri):
    """رابط صفحة موافقة Google. state و code_verifier يُحفظان لحين عودة المتصفح."""
    client_id = _get("gdrive_client_id")
    if not client_id or not _get("gdrive_client_secret"):
        raise RuntimeError("أدخل Client ID و Client Secret أولًا.")
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    _set(gdrive_oauth_state=state, gdrive_oauth_verifier=verifier, gdrive_oauth_redirect=redirect_uri,
         gdrive_oauth_started=datetime.utcnow().isoformat())
    params = {
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": DRIVE_SCOPE, "access_type": "offline", "prompt": "consent",
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def is_pending_callback(state):
    """هل هذا state هو نفس طلب الربط الجاري (وعمره أقل من 15 دقيقة)؟"""
    saved = _get("gdrive_oauth_state")
    started = _parse_time(_get("gdrive_oauth_started"))
    return bool(saved and state and secrets.compare_digest(saved, state)
                and started and datetime.utcnow() - started < timedelta(minutes=15))


def finish_auth(code):
    """يبادل رمز Google بـ refresh token ويحفظه. يعيد نص النتيجة للمستخدم."""
    resp = requests.post(GOOGLE_TOKEN_URL, data={
        "client_id": _get("gdrive_client_id"), "client_secret": _get("gdrive_client_secret"),
        "code": code, "code_verifier": _get("gdrive_oauth_verifier"),
        "grant_type": "authorization_code", "redirect_uri": _get("gdrive_oauth_redirect"),
    }, timeout=30)
    _set(gdrive_oauth_state="", gdrive_oauth_verifier="")
    if resp.status_code != 200:
        raise RuntimeError(f"رفضت Google إتمام الربط ({resp.status_code}): {resp.text[:200]}")
    data = resp.json()
    refresh = data.get("refresh_token")
    if not refresh:
        raise RuntimeError("لم ترسل Google رمز الربط الدائم - احذف إذن التطبيق من حساب Google ثم أعد الربط.")
    account = ""
    try:
        about = requests.get(DRIVE_ABOUT_URL, params={"fields": "user(emailAddress)"},
                             headers={"Authorization": f"Bearer {data['access_token']}"}, timeout=30)
        if about.ok:
            account = about.json().get("user", {}).get("emailAddress", "")
    except requests.RequestException:
        pass
    _set(gdrive_refresh_token=refresh, gdrive_account=account, gdrive_folder_id="", backup_last_error="")
    return account


def disconnect():
    """يحذف مفتاح الربط من الجهاز دائمًا، ثم يحاول إلغاءه لدى Google (فشل الإلغاء لا يمنع الحذف المحلي)."""
    token = _get("gdrive_refresh_token")
    _set(gdrive_refresh_token="", gdrive_account="", gdrive_folder_id="")
    if token:
        try:
            requests.post("https://oauth2.googleapis.com/revoke", data={"token": token}, timeout=15)
        except Exception:
            pass


def _access_token():
    resp = requests.post(GOOGLE_TOKEN_URL, data={
        "client_id": _get("gdrive_client_id"), "client_secret": _get("gdrive_client_secret"),
        "refresh_token": _get("gdrive_refresh_token"), "grant_type": "refresh_token",
    }, timeout=30)
    if resp.status_code != 200:
        if "invalid_grant" in resp.text:
            raise RuntimeError("انتهى ربط Google Drive أو أُلغي من حساب Google - أعد الربط من صفحة النسخ الاحتياطي. "
                               "(إن تكرر كل أسبوع: انشر التطبيق في Google Cloud من وضع Testing إلى In production.)")
        raise RuntimeError(f"تعذر تجديد صلاحية Google ({resp.status_code}).")
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Google Drive: الرفع
# ---------------------------------------------------------------------------
def _ensure_folder(headers):
    folder_id = _get("gdrive_folder_id")
    if folder_id:
        r = requests.get(f"{DRIVE_FILES_URL}/{folder_id}", params={"fields": "id,trashed"}, headers=headers, timeout=30)
        if r.ok and not r.json().get("trashed"):
            return folder_id
    r = requests.post(DRIVE_FILES_URL, headers=headers, timeout=30,
                      json={"name": DRIVE_FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder"})
    r.raise_for_status()
    folder_id = r.json()["id"]
    _set(gdrive_folder_id=folder_id)
    return folder_id


def upload_to_drive(path):
    token = _access_token()
    headers = {"Authorization": f"Bearer {token}"}
    folder_id = _ensure_folder(headers)
    # رفع قابل للاستئناف (resumable): يناسب أي حجم ولا يُحمَّل الملف كله في طلب واحد ضخم
    init = requests.post(f"{DRIVE_UPLOAD_URL}?uploadType=resumable", headers=headers, timeout=30,
                         json={"name": os.path.basename(path), "parents": [folder_id]})
    init.raise_for_status()
    with open(path, "rb") as fh:
        put = requests.put(init.headers["Location"], data=fh, timeout=300,
                           headers={"Content-Type": "application/zip"})
    put.raise_for_status()
    _prune_drive(headers, folder_id)
    return put.json().get("id")


def _prune_drive(headers, folder_id):
    r = requests.get(DRIVE_FILES_URL, headers=headers, timeout=30, params={
        "q": f"'{folder_id}' in parents and trashed = false",
        "orderBy": "createdTime desc", "fields": "files(id,name)", "pageSize": 200})
    if not r.ok:
        return
    for f in r.json().get("files", [])[DRIVE_KEEP:]:
        requests.delete(f"{DRIVE_FILES_URL}/{f['id']}", headers=headers, timeout=30)


# ---------------------------------------------------------------------------
# العملية الكاملة + السجل
# ---------------------------------------------------------------------------
def _log(ok, message, digest, triggered_by):
    from models import db, BackupLog
    db.session.add(BackupLog(ok=ok, message=message[:300], sha256=digest, triggered_by=triggered_by))
    db.session.commit()


def backup_database(app, triggered_by="manual", upload=True):
    """نسخة محلية دائمًا، ثم رفعها إلى Google Drive إن كان مربوطًا وupload=True.
    يعيد {"ok", "message", "sha256"}. لا يرمي استثناءات (كل خطأ يُسجَّل ويُعرض)."""
    with _lock:
        digest = None
        try:
            path, digest = make_local_backup(app)
        except Exception as exc:
            msg = f"فشلت النسخة المحلية: {exc}"
            _set(backup_last_error=msg)
            _log(False, msg, None, triggered_by)
            return {"ok": False, "message": msg}
        _set(backup_last_local_at=datetime.utcnow().isoformat())
        notes = [f"نسخة محلية: {os.path.basename(path)}"]
        extra_err = _copy_to_extra_folder(path)
        if extra_err:
            notes.append(extra_err)
        ok = True
        if upload and _get("gdrive_refresh_token"):
            _set(backup_last_drive_attempt=datetime.utcnow().isoformat())
            try:
                upload_to_drive(path)
                _set(backup_last_drive_at=datetime.utcnow().isoformat(), backup_drive_pending="")
                notes.append("ورُفعت إلى Google Drive ✓")
            except Exception as exc:
                ok = False
                notes.append(f"فشل الرفع إلى Google Drive: {exc}")
        msg = " — ".join(notes)
        _set(backup_last_error="" if ok and not extra_err else msg)
        _log(ok, msg, digest, triggered_by)
        return {"ok": ok, "message": msg, "sha256": digest}


def request_drive_upload():
    """يُستدعى بعد إغلاق وردية: يطلب نسخة + رفعًا خلال دقائق دون تعطيل الكاشير."""
    _set(backup_drive_pending="1")
    _wake.set()


def get_backup_status(app):
    last_local = _parse_time(_get("backup_last_local_at"))
    last_drive = _parse_time(_get("backup_last_drive_at"))
    return {
        **drive_status(),
        "last_local": last_local,
        "last_drive": last_drive,
        "last_error": _get("backup_last_error"),
        "extra_dir": _get("backup_extra_dir"),
        "local_files": list_local_backups(app)[:10],
        "local_count": len(list_local_backups(app)),
        "backup_dir": backup_dir(app),
    }


# ---------------------------------------------------------------------------
# الجدولة (خيط خلفي واحد)
# ---------------------------------------------------------------------------
def due_backups(now, interval):
    """يقرر (محلي مستحق؟، رفع إلى Drive مستحق؟) حسب آخر الأوقات المحفوظة."""
    last_local = _parse_time(_get("backup_last_local_at"))
    last_drive = _parse_time(_get("backup_last_drive_at"))
    last_try = _parse_time(_get("backup_last_drive_attempt"))
    connected = bool(_get("gdrive_refresh_token"))
    drive_due = connected and (_get("backup_drive_pending") == "1" or not last_drive
                               or now - last_drive >= timedelta(hours=DRIVE_EVERY_HOURS))
    # عند انقطاع الإنترنت: إعادة المحاولة كل 30 دقيقة فقط (لا كل 5 دقائق بنسخة محلية جديدة كل مرة)
    if drive_due and last_try and now - last_try < timedelta(minutes=30) and (not last_drive or last_try > last_drive):
        drive_due = False
    local_due = not last_local or now - last_local >= interval
    return local_due, drive_due


def run_due_backups(app, interval):
    local_due, drive_due = due_backups(datetime.utcnow(), interval)
    if local_due or drive_due:
        return backup_database(app, triggered_by="auto", upload=drive_due)
    return None


def start_auto_backup(app, interval_minutes=60):
    """كل 5 دقائق يتحقق: هل حان وقت النسخة المحلية (كل interval_minutes)؟ وهل حان الرفع إلى Drive
    (مرة كل 24 ساعة، أو مطلوب بعد إغلاق وردية)؟ يعمل في الخلفية فلا يبطئ شاشة البيع."""
    if getattr(app, "_backup_thread_started", False):
        return
    app._backup_thread_started = True
    interval = timedelta(minutes=max(10, int(interval_minutes)))

    def _worker():
        time.sleep(60)  # لا نثقل لحظة تشغيل البرنامج
        while True:
            try:
                with app.app_context():
                    run_due_backups(app, interval)
            except Exception:
                app.logger.exception("خطأ في خيط النسخ الاحتياطي")
            _wake.wait(CHECK_EVERY_SECONDS)
            _wake.clear()

    threading.Thread(target=_worker, name="zg-backup", daemon=True).start()
