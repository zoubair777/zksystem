import os
import json
import csv
import io
import logging
import threading
import time
import hashlib
import sqlite3
import tempfile
import traceback
import requests
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, render_template, redirect, url_for, request, flash, jsonify, send_file
from werkzeug.datastructures import MultiDict
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user
)

from config import Config
from label_service import barcode_svg, is_printable_code
from models import (
    db, User, Category, Warehouse, Product, Stock, StockMovement,
    Customer, Supplier, Employee, Invoice, InvoiceItem, Purchase, PurchaseItem,
    AccountEntry, Shift, ApprovalRequest, AuditLog, Cashbox, BackupLog, SupplierDebt,
    StockTransfer, StockTransferItem, Setting, Currency, CustomerDiscount, InventoryAdjustment, CashTransfer,
    ExternalBorrower, SalesReturn, SalesReturnItem,
    FundAccount, FundTransfer, PurchaseReturn, PurchaseReturnItem
)

app = Flask(
    __name__,
    template_folder=os.path.join(Config.RESOURCE_DIR, "templates"),
    static_folder=os.path.join(Config.RESOURCE_DIR, "static"),
)
app.config.from_object(Config)
db.init_app(app)


# ---------------------------------------------------------------------------
# SQLite بوضع WAL: يسمح لأكثر من كاشير بالقراءة والكتابة معًا دون أن يعطّل أحدهما الآخر،
# و busy_timeout يجعل أي كتابة متزامنة نادرة تنتظر لحظة بدل أن تفشل بخطأ "database is locked".
# ---------------------------------------------------------------------------
from sqlalchemy import event
from sqlalchemy.engine import Engine


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record):
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA busy_timeout=10000")
    cur.close()

# ---------------------------------------------------------------------------
# سجل أخطاء دائم (logs/erp.log) بجانب البرنامج نفسه — حتى في نسخة سطح المكتب
# المُجمَّعة (.exe) التي لا تملك نافذة طرفية (console=False)، حتى يكون هناك دائمًا
# مكان واحد يمكن الرجوع إليه لمعرفة سبب أي عطل حقيقي بدل اختفاء الخطأ بصمت.
# ---------------------------------------------------------------------------
_LOG_DIR = os.path.join(Config.BASE_DIR, "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
_log_handler = RotatingFileHandler(os.path.join(_LOG_DIR, "erp.log"), maxBytes=2_000_000,
                                    backupCount=3, encoding="utf-8")
_log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_log_handler.setLevel(logging.WARNING)
app.logger.addHandler(_log_handler)
app.logger.setLevel(logging.WARNING)
logging.getLogger("werkzeug").addHandler(_log_handler)


@app.errorhandler(Exception)
def handle_unexpected_error(exc):
    """يسجّل أي خطأ غير متوقع بكامل تفاصيله في logs/erp.log، ثم يرجّع رسالة
    واضحة بدل صفحة/استجابة غامضة قد تُفسَّر خطأً كـ'انقطاع اتصال' في الواجهة."""
    from werkzeug.exceptions import HTTPException
    if isinstance(exc, HTTPException):
        return exc  # أخطاء HTTP الطبيعية (404،403..) تُترك كما هي
    app.logger.error("خطأ غير متوقع في %s %s:\n%s", request.method, request.path,
                      traceback.format_exc())
    try:
        db.session.rollback()
    except Exception:
        pass
    wants_json = (
        request.path.startswith("/pos/") or request.path.startswith("/api/")
        or request.is_json or "application/json" in (request.headers.get("Accept") or "")
    )
    if wants_json:
        return jsonify({"error": "حدث خطأ داخلي غير متوقع في السيرفر. راجع ملف logs/erp.log للتفاصيل."}), 500
    return render_template("error_500.html") if _has_template("error_500.html") else (
        "حدث خطأ داخلي غير متوقع. راجع ملف logs/erp.log للتفاصيل.", 500)


def _has_template(name):
    try:
        app.jinja_env.get_or_select_template(name)
        return True
    except Exception:
        return False


login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "الرجاء تسجيل الدخول للمتابعة"


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ---------------------------------------------------------------------------
# RBAC - التحكم بالصلاحيات حسب الدور
# admin   : صلاحيات كاملة (تقارير، مستخدمين، إعدادات، حذف/تعديل فواتير)
# manager : كصلاحيات الأدمن على العمليات اليومية لكن بدون إدارة المستخدمين
# cashier : شاشة البيع + ورديته فقط، بدون تكاليف/أرباح/تقارير/تعديل أسعار
# ---------------------------------------------------------------------------
def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if current_user.role not in roles:
                flash("لا تملك صلاحية الوصول لهذه الصفحة", "danger")
                return redirect(url_for("dashboard"))
            return f(*args, **kwargs)
        return wrapper
    return decorator


admin_required = role_required("admin")
manager_or_admin_required = role_required("admin", "manager")

PERMISSIONS = {
    "dashboard": "لوحة التحكم", "pos": "نقطة البيع", "sales_view": "عرض الفواتير",
    "sales_edit": "تعديل الفواتير", "sales_void": "إلغاء/إرجاع الفواتير",
    "products": "إدارة المنتجات", "categories": "إدارة التصنيفات", "inventory_import": "استيراد الجرد القديم",
    "old_sales_import": "إدخال فواتير البيع القديمة",
    "warehouses": "إدارة المخازن", "purchases": "إدارة المشتريات", "transfers": "نقل البضاعة بين المخازن",
    "customers": "إدارة العملاء", "suppliers": "إدارة الموردين",
    "accounts": "إدارة الحسابات", "reports": "التقارير المالية",
    "shifts": "إدارة الورديات", "approvals": "الموافقات", "cashboxes": "إدارة الخزائن",
    "backup": "النسخ الاحتياطي", "audit_log": "سجل الأنشطة", "users": "إدارة المستخدمين", "inventory_adjustment": "تسوية المخزون", "customer_discounts": "خصومات العملاء",
    "override_price": "تغيير سعر البيع يدويًا وقت البيع", "sales_delete": "حذف الفواتير نهائيًا", "shifts_manage": "تعديل/حذف الورديات", "financial_report": "التقرير المالي (نقد/بنك)",
    "employees": "إدارة الموظفين وسلفهم",
    # صلاحيات دقيقة إضافية (V10) لحماية الحقول الحساسة في السلف/السحوبات/الخزائن/الورديات
    "owner_withdrawal": "سحب المالك النقدي/البنكي (بدون راتب ولا سقف تلقائي)",
    "advance_override_limit": "تجاوز سقف سلفة الموظف المحسوب من راتبه",
    "cashbox_balance_edit": "تعديل رصيد الخزينة يدويًا (تصحيح الرصيد من صفحة التعديل)",
    "shift_values_edit": "تعديل النقد الافتتاحي/المعدود لوردية بعد إنشائها",
    "external_borrowers": "سلف لأشخاص من خارج الموظفين (إضافتهم وصرف/تسديد سلفهم)",
    "employee_draw": "سحب على الحساب لموظف (بدون سقف سلفة مرتبط بالراتب)",
    "sales_return": "إرجاع (كلي/جزئي) لفواتير بيع مع استرجاع الفلوس فعليًا",
    "ai_assistant": "استخدام مساعد الذكاء الاصطناعي للسؤال عن بيانات النظام",
    "sales_exchange": "استبدال بضاعة للعميل (إرجاع أصناف وأخذ أصناف بدلها مع تسوية الفرق)",
    "inventory_view": "عرض لوحة المخزون الحالي والكميات الإجمالية",
    "barcode_labels": "طباعة ملصقات الباركود",
}

def permission_required(permission):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not current_user.has_permission(permission):
                flash("لا تملك الصلاحية المطلوبة لتنفيذ هذه العملية", "danger")
                return redirect(url_for("dashboard"))
            return f(*args, **kwargs)
        return wrapper
    return decorator


def log_action(action, entity_type=None, entity_id=None, details=None):
    entry = AuditLog(
        user_id=current_user.id if current_user.is_authenticated else None,
        action=action, entity_type=entity_type, entity_id=entity_id,
        details=details, ip_address=request.remote_addr,
    )
    db.session.add(entry)


def branch_scope_cashbox_id():
    """يُرجع cashbox_id (= نقطة البيع/الفرع) الذي يجب تقييد المستخدم الحالي عليه في كل شاشات
    الحسابات والمبيعات والتقارير المالية، أو None إن كان غير مقيَّد (مدير/أدمن يشوف كل الفروع،
    أو مستخدم بدون خزينة مخصصة أصلاً - في هذه الحالة يُفضَّل تعيين خزينة له من صفحة المستخدمين).
    هذا هو أساس عزل "كل نقطة بيع لها حساباتها لحالها" بين الفروع."""
    if current_user.is_manager_or_admin():
        return None
    return current_user.cashbox_id


def _parse_money(raw, default=0.0):
    """يحوّل مبلغًا مُدخلًا إلى float ويقبل القيم السالبة (مسموحة في الورديات)، ويقبل الأرقام العربية
    والفاصلة العشرية. يرفع ValueError لو القيمة ليست رقمًا صالحًا (بدل خطأ 500)."""
    import math
    if raw is None or str(raw).strip() == "":
        return default
    text = str(raw).strip().translate(str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬", "0123456789.,")).replace(",", "")
    value = float(text)
    if math.isnan(value) or math.isinf(value):
        raise ValueError("invalid number")
    return round(value, 2)


def _resolve_form_cashbox(form):
    """يحدد الخزينة/الفرع الذي تُربط به الحركة: مستخدم الفرع المقيَّد يُفرض عليه فرعه دائمًا (يُتجاهل أي
    اختيار آخر)، وغيره يختار من النموذج. يعيد (cashbox_id أو None, رسالة خطأ أو None)."""
    scope = branch_scope_cashbox_id()
    if scope:
        return scope, None
    raw = form.get("cashbox_id")
    if not raw:
        return None, None
    try:
        box = Cashbox.query.get(int(raw))
    except (TypeError, ValueError):
        box = None
    if not box:
        return None, "اختر خزينة صحيحة."
    return box.id, None


def get_active_shift():
    if not current_user.is_authenticated:
        return None
    return Shift.query.filter_by(user_id=current_user.id, status="open").first()


# ---------------------------------------------------------------------------
# التوقيت: كل الأوقات تُخزَّن بتوقيت غرينتش (UTC)، وتُعرض وتُبحث بتوقيت جهاز المحل (ليبيا = UTC+2).
# الفرق يُقرأ من ساعة الجهاز نفسه، ويمكن فرضه بمتغير البيئة ERP_TZ_OFFSET_HOURS.
# (سابقًا كانت الفواتير تُطبع بوقت متأخر ساعتين، ومبيعات ما بعد منتصف الليل تُحسب على اليوم السابق.)
# ---------------------------------------------------------------------------
def tz_offset():
    forced = os.getenv("ERP_TZ_OFFSET_HOURS")
    if forced not in (None, ""):
        return timedelta(hours=float(forced))
    seconds = (datetime.now() - datetime.utcnow()).total_seconds()
    return timedelta(seconds=round(seconds / 900) * 900)


def to_local(dt):
    return dt + tz_offset() if dt else dt


def to_utc(dt):
    return dt - tz_offset() if dt else dt


def local_today_range():
    """بداية ونهاية "اليوم" بتوقيت المحل، محوّلتين إلى UTC لمقارنتهما بما هو مخزّن."""
    start_local = datetime.combine(to_local(datetime.utcnow()).date(), datetime.min.time())
    return to_utc(start_local), to_utc(start_local + timedelta(days=1))


app.jinja_env.filters["local"] = to_local


@app.template_filter("bc")
def _barcode_sub_filter(product):
    """سطر الباركود الصغير تحت اسم المنتج في الجداول: {{ p.name }}{{ p|bc }}. يُهرَّب تلقائيًا عبر Markup.format."""
    from markupsafe import Markup
    code = getattr(product, "barcode", None)
    if not code:
        return ""
    return Markup('<div class="small text-muted font-monospace" dir="ltr" style="text-align:right">{}</div>').format(code)


def get_settings():
    """إرجاع كل إعدادات النظام كـ dict مع القيم الافتراضية."""
    defaults = {
        "company_name":        app.config.get("COMPANY_NAME", "Z.G ERP"),
        "company_subtitle":    "",
        "company_phone":       "",
        "company_address":     "",
        "invoice_header_note": "",
        "invoice_footer":      "شكرًا لتعاملكم معنا",
        "currency":            app.config.get("CURRENCY", "د.ل"),
        "low_stock_threshold": str(app.config.get("LOW_STOCK_THRESHOLD", 10)),
        "export_path": os.path.join(Config.BASE_DIR, "exports"),
        "local_currency": "LYD",
        "usd_exchange_rate": "5.00",  # سعر صرف الدولار مقابل الدينار الليبي (لعرض قيمة المخزون بعملتين)
        "gemini_api_key": "",  # مفتاح Gemini API لمساعد الذكاء الاصطناعي (من aistudio.google.com/apikey)
        "claude_api_key": "",  # مفتاح Anthropic (Claude) API البديل (من console.anthropic.com)
        "ai_provider": "gemini",  # أي مزوّد يُستخدم فعليًا: gemini أو claude
    }
    for key, default in defaults.items():
        defaults[key] = Setting.get(key, default)
    return defaults


@app.context_processor
def inject_globals():
    return {
        "active_shift": get_active_shift() if current_user.is_authenticated else None,
        "permissions_catalog": PERMISSIONS,
        "pending_approvals_count": (
            ApprovalRequest.query.filter_by(status="pending").count()
            if current_user.is_authenticated and current_user.is_manager_or_admin() else 0
        ),
    }


# ---------------------------------------------------------------------------
# أدوات مساعدة
# ---------------------------------------------------------------------------
def generate_number(prefix, model, field_name=None):
    """يولّد رقم مستند فريد لهذا الشهر (فاتورة/سند/تحويل...).

    ملاحظة مهمة: النسخة القديمة كانت تعتمد على *عدد كل الصفوف* في الجدول (count())
    لتوليد الرقم التالي. هذا يتسبب في تكرار الرقم بمجرد حذف أي سجل واحد (فاتورة،
    فاتورة شراء، تسوية مخزون...)، لأن العدّاد يتراجع للخلف بينما الأرقام الأعلى لا
    تزال موجودة فعليًا في الجدول → فشل الحفظ برسالة قيد فريد مكرر (UNIQUE constraint)،
    وهو تحديدًا سبب رسالة "فشل الاتصال بالخادم" عند إتمام البيع.
    الحل: نعتمد على أعلى رقم تسلسلي *موجود فعليًا* لنفس الشهر بدل عدد الصفوف، بحيث لا
    يتكرر الرقم أبدًا حتى لو حُذفت سجلات في المنتصف.
    """
    if field_name is None:
        field_name = next((c.name for c in model.__table__.columns if c.name.endswith("_number")), None)
    month_prefix = f"{prefix}-{datetime.now().strftime('%Y%m')}-"
    column = getattr(model, field_name)
    existing_numbers = db.session.query(column).filter(column.like(f"{month_prefix}%")).all()
    max_seq = 0
    for (num,) in existing_numbers:
        try:
            seq = int(str(num).rsplit("-", 1)[-1])
            max_seq = max(max_seq, seq)
        except (ValueError, AttributeError, IndexError):
            continue
    return f"{month_prefix}{max_seq + 1:04d}"


def _paginate(query, per_page):
    """يقسم استعلامًا إلى صفحات (رقم الصفحة من ?page=). يعيد (عناصر الصفحة، pager) حيث pager يحوي
    page/pages/total و args (باقي فلاتر الرابط) لاستخدامها في templates/_pagination.html."""
    total = query.order_by(None).count()
    pages = max(1, -(-total // per_page))
    page = min(max(request.args.get("page", 1, type=int), 1), pages)
    items = query.offset((page - 1) * per_page).limit(per_page).all()
    args = {k: v for k, v in request.args.items() if k != "page"}
    return items, {"page": page, "pages": pages, "total": total, "args": args}


def get_or_create_stock(product_id, warehouse_id):
    stock = Stock.query.filter_by(product_id=product_id, warehouse_id=warehouse_id).first()
    if not stock:
        stock = Stock(product_id=product_id, warehouse_id=warehouse_id, quantity=0)
        db.session.add(stock)
        db.session.flush()
    return stock


def _post_invoice_cash(invoice, shift, sign):
    """يرحّل نقد الفاتورة لرصيد خزينة الوردية فورًا (sign=+1 عند البيع، -1 عند الإلغاء/الحذف).
    الفواتير التي سبقت هذا التحديث (cash_posted=False) لا تُعكس، لأن نقدها دخل الرصيد عند إغلاق ورديتها."""
    box = shift.cashbox if shift else None
    if sign > 0:
        if box and invoice.cash_amount:
            box.balance = round((box.balance or 0) + invoice.cash_amount, 2)
        invoice.cash_posted = bool(box)
    elif invoice.cash_posted:
        if box and invoice.cash_amount:
            box.balance = round((box.balance or 0) - invoice.cash_amount, 2)
        invoice.cash_posted = False


def restore_invoice_stock(invoice):
    """إعادة الكمية إلى المخزون عند إلغاء فاتورة معتمدة. ما أُرجع سابقًا من الفاتورة عاد للمخزون
    وقت الإرجاع، فنعيد الباقي فقط (وإلا تتضاعف كميته في المخزون)."""
    for item in invoice.items:
        qty_back = item.returnable_quantity() if invoice.stock_affected is not False else 0
        if qty_back <= 0:
            continue
        stock = get_or_create_stock(item.product_id, invoice.warehouse_id)
        stock.quantity += qty_back
        db.session.add(StockMovement(product_id=item.product_id, warehouse_id=invoice.warehouse_id,
                                      movement_type="in", quantity=qty_back,
                                      reference=f"إلغاء فاتورة {invoice.invoice_number}"))
    if invoice.credit_amount and invoice.customer_id:
        customer = Customer.query.get(invoice.customer_id)
        customer.balance -= invoice.credit_amount


# ---------------------------------------------------------------------------
# API auth (تطبيق سطح المكتب)
# ---------------------------------------------------------------------------
def api_login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        token = request.headers.get("X-API-TOKEN")
        if not token:
            return jsonify({"error": "missing token"}), 401
        user = User.query.filter_by(api_token=token, active=True).first()
        if not user:
            return jsonify({"error": "invalid token"}), 401
        request.api_user = user
        return f(*args, **kwargs)
    return wrapper


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(force=True)
    user = User.query.filter_by(username=data.get("username"), active=True).first()
    if user and user.check_password(data.get("password", "")):
        token = user.generate_token()
        db.session.commit()
        return jsonify({"token": token, "full_name": user.full_name, "role": user.role})
    return jsonify({"error": "بيانات دخول غير صحيحة"}), 401


@app.route("/api/products/barcode/<code>")
@api_login_required
def api_product_by_barcode(code):
    p = Product.query.filter_by(barcode=code, active=True).first()
    if not p:
        return jsonify({"error": "المنتج غير موجود"}), 404
    return jsonify({"id": p.id, "name": p.name, "barcode": p.barcode,
                     "sale_price": p.sale_price, "quantity": p.total_quantity()})


@app.route("/api/products")
@api_login_required
def api_products():
    products = Product.query.filter_by(active=True).all()
    return jsonify([{"id": p.id, "name": p.name, "barcode": p.barcode,
                      "sale_price": p.sale_price, "quantity": p.total_quantity()} for p in products])


@app.route("/api/sales", methods=["POST"])
@api_login_required
def api_create_sale():
    data = request.get_json(force=True)
    warehouse_id = data.get("warehouse_id")
    items = data.get("items", [])
    if not warehouse_id or not items:
        return jsonify({"error": "بيانات ناقصة"}), 400

    resolved, subtotal = [], 0.0
    for it in items:
        try:
            product = Product.query.get(int(it["product_id"]))
            qty = int(it["quantity"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "بيانات صنف غير صالحة"}), 400
        if not product or not product.active or qty <= 0:
            return jsonify({"error": "صنف غير صالح أو كمية غير صحيحة"}), 400
        resolved.append((product, qty))
        subtotal += qty * (product.sale_price or 0)
    subtotal = round(subtotal, 2)
    try:
        discount = round(float(data.get("discount") or 0), 2)
    except (TypeError, ValueError):
        discount = -1
    if discount < 0 or discount > subtotal:
        return jsonify({"error": "خصم غير صالح"}), 400
    total = round(subtotal - discount, 2)

    payment_method = data.get("payment_method", "cash")
    customer_id = data.get("customer_id")
    # الآجل الكامل = الإجمالي بعد الخصم (كان يُحسب قبل الخصم فيزيد دين العميل بقيمة الخصم)
    credit_amount = total if payment_method == "credit" else round(float(data.get("credit_amount") or 0), 2)
    if credit_amount < 0 or credit_amount > total:
        return jsonify({"error": "المبلغ الآجل غير صالح"}), 400
    if credit_amount > 0:
        customer = Customer.query.get(customer_id) if customer_id else None
        if not customer:
            return jsonify({"error": "البيع الآجل يتطلب عميلًا"}), 400
        if customer.would_exceed_limit(credit_amount) and not data.get("force"):
            return jsonify({"error": "تجاوز سقف الائتمان المسموح للعميل",
                             "available_credit": customer.available_credit()}), 409

    # الفاتورة تُربط بوردية المستخدم المفتوحة (إن وُجدت) حتى تظهر في تقرير الوردية ومطابقة الصندوق
    shift = Shift.query.filter_by(user_id=request.api_user.id, status="open").first()
    invoice = Invoice(
        invoice_number=generate_number("INV", Invoice),
        customer_id=customer_id, warehouse_id=warehouse_id, user_id=request.api_user.id,
        shift_id=shift.id if shift else None, payment_method=payment_method, discount=discount,
    )
    db.session.add(invoice)
    db.session.flush()
    for product, qty in resolved:
        stock = get_or_create_stock(product.id, warehouse_id)
        if stock.quantity < qty:
            db.session.rollback()
            return jsonify({"error": f"الكمية غير متوفرة للمنتج {product.display_name()}"}), 400
        stock.quantity -= qty
        line_total = round(qty * (product.sale_price or 0), 2)
        db.session.add(InvoiceItem(invoice_id=invoice.id, product_id=product.id, quantity=qty,
                                    unit_price=product.sale_price, total=line_total,
                                    unit_cost=product.purchase_price or 0))
        db.session.add(StockMovement(product_id=product.id, warehouse_id=warehouse_id,
                                      movement_type="out", quantity=qty, reference=invoice.invoice_number))

    invoice.subtotal = subtotal
    invoice.total = total
    invoice.credit_amount = credit_amount
    remaining = round(total - credit_amount, 2)
    if payment_method == "bank":
        invoice.bank_amount = remaining
        invoice.bank_name = data.get("bank_name")
    else:
        invoice.cash_amount = remaining
    if invoice.credit_amount and invoice.customer_id:
        Customer.query.get(invoice.customer_id).balance += invoice.credit_amount
    if shift:
        _post_invoice_cash(invoice, shift, sign=1)
    db.session.commit()
    return jsonify({"invoice_id": invoice.id, "invoice_number": invoice.invoice_number, "total": invoice.total})


@app.route("/api/stock")
@api_login_required
def api_stock():
    stocks = Stock.query.all()
    return jsonify([{"product": s.product.name, "warehouse": s.warehouse.name,
                      "quantity": s.quantity} for s in stocks])


@app.route("/api/stock/<int:product_id>/<int:warehouse_id>")
@api_login_required
def api_stock_for_product_warehouse(product_id, warehouse_id):
    """الكمية الفعلية لمنتج معيّن داخل مخزن معيّن تحديدًا (وليس إجمالي الكمية عبر كل المخازن) -
    يُستخدم في نموذج نقل البضاعة بين المخازن للتحقق من التوفر بمخزن المصدر عند مسح الباركود."""
    stock = Stock.query.filter_by(product_id=product_id, warehouse_id=warehouse_id).first()
    return jsonify({"quantity": stock.quantity if stock else 0})


# ---------------------------------------------------------------------------
# مساعد الذكاء الاصطناعي (اختياري - يحتاج مفتاح Gemini API من المستخدم)
# ---------------------------------------------------------------------------
AI_ASSISTANT_SYSTEM_PROMPT = """أنت مساعد داخلي ذكي ضمن نظام Z.G ERP لمحل ملابس (مبيعات، مخازن، مشتريات، موردين،
حسابات، سلف، ورديات). تجيب بالعربية، باختصار ومباشرة، عن أي سؤال يُطرح عليك:
1. أي سؤال عن بيانات النظام وأي رقم فيه (مبيعات، أرباح، أرصدة، مخزون، موردين، عملاء، فواتير، حركات مالية...).
2. كيفية استخدام أي صفحة أو ميزة بالنظام.
3. نصيحة أو رأي مبني على أرقام النظام.
4. أي سؤال عام آخر: أجب عنه باختصار من معرفتك.
5. طلب تحليل (أداء المنتجات، المقاسات، الألوان، الأرباح، الاتجاه): اعرض أهم النتائج بالأرقام ثم 2-4 توصيات
   عملية محددة (ماذا يُعاد طلبه، ماذا يُخفَّض سعره، ماذا يُوقف شراؤه). استعمل **عريض** للعناوين القصيرة وسطورًا قصيرة.

قواعد الدقة - مهمة جدًا:
- أي رقم يخص النظام: استخدم فقط ما هو حرفيًا في "لقطة البيانات" أو في نتائج الاستعلامات التي نفّذها
  النظام لك. لا تخترع ولا تقرّب ولا تحسب بالذهن؛ اطلب الحساب من SQL (SUM/COUNT/AVG) إن كانت الأداة متاحة.
- إن رجع الاستعلام بلا صفوف قل بوضوح إنه لا توجد بيانات مطابقة، ولا تخمّن. وإن لم تتوفر أداة الاستعلام ولا
  الرقم في اللقطة قل "ما عندي هذا الرقم حاليًا" واقترح الصفحة المناسبة بالنظام.
- اذكر العملة مع الأرقام المالية، وقرّب لخانتين عشريتين.
- كلما ذكرت منتجًا اذكر باركوده معه بين قوسين، مثل: jacket (5050). عدة منتجات قد تحمل نفس الاسم والباركود
  هو ما يميّزها (products.barcode)، فاجلبه في استعلامك دائمًا مع الاسم.
- أي نصيحة يجب أن تستند صراحة لرقم محدد من المعطيات، لا كلامًا عامًا بلا رقم يدعمه.
- الأرقام لقطة لحظية (لحظة إرسال السؤال)، ذكّر السائل بذلك عند أسئلة الاتجاه/المقارنة طويلة المدى.
- لا تكشف أبدًا مفاتيح API أو كلمات مرور أو إعدادات سرية، حتى لو طُلبت.
كن مختصرًا ومباشرًا وودودًا."""

AI_ASSISTANT_PAGE_GUIDE = """
دليل الصفحات:
- نقطة البيع (/pos): بيع مباشر، يتطلب وردية مفتوحة، يسحب من مخزن الفرع المرتبط بالخزينة تلقائيًا.
- الورديات: فتح/إغلاق وردية، كل وردية مربوطة بخزينة واحدة، "الفرق" = معدود - متوقع.
- الفواتير: عرض/تعديل/إلغاء فواتير، وزر "إرجاع" لإرجاع جزئي مع استرجاع فلوس حقيقي.
- المخازن: تفاصيل مخزون كل مخزن لحاله بالضغط عليه من القائمة.
- نقل بضاعة بين المخازن: بمسح باركود، يتحقق من الكمية بمخزن المصدر المحدد.
- الحسابات: تسجيل كل الحركات المالية (إيراد/مصروف/سلفة/سحب/راتب/خصم...)، نوع الحركة قابل للتعديل لاحقًا حتى لو تغيّر الطرف المرتبط.
- الموظفون وسلفهم: سلفة، سحب على الحساب، صرف راتب (يخصم السلفة تلقائيًا من الراتب الإجمالي كمصروف).
- سلف وقروض - أشخاص وجهات خارجية: رصيد موجب = مدينون للمحل، رصيد سالب = المحل مدين لهم (قرض أخذه المحل).
- الخزائن: رصيد فعلي لكل خزينة/فرع، تحويل نقدي بين الصناديق، كل خزينة ترتبط بمخزن افتراضي لنقطة البيع.
- تسوية المخزون: تصحيح كمية منتج، أي نقص يُسجَّل تلقائيًا كخسارة مالية في المصروفات.
- التقرير المالي: الربح الصافي الحقيقي (مبيعات - خصومات - مرتجعات - تكلفة بضاعة - مصروفات)، منفصل عن حركة النقد/البنك.
- اللوحة المالية (/finance): نقطة تجميع لكل صفحات المال بإحصائية سريعة لكل واحدة.
- المشتريات: فاتورة شراء بمورد ومخزن وعملة وسعر صرف ودفعة (نقد من خزينة أو بنك)؛ تعديل الفاتورة (مدير/أدمن) يعكس أثرها القديم ويطبّق الجديد.
- كشف حساب المورد: من صفحة الموردين زر "كشف حساب" (أو اسم المورد في المشتريات): كل الفواتير والديون القديمة والمدفوعات/سندات الصرف برصيد جارٍ وفلتر بتاريخ وطباعة.
"""


def _find_mentioned_entities(question):
    """يبحث عن أي اسم عميل/موظف/مورد/شخص-جهة خارجية مذكور حرفيًا داخل نص السؤال (مطابقة جزئية
    بالاسم)، ويرجع رصيده الدقيق من قاعدة البيانات مباشرة - بدل ما يُترك للذكاء الاصطناعي يخمّنه
    أو "يتذكره" غلط من بيانات تدريبه. هذا أهم إجراء وحيد لدقة أسئلة من نوع "كم على فلان"."""
    words = sorted({w.strip(" ؟?,.،") for w in question.split() if len(w.strip(" ؟?,.،")) >= 3}, key=len, reverse=True)
    found, seen = [], set()
    specs = (
        (Customer, "عميل", lambda o: f'دين عليه (له نقابله): {o.balance:.2f}'),
        (Employee, "موظف", lambda o: f'سلفة/سحب عليه: {o.balance:.2f}, راتبه الأساسي: {o.salary or 0:.2f}'),
        (Supplier, "مورد", lambda o: f'مستحق له عندنا: {o.balance:.2f}'),
        (ExternalBorrower, "شخص/جهة خارجية",
         lambda o: (f'له عندنا: {o.balance:.2f}' if (o.balance or 0) >= 0 else f'علينا له (قرض): {-o.balance:.2f}')),
    )
    for w in words[:6]:  # أول 6 كلمات مميزة بالسؤال كافية عادة، لتفادي بحث بطيء على سؤال طويل
        for model, label, extra in specs:
            for m in model.query.filter(model.name.ilike(f"%{w}%")).limit(3).all():
                key = (model.__name__, m.id)
                if key in seen:
                    continue
                seen.add(key)
                found.append(f'{label} "{m.name}": {extra(m)}')
        if len(found) >= 8:
            break
    return found[:8]


def _sales_trends(scope_cashbox):
    """مقارنات ومؤشرات محسوبة بدقة في بايثون (مو متروكة للذكاء الاصطناعي يحسبها من الذاكرة) -
    هذا ما يخلي "النصيحة" مبنية على رقم فعلي صحيح 100%، لا على تخمين."""
    today = datetime.utcnow().date()
    month_start = today.replace(day=1)
    last_month_end = month_start - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)

    inv_q = Invoice.query.filter(Invoice.status != "voided")
    if scope_cashbox:
        inv_q = inv_q.join(Shift, Invoice.shift_id == Shift.id).filter(Shift.cashbox_id == scope_cashbox)
    this_month = inv_q.filter(Invoice.date >= month_start).all()
    last_month = inv_q.filter(Invoice.date >= last_month_start, Invoice.date <= last_month_end).all()
    this_month_total = sum(i.total for i in this_month)
    last_month_total = sum(i.total for i in last_month)

    lines = [f"مبيعات الشهر الماضي كاملًا: {last_month_total:.2f}"]
    if last_month_total > 0:
        change = round((this_month_total - last_month_total) / last_month_total * 100, 1)
        lines.append(f"نسبة تغيّر مبيعات هذا الشهر (حتى الآن) مقارنة بكامل الشهر الماضي: {change:+.1f}%")

    # أداء المنتجات آخر 30 يومًا (صافي بعد المرتجعات وخصم الفاتورة) من نفس دالة تقرير "أفضل المنتجات"،
    # حتى يجيب المساعد عن "شن أفضل منتج/مقاس/لون" بنفس أرقام التقرير بالضبط
    start_30 = datetime.utcnow() - timedelta(days=30)
    rows, totals, slow = _product_performance(start_30, None, "product", scope_cashbox)
    if rows:
        def fmt(r):
            return f"{r['label']} [كود {r['barcode'] or '-'}] (صافي {r['net_qty']} قطعة، مبيعات {r['revenue']:.2f}، ربح {r['profit']:.2f}، مخزون {r['stock']})"
        lines.append(f"آخر 30 يومًا: صافي {totals['net_qty']} قطعة، مبيعات {totals['revenue']:.2f}، ربح {totals['profit']:.2f}")
        lines.append("أفضل 5 منتجات بالكمية (آخر 30 يومًا): " + "؛ ".join(fmt(r) for r in _perf_sorted(rows, "qty", "best")[:5]))
        lines.append("أفضل 5 منتجات بالربح (آخر 30 يومًا): " + "؛ ".join(fmt(r) for r in _perf_sorted(rows, "profit", "best")[:5]))
        for g in ("size", "color"):
            g_rows, _t, _s = _product_performance(start_30, None, g, scope_cashbox)
            top = _perf_sorted(g_rows, "qty", "best")[:3]
            if top:
                lines.append(f"أكثر {PERF_GROUPS[g]} مبيعًا (آخر 30 يومًا): " + "، ".join(f"{r['label']} ({r['net_qty']})" for r in top))
    if slow:
        lines.append(f"بضاعة راكدة (عليها مخزون ولم تُبع آخر 30 يومًا): {len(slow)} منتج بقيمة "
                     f"{sum(r['stock_value'] for r in slow):.2f} - أكبرها: " + "، ".join(r["label"] for r in slow[:5]))
    lines.append("لتحليل مفصّل لأي فترة: صفحة التقارير ← أفضل المنتجات (/reports/top-products) وفيها زر تحليل بالذكاء الاصطناعي.")
    return lines


def _build_ai_snapshot(question):
    """يجمع لقطة نصية محدودة الحجم من أهم أرقام النظام الحالية + أي أشخاص مذكورين بالسؤال +
    مؤشرات اتجاه محسوبة بدقة، لتُرسَل كسياق لمساعد الذكاء الاصطناعي. لا يُرسِل أي بيانات خام
    كاملة (كل الفواتير/كل العملاء) - فقط ملخصات رقمية محسوبة مسبقًا، حتى يبقى السياق دقيقًا
    وصغيرًا، ولأن الدقة تجي من حساب الأرقام في بايثون لا من طلب الذكاء الاصطناعي يحسبها بنفسه."""
    today = datetime.utcnow().date()
    month_start = today.replace(day=1)
    scope_cashbox = branch_scope_cashbox_id()

    inv_q = Invoice.query.filter(Invoice.status != "voided")
    if scope_cashbox:
        inv_q = inv_q.join(Shift, Invoice.shift_id == Shift.id).filter(Shift.cashbox_id == scope_cashbox)
    day_start, day_end = local_today_range()
    today_sales = sum(i.total for i in inv_q.filter(Invoice.date >= day_start, Invoice.date < day_end).all())
    month_sales = sum(i.total for i in inv_q.filter(Invoice.date >= month_start).all())

    low_stock = Product.query.filter(Product.active == True).all()  # noqa: E712
    threshold = int(get_settings()["low_stock_threshold"] or 10)
    low_stock_names = [p.display_name() for p in low_stock if p.total_quantity() <= threshold][:15]

    lines = [
        f"تاريخ اليوم: {today}",
        f"مبيعات اليوم: {today_sales:.2f}",
        f"مبيعات هذا الشهر حتى الآن: {month_sales:.2f}",
        f"عدد المنتجات منخفضة المخزون (حد {threshold}): {len(low_stock_names)}" +
        (f" - أمثلة: {', '.join(low_stock_names[:8])}" if low_stock_names else ""),
    ]
    lines += _sales_trends(scope_cashbox)

    if scope_cashbox:
        box = Cashbox.query.get(scope_cashbox)
        lines.append(f"أنت مقيَّد بفرع: {box.name if box else '-'} (الرصيد الحالي: {box.balance if box else 0:.2f})")
    else:
        total_customer_debt = sum(c.balance or 0 for c in Customer.query.filter(Customer.balance > 0).all())
        total_employee_advances = sum(e.balance or 0 for e in Employee.query.filter(Employee.balance > 0).all())
        cashboxes = Cashbox.query.filter_by(active=True).all()
        lines += [
            f"إجمالي ديون العملاء (مستحق لنا): {total_customer_debt:.2f}",
            f"إجمالي ديون الموردين (مستحق علينا، كل عملة لوحدها): {_supplier_debt_text()}",
            f"إجمالي سلف الموظفين القائمة: {total_employee_advances:.2f}",
            "أرصدة الخزائن: " + "، ".join(f"{b.name}={b.balance:.2f}" for b in cashboxes),
        ]

    mentioned = _find_mentioned_entities(question)
    if mentioned:
        lines.append("أشخاص/جهات مذكورون صراحة بالسؤال (بياناتهم الدقيقة الآن):")
        lines += [f"- {m}" for m in mentioned]

    return "\n".join(lines)


# موديلات Gemini بالترتيب: إن كان الأول غير متاح (404 = موديل أُوقف) نجرّب التالي. جوجل توقف موديلاتها
# القديمة دوريًا (gemini-2.0-flash أُوقف في 1 يونيو 2026)، فلا نعتمد على اسم واحد ثابت.
GEMINI_MODELS = ["gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash"]


def _gemini_generate(api_key, prompt):
    """استدعاء Gemini بنص جاهز. يعيد (النص, None) أو (None, رسالة خطأ واضحة)."""
    # temperature منخفضة عمدًا (دقة/اتساق أهم من الإبداع هنا). الحد أعلى من قبل لأن موديلات 3.x
    # "تفكّر" وقد تستهلك جزءًا من الحد قبل كتابة الرد.
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 4000},  # التحليلات أطول من الأجوبة السريعة
    }
    last_status = None
    try:
        for model in GEMINI_MODELS:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            resp = requests.post(url, json=payload, headers={"x-goog-api-key": api_key}, timeout=90)
            last_status = resp.status_code
            if resp.status_code == 404:
                continue  # هذا الموديل غير متاح (أُوقف) - جرّب التالي
            if resp.status_code != 200:
                if resp.status_code in (400, 401, 403):
                    return None, f"خدمة Gemini رفضت الطلب (كود {resp.status_code}) - تحقق من صحة المفتاح وصلاحيته."
                if resp.status_code == 429:
                    return None, "تجاوزت حد الاستخدام المجاني لـ Gemini - حاول بعد قليل."
                return None, f"تعذّر الاتصال بخدمة Gemini (كود {resp.status_code})."
            data = resp.json()
            parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
            if text:
                return text, None
            return None, "رد غير متوقع من خدمة Gemini (رد فارغ) - أعد المحاولة."
        return None, f"موديلات Gemini المضبوطة غير متاحة (كود {last_status}) - حدّث قائمة GEMINI_MODELS في app.py."
    except requests.RequestException:
        return None, "تعذّر الاتصال بخدمة Gemini - تحقق من الإنترنت."
    except (KeyError, IndexError, ValueError):
        return None, "رد غير متوقع من خدمة Gemini."


def _claude_generate(api_key, system, prompt):
    """استدعاء Claude بنص جاهز. يعيد (النص, None) أو (None, رسالة خطأ)."""
    url = "https://api.anthropic.com/v1/messages"
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 2500,
        "temperature": 0.2,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=90)
        if resp.status_code != 200:
            return None, f"تعذّر الاتصال بخدمة Claude (كود {resp.status_code}) - تحقق من صحة المفتاح."
        data = resp.json()
        text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text").strip()
        return (text, None) if text else (None, "رد غير متوقع من خدمة Claude (رد فارغ).")
    except requests.RequestException:
        return None, "تعذّر الاتصال بخدمة Claude - تحقق من الإنترنت."
    except (KeyError, IndexError, ValueError):
        return None, "رد غير متوقع من خدمة Claude."


def _ai_generate(system, prompt):
    """يختار مزوّد الذكاء الاصطناعي حسب إعداد ai_provider (gemini/claude) ويستدعيه بمفتاحه الخاص.
    يُرجع (text, error) - error نص واضح للمستخدم عند أي مشكلة (مفتاح غير مُضاف، فشل اتصال...)."""
    s = get_settings()
    provider = s["ai_provider"] if s["ai_provider"] in ("gemini", "claude") else "gemini"
    if provider == "claude":
        if not s["claude_api_key"]:
            return None, "اخترت Claude كمزوّد لكن ما فيه مفتاح مُضاف - أضفه من إعدادات النظام."
        return _claude_generate(s["claude_api_key"], system, prompt)
    if not s["gemini_api_key"]:
        return None, "اخترت Gemini كمزوّد لكن ما فيه مفتاح مُضاف - أضفه من إعدادات النظام."
    return _gemini_generate(s["gemini_api_key"], f"{system}\n\n{prompt}")


def _call_ai(question, snapshot):
    """المسار البسيط (بدون استعلامات SQL): لقطة بيانات واحدة + السؤال. يُستخدم للمستخدم المقيَّد بفرع."""
    prompt = f"{AI_ASSISTANT_PAGE_GUIDE}\n\nلقطة البيانات الحالية:\n{snapshot}\n\nسؤال المستخدم: {question}"
    return _ai_generate(AI_ASSISTANT_SYSTEM_PROMPT, prompt)


# ---------------------------------------------------------------------------
# مساعد "يجيب عن أي رقم": يكتب استعلام SELECT للقراءة فقط، ينفّذه النظام على قاعدة البيانات،
# ثم يجيب من النتيجة الفعلية (حتى 4 استعلامات متتابعة للسؤال الواحد).
# الأمان: اتصال query_only + authorizer يمنع أي شيء غير القراءة، ويحجب جدول الإعدادات (مفاتيح API)
# وأعمدة كلمات المرور/الرموز، مع حدّ للصفوف وللوقت. ولا يُفعَّل إلا للمدير/الأدمن غير المقيَّد بفرع.
# ---------------------------------------------------------------------------
AI_SQL_MAX_ROWS = 200
AI_SQL_MAX_CHARS = 9000
AI_SQL_MAX_ROUNDS = 4
AI_SQL_BLOCKED_TABLES = {"settings"}
AI_SQL_BLOCKED_COLUMN_WORDS = ("password", "hash", "secret", "api_key", "token")

AI_SQL_PROTOCOL = """
لديك أداة قراءة فقط على قاعدة بيانات النظام (SQLite). للحصول على أي رقم أو بيان غير موجود حرفيًا في
"لقطة البيانات":
- اكتب الاستعلام وحده داخل ```sql ... ``` بدون أي كلام آخر (استعلام SELECT واحد، يجوز WITH/JOIN/
  GROUP BY/ORDER BY/LIMIT). ستصلك النتيجة ثم تكتب استعلامًا آخر أو الإجابة النهائية.
- حين تكتفي بالمعلومات، اكتب الإجابة النهائية للمستخدم نصًا عاديًا (بدون ```sql).
قواعد الاستعلام:
- لا تستخدم SELECT * على جداول كبيرة؛ اختر الأعمدة اللازمة وحدّد LIMIT مناسبًا.
- أي جمع أو عدّ أو متوسط أو فرق: احسبه داخل SQL (SUM/COUNT/AVG) ولا تحسبه بالذهن.
- ابحث عن الأسماء بـ LIKE '%كلمة%' لأن الاسم قد لا يطابق حرفيًا. إن رجع أكثر من تطابق فاذكرها كلها أو اسأل.
- إن فشل استعلامك (خطأ)، صحّحه وأعد المحاولة.
- لا تذكر للمستخدم أسماء الجداول أو الأعمدة أو الاستعلام نفسه؛ أجب بلغة بسيطة وبالأرقام.
"""

AI_SQL_HINTS = """
- التواريخ مخزنة بتوقيت UTC، وليبيا = UTC+2: لعرض/تجميع حسب اليوم استخدم date(العمود, '+2 hours').
  تاريخ اليوم المعطى في اللقطة هو تاريخ UTC.
- المبيعات: جدول invoices (استثنِ status = 'voided' دائمًا)، total = الإجمالي، cash_amount/bank_amount نقد
  وبنك، credit_amount = الآجل على العميل. أصناف الفاتورة في invoice_items، والمرتجعات في sales_returns.
- الأرصدة: customers.balance موجب = دين على العميل. suppliers.balance موجب = مستحق للمورد (بعملته).
  employees.balance = سلف قائمة على الموظف. external_borrowers.balance موجب = له عندنا، سالب = علينا له.
  cashboxes.balance = رصيد الخزينة الحالي.
- account_entries (الحسابات): entry_type من: expense (مصروف)، customer_payment، customer_debt، customer_discount،
  supplier_payment (دفعة/سند صرف لمورد)، employee_advance، employee_repayment، employee_draw، external_advance،
  external_repayment، loan_received، loan_repayment، salary_payment، owner_withdrawal، inventory_loss، sales_return.
  payment_method = cash أو bank. الربط بالطرف عبر customer_id/supplier_id/employee_id/external_borrower_id.
- المشتريات: purchases (foreign_total بعملة الفاتورة، local_total بالدينار، paid_amount المدفوع، currency_code،
  exchange_rate) وبنودها purchase_items. دفعة المورد في account_entries: amount = ما خرج بالدينار،
  foreign_amount = ما وصل المورد بعملته (إن كان NULL فهو amount/exchange_rate)، exchange_rate = السعر.
  كشف حساب المورد = purchases + supplier_debts (دائن) مقابل
  account_entries حيث entry_type='supplier_payment' (مدين).
- المخزون الحالي لكل مخزن في جدول stocks (product_id, warehouse_id, quantity). products.purchase_price = متوسط
  تكلفة الشراء الحالية، products.sale_price = سعر البيع. الربح = (unit_price - invoice_items.unit_cost) × الكمية
  (unit_cost = تكلفة القطعة لحظة بيعها).
- أداء منتج/مقاس/لون: إيراد السطر الصافي = invoice_items.total × (invoices.total / invoices.subtotal) لتوزيع خصم
  الفاتورة، ثم اطرح المرتجعات: sales_return_items (quantity, total) مربوطة بـ invoice_items عبر invoice_item_id.
  المقاس واللون والتصنيف من products (size, color, category_id → categories.name).
- الورديات: shifts (opening_cash, counted_cash, cashbox_id, status). الخزائن: cashboxes، تحويلاتها cash_transfers.
"""


def _ai_sql_authorizer(action, arg1, arg2, dbname, source):
    """سماح بالقراءة فقط: SELECT وقراءة الأعمدة والدوال، مع حجب جدول الإعدادات وأعمدة الأسرار."""
    ok, deny = getattr(sqlite3, "SQLITE_OK", 0), getattr(sqlite3, "SQLITE_DENY", 1)
    if action == getattr(sqlite3, "SQLITE_READ", 20):
        table = (arg1 or "").lower()
        column = (arg2 or "").lower()
        if table in AI_SQL_BLOCKED_TABLES or table.startswith("sqlite_"):
            return deny
        if any(word in column for word in AI_SQL_BLOCKED_COLUMN_WORDS):
            return deny
        return ok
    if action in (getattr(sqlite3, "SQLITE_SELECT", 21), getattr(sqlite3, "SQLITE_FUNCTION", 31),
                  getattr(sqlite3, "SQLITE_RECURSIVE", 33)):
        return ok
    return deny


def _ai_db_path():
    return db.engine.url.database


def _ai_schema_text():
    """وصف مختصر لجداول وأعمدة قاعدة البيانات (بدون الجداول/الأعمدة المحجوبة) ليكتب منه الذكاء الاصطناعي
    استعلاماته. يُقرأ من القاعدة نفسها فيتحدّث تلقائيًا مع أي عمود جديد."""
    conn = sqlite3.connect(_ai_db_path(), timeout=5)
    try:
        lines = []
        names = [r[0] for r in conn.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name")]
        for name in names:
            if name.lower() in AI_SQL_BLOCKED_TABLES:
                continue
            cols = []
            for _cid, col, ctype, *_rest in conn.execute(f'pragma table_info("{name}")'):
                if any(w in col.lower() for w in AI_SQL_BLOCKED_COLUMN_WORDS):
                    continue
                cols.append(col + ("(dt)" if (ctype or "").upper().startswith("DATE") else ""))
            lines.append(f"{name}({', '.join(cols)})")
        return "\n".join(lines)
    finally:
        conn.close()


def _run_ai_sql(sql):
    """ينفّذ استعلام قراءة فقط ويعيد (نص النتيجة, None) أو (None, رسالة خطأ)."""
    import re
    sql = (sql or "").strip().rstrip(";").strip()
    if ";" in sql:
        return None, "مسموح باستعلام واحد فقط (بدون فاصلة منقوطة)."
    if not re.match(r"^(select|with)\b", sql, re.I):
        return None, "مسموح فقط باستعلامات SELECT."
    conn = sqlite3.connect(_ai_db_path(), timeout=5)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.set_authorizer(_ai_sql_authorizer)
        ticks = [0]

        def _guard():
            ticks[0] += 1
            return 1 if ticks[0] > 3000 else 0  # ~30 مليون تعليمة كحد أقصى ثم يُقطع الاستعلام

        conn.set_progress_handler(_guard, 10000)
        cur = conn.execute(sql)
        cols = [d[0] for d in (cur.description or [])]
        rows = cur.fetchmany(AI_SQL_MAX_ROWS + 1)
    except sqlite3.Error as exc:
        return None, f"خطأ في الاستعلام: {exc}"
    finally:
        conn.close()
    truncated = len(rows) > AI_SQL_MAX_ROWS
    rows = rows[:AI_SQL_MAX_ROWS]

    def fmt(v):
        if v is None:
            return ""
        return str(round(v, 4)) if isinstance(v, float) else str(v)

    text = "\n".join([" | ".join(cols)] + [" | ".join(fmt(v) for v in r) for r in rows])
    if not rows:
        text += "\n(لا توجد صفوف)"
    if truncated:
        text += f"\n(اقتُصرت النتيجة على أول {AI_SQL_MAX_ROWS} صفًا)"
    if len(text) > AI_SQL_MAX_CHARS:
        text = text[:AI_SQL_MAX_CHARS] + "\n... (قُصّت النتيجة لطولها)"
    return text, None


def _extract_sql(text):
    """يستخرج الاستعلام من رد النموذج (داخل ```sql أو رد يبدأ بـ SELECT/WITH)، وإلا None (= إجابة نهائية)."""
    import re
    m = re.search(r"```(?:sql)?\s*(.*?)```", text, re.S | re.I)
    candidate = (m.group(1) if m else text).strip()
    return candidate if re.match(r"^(select|with)\b", candidate, re.I) else None


def _ask_ai_agent(question, snapshot, executed):
    """حلقة السؤال: النموذج يكتب استعلامًا → ننفّذه → يرى النتيجة → استعلام آخر أو الإجابة النهائية.
    executed: قائمة تُملأ بالاستعلامات المنفَّذة (للتدقيق في سجل العمليات)."""
    base = (f"{AI_ASSISTANT_PAGE_GUIDE}\n{AI_SQL_PROTOCOL}\n"
            f"=== هيكل قاعدة البيانات (أسماء الجداول والأعمدة) ===\n{_ai_schema_text()}\n"
            f"=== ملاحظات مهمة عن البيانات ===\n{AI_SQL_HINTS}\n"
            f"=== لقطة البيانات الحالية ===\n{snapshot}\n"
            f"=== سؤال المستخدم ===\n{question}")
    transcript = ""
    for round_no in range(AI_SQL_MAX_ROUNDS + 1):
        last = round_no == AI_SQL_MAX_ROUNDS
        prompt = base + transcript
        if last:
            prompt += "\n\nوصلت للحد الأقصى من الاستعلامات: اكتب الإجابة النهائية الآن بما لديك من نتائج."
        text, err = _ai_generate(AI_ASSISTANT_SYSTEM_PROMPT, prompt)
        if err:
            return None, err
        sql = None if last else _extract_sql(text)
        if sql is None:
            return text.strip(), None
        result, sql_error = _run_ai_sql(sql)
        executed.append(sql)
        transcript += (f"\n\n--- استعلام {round_no + 1} ---\n{sql}\n--- النتيجة ---\n"
                       f"{result if result is not None else 'فشل: ' + sql_error}")
    return None, "ما قدرت أصل لإجابة بعد عدة محاولات - جرّب صياغة السؤال بشكل أوضح."


@app.route("/ai/ask", methods=["POST"])
@login_required
@permission_required("ai_assistant")
def ai_ask():
    question = (request.json or {}).get("question", "").strip() if request.is_json else request.form.get("question", "").strip()
    if not question:
        return jsonify({"error": "اكتب سؤالك أولاً."}), 400
    s = get_settings()
    if not s["gemini_api_key"] and not s["claude_api_key"]:
        return jsonify({"error": "مساعد الذكاء الاصطناعي غير مُفعَّل - أضف مفتاح Gemini أو Claude من صفحة إعدادات النظام (للأدمن)."}), 400
    snapshot = _build_ai_snapshot(question)
    executed = []
    if current_user.is_manager_or_admin() and not branch_scope_cashbox_id():
        answer, error = _ask_ai_agent(question, snapshot, executed)
    else:
        answer, error = _call_ai(question, snapshot)  # المقيَّد بفرع/الكاشير: لقطة فرعه فقط، بدون SQL
    if error:
        return jsonify({"error": error}), 502
    details = question[:200] + (" | SQL: " + " ;; ".join(executed)[:700] if executed else "")
    log_action("ai_ask", "ai_assistant", details=details)
    db.session.commit()
    return jsonify({"answer": answer})


# ---------------------------------------------------------------------------
# تسجيل الدخول / الخروج
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        user = User.query.filter_by(username=username, active=True).first()
        if user and user.check_password(password):
            login_user(user)
            log_action("login", "user", user.id, f"تسجيل دخول: {user.username}")
            db.session.commit()
            return redirect(url_for("dashboard"))
        flash("اسم المستخدم أو كلمة المرور غير صحيحة", "danger")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    log_action("logout", "user", current_user.id)
    db.session.commit()
    logout_user()
    return redirect(url_for("login"))


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current_pw = request.form.get("current_password")
        new_pw = request.form.get("new_password")
        confirm_pw = request.form.get("confirm_password")
        if not current_user.check_password(current_pw):
            flash("كلمة المرور الحالية غير صحيحة", "danger")
        elif new_pw != confirm_pw:
            flash("كلمة المرور الجديدة غير متطابقة", "danger")
        elif len(new_pw) < 4:
            flash("كلمة المرور الجديدة قصيرة جدًا", "danger")
        else:
            current_user.set_password(new_pw)
            log_action("change_password", "user", current_user.id)
            db.session.commit()
            flash("تم تغيير كلمة المرور بنجاح", "success")
            return redirect(url_for("dashboard"))
    return render_template("change_password.html")


# ---------------------------------------------------------------------------
# لوحة التحكم
# ---------------------------------------------------------------------------
@app.route("/")
@login_required
def dashboard():
    day_start, day_end = local_today_range()
    today_invoices = Invoice.query.filter(
        Invoice.date >= day_start, Invoice.date < day_end, Invoice.status != "voided"
    ).all()
    total_today = sum(i.total for i in today_invoices)

    context = {"total_today": total_today, "sales_count": len(today_invoices)}

    if current_user.is_manager_or_admin():
        low_stock = [p for p in Product.query.filter_by(active=True).all()
                     if p.total_quantity() <= app.config["LOW_STOCK_THRESHOLD"]]
        overdue_customers = Customer.query.filter(Customer.balance > 0).order_by(Customer.balance.desc()).all()
        context.update({
            "low_stock": low_stock,
            "total_products": Product.query.filter_by(active=True).count(),
            "total_customers_debt": sum(c.balance for c in Customer.query.all()),
            "top_debtors": overdue_customers[:5],
            "pending_approvals": ApprovalRequest.query.filter_by(status="pending").count(),
        })
    return render_template("dashboard.html", **context)


# ---------------------------------------------------------------------------
# التصنيفات / المخازن (مدير أو أدمن فقط للإضافة، الكاشير يمكنه العرض فقط)
# ---------------------------------------------------------------------------
@app.route("/categories", methods=["GET", "POST"])
@login_required
@permission_required("categories")
def categories():
    if request.method == "POST":
        if not current_user.has_permission("categories"):
            flash("لا تملك صلاحية الإضافة", "danger")
            return redirect(url_for("categories"))
        name = request.form.get("name", "").strip()
        if name:
            db.session.add(Category(name=name))
            log_action("create_category", "category", details=name)
            db.session.commit()
            flash("تمت إضافة التصنيف", "success")
        return redirect(url_for("categories"))
    return render_template("categories.html", categories=Category.query.all())


@app.route("/categories/edit/<int:cid>", methods=["POST"])
@login_required
@permission_required("categories")
def edit_category(cid):
    c = Category.query.get_or_404(cid)
    name = request.form.get("name", "").strip()
    if name:
        old = c.name
        c.name = name
        log_action("edit_category", "category", cid, f"{old} -> {name}")
        db.session.commit()
        flash("تم تعديل التصنيف", "success")
    return redirect(url_for("categories"))


@app.route("/categories/delete/<int:cid>")
@login_required
@permission_required("categories")
def delete_category(cid):
    c = Category.query.get_or_404(cid)
    log_action("delete_category", "category", cid, c.name)
    db.session.delete(c)
    db.session.commit()
    flash("تم حذف التصنيف", "info")
    return redirect(url_for("categories"))


@app.route("/warehouses", methods=["GET", "POST"])
@login_required
@permission_required("warehouses")
def warehouses():
    if request.method == "POST":
        if not current_user.has_permission("warehouses"):
            flash("لا تملك صلاحية الإضافة", "danger")
            return redirect(url_for("warehouses"))
        db.session.add(Warehouse(name=request.form.get("name"), location=request.form.get("location")))
        db.session.commit()
        flash("تمت إضافة المخزن", "success")
        return redirect(url_for("warehouses"))
    return render_template("warehouses.html", warehouses=Warehouse.query.all())


@app.route("/warehouses/edit/<int:wid>", methods=["POST"])
@login_required
@permission_required("warehouses")
def edit_warehouse(wid):
    w = Warehouse.query.get_or_404(wid)
    w.name = request.form.get("name") or w.name
    w.location = request.form.get("location")
    log_action("edit_warehouse", "warehouse", wid, w.name)
    db.session.commit()
    flash("تم تعديل المخزن", "success")
    return redirect(url_for("warehouses"))


@app.route("/warehouses/delete/<int:wid>")
@login_required
@permission_required("warehouses")
def delete_warehouse(wid):
    w = Warehouse.query.get_or_404(wid)
    db.session.delete(w)
    db.session.commit()
    flash("تم حذف المخزن", "info")
    return redirect(url_for("warehouses"))


@app.route("/warehouses/<int:wid>")
@login_required
@permission_required("warehouses")
def warehouse_detail(wid):
    warehouse = Warehouse.query.get_or_404(wid)
    search = request.args.get("q", "").strip()
    q = Stock.query.filter_by(warehouse_id=wid).join(Product, Stock.product_id == Product.id)
    if search:
        q = q.filter(Product.name.ilike(f"%{search}%"))
    stocks = q.order_by(Product.name).all()

    usd_rate = float(Setting.get("usd_exchange_rate", "5.00") or 5.00) or 1.0
    rows = []
    total_value_local = 0.0
    for s in stocks:
        if not s.product:
            continue
        value_local = round((s.quantity or 0) * (s.product.purchase_price or 0), 2)
        total_value_local += value_local
        rows.append({
            "product": s.product, "quantity": s.quantity or 0,
            "cost": s.product.purchase_price or 0, "value_local": value_local,
            "cost_usd": round((s.product.purchase_price or 0) / usd_rate, 2),
            "value_usd": round(value_local / usd_rate, 2),
        })
    total_value_usd = round(total_value_local / usd_rate, 2)

    return render_template("warehouse_detail.html", warehouse=warehouse, rows=rows, search=search,
                            total_value_local=total_value_local, total_value_usd=total_value_usd,
                            currency=app.config["CURRENCY"], usd_rate=usd_rate)


@app.route("/warehouses/<int:wid>/remove-products", methods=["POST"])
@login_required
@permission_required("warehouses")
def warehouse_remove_products(wid):
    """إزالة منتجات محددة من مخزن (مثل أصناف دخلت بالخطأ أو بالاستيراد وليست من منتجاتك). يُسجَّل لكل منتج
    ذي رصيد حركة إخراج في سجل المخزون وفي سجل العمليات. خيار purge يوقف المنتج من قائمة المنتجات أيضًا،
    لكن فقط إن لم يكن له رصيد في مخزن آخر (الإيقاف لا يمسح التاريخ، ويمكن استعادته بإضافة نفس الباركود)."""
    if not current_user.is_manager_or_admin():
        flash("إزالة المنتجات من المخزن للمدير/الأدمن فقط.", "danger")
        return redirect(url_for("warehouse_detail", wid=wid))
    Warehouse.query.get_or_404(wid)
    ids = [int(x) for x in request.form.getlist("product_ids") if x.isdigit()]
    purge = request.form.get("purge_products") == "1"
    if not ids:
        flash("اختر منتجًا واحدًا على الأقل.", "warning")
        return redirect(url_for("warehouse_detail", wid=wid))
    removed = purged = lost_units = 0
    kept_active = []
    for pid in ids:
        stock = Stock.query.filter_by(product_id=pid, warehouse_id=wid).first()
        product = Product.query.get(pid)
        if not stock or not product:
            continue
        qty = stock.quantity or 0
        if qty:
            db.session.add(StockMovement(product_id=pid, warehouse_id=wid, movement_type="out",
                                          quantity=qty, reference="إزالة من المخزن"))
            lost_units += qty
        db.session.delete(stock)
        removed += 1
        if purge:
            elsewhere = sum((st.quantity or 0) for st in
                            Stock.query.filter(Stock.product_id == pid, Stock.warehouse_id != wid).all())
            if elsewhere > 0:
                kept_active.append(product.display_name())
            else:
                product.active = False
                purged += 1
        log_action("remove_from_warehouse", "warehouse", wid,
                   f"{product.name} (باركود {product.barcode}) | الكمية وقت الإزالة: {qty}" + (" | + إيقاف المنتج" if purge else ""))
    db.session.commit()
    msg = f"تمت إزالة {removed} منتج من المخزن"
    if lost_units:
        msg += f" (برصيد إجمالي {lost_units} وحدة سُجّل كحركة إخراج)"
    if purged:
        msg += f"، وأُوقف {purged} منها من قائمة المنتجات"
    if kept_active:
        msg += f". لم يُوقف من قائمة المنتجات لأن له رصيدًا في مخزن آخر: {'، '.join(kept_active)}"
    flash(msg + ".", "success")
    return redirect(url_for("warehouse_detail", wid=wid))


# ---------------------------------------------------------------------------
# المنتجات - الكاشير يرى القائمة بدون تكلفة/ربح، ولا يضيف/يعدل إلا بصلاحية
# ---------------------------------------------------------------------------
@app.route("/products/recalc-cost", methods=["GET", "POST"])
@login_required
def recalc_product_costs():
    """أداة تصحيح تكلفة المنتجات المشتراة بعملة أجنبية: الإصدارات السابقة كانت تدخل سعر الوحدة بعملة
    الشراء (دولار مثلًا) في تكلفة المنتج كأنه دينار. هنا تُعاد حساب التكلفة بالدينار كمتوسط مرجّح لكل
    سطور الشراء (سعر الوحدة × سعر صرف فاتورتها)، وتُعرض للمراجعة قبل الحفظ."""
    if not current_user.is_manager_or_admin():
        flash("هذه الأداة للمدير/الأدمن فقط.", "danger")
        return redirect(url_for("dashboard"))

    rows = []
    lines = (db.session.query(PurchaseItem, Purchase).join(Purchase, PurchaseItem.purchase_id == Purchase.id)
             .order_by(Purchase.date).all())
    by_product = {}
    for item, pur in lines:
        g = by_product.setdefault(item.product_id, {"qty": 0, "value": 0.0, "count": 0, "currencies": set(),
                                                    "foreign": False, "last": None})
        rate = pur.exchange_rate or 1
        g["qty"] += item.quantity or 0
        g["value"] += (item.quantity or 0) * ((item.unit_cost or 0) * rate + (item.extra_unit_cost or 0))
        g["count"] += 1
        g["currencies"].add(pur.currency_code or "LYD")
        if (pur.currency_code or "LYD") != "LYD" and rate != 1:
            g["foreign"] = True
        g["last"] = (item.unit_cost or 0) * rate + (item.extra_unit_cost or 0)
    for pid, g in by_product.items():
        product = Product.query.get(pid)
        if not product or not g["foreign"] or g["qty"] <= 0:
            continue
        proposed = round(g["value"] / g["qty"], 2)
        current = round(product.purchase_price or 0, 2)
        rows.append({"product": product, "count": g["count"], "qty": g["qty"],
                     "currencies": "، ".join(sorted(g["currencies"])), "current": current,
                     "proposed": proposed, "last": round(g["last"] or 0, 2),
                     "stock": product.total_quantity(), "differs": abs(current - proposed) > 0.01})
    rows.sort(key=lambda r: (not r["differs"], r["product"].name))

    if request.method == "POST":
        changed = []
        for r in rows:
            p = r["product"]
            if not request.form.get(f"apply_{p.id}"):
                continue
            try:
                new_cost = _parse_money(request.form.get(f"cost_{p.id}"), r["proposed"])
            except ValueError:
                flash(f'قيمة غير صالحة لتكلفة "{p.display_name()}".', "danger")
                return redirect(url_for("recalc_product_costs"))
            if new_cost < 0:
                flash(f'التكلفة لا تكون سالبة ("{p.display_name()}").', "danger")
                return redirect(url_for("recalc_product_costs"))
            if abs((p.purchase_price or 0) - new_cost) > 0.0001:
                changed.append(f"{p.display_name()}: {p.purchase_price or 0:.2f} → {new_cost:.2f}")
                p.purchase_price = new_cost
        if changed:
            log_action("recalc_product_costs", "product", details=" | ".join(changed))
            db.session.commit()
            flash(f"تم تصحيح تكلفة {len(changed)} منتج. تقارير الأرباح وقيمة المخزون تستخدم التكلفة الجديدة الآن.", "success")
        else:
            flash("لم يتغير شيء.", "info")
        return redirect(url_for("recalc_product_costs"))

    stock_before = round(sum((r["stock"] or 0) * r["current"] for r in rows), 2)
    stock_after = round(sum((r["stock"] or 0) * r["proposed"] for r in rows), 2)
    return render_template("recalc_costs.html", rows=rows, stock_before=stock_before, stock_after=stock_after,
                           currency=app.config["CURRENCY"])


@app.route("/products")
@login_required
@permission_required("products")
def products():
    q = request.args.get("q", "")
    query = Product.query.filter_by(active=True)
    if q:
        query = query.filter(db.or_(Product.name.contains(q), Product.barcode.contains(q)))
    return render_template("products.html", products=query.all(), q=q)


@app.route("/products/<int:pid>/details")
@login_required
@permission_required("products")
def product_details(pid):
    """بطاقة المنتج: كم كانت الكمية، كم انباع (صافي بعد المرتجعات)، كم باقي، بكم انباع،
    ومن باع (الكاشير) وكم اشترى كل زبون من هذا المنتج بالذات، مع كل فاتورة بيع على حدة."""
    product = Product.query.get_or_404(pid)

    rows = (db.session.query(InvoiceItem, Invoice)
            .join(Invoice, InvoiceItem.invoice_id == Invoice.id)
            .filter(InvoiceItem.product_id == pid, Invoice.status != "voided")
            .order_by(Invoice.date.desc()).all())
    returned = {}
    if rows:
        ids = [it.id for it, _ in rows]
        for item_id, qty in (db.session.query(SalesReturnItem.invoice_item_id, db.func.sum(SalesReturnItem.quantity))
                              .filter(SalesReturnItem.invoice_item_id.in_(ids))
                              .group_by(SalesReturnItem.invoice_item_id).all()):
            returned[item_id] = qty or 0

    users = {u.id: (u.full_name or u.username) for u in User.query.all()}
    sold_qty = sold_value = returned_qty = sold_cost = 0
    by_user, by_customer, lines = {}, {}, []
    for it, inv in rows:
        r_qty = returned.get(it.id, 0)
        net_qty = (it.quantity or 0) - r_qty
        net_val = round(net_qty * (it.unit_price or 0), 2)
        sold_qty += net_qty
        sold_value += net_val
        sold_cost += net_qty * it.cost_each()  # تكلفة القطعة وقت بيعها لا تكلفتها الحالية
        returned_qty += r_qty
        seller = users.get(inv.user_id, "-")
        cust = inv.customer.name if inv.customer else "زبون نقدي"
        cid = inv.customer_id or 0
        b = by_user.setdefault(seller, {"name": seller, "qty": 0, "value": 0.0, "invoices": set()})
        b["qty"] += net_qty; b["value"] += net_val; b["invoices"].add(inv.id)
        c = by_customer.setdefault(cid, {"id": cid, "name": cust, "qty": 0, "value": 0.0, "invoices": set(), "last": None})
        c["qty"] += net_qty; c["value"] += net_val; c["invoices"].add(inv.id)
        if not c["last"] or (inv.date and inv.date > c["last"]):
            c["last"] = inv.date
        lines.append({"invoice": inv, "item": it, "returned": r_qty, "net": net_qty, "seller": seller, "customer": cust})

    for d in list(by_user.values()) + list(by_customer.values()):
        d["invoices"] = len(d["invoices"])
        d["value"] = round(d["value"], 2)

    purchased = (db.session.query(db.func.coalesce(db.func.sum(PurchaseItem.quantity), 0))
                 .filter(PurchaseItem.product_id == pid).scalar() or 0)
    stocks = [(s.warehouse.name if s.warehouse else "-", s.quantity or 0) for s in product.stocks]
    remaining = sum(q for _, q in stocks)
    return render_template(
        "product_details.html", product=product, stocks=stocks, remaining=remaining,
        original_qty=remaining + sold_qty, purchased=purchased,
        sold_qty=sold_qty, sold_value=round(sold_value, 2), returned_qty=returned_qty,
        avg_price=round(sold_value / sold_qty, 2) if sold_qty else 0,
        profit=round(sold_value - sold_cost, 2),
        by_user=sorted(by_user.values(), key=lambda x: -x["qty"]),
        by_customer=sorted(by_customer.values(), key=lambda x: -x["qty"]),
        lines=lines, currency=app.config["CURRENCY"])


@app.route("/products/add", methods=["GET", "POST"])
@login_required
@permission_required("products")
def add_product():
    if request.method == "POST":
        barcode = (request.form.get("barcode") or "").strip() or f"P{int(datetime.now().timestamp())}"
        existing = Product.query.filter_by(barcode=barcode).first()
        if existing and existing.active:
            flash(f"الباركود {barcode} مستخدم لمنتج موجود: {existing.name}. غيّر الباركود أو عدّل ذلك المنتج.", "danger")
            return redirect(url_for("add_product"))
        fields = dict(
            name=request.form.get("name"),
            category_id=request.form.get("category_id") or None,
            size=request.form.get("size"), length=request.form.get("length"), color=request.form.get("color"),
            unit=request.form.get("unit") or "قطعة",
            purchase_price=float(request.form.get("purchase_price") or 0),
            sale_price=float(request.form.get("sale_price") or 0),
        )
        restored_prev_qty = None
        if existing:
            # الباركود لمنتج "محذوف" سابقًا (الحذف إيقاف فقط ويبقى الباركود محجوزًا بقيد التفرّد). بدل الانهيار
            # باستثناء UNIQUE نستعيد المنتج بالبيانات الجديدة ونضيف الكميات المدخلة على رصيده السابق.
            product = existing
            restored_prev_qty = product.total_quantity()
            for key, value in fields.items():
                setattr(product, key, value)
            product.active = True
        else:
            product = Product(barcode=barcode, **fields)
            db.session.add(product)
        db.session.flush()
        for w in Warehouse.query.all():
            qty = int(request.form.get(f"qty_{w.id}") or 0)
            stock = Stock.query.filter_by(product_id=product.id, warehouse_id=w.id).first()
            if not stock:
                stock = Stock(product_id=product.id, warehouse_id=w.id, quantity=0)
                db.session.add(stock)
            stock.quantity = (stock.quantity or 0) + qty
            if qty:
                db.session.add(StockMovement(product_id=product.id, warehouse_id=w.id,
                                              movement_type="in", quantity=qty, reference="رصيد افتتاحي"))
        log_action("restore_product" if existing else "create_product", "product", product.id, product.name)
        db.session.commit()
        if existing:
            flash(f"كان هذا الباركود لمنتج محذوف سابقًا، فتمت استعادته بالبيانات الجديدة وأُضيفت الكميات على رصيده "
                  f"السابق ({restored_prev_qty}).", "success")
        else:
            flash("تمت إضافة المنتج", "success")
        return redirect(url_for("products"))
    return render_template("product_form.html", categories=Category.query.all(),
                            warehouses=Warehouse.query.all(), product=None)


def _auto_barcode(used=()):
    """باركود تلقائي فريد بصيغة P<الوقت><رقم> (يتجنب المحجوز في القاعدة وفي نفس الدفعة)."""
    base = int(datetime.now().timestamp())
    n = 0
    while True:
        code = f"P{base}{n:02d}"
        if code not in used and not Product.query.filter_by(barcode=code).first():
            return code
        n += 1


def _save_product_row(fields, barcode):
    """ينشئ منتجًا جديدًا أو يستعيد منتجًا محذوفًا (موقوفًا) بنفس الباركود لأن الباركود فريد في القاعدة.
    على المستدعي التأكد قبلًا أن الباركود ليس لمنتج فعّال. يعيد (المنتج, هل_استُعيد, الرصيد_السابق)."""
    existing = Product.query.filter_by(barcode=barcode).first()
    if existing:
        prev_qty = existing.total_quantity()
        for key, value in fields.items():
            setattr(existing, key, value)
        existing.active = True
        return existing, True, prev_qty
    product = Product(barcode=barcode, **fields)
    db.session.add(product)
    return product, False, 0


def _ensure_stock_rows(product):
    """صف مخزون (برصيد صفر) لكل مخزن، مثل إضافة المنتج العادية. يعيد قاموس {warehouse_id: Stock}."""
    stocks = {}
    for w in Warehouse.query.all():
        stock = Stock.query.filter_by(product_id=product.id, warehouse_id=w.id).first()
        if not stock:
            stock = Stock(product_id=product.id, warehouse_id=w.id, quantity=0)
            db.session.add(stock)
        stocks[w.id] = stock
    return stocks


@app.route("/products/bulk-add", methods=["GET", "POST"])
@login_required
@permission_required("products")
def bulk_add_products():
    """إضافة عدة منتجات دفعة واحدة (جدول أسطر أو لصق من Excel). كل شيء أو لا شيء: لو في أي سطر خطأ لا يُحفظ
    شيء وتُعاد الصفحة بكل ما أدخلته مع قائمة الأخطاء. باركود محذوف سابقًا يُستعاد، وباركود فارغ يُولَّد."""
    if not current_user.is_manager_or_admin():
        flash("إضافة المنتجات للمدير/الأدمن فقط.", "danger")
        return redirect(url_for("products"))
    categories = Category.query.all()
    warehouses = Warehouse.query.all()
    if request.method != "POST":
        return render_template("products_bulk.html", categories=categories, warehouses=warehouses,
                               rows=[], errors=[], warehouse_id=None)

    f = request.form
    cols = {k: f.getlist(f"{k}[]") for k in
            ("name", "barcode", "category_id", "size", "color", "purchase_price", "sale_price", "qty")}
    warehouse_id = f.get("warehouse_id", type=int)
    rows, errors, seen = [], [], set()
    for i in range(len(cols["name"])):
        raw = {k: (cols[k][i] if i < len(cols[k]) else "").strip() for k in cols}
        if not any(raw.values()):
            continue  # سطر فارغ
        line = f"سطر {len(rows) + 1}"
        rows.append(raw)
        if not raw["name"]:
            errors.append(f"{line}: اسم المنتج مطلوب.")
            continue
        try:
            raw["_buy"] = _parse_money(raw["purchase_price"])
            raw["_sell"] = _parse_money(raw["sale_price"])
            raw["_qty"] = int(_parse_money(raw["qty"]))
        except ValueError:
            errors.append(f"{line} ({raw['name']}): الأسعار والكمية يجب أن تكون أرقامًا.")
            continue
        if raw["_buy"] < 0 or raw["_sell"] < 0 or raw["_qty"] < 0:
            errors.append(f"{line} ({raw['name']}): الأسعار والكمية لا تكون سالبة.")
        if raw["barcode"]:
            if raw["barcode"] in seen:
                errors.append(f"{line} ({raw['name']}): الباركود {raw['barcode']} مكرر داخل نفس الدفعة.")
            seen.add(raw["barcode"])
            clash = Product.query.filter_by(barcode=raw["barcode"], active=True).first()
            if clash:
                errors.append(f"{line} ({raw['name']}): الباركود {raw['barcode']} مستخدم لمنتج موجود: {clash.name}.")
        if raw.get("_qty", 0) > 0 and not warehouse_id:
            errors.append(f"{line} ({raw['name']}): اختر المخزن الذي ستُسجَّل فيه الكميات.")
    if not rows and not errors:
        errors.append("لم تُدخل أي منتج.")
    if errors:
        # نُبقي كل ما أدخله المستخدم ولا نحفظ شيئًا (لا حفظ جزئي)
        return render_template("products_bulk.html", categories=categories, warehouses=warehouses,
                               rows=rows, errors=list(dict.fromkeys(errors)), warehouse_id=warehouse_id)

    created = restored = 0
    for idx, raw in enumerate(rows):
        barcode = raw["barcode"] or _auto_barcode(seen)
        seen.add(barcode)
        fields = dict(name=raw["name"], category_id=int(raw["category_id"]) if raw["category_id"] else None,
                      size=raw["size"] or None, color=raw["color"] or None, unit="قطعة",
                      purchase_price=raw["_buy"], sale_price=raw["_sell"])
        product, was_restored, _prev = _save_product_row(fields, barcode)
        db.session.flush()
        stocks = _ensure_stock_rows(product)
        if raw["_qty"] > 0 and warehouse_id in stocks:
            stocks[warehouse_id].quantity = (stocks[warehouse_id].quantity or 0) + raw["_qty"]
            db.session.add(StockMovement(product_id=product.id, warehouse_id=warehouse_id, movement_type="in",
                                          quantity=raw["_qty"], reference="رصيد افتتاحي"))
        restored += 1 if was_restored else 0
        created += 0 if was_restored else 1
    log_action("bulk_add_products", "product", None, f"جديد={created} | مستعاد={restored}")
    db.session.commit()
    msg = f"تمت إضافة {created} منتج"
    if restored:
        msg += f" واستعادة {restored} منتج محذوف سابقًا بنفس الباركود (أُضيفت الكميات على رصيده السابق)"
    flash(msg + ".", "success")
    return redirect(url_for("products"))


@app.route("/products/quick-add", methods=["POST"])
@login_required
@permission_required("purchases")
def quick_add_product():
    """إضافة منتج جديد سريعة من داخل فاتورة الشراء (JSON). الرصيد يبدأ صفرًا ويدخل بالفاتورة نفسها، وسعر
    الشراء يتحدد من الفاتورة (متوسط مرجّح). باركود موجود لمنتج فعّال يُرجع المنتج الموجود (409)."""
    if not (current_user.is_manager_or_admin() or current_user.has_permission("products")):
        return jsonify({"error": "لا تملك صلاحية إضافة منتجات."}), 403
    f = request.form
    name = (f.get("name") or "").strip()
    if not name:
        return jsonify({"error": "اسم المنتج مطلوب."}), 400
    try:
        sale_price = _parse_money(f.get("sale_price"))
    except ValueError:
        return jsonify({"error": "سعر البيع يجب أن يكون رقمًا."}), 400
    if sale_price < 0:
        return jsonify({"error": "سعر البيع لا يكون سالبًا."}), 400
    barcode = (f.get("barcode") or "").strip() or _auto_barcode()
    existing = Product.query.filter_by(barcode=barcode).first()
    if existing and existing.active:
        return jsonify({"error": f"الباركود مستخدم لمنتج موجود: {existing.name}",
                        "existing": {"id": existing.id, "name": existing.name, "barcode": existing.barcode,
                                     "purchase_price": existing.purchase_price or 0}}), 409
    fields = dict(name=name, category_id=f.get("category_id", type=int), size=(f.get("size") or "").strip() or None,
                  color=(f.get("color") or "").strip() or None, unit=(f.get("unit") or "").strip() or "قطعة",
                  purchase_price=0, sale_price=sale_price)
    if existing:
        fields.pop("purchase_price")  # منتج مستعاد: نبقي تكلفته القديمة
    product, was_restored, _prev = _save_product_row(fields, barcode)
    db.session.flush()
    _ensure_stock_rows(product)
    log_action("restore_product" if was_restored else "create_product", "product", product.id,
               f"{product.name} (إضافة سريعة من فاتورة شراء)")
    db.session.commit()
    return jsonify({"id": product.id, "name": product.name, "barcode": product.barcode,
                    "purchase_price": product.purchase_price or 0, "restored": was_restored})


@app.route("/products/edit/<int:pid>", methods=["GET", "POST"])
@login_required
@permission_required("products")
def edit_product(pid):
    product = Product.query.get_or_404(pid)
    if request.method == "POST":
        can_edit_price = current_user.has_permission("products") and (current_user.can_override_price or current_user.is_manager_or_admin())
        old_price = product.sale_price
        new_barcode = (request.form.get("barcode") or "").strip() or product.barcode
        clash = Product.query.filter(Product.barcode == new_barcode, Product.id != product.id).first()
        if clash:
            flash(f"الباركود {new_barcode} مستخدم لمنتج آخر: {clash.name}{' (محذوف)' if not clash.active else ''}.", "danger")
            return redirect(url_for("edit_product", pid=pid))
        product.name = request.form.get("name")
        product.barcode = new_barcode
        product.category_id = request.form.get("category_id") or None
        product.size = request.form.get("size")
        product.length = request.form.get("length")
        product.color = request.form.get("color")
        product.unit = request.form.get("unit") or "قطعة"
        if current_user.is_manager_or_admin():
            product.purchase_price = float(request.form.get("purchase_price") or 0)
        new_price = float(request.form.get("sale_price") or product.sale_price)
        if can_edit_price:
            product.sale_price = new_price
            if new_price != old_price:
                log_action("edit_price", "product", product.id,
                           f"{product.name}: {old_price} -> {new_price}")
        elif new_price != old_price:
            flash("لا تملك صلاحية تعديل السعر يدويًا", "warning")
        db.session.commit()
        flash("تم تحديث المنتج", "success")
        return redirect(url_for("products"))
    if not current_user.is_manager_or_admin():
        flash("العرض فقط - لا تملك صلاحية التعديل الكامل", "warning")
    return render_template("product_form.html", categories=Category.query.all(),
                            warehouses=Warehouse.query.all(), product=product)


@app.route("/products/delete/<int:pid>")
@login_required
@permission_required("products")
def delete_product(pid):
    product = Product.query.get_or_404(pid)
    product.active = False
    log_action("delete_product", "product", pid, product.name)
    db.session.commit()
    flash("تم حذف المنتج", "info")
    return redirect(url_for("products"))


# ---------------------------------------------------------------------------
# العملاء - سقف الائتمان وكشف حساب الذمم المدينة
# ---------------------------------------------------------------------------
@app.route("/customers")
@login_required
def customers():
    search = request.args.get("q", "").strip()
    query = Customer.query
    if search:
        query = query.filter(Customer.name.ilike(f"%{search}%"))
    return render_template("customers.html", customers=query.order_by(Customer.name).all(), search=search)


@app.route("/customers/add", methods=["GET", "POST"])
@login_required
@permission_required("customers")
def add_customer():
    if request.method == "POST":
        db.session.add(Customer(name=request.form.get("name"), phone=request.form.get("phone"),
                                 address=request.form.get("address"),
                                 credit_limit=float(request.form.get("credit_limit") or 0)))
        db.session.commit()
        flash("تمت إضافة العميل", "success")
        return redirect(url_for("customers"))
    return render_template("customer_form.html", customer=None)


@app.route("/customers/edit/<int:cid>", methods=["GET", "POST"])
@login_required
@permission_required("customers")
def edit_customer(cid):
    customer = Customer.query.get_or_404(cid)
    if request.method == "POST":
        customer.name = request.form.get("name")
        customer.phone = request.form.get("phone")
        customer.address = request.form.get("address")
        customer.credit_limit = float(request.form.get("credit_limit") or 0)
        db.session.commit()
        flash("تم التحديث", "success")
        return redirect(url_for("customers"))
    return render_template("customer_form.html", customer=customer)


@app.route("/customers/delete/<int:cid>")
@login_required
@permission_required("customers")
def delete_customer(cid):
    customer = Customer.query.get_or_404(cid)
    if customer.balance:
        flash("لا يمكن حذف عميل عليه رصيد/دين مستحق. قم بتسوية الرصيد أولاً.", "danger")
        return redirect(url_for("customers"))
    log_action("delete_customer", "customer", cid, customer.name)
    db.session.delete(customer)
    db.session.commit()
    flash("تم حذف العميل", "info")
    return redirect(url_for("customers"))


@app.route("/customers/<int:cid>/statement")
@login_required
@permission_required("customers")
def customer_statement(cid):
    customer = Customer.query.get_or_404(cid)
    invoices = Invoice.query.filter_by(customer_id=cid).order_by(Invoice.date).all()
    payments = AccountEntry.query.filter_by(customer_id=cid, entry_type="customer_payment").order_by(AccountEntry.date).all()
    manual_debts = AccountEntry.query.filter_by(customer_id=cid, entry_type="customer_debt").order_by(AccountEntry.date).all()

    # حركات الرصيد (ما يؤثر فعليًا على دين العميل: مبيعات آجلة + ديون يدوية + تحصيلات)
    movements = sorted(
        [{"date": i.date, "type": "فاتورة", "ref": i.invoice_number, "invoice_id": i.id,
          "debit": i.credit_amount, "credit": 0}
         for i in invoices if i.credit_amount > 0] +
        [{"date": d.date, "type": "دين يدوي", "ref": d.note or "-", "invoice_id": None,
          "debit": d.amount, "credit": 0}
         for d in manual_debts] +
        [{"date": p.date, "type": "تحصيل", "ref": p.note or "-", "invoice_id": None,
          "debit": 0, "credit": p.amount}
         for p in payments],
        key=lambda x: x["date"]
    )

    # ملخص مالي شامل
    total_invoiced = sum(i.total for i in invoices)
    total_credit_sales = sum(i.credit_amount for i in invoices) + sum(d.amount for d in manual_debts)
    total_paid_at_sale = sum(i.paid_amount for i in invoices)
    total_collected = sum(p.amount for p in payments)

    return render_template("customer_statement.html", customer=customer, movements=movements,
                            invoices=invoices, total_invoiced=total_invoiced,
                            total_credit_sales=total_credit_sales, total_paid_at_sale=total_paid_at_sale,
                            total_collected=total_collected, currency=app.config["CURRENCY"])


@app.route("/customers/<int:cid>/statement/export/excel")
@login_required
@permission_required("customers")
def export_customer_statement_excel_route(cid):
    from export_service import export_generic_excel
    customer = Customer.query.get_or_404(cid)
    invoices = Invoice.query.filter_by(customer_id=cid).order_by(Invoice.date).all()
    headers = ["رقم الفاتورة", "التاريخ", "طريقة الدفع", "الإجمالي", "المدفوع", "المتبقي (آجل)"]
    rows = [[i.invoice_number, to_local(i.date).strftime("%Y-%m-%d %H:%M"), i.payment_label(),
             round(i.total, 2), round(i.paid_amount, 2), round(i.credit_amount, 2)] for i in invoices]
    buf = export_generic_excel(f"كشف حساب - {customer.name}", headers, rows)
    log_action("export_customer_statement_excel", "customer", cid)
    db.session.commit()
    return send_file(buf, as_attachment=True, download_name=f"statement_{customer.name}.xlsx",
                      mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/customers/<int:cid>/statement/export/pdf")
@login_required
@permission_required("customers")
def export_customer_statement_pdf_route(cid):
    from export_service import export_generic_pdf
    customer = Customer.query.get_or_404(cid)
    invoices = Invoice.query.filter_by(customer_id=cid).order_by(Invoice.date).all()
    headers = ["رقم الفاتورة", "التاريخ", "طريقة الدفع", "الإجمالي", "المدفوع", "المتبقي (آجل)"]
    rows = [[i.invoice_number, to_local(i.date).strftime("%Y-%m-%d %H:%M"), i.payment_label(),
             round(i.total, 2), round(i.paid_amount, 2), round(i.credit_amount, 2)] for i in invoices]
    buf = export_generic_pdf(f"كشف حساب - {customer.name}", headers, rows, company=app.config["COMPANY_NAME"])
    log_action("export_customer_statement_pdf", "customer", cid)
    db.session.commit()
    return send_file(buf, as_attachment=True, download_name=f"statement_{customer.name}.pdf", mimetype="application/pdf")


# ---------------------------------------------------------------------------
# لوحة الديون والتنبيهات
# ---------------------------------------------------------------------------
def _debts_data(args):
    """كل عميل عليه دين: المبلغ، آخر تحصيل منه، آخر شراء بالآجل، وكم يومًا مرّ على آخر تحصيل.
    التواريخ تُجمع باستعلامين مجمّعين (لا استعلام لكل عميل)."""
    q = (args.get("q") or "").strip()
    sort = args.get("sort", "amount")
    query = Customer.query.filter(Customer.balance > 0.005)
    if q:
        query = query.filter(db.or_(Customer.name.ilike(f"%{q}%"), Customer.phone.ilike(f"%{q}%")))
    customers = query.all()
    ids = [c.id for c in customers]
    last_pay, last_buy = {}, {}
    if ids:
        last_pay = dict(db.session.query(AccountEntry.customer_id, db.func.max(AccountEntry.date))
                        .filter(AccountEntry.customer_id.in_(ids), AccountEntry.entry_type == "customer_payment")
                        .group_by(AccountEntry.customer_id).all())
        last_buy = dict(db.session.query(Invoice.customer_id, db.func.max(Invoice.date))
                        .filter(Invoice.customer_id.in_(ids), Invoice.credit_amount > 0, Invoice.status != "voided")
                        .group_by(Invoice.customer_id).all())
    now = datetime.utcnow()
    rows = []
    for c in customers:
        lp, lb = last_pay.get(c.id), last_buy.get(c.id)
        ref = lp or lb  # إن لم يدفع أبدًا: نحسب المدة من آخر شراء بالآجل
        rows.append({"customer": c, "balance": round(c.balance or 0, 2), "last_payment": lp, "last_purchase": lb,
                     "days": (now - ref).days if ref else None,
                     "limit_pct": round((c.balance / c.credit_limit) * 100) if c.credit_limit else None})
    if sort == "name":
        rows.sort(key=lambda r: r["customer"].name)
    elif sort == "oldest":
        rows.sort(key=lambda r: -(r["days"] if r["days"] is not None else 10 ** 6))
    else:
        rows.sort(key=lambda r: -r["balance"])
    credits = Customer.query.filter(Customer.balance < -0.005).order_by(Customer.balance).all()
    return {"rows": rows, "total": round(sum(r["balance"] for r in rows), 2), "q": q, "sort": sort,
            "credits": credits, "credits_total": round(-sum(c.balance for c in credits), 2)}


@app.route("/debts")
@login_required
@permission_required("customers")
def debts():
    data = _debts_data(request.args)
    near_limit = [r["customer"] for r in data["rows"] if r["limit_pct"] is not None and r["limit_pct"] >= 80]
    return render_template("debts.html", near_limit=near_limit, currency=app.config["CURRENCY"], **data)


@app.route("/debts/print")
@login_required
@permission_required("customers")
def debts_print():
    s = get_settings()
    return render_template("debts_print.html", company=s["company_name"], company_phone=s["company_phone"],
                           currency=s["currency"], printed_at=datetime.now(), **_debts_data(request.args))


def _debts_export_rows(data):
    rows = []
    for i, r in enumerate(data["rows"], 1):
        c = r["customer"]
        rows.append([i, c.name, c.phone or "-", r["balance"],
                     to_local(r["last_payment"]).strftime("%Y-%m-%d") if r["last_payment"] else "لم يدفع",
                     to_local(r["last_purchase"]).strftime("%Y-%m-%d") if r["last_purchase"] else "-",
                     r["days"] if r["days"] is not None else "-"])
    rows.append(["", "الإجمالي", f"{len(data['rows'])} عميل", data["total"], "", "", ""])
    return ["#", "العميل", "الهاتف", f"المبلغ المستحق ({app.config['CURRENCY']})", "آخر تحصيل",
            "آخر شراء بالآجل", "أيام منذ آخر دفعة"], rows


@app.route("/debts/export/<fmt>")
@login_required
@permission_required("customers")
def debts_export(fmt):
    from export_service import export_generic_excel, export_generic_pdf
    data = _debts_data(request.args)
    headers, rows = _debts_export_rows(data)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    log_action("export_debts", entity_type="report", details=f"{fmt}: {len(data['rows'])} عميل، {data['total']}")
    db.session.commit()
    if fmt == "excel":
        buf = export_generic_excel("ديون العملاء", headers, rows)
        return send_file(buf, as_attachment=True, download_name=f"debts_{stamp}.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    buf = export_generic_pdf(f"تقرير ديون العملاء - الإجمالي {data['total']:,.2f} {app.config['CURRENCY']}",
                             headers, rows, company=get_settings()["company_name"])
    return send_file(buf, as_attachment=True, download_name=f"debts_{stamp}.pdf", mimetype="application/pdf")



# تم دمج السندات (القبض/الصرف) داخل صفحة الحسابات، وحذفت الحسابات البنكية.

@app.route("/customers/<int:cid>/discount", methods=["GET", "POST"])
@login_required
@permission_required("customer_discounts")
def customer_discount(cid):
    customer = Customer.query.get_or_404(cid)
    if request.method == "POST":
        amount = float(request.form.get("amount") or 0)
        reason = (request.form.get("reason") or "").strip()
        invoice_id = request.form.get("invoice_id") or None
        if amount <= 0 or not reason:
            flash("أدخل مبلغ خصم صحيحًا وسبب الخصم.", "danger")
            return redirect(request.url)
        if amount > customer.balance + 0.0001:
            flash("الخصم أكبر من الرصيد المستحق على العميل.", "danger")
            return redirect(request.url)
        d=CustomerDiscount(customer_id=cid, invoice_id=invoice_id, amount=amount, reason=reason, user_id=current_user.id)
        db.session.add(d)
        customer.balance -= amount
        db.session.add(AccountEntry(entry_type="customer_discount", amount=amount,
                                    note=f"خصم مسموح: {reason}", customer_id=cid, user_id=current_user.id,
                                    local_amount=amount, reference=f"DISC-{cid}-{datetime.now().strftime('%Y%m%d%H%M%S')}",
                                    cashbox_id=branch_scope_cashbox_id()))
        log_action("customer_discount", "customer", cid, f"{amount}: {reason}")
        db.session.commit()
        flash("تم تطبيق الخصم وتخفيض رصيد العميل.", "success")
        return redirect(url_for("customer_statement", cid=cid))
    return render_template("customer_discount.html", customer=customer,
                           invoices=Invoice.query.filter_by(customer_id=cid).all())



# ---------------------------------------------------------------------------
# لوحة المخزون (Z.G ERP): الكميات الحالية والإجماليات مع أزرار عرض حسب المنتج/التصنيف/المقاس/اللون/المخزن
# ---------------------------------------------------------------------------
INVENTORY_VIEWS = {
    "products": "حسب المنتج", "category": "حسب التصنيف", "size": "حسب المقاس",
    "color": "حسب اللون", "warehouse": "حسب المخزن", "low": "منخفض المخزون", "out": "نافد",
}


def _inventory_data(args):
    """يجمع المخزون الحالي مرة واحدة باستعلام واحد على stocks، ثم يبني منه الملخص والعرض المطلوب."""
    view = args.get("view", "products")
    if view not in INVENTORY_VIEWS:
        view = "products"
    q = (args.get("q") or "").strip()
    warehouse_id = args.get("warehouse_id", type=int)
    category_id = args.get("category_id", type=int)
    threshold = int(get_settings()["low_stock_threshold"] or 5)

    products_q = Product.query.filter(Product.active == True)  # noqa: E712
    if q:
        products_q = products_q.filter(db.or_(Product.name.ilike(f"%{q}%"), Product.barcode.ilike(f"%{q}%"),
                                              Product.color.ilike(f"%{q}%"), Product.size.ilike(f"%{q}%")))
    if category_id:
        products_q = products_q.filter(Product.category_id == category_id)
    products = products_q.order_by(Product.name).all()
    ids = [p.id for p in products]

    stock_q = db.session.query(Stock.product_id, Stock.warehouse_id, Stock.quantity)
    if ids:
        stock_q = stock_q.filter(Stock.product_id.in_(ids))
    if warehouse_id:
        stock_q = stock_q.filter(Stock.warehouse_id == warehouse_id)
    per_wh = {}
    for pid, wid, qty in (stock_q.all() if ids else []):
        per_wh.setdefault(pid, {})[wid] = (per_wh.get(pid, {}).get(wid, 0)) + (qty or 0)
    warehouses = {w.id: w for w in Warehouse.query.all()}

    rows = []
    for p in products:
        breakdown = per_wh.get(p.id, {})
        qty = sum(breakdown.values())
        rows.append({
            "product": p, "qty": qty,
            "breakdown": [(warehouses[w].name if w in warehouses else f"#{w}", n) for w, n in breakdown.items() if n],
            "sale_value": round(qty * (p.sale_price or 0), 2),
            "cost_value": round(qty * (p.purchase_price or 0), 2),
            "status": "out" if qty <= 0 else ("low" if qty <= threshold else "ok"),
        })

    summary = {
        "skus": len(rows),
        "pieces": sum(max(r["qty"], 0) for r in rows),
        "in_stock": sum(1 for r in rows if r["qty"] > 0),
        "low": sum(1 for r in rows if r["status"] == "low"),
        "out": sum(1 for r in rows if r["status"] == "out"),
        "sale_value": round(sum(max(r["sale_value"], 0) for r in rows), 2),
        "cost_value": round(sum(max(r["cost_value"], 0) for r in rows), 2),
        "threshold": threshold,
    }

    groups = []
    if view in ("category", "size", "color"):
        keyf = {
            "category": lambda p: p.category.name if p.category else "بدون تصنيف",
            "size": lambda p: p.size or "بدون مقاس",
            "color": lambda p: p.color or "بدون لون",
        }[view]
        agg = {}
        for r in rows:
            g = agg.setdefault(keyf(r["product"]), {"label": keyf(r["product"]), "skus": 0, "qty": 0,
                                                     "sale_value": 0.0, "cost_value": 0.0})
            g["skus"] += 1
            g["qty"] += r["qty"]
            g["sale_value"] += r["sale_value"]
            g["cost_value"] += r["cost_value"]
        groups = sorted(agg.values(), key=lambda g: -g["qty"])
    elif view == "warehouse":
        agg = {}
        prod_by_id = {p.id: p for p in products}
        for pid, br in per_wh.items():
            for wid, n in br.items():
                label = warehouses[wid].name if wid in warehouses else f"#{wid}"
                g = agg.setdefault(label, {"label": label, "skus": 0, "qty": 0, "sale_value": 0.0, "cost_value": 0.0})
                if n > 0:
                    g["skus"] += 1
                g["qty"] += n
                g["sale_value"] += n * (prod_by_id[pid].sale_price or 0)
                g["cost_value"] += n * (prod_by_id[pid].purchase_price or 0)
        groups = sorted(agg.values(), key=lambda g: -g["qty"])
    elif view == "low":
        rows = [r for r in rows if r["status"] == "low"]
    elif view == "out":
        rows = [r for r in rows if r["status"] == "out"]
    for g in groups:
        g["sale_value"] = round(g["sale_value"], 2)
        g["cost_value"] = round(g["cost_value"], 2)
    return {"view": view, "rows": rows, "groups": groups, "summary": summary, "q": q,
            "warehouse_id": warehouse_id, "category_id": category_id}


def _can_view_inventory():
    return (current_user.is_manager_or_admin() or current_user.has_permission("inventory_view")
            or current_user.has_permission("products"))


@app.route("/inventory")
@login_required
def inventory_overview():
    if not _can_view_inventory():
        flash("لا تملك صلاحية عرض المخزون", "danger")
        return redirect(url_for("dashboard"))
    data = _inventory_data(request.args)
    return render_template("inventory_overview.html", views=INVENTORY_VIEWS,
                           warehouses=Warehouse.query.all(), categories=Category.query.order_by(Category.name).all(),
                           show_cost=current_user.is_manager_or_admin(), currency=app.config["CURRENCY"], **data)


@app.route("/inventory/export/excel")
@login_required
def inventory_export_excel():
    if not _can_view_inventory():
        flash("لا تملك صلاحية عرض المخزون", "danger")
        return redirect(url_for("dashboard"))
    from export_service import export_generic_excel
    data = _inventory_data(request.args)
    show_cost = current_user.is_manager_or_admin()
    if data["groups"]:
        headers = ["البند", "عدد الأصناف", "الكمية", "قيمة البيع"] + (["قيمة التكلفة"] if show_cost else [])
        rows = [[g["label"], g["skus"], g["qty"], g["sale_value"]] + ([g["cost_value"]] if show_cost else [])
                for g in data["groups"]]
    else:
        headers = ["المنتج", "الباركود", "التصنيف", "المقاس", "اللون", "الكمية", "سعر البيع", "قيمة البيع"] + \
                  (["قيمة التكلفة"] if show_cost else [])
        rows = [[r["product"].name, r["product"].barcode or "", r["product"].category.name if r["product"].category else "",
                 r["product"].size or "", r["product"].color or "", r["qty"], r["product"].sale_price or 0,
                 r["sale_value"]] + ([r["cost_value"]] if show_cost else []) for r in data["rows"]]
    buf = export_generic_excel(f"المخزون - {INVENTORY_VIEWS[data['view']]}", headers, rows)
    return send_file(buf, as_attachment=True, download_name=f"inventory_{data['view']}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------------------------------------------------------------------------
# ملصقات الباركود (طابعة ملصقات حرارية، الافتراضي 40×30 مم)
# الطريقة الأساسية: طباعة مباشرة (label_service: الملصق يُرسم صورة بنقاط الطابعة ويُرسل بأوامر TSPL)، لأن
# شاشة طباعة المتصفح طبعت على مقاس ورق التعريفة مع عنوان وتاريخ فخرج الملصق الواحد على 4 ملصقات.
# العربي يُرسم داخل الصورة فلا يحتاج دعم الطابعة له. صفحة المتصفح بقيت طريقة احتياطية.
# ---------------------------------------------------------------------------
LABEL_FIELDS = ("company", "name", "attrs", "price", "code")
LABEL_DEFAULTS = {"label_width": "40", "label_height": "30", "label_gap": "2",
                  "label_fields": "company,name,attrs,price,code"}
LABEL_MAX_TOTAL = 3000


def _can_print_labels():
    # من يستلم البضاعة (المشتريات) هو اللي يطبع ملصقاتها عادة
    return any(current_user.has_permission(p) for p in ("barcode_labels", "products", "purchases"))


def _label_settings():
    def num(key, lo, hi):
        try:
            return min(max(float(Setting.get(key, LABEL_DEFAULTS[key]) or LABEL_DEFAULTS[key]), lo), hi)
        except ValueError:
            return float(LABEL_DEFAULTS[key])
    from label_service import list_printers, guess_label_printer
    fields = [f for f in (Setting.get("label_fields", LABEL_DEFAULTS["label_fields"]) or "").split(",") if f in LABEL_FIELDS]
    printers = list_printers()
    saved = Setting.get("label_printer", "")
    return {"width": num("label_width", 20, 110), "height": num("label_height", 15, 80),
            "gap": num("label_gap", 0, 10), "fields": fields, "flip": bool(Setting.get("label_flip", "")),
            "printers": printers,
            "printer": saved if saved in [n for n, _ in printers] else guess_label_printer(printers)}


def _label_product_json(p, qty=1):
    return {"id": p.id, "name": p.name, "barcode": p.barcode or "", "size": p.size or "", "color": p.color or "",
            "price": p.sale_price or 0, "stock": p.total_quantity(), "qty": qty,
            "ok": is_printable_code(p.barcode or "")}


@app.route("/labels")
@login_required
def barcode_labels():
    """اختيار المنتجات وعدد الملصقات لكل واحد. يُعبّأ مسبقًا من فاتورة شراء (بنفس الكميات المستلمة)
    أو من منتج/منتجات محددة."""
    if not _can_print_labels():
        flash("لا تملك صلاحية طباعة ملصقات الباركود.", "danger")
        return redirect(url_for("dashboard"))
    rows = []
    purchase = None
    pid = request.args.get("purchase_id", type=int)
    if pid:
        purchase = Purchase.query.get_or_404(pid)
        merged = {}
        for it in purchase.items:
            if it.product:
                merged[it.product_id] = merged.get(it.product_id, 0) + (it.quantity or 0)
        rows = [_label_product_json(Product.query.get(k), v) for k, v in merged.items()]
    for prod_id in request.args.getlist("product_id", type=int):
        p = Product.query.get(prod_id)
        if p and not any(r["id"] == p.id for r in rows):
            rows.append(_label_product_json(p))
    return render_template("labels.html", rows=rows, purchase=purchase, settings=_label_settings(),
                           company=get_settings()["company_name"],
                           currency=app.config["CURRENCY"], max_total=LABEL_MAX_TOTAL)


@app.route("/labels/search")
@login_required
def labels_search():
    if not _can_print_labels():
        return jsonify([]), 403
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify([])
    exact = Product.query.filter_by(barcode=q).first()
    if exact:
        return jsonify([_label_product_json(exact)])
    if len(q) < 2:
        return jsonify([])
    like = f"%{q}%"
    found = (Product.query.filter(Product.active == True,  # noqa: E712
                                  db.or_(Product.name.ilike(like), Product.barcode.ilike(like),
                                         Product.size.ilike(like), Product.color.ilike(like)))
             .order_by(Product.name).limit(20).all())
    return jsonify([_label_product_json(p) for p in found])


def _label_text(product, fields, company):
    """النصوص فوق الباركود (بالترتيب) ونص السعر، بنفس ما يختاره المالك من الخيارات."""
    price = product.sale_price or 0
    price_txt = (f"{price:,.0f}" if float(price).is_integer() else f"{price:,.2f}") + f" {app.config['CURRENCY']}"
    attrs = " / ".join(x for x in (product.size, product.color) if x and x != "None")
    lines = []
    if "company" in fields and company:
        lines.append((company, False, 18))
    if "name" in fields:
        lines.append((product.name or "", True, 24))
    if "attrs" in fields and attrs:
        lines.append((attrs, False, 20))
    # منتج بلا سعر بيع لا يُطبع عليه "0 د.ل"
    return lines, attrs, (price_txt if "price" in fields and price > 0 else None)


def _parse_label_form(form):
    """يقرأ نموذج الملصقات، يحفظ المقاس والخيارات للمرة الجاية، ويعيد (إعدادات، [(منتج، عدد)]، أكواد مرفوضة، خطأ)."""
    try:
        width = min(max(float(form.get("width") or 40), 20), 110)
        height = min(max(float(form.get("height") or 30), 15), 80)
        gap = min(max(float(form.get("gap") or 2), 0), 10)
    except ValueError:
        width, height, gap = 40.0, 30.0, 2.0
    fields = [f for f in LABEL_FIELDS if form.get(f"f_{f}")]
    flip = bool(form.get("flip"))
    printer = (form.get("printer") or "").strip()
    # آخر مقاس وخيارات تُحفظ للمنظومة كلها (الكاشيرين يطبعون على نفس الطابعة ونفس الملصق)
    for k, v in (("label_width", f"{width:g}"), ("label_height", f"{height:g}"), ("label_gap", f"{gap:g}"),
                 ("label_fields", ",".join(fields)), ("label_flip", "1" if flip else "")):
        Setting.set(k, v)
    if printer:
        Setting.set("label_printer", printer)
    db.session.commit()
    items, skipped, total = [], [], 0
    for prod_id, raw_qty in zip(form.getlist("product_id[]"), form.getlist("qty[]")):
        try:
            product = Product.query.get(int(prod_id))
            qty = min(int(raw_qty or 0), 999)
        except ValueError:
            continue
        if not product or qty <= 0:
            continue
        if not is_printable_code(product.barcode or ""):
            skipped.append(product.name)
            continue
        total += qty
        items.append((product, qty))
    cfg = {"width": width, "height": height, "gap": gap, "fields": fields, "flip": flip, "printer": printer,
           "company": get_settings()["company_name"] if "company" in fields else "", "total": total}
    error = None
    if total > LABEL_MAX_TOTAL:
        error = f"أكثر شي {LABEL_MAX_TOTAL} ملصق في المرة الواحدة. قسّمها على أكثر من مرة."
    elif not items:
        error = "ما فيش ملصقات للطباعة." + (
            f" هذه الأكواد ما تنطبعش باركود (فيها حروف عربية): {'، '.join(skipped)}" if skipped else "")
    return cfg, items, skipped, error


@app.route("/labels/print-direct", methods=["POST"])
@login_required
def labels_print_direct():
    """يرسل الملصقات للطابعة مباشرة بمقاس الملصق (بدون شاشة طباعة المتصفح)."""
    from label_service import render_label, tspl_job, send_raw
    if not _can_print_labels():
        return jsonify({"error": "لا تملك صلاحية طباعة ملصقات الباركود."}), 403
    cfg, items, skipped, error = _parse_label_form(request.form)
    if error:
        return jsonify({"error": error}), 400
    if not cfg["printer"]:
        return jsonify({"error": "اختار طابعة الملصقات."}), 400
    labels = []
    for product, qty in items:
        lines, _, price_txt = _label_text(product, cfg["fields"], cfg["company"])
        img, _ = render_label(cfg["width"], cfg["height"], lines, product.barcode, price_txt, "code" in cfg["fields"])
        labels.append((img, qty))
    try:
        send_raw(cfg["printer"], tspl_job(labels, cfg["width"], cfg["height"], cfg["gap"], cfg["flip"]),
                 doc_name=f"ZG labels ({cfg['total']})")
    except Exception as exc:  # الطابعة مطفية/مفصولة/اسم غلط: رسالة واضحة بدل خطأ 500
        app.logger.exception("label print failed")
        return jsonify({"error": f"ما قدرتش نبعث للطابعة: {exc}"}), 500
    log_action("print_labels", "product", details=f"{cfg['total']} ملصق {cfg['width']:g}x{cfg['height']:g} → {cfg['printer']}")
    db.session.commit()
    msg = f"تم إرسال {cfg['total']} ملصق للطابعة."
    if skipped:
        msg += f" ما انطبعوش (الكود فيه حروف عربية): {'، '.join(skipped)}"
    return jsonify({"ok": True, "message": msg})


@app.route("/labels/preview.png")
@login_required
def labels_preview():
    """صورة الملصق بالضبط كما يُرسل للطابعة (نفس الرسم)، لمعاينته قبل الطباعة."""
    from label_service import render_label
    if not _can_print_labels():
        return "", 403
    product = Product.query.get(request.args.get("product_id", type=int) or 0)
    if not product or not is_printable_code(product.barcode or ""):
        return "", 404
    try:
        w = min(max(float(request.args.get("width") or 40), 20), 110)
        h = min(max(float(request.args.get("height") or 30), 15), 80)
    except ValueError:
        w, h = 40.0, 30.0
    fields = [f for f in LABEL_FIELDS if request.args.get(f"f_{f}")]
    company = get_settings()["company_name"] if "company" in fields else ""
    lines, _, price_txt = _label_text(product, fields, company)
    img, _ = render_label(w, h, lines, product.barcode, price_txt, "code" in fields)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", max_age=0)


@app.route("/labels/print", methods=["POST"])
@login_required
def labels_print():
    """الطريقة الاحتياطية: صفحة بمقاس الملصق تُطبع من شاشة طباعة المتصفح."""
    if not _can_print_labels():
        flash("لا تملك صلاحية طباعة ملصقات الباركود.", "danger")
        return redirect(url_for("dashboard"))
    cfg, items, skipped, error = _parse_label_form(request.form)
    if error:
        flash(error, "warning" if not items else "danger")
        return redirect(url_for("barcode_labels"))
    width, height, fields = cfg["width"], cfg["height"], cfg["fields"]
    bar_h = round(height * (0.42 if len(fields) >= 4 else 0.5), 1)
    labels, small = [], []
    for product, qty in items:
        svg, mw = barcode_svg(product.barcode or "", width, bar_h)
        if mw < 0.25:
            small.append(product.display_name())
        _, attrs, _ = _label_text(product, fields, cfg["company"])
        price = product.sale_price or 0
        labels.append({"svg": svg, "qty": qty, "name": product.name, "code": product.barcode, "attrs": attrs,
                       "price": (f"{price:,.0f}" if float(price).is_integer() else f"{price:,.2f}")})
    log_action("print_labels", "product", details=f"{cfg['total']} ملصق {width:g}x{height:g} (متصفح)")
    db.session.commit()
    return render_template("labels_print.html", labels=labels, width=width, height=height, fields=fields,
                           bar_h=bar_h, company=cfg["company"], currency=app.config["CURRENCY"], total=cfg["total"],
                           skipped=skipped, small=small)


@app.route("/products/export/barcodes.pdf")
@login_required
def products_barcode_pdf():
    """قائمة المنتجات PDF مع صورة باركود (Code128) لكل منتج. تقبل نفس فلاتر صفحة المخزون/المنتجات
    (بحث، مخزن، تصنيف، منخفض/نافد)، فيُطبع بالضبط ما يظهر على الشاشة."""
    if not _can_view_inventory():
        flash("لا تملك صلاحية عرض المخزون", "danger")
        return redirect(url_for("dashboard"))
    from export_service import export_products_barcode_pdf
    args = request.args.to_dict()
    if args.get("view") not in ("low", "out"):
        args["view"] = "products"
    data = _inventory_data(MultiDict(args))
    show_cost = current_user.is_manager_or_admin() and request.args.get("cost") == "1"
    rows = [{"name": r["product"].name, "barcode": r["product"].barcode or "", "size": r["product"].size or "",
             "color": r["product"].color or "", "category": r["product"].category.name if r["product"].category else "",
             "qty": r["qty"], "price": r["product"].sale_price or 0, "cost": r["product"].purchase_price or 0}
            for r in data["rows"]]
    s = get_settings()
    buf = export_products_barcode_pdf(rows, company=s["company_name"], currency=s["currency"], show_cost=show_cost)
    log_action("export_products_barcode_pdf", entity_type="report", details=f"{len(rows)} منتج")
    db.session.commit()
    return send_file(buf, as_attachment=True, mimetype="application/pdf",
                     download_name=f"products_barcodes_{datetime.now():%Y%m%d_%H%M}.pdf")


@app.route("/inventory/adjust", methods=["GET", "POST"])
@login_required
@permission_required("inventory_adjustment")
def inventory_adjust():
    if request.method == "POST":
        product_id = int(request.form.get("product_id"))
        warehouse_id = int(request.form.get("warehouse_id"))
        reason = (request.form.get("reason") or "").strip()
        mode = request.form.get("mode") or "set"  # set / increase / decrease
        if not reason:
            flash("سبب التعديل إلزامي.", "danger"); return redirect(request.url)

        stock = get_or_create_stock(product_id, warehouse_id)
        old_qty = stock.quantity or 0

        if mode == "increase":
            change = int(request.form.get("change_quantity") or 0)
            if change <= 0:
                flash("قيمة الزيادة يجب أن تكون أكبر من صفر.", "danger"); return redirect(request.url)
            new_qty = old_qty + change
        elif mode == "decrease":
            change = int(request.form.get("change_quantity") or 0)
            if change <= 0:
                flash("قيمة النقص يجب أن تكون أكبر من صفر.", "danger"); return redirect(request.url)
            new_qty = old_qty - change
        else:
            new_qty = int(request.form.get("new_quantity") or 0)

        if new_qty < 0:
            flash("الكمية الناتجة لا يمكن أن تكون سالبة.", "danger"); return redirect(request.url)

        diff = new_qty - old_qty
        adj = InventoryAdjustment(adjustment_number=generate_number("ADJ", InventoryAdjustment),
                                product_id=product_id, warehouse_id=warehouse_id, old_quantity=old_qty,
                                new_quantity=new_qty, difference=diff, reason=reason, user_id=current_user.id)
        stock.quantity = new_qty
        db.session.add(adj)
        db.session.add(StockMovement(product_id=product_id, warehouse_id=warehouse_id,
                                     movement_type="adjustment", quantity=diff,
                                     reference=adj.adjustment_number))
        loss_msg = ""
        if diff < 0:
            # نقص في الكمية (تلف/سرقة/خطأ جرد سابق) = خسارة مالية حقيقية تدخل في الربح الصافي،
            # تُحسب بسعر تكلفة المنتج الحالي (متوسط مرجّح)، وتُسجَّل تلقائيًا كمصروف "خسارة مخزون".
            product = Product.query.get(product_id)
            loss_value = round(abs(diff) * (product.purchase_price or 0), 2)
            if loss_value > 0:
                db.session.add(AccountEntry(
                    entry_type="inventory_loss", amount=loss_value, user_id=current_user.id,
                    note=f"نقص جرد: {product.name} ({abs(diff)} قطعة) - {reason} - قيد {adj.adjustment_number}",
                    reference=adj.adjustment_number, cashbox_id=branch_scope_cashbox_id(),
                ))
                loss_msg = f" وسُجِّلت خسارة مخزون بقيمة {loss_value:.2f} {app.config['CURRENCY']} تلقائيًا في المصروفات."
        log_action("inventory_adjustment", "product", product_id,
                   f"warehouse={warehouse_id}; old={old_qty}; new={new_qty}; diff={diff}; reason={reason}")
        db.session.commit()
        flash("تمت تسوية كمية المخزون وتسجيلها في سجل التدقيق." + loss_msg, "success")
        return redirect(url_for("inventory_adjust"))
    return render_template("inventory_adjustment.html", products=Product.query.filter_by(active=True).order_by(Product.name).all(), warehouses=Warehouse.query.all(),
                           adjustments=InventoryAdjustment.query.order_by(InventoryAdjustment.created_at.desc()).limit(100).all())


@app.route("/inventory/lookup")
@login_required
@permission_required("inventory_adjustment")
def inventory_lookup():
    """بحث بالباركود لشاشة التسوية: يعيد المنتج وكميته الحالية في المخزن المحدد (لا كل المخازن)."""
    code = (request.args.get("barcode") or "").strip()
    product = Product.query.filter_by(barcode=code, active=True).first()
    if not product:
        return jsonify({"error": "لم يتم العثور على منتج بهذا الباركود"}), 404
    warehouse_id = request.args.get("warehouse_id", type=int)
    stock = Stock.query.filter_by(product_id=product.id, warehouse_id=warehouse_id).first() if warehouse_id else None
    return jsonify({"id": product.id, "name": product.name, "barcode": product.barcode,
                    "quantity": (stock.quantity or 0) if stock else 0})


@app.route("/inventory/adjust-batch", methods=["POST"])
@login_required
@permission_required("inventory_adjustment")
def inventory_adjust_batch():
    """تسوية/جرد دفعة واحدة بالباركود: مخزن واحد وسبب واحد وأي عدد من الأصناف بكميتها النهائية.
    كل شيء أو لا شيء. الأصناف التي لم تتغير كميتها تُتجاهل، والنقص يُسجَّل خسارة مخزون كما في التسوية الفردية."""
    f = request.form
    warehouse_id = f.get("warehouse_id", type=int)
    reason = (f.get("reason") or "").strip()
    if not warehouse_id or not Warehouse.query.get(warehouse_id):
        flash("اختر المخزن.", "danger")
        return redirect(url_for("inventory_adjust"))
    if not reason:
        flash("سبب التسوية إلزامي.", "danger")
        return redirect(url_for("inventory_adjust"))
    lines, seen = [], set()
    for pid, qty in zip(f.getlist("product_id[]"), f.getlist("new_quantity[]")):
        if not pid or str(qty).strip() == "":
            continue
        try:
            pid, qty = int(pid), int(float(qty))
        except ValueError:
            flash("الكميات يجب أن تكون أرقامًا صحيحة.", "danger")
            return redirect(url_for("inventory_adjust"))
        if qty < 0:
            flash("الكمية النهائية لا تكون سالبة.", "danger")
            return redirect(url_for("inventory_adjust"))
        if pid in seen:
            flash("نفس المنتج مكرر في الدفعة - اجمع كمياته في سطر واحد.", "danger")
            return redirect(url_for("inventory_adjust"))
        seen.add(pid)
        lines.append((pid, qty))
    if not lines:
        flash("أضف صنفًا واحدًا على الأقل بكميته النهائية.", "warning")
        return redirect(url_for("inventory_adjust"))

    changed = unchanged = 0
    total_loss = 0.0
    for pid, new_qty in lines:
        product = Product.query.get(pid)
        if not product:
            continue
        stock = get_or_create_stock(pid, warehouse_id)
        old_qty = stock.quantity or 0
        diff = new_qty - old_qty
        if diff == 0:
            unchanged += 1
            continue
        adj = InventoryAdjustment(adjustment_number=generate_number("ADJ", InventoryAdjustment),
                                  product_id=pid, warehouse_id=warehouse_id, old_quantity=old_qty,
                                  new_quantity=new_qty, difference=diff, reason=reason, user_id=current_user.id)
        stock.quantity = new_qty
        db.session.add(adj)
        db.session.add(StockMovement(product_id=pid, warehouse_id=warehouse_id, movement_type="adjustment",
                                     quantity=diff, reference=adj.adjustment_number))
        if diff < 0:
            loss_value = round(abs(diff) * (product.purchase_price or 0), 2)
            if loss_value > 0:
                total_loss += loss_value
                db.session.add(AccountEntry(
                    entry_type="inventory_loss", amount=loss_value, user_id=current_user.id,
                    note=f"نقص جرد: {product.name} ({abs(diff)} قطعة) - {reason} - قيد {adj.adjustment_number}",
                    reference=adj.adjustment_number, cashbox_id=branch_scope_cashbox_id()))
        log_action("inventory_adjustment", "product", pid,
                   f"batch; warehouse={warehouse_id}; old={old_qty}; new={new_qty}; diff={diff}; reason={reason}")
        changed += 1
        db.session.flush()
    db.session.commit()
    msg = f"تمت تسوية {changed} صنف"
    if unchanged:
        msg += f" (وتم تجاهل {unchanged} صنف كميته لم تتغير)"
    if total_loss:
        msg += f". سُجِّلت خسارة مخزون إجمالية {total_loss:.2f} {app.config['CURRENCY']} في المصروفات."
    flash(msg, "success")
    return redirect(url_for("inventory_adjust"))


def _sync_adjustment_loss(adj, delete=False):
    """قيد "خسارة مخزون" المرتبط بالتسوية يتبعها دائمًا: يُحذف عند حذفها، ويُعاد حسابه عند تعديلها
    (كان يبقى كما هو فتبقى المصروفات منفوخة بخسارة لم تعد موجودة)."""
    AccountEntry.query.filter_by(entry_type="inventory_loss", reference=adj.adjustment_number) \
        .delete(synchronize_session=False)
    if delete or (adj.difference or 0) >= 0:
        return
    product = Product.query.get(adj.product_id)
    loss_value = round(abs(adj.difference) * ((product.purchase_price or 0) if product else 0), 2)
    if loss_value > 0:
        db.session.add(AccountEntry(
            entry_type="inventory_loss", amount=loss_value, user_id=current_user.id,
            note=f"نقص جرد: {product.name} ({abs(adj.difference)} قطعة) - {adj.reason} - قيد {adj.adjustment_number}",
            reference=adj.adjustment_number, cashbox_id=branch_scope_cashbox_id(), date=adj.created_at))


@app.route("/inventory/adjust/<int:aid>/edit", methods=["POST"])
@login_required
@permission_required("inventory_adjustment")
def edit_inventory_adjustment(aid):
    """تعديل تسوية سابقة: "الكمية الجديدة" هي ما كان يجب أن تصبح عليه الكمية وقت التسوية. يُطبَّق على المخزون
    الحالي فرق التعديل فقط (الفرق الجديد − الفرق القديم)، فلا تُمسح المبيعات أو المشتريات التي جاءت بعد التسوية."""
    adj = InventoryAdjustment.query.get_or_404(aid)
    new_qty = request.form.get("new_quantity")
    reason = (request.form.get("reason") or "").strip()
    if new_qty is None or new_qty == "" or not reason:
        flash("الكمية الجديدة والسبب إلزاميان.", "danger")
        return redirect(url_for("inventory_adjust"))
    try:
        new_qty = int(new_qty)
    except ValueError:
        flash("الكمية يجب أن تكون رقمًا صحيحًا.", "danger")
        return redirect(url_for("inventory_adjust"))
    if new_qty < 0:
        flash("الكمية لا يمكن أن تكون سالبة.", "danger")
        return redirect(url_for("inventory_adjust"))

    new_diff = new_qty - (adj.old_quantity or 0)
    delta = new_diff - (adj.difference or 0)
    stock = get_or_create_stock(adj.product_id, adj.warehouse_id)
    if (stock.quantity or 0) + delta < 0:
        flash(f"لا يمكن: التعديل يجعل المخزون الحالي سالبًا ({(stock.quantity or 0) + delta}).", "danger")
        return redirect(url_for("inventory_adjust"))
    stock.quantity = (stock.quantity or 0) + delta
    adj.new_quantity = new_qty
    adj.difference = new_diff
    adj.reason = reason

    movement = StockMovement.query.filter_by(reference=adj.adjustment_number).first()
    if movement:
        movement.quantity = new_diff
    else:
        db.session.add(StockMovement(product_id=adj.product_id, warehouse_id=adj.warehouse_id,
                                     movement_type="adjustment", quantity=new_diff,
                                     reference=adj.adjustment_number))
    _sync_adjustment_loss(adj)
    log_action("edit_inventory_adjustment", "product", adj.product_id,
               f"adjustment={adj.adjustment_number}; old={adj.old_quantity}; new={new_qty}; diff={new_diff}; "
               f"applied_delta={delta}; reason={reason}")
    db.session.commit()
    flash("تم تعديل تسوية المخزون، وطُبّق الفرق على الكمية الحالية مع تحديث قيد الخسارة.", "success")
    return redirect(url_for("inventory_adjust"))


@app.route("/inventory/adjust/<int:aid>/delete", methods=["POST"])
@login_required
@permission_required("inventory_adjustment")
def delete_inventory_adjustment(aid):
    """حذف تسوية مخزون: يعكس أثرها على المخزون الحالي ويحذف قيد الخسارة المرتبط بها."""
    adj = InventoryAdjustment.query.get_or_404(aid)
    stock = get_or_create_stock(adj.product_id, adj.warehouse_id)
    if (stock.quantity or 0) - (adj.difference or 0) < 0:
        flash(f"لا يمكن حذف التسوية: عكسها يجعل المخزون الحالي سالبًا "
              f"({(stock.quantity or 0) - (adj.difference or 0)}) لأن الكمية المضافة بيعت بعدها.", "danger")
        return redirect(url_for("inventory_adjust"))
    stock.quantity = (stock.quantity or 0) - (adj.difference or 0)
    StockMovement.query.filter_by(reference=adj.adjustment_number).delete()
    _sync_adjustment_loss(adj, delete=True)
    log_action("delete_inventory_adjustment", "product", adj.product_id,
               f"adjustment={adj.adjustment_number}; reversed_diff={adj.difference}")
    db.session.delete(adj)
    db.session.commit()
    flash("تم حذف تسوية المخزون وعكس أثرها على الكمية الحالية وحذف قيد الخسارة المرتبط.", "info")
    return redirect(url_for("inventory_adjust"))


# ---------------------------------------------------------------------------
# الموردون
# ---------------------------------------------------------------------------
def _money_by_currency(pairs):
    """يجمع مبالغ (عملة، مبلغ) كل عملة لوحدها ويعيد نصًا مثل "6,185.00 LYD + 1,200.00 USD".
    جمع دولار وجنيه ودينار في رقم واحد لا معنى له (كان إجمالي مستحقات الموردين يُحسب هكذا)."""
    totals = {}
    for cur, amount in pairs:
        cur = cur or "LYD"
        totals[cur] = totals.get(cur, 0) + (amount or 0)
    parts = [f"{v:,.2f} {c}" for c, v in sorted(totals.items(), key=lambda x: (x[0] != "LYD", x[0]))
             if abs(v) > 0.005]
    return " + ".join(parts) or "0.00"


def _supplier_debt_text():
    return _money_by_currency((s.currency_code, s.balance) for s in Supplier.query.all())


def _active_currency_codes():
    return [c.code for c in Currency.query.filter_by(is_active=True)
            .order_by(Currency.is_local.desc(), Currency.code).all()]


def _supplier_has_history(supplier):
    return bool(abs(supplier.balance or 0) > 0.005
                or Purchase.query.filter_by(supplier_id=supplier.id).first()
                or SupplierDebt.query.filter_by(supplier_id=supplier.id).first()
                or AccountEntry.query.filter_by(supplier_id=supplier.id, entry_type="supplier_payment").first())


def _convert_supplier_to_currency(supplier, new_currency, rate):
    """ينقل حساب مورد مسجَّل بالدينار إلى عملته الحقيقية بسعر واحد (كم دينار = 1 وحدة).
    الحالة: مورد مصري/تركي سُجّلت فواتيره ودفعاته بالدينار لأن المنظومة لم تكن تسمح بعملته.
    ما لا يتغير: كل ما هو بالدينار (local_total، مبالغ الدفعات الخارجة من الخزينة، تكلفة المنتجات)،
    فلا يتأثر ربح ولا خزينة ولا مخزون. ما يتغير: الأرقام بعملة المورد فقط (÷ السعر)، ويمكن بعدها
    تصحيح أي فاتورة أو دفعة بمبلغها الأجنبي الفعلي من شاشة تعديلها."""
    before = _supplier_statement_data(supplier)
    opening_old = (supplier.balance or 0) - sum(r["credit"] - r["debit"] for r in before["rows"])
    for p in Purchase.query.filter_by(supplier_id=supplier.id).all():
        if (p.currency_code or "LYD") != "LYD":
            continue
        for it in p.items:
            it.unit_cost = round((it.unit_cost or 0) / rate, 4)
            it.total = round((it.total or 0) / rate, 2)
        p.local_total = p.local_total or p.foreign_total or p.total or 0
        p.foreign_total = round(p.local_total / rate, 2)
        p.total = p.foreign_total
        p.paid_amount = round((p.paid_amount or 0) / rate, 2)
        p.currency_code, p.exchange_rate = new_currency, rate
        for pr in p.returns:
            for ri in pr.items:
                ri.unit_cost = round((ri.unit_cost or 0) / rate, 4)
                ri.total = round((ri.total or 0) / rate, 2)
            pr.local_total = pr.local_total or pr.total or 0
            pr.total = round(pr.local_total / rate, 2)
    for e in AccountEntry.query.filter_by(supplier_id=supplier.id, entry_type="supplier_payment").all():
        if (e.exchange_rate or 1) != 1:
            continue
        e.foreign_amount = round((e.amount or 0) / rate, 2)
        e.currency_code, e.exchange_rate, e.local_amount = new_currency, rate, e.amount
    for d in SupplierDebt.query.filter_by(supplier_id=supplier.id).all():
        d.amount = round((d.amount or 0) / rate, 2)
        d.paid_amount = round((d.paid_amount or 0) / rate, 2)
        d.refresh_status()
    supplier.currency_code = new_currency
    db.session.flush()
    after = _supplier_statement_data(supplier)
    # الرصيد يُعاد بناؤه من الحركات المحوَّلة نفسها حتى يطابق الكشف بالقرش (لا من قسمة الرصيد القديم)
    supplier.balance = round(opening_old / rate + sum(r["credit"] - r["debit"] for r in after["rows"]), 2)


@app.route("/suppliers")
@login_required
def suppliers():
    status_filter = request.args.get("status", "")
    query = SupplierDebt.query
    if status_filter:
        query = query.filter_by(status=status_filter)
    debts_list = query.order_by(SupplierDebt.due_date.asc().nullslast(), SupplierDebt.created_at.desc()).all()
    total_open = _money_by_currency((d.supplier.currency_code if d.supplier else "LYD", d.remaining())
                                    for d in SupplierDebt.query.all() if d.status != "paid")
    overdue = [d for d in SupplierDebt.query.all() if d.is_overdue()]
    return render_template("suppliers.html", suppliers=Supplier.query.all(),
                            cashboxes=Cashbox.query.filter_by(active=True).all(),
                            debts=debts_list, total_open=total_open, overdue_count=len(overdue),
                            total_balance=_supplier_debt_text(),
                            status_filter=status_filter, currency=app.config["CURRENCY"])


@app.route("/suppliers/add", methods=["GET", "POST"])
@login_required
@permission_required("suppliers")
def add_supplier():
    codes = _active_currency_codes()
    if request.method == "POST":
        currency = request.form.get("currency_code") or "LYD"
        db.session.add(Supplier(name=request.form.get("name"), phone=request.form.get("phone"),
                                 address=request.form.get("address"),
                                 currency_code=currency if currency in codes else "LYD"))
        db.session.commit()
        flash("تمت إضافة المورد", "success")
        return redirect(url_for("suppliers"))
    return render_template("supplier_form.html", supplier=None, currency_codes=codes, has_history=False)


@app.route("/suppliers/edit/<int:sid>", methods=["GET", "POST"])
@login_required
@permission_required("suppliers")
def edit_supplier(sid):
    supplier = Supplier.query.get_or_404(sid)
    codes = _active_currency_codes()
    old_currency = supplier.currency_code or "LYD"
    if old_currency not in codes:
        codes.append(old_currency)
    has_history = _supplier_has_history(supplier)
    if request.method == "POST":
        new_currency = request.form.get("currency_code") or "LYD"
        if new_currency not in codes:
            new_currency = old_currency
        supplier.name = request.form.get("name")
        supplier.phone = request.form.get("phone")
        supplier.address = request.form.get("address")
        converted = False
        if new_currency != old_currency:
            if not has_history:
                supplier.currency_code = new_currency
            elif old_currency != "LYD":
                db.session.rollback()
                flash(f"حساب {supplier.name} فيه حركات بـ{old_currency}، فلا تتغير عملته مباشرة. "
                      "إن كانت دفعة مسجّلة بسعر خطأ، صحّحها من صفحة الحسابات (زر التعديل) واكتب المبلغ بعملة المورد.",
                      "danger")
                return redirect(url_for("edit_supplier", sid=sid))
            else:
                try:
                    rate = float(request.form.get("convert_rate") or 0)
                except ValueError:
                    rate = 0
                if rate <= 0:
                    db.session.rollback()
                    flash(f"حساب المورد فيه حركات مسجّلة بالدينار. اكتب سعر التحويل (كم دينار = 1 {new_currency}) "
                          "لتُنقل الأرقام إلى عملته.", "danger")
                    return redirect(url_for("edit_supplier", sid=sid))
                _convert_supplier_to_currency(supplier, new_currency, rate)
                converted = True
                log_action("convert_supplier_currency", "supplier", sid,
                           f"{supplier.name}: LYD -> {new_currency} @ {rate:g} | الرصيد {supplier.balance}")
        log_action("edit_supplier", "supplier", sid, supplier.name)
        db.session.commit()
        if converted:
            flash(f"تم نقل حساب {supplier.name} إلى {new_currency}. راجع كشف الحساب، وصحّح أي فاتورة أو دفعة "
                  "بمبلغها الفعلي إن كان سعرها مختلفًا.", "success")
            return redirect(url_for("supplier_statement", sid=sid))
        flash("تم تعديل بيانات المورد", "success")
        return redirect(url_for("suppliers"))
    return render_template("supplier_form.html", supplier=supplier, currency_codes=codes, has_history=has_history)


def _parse_date_arg(raw):
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date() if raw else None
    except ValueError:
        return None


def _supplier_statement_data(supplier, date_from=None, date_to=None):
    """يجمع كل حركات المورد في كشف واحد مرتب زمنيًا مع رصيد جارٍ:
    - فواتير الشراء (دائن = مستحق للمورد بعملة الفاتورة، كما يُحسب في supplier.balance)
    - الديون القديمة المسجَّلة يدويًا (دائن)
    - كل المدفوعات/سندات الصرف المرتبطة به (مدين): دفعات من صفحة الحسابات، سداد الديون القديمة،
      ودفعة \"عند الشراء\" المرتبطة بالفاتورة.
    الرصيد الافتتاحي = رصيد المورد المسجَّل - صافي كل الحركات (غير صفر = رصيد قبل التسجيل بالنظام أو
    تعديل يدوي سابق، ويُعرض صراحةً بدل إخفائه)."""
    cashbox_names = {c.id: c.name for c in Cashbox.query.all()}
    user_names = {u.id: (u.full_name or u.username) if hasattr(u, "full_name") else u.username
                  for u in User.query.all()}
    rows = []
    linked_purchase_ids = {pid for (pid,) in db.session.query(AccountEntry.purchase_id).filter(
        AccountEntry.supplier_id == supplier.id, AccountEntry.entry_type == "supplier_payment",
        AccountEntry.purchase_id.isnot(None)).all()}
    for p in Purchase.query.filter_by(supplier_id=supplier.id).all():
        if (p.paid_amount or 0) > 0 and p.id not in linked_purchase_ids:
            # فاتورة قديمة (قبل ربط الدفعات بالحسابات): المبلغ المدفوع مسجَّل على الفاتورة نفسها بدون حركة
            # مستقلة، فنعرضه كسطر دفعة حتى يتطابق الكشف مع رصيد المورد.
            rows.append(dict(date=p.date, order=2, kind="payment", label="دفعة عند الشراء",
                             ref=p.purchase_number, desc="مدفوع وقت الشراء (مسجَّل على الفاتورة)",
                             credit=0, debit=p.paid_amount, by=user_names.get(p.user_id, "")))
        names = [(it.product.display_name() if it.product else f"#{it.product_id}") + f" ×{it.quantity}" for it in p.items]
        desc = "، ".join(names[:6]) + (f" … (+{len(names) - 6})" if len(names) > 6 else "")
        cur = p.currency_code or "LYD"
        if cur != "LYD":
            desc += f" — بسعر صرف {p.exchange_rate:g} ({cur})"
        rows.append(dict(date=p.date, order=1, kind="purchase", label="فاتورة شراء",
                         ref=p.purchase_number, desc=desc, credit=p.foreign_total or p.total or 0, debit=0,
                         local=p.local_total or 0, rate=p.exchange_rate or 1,
                         by=user_names.get(p.user_id, "")))
    for pr in PurchaseReturn.query.filter_by(supplier_id=supplier.id).all():
        names = [(ri.product.display_name() if ri.product else f"#{ri.product_id}") + f" ×{ri.quantity}" for ri in pr.items]
        rate = (pr.local_total / pr.total) if pr.total else 1
        rows.append(dict(date=pr.date, order=2, kind="return", label="إرجاع بضاعة للمورد", ref=pr.return_number,
                         desc="، ".join(names[:6]) + (f" — {pr.reason}" if pr.reason else ""),
                         credit=0, debit=pr.total or 0, local=pr.local_total or 0, rate=rate,
                         by=user_names.get(pr.user_id, "")))
    for d in SupplierDebt.query.filter_by(supplier_id=supplier.id).all():
        extra = f" — استحقاق {d.due_date}" if d.due_date else ""
        rows.append(dict(date=d.created_at, order=1, kind="debt", label="دين قديم", ref=f"دين #{d.id}",
                         desc=(d.note or "") + extra, credit=d.amount or 0, debit=0,
                         by=user_names.get(d.created_by, "")))
    for e in AccountEntry.query.filter_by(supplier_id=supplier.id, entry_type="supplier_payment").all():
        # دفعة مرتبطة بفاتورة شراء تُسجَّل بالدينار؛ نعيدها لعملة الفاتورة كي تتطابق مع رصيد المورد.
        debit = round(e.supplier_amount(), 2)
        method = {"cash": "نقدًا", "bank": "بنكي"}.get(e.payment_method, "")
        box = cashbox_names.get(e.cashbox_id, "")
        bits = [b for b in (method, f"خزينة {box}" if box else "", e.note or "") if b]
        rows.append(dict(date=e.date, order=2, kind="payment",
                         label="دفعة عند الشراء" if e.purchase_id else "سند صرف",
                         ref=e.reference or f"حركة #{e.id}", desc=" — ".join(bits), credit=0, debit=debit,
                         local=e.amount or 0, rate=(round(e.amount / debit, 4) if debit else (e.exchange_rate or 1)),
                         by=user_names.get(e.user_id, "")))
    rows.sort(key=lambda r: (r["date"] or datetime.min, r["order"]))

    net_all = sum(r["credit"] - r["debit"] for r in rows)
    opening_all = round((supplier.balance or 0) - net_all, 2)

    def _day(r):
        return r["date"].date() if r["date"] else None

    before = [r for r in rows if date_from and _day(r) and _day(r) < date_from]
    shown = [r for r in rows if not (date_from and _day(r) and _day(r) < date_from)
             and not (date_to and _day(r) and _day(r) > date_to)]
    opening = round(opening_all + sum(r["credit"] - r["debit"] for r in before), 2)
    running = opening
    for r in shown:
        running = round(running + r["credit"] - r["debit"], 2)
        r["balance"] = running
    # بالدينار: كم كلّفتنا مشتريات الفترة وكم خرج من خزائننا فعلًا، ومتوسط السعر الذي دفعنا به
    paid_local = round(sum(r.get("local", 0) for r in shown if r["kind"] == "payment"), 2)
    paid_foreign = sum(r["debit"] for r in shown if r["kind"] == "payment")
    return dict(rows=shown, opening=opening, closing=running,
                purchases_local=round(sum(r.get("local", 0) for r in shown if r["kind"] == "purchase"), 2),
                paid_local=paid_local,
                avg_paid_rate=round(paid_local / paid_foreign, 4) if paid_foreign else None,
                total_credit=round(sum(r["credit"] for r in shown), 2),
                total_debit=round(sum(r["debit"] for r in shown), 2),
                unexplained_opening=abs(opening_all) > 0.01)


@app.route("/suppliers/<int:sid>/statement")
@login_required
@permission_required("suppliers")
def supplier_statement(sid):
    """كشف حساب مورد: كل فواتير الشراء والديون القديمة وسندات الصرف/الدفعات برصيد جارٍ."""
    supplier = Supplier.query.get_or_404(sid)
    date_from = _parse_date_arg(request.args.get("from"))
    date_to = _parse_date_arg(request.args.get("to"))
    data = _supplier_statement_data(supplier, date_from, date_to)
    return render_template("supplier_statement.html", supplier=supplier, date_from=date_from,
                           date_to=date_to, printed_at=datetime.now(), **data)


@app.route("/suppliers/delete/<int:sid>")
@login_required
@permission_required("suppliers")
def delete_supplier(sid):
    supplier = Supplier.query.get_or_404(sid)
    if supplier.balance:
        flash("لا يمكن حذف مورد عليه رصيد مستحق. قم بتسوية الرصيد أولاً.", "danger")
        return redirect(url_for("suppliers"))
    log_action("delete_supplier", "supplier", sid, supplier.name)
    db.session.delete(supplier)
    db.session.commit()
    flash("تم حذف المورد", "info")
    return redirect(url_for("suppliers"))


# ---------------------------------------------------------------------------
# ديون الموردين القديمة (بدون فاتورة مشتريات)
# تسجيل دين قديم مستحق لمورد مباشرة، مع تاريخ استحقاق ومتابعة حالة السداد،
# دون الحاجة لإنشاء فاتورة مشتريات فعلية.
# تم دمج هذه الإدارة داخل صفحة الموردين (/suppliers) مباشرة.
# ---------------------------------------------------------------------------
@app.route("/suppliers/debts/add", methods=["POST"])
@login_required
@permission_required("suppliers")
def add_supplier_debt():
    supplier_id = request.form.get("supplier_id")
    amount = float(request.form.get("amount") or 0)
    supplier = Supplier.query.get(supplier_id) if supplier_id else None
    if not supplier or amount <= 0:
        flash("اختر موردًا صحيحًا وأدخل قيمة دين أكبر من صفر.", "danger")
        return redirect(url_for("suppliers"))

    due_date_raw = request.form.get("due_date")
    due_date = None
    if due_date_raw:
        try:
            due_date = datetime.strptime(due_date_raw, "%Y-%m-%d").date()
        except ValueError:
            due_date = None

    debt = SupplierDebt(supplier_id=supplier.id, amount=amount, due_date=due_date,
                        note=request.form.get("note"), created_by=current_user.id, status="unpaid")
    db.session.add(debt)
    supplier.balance += amount  # موجب = مستحق للمورد (نفس منطق فواتير المشتريات)
    log_action("add_supplier_debt", "supplier_debt", details=f"{supplier.name}: {amount}")
    db.session.commit()
    flash("تم تسجيل الدين القديم على المورد بنجاح.", "success")
    return redirect(url_for("suppliers"))


@app.route("/suppliers/debts/<int:did>/pay", methods=["POST"])
@login_required
@permission_required("suppliers")
def pay_supplier_debt(did):
    debt = SupplierDebt.query.get_or_404(did)
    amount = float(request.form.get("amount") or 0)
    if amount <= 0:
        flash("مبلغ السداد يجب أن يكون أكبر من صفر.", "danger")
        return redirect(url_for("suppliers"))
    if amount > debt.remaining() + 0.0001:
        flash(f"المبلغ المدخل ({amount:.2f}) أكبر من المتبقي على هذا الدين ({debt.remaining():.2f}).", "danger")
        return redirect(url_for("suppliers"))

    payment_method = request.form.get("payment_method")
    if payment_method not in ("cash", "bank"):
        payment_method = None
    cashbox_id, box_error = _resolve_form_cashbox(request.form)
    if box_error or (payment_method == "cash" and not cashbox_id):
        flash(box_error or "اختر الخزينة التي سيخرج منها المبلغ النقدي.", "danger")
        return redirect(url_for("suppliers"))

    # المبلغ المُدخل بعملة المورد (مثل الدين نفسه)، وما يخرج من الخزينة = المبلغ × سعر الصرف بالدينار
    currency = debt.supplier.currency_code or "LYD"
    rate = 1.0
    if currency != "LYD":
        try:
            rate = float(request.form.get("exchange_rate") or 0)
        except ValueError:
            rate = 0
        if rate <= 0:
            flash(f"أدخل سعر الصرف (كم دينار = 1 {currency}).", "danger")
            return redirect(url_for("suppliers"))
    debt.paid_amount += amount
    debt.refresh_status()
    debt.supplier.balance -= amount
    pay_entry = AccountEntry(entry_type="supplier_payment", amount=round(amount * rate, 2),
                             currency_code=currency, exchange_rate=rate, local_amount=round(amount * rate, 2),
                             foreign_amount=amount,
                             note=f"سداد دين قديم #{debt.id}" + (f" - {request.form.get('note')}" if request.form.get("note") else ""),
                             supplier_id=debt.supplier_id, user_id=current_user.id,
                             payment_method=payment_method, cashbox_id=cashbox_id, scope="general")
    db.session.add(pay_entry)
    _apply_cashbox_cash_effect(pay_entry, sign=1)
    log_action("pay_supplier_debt", "supplier_debt", debt.id, f"سداد {amount}")
    db.session.commit()
    flash("تم تسجيل السداد بنجاح.", "success")
    return redirect(url_for("suppliers"))


@app.route("/suppliers/debts/<int:did>/delete", methods=["POST"])
@login_required
@permission_required("suppliers")
def delete_supplier_debt(did):
    debt = SupplierDebt.query.get_or_404(did)
    debt.supplier.balance -= debt.remaining()  # التراجع عن الأثر المتبقي فقط على رصيد المورد
    log_action("delete_supplier_debt", "supplier_debt", debt.id, f"{debt.supplier.name}: {debt.amount}")
    db.session.delete(debt)
    db.session.commit()
    flash("تم حذف سجل الدين.", "success")
    return redirect(url_for("suppliers"))


# ---------------------------------------------------------------------------
# الموظفون وسلفهم (مبالغ يأخذها الموظف على حسابه قبل قبض راتبه)
# ---------------------------------------------------------------------------
@app.route("/finance")
@login_required
def finance_hub():
    """لوحة مالية موحّدة: تجمع روابط وإحصائيات سريعة من كل صفحات المال المتفرّقة (الحسابات،
    الموظفون وسلفهم، سلف الأشخاص الخارجيين، الخزائن، تسوية المخزون، التقرير المالي) في مكان
    واحد، بدل تنقّل المستخدم بينها يدويًا من القائمة الجانبية. كل بطاقة تظهر فقط لمن يملك
    صلاحيتها، وتُخفى تمامًا إن لم تكن هناك أي صلاحية مالية أصلًا."""
    cards = []
    if current_user.has_permission("accounts"):
        entries = AccountEntry.query.all()
        income = sum(e.amount for e in entries if e.entry_type in ("income", "customer_payment"))
        expense = sum(e.amount for e in entries if e.entry_type in ("expense", "salary_payment", "inventory_loss"))
        cards.append({"title": "الحسابات", "icon": "bi-cash-coin", "url": url_for("accounts"),
                       "stat": f"إيرادات {income:.2f} / مصروفات {expense:.2f} {app.config['CURRENCY']}"})
    if current_user.has_permission("employees"):
        open_advances = sum(e.balance or 0 for e in Employee.query.filter(Employee.balance > 0).all())
        cards.append({"title": "الموظفون وسلفهم", "icon": "bi-people", "url": url_for("employees"),
                       "stat": f"سلف قائمة: {open_advances:.2f} {app.config['CURRENCY']}"})
    if current_user.has_permission("external_borrowers"):
        ext_all = ExternalBorrower.query.all()
        owed_to_us = sum(b.balance for b in ext_all if (b.balance or 0) > 0)
        we_owe = sum(-b.balance for b in ext_all if (b.balance or 0) < 0)
        cards.append({"title": "سلف وقروض - أشخاص وجهات خارجية", "icon": "bi-person-lines-fill",
                       "url": url_for("external_borrowers"),
                       "stat": f"لنا {owed_to_us:.2f} / علينا {we_owe:.2f} {app.config['CURRENCY']}"})
    if current_user.has_permission("cashboxes"):
        total_cash = sum(b.balance or 0 for b in Cashbox.query.all())
        cards.append({"title": "الخزائن", "icon": "bi-safe2", "url": url_for("cashboxes"),
                       "stat": f"إجمالي أرصدة الصناديق: {total_cash:.2f} {app.config['CURRENCY']}"})
        cards.append({"title": "البنوك وخزائن العملات", "icon": "bi-bank", "url": url_for("fund_accounts"),
                       "stat": _money_by_currency((f.currency_code, _fund_balance(f))
                                                  for f in FundAccount.query.filter_by(active=True).all())})
    if current_user.has_permission("financial_report"):
        cards.append({"title": "المركز المالي وفروقات الصرف", "icon": "bi-clipboard-data",
                       "url": url_for("financial_position"), "stat": "ما نملك وما علينا الآن"})
    if current_user.is_manager_or_admin():
        cards.append({"title": "الموردون وديونهم", "icon": "bi-truck", "url": url_for("suppliers"),
                       "stat": f"مستحق للموردين: {_supplier_debt_text()}"})
    if current_user.has_permission("financial_report"):
        cards.append({"title": "التقرير المالي (الربح الصافي)", "icon": "bi-graph-up-arrow",
                       "url": url_for("financial_report"), "stat": "نقد/بنك + الربح الصافي الحقيقي"})
        _ow = AccountEntry.query.filter_by(entry_type="owner_withdrawal").order_by(AccountEntry.date.desc()).first()
        cards.append({"title": "سحوبات المالك (متى وكم)", "icon": "bi-person-dash",
                       "url": url_for("owner_withdrawals_report"),
                       "stat": (f"آخر سحبة: {to_local(_ow.date).strftime('%Y-%m-%d %H:%M')} - {_ow.amount:.2f} {app.config['CURRENCY']}"
                                if _ow and _ow.date else "لا توجد سحبات مسجّلة")})
    cards.append({"title": "تسوية المخزون", "icon": "bi-sliders", "url": url_for("inventory_adjust"),
                  "stat": "تسجيل خسائر المخزون تلقائيًا كمصروف"})
    return render_template("finance_hub.html", cards=cards)


@app.route("/employees")
@login_required
@permission_required("employees")
def employees():
    entries = AccountEntry.query.filter(
        AccountEntry.entry_type.in_(("employee_advance", "employee_repayment", "salary_payment"))
    ).order_by(AccountEntry.date.desc()).all()
    total_outstanding = sum(e.balance for e in Employee.query.all())
    return render_template("employees.html", employees=Employee.query.order_by(Employee.name).all(),
                            entries=entries, total_outstanding=total_outstanding,
                            cashboxes=Cashbox.query.filter_by(active=True).all(),
                            currency=app.config["CURRENCY"])


@app.route("/employees/add", methods=["POST"])
@login_required
@permission_required("employees")
def add_employee():
    name = request.form.get("name", "").strip()
    if not name:
        flash("اسم الموظف إلزامي.", "danger")
        return redirect(url_for("employees"))
    emp = Employee(name=name, phone=request.form.get("phone"), position=request.form.get("position"),
                    salary=float(request.form.get("salary") or 0))
    db.session.add(emp)
    log_action("add_employee", "employee", details=name)
    db.session.commit()
    flash("تمت إضافة الموظف", "success")
    return redirect(url_for("employees"))


@app.route("/employees/edit/<int:eid>", methods=["POST"])
@login_required
@permission_required("employees")
def edit_employee(eid):
    emp = Employee.query.get_or_404(eid)
    emp.name = request.form.get("name", emp.name).strip() or emp.name
    emp.phone = request.form.get("phone")
    emp.position = request.form.get("position")
    emp.salary = float(request.form.get("salary") or 0)
    log_action("edit_employee", "employee", eid, emp.name)
    db.session.commit()
    flash("تم تعديل بيانات الموظف", "success")
    return redirect(url_for("employees"))


@app.route("/employees/<int:eid>/pay_salary", methods=["POST"])
@login_required
@permission_required("accounts")
def pay_employee_salary(eid):
    """صرف راتب موظف. يُسجَّل الراتب الإجمالي بالكامل كمصروف تشغيلي حقيقي (salary_payment) يدخل
    في حساب الربح الصافي، وإن اختار المستخدم خصم جزء من السلفة القائمة، يُنشأ تلقائيًا قيد
    "تسديد سلفة موظف" (employee_repayment) منفصل بنفس المبلغ المخصوم، فيقلّ رصيد سلفة الموظف
    بمقدار الخصم، ويصبح صافي ما يخرج فعليًا من الصندوق = الراتب الإجمالي - السلفة المخصومة."""
    employee = Employee.query.get_or_404(eid)
    gross_amount = float(request.form.get("gross_amount") or 0)
    if gross_amount <= 0:
        flash("مبلغ الراتب يجب أن يكون أكبر من صفر.", "danger")
        return redirect(url_for("employees"))
    deduct_advance = float(request.form.get("deduct_advance") or 0)
    if deduct_advance < 0:
        deduct_advance = 0
    if deduct_advance > (employee.balance or 0) + 0.0001:
        flash(f"لا يمكن خصم {deduct_advance:.2f} وسلفة {employee.name} الحالية "
              f"{employee.balance:.2f} {app.config['CURRENCY']} فقط.", "danger")
        return redirect(url_for("employees"))
    if deduct_advance > gross_amount:
        flash("لا يمكن أن يكون الخصم أكبر من مبلغ الراتب نفسه.", "danger")
        return redirect(url_for("employees"))

    payment_method = request.form.get("payment_method") or None
    if payment_method not in ("cash", "bank"):
        payment_method = None
    cashbox_id, box_error = _resolve_form_cashbox(request.form)
    if box_error:
        flash(box_error, "danger")
        return redirect(url_for("employees"))
    note = request.form.get("note")

    salary_entry = AccountEntry(entry_type="salary_payment", amount=gross_amount, employee_id=employee.id,
                                 note=note, user_id=current_user.id, payment_method=payment_method,
                                 cashbox_id=cashbox_id, scope="general")
    db.session.add(salary_entry)
    _apply_cashbox_cash_effect(salary_entry, sign=1)

    if deduct_advance > 0:
        repayment_entry = AccountEntry(
            entry_type="employee_repayment", amount=deduct_advance, employee_id=employee.id,
            note=f"خصم من راتب {employee.name}" + (f" - {note}" if note else ""),
            user_id=current_user.id, payment_method=payment_method, cashbox_id=cashbox_id, scope="general",
        )
        employee.balance = (employee.balance or 0) - deduct_advance
        db.session.add(repayment_entry)
        _apply_cashbox_cash_effect(repayment_entry, sign=1)

    net_paid = round(gross_amount - deduct_advance, 2)
    log_action("pay_employee_salary", "employee", eid,
               f"راتب إجمالي: {gross_amount}, خصم سلفة: {deduct_advance}, صافي مدفوع: {net_paid}")
    db.session.commit()
    flash(f"تم صرف راتب {employee.name}: إجمالي {gross_amount:.2f} - خصم سلفة {deduct_advance:.2f} "
          f"= صافي مدفوع فعليًا {net_paid:.2f} {app.config['CURRENCY']}.", "success")
    return redirect(url_for("employees"))


@app.route("/employees/delete/<int:eid>")
@login_required
@permission_required("employees")
def delete_employee(eid):
    emp = Employee.query.get_or_404(eid)
    if emp.balance:
        flash(f"لا يمكن حذف {emp.name} لأن عليه سلفة غير مسدَّدة "
              f"({emp.balance:.2f} {app.config['CURRENCY']}). قم بتسوية السلفة أولاً.", "danger")
        return redirect(url_for("employees"))
    log_action("delete_employee", "employee", eid, emp.name)
    db.session.delete(emp)
    db.session.commit()
    flash("تم حذف الموظف", "info")
    return redirect(url_for("employees"))


# ---------------------------------------------------------------------------
# أشخاص من خارج الموظفين يأخذون سلفًا ويسددونها (قريب/صديق/شريك غير رسمي...)
# ---------------------------------------------------------------------------
@app.route("/external-borrowers")
@login_required
@permission_required("external_borrowers")
def external_borrowers():
    entries = AccountEntry.query.filter(
        AccountEntry.entry_type.in_(("external_advance", "external_repayment",
                                      "loan_received", "loan_repayment"))
    ).order_by(AccountEntry.date.desc()).all()
    all_borrowers = ExternalBorrower.query.order_by(ExternalBorrower.name).all()
    # balance > 0: الجهة مدينة للمحل (سلفة أعطاها المحل). balance < 0: المحل مدين للجهة (قرض أخذه المحل).
    total_owed_to_us = sum(b.balance for b in all_borrowers if (b.balance or 0) > 0)
    total_we_owe = sum(-b.balance for b in all_borrowers if (b.balance or 0) < 0)
    return render_template("external_borrowers.html", borrowers=all_borrowers,
                            entries=entries, total_owed_to_us=total_owed_to_us, total_we_owe=total_we_owe,
                            currency=app.config["CURRENCY"])


@app.route("/external-borrowers/add", methods=["POST"])
@login_required
@permission_required("external_borrowers")
def add_external_borrower():
    name = request.form.get("name", "").strip()
    if not name:
        flash("اسم الشخص إلزامي.", "danger")
        return redirect(url_for("external_borrowers"))
    borrower = ExternalBorrower(name=name, phone=request.form.get("phone"), note=request.form.get("note"))
    db.session.add(borrower)
    log_action("add_external_borrower", "external_borrower", details=name)
    db.session.commit()
    flash("تمت إضافة الشخص", "success")
    return redirect(url_for("external_borrowers"))


@app.route("/external-borrowers/edit/<int:bid>", methods=["POST"])
@login_required
@permission_required("external_borrowers")
def edit_external_borrower(bid):
    borrower = ExternalBorrower.query.get_or_404(bid)
    borrower.name = request.form.get("name", borrower.name).strip() or borrower.name
    borrower.phone = request.form.get("phone")
    borrower.note = request.form.get("note")
    log_action("edit_external_borrower", "external_borrower", bid, borrower.name)
    db.session.commit()
    flash("تم تعديل بيانات الشخص", "success")
    return redirect(url_for("external_borrowers"))


@app.route("/external-borrowers/delete/<int:bid>")
@login_required
@permission_required("external_borrowers")
def delete_external_borrower(bid):
    borrower = ExternalBorrower.query.get_or_404(bid)
    if borrower.balance:
        flash(f"لا يمكن حذف {borrower.name} لأن عليه سلفة غير مسدَّدة "
              f"({borrower.balance:.2f} {app.config['CURRENCY']}). قم بتسوية السلفة أولاً.", "danger")
        return redirect(url_for("external_borrowers"))
    log_action("delete_external_borrower", "external_borrower", bid, borrower.name)
    db.session.delete(borrower)
    db.session.commit()
    flash("تم حذف الشخص", "info")
    return redirect(url_for("external_borrowers"))


# ---------------------------------------------------------------------------
# المشتريات
# ---------------------------------------------------------------------------
@app.route("/purchases")
@login_required
@permission_required("purchases")
def purchases():
    return render_template("purchases.html", purchases=Purchase.query.order_by(Purchase.date.desc()).all())


def _reverse_purchase_effects(purchase):
    """يعكس كل أثر فاتورة شراء: المخزون (طرح الكميات المستلمة)، رصيد المورد (طرح الجزء الآجل)، دفعاتها
    (إعادة النقد للخزينة وحذف حركتها)، وحركات المخزون. لا يحذف الفاتورة نفسها ولا يعمل commit.
    يعيد True إن اضطُرّ لتصفير مخزون كان سيصير سالبًا (بعض الكميات بيعت بالفعل)."""
    stock_warning = False
    rate = purchase.exchange_rate or 1
    for item in purchase.items:
        # عكس المتوسط المرجّح لتكلفة المنتج: نُخرج كمية هذه الفاتورة بتكلفتها (بالدينار) من المتوسط،
        # وإلا يبقى أثر فاتورة محذوفة/معدّلة في تكلفة المنتج (فتتغير الأرباح وقيمة المخزون خطأً).
        product = Product.query.get(item.product_id)
        if product and item.quantity:
            total_qty = product.total_quantity() or 0
            remaining_qty = total_qty - item.quantity
            if remaining_qty > 0:
                restored = ((total_qty * (product.purchase_price or 0))
                            - (item.quantity * ((item.unit_cost or 0) * rate + (item.extra_unit_cost or 0)))) / remaining_qty
                if restored >= 0:
                    product.purchase_price = round(restored, 4)
        stock = get_or_create_stock(item.product_id, purchase.warehouse_id)
        stock.quantity = (stock.quantity or 0) - item.quantity
        if stock.quantity < 0:
            stock.quantity = 0
            stock_warning = True
    if purchase.supplier_id:
        supplier = Supplier.query.get(purchase.supplier_id)
        if supplier:
            supplier.balance -= max(0, purchase.foreign_total - purchase.paid_amount)
    for pay in AccountEntry.query.filter_by(purchase_id=purchase.id).all():
        _apply_cashbox_cash_effect(pay, sign=-1)  # إعادة المبلغ النقدي المدفوع إلى الخزينة
        db.session.delete(pay)
    StockMovement.query.filter_by(reference=purchase.purchase_number).delete()
    return stock_warning


def _fill_purchase_from_form(purchase, form):
    """يطبّق بيانات نموذج الشراء على purchase (جديدة، أو قديمة بعد عكس أثرها): المورد والمخزن والعملة
    والأصناف والمخزون وتكلفة المنتج (متوسط مرجّح) ورصيد المورد والدفعة. يعيد نص الخطأ أو None.
    على المستدعي عمل db.session.rollback() عند الخطأ، وcommit عند النجاح."""
    currency_code = form.get("currency_code") or "LYD"
    try:
        exchange_rate = float(form.get("exchange_rate") or 1)
    except ValueError:
        exchange_rate = 1
    if currency_code == "LYD":
        exchange_rate = 1
    if exchange_rate <= 0:
        return "سعر الصرف يجب أن يكون أكبر من صفر."
    try:
        purchase.warehouse_id = int(form.get("warehouse_id"))
    except (TypeError, ValueError):
        return "اختر المخزن المستقبل للبضاعة."
    purchase.supplier_id = int(form.get("supplier_id")) if form.get("supplier_id") else None
    if purchase.supplier_id:
        # رصيد المورد رقم واحد بعملته، ففاتورة بعملة أخرى تُجمع عليه كأنها نفس العملة (جنيه فوق دينار).
        sup = Supplier.query.get(purchase.supplier_id)
        sup_cur = (sup.currency_code or "LYD") if sup else currency_code
        if sup and sup_cur != currency_code:
            return (f"المورد {sup.name} حسابه بـ{sup_cur} والفاتورة بـ{currency_code}. سجّل الفاتورة بعملة المورد، "
                    "أو غيّر عملة المورد أولًا من صفحة الموردين.")
    purchase.currency_code = currency_code
    purchase.exchange_rate = exchange_rate
    if purchase.date is None:
        purchase.date = datetime.utcnow()

    total = 0.0
    parsed = []
    for pid, qty, cost in zip(form.getlist("product_id[]"), form.getlist("quantity[]"),
                              form.getlist("unit_cost[]")):
        if not pid or not qty:
            continue
        try:
            pid, qty, cost = int(pid), int(qty), float(cost or 0)
        except ValueError:
            return "الكمية وسعر الوحدة يجب أن يكونا أرقامًا صحيحة."
        if qty <= 0 or cost < 0:
            return "الكمية يجب أن تكون أكبر من صفر وسعر الوحدة لا يكون سالبًا."
        parsed.append((pid, qty, cost))
        total += qty * cost

    # مصاريف إضافية بالدينار (شحن/جمارك/عمولة تحويل) كما في "التكاليف المحمَّلة" بـ Odoo: تُوزَّع على
    # الأصناف بنسبة قيمتها وتدخل في تكلفة القطعة، فيظهر الربح الحقيقي عند البيع. لا تدخل حساب المورد.
    try:
        extra_cost = round(float(form.get("extra_cost") or 0), 2)
    except ValueError:
        return "المصاريف الإضافية يجب أن تكون رقمًا."
    if extra_cost < 0:
        return "المصاريف الإضافية لا تكون سالبة."
    total_qty_lines = sum(q for _, q, _ in parsed)
    purchase.extra_cost = extra_cost
    purchase.extra_cost_note = (form.get("extra_cost_note") or "").strip()[:200] or None

    lines = 0
    for pid, qty, cost in parsed:
        lines += 1
        line_total = qty * cost
        share = extra_cost * ((line_total / total) if total > 0 else (qty / total_qty_lines))
        extra_unit = round(share / qty, 4)
        db.session.add(PurchaseItem(purchase_id=purchase.id, product_id=pid, quantity=qty, unit_cost=cost,
                                     total=line_total, extra_unit_cost=extra_unit))
        stock = get_or_create_stock(pid, purchase.warehouse_id)
        # تحديث تكلفة المنتج (Product.purchase_price) تلقائيًا بمتوسط مرجّح (Weighted Average
        # Cost) بدل تركه على آخر سعر أُدخل يدويًا - هذا السعر هو نفسه المستخدم لاحقًا في حساب
        # تكلفة البضاعة المباعة (COGS) والربح الصافي وقيمة المخزون، فلازم يعكس تكلفة الشراء
        # الفعلية بدل ما يضل ثابتًا وقديمًا.
        # مهم: تكلفة المنتج تُحفظ دائمًا بالدينار (تُستخدم في الربح وقيمة المخزون)، أما سعر الوحدة في
        # الفاتورة فهو بعملة الشراء - لذلك يُحوَّل بسعر صرف الفاتورة قبل دخوله في المتوسط المرجّح.
        product = Product.query.get(pid)
        cost_local = cost * exchange_rate + extra_unit
        if product and cost_local > 0:
            old_qty_total = product.total_quantity() or 0  # عبر كل المخازن، قبل إضافة هذه الكمية
            old_cost = product.purchase_price or 0
            total_qty_after = old_qty_total + qty
            if total_qty_after > 0:
                product.purchase_price = round(((old_qty_total * old_cost) + (qty * cost_local)) / total_qty_after, 4)
        stock.quantity += qty
        db.session.add(StockMovement(product_id=pid, warehouse_id=purchase.warehouse_id,
                                      movement_type="in", quantity=qty,
                                      reference=purchase.purchase_number))
    if lines == 0:
        return "أضف صنفًا واحدًا على الأقل في الفاتورة."

    purchase.foreign_total = round(total, 2)
    purchase.local_total = round(total * exchange_rate, 2)
    purchase.total = purchase.foreign_total
    try:
        paid = float(form.get("paid_amount") or 0)
    except ValueError:
        return "المبلغ المدفوع يجب أن يكون رقمًا."
    if paid < 0 or paid > purchase.foreign_total + 0.0001:
        return "المبلغ المدفوع يجب أن يكون بين صفر وإجمالي الفاتورة."
    pay_method = form.get("payment_method")
    if pay_method not in ("cash", "bank"):
        pay_method = None
    pay_cashbox, box_error = _resolve_form_cashbox(form)
    if paid > 0 and (box_error or (pay_method == "cash" and not pay_cashbox)):
        return box_error or "اختر الخزينة التي دُفع منها المبلغ النقدي."
    purchase.paid_amount = paid
    if purchase.supplier_id:
        supplier = Supplier.query.get(purchase.supplier_id)
        if supplier:
            supplier.balance += max(0, purchase.foreign_total - paid)
    if paid > 0:
        # الدفعة تُسجَّل كحركة "دفع لمورد" مرتبطة بالفاتورة (بالدينار حسب سعر الصرف)، فتنزل من
        # الخزينة إن كانت نقدًا وتظهر في الحسابات وكشف حساب المورد. لا تمسّ رصيد المورد (الآجل فقط
        # أُضيف أعلاه).
        local_paid = round(paid * exchange_rate, 2)
        pay_entry = AccountEntry(entry_type="supplier_payment", amount=local_paid,
                                 supplier_id=purchase.supplier_id, user_id=current_user.id,
                                 note=f"دفعة عند الشراء {purchase.purchase_number}",
                                 reference=purchase.purchase_number, purchase_id=purchase.id,
                                 payment_method=pay_method, cashbox_id=pay_cashbox, scope="general",
                                 currency_code=currency_code, exchange_rate=exchange_rate,
                                 local_amount=local_paid, foreign_amount=paid, date=purchase.date)
        db.session.add(pay_entry)
        _apply_cashbox_cash_effect(pay_entry, sign=1)
    if extra_cost > 0:
        # المصاريف تُدفع لطرف آخر (شركة الشحن/الجمارك)، وقد تختلف طريقة دفعها عن دفعة المورد
        extra_method = form.get("extra_payment_method")
        if extra_method not in ("cash", "bank"):
            extra_method = None
        extra_box = branch_scope_cashbox_id()
        if not extra_box and form.get("extra_cashbox_id"):
            box = Cashbox.query.get(form.get("extra_cashbox_id"))
            if not box:
                return "اختر خزينة صحيحة للمصاريف الإضافية."
            extra_box = box.id
        if extra_method == "cash" and not extra_box:
            return "اختر الخزينة التي دُفعت منها المصاريف الإضافية نقدًا."
        cost_entry = AccountEntry(entry_type="purchase_cost", amount=extra_cost, user_id=current_user.id,
                                  note=f"مصاريف شراء {purchase.purchase_number}"
                                       + (f" - {purchase.extra_cost_note}" if purchase.extra_cost_note else ""),
                                  reference=purchase.purchase_number, purchase_id=purchase.id,
                                  payment_method=extra_method, cashbox_id=extra_box, scope="general",
                                  local_amount=extra_cost, date=purchase.date)
        db.session.add(cost_entry)
        _apply_cashbox_cash_effect(cost_entry, sign=1)
    return None


def _purchase_form_context(purchase=None):
    pay_entry = (AccountEntry.query.filter_by(purchase_id=purchase.id, entry_type="supplier_payment").first()
                 if purchase else None)
    extra_entry = (AccountEntry.query.filter_by(purchase_id=purchase.id, entry_type="purchase_cost").first()
                   if purchase else None)
    # آخر سعر صرف استُخدم لكل عملة أجنبية، يُقترح تلقائيًا عند اختيار العملة (ويمكن تعديله يدويًا).
    last_rates = {}
    for p in Purchase.query.order_by(Purchase.date.desc()).limit(300).all():
        if p.currency_code and p.currency_code != "LYD" and p.exchange_rate and p.currency_code not in last_rates:
            last_rates[p.currency_code] = p.exchange_rate
    supplier_balances = {}
    for sup in Supplier.query.all():
        base = sup.balance or 0
        if purchase and purchase.supplier_id == sup.id:
            # عند التعديل: رصيد المورد يتضمن الجزء الآجل من هذه الفاتورة نفسها، نطرحه لنحسب الرصيد "قبلها"
            base -= max(0, (purchase.foreign_total or 0) - (purchase.paid_amount or 0))
        supplier_balances[sup.id] = round(base, 2)
    return dict(purchase=purchase, pay_entry=pay_entry, extra_entry=extra_entry, last_rates=last_rates, supplier_balances=supplier_balances, suppliers=Supplier.query.all(),
                cashboxes=Cashbox.query.filter_by(active=True).all(),
                warehouses=Warehouse.query.all(),
                products=[p for p in Product.query.order_by(Product.name).all()
                          if p.active or (purchase and any(it.product_id == p.id for it in purchase.items))],
                categories=Category.query.all(),
                barcode_map={p.barcode: {"id": p.id, "name": p.name, "cost": p.purchase_price or 0}
                             for p in Product.query.filter_by(active=True).all() if p.barcode},
                can_add_product=bool(current_user.is_manager_or_admin() or current_user.has_permission("products")),
                currencies=Currency.query.filter_by(is_active=True).all())


@app.route("/currencies", methods=["GET", "POST"])
@login_required
@permission_required("purchases")
def currencies_page():
    """إدارة العملات المتاحة في فواتير الشراء (إضافة عملة جديدة، تفعيل/إيقاف). سعر الصرف لا يُخزَّن هنا
    بل يُدخل ويُثبَّت مع كل فاتورة شراء."""
    if not current_user.is_manager_or_admin():
        flash("لا تملك صلاحية إدارة العملات.", "danger")
        return redirect(url_for("purchases"))
    if request.method == "POST":
        if request.form.get("action") == "toggle":
            cur = Currency.query.get_or_404(request.form.get("currency_id", type=int))
            if cur.is_local:
                flash("لا يمكن إيقاف العملة المحلية.", "danger")
            else:
                cur.is_active = not cur.is_active
                db.session.commit()
                flash(f"تم {'تفعيل' if cur.is_active else 'إيقاف'} العملة {cur.code}.", "success")
        else:
            code = (request.form.get("code") or "").strip().upper()
            name = (request.form.get("name") or "").strip()
            symbol = (request.form.get("symbol") or "").strip() or None
            if not (2 <= len(code) <= 6 and code.isalnum() and code.isascii()):
                flash("رمز العملة حروف/أرقام إنجليزية من 2 إلى 6 (مثل EGP أو CNY).", "danger")
            elif not name:
                flash("اكتب اسم العملة.", "danger")
            else:
                existing = Currency.query.filter_by(code=code).first()
                if existing:
                    existing.is_active = True
                    flash(f"العملة {code} موجودة مسبقًا وتم تفعيلها.", "info")
                else:
                    db.session.add(Currency(code=code, name=name, symbol=symbol, is_local=False))
                    flash(f"تمت إضافة العملة {code}.", "success")
                db.session.commit()
        return redirect(url_for("currencies_page"))
    return render_template("currencies.html", currencies=Currency.query.order_by(Currency.is_local.desc(), Currency.code).all())


@app.route("/purchases/add", methods=["GET", "POST"])
@login_required
@permission_required("purchases")
def add_purchase():
    if request.method == "POST":
        try:
            warehouse_id = int(request.form.get("warehouse_id"))
        except (TypeError, ValueError):
            flash("اختر المخزن المستقبل للبضاعة.", "danger")
            return redirect(request.url)
        purchase = Purchase(purchase_number=generate_number("PUR", Purchase), user_id=current_user.id,
                            warehouse_id=warehouse_id, date=datetime.utcnow())
        db.session.add(purchase)
        db.session.flush()
        error = _fill_purchase_from_form(purchase, request.form)
        if error:
            db.session.rollback()
            flash(error, "danger")
            return redirect(request.url)
        log_action("add_purchase", "purchase", purchase.id,
                   f"{purchase.purchase_number} | {purchase.foreign_total} {purchase.currency_code} | rate={purchase.exchange_rate}")
        db.session.commit()
        flash("تم حفظ فاتورة الشراء مع تثبيت العملة وسعر الصرف.", "success")
        return redirect(url_for("purchases"))
    return render_template("purchase_form.html", **_purchase_form_context())


@app.route("/purchases/<int:pid>/edit", methods=["GET", "POST"])
@login_required
@permission_required("purchases")
def edit_purchase(pid):
    """تعديل فاتورة شراء (للمدير/الأدمن): يعكس أثر الفاتورة القديمة بالكامل (مخزون، رصيد مورد، دفعة،
    خزينة) ثم يطبّق النموذج الجديد على نفس الفاتورة (نفس الرقم والتاريخ). أي خطأ = لا يتغيّر شيء."""
    if not current_user.is_manager_or_admin():
        flash("لا تملك صلاحية تعديل فواتير الشراء.", "danger")
        return redirect(url_for("purchases"))
    purchase = Purchase.query.get_or_404(pid)
    if purchase.returns:
        flash("هذه الفاتورة عليها إرجاع للمورد فلا تُعدَّل. احذف الإرجاع أولًا إن لزم.", "warning")
        return redirect(url_for("purchases"))
    if request.method == "POST":
        before = (f"{purchase.purchase_number} | {purchase.foreign_total} {purchase.currency_code}"
                  f" | paid={purchase.paid_amount} | items=" +
                  ", ".join(f"{it.product.name if it.product else it.product_id}x{it.quantity}@{it.unit_cost}"
                            for it in purchase.items))
        stock_warning = _reverse_purchase_effects(purchase)
        purchase.items = []
        db.session.flush()
        error = _fill_purchase_from_form(purchase, request.form)
        if error:
            db.session.rollback()
            flash(error, "danger")
            return redirect(request.url)
        log_action("edit_purchase", "purchase", purchase.id,
                   f"قبل: {before} || بعد: {purchase.foreign_total} {purchase.currency_code} | paid={purchase.paid_amount}")
        db.session.commit()
        if stock_warning:
            flash("تم تعديل الفاتورة، لكن بعض الكميات القديمة كانت قد بيعت فتم تصفير الرصيد المتأثر قبل "
                  "إعادة التطبيق - راجع كميات المخزون.", "warning")
        else:
            flash("تم تعديل فاتورة الشراء وإعادة احتساب المخزون ورصيد المورد والخزينة.", "success")
        return redirect(url_for("purchases"))
    return render_template("purchase_form.html", **_purchase_form_context(purchase))


@app.route("/purchases/<int:pid>/delete")
@login_required
@permission_required("purchases")
def delete_purchase(pid):
    """حذف فاتورة شراء نهائيًا: يعكس أثرها على المخزون (طرح الكميات المستلمة)
    وعلى رصيد المورد (طرح المبلغ الآجل الذي أُضيف عليه عند إنشائها)."""
    if not current_user.is_manager_or_admin():
        flash("لا تملك صلاحية حذف فواتير الشراء.", "danger")
        return redirect(url_for("purchases"))

    purchase = Purchase.query.get_or_404(pid)
    if purchase.returns:
        flash("هذه الفاتورة عليها إرجاع للمورد فلا تُحذف. احذف الإرجاع أولًا إن لزم.", "warning")
        return redirect(url_for("purchases"))
    snapshot = {
        "purchase_number": purchase.purchase_number,
        "supplier": purchase.supplier.name if purchase.supplier else None,
        "date": purchase.date.isoformat() if purchase.date else None,
        "foreign_total": purchase.foreign_total, "paid_amount": purchase.paid_amount,
        "currency_code": purchase.currency_code, "exchange_rate": purchase.exchange_rate,
        "items": [{"product": it.product.name if it.product else it.product_id,
                   "quantity": it.quantity, "unit_cost": it.unit_cost} for it in purchase.items],
    }

    stock_warning = _reverse_purchase_effects(purchase)

    log_action("delete_purchase", "purchase", pid, json.dumps(snapshot, ensure_ascii=False, default=str))
    db.session.delete(purchase)  # عناصر الفاتورة تُحذف تلقائيًا (cascade)
    db.session.commit()

    if stock_warning:
        flash(f"تم حذف فاتورة الشراء {snapshot['purchase_number']}، لكن بعض الكميات كانت قد بيعت "
              "بالفعل فتم تصفير الرصيد المتأثر بدل جعله سالبًا — يُرجى مراجعة كميات المخزون يدويًا.", "warning")
    else:
        flash(f"تم حذف فاتورة الشراء {snapshot['purchase_number']} وعكس أثرها على المخزون ورصيد المورد.", "info")
    return redirect(url_for("purchases"))


# ---------------------------------------------------------------------------
# إرجاع بضاعة للمورد (مرتجع مشتريات)
# ---------------------------------------------------------------------------
def _shift_product_cost(product, qty_delta, unit_local):
    """يُدخل (موجب) أو يُخرج (سالب) كمية بتكلفة محددة من المتوسط المرجّح لتكلفة المنتج.
    يُستدعى قبل تغيير كمية المخزون نفسها."""
    total_qty = product.total_quantity() or 0
    new_qty = total_qty + qty_delta
    if new_qty > 0:
        value = total_qty * (product.purchase_price or 0) + qty_delta * unit_local
        if value >= 0:
            product.purchase_price = round(value / new_qty, 4)


@app.route("/purchases/<int:pid>/return", methods=["GET", "POST"])
@login_required
@permission_required("purchases")
def purchase_return(pid):
    """إرجاع أصناف من فاتورة شراء للمورد: تخرج من مخزن الفاتورة بثمن شرائها، ويُخصم ثمنها من حساب المورد
    بعملته. مصاريف الشحن التي دُفعت عليها لا تُسترد، فتبقى في تكلفة الكمية الباقية."""
    if not current_user.is_manager_or_admin():
        flash("إرجاع المشتريات للمدير/الأدمن فقط.", "danger")
        return redirect(url_for("purchases"))
    purchase = Purchase.query.get_or_404(pid)
    rate = purchase.exchange_rate or 1
    if request.method == "POST":
        ret = PurchaseReturn(return_number=generate_number("PRT", PurchaseReturn), purchase_id=purchase.id,
                             supplier_id=purchase.supplier_id, reason=(request.form.get("reason") or "").strip()[:250],
                             user_id=current_user.id, date=datetime.utcnow())
        db.session.add(ret)
        db.session.flush()
        total, error = 0.0, None
        for item in purchase.items:
            try:
                qty = int(request.form.get(f"qty_{item.id}") or 0)
            except ValueError:
                qty = 0
            if qty <= 0:
                continue
            stock = get_or_create_stock(item.product_id, purchase.warehouse_id)
            name = item.product.display_name() if item.product else f"#{item.product_id}"
            if qty > item.returnable_quantity():
                error = f"الكمية المطلوب إرجاعها من \"{name}\" أكبر من المتاح للإرجاع ({item.returnable_quantity()})."
            elif qty > (stock.quantity or 0):
                error = (f"الموجود من \"{name}\" في مخزن {purchase.warehouse.name} هو {stock.quantity or 0} فقط "
                         "(الباقي بِيع أو نُقل).")
            if error:
                break
            if item.product:
                _shift_product_cost(item.product, -qty, (item.unit_cost or 0) * rate)
            stock.quantity = (stock.quantity or 0) - qty
            line = round(qty * (item.unit_cost or 0), 2)
            total += line
            db.session.add(PurchaseReturnItem(purchase_return_id=ret.id, purchase_item_id=item.id,
                                              product_id=item.product_id, quantity=qty,
                                              unit_cost=item.unit_cost or 0, total=line))
            db.session.add(StockMovement(product_id=item.product_id, warehouse_id=purchase.warehouse_id,
                                          movement_type="out", quantity=qty, reference=ret.return_number))
        if not error and total <= 0 and not ret.items:
            error = "حدّد كمية صنف واحد على الأقل للإرجاع."
        if error:
            db.session.rollback()
            flash(error, "danger")
            return redirect(request.url)
        ret.total = round(total, 2)
        ret.local_total = round(total * rate, 2)
        if purchase.supplier_id:
            supplier = Supplier.query.get(purchase.supplier_id)
            if supplier:
                supplier.balance = round((supplier.balance or 0) - ret.total, 2)
        log_action("purchase_return", "purchase_return", ret.id,
                   f"{ret.return_number} من {purchase.purchase_number}: {ret.total} {purchase.currency_code}")
        db.session.commit()
        flash(f"تم تسجيل الإرجاع {ret.return_number}: خرجت البضاعة من المخزون وخُصم "
              f"{ret.total:,.2f} {purchase.currency_code or 'LYD'} من حساب المورد.", "success")
        return redirect(url_for("purchase_return", pid=pid))
    return render_template("purchase_return.html", purchase=purchase,
                           stocks={it.id: (get_or_create_stock(it.product_id, purchase.warehouse_id).quantity or 0)
                                   for it in purchase.items})


@app.route("/purchases/returns/<int:rid>/delete", methods=["POST"])
@login_required
@permission_required("purchases")
def delete_purchase_return(rid):
    if not current_user.is_manager_or_admin():
        flash("حذف إرجاع المشتريات للمدير/الأدمن فقط.", "danger")
        return redirect(url_for("purchases"))
    ret = PurchaseReturn.query.get_or_404(rid)
    purchase = ret.purchase
    rate = purchase.exchange_rate or 1
    for ri in ret.items:
        product = Product.query.get(ri.product_id)
        if product:
            _shift_product_cost(product, ri.quantity, (ri.unit_cost or 0) * rate)
        stock = get_or_create_stock(ri.product_id, purchase.warehouse_id)
        stock.quantity = (stock.quantity or 0) + ri.quantity
    if ret.supplier_id:
        supplier = Supplier.query.get(ret.supplier_id)
        if supplier:
            supplier.balance = round((supplier.balance or 0) + (ret.total or 0), 2)
    StockMovement.query.filter_by(reference=ret.return_number).delete()
    log_action("delete_purchase_return", "purchase_return", rid, f"{ret.return_number}: {ret.total}")
    db.session.delete(ret)
    db.session.commit()
    flash("تم حذف الإرجاع: رجعت البضاعة للمخزون والمبلغ لحساب المورد.", "info")
    return redirect(url_for("purchase_return", pid=purchase.id))


# ---------------------------------------------------------------------------
# نقل البضاعة بين المخازن
# ---------------------------------------------------------------------------
@app.route("/transfers")
@login_required
@permission_required("transfers")
def transfers():
    return render_template("transfers.html",
                            transfers=StockTransfer.query.order_by(StockTransfer.date.desc()).all())


@app.route("/transfers/add", methods=["GET", "POST"])
@login_required
@permission_required("transfers")
def add_transfer():
    if request.method == "POST":
        from_wid = int(request.form.get("from_warehouse_id"))
        to_wid = int(request.form.get("to_warehouse_id"))
        if from_wid == to_wid:
            flash("لا يمكن أن يكون مخزن المصدر هو نفس مخزن الوجهة", "danger")
            return redirect(url_for("add_transfer"))

        transfer = StockTransfer(
            transfer_number=generate_number("TRF", StockTransfer),
            from_warehouse_id=from_wid, to_warehouse_id=to_wid,
            user_id=current_user.id, note=request.form.get("note"),
        )
        db.session.add(transfer)
        db.session.flush()

        moved_any = False
        for pid, qty in zip(request.form.getlist("product_id[]"), request.form.getlist("quantity[]")):
            if not pid or not qty:
                continue
            pid, qty = int(pid), int(qty)
            if qty <= 0:
                continue
            source_stock = get_or_create_stock(pid, from_wid)
            if source_stock.quantity < qty:
                flash(f"الكمية غير كافية في مخزن المصدر للمنتج المحدد", "danger")
                db.session.rollback()
                return redirect(url_for("add_transfer"))
            source_stock.quantity -= qty
            dest_stock = get_or_create_stock(pid, to_wid)
            dest_stock.quantity += qty
            db.session.add(StockTransferItem(transfer_id=transfer.id, product_id=pid, quantity=qty))
            db.session.add(StockMovement(product_id=pid, warehouse_id=from_wid, movement_type="out",
                                          quantity=qty, reference=transfer.transfer_number))
            db.session.add(StockMovement(product_id=pid, warehouse_id=to_wid, movement_type="in",
                                          quantity=qty, reference=transfer.transfer_number))
            moved_any = True

        if not moved_any:
            db.session.rollback()
            flash("الرجاء إدخال منتج وكمية واحدة على الأقل للنقل", "danger")
            return redirect(url_for("add_transfer"))

        log_action("create_transfer", "stock_transfer", transfer.id, transfer.transfer_number)
        db.session.commit()
        flash("تم نقل البضاعة بين المخازن بنجاح", "success")
        return redirect(url_for("transfers"))

    return render_template("transfer_form.html", warehouses=Warehouse.query.all(),
                            products=Product.query.filter_by(active=True).order_by(Product.name).all())


@app.route("/transfers/delete/<int:tid>")
@login_required
@permission_required("transfers")
def delete_transfer(tid):
    transfer = StockTransfer.query.get_or_404(tid)
    # لا يُحذف النقل إن كانت الكمية المنقولة قد بيعت/نُقلت من مخزن الوجهة (وإلا يصير مخزونه سالبًا)
    for item in transfer.items:
        dest = Stock.query.filter_by(product_id=item.product_id, warehouse_id=transfer.to_warehouse_id).first()
        available = dest.quantity if dest else 0
        if available < item.quantity:
            name = item.product.display_name() if getattr(item, "product", None) else f"#{item.product_id}"
            flash(f"لا يمكن حذف النقل {transfer.transfer_number}: المتبقي من \"{name}\" في مخزن الوجهة {available} "
                  f"فقط من أصل {item.quantity} منقولة (بيع جزء منها). انقل الكمية المتبقية يدويًا بدل الحذف.", "danger")
            return redirect(url_for("transfers"))
    for item in transfer.items:
        source_stock = get_or_create_stock(item.product_id, transfer.to_warehouse_id)
        source_stock.quantity -= item.quantity
        dest_stock = get_or_create_stock(item.product_id, transfer.from_warehouse_id)
        dest_stock.quantity += item.quantity
        db.session.add(StockMovement(product_id=item.product_id, warehouse_id=transfer.to_warehouse_id,
                                      movement_type="out", quantity=item.quantity,
                                      reference=f"إلغاء {transfer.transfer_number}"))
        db.session.add(StockMovement(product_id=item.product_id, warehouse_id=transfer.from_warehouse_id,
                                      movement_type="in", quantity=item.quantity,
                                      reference=f"إلغاء {transfer.transfer_number}"))
    log_action("delete_transfer", "stock_transfer", tid, transfer.transfer_number)
    db.session.delete(transfer)
    db.session.commit()
    flash("تم حذف عملية النقل وإعادة الكميات إلى مخزن المصدر", "info")
    return redirect(url_for("transfers"))


# ---------------------------------------------------------------------------
# الورديات (Shift Management)
# ---------------------------------------------------------------------------
@app.route("/shifts/open", methods=["GET", "POST"])
@login_required
def open_shift():
    if get_active_shift():
        flash("لديك وردية مفتوحة بالفعل", "warning")
        return redirect(url_for("pos"))
    if request.method == "POST":
        cashbox_id = request.form.get("cashbox_id", type=int) or current_user.cashbox_id
        if not cashbox_id or not Cashbox.query.get(cashbox_id):
            flash("يجب اختيار خزينة صحيحة قبل فتح الوردية.", "danger")
            return redirect(url_for("open_shift"))
        # تقييد الكاشير العادي على خزينته المخصصة فقط (مهم عند وجود أكثر من فرع/نقطة بيع لنفس
        # المالك، حتى لا يفتح كاشير فرع وردية بالخطأ على خزينة فرع آخر فتُربط مبيعاته بمخزن غلط).
        # المدير/الأدمن مستثنى لحاجتهم للتنقل والإشراف بين الفروع.
        if (not current_user.is_manager_or_admin() and current_user.cashbox_id
                and cashbox_id != current_user.cashbox_id):
            flash("خزينتك المخصصة مختلفة عن المختارة - لا تملك صلاحية فتح وردية على خزينة فرع آخر.", "danger")
            return redirect(url_for("open_shift"))
        try:
            opening_cash = _parse_money(request.form.get("opening_cash"))  # السالب مسموح
        except ValueError:
            flash("النقد الافتتاحي يجب أن يكون رقمًا صحيحًا.", "danger")
            return redirect(url_for("open_shift"))
        shift = Shift(user_id=current_user.id, cashbox_id=cashbox_id, opening_cash=opening_cash)
        db.session.add(shift)
        # إن اختلف العدّ الفعلي عند الفتح عن رصيد الخزينة المسجّل، يُسجَّل الفرق صراحة (لا يضيع بصمت)
        _record_cash_count_difference(Cashbox.query.get(cashbox_id), opening_cash, "عند فتح وردية")
        log_action("open_shift", "shift", details=f"نقد افتتاحي: {shift.opening_cash}")
        db.session.commit()
        flash("تم فتح الوردية بنجاح", "success")
        return redirect(url_for("pos"))
    return render_template("shift_open.html", cashboxes=Cashbox.query.filter_by(active=True).all())


def _record_cash_count_difference(box, counted, when):
    if not box:
        return
    diff = round((counted or 0) - (box.balance or 0), 2)
    if abs(diff) < 0.005:
        return
    label = "زيادة" if diff > 0 else "عجز"
    note = f"{label} صندوق {when}: المعدود {counted:.2f} والرصيد المسجّل {(box.balance or 0):.2f}"
    if diff > 0:
        _apply_cash_transfer(None, box, diff, note, prefix="CLS")
    else:
        _apply_cash_transfer(box, None, -diff, note, prefix="CLS")


@app.route("/shifts/close", methods=["GET", "POST"])
@login_required
def close_shift():
    shift = get_active_shift()
    if not shift:
        flash("لا توجد وردية مفتوحة لإغلاقها", "warning")
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        try:
            counted = _parse_money(request.form.get("counted_cash"))  # السالب مسموح
        except ValueError:
            flash("النقد المعدود يجب أن يكون رقمًا صحيحًا.", "danger")
            return redirect(url_for("close_shift"))
        shift.counted_cash = counted
        shift.note = request.form.get("note")
        shift.status = "closed"
        shift.closed_at = datetime.utcnow()
        if shift.cashbox:
            # رصيد الخزينة يتحرك لحظيًا مع كل بيع/استرجاع/مصروف، فعند الإغلاق يُسجَّل الفرق بين المعدود
            # والرصيد كقيد "عجز/زيادة صندوق" ظاهر في سجل التحويلات (بدل مسح الرصيد واستبداله بصمت).
            _record_cash_count_difference(shift.cashbox, shift.counted_cash, f"عند إغلاق وردية #{shift.id}")
        log_action("close_shift", "shift", shift.id,
                   f"الفرق: {shift.difference()} | معدود: {shift.counted_cash}")
        db.session.commit()
        try:
            from backup_service import request_drive_upload
            request_drive_upload()  # نسخة احتياطية + رفع إلى Google Drive في الخلفية بعد كل إغلاق وردية
        except Exception:
            app.logger.exception("تعذر طلب النسخ الاحتياطي بعد إغلاق الوردية")
        flash("تم إغلاق الوردية", "success")
        return redirect(url_for("shift_detail", sid=shift.id))
    return render_template("shift_close.html", shift=shift, currency=app.config["CURRENCY"])


@app.route("/shifts")
@login_required
def shifts():
    query = Shift.query.order_by(Shift.opened_at.desc())
    if not current_user.is_manager_or_admin():
        query = query.filter_by(user_id=current_user.id)

    start = _parse_date(request.args.get("start"))
    end = _parse_date(request.args.get("end"), end=True)
    if start:
        query = query.filter(Shift.opened_at >= start)
    if end:
        query = query.filter(Shift.opened_at <= end)

    # الأداء: صفحة من 30 وردية، و"المتوقع" يُحسب مرة واحدة لكل وردية (كان يُحسب 3 مرات بالقالب)
    page_shifts, pager = _paginate(query.options(db.joinedload(Shift.user)), SHIFTS_PAGE_SIZE)
    rows = []
    for s in page_shifts:
        expected = s.expected_cash()
        diff = round(s.counted_cash - expected, 2) if s.counted_cash is not None else None
        rows.append({"shift": s, "expected": expected, "difference": diff})
    return render_template("shifts.html", rows=rows, pager=pager, currency=app.config["CURRENCY"],
                            start=request.args.get("start", ""), end=request.args.get("end", ""))


@app.route("/shifts/<int:sid>")
@login_required
def shift_detail(sid):
    shift = Shift.query.get_or_404(sid)
    if not current_user.is_manager_or_admin() and shift.user_id != current_user.id:
        flash("لا تملك صلاحية عرض هذه الوردية", "danger")
        return redirect(url_for("shifts"))
    return render_template("shift_detail.html", shift=shift, currency=app.config["CURRENCY"])


@app.route("/shifts/<int:sid>/edit", methods=["POST"])
@login_required
@permission_required("shifts_manage")
def edit_shift(sid):
    shift = Shift.query.get_or_404(sid)
    before = {"opening_cash": shift.opening_cash, "counted_cash": shift.counted_cash, "note": shift.note}
    # تعديل القيم النقدية للوردية (الافتتاحي/المعدود) يتطلب صلاحية دقيقة إضافية فوق shifts_manage،
    # لأن هذا الحقل تحديدًا يُستخدم لاحقًا في حساب الفرق (difference) عند إغلاق الصندوق.
    try:
        new_opening = _parse_money(request.form.get("opening_cash"), shift.opening_cash)
        new_counted = (_parse_money(request.form.get("counted_cash"))
                       if request.form.get("counted_cash") not in (None, "") else None)
    except ValueError:
        flash("القيم النقدية يجب أن تكون أرقامًا صحيحة (السالب مسموح).", "danger")
        return redirect(url_for("shift_detail", sid=sid))
    wants_value_change = (new_opening != shift.opening_cash or new_counted is not None)
    if wants_value_change and not current_user.has_permission("shift_values_edit"):
        flash("لا تملك صلاحية تعديل القيم النقدية للوردية.", "danger")
        return redirect(url_for("shift_detail", sid=sid))
    shift.opening_cash = new_opening
    if new_counted is not None:
        shift.counted_cash = new_counted
    shift.note = request.form.get("note", shift.note)
    log_action("edit_shift", "shift", sid,
               f"قبل: {before} | بعد: opening_cash={shift.opening_cash}, counted_cash={shift.counted_cash}, note={shift.note}")
    db.session.commit()
    flash("تم تعديل بيانات الوردية.", "success")
    return redirect(url_for("shift_detail", sid=sid))


@app.route("/shifts/<int:sid>/delete", methods=["POST"])
@login_required
@permission_required("shifts_manage")
def delete_shift(sid):
    shift = Shift.query.get_or_404(sid)
    linked_invoices = Invoice.query.filter_by(shift_id=sid).count()
    if linked_invoices:
        # لا نحذف الفواتير المرتبطة، فقط نفك ارتباطها بالوردية حتى لا تُفقد بيانات المبيعات
        Invoice.query.filter_by(shift_id=sid).update({Invoice.shift_id: None})
    log_action("delete_shift", "shift", sid,
               f"user={shift.user.username if shift.user else ''}; invoices_unlinked={linked_invoices}")
    db.session.delete(shift)
    db.session.commit()
    flash("تم حذف الوردية. تم فكّ ارتباط أي فواتير كانت مرتبطة بها دون حذفها.", "info")
    return redirect(url_for("shifts"))


def _shift_report_rows(shift):
    cur = app.config["CURRENCY"]
    return [
        ["أمين الصندوق", shift.user.full_name if shift.user else "-"],
        ["الخزينة", shift.cashbox.name if shift.cashbox else "-"],
        ["وقت الفتح", to_local(shift.opened_at).strftime("%Y-%m-%d %H:%M")],
        ["وقت الإغلاق", to_local(shift.closed_at).strftime("%Y-%m-%d %H:%M") if shift.closed_at else "لا تزال مفتوحة"],
        [f"الرصيد الافتتاحي ({cur})", round(shift.opening_cash, 2)],
        [f"إجمالي المبيعات ({cur})", round(shift.total_sales(), 2)],
        [f"المبيعات النقدية ({cur})", round(shift.cash_sales(), 2)],
        [f"المبيعات عبر البنك ({cur})", round(shift.bank_sales(), 2)],
        [f"المبيعات الآجلة ({cur})", round(shift.credit_sales(), 2)],
        [f"تحصيل من عملاء ({cur})", round(shift.collected_from_customers(), 2)],
        [f"إيرادات أخرى ({cur})", round(shift.other_income(), 2)],
        [f"إجمالي المقبوضات ({cur})", round(shift.total_receipts(), 2)],
        [f"إجمالي المصروفات ({cur})", round(shift.total_expenses(), 2)],
        [f"مرتجعات (إرجاع عادي) خلال الوردية ({cur})", shift.pure_returns_total()],
        ["عدد عمليات الإرجاع", len(shift.pure_returns())],
        ["عدد عمليات الاستبدال", len(shift.exchanges())],
        [f"قيمة البضاعة المُعادة بالاستبدال ({cur})", shift.exchange_returned_total()],
        [f"قيمة البضاعة الجديدة بالاستبدال ({cur})", shift.exchange_new_total()],
        [f"فروقات استبدال حصّلناها ({cur})", shift.exchange_collected()],
        [f"فروقات استبدال رددناها ({cur})", shift.exchange_refunded()],
        [f"مردود للعملاء نقدًا ({cur})", shift.refunds_by_method()["cash"]],
        [f"مردود للعملاء بنكيًا ({cur})", shift.refunds_by_method()["bank"]],
        [f"مردود كخصم من دين العملاء ({cur})", shift.refunds_by_method()["customer_credit"]],
        [f"صافي المبيعات بعد المرتجعات ({cur})", shift.net_sales()],
        ["قطع مباعة", shift.pieces_sold()],
        ["قطع مُرجَعة", shift.pieces_returned()],
        ["عدد الفواتير", shift.invoices_count()],
        ["عدد العمليات (فواتير + حركات مالية)", shift.operations_count()],
        [f"محوَّل إلى الخزينة خلال الوردية ({cur})", shift.transfers_in()],
        [f"محوَّل من الخزينة خلال الوردية ({cur})", shift.transfers_out()],
        [f"الرصيد النهائي المتوقع بالصندوق ({cur})", shift.expected_cash()],
        [f"الرصيد الفعلي المعدود ({cur})", shift.counted_cash if shift.counted_cash is not None else "لم يُغلق بعد"],
        [f"الفرق ({cur})", shift.difference() if shift.difference() is not None else "-"],
    ]


@app.route("/shifts/<int:sid>/report")
@login_required
def shift_report_print(sid):
    """تقرير وردية مالي تفصيلي قابل للطباعة (A4 أو طابعة إيصالات 80/58مم): مبيعات حسب طريقة الدفع،
    المرتجعات والاستبدالات، الأصناف المباعة، المصروفات، ومطابقة الصندوق."""
    shift = Shift.query.get_or_404(sid)
    if not current_user.is_manager_or_admin() and shift.user_id != current_user.id:
        flash("لا تملك صلاحية عرض هذه الوردية", "danger")
        return redirect(url_for("shifts"))
    s = get_settings()
    return render_template("shift_report_print.html", shift=shift, company=s["company_name"],
                           company_phone=s["company_phone"], company_address=s["company_address"],
                           currency=s["currency"], printed_at=datetime.now())


@app.route("/shifts/<int:sid>/export/excel")
@login_required
def export_shift_excel_route(sid):
    shift = Shift.query.get_or_404(sid)
    if not current_user.is_manager_or_admin() and shift.user_id != current_user.id:
        flash("لا تملك صلاحية تصدير هذه الوردية", "danger")
        return redirect(url_for("shifts"))
    from export_service import export_generic_excel
    rows = _shift_report_rows(shift)
    buf = export_generic_excel(f"تقرير وردية #{shift.id}", ["البند", "القيمة"], rows)
    log_action("export_shift_excel", "shift", sid)
    db.session.commit()
    return send_file(buf, as_attachment=True, download_name=f"shift_{shift.id}_report.xlsx",
                      mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/shifts/<int:sid>/export/pdf")
@login_required
def export_shift_pdf_route(sid):
    shift = Shift.query.get_or_404(sid)
    if not current_user.is_manager_or_admin() and shift.user_id != current_user.id:
        flash("لا تملك صلاحية تصدير هذه الوردية", "danger")
        return redirect(url_for("shifts"))
    from export_service import export_generic_pdf
    rows = _shift_report_rows(shift)
    buf = export_generic_pdf(f"تقرير وردية #{shift.id}", ["البند", "القيمة"], rows, company=app.config["COMPANY_NAME"])
    log_action("export_shift_pdf", "shift", sid)
    db.session.commit()
    return send_file(buf, as_attachment=True, download_name=f"shift_{shift.id}_report.pdf", mimetype="application/pdf")


# ---------------------------------------------------------------------------
# نقطة البيع (POS) - دفع نقدي/بنكي/آجل/مزدوج + التحقق من سقف الائتمان
# ---------------------------------------------------------------------------
@app.route("/pos")
@login_required
@permission_required("pos")
def pos():
    shift = get_active_shift()
    if not shift:
        flash("يجب فتح وردية أولاً قبل استخدام نقطة البيع", "warning")
        return redirect(url_for("open_shift"))
    last_invoice = (Invoice.query.filter_by(shift_id=shift.id, user_id=current_user.id)
                    .order_by(Invoice.id.desc()).first())
    return render_template("pos.html", warehouses=Warehouse.query.all(),
                            customers=Customer.query.all(), currency=app.config["CURRENCY"],
                            shift=shift, can_override_price=current_user.has_permission("override_price"),
                            last_invoice=last_invoice,
                            default_warehouse_id=(shift.cashbox.default_warehouse_id if shift.cashbox else None))


@app.route("/pos/lookup")
@login_required
def pos_lookup():
    code = request.args.get("barcode", "")
    product = Product.query.filter_by(barcode=code, active=True).first()
    if not product:
        return jsonify({"error": "لم يتم العثور على المنتج بهذا الباركود"}), 404
    return jsonify({"id": product.id, "name": product.name, "price": product.sale_price,
                     "purchase_price": product.purchase_price,
                     "barcode": product.barcode, "quantity": product.total_quantity()})


@app.route("/pos/search")
@login_required
def pos_search():
    """بحث عن منتج بالاسم/المقاس/اللون/جزء من الباركود من شاشة البيع - للقطع التي بلا ملصق أو ملصقها تالف."""
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify([])
    like = f"%{q}%"
    found = (Product.query.filter(Product.active == True,  # noqa: E712
                                  db.or_(Product.name.ilike(like), Product.barcode.ilike(like),
                                         Product.size.ilike(like), Product.color.ilike(like)))
             .order_by(Product.name).limit(15).all())
    return jsonify([{"id": p.id, "name": p.name, "price": p.sale_price, "barcode": p.barcode,
                     "size": p.size or "", "color": p.color or "", "quantity": p.total_quantity()}
                    for p in found])


@app.route("/pos/customer-credit/<int:cid>")
@login_required
def pos_customer_credit(cid):
    customer = Customer.query.get_or_404(cid)
    return jsonify({"balance": customer.balance, "credit_limit": customer.credit_limit,
                     "available": customer.available_credit()})


@app.route("/pos/checkout", methods=["POST"])
@login_required
def pos_checkout():
    shift = get_active_shift()
    if not shift:
        return jsonify({"error": "يجب فتح وردية أولاً"}), 400

    data = request.get_json(force=True)
    try:
        warehouse_id = int(data.get("warehouse_id") or 0)
    except (TypeError, ValueError):
        warehouse_id = 0
    items = data.get("items") or []
    if not warehouse_id or not items or not Warehouse.query.get(warehouse_id):
        return jsonify({"error": "السلة فارغة أو لم يتم اختيار مخزن"}), 400

    # الفحص الحسابي: الخادم لا يثق بأرقام الشاشة. الكميات والأسعار والخصم ومجموع الدفعات تُتحقق هنا،
    # وإلا تقبل فاتورة بكمية سالبة (تزيد المخزون) أو دفعات لا تساوي الإجمالي.
    payment_method = data.get("payment_method", "cash")
    if payment_method not in ("cash", "bank", "credit", "split"):
        return jsonify({"error": "طريقة دفع غير معروفة"}), 400
    customer_id = data.get("customer_id") or None
    try:
        discount = round(float(data.get("discount") or 0), 2)
        cash_amount = round(float(data.get("cash_amount") or 0), 2)
        bank_amount = round(float(data.get("bank_amount") or 0), 2)
        credit_amount = round(float(data.get("credit_amount") or 0), 2)
    except (TypeError, ValueError):
        return jsonify({"error": "مبالغ الدفع أو الخصم غير صالحة"}), 400
    if min(discount, cash_amount, bank_amount, credit_amount) < 0:
        return jsonify({"error": "لا تُقبل مبالغ سالبة في الدفع أو الخصم"}), 400
    bank_name = data.get("bank_name")
    can_override_price = current_user.has_permission("override_price")

    lines, subtotal = [], 0.0
    for it in items:
        try:
            product = Product.query.get(int(it["id"]))
            qty = int(it["qty"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "بيانات صنف غير صالحة"}), 400
        if not product or not product.active:
            return jsonify({"error": "يوجد صنف غير موجود أو موقوف"}), 400
        if qty <= 0:
            return jsonify({"error": f"الكمية يجب أن تكون أكبر من صفر: {product.display_name()}"}), 400
        # تغيير السعر يدويًا لهذه الفاتورة فقط ولمن يملك صلاحية override_price، وإلا يُتجاهل
        unit_price = product.sale_price or 0
        if can_override_price and it.get("price") is not None:
            try:
                custom_price = float(it.get("price"))
                if custom_price >= 0:
                    unit_price = custom_price
            except (TypeError, ValueError):
                pass
        lines.append((product, qty, unit_price))
        subtotal += qty * unit_price
    subtotal = round(subtotal, 2)
    if discount > subtotal:
        return jsonify({"error": "الخصم أكبر من قيمة الفاتورة"}), 400
    total = round(subtotal - discount, 2)
    if payment_method == "cash":
        cash_amount, bank_amount, credit_amount = total, 0, 0
    elif payment_method == "bank":
        cash_amount, bank_amount, credit_amount = 0, total, 0
    elif payment_method == "credit":
        cash_amount, bank_amount, credit_amount = 0, 0, total
    elif abs(cash_amount + bank_amount + credit_amount - total) > 0.01:
        return jsonify({"error": f"مجموع طرق الدفع ({cash_amount + bank_amount + credit_amount:.2f}) "
                                 f"لا يساوي إجمالي الفاتورة ({total:.2f})"}), 400

    # التحقق من سقف الائتمان قبل تنفيذ البيع
    if credit_amount > 0:
        customer = Customer.query.get(customer_id) if customer_id else None
        if not customer:
            return jsonify({"error": "البيع الآجل يتطلب اختيار عميل"}), 400
        if customer.would_exceed_limit(credit_amount) and not data.get("force"):
            return jsonify({
                "warning": "credit_limit_exceeded",
                "message": f"تحذير: هذا البيع سيجعل دين العميل {customer.name} "
                           f"يتجاوز سقف الائتمان المسموح ({customer.credit_limit:.2f} {app.config['CURRENCY']}). "
                           f"المتاح حاليًا: {customer.available_credit():.2f}",
            }), 409

    invoice = Invoice(
        invoice_number=generate_number("INV", Invoice), customer_id=customer_id,
        warehouse_id=warehouse_id, user_id=current_user.id, shift_id=shift.id,
        discount=discount, payment_method=payment_method,
        bank_name=bank_name if payment_method in ("bank", "split") else None,
    )
    db.session.add(invoice)
    db.session.flush()
    for product, qty, unit_price in lines:
        stock = get_or_create_stock(product.id, warehouse_id)
        if stock.quantity < qty:
            db.session.rollback()
            return jsonify({"error": f"الكمية غير متوفرة للمنتج: {product.display_name()}"}), 400
        stock.quantity -= qty
        db.session.add(InvoiceItem(invoice_id=invoice.id, product_id=product.id, quantity=qty,
                                    unit_price=unit_price, total=round(qty * unit_price, 2),
                                    unit_cost=product.purchase_price or 0))
        db.session.add(StockMovement(product_id=product.id, warehouse_id=warehouse_id,
                                      movement_type="out", quantity=qty, reference=invoice.invoice_number))

    invoice.subtotal = subtotal
    invoice.total = total
    invoice.cash_amount = cash_amount
    invoice.bank_amount = bank_amount
    invoice.credit_amount = credit_amount
    if credit_amount > 0 and customer_id:
        Customer.query.get(customer_id).balance += credit_amount
    _post_invoice_cash(invoice, shift, sign=1)

    log_action("create_invoice", "invoice", invoice.id,
               f"{invoice.invoice_number} - {invoice.payment_label()} - {invoice.total}")

    # تعارض نادر في رقم الفاتورة (كاشيران يحفظان في نفس اللحظة): نتراجع عن العملية كاملة ونطلب الإعادة.
    # (المحاولة السابقة كانت تعيد حفظ رأس الفاتورة فقط بعد التراجع، فتُحفظ فاتورة بلا أصناف ولا مخزون.)
    from sqlalchemy.exc import IntegrityError
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "تزامن الحفظ مع فاتورة أخرى في نفس اللحظة - اضغط إتمام البيع مرة أخرى "
                                 "(السلة كما هي ولم يُسجَّل شيء)."}), 409
    return jsonify({"success": True, "invoice_id": invoice.id})


# ---------------------------------------------------------------------------
# المبيعات + طباعة الفاتورة + طلبات إلغاء/موافقة
# ---------------------------------------------------------------------------
SALES_PAGE_SIZE = 50
SHIFTS_PAGE_SIZE = 30


@app.route("/sales")
@login_required
def sales():
    query = Invoice.query.order_by(Invoice.date.desc())
    if not current_user.is_manager_or_admin():
        query = query.filter_by(user_id=current_user.id)

    customer_name = request.args.get("customer_name", "").strip()
    invoice_number = request.args.get("invoice_number", "").strip()
    start = _parse_date(request.args.get("start"))
    end = _parse_date(request.args.get("end"), end=True)

    if customer_name:
        query = query.join(Customer, Invoice.customer_id == Customer.id, isouter=True) \
                     .filter(Customer.name.ilike(f"%{customer_name}%"))
    if invoice_number:
        query = query.filter(Invoice.invoice_number.ilike(f"%{invoice_number}%"))
    if start:
        query = query.filter(Invoice.date >= start)
    if end:
        query = query.filter(Invoice.date <= end)

    # ملخص كل الفواتير المطابقة للبحث (ليس الصفحة الظاهرة فقط): العدد والمجموع (بدون الملغاة)
    summary_q = query.filter(Invoice.status != "voided").order_by(None)
    match_total = summary_q.with_entities(db.func.coalesce(db.func.sum(Invoice.total), 0)).scalar() or 0
    match_count = summary_q.count()

    # الأداء: صفحة من 50 فاتورة بدل كل الفواتير، وأسماء العملاء تُجلب مع الفواتير في نفس الاستعلام
    invoices, pager = _paginate(query.options(db.joinedload(Invoice.customer))
                                .order_by(Invoice.date.desc(), Invoice.id.desc()), SALES_PAGE_SIZE)
    returned_map = {}
    if invoices:
        ids = [i.id for i in invoices]
        returned_map = dict(db.session.query(SalesReturn.invoice_id, db.func.sum(SalesReturn.total))
                            .filter(SalesReturn.invoice_id.in_(ids))
                            .group_by(SalesReturn.invoice_id).all())
    return render_template("sales.html", invoices=invoices, customer_name=customer_name, returned_map=returned_map,
                            invoice_number=invoice_number, pager=pager,
                            match_total=round(match_total, 2), match_count=match_count,
                            currency=app.config["CURRENCY"],
                            start=request.args.get("start", ""), end=request.args.get("end", ""))


@app.route("/sales/invoice/<int:iid>")
@login_required
def invoice_print(iid):
    invoice = Invoice.query.get_or_404(iid)
    s = get_settings()
    exchange_src = invoice.exchange_source()
    refund_entry = (AccountEntry.query.filter_by(entry_type="sales_return", reference=exchange_src.return_number).first()
                    if exchange_src else None)
    return render_template("invoice_print.html", invoice=invoice,
                           returned_total=invoice.returned_total(),
                           exchange_src=exchange_src, refund_entry=refund_entry,
                           returns=invoice.returns_list(),
                           company=s["company_name"],
                           company_subtitle=s["company_subtitle"],
                           company_phone=s["company_phone"],
                           company_address=s["company_address"],
                           invoice_header_note=s["invoice_header_note"],
                           invoice_footer=s["invoice_footer"],
                           currency=s["currency"])


@app.route("/sales/invoice/<int:iid>/export/pdf")
@login_required
def export_invoice_pdf_route(iid):
    invoice = Invoice.query.get_or_404(iid)
    s = get_settings()
    from export_service import export_generic_pdf
    headers = ["المنتج", "الباركود", "الكمية", "سعر الوحدة", "الإجمالي"]
    rows = [[it.product.name if it.product else "-", (it.product.barcode if it.product else "") or "-",
             it.quantity, round(it.unit_price, 2), round(it.total, 2)]
            for it in invoice.items]
    rows.append(["", "", "", "الإجمالي الفرعي", round(invoice.subtotal, 2)])
    if invoice.discount:
        rows.append(["", "", "", "الخصم", round(invoice.discount, 2)])
    rows.append(["", "", "", "الإجمالي النهائي", round(invoice.total, 2)])
    src = invoice.exchange_source()
    if src:
        rows.append(["", "", "", f"رصيد استبدال ({src.invoice.invoice_number})", -round(src.total, 2)])
        rows.append(["", "", "", "فرق الاستبدال (+ يدفع / - يُرد)", src.exchange_difference()])
    rows.append(["", "", "", "المدفوع", round(invoice.paid_amount, 2)])
    rows.append(["", "", "", "المتبقي (آجل)", round(invoice.credit_amount, 2)])
    for r in invoice.returns_list():
        kind = "استبدال" if r.is_exchange() else "إرجاع"
        rows.append([f"{kind} {r.return_number}", to_local(r.date).strftime("%Y-%m-%d"),
                     "، ".join(f"{ri.invoice_item.product.display_name()}×{ri.quantity}" for ri in r.items), "", -round(r.total, 2)])
    if invoice.returned_total():
        rows.append(["", "", "", "الصافي بعد المرتجع", invoice.net_total()])
    title = (f"فاتورة {invoice.invoice_number} - العميل: {invoice.customer.name if invoice.customer else 'نقدي'} "
             f"- التاريخ: {to_local(invoice.date).strftime('%Y-%m-%d %H:%M')} - العملة: {s['currency']}")
    buf = export_generic_pdf(title, headers, rows, company=s["company_name"])
    log_action("export_invoice_pdf", "invoice", iid)
    db.session.commit()
    return send_file(buf, as_attachment=True, download_name=f"{invoice.invoice_number}.pdf", mimetype="application/pdf")


@app.route("/sales/export/excel")
@login_required
def export_sales_excel_route():
    from export_service import export_invoices_excel
    query = Invoice.query.order_by(Invoice.date.desc())
    if not current_user.is_manager_or_admin():
        query = query.filter_by(user_id=current_user.id)
    invoices = query.all()
    buf = export_invoices_excel(invoices, currency=app.config["CURRENCY"])
    log_action("export_sales_excel", entity_type="report", details=f"{len(invoices)} فاتورة")
    db.session.commit()
    filename = f"invoices_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return send_file(buf, as_attachment=True, download_name=filename,
                      mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/sales/delete/<int:iid>", methods=["POST"])
@login_required
@permission_required("sales_delete")
def delete_invoice(iid):
    """حذف فاتورة نهائيًا من النظام (مختلف عن الإلغاء): يعكس أثرها على المخزون ورصيد العميل،
    ويتعامل بأمان مع كل ما يرتبط بها (مرتجعات، طلبات موافقة، خصومات، قيود مالية) مهما كانت
    قيمة الفاتورة أو عدد أصنافها، داخل عملية واحدة: إما تنجح كلها أو لا يتغير شيء."""
    invoice = Invoice.query.get_or_404(iid)
    number = invoice.invoice_number
    blocked = _exchange_lock_reason(invoice)
    if blocked:
        flash(blocked, "danger")
        return redirect(url_for("sales"))
    snapshot = {
        "invoice_number": number,
        "customer": invoice.customer.name if invoice.customer else None,
        "date": invoice.date.isoformat() if invoice.date else None,
        "total": invoice.total, "subtotal": invoice.subtotal, "discount": invoice.discount,
        "payment_method": invoice.payment_method, "cash_amount": invoice.cash_amount,
        "bank_amount": invoice.bank_amount, "credit_amount": invoice.credit_amount,
        "status": invoice.status, "warehouse_id": invoice.warehouse_id,
        "items": [{"product": it.product.name if it.product else it.product_id,
                   "quantity": it.quantity, "unit_price": it.unit_price, "total": it.total}
                  for it in invoice.items],
    }
    try:
        # 1) المرتجعات المرتبطة بالفاتورة: نعكس أثرها المالي ثم نحذفها (هي السبب الأشهر لفشل الحذف
        #    سابقًا، لأن سطور المرتجع تشير لأسطر الفاتورة ولا يمكن فك ارتباطها).
        returned_by_item = {}
        returns = SalesReturn.query.filter_by(invoice_id=invoice.id).all()
        snapshot["returns"] = []
        for sr in returns:
            snapshot["returns"].append({"number": sr.return_number, "total": sr.total,
                                         "refund_method": sr.refund_method})
            for ri in sr.items:
                returned_by_item[ri.invoice_item_id] = returned_by_item.get(ri.invoice_item_id, 0) + ri.quantity
            ret_entry = AccountEntry.query.filter_by(entry_type="sales_return",
                                                      reference=sr.return_number).first()
            if ret_entry:
                if sr.refund_method == "customer_credit":
                    cust = Customer.query.get(ret_entry.customer_id) if ret_entry.customer_id else None
                    if cust:
                        cust.balance = (cust.balance or 0) + (sr.total or 0)  # كان الإرجاع قد خصم من دينه
                else:
                    _apply_cashbox_cash_effect(ret_entry, sign=-1)  # يعيد المبلغ المسترجع للخزينة
                db.session.delete(ret_entry)
            StockMovement.query.filter_by(
                reference=f"إرجاع من فاتورة {number}").delete(synchronize_session=False)
            db.session.delete(sr)  # سطور المرتجع تُحذف تلقائيًا (cascade)
        db.session.flush()

        # 2) أثر الفاتورة نفسها على المخزون ودين العميل (إن لم تكن ملغاة سابقًا).
        #    الكميات المرتجعة سابقًا عادت للمخزون أصلاً، فنعيد الباقي فقط.
        if invoice.status != "voided":
            _post_invoice_cash(invoice, invoice.shift, sign=-1)  # النقد الذي دخل الخزينة يخرج منها
            for item in invoice.items:
                qty_back = (item.quantity or 0) - returned_by_item.get(item.id, 0)
                if invoice.stock_affected is False:
                    qty_back = 0  # فاتورة قديمة لم تُخصم من المخزون أصلًا
                if qty_back > 0:
                    stock = get_or_create_stock(item.product_id, invoice.warehouse_id)
                    stock.quantity = (stock.quantity or 0) + qty_back
            if invoice.credit_amount and invoice.customer_id:
                cust = Customer.query.get(invoice.customer_id)
                if cust:
                    cust.balance = (cust.balance or 0) - invoice.credit_amount

        # 3) فك/حذف كل ما يشير للفاتورة حتى لا يبقى شيء يمنع حذفها أو يتيتّم
        ApprovalRequest.query.filter_by(invoice_id=invoice.id).delete(synchronize_session=False)
        CustomerDiscount.query.filter_by(invoice_id=invoice.id).update(
            {CustomerDiscount.invoice_id: None}, synchronize_session=False)
        AccountEntry.query.filter_by(invoice_id=invoice.id).update(
            {AccountEntry.invoice_id: None}, synchronize_session=False)
        StockMovement.query.filter_by(reference=number).delete(synchronize_session=False)
        StockMovement.query.filter_by(reference=f"إلغاء فاتورة {number}").delete(synchronize_session=False)
        db.session.flush()
        db.session.expire_all()

        invoice = Invoice.query.get(iid)
        log_action("delete_invoice", "invoice", iid, json.dumps(snapshot, ensure_ascii=False, default=str))
        db.session.delete(invoice)  # الأصناف تُحذف تلقائيًا (cascade)
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.error("فشل حذف الفاتورة %s:\n%s", iid, traceback.format_exc())
        flash(f"تعذّر حذف الفاتورة {number}، ولم يتم تغيير أي شيء (تفاصيل الخطأ في logs/erp.log).", "danger")
        return redirect(url_for("sales"))
    flash(f"تم حذف الفاتورة {number} نهائيًا، وتمت إعادة أي كميات أو أرصدة متأثرة.", "info")
    return redirect(url_for("sales"))


@app.route("/sales/<int:iid>/return", methods=["GET", "POST"])
@login_required
@permission_required("sales_return")
def sales_return(iid):
    """إرجاع كلي أو جزئي لفاتورة بيع - يعيد الكمية المختارة فقط للمخزون، ويرجّع الفلوس فعليًا
    (نقدًا/بنكًا من خزينة محددة، أو كخصم مباشر من دين العميل)، بخلاف إلغاء الفاتورة الذي يبطلها
    بالكامل بدون التعامل مع الفلوس. منفصل تمامًا عن سجل الإلغاءات ومرتبط بالفاتورة الأصلية."""
    invoice = Invoice.query.get_or_404(iid)
    if invoice.status == "voided":
        flash("هذه الفاتورة ملغاة بالكامل بالفعل - لا حاجة لإرجاع جزئي منها.", "danger")
        return redirect(url_for("sales"))

    if request.method == "POST":
        refund_method = request.form.get("refund_method")
        if refund_method not in ("cash", "bank", "customer_credit"):
            flash("اختر طريقة استرجاع صحيحة.", "danger")
            return redirect(url_for("sales_return", iid=iid))
        if refund_method == "customer_credit" and not invoice.customer_id:
            flash("لا يمكن الاسترجاع كخصم من رصيد عميل - هذه الفاتورة بلا عميل مرتبط.", "danger")
            return redirect(url_for("sales_return", iid=iid))

        active = get_active_shift()
        sr = SalesReturn(return_number=generate_number("RET", SalesReturn), invoice_id=invoice.id,
                          reason=request.form.get("reason"), refund_method=refund_method,
                          user_id=current_user.id, shift_id=active.id if active else None)
        total = 0.0
        for item in invoice.items:
            qty = int(request.form.get(f"qty_{item.id}") or 0)
            if qty <= 0:
                continue
            returnable = item.returnable_quantity()
            if qty > returnable:
                flash(f'لا يمكن إرجاع {qty} من "{item.product.display_name()}" - المتاح للإرجاع {returnable} فقط.', "danger")
                return redirect(url_for("sales_return", iid=iid))
            line_total = round(qty * item.unit_price, 2)
            total += line_total
            sr.items.append(SalesReturnItem(invoice_item_id=item.id, quantity=qty,
                                             unit_price=item.unit_price, total=line_total))
            stock = get_or_create_stock(item.product_id, invoice.warehouse_id)
            stock.quantity += qty
            db.session.add(StockMovement(product_id=item.product_id, warehouse_id=invoice.warehouse_id,
                                          movement_type="in", quantity=qty,
                                          reference=f"إرجاع من فاتورة {invoice.invoice_number}"))
        if total <= 0:
            flash("حدد كمية صنف واحد على الأقل للإرجاع.", "danger")
            return redirect(url_for("sales_return", iid=iid))
        sr.total = round(total, 2)

        cashbox_id, box_error = _resolve_form_cashbox(request.form)
        if box_error or (refund_method == "cash" and not cashbox_id):
            flash(box_error or "اختر الخزينة التي سيخرج منها المبلغ النقدي.", "danger")
            return redirect(url_for("sales_return", iid=iid))
        entry = AccountEntry(entry_type="sales_return", amount=sr.total, user_id=current_user.id,
                              note=f"إرجاع {sr.return_number} لفاتورة {invoice.invoice_number}" +
                                   (f" - {sr.reason}" if sr.reason else ""),
                              reference=sr.return_number, cashbox_id=cashbox_id)
        if refund_method == "customer_credit":
            customer = invoice.customer
            if sr.total > (customer.balance or 0) + 0.0001:
                flash(f"مبلغ الإرجاع ({sr.total:.2f}) أكبر من دين {customer.name} الحالي "
                      f"({customer.balance:.2f}) - استخدم استرجاع نقدي أو بنكي بدل ذلك.", "danger")
                return redirect(url_for("sales_return", iid=iid))
            entry.customer_id = customer.id
            customer.balance -= sr.total
        else:
            entry.payment_method = refund_method
            _apply_cashbox_cash_effect(entry, sign=1)  # الأثر معرّف -1 أصلاً لهذا النوع، فيخرج من الخزينة

        db.session.add(sr)
        db.session.add(entry)
        log_action("sales_return", "invoice", invoice.id, f"{sr.return_number}: {sr.total}")
        db.session.commit()
        flash(f"تم تسجيل الإرجاع {sr.return_number} بقيمة {sr.total:.2f} {app.config['CURRENCY']}", "success")
        return redirect(url_for("sales"))

    return render_template("sales_return.html", invoice=invoice,
                            cashboxes=Cashbox.query.filter_by(active=True).all(),
                            currency=app.config["CURRENCY"])



def _discard(response):
    """يتراجع عن أي تغيير غير محفوظ في الجلسة (مخزون مُنشأ مؤقتًا...) قبل إرجاع رسالة الخطأ."""
    db.session.rollback()
    return response


def _exchange_lock_reason(invoice):
    """فاتورة دخلت في عملية استبدال (أصلية أو جديدة) لا تُحذف/تُلغى/تُعدَّل مباشرة: الإلغاء يعيد كل
    كمياتها للمخزون وهي جزئيًا عادت مسبقًا بالاستبدال، فيتضاعف المخزون وتنكسر تسوية الفرق."""
    if invoice.exchange_credit:
        return (f"الفاتورة {invoice.invoice_number} فاتورة استبدال - لا يمكن حذفها/إلغاؤها/تعديلها مباشرة. "
                "استخدم الإرجاع إن أراد العميل إعادة البضاعة.")
    if SalesReturn.query.filter_by(invoice_id=invoice.id, refund_method="exchange").first():
        return (f"على الفاتورة {invoice.invoice_number} عملية استبدال مسجّلة - لا يمكن حذفها/إلغاؤها/تعديلها "
                "مباشرة حتى لا يتضاعف المخزون. استخدم الإرجاع للأصناف المتبقية.")
    return None


@app.route("/sales/find")
@login_required
def find_invoice_for_service():
    """بحث سريع برقم الفاتورة من شاشة البيع، ثم التوجيه لشاشة الإرجاع أو الاستبدال مباشرة."""
    number = (request.args.get("number") or "").strip()
    action = request.args.get("action", "exchange")
    if not number:
        flash("أدخل رقم الفاتورة.", "warning")
        return redirect(request.referrer or url_for("pos"))
    invoice = (Invoice.query.filter_by(invoice_number=number).first()
               or Invoice.query.filter(Invoice.invoice_number.ilike(f"%{number}")).order_by(Invoice.date.desc()).first())
    if not invoice:
        flash(f"لا توجد فاتورة بالرقم {number}.", "danger")
        return redirect(request.referrer or url_for("pos"))
    if action == "return":
        return redirect(url_for("sales_return", iid=invoice.id))
    return redirect(url_for("sales_exchange", iid=invoice.id))


@app.route("/sales/<int:iid>/exchange", methods=["GET", "POST"])
@login_required
@permission_required("sales_exchange")
def sales_exchange(iid):
    """استبدال: العميل يعيد أصنافًا من فاتورة سابقة ويأخذ أصنافًا أخرى بدلها في نفس العملية.
    التنفيذ المحاسبي (معاملة واحدة):
      1) سجل إرجاع SalesReturn (refund_method=exchange) يعيد الأصناف المُرجَعة لمخزن الفاتورة الأصلية.
      2) فاتورة جديدة INV للأصناف الجديدة في الوردية الحالية، يُدفع جزء منها بقيمة المُرجَع (exchange_credit).
      3) الفرق: إن كانت الجديدة أغلى يدفعه العميل (نقد/بنك/آجل) ضمن الفاتورة الجديدة، وإن كانت أرخص
         يُرد له عبر قيد sales_return (نقد/بنك من خزينة الوردية، أو خصم من دينه).
    هكذا تبقى تقارير الأرباح صحيحة تلقائيًا: المرتجع يُخصم من المبيعات والفاتورة الجديدة تُضاف إليها."""
    invoice = Invoice.query.get_or_404(iid)
    if invoice.status != "completed":
        flash("لا يمكن الاستبدال من فاتورة ملغاة أو عليها طلب إلغاء معلّق.", "danger")
        return redirect(url_for("sales"))
    shift = get_active_shift()
    if not shift:
        flash("يجب فتح وردية أولاً قبل تنفيذ الاستبدال (الفرق النقدي يُسجَّل على صندوق الوردية).", "warning")
        return redirect(url_for("open_shift"))
    default_wh = (shift.cashbox.default_warehouse_id if shift.cashbox and shift.cashbox.default_warehouse_id
                  else invoice.warehouse_id)

    if request.method == "POST":
        form = request.form
        back = redirect(url_for("sales_exchange", iid=iid))
        try:
            warehouse_id = int(form.get("warehouse_id") or default_wh)
        except ValueError:
            warehouse_id = default_wh
        if not Warehouse.query.get(warehouse_id):
            flash("اختر مخزنًا صحيحًا للأصناف الجديدة.", "danger")
            return _discard(back)

        # 1) الأصناف المُرجَعة
        sr = SalesReturn(return_number=generate_number("RET", SalesReturn), invoice_id=invoice.id,
                          reason=form.get("reason"), refund_method="exchange", user_id=current_user.id,
                          shift_id=shift.id, cashbox_id=shift.cashbox_id)
        returned_total, returned_lines = 0.0, []
        for item in invoice.items:
            try:
                qty = int(form.get(f"qty_{item.id}") or 0)
            except ValueError:
                qty = 0
            if qty <= 0:
                continue
            if qty > item.returnable_quantity():
                flash(f'لا يمكن إرجاع {qty} من "{item.product.display_name()}" - المتاح {item.returnable_quantity()} فقط.', "danger")
                return _discard(back)
            line_total = round(qty * item.unit_price, 2)
            returned_total += line_total
            returned_lines.append((item, qty))
            sr.items.append(SalesReturnItem(invoice_item_id=item.id, quantity=qty,
                                             unit_price=item.unit_price, total=line_total))
        returned_total = round(returned_total, 2)
        if not returned_lines:
            flash("حدد صنفًا واحدًا على الأقل يعيده العميل.", "danger")
            return _discard(back)

        # 2) الأصناف الجديدة
        new_lines = {}
        for pid, qty in zip(form.getlist("new_product_id"), form.getlist("new_qty")):
            try:
                pid, qty = int(pid), int(qty)
            except ValueError:
                continue
            if qty > 0:
                new_lines[pid] = new_lines.get(pid, 0) + qty
        if not new_lines:
            flash("أضف صنفًا جديدًا واحدًا على الأقل يأخذه العميل (لإرجاع بدون بديل استخدم شاشة الإرجاع).", "danger")
            return _discard(back)

        new_items, new_total = [], 0.0
        for pid, qty in new_lines.items():
            product = Product.query.get(pid)
            if not product or not product.active:
                flash("يوجد صنف جديد غير صالح.", "danger")
                return _discard(back)
            stock = get_or_create_stock(pid, warehouse_id)
            if (stock.quantity or 0) < qty:
                flash(f'الكمية غير متوفرة من "{product.display_name()}" في المخزن المختار (المتاح {stock.quantity}).', "danger")
                return _discard(back)
            line_total = round(qty * (product.sale_price or 0), 2)
            new_total += line_total
            new_items.append((product, qty, product.sale_price or 0, line_total, stock))
        new_total = round(new_total, 2)
        difference = round(new_total - returned_total, 2)  # موجب: يدفع العميل، سالب: نرد له

        settle_method = form.get("settle_method") or "cash"
        customer = invoice.customer
        if difference > 0 and settle_method not in ("cash", "bank", "credit"):
            flash("اختر طريقة تحصيل الفرق.", "danger")
            return _discard(back)
        if difference < 0 and settle_method not in ("cash", "bank", "customer_credit"):
            flash("اختر طريقة رد الفرق للعميل.", "danger")
            return _discard(back)
        if settle_method in ("credit", "customer_credit") and difference != 0 and not customer:
            flash("الآجل/الخصم من الدين يتطلب فاتورة مرتبطة بعميل.", "danger")
            return _discard(back)
        if difference < 0 and settle_method == "customer_credit" and -difference > (customer.balance or 0) + 0.0001:
            flash(f"الفرق المستحق للعميل ({-difference:.2f}) أكبر من دينه الحالي ({customer.balance:.2f}) - "
                  "رده نقدًا أو بنكيًا.", "danger")
            return _discard(back)
        if difference > 0 and settle_method == "credit" and customer.would_exceed_limit(difference) \
                and not form.get("force"):
            flash(f"الفرق سيجعل دين {customer.name} يتجاوز سقف الائتمان ({customer.credit_limit:.2f}).", "danger")
            return _discard(back)

        # --- التنفيذ ---
        for it, qty in returned_lines:
            st = get_or_create_stock(it.product_id, invoice.warehouse_id)
            st.quantity = (st.quantity or 0) + qty
            db.session.add(StockMovement(product_id=it.product_id, warehouse_id=invoice.warehouse_id,
                                          movement_type="in", quantity=qty,
                                          reference=f"استبدال {sr.return_number} من فاتورة {invoice.invoice_number}"))
        sr.total = returned_total
        db.session.add(sr)

        new_inv = Invoice(invoice_number=generate_number("INV", Invoice), customer_id=invoice.customer_id,
                          warehouse_id=warehouse_id, user_id=current_user.id, shift_id=shift.id,
                          subtotal=new_total, discount=0, total=new_total, payment_method="exchange",
                          exchange_credit=min(returned_total, new_total))
        if difference > 0:
            if settle_method == "cash":
                new_inv.cash_amount = difference
            elif settle_method == "bank":
                new_inv.bank_amount = difference
                new_inv.bank_name = form.get("bank_name") or None
            else:
                new_inv.credit_amount = difference
                customer.balance = (customer.balance or 0) + difference
        db.session.add(new_inv)
        db.session.flush()
        for product, qty, price, line_total, stock in new_items:
            stock.quantity -= qty
            db.session.add(InvoiceItem(invoice_id=new_inv.id, product_id=product.id, quantity=qty,
                                        unit_price=price, total=line_total, unit_cost=product.purchase_price or 0))
            db.session.add(StockMovement(product_id=product.id, warehouse_id=warehouse_id, movement_type="out",
                                          quantity=qty, reference=new_inv.invoice_number))
        sr.exchange_invoice_id = new_inv.id
        _post_invoice_cash(new_inv, shift, sign=1)  # فرق الاستبدال المدفوع نقدًا يدخل الخزينة فورًا

        if difference < 0:
            refund = -difference
            entry = AccountEntry(entry_type="sales_return", amount=refund, user_id=current_user.id,
                                  reference=sr.return_number, invoice_id=new_inv.id,
                                  note=f"فرق استبدال {sr.return_number} (فاتورة {invoice.invoice_number} ← {new_inv.invoice_number})")
            if settle_method == "customer_credit":
                entry.customer_id = customer.id
                customer.balance = (customer.balance or 0) - refund
            else:
                entry.payment_method = settle_method
                entry.cashbox_id = shift.cashbox_id
                _apply_cashbox_cash_effect(entry, sign=1)
            db.session.add(entry)

        log_action("sales_exchange", "invoice", invoice.id,
                   f"{sr.return_number}: مُرجَع {returned_total} | جديد {new_inv.invoice_number} {new_total} | فرق {difference}")
        db.session.commit()
        if difference > 0:
            msg = f"يدفع العميل الفرق {difference:.2f}"
        elif difference < 0:
            msg = f"يُرد للعميل الفرق {-difference:.2f}"
        else:
            msg = "تبديل متكافئ بدون فرق"
        flash(f"تم الاستبدال {sr.return_number} - {msg} {app.config['CURRENCY']}.", "success")
        return redirect(url_for("invoice_print", iid=new_inv.id))

    return render_template("sales_exchange.html", invoice=invoice, shift=shift,
                            warehouses=Warehouse.query.all(), default_warehouse_id=default_wh,
                            currency=app.config["CURRENCY"])


def _void_block_reason(invoice):
    """الإلغاء = كأن البيعة لم تحدث، ويُسترجع للعميل ما دفعه. يُسمح به فقط لفواتير وردية ما زالت مفتوحة
    وبلا مرتجعات؛ الفاتورة الأقدم أو التي عليها إرجاع تُعالج بشاشة الإرجاع (حتى يُسجَّل الاسترجاع صراحة
    ولا يُسترجع المبلغ نفسه مرتين)."""
    blocked = _exchange_lock_reason(invoice)
    if blocked:
        return blocked
    if SalesReturn.query.filter_by(invoice_id=invoice.id).first():
        return (f"على الفاتورة {invoice.invoice_number} إرجاع مسجّل - لا تُلغى كاملة حتى لا يُسترجع نفس المبلغ مرتين. "
                "استخدم الإرجاع للأصناف المتبقية.")
    if invoice.shift_id is None or not invoice.shift or invoice.shift.status != "open":
        return (f"الفاتورة {invoice.invoice_number} من وردية مغلقة - الإلغاء مسموح فقط داخل نفس الوردية. "
                "استخدم الإرجاع ليُسجَّل استرجاع الفلوس في الوردية الحالية.")
    return None


def _void_invoice(invoice, reason):
    """ينفّذ الإلغاء: يعيد المخزون ودين العميل، ويخرج النقد المدفوع من رصيد خزينة الوردية (استرجاع للعميل)."""
    restore_invoice_stock(invoice)
    _post_invoice_cash(invoice, invoice.shift, sign=-1)
    invoice.status = "voided"
    invoice.void_reason = reason


@app.route("/sales/void/<int:iid>", methods=["POST"])
@login_required
@permission_required("sales_void")
def request_void_invoice(iid):
    invoice = Invoice.query.get_or_404(iid)
    reason = request.form.get("reason", "")
    blocked = _void_block_reason(invoice)
    if blocked:
        flash(blocked, "danger")
        return redirect(url_for("sales"))
    if current_user.is_manager_or_admin():
        # المدير/المالك يلغي مباشرة
        if invoice.status == "voided":
            flash("الفاتورة ملغاة مسبقًا.", "warning")
            return redirect(url_for("sales"))
        _void_invoice(invoice, reason)
        log_action("void_invoice", "invoice", invoice.id,
                   f"{reason} | استرجاع نقدي {invoice.cash_amount or 0:.2f}، بنكي {invoice.bank_amount or 0:.2f}")
        db.session.commit()
        flash("تم إلغاء الفاتورة", "info")
    else:
        # الكاشير يقدّم طلب إلغاء يحتاج موافقة
        existing = ApprovalRequest.query.filter_by(invoice_id=iid, status="pending").first()
        if existing:
            flash("يوجد طلب إلغاء معلّق لهذه الفاتورة بالفعل", "warning")
        else:
            db.session.add(ApprovalRequest(request_type="void_invoice", invoice_id=iid,
                                            requested_by=current_user.id, reason=reason))
            invoice.status = "void_requested"
            log_action("request_void", "invoice", invoice.id, reason)
            db.session.commit()
            flash("تم إرسال طلب الإلغاء إلى المالك للموافقة", "info")
    return redirect(url_for("sales"))


@app.route("/approvals")
@login_required
@permission_required("approvals")
def approvals():
    pending = ApprovalRequest.query.filter_by(status="pending").order_by(ApprovalRequest.created_at.desc()).all()
    history = ApprovalRequest.query.filter(ApprovalRequest.status != "pending").order_by(
        ApprovalRequest.reviewed_at.desc()).limit(30).all()
    return render_template("approvals.html", pending=pending, history=history)


@app.route("/approvals/<int:aid>/decide", methods=["POST"])
@login_required
@permission_required("approvals")
def decide_approval(aid):
    req = ApprovalRequest.query.get_or_404(aid)
    decision = request.form.get("decision")  # approve / reject
    req.reviewed_by = current_user.id
    req.reviewed_at = datetime.utcnow()
    req.review_note = request.form.get("review_note")
    invoice = req.invoice
    if decision == "approve":
        req.status = "approved"
        if req.request_type == "void_invoice":
            if invoice.status != "voided":
                blocked = _void_block_reason(invoice)
                if blocked:
                    flash(f"تعذرت الموافقة: {blocked}", "danger")
                    return redirect(url_for("approvals"))
                _void_invoice(invoice, req.reason)
        elif req.request_type == "edit_invoice":
            try:
                payload = json.loads(req.proposed_data or "{}")
                apply_invoice_edit(invoice, payload)
            except Exception as exc:
                db.session.rollback()
                flash(f"تعذر تطبيق التعديل: {exc}", "danger")
                return redirect(url_for("approvals"))
        log_action("approve_request", "approval_request", req.id)
        flash("تمت الموافقة على الطلب", "success")
    else:
        req.status = "rejected"
        invoice.status = "completed"
        log_action("reject_request", "approval_request", req.id)
        flash("تم رفض الطلب", "info")
    db.session.commit()
    return redirect(url_for("approvals"))


# ---------------------------------------------------------------------------
# الحسابات
# ---------------------------------------------------------------------------
ACCOUNTS_PAGE_SIZE = 100
INCOME_TYPES = ("income", "customer_payment")
EXPENSE_TYPES = ("expense", "salary_payment", "inventory_loss")


@app.route("/accounts")
@login_required
@permission_required("accounts")
def accounts():
    # الأداء: الصفحة كانت تحمّل كل الحركات وتبني لكل حركة نافذة تعديل كاملة بقوائم كل العملاء والموردين،
    # فيتضخم حجمها (حركات × عملاء) ويبطؤ فتحها. الآن: الإجماليات تُحسب داخل قاعدة البيانات، والجدول
    # يعرض صفحة واحدة (100 حركة) بفلاتر، ونافذة تعديل واحدة مشتركة تُعبّأ عند الضغط.
    scope_cashbox = branch_scope_cashbox_id()
    filter_cashbox_id = None if scope_cashbox else request.args.get("cashbox_id", type=int)
    box_filter = scope_cashbox or filter_cashbox_id

    totals_q = db.session.query(AccountEntry.entry_type, db.func.sum(AccountEntry.amount))
    if box_filter:
        totals_q = totals_q.filter(AccountEntry.cashbox_id == box_filter)
    by_type = {t: (v or 0) for t, v in totals_q.group_by(AccountEntry.entry_type).all()}
    total_income = sum(by_type.get(t, 0) for t in INCOME_TYPES)
    # ملاحظة محاسبية: السلفة وسحب المالك ودفع المورد ليست "مصروفًا" (لا تدخل قائمة الدخل) بل بنود
    # ميزانية عمومية، لذلك استُبعدت من إجمالي المصروفات هنا. دفع الراتب وخسارة تسوية المخزون مصروفات
    # تشغيلية حقيقية فتُحتسب مثل "expense".
    total_expense = sum(by_type.get(t, 0) for t in EXPENSE_TYPES)
    total_owner_withdrawals = by_type.get("owner_withdrawal", 0)
    total_employee_advances_open = db.session.query(db.func.coalesce(db.func.sum(Employee.balance), 0)) \
        .filter(Employee.balance > 0).scalar()
    total_supplier_debt = _supplier_debt_text()

    # ملخص سريع لكل فرع/نقطة بيع على حدة - للمدير/الأدمن فقط
    branch_summary, unassigned_count = [], 0
    if not scope_cashbox:
        q = db.session.query(AccountEntry.cashbox_id, AccountEntry.entry_type,
                             db.func.sum(AccountEntry.amount), db.func.count(AccountEntry.id))
        if filter_cashbox_id:
            q = q.filter(AccountEntry.cashbox_id == filter_cashbox_id)
        per_box = {}
        for box_id, etype, amount, count in q.group_by(AccountEntry.cashbox_id, AccountEntry.entry_type).all():
            g = per_box.setdefault(box_id, {"income": 0.0, "expense": 0.0, "count": 0})
            g["count"] += count
            if etype in INCOME_TYPES:
                g["income"] += amount or 0
            elif etype in EXPENSE_TYPES:
                g["expense"] += amount or 0
        for box in Cashbox.query.filter_by(active=True).order_by(Cashbox.id).all():
            g = per_box.get(box.id, {"income": 0.0, "expense": 0.0, "count": 0})
            branch_summary.append({"cashbox": box, **g})
        unassigned_count = per_box.get(None, {}).get("count", 0)

    # الجدول: صفحة واحدة مع فلاتر (تاريخ/نوع/بحث في الملاحظة)
    entries_q = AccountEntry.query.options(
        db.joinedload(AccountEntry.customer), db.joinedload(AccountEntry.supplier),
        db.joinedload(AccountEntry.employee), db.joinedload(AccountEntry.external_borrower))
    if box_filter:
        entries_q = entries_q.filter(AccountEntry.cashbox_id == box_filter)
    start = _parse_date(request.args.get("start"))
    end = _parse_date(request.args.get("end"), end=True)
    f_type = request.args.get("f_type", "")
    q_text = (request.args.get("q") or "").strip()
    if start:
        entries_q = entries_q.filter(AccountEntry.date >= start)
    if end:
        entries_q = entries_q.filter(AccountEntry.date <= end)
    if f_type:
        entries_q = entries_q.filter(AccountEntry.entry_type == f_type)
    if q_text:
        entries_q = entries_q.filter(AccountEntry.note.ilike(f"%{q_text}%"))
    entries, pager = _paginate(entries_q.order_by(AccountEntry.date.desc(), AccountEntry.id.desc()),
                               ACCOUNTS_PAGE_SIZE)
    filtered_total = None
    if start or end or f_type or q_text:
        filtered_total = round(entries_q.with_entities(db.func.coalesce(db.func.sum(AccountEntry.amount), 0))
                               .order_by(None).scalar() or 0, 2)

    return render_template("accounts.html", entries=entries, customers=Customer.query.order_by(Customer.name).all(),
                            branch_summary=branch_summary, unassigned_count=unassigned_count,
                            scope_cashbox=scope_cashbox,
                            total_owner_withdrawals=total_owner_withdrawals,
                            total_employee_advances_open=total_employee_advances_open,
                            total_supplier_debt=total_supplier_debt,
                            suppliers=Supplier.query.all(), employees=Employee.query.order_by(Employee.name).all(),
                            external_borrowers=ExternalBorrower.query.order_by(ExternalBorrower.name).all(),
                            cashboxes=Cashbox.query.filter_by(active=True).all(),
                            funds=[_fund_view(f) for f in FundAccount.query.filter_by(active=True).order_by(FundAccount.id).all()],
                            total_income=total_income, total_expense=total_expense, currency=app.config["CURRENCY"],
                            pager=pager, filtered_total=filtered_total, f_type=f_type, q_text=q_text,
                            locked_ids=_locked_entry_ids(entries),
                            start=request.args.get("start", ""), end=request.args.get("end", ""))


ACCOUNT_ENTRY_TYPES = (
    "income", "expense", "customer_payment", "customer_debt", "supplier_payment",
    "employee_advance", "employee_repayment", "employee_draw", "external_advance", "external_repayment",
    "salary_payment", "owner_withdrawal", "customer_discount", "inventory_loss",
    "loan_received", "loan_repayment", "sales_return",
)


# أنواع الحركات اللي تمثّل نقدية فعلية داخلة/خارجة من صندوق، والاتجاه (+1 = تدخل الصندوق،
# -1 = تخرج منه). أي نوع غير موجود هنا (دين آجل، خصم عميل، خسارة مخزون...) لا يمسّ أي صندوق لأنه
# لا حركة نقدية فعلية معه.
CASHBOX_CASH_EFFECT = {
    "income": 1, "expense": -1, "customer_payment": 1, "supplier_payment": -1,
    "employee_advance": -1, "employee_repayment": 1, "employee_draw": -1,
    "external_advance": -1, "external_repayment": 1, "salary_payment": -1,
    "owner_withdrawal": -1, "loan_received": 1, "loan_repayment": -1, "sales_return": -1,
    # مصاريف شراء إضافية (شحن/جمارك): نقد يخرج فعلًا، لكنها ليست مصروفًا في قائمة الدخل لأنها دخلت
    # في تكلفة القطع وتُحسب ضمن تكلفة البضاعة المباعة عند البيع (وإلا تُخصم من الربح مرتين).
    "purchase_cost": -1,
}


def _allocate_supplier_payment(supplier_id, amount):
    """يوزّع دفعة مورد على ديونه القديمة المفتوحة (SupplierDebt) الأقدم استحقاقًا أولًا، حتى يتطابق حقل
    \"المسدَّد\" في سجل الدين مع رصيد المورد. ما يزيد عن الديون المفتوحة يبقى سدادًا لفواتير شراء آجلة
    (لا سجل دين لها)."""
    if not supplier_id or amount <= 0:
        return
    debts = (SupplierDebt.query.filter_by(supplier_id=supplier_id)
             .order_by(SupplierDebt.due_date.asc().nullslast(), SupplierDebt.created_at.asc()).all())
    left = amount
    for d in debts:
        room = d.remaining()
        if room <= 0.0001 or left <= 0:
            continue
        take = min(room, left)
        d.paid_amount = round((d.paid_amount or 0) + take, 2)
        d.refresh_status()
        left -= take


def _deallocate_supplier_payment(supplier_id, amount):
    """عكس _allocate_supplier_payment عند حذف/تعديل دفعة: يُنقص المسدَّد من الأحدث إلى الأقدم."""
    if not supplier_id or amount <= 0:
        return
    debts = (SupplierDebt.query.filter_by(supplier_id=supplier_id)
             .order_by(SupplierDebt.created_at.desc()).all())
    left = amount
    for d in debts:
        paid = d.paid_amount or 0
        if paid <= 0 or left <= 0:
            continue
        take = min(paid, left)
        d.paid_amount = round(paid - take, 2)
        d.refresh_status()
        left -= take


def _apply_cashbox_cash_effect(entry, sign):
    """يطبّق أثر الحركة (sign=+1 تطبيق، -1 عكس) على رصيد الخزينة الفعلية المختارة (entry.cashbox)،
    لكن فقط إن كانت طريقة الدفع نقدًا - الدفع البنكي لا يمسّ صندوق النقد الفعلي."""
    effect = CASHBOX_CASH_EFFECT.get(entry.entry_type)
    if effect and entry.payment_method == "cash" and entry.cashbox_id:
        cashbox = Cashbox.query.get(entry.cashbox_id)
        if cashbox:
            cashbox.balance = (cashbox.balance or 0) + (sign * effect * entry.amount)


def _configure_entry_from_form(entry, entry_type, form, apply_effect):
    """يربط الحركة المالية (entry) بالطرف الصحيح حسب نوعها (عميل/مورد/موظف/شخص من خارج الموظفين)،
    ويتحقق من القيود (سقف ائتمان العميل، سقف سلفة الموظف، صلاحيات السحب والسلف الخارجية).
    إن كانت apply_effect=True يطبّق أثر الحركة فورًا على رصيد الطرف المرتبط.
    تُستخدم هذه الدالة الواحدة في الإضافة والتعديل معًا، بما يسمح بتغيير نوع الحركة نفسها عند
    التعديل (مثال: من "سلفة موظف" إلى "سحب مالك") دون تكرار المنطق أو نسيان حالة.
    تعيد نص تحذير/خطأ يمنع الحفظ، أو None إذا كان كل شيء سليمًا."""
    entry.customer_id = None
    entry.supplier_id = None
    entry.employee_id = None
    entry.external_borrower_id = None
    # حقول العملة تخص دفعة المورد فقط؛ تُصفَّر حتى لا تبقى من نوع سابق عند تغيير نوع الحركة بالتعديل
    entry.foreign_amount = None
    entry.currency_code, entry.exchange_rate = "LYD", 1
    amount = entry.amount

    # الحساب البنكي/خزنة العملة (للدفع غير النقدي). بدون اختيار: الحركة الجديدة تدخل الحساب الافتراضي
    # تلقائيًا (_assign_default_fund). حساب بعملة أجنبية لا يُستعمل إلا لدفع مورد بنفس العملة.
    entry.fund_account_id = None
    fund = None
    if entry.payment_method == "bank" and form.get("fund_account_id"):
        fund = FundAccount.query.get(form.get("fund_account_id"))
        if not fund:
            return "اختر حسابًا بنكيًا صحيحًا."
        if (fund.currency_code or "LYD") != "LYD":
            sup = Supplier.query.get(form.get("supplier_id")) if entry_type == "supplier_payment" else None
            if not sup or (sup.currency_code or "LYD") != fund.currency_code:
                return (f"الحساب \"{fund.name}\" بعملة {fund.currency_code}: يُستعمل فقط للدفع لمورد حسابه "
                        f"بـ{fund.currency_code}. لتحويله إلى دينار استعمل \"صرف عملة\" من صفحة الحسابات البنكية.")
        entry.fund_account_id = fund.id

    # ربط الحركة بخزينة/نقطة بيع (= الفرع) - هذا الربط له غرضان معًا:
    # 1) محاسبي: لو كانت الحركة نقدية (payment_method=cash) من نوع له أثر نقدي، يُخصم/يُضاف
    #    المبلغ فورًا من رصيد هذه الخزينة الحقيقي (_apply_cashbox_cash_effect أسفل الدالة).
    # 2) تنظيمي/عزل فروع: يُسجَّل على *كل* الحركات (حتى الآجلة وغير النقدية) حتى تُعرض لاحقًا
    #    ضمن حسابات فرعها فقط - وهو أساس "كل نقطة بيع لها حساباتها لحالها" بين الفروع.
    # مستخدم مقيَّد بفرع (غير مدير/أدمن وله خزينة مخصصة): يُفرض عليه فرعه تلقائيًا ويُتجاهل أي
    # اختيار آخر يحاول إرساله، حتى لا يقدر يسجّل حركة باسم فرع غيره مهما تلاعب بالنموذج.
    scope_cashbox = branch_scope_cashbox_id()
    if scope_cashbox:
        entry.cashbox_id = scope_cashbox
    else:
        entry.cashbox_id = None
        cashbox_id = form.get("cashbox_id")
        if cashbox_id:
            cashbox = Cashbox.query.get(cashbox_id)
            if not cashbox:
                return "اختر نقطة بيع/خزينة صحيحة."
            entry.cashbox_id = cashbox.id

    if entry_type == "customer_payment":
        customer = Customer.query.get(form.get("customer_id"))
        if not customer:
            return "اختر عميلاً صحيحًا."
        entry.customer_id = customer.id
        if apply_effect:
            customer.balance -= amount

    elif entry_type == "customer_debt":
        customer = Customer.query.get(form.get("customer_id"))
        if not customer:
            return "اختر عميلاً صحيحًا."
        if customer.would_exceed_limit(amount) and not form.get("force"):
            return (f"تحذير: هذا الدين سيجعل رصيد {customer.name} يتجاوز سقف الائتمان المتاح "
                    f"({customer.available_credit()} {app.config['CURRENCY']}). "
                    "إن كنت متأكدًا، أعد المحاولة مع تفعيل خيار التجاوز.")
        entry.customer_id = customer.id
        if apply_effect:
            customer.balance += amount

    elif entry_type == "supplier_payment":
        supplier = Supplier.query.get(form.get("supplier_id"))
        if not supplier:
            return "اختر موردًا صحيحًا."
        # المبلغ = ما خرج بالدينار. رصيد المورد بعملته، فيُخصم منه (المبلغ ÷ سعر الصرف)
        # (كان يُخصم المبلغ بالدينار من رصيد بالدولار مباشرة: 700 د.ل كانت تُنقص 700$).
        # الطريقة الأوضح (سعر السوق يتغير كل يوم): المالك يكتب المبلغين كما حصلا فعلًا، كم خرج بالدينار
        # وكم وصل المورد بعملته، والسعر يُحسب منهما. وإن كتب السعر فقط يُحسب المبلغ الأجنبي منه.
        currency = supplier.currency_code or "LYD"
        rate, foreign = 1.0, amount
        if currency != "LYD":
            try:
                foreign = float(form.get("foreign_amount") or 0)
                rate = float(form.get("exchange_rate") or 0)
            except ValueError:
                foreign, rate = 0, 0
            if foreign > 0:
                rate = round(amount / foreign, 6)
            elif rate > 0:
                foreign = round(amount / rate, 4)
            else:
                return (f"المورد {supplier.name} يتعامل بـ{currency}: اكتب المبلغ الذي وصله بـ{currency} "
                        f"أو سعر الصرف (كم دينار = 1 {currency}).")
            if fund and fund.currency_code == currency:
                # الدفع من خزنة العملة نفسها: يخرج منها المبلغ الأجنبي، فيجب أن يكون موجودًا فيها
                have = _fund_balance(fund, exclude_entry_id=entry.id)
                if foreign > have + 0.005:
                    return (f"رصيد \"{fund.name}\" ({have:,.2f} {currency}) أقل من المبلغ ({foreign:,.2f}). "
                            "سجّل شراء العملة أولًا من \"صرف عملة\".")
        entry.supplier_id = supplier.id
        entry.currency_code = currency
        entry.exchange_rate = rate
        entry.local_amount = amount
        entry.foreign_amount = foreign
        if apply_effect:
            supplier.balance -= foreign
            _allocate_supplier_payment(supplier.id, foreign)

    elif entry_type == "employee_advance":
        employee = Employee.query.get(form.get("employee_id"))
        if not employee:
            return "اختر موظفًا صحيحًا."
        if employee.would_exceed_advance_limit(amount):
            if not (form.get("force") and current_user.has_permission("advance_override_limit")):
                return (f"تحذير: هذه السلفة ستجعل رصيد سلف {employee.name} "
                        f"({(employee.balance or 0) + amount:.2f}) يتجاوز السقف المسموح "
                        f"({employee.advance_limit():.2f} {app.config['CURRENCY']}) بناءً على راتبه. "
                        "إن كنت مخوّلًا بتجاوز السقف، فعّل خيار التجاوز.")
        entry.employee_id = employee.id
        if apply_effect:
            employee.balance = (employee.balance or 0) + amount

    elif entry_type == "employee_repayment":
        employee = Employee.query.get(form.get("employee_id"))
        if not employee:
            return "اختر موظفًا صحيحًا."
        if amount > (employee.balance or 0) + 0.0001:
            return f"المبلغ المدخل ({amount:.2f}) أكبر من سلفة {employee.name} المستحقة ({employee.balance:.2f})."
        entry.employee_id = employee.id
        if apply_effect:
            employee.balance = (employee.balance or 0) - amount

    elif entry_type == "employee_draw":
        # سحب على الحساب: مثل السلفة في أنه يزيد رصيد ما هو مستحق على الموظف (يُسدَّد لاحقًا
        # بنفس قيد "تسديد سلفة موظف")، لكن بدون التحقق من سقف الراتب - يتطلب صلاحية خاصة صراحة
        # بدل ذلك لأنه أعلى مخاطرة (لا حد أقصى تلقائي).
        if not current_user.has_permission("employee_draw"):
            return "لا تملك صلاحية تسجيل سحب على الحساب لموظف."
        employee = Employee.query.get(form.get("employee_id"))
        if not employee:
            return "اختر موظفًا صحيحًا."
        entry.employee_id = employee.id
        if apply_effect:
            employee.balance = (employee.balance or 0) + amount

    elif entry_type == "external_advance":
        if not current_user.has_permission("external_borrowers"):
            return "لا تملك صلاحية تسجيل سلفة لشخص من خارج الموظفين."
        borrower = ExternalBorrower.query.get(form.get("external_borrower_id"))
        if not borrower:
            return "اختر الشخص الذي صُرفت له السلفة، أو أضفه أولاً من زر \"شخص جديد\"."
        entry.external_borrower_id = borrower.id
        if apply_effect:
            borrower.balance = (borrower.balance or 0) + amount

    elif entry_type == "external_repayment":
        if not current_user.has_permission("external_borrowers"):
            return "لا تملك صلاحية تسجيل تسديد سلفة لشخص من خارج الموظفين."
        borrower = ExternalBorrower.query.get(form.get("external_borrower_id"))
        if not borrower:
            return "اختر الشخص الصحيح."
        if amount > (borrower.balance or 0) + 0.0001:
            return f"المبلغ المدخل ({amount:.2f}) أكبر من سلفة {borrower.name} المستحقة ({borrower.balance:.2f})."
        entry.external_borrower_id = borrower.id
        if apply_effect:
            borrower.balance = (borrower.balance or 0) - amount

    elif entry_type == "customer_discount":
        if not current_user.has_permission("customer_discounts"):
            return "لا تملك صلاحية تسجيل خصم عميل."
        customer = Customer.query.get(form.get("customer_id"))
        if not customer:
            return "اختر عميلاً صحيحًا."
        if amount > (customer.balance or 0) + 0.0001:
            return f"الخصم ({amount:.2f}) أكبر من رصيد {customer.name} المستحق ({customer.balance:.2f})."
        entry.customer_id = customer.id
        if apply_effect:
            customer.balance = (customer.balance or 0) - amount

    elif entry_type == "sales_return":
        # حركة إرجاع بيع: إن اختير عميل فهي استرجاع كخصم من دينه (لا نقد فعلي، فتُصفَّر طريقة الدفع حتى
        # لا يُخصم نفس المبلغ مرتين: من الدين ومن الخزينة)، وإلا فهي استرجاع نقدي/بنكي يمسّ الخزينة فقط.
        # (قبل هذا الفرع كان تعديل حركة إرجاع يفصلها عن العميل ويُعيد دينه للأعلى بلا مقابل.)
        cust_id = form.get("customer_id")
        if cust_id:
            customer = Customer.query.get(cust_id)
            if not customer:
                return "اختر عميلاً صحيحًا."
            if amount > (customer.balance or 0) + 0.0001:
                return (f"مبلغ الإرجاع ({amount:.2f}) أكبر من دين {customer.name} الحالي "
                        f"({(customer.balance or 0):.2f}) - استخدم استرجاعًا نقديًا أو بنكيًا بدل ذلك.")
            entry.customer_id = customer.id
            entry.payment_method = None
            entry.fund_account_id = None
            if apply_effect:
                customer.balance = (customer.balance or 0) - amount

    elif entry_type == "inventory_loss":
        # خسارة مخزون (تلف/سرقة/نقص جرد): مصروف تشغيلي حقيقي، يُنشأ عادة تلقائيًا من صفحة
        # "تسوية المخزون" عند نقص الكمية، ولا يرتبط بأي طرف (عميل/مورد/موظف).
        pass

    elif entry_type == "loan_received":
        # عكس "سلفة لشخص خارجي" تمامًا: هنا المحل هو اللي ياخذ سلفة/قرض من بنك أو شخص، فتدخل
        # نقدية للمحل (عكس اتجاه السلفة العادية)، ويصبح المحل مدينًا لهذه الجهة - نستخدم نفس
        # جدول "الأشخاص من خارج الموظفين" ونفس حقل balance، لكن بإشارة سالبة تعني "المحل مدين
        # لهم" (بدل الإشارة الموجبة المعتادة التي تعني "هم مدينون للمحل").
        if not current_user.has_permission("external_borrowers"):
            return "لا تملك صلاحية تسجيل قرض/سلفة من جهة خارجية."
        borrower = ExternalBorrower.query.get(form.get("external_borrower_id"))
        if not borrower:
            return "اختر البنك أو الشخص الذي أخذ المحل سلفة/قرضًا منه، أو أضفه أولاً."
        entry.external_borrower_id = borrower.id
        if apply_effect:
            borrower.balance = (borrower.balance or 0) - amount

    elif entry_type == "loan_repayment":
        if not current_user.has_permission("external_borrowers"):
            return "لا تملك صلاحية تسجيل تسديد قرض/سلفة لجهة خارجية."
        borrower = ExternalBorrower.query.get(form.get("external_borrower_id"))
        if not borrower:
            return "اختر الجهة الصحيحة."
        owed_to_them = max(0, -(borrower.balance or 0))
        if amount > owed_to_them + 0.0001:
            return f"المبلغ المدخل ({amount:.2f}) أكبر من المستحق عليك لـ{borrower.name} ({owed_to_them:.2f})."
        entry.external_borrower_id = borrower.id
        if apply_effect:
            borrower.balance = (borrower.balance or 0) + amount

    elif entry_type == "salary_payment":
        # دفع راتب: مصروف تشغيلي حقيقي (يدخل في الربح الصافي)، بخلاف السلفة وسحب المالك.
        # ربطه بموظف اختياري (لغرض السجل والتقارير فقط) ولا يؤثر مباشرة على رصيد سلفة الموظف -
        # خصم السلفة من الراتب يتم بقيد "تسديد سلفة موظف" منفصل يُنشأ تلقائيًا من مسار "صرف راتب"
        # المخصص في صفحة الموظفين (routes: pay_employee_salary)، حتى يبقى كل قيد بأثر واحد واضح.
        eid = form.get("employee_id")
        if eid:
            employee = Employee.query.get(eid)
            if not employee:
                return "اختر موظفًا صحيحًا."
            entry.employee_id = employee.id

    elif entry_type == "owner_withdrawal":
        if not current_user.has_permission("owner_withdrawal"):
            return "لا تملك صلاحية تسجيل سحب مالك."
        # سحب مالك: لا يرتبط بأي طرف، ولا يُنشئ التزامًا بالتسديد - يُسجَّل منفصلًا تمامًا عن
        # المصروفات وعن السلف حتى لا يُحتسب ضمن الربح الصافي.

    # income / expense: لا طرف مرتبط ولا تأثير على أي رصيد طرف (لكن قد يؤثر على خزينة، أدناه)
    if apply_effect:
        _apply_cashbox_cash_effect(entry, sign=1)
    return None


@app.route("/accounts/add", methods=["POST"])
@login_required
@permission_required("accounts")
def add_account_entry():
    entry_type = request.form.get("entry_type")
    if entry_type not in ACCOUNT_ENTRY_TYPES:
        flash("نوع حركة مالية غير معروف.", "danger")
        return redirect(url_for("accounts"))
    amount = float(request.form.get("amount") or 0)
    if amount <= 0:
        flash("المبلغ يجب أن يكون أكبر من صفر.", "danger")
        return redirect(url_for("accounts"))

    # خانة بنكي/نقد (بديل صفحة السندات القديمة، بدون ربط بحساب بنكي فعلي)
    payment_method = request.form.get("payment_method") or None
    if payment_method not in ("cash", "bank"):
        payment_method = None

    entry = AccountEntry(entry_type=entry_type, amount=amount, note=request.form.get("note"),
                          user_id=current_user.id, payment_method=payment_method, scope="general")
    error = _configure_entry_from_form(entry, entry_type, request.form, apply_effect=True)
    if error:
        flash(error, "warning" if error.startswith("تحذير") else "danger")
        return redirect(url_for("accounts"))

    db.session.add(entry)
    log_action("add_account_entry", "account_entry", details=f"{entry_type}: {amount}")
    db.session.commit()
    flash("تم تسجيل الحركة المالية", "success")
    return redirect(url_for("accounts"))


def _reverse_account_entry_effect(entry):
    """يعكس تأثير حركة مالية موجودة على رصيد الطرف المرتبط بها وعلى رصيد الخزينة المرتبطة
    (بحالتها الحالية قبل أي تعديل)، يُستخدم قبل تعديل الحركة أو حذفها."""
    _apply_cashbox_cash_effect(entry, sign=-1)
    if entry.entry_type == "customer_payment" and entry.customer:
        entry.customer.balance += entry.amount
    elif entry.entry_type == "customer_debt" and entry.customer:
        entry.customer.balance -= entry.amount
    elif entry.entry_type == "supplier_payment" and entry.supplier:
        in_supplier_currency = entry.supplier_amount()
        entry.supplier.balance += in_supplier_currency
        _deallocate_supplier_payment(entry.supplier_id, in_supplier_currency)
    elif entry.entry_type == "employee_advance" and entry.employee:
        entry.employee.balance -= entry.amount
    elif entry.entry_type == "employee_repayment" and entry.employee:
        entry.employee.balance += entry.amount
    elif entry.entry_type == "employee_draw" and entry.employee:
        entry.employee.balance -= entry.amount
    elif entry.entry_type == "external_advance" and entry.external_borrower:
        entry.external_borrower.balance -= entry.amount
    elif entry.entry_type == "external_repayment" and entry.external_borrower:
        entry.external_borrower.balance += entry.amount
    elif entry.entry_type == "customer_discount" and entry.customer:
        entry.customer.balance += entry.amount
    elif entry.entry_type == "sales_return" and entry.customer:
        entry.customer.balance += entry.amount
    elif entry.entry_type == "loan_received" and entry.external_borrower:
        entry.external_borrower.balance += entry.amount
    elif entry.entry_type == "loan_repayment" and entry.external_borrower:
        entry.external_borrower.balance -= entry.amount
    # income / expense / salary_payment / inventory_loss / owner_withdrawal: لا رصيد طرف لعكسه


def _system_entry_reason(entry):
    """قيود ينشئها النظام تلقائيًا من عملية أخرى لا تُعدَّل ولا تُحذف من صفحة الحسابات، وإلا تبقى العملية
    الأصلية مسجّلة بلا قيدها (مثلًا مرتجع مسجّل والفلوس المسترجعة محذوفة)."""
    if entry.purchase_id:
        return "هذه الحركة دفعة مرتبطة بفاتورة شراء - عدّل/احذف فاتورة الشراء نفسها."
    if entry.entry_type == "sales_return" and entry.reference and \
            SalesReturn.query.filter_by(return_number=entry.reference).first():
        return f"هذه الحركة ناتجة عن الإرجاع/الاستبدال {entry.reference} - لا تُعدَّل ولا تُحذف من هنا."
    if entry.entry_type == "inventory_loss" and entry.reference and \
            InventoryAdjustment.query.filter_by(adjustment_number=entry.reference).first():
        return f"هذه خسارة ناتجة عن تسوية المخزون {entry.reference} - عدّل أو احذف التسوية نفسها من صفحة تسوية المخزون."
    return None


def _locked_entry_ids(entries):
    """أرقام القيود التلقائية ضمن قائمة (لإخفاء أزرار التعديل/الحذف في الجدول)."""
    refs = [e.reference for e in entries if e.reference]
    ret = {r for (r,) in db.session.query(SalesReturn.return_number).filter(SalesReturn.return_number.in_(refs))} if refs else set()
    adj = {r for (r,) in db.session.query(InventoryAdjustment.adjustment_number)
           .filter(InventoryAdjustment.adjustment_number.in_(refs))} if refs else set()
    return {e.id for e in entries if e.purchase_id
            or (e.entry_type == "sales_return" and e.reference in ret)
            or (e.entry_type == "inventory_loss" and e.reference in adj)}


@app.route("/accounts/edit/<int:eid>", methods=["POST"])
@login_required
@permission_required("accounts")
def edit_account_entry(eid):
    entry = AccountEntry.query.get_or_404(eid)
    locked = _system_entry_reason(entry)
    if locked:
        flash(locked, "warning")
        return redirect(url_for("accounts"))
    new_amount = float(request.form.get("amount") or 0)
    if new_amount <= 0:
        flash("المبلغ يجب أن يكون أكبر من صفر.", "danger")
        return redirect(url_for("accounts"))
    new_entry_type = request.form.get("entry_type") or entry.entry_type
    if new_entry_type not in ACCOUNT_ENTRY_TYPES:
        flash("نوع حركة مالية غير معروف.", "danger")
        return redirect(url_for("accounts"))

    # 1) نعكس أثر الحركة بحالتها القديمة (نوعها وطرفها الحاليين) قبل أي تغيير على entry نفسها.
    _reverse_account_entry_effect(entry)

    # 2) نحدّث بيانات الحركة الأساسية.
    entry.entry_type = new_entry_type
    entry.amount = new_amount
    entry.note = request.form.get("note")
    payment_method = request.form.get("payment_method") or None
    if payment_method in ("cash", "bank"):
        entry.payment_method = payment_method

    # 3) نربطها بالطرف الجديد ونطبّق أثرها - قد يكون الطرف/النوع مختلفًا تمامًا عن القديم
    #    (مثال: تحويل حركة من "سلفة موظف" إلى "سحب مالك" أو إلى "سلفة لشخص من خارج الموظفين").
    error = _configure_entry_from_form(entry, new_entry_type, request.form, apply_effect=True)
    if error:
        db.session.rollback()
        flash(error, "warning" if error.startswith("تحذير") else "danger")
        return redirect(url_for("accounts"))

    log_action("edit_account_entry", "account_entry", eid, f"{new_entry_type}: {new_amount}")
    db.session.commit()
    flash("تم تعديل الحركة المالية", "success")
    return redirect(url_for("accounts"))


@app.route("/accounts/delete/<int:eid>")
@login_required
@permission_required("accounts")
def delete_account_entry(eid):
    entry = AccountEntry.query.get_or_404(eid)
    locked = _system_entry_reason(entry)
    if locked:
        flash(locked, "warning")
        return redirect(url_for("accounts"))
    _reverse_account_entry_effect(entry)
    log_action("delete_account_entry", "account_entry", eid, f"{entry.entry_type}: {entry.amount}")
    db.session.delete(entry)
    db.session.commit()
    flash("تم حذف الحركة المالية وعكس تأثيرها على الرصيد", "info")
    return redirect(url_for("accounts"))


# ---------------------------------------------------------------------------
# التقارير - مبيعات حسب طريقة الدفع + قيمة المخزون + الديون
# ---------------------------------------------------------------------------
@app.route("/reports")
@login_required
@permission_required("reports")
def reports():
    products = Product.query.filter_by(active=True).all()
    stock_value = sum(p.total_quantity() * p.purchase_price for p in products)
    valid_invoices = Invoice.query.filter(Invoice.status != "voided").all()
    total_sales = sum(i.total for i in valid_invoices)
    total_debt = sum(c.balance for c in Customer.query.all())

    by_method = {"cash": 0, "bank": 0, "credit": 0}
    for i in valid_invoices:
        by_method["cash"] += i.cash_amount
        by_method["bank"] += i.bank_amount
        by_method["credit"] += i.credit_amount

    returns_total = round(db.session.query(db.func.coalesce(db.func.sum(SalesReturn.total), 0)).scalar() or 0, 2)
    return render_template("reports.html", stock_value=stock_value, total_sales=total_sales,
                            returns_total=returns_total, net_sales=round(total_sales - returns_total, 2),
                            total_debt=total_debt, products=products, by_method=by_method,
                            currency=app.config["CURRENCY"])


def _financial_breakdown(invoices, entries):
    """يحسب تفصيل الحركة المالية (نقد/بنك) لمجموعة فواتير وحركات حسابات معطاة."""
    b = {
        "cash_sales": 0.0, "bank_sales": 0.0, "credit_sales": 0.0,
        "collected_cash": 0.0, "collected_bank": 0.0, "collected_unspecified": 0.0,
        "income_cash": 0.0, "income_bank": 0.0, "income_unspecified": 0.0,
        "expense_cash": 0.0, "expense_bank": 0.0, "expense_unspecified": 0.0,
        "supplier_cash": 0.0, "supplier_bank": 0.0, "supplier_unspecified": 0.0,
        "employee_advance_cash": 0.0, "employee_advance_bank": 0.0, "employee_advance_unspecified": 0.0,
        "employee_repayment_cash": 0.0, "employee_repayment_bank": 0.0, "employee_repayment_unspecified": 0.0,
        "employee_draw_cash": 0.0, "employee_draw_bank": 0.0, "employee_draw_unspecified": 0.0,
        "external_advance_cash": 0.0, "external_advance_bank": 0.0, "external_advance_unspecified": 0.0,
        "external_repayment_cash": 0.0, "external_repayment_bank": 0.0, "external_repayment_unspecified": 0.0,
        "salary_payment_cash": 0.0, "salary_payment_bank": 0.0, "salary_payment_unspecified": 0.0,
        "owner_withdrawal_cash": 0.0, "owner_withdrawal_bank": 0.0, "owner_withdrawal_unspecified": 0.0,
        "sales_return_cash": 0.0, "sales_return_bank": 0.0, "sales_return_unspecified": 0.0,
        "loan_received_cash": 0.0, "loan_received_bank": 0.0, "loan_received_unspecified": 0.0,
        "loan_repayment_cash": 0.0, "loan_repayment_bank": 0.0, "loan_repayment_unspecified": 0.0,
        "purchase_cost_cash": 0.0, "purchase_cost_bank": 0.0, "purchase_cost_unspecified": 0.0,
    }
    for i in invoices:
        b["cash_sales"] += i.cash_amount or 0
        b["bank_sales"] += i.bank_amount or 0
        b["credit_sales"] += i.credit_amount or 0
    for e in entries:
        method = e.payment_method or "unspecified"
        suffix = {"cash": "cash", "bank": "bank"}.get(method, "unspecified")
        if e.entry_type == "customer_payment":
            b[f"collected_{suffix}"] += e.amount
        elif e.entry_type == "income":
            b[f"income_{suffix}"] += e.amount
        elif e.entry_type == "expense":
            b[f"expense_{suffix}"] += e.amount
        elif e.entry_type == "salary_payment":
            b[f"salary_payment_{suffix}"] += e.amount
        elif e.entry_type == "supplier_payment":
            b[f"supplier_{suffix}"] += e.amount
        elif e.entry_type == "employee_advance":
            b[f"employee_advance_{suffix}"] += e.amount
        elif e.entry_type == "employee_repayment":
            b[f"employee_repayment_{suffix}"] += e.amount
        elif e.entry_type == "employee_draw":
            b[f"employee_draw_{suffix}"] += e.amount
        elif e.entry_type == "external_advance":
            b[f"external_advance_{suffix}"] += e.amount
        elif e.entry_type == "external_repayment":
            b[f"external_repayment_{suffix}"] += e.amount
        elif e.entry_type == "owner_withdrawal":
            b[f"owner_withdrawal_{suffix}"] += e.amount
        elif e.entry_type == "sales_return":
            b[f"sales_return_{suffix}"] += e.amount
        elif e.entry_type == "loan_received":
            b[f"loan_received_{suffix}"] += e.amount
        elif e.entry_type == "loan_repayment":
            b[f"loan_repayment_{suffix}"] += e.amount
        elif e.entry_type == "purchase_cost":
            b[f"purchase_cost_{suffix}"] += e.amount
    # net_cash/net_bank هي حركة النقدية الفعلية فقط (Cash Flow) وليست الربح - لذلك تشمل السلف
    # (لموظفين أو لأشخاص من خارج النظام) وسحوبات المالك (تخرج نقدًا فعليًا) رغم أنها لا تُحتسب
    # ضمن الربح الصافي في calculate_net_profit.
    # دفع الراتب (salary_payment) يُحسب هنا بكامل مبلغه الإجمالي كخروج نقدية، وأي خصم سلفة مرتبط
    # به يُسجَّل كقيد "تسديد سلفة موظف" (employee_repayment) منفصل بنفس طريقة الدفع، فيعوّض الفرق
    # تلقائيًا هنا ليصبح صافي الأثر على الصندوق = الراتب الإجمالي - السلفة المخصومة (الصافي الفعلي
    # المدفوع)، دون الحاجة لأي معادلة خاصة.
    # القرض الذي يأخذه المحل نقدية داخلة فعلًا، وتسديده نقدية خارجة (كانا غير محسوبين هنا)
    b["net_cash"] = round(b["cash_sales"] + b["collected_cash"] + b["income_cash"]
                           + b["employee_repayment_cash"] + b["external_repayment_cash"]
                           + b["loan_received_cash"] - b["loan_repayment_cash"]
                           - b["expense_cash"] - b["supplier_cash"] - b["salary_payment_cash"]
                           - b["employee_advance_cash"] - b["employee_draw_cash"] - b["external_advance_cash"]
                           - b["owner_withdrawal_cash"] - b["sales_return_cash"] - b["purchase_cost_cash"], 2)
    b["net_bank"] = round(b["bank_sales"] + b["collected_bank"] + b["income_bank"]
                           + b["employee_repayment_bank"] + b["external_repayment_bank"]
                           + b["loan_received_bank"] - b["loan_repayment_bank"]
                           - b["expense_bank"] - b["supplier_bank"] - b["salary_payment_bank"]
                           - b["employee_advance_bank"] - b["employee_draw_bank"] - b["external_advance_bank"]
                           - b["owner_withdrawal_bank"] - b["sales_return_bank"] - b["purchase_cost_bank"], 2)
    for k in list(b.keys()):
        if k not in ("net_cash", "net_bank"):
            b[k] = round(b[k], 2)
    return b


def calculate_net_profit(invoices, entries, sales_returns=None):
    """يحسب الربح الصافي الحقيقي (قائمة الدخل) لمجموعة فواتير وحركات حسابات معطاة:
    الربح الصافي = المبيعات - الخصومات - قيمة المرتجعات - تكلفة البضاعة المباعة الصافية (بعد
    خصم تكلفة المرتجعات) - المصروفات التشغيلية الفعلية. السلف وسحوبات المالك والقروض مستبعدة
    عمدًا لأنها بنود ميزانية عمومية وليست ربحًا أو خسارة.
    ملاحظة: تكلفة البضاعة = تكلفة القطعة المحفوظة في سطر الفاتورة لحظة البيع (متوسط مرجّح وقتها)."""
    sales_returns = sales_returns or []
    total_sales = round(sum(i.total for i in invoices), 2)
    discounts = round(sum(e.amount for e in entries if e.entry_type == "customer_discount"), 2)
    returns_value = round(sum(sr.total for sr in sales_returns), 2)
    net_sales = round(total_sales - discounts - returns_value, 2)

    # التكلفة = تكلفة كل قطعة لحظة بيعها (invoice_items.unit_cost)، حتى لا يتغير ربح فترة سابقة
    # إذا تغيّرت تكلفة المنتج لاحقًا (شراء جديد بسعر مختلف، أو تصحيح تكلفة).
    cogs = 0.0
    for inv in invoices:
        for item in inv.items:
            cogs += (item.quantity or 0) * item.cost_each()
    returns_cogs = 0.0
    for sr in sales_returns:
        for ri in sr.items:
            returns_cogs += (ri.quantity or 0) * ri.invoice_item.cost_each()
    cogs = round(cogs - returns_cogs, 2)

    operating_expenses = round(
        sum(e.amount for e in entries if e.entry_type in ("expense", "salary_payment", "inventory_loss")), 2
    )
    gross_profit = round(net_sales - cogs, 2)
    net_profit = round(gross_profit - operating_expenses, 2)
    return {
        "total_sales": total_sales, "discounts": discounts, "returns_value": returns_value,
        "net_sales": net_sales, "cogs": cogs, "gross_profit": gross_profit,
        "operating_expenses": operating_expenses, "net_profit": net_profit,
    }


def _report_period_args():
    """start/end من الرابط (نفس صيغة التقرير المالي) + نص التاريخ كما أُدخل لإعادة عرضه في الفلتر."""
    return (_parse_date(request.args.get("start")), _parse_date(request.args.get("end"), end=True),
            request.args.get("start", ""), request.args.get("end", ""))


@app.route("/reports/owner-withdrawals")
@login_required
@permission_required("financial_report")
def owner_withdrawals_report():
    """تقرير تفصيلي بكل سحبات المالك: وقت كل سحبة ومبلغها وطريقتها والخزينة ومن سجّلها (بالفترة المحددة)."""
    start, end, start_raw, end_raw = _report_period_args()
    method = request.args.get("method", "")  # cash / bank / none / '' (الكل)
    q = AccountEntry.query.filter_by(entry_type="owner_withdrawal")
    if start:
        q = q.filter(AccountEntry.date >= start)
    if end:
        q = q.filter(AccountEntry.date <= end)
    if method in ("cash", "bank"):
        q = q.filter(AccountEntry.payment_method == method)
    elif method == "none":
        q = q.filter(db.or_(AccountEntry.payment_method.is_(None), AccountEntry.payment_method == ""))
    entries = q.order_by(AccountEntry.date).all()
    boxes = {c.id: c.name for c in Cashbox.query.all()}
    users = {u.id: (u.full_name or u.username) for u in User.query.all()}  # AccountEntry ليس فيه علاقة user
    totals = {"cash": 0.0, "bank": 0.0, "none": 0.0}
    for e in entries:
        totals[e.payment_method if e.payment_method in ("cash", "bank") else "none"] += e.amount or 0
    return render_template("owner_withdrawals_report.html", entries=entries, boxes=boxes, users=users, totals=totals,
                           total=round(sum(totals.values()), 2), start=start_raw, end=end_raw, method=method,
                           currency=app.config["CURRENCY"], printed_at=datetime.now())


@app.route("/reports/bank-sales")
@login_required
@permission_required("financial_report")
def bank_sales_report():
    """تقرير تفصيلي بكل فاتورة بيع فيها مبلغ بنكي (دفع بنكي كامل أو مزدوج): الوقت ورقم الفاتورة والعميل
    والمبلغ البنكي ووسيلة التحويل والكاشير. غير المُلغاة فقط."""
    start, end, start_raw, end_raw = _report_period_args()
    shift_id = request.args.get("shift_id", type=int)
    q = Invoice.query.filter(Invoice.status != "voided", Invoice.bank_amount > 0)
    if start:
        q = q.filter(Invoice.date >= start)
    if end:
        q = q.filter(Invoice.date <= end)
    if shift_id:
        q = q.filter(Invoice.shift_id == shift_id)
    invoices = q.order_by(Invoice.date).all()
    return render_template("bank_sales_report.html", invoices=invoices,
                           total_bank=round(sum(i.bank_amount or 0 for i in invoices), 2),
                           total_invoices=round(sum(i.total or 0 for i in invoices), 2),
                           start=start_raw, end=end_raw, shift_id=shift_id,
                           currency=app.config["CURRENCY"], printed_at=datetime.now())


# ---------------------------------------------------------------------------
# تقرير أداء المنتجات: الأكثر/الأقل مبيعًا حسب المنتج أو الموديل أو المقاس أو اللون أو التصنيف
# ---------------------------------------------------------------------------
PERF_GROUPS = {"product": "المنتج", "model": "الموديل (الاسم)", "size": "المقاس", "color": "اللون", "category": "التصنيف"}
PERF_SORTS = {"qty": "الكمية", "revenue": "المبيعات", "profit": "الربح"}


def _clean_attr(v):
    """منتجات قديمة خُزّن فيها المقاس/اللون كنص "None" أو بمسافات، فكانت تظهر كمجموعة مستقلة."""
    v = (v or "").strip()
    return "" if v.lower() in ("none", "null", "-") else v


def _perf_group_key(p, group):
    if group == "model":
        return (p.name or "").strip()
    if group == "size":
        return _clean_attr(p.size) or "بدون مقاس"
    if group == "color":
        return _clean_attr(p.color) or "بدون لون"
    if group == "category":
        return p.category.name if p.category else "بدون تصنيف"
    return p.id


def _product_performance(start, end, group="product", scope_cashbox=None):
    """أداء المنتجات في فترة: الكمية والمبيعات والربح بعد خصم المرتجعات.
    - إيراد السطر يُوزَّع عليه خصم الفاتورة بالنسبة (total/subtotal)، فمجموع المبيعات = مجموع الفواتير.
    - التكلفة = تكلفة القطعة وقت البيع (cost_each)، نفس قاعدة التقرير المالي.
    - المرتجعات تُحسب بتاريخ الإرجاع وبقيمتها المسجّلة (sales_return_items.total)، مثل calculate_net_profit.
    يعيد (rows, totals, slow) حيث slow = منتجات عليها مخزون ولم يُبع منها شيء صافٍ في الفترة."""
    products = {p.id: p for p in Product.query.all()}
    stock = dict(db.session.query(Stock.product_id, db.func.coalesce(db.func.sum(Stock.quantity), 0))
                 .group_by(Stock.product_id).all())

    acc = {}  # product_id -> أرقام خام

    def bucket(pid):
        return acc.setdefault(pid, {"sold": 0, "returned": 0, "revenue": 0.0, "cost": 0.0, "invoices": set()})

    sales_q = (db.session.query(InvoiceItem.product_id, InvoiceItem.quantity, InvoiceItem.total, InvoiceItem.unit_cost,
                                Invoice.id, Invoice.total, Invoice.subtotal)
               .join(Invoice, InvoiceItem.invoice_id == Invoice.id).filter(Invoice.status != "voided"))
    ret_q = (db.session.query(InvoiceItem.product_id, SalesReturnItem.quantity, SalesReturnItem.total, InvoiceItem.unit_cost)
             .join(InvoiceItem, SalesReturnItem.invoice_item_id == InvoiceItem.id)
             .join(SalesReturn, SalesReturnItem.sales_return_id == SalesReturn.id))
    if start:
        sales_q = sales_q.filter(Invoice.date >= start)
        ret_q = ret_q.filter(SalesReturn.date >= start)
    if end:
        sales_q = sales_q.filter(Invoice.date <= end)
        ret_q = ret_q.filter(SalesReturn.date <= end)
    if scope_cashbox:
        sales_q = sales_q.join(Shift, Invoice.shift_id == Shift.id).filter(Shift.cashbox_id == scope_cashbox)
        ret_q = ret_q.filter(SalesReturn.cashbox_id == scope_cashbox)

    def unit_cost(pid, uc):
        if uc is not None:
            return uc
        p = products.get(pid)
        return (p.purchase_price or 0) if p else 0

    for pid, qty, line_total, uc, inv_id, inv_total, inv_sub in sales_q.all():
        b = bucket(pid)
        ratio = (inv_total / inv_sub) if inv_sub else 1.0
        b["sold"] += qty or 0
        b["revenue"] += (line_total or 0) * ratio
        b["cost"] += (qty or 0) * unit_cost(pid, uc)
        b["invoices"].add(inv_id)
    for pid, qty, ret_total, uc in ret_q.all():
        b = bucket(pid)
        b["returned"] += qty or 0
        b["revenue"] -= ret_total or 0
        b["cost"] -= (qty or 0) * unit_cost(pid, uc)

    days = max(((end or datetime.utcnow()) - (start or min(
        [p.created_at for p in products.values() if p.created_at] or [datetime.utcnow()]))).days, 1)

    groups = {}
    for pid, p in products.items():
        if pid not in acc and not (p.active and stock.get(pid, 0) > 0):
            continue  # منتج محذوف بلا مبيعات في الفترة
        key = _perf_group_key(p, group)
        g = groups.setdefault(key, {"key": key, "label": "", "sold": 0, "returned": 0, "revenue": 0.0, "cost": 0.0,
                                    "stock": 0, "stock_value": 0.0, "invoices": set(), "products": 0})
        if group == "product":
            extra = " / ".join(x for x in (_clean_attr(p.size), _clean_attr(p.color)) if x)
            g["label"] = (p.name or "").strip() + (f" ({extra})" if extra else "")
            g["barcode"] = p.barcode or ""
            g["product_id"] = p.id
        else:
            g["label"] = str(key)
        a = acc.get(pid)
        if a:
            g["sold"] += a["sold"]
            g["returned"] += a["returned"]
            g["revenue"] += a["revenue"]
            g["cost"] += a["cost"]
            g["invoices"] |= a["invoices"]
        if p.active:
            q = stock.get(pid, 0)
            g["stock"] += q
            g["stock_value"] += max(q, 0) * (p.purchase_price or 0)
            g["products"] += 1

    total_revenue = sum(g["revenue"] for g in groups.values())
    rows = []
    for g in groups.values():
        net_qty = g["sold"] - g["returned"]
        revenue, cost = round(g["revenue"], 2), round(g["cost"], 2)
        profit = round(revenue - cost, 2)
        daily = net_qty / days
        rows.append({
            **{k: v for k, v in g.items() if k != "invoices"},
            "invoices": len(g["invoices"]), "net_qty": net_qty, "revenue": revenue, "cost": cost, "profit": profit,
            "margin": round(profit / revenue * 100, 1) if revenue > 0 else None,
            "share": round(revenue / total_revenue * 100, 1) if total_revenue > 0 else 0,
            "return_rate": round(g["returned"] / g["sold"] * 100, 1) if g["sold"] else 0,
            # كم يومًا يكفي المخزون الحالي بنفس معدل بيع الفترة
            "cover_days": round(g["stock"] / daily) if daily > 0 and g["stock"] > 0 else None,
            "stock_value": round(g["stock_value"], 2),
        })

    sold_rows = [r for r in rows if r["sold"] or r["returned"]]
    totals = {
        "net_qty": sum(r["net_qty"] for r in sold_rows), "returned": sum(r["returned"] for r in sold_rows),
        "revenue": round(sum(r["revenue"] for r in sold_rows), 2), "cost": round(sum(r["cost"] for r in sold_rows), 2),
        "profit": round(sum(r["profit"] for r in sold_rows), 2), "groups_sold": sum(1 for r in sold_rows if r["net_qty"] > 0),
        "days": days,
    }
    totals["margin"] = round(totals["profit"] / totals["revenue"] * 100, 1) if totals["revenue"] > 0 else None
    slow = sorted((r for r in rows if r["net_qty"] <= 0 and r["stock"] > 0), key=lambda r: -r["stock_value"])
    return sold_rows, totals, slow


def _perf_period():
    """الفترة من الرابط، وافتراضيًا آخر 30 يومًا بتوقيت المحل."""
    start_raw, end_raw = request.args.get("start", ""), request.args.get("end", "")
    if not start_raw and not end_raw and not request.args.get("all"):
        today_local = to_local(datetime.utcnow()).date()
        start_raw, end_raw = (today_local - timedelta(days=29)).isoformat(), today_local.isoformat()
    return _parse_date(start_raw), _parse_date(end_raw, end=True), start_raw, end_raw


def _perf_args():
    group = request.args.get("group", "product")
    sort = request.args.get("sort", "qty")
    order = request.args.get("order", "best")
    limit = request.args.get("limit", 30, type=int)
    return (group if group in PERF_GROUPS else "product", sort if sort in PERF_SORTS else "qty",
            order if order in ("best", "least") else "best", limit if limit in (10, 30, 50, 0) else 30)


def _perf_sorted(rows, sort, order):
    field = {"qty": "net_qty", "revenue": "revenue", "profit": "profit"}[sort]
    return sorted(rows, key=lambda r: (r[field], r["revenue"]), reverse=(order == "best"))


@app.route("/reports/top-products")
@login_required
@permission_required("reports")
def top_products_report():
    start, end, start_raw, end_raw = _perf_period()
    group, sort, order, limit = _perf_args()
    rows, totals, slow = _product_performance(start, end, group)
    rows = _perf_sorted(rows, sort, order)
    if order == "least":
        rows = [r for r in rows if r["net_qty"] > 0]  # "الأقل مبيعًا" = ما بيع منه قليل؛ ما لم يُبع في جدول الراكد
    shown = rows[:limit] if limit else rows
    return render_template("top_products_report.html", rows=shown, all_count=len(rows), totals=totals,
                           slow=slow[:30], slow_count=len(slow), slow_value=round(sum(r["stock_value"] for r in slow), 2),
                           group=group, sort=sort, order=order, limit=limit, groups=PERF_GROUPS, sorts=PERF_SORTS,
                           start=start_raw, end=end_raw, show_all=bool(request.args.get("all")),
                           can_ai=current_user.has_permission("ai_assistant"),
                           currency=app.config["CURRENCY"], printed_at=datetime.now())


def _perf_text_for_ai(start_raw, end_raw):
    """ملخص نصي محسوب مسبقًا لأداء المنتجات (كل التجميعات) ليحلله الذكاء الاصطناعي دون أن يحسب بنفسه."""
    start, end = _parse_date(start_raw), _parse_date(end_raw, end=True)
    out = []
    by_product, totals, slow = _product_performance(start, end, "product")
    out.append(f"الفترة: {start_raw or 'من البداية'} إلى {end_raw or 'اليوم'} ({totals['days']} يوم)")
    out.append(f"الإجمالي: صافي القطع {totals['net_qty']}، المرتجع {totals['returned']}، صافي المبيعات {totals['revenue']:.2f}، "
               f"التكلفة {totals['cost']:.2f}، الربح {totals['profit']:.2f}، هامش الربح {totals['margin'] if totals['margin'] is not None else '-'}%")

    def fmt(r):
        # الكود يميّز المنتجات التي تحمل نفس الاسم (مثلاً عدة "jacket")
        label = r["label"] + (f" [كود {r['barcode']}]" if r.get("barcode") else "")
        return (f"- {label}: صافي {r['net_qty']} قطعة، مبيعات {r['revenue']:.2f}، ربح {r['profit']:.2f}، "
                f"هامش {r['margin'] if r['margin'] is not None else '-'}%، حصة {r['share']}%، مرتجع {r['return_rate']}%، "
                f"مخزون {r['stock']}" + (f"، يكفي ~{r['cover_days']} يوم" if r["cover_days"] else ""))

    out.append("أفضل 15 منتجًا بالكمية:")
    out += [fmt(r) for r in _perf_sorted(by_product, "qty", "best")[:15]]
    out.append("أفضل 10 منتجات بالربح:")
    out += [fmt(r) for r in _perf_sorted(by_product, "profit", "best")[:10]]
    least = [r for r in _perf_sorted(by_product, "qty", "least") if r["net_qty"] > 0][:10]
    if least:
        out.append("الأقل مبيعًا (بيع منها شيء):")
        out += [fmt(r) for r in least]
    low_margin = sorted((r for r in by_product if r["margin"] is not None and r["revenue"] > 0), key=lambda r: r["margin"])[:5]
    if low_margin:
        out.append("أقل هامش ربح:")
        out += [fmt(r) for r in low_margin]
    for g in ("model", "size", "color", "category"):
        rows, _t, _s = _product_performance(start, end, g)
        rows = _perf_sorted(rows, "qty", "best")[:10]
        if rows:
            out.append(f"حسب {PERF_GROUPS[g]}:")
            out += [fmt(r) for r in rows]
    out.append(f"منتجات راكدة (عليها مخزون ولم يُبع منها شيء صافٍ): {len(slow)}، قيمتها بالتكلفة "
               f"{sum(r['stock_value'] for r in slow):.2f}")
    out += [f"- {r['label']}: مخزون {r['stock']}، قيمة {r['stock_value']:.2f}" for r in slow[:10]]
    return "\n".join(out)


AI_PERF_INSTRUCTIONS = """أنت محلل مبيعات لمحل ملابس. حلّل أداء المنتجات من الأرقام المعطاة فقط، واكتب تقريرًا قصيرًا بالعربية
بلغة بسيطة يفهمها صاحب المحل، بهذه الأقسام (عناوين قصيرة بخط عريض **...**):
1. **الخلاصة**: سطران عن أهم ما في الفترة.
2. **الأفضل أداءً**: أهم المنتجات/المقاسات/الألوان التي تبيع وتربح، ولماذا تهمّ (بالأرقام).
3. **تحتاج انتباه**: منتجات قليلة البيع أو هامشها ضعيف أو مرتجعها عالٍ، والبضاعة الراكدة وقيمتها.
4. **توصيات عملية**: 3 إلى 6 خطوات محددة (ماذا يُعاد طلبه وبأي أولوية حسب أيام كفاية المخزون، ماذا يُعرض
   بتخفيض، ماذا يُوقف شراؤه، أسعار تحتاج مراجعة). كل توصية مربوطة برقم من المعطيات.
لا تخترع أرقامًا ولا تحسب نسبًا غير موجودة. اذكر العملة مع المبالغ. إن كانت البيانات قليلة جدًا قل ذلك بوضوح."""


@app.route("/reports/top-products/analyze", methods=["POST"])
@login_required
@permission_required("reports")
@permission_required("ai_assistant")
def top_products_analyze():
    s = get_settings()
    if not s["gemini_api_key"] and not s["claude_api_key"]:
        return jsonify({"error": "مساعد الذكاء الاصطناعي غير مُفعَّل - أضف مفتاح Gemini أو Claude من صفحة إعدادات النظام."}), 400
    data = request.get_json(silent=True) or {}
    start_raw, end_raw = (data.get("start") or "").strip(), (data.get("end") or "").strip()
    focus = (data.get("question") or "").strip()[:300]
    summary = _perf_text_for_ai(start_raw, end_raw)
    prompt = f"=== أرقام أداء المنتجات (محسوبة بدقة من النظام) ===\n{summary}\n"
    if focus:
        prompt += f"\n=== سؤال إضافي من صاحب المحل ===\n{focus}\n"
    answer, error = _ai_generate(AI_ASSISTANT_SYSTEM_PROMPT + "\n\n" + AI_PERF_INSTRUCTIONS, prompt)
    if error:
        return jsonify({"error": error}), 502
    log_action("ai_analyze_products", "ai_assistant", details=f"{start_raw} → {end_raw} {focus[:150]}")
    db.session.commit()
    return jsonify({"answer": answer})


@app.route("/reports/top-products/export/excel")
@login_required
@permission_required("reports")
def top_products_excel():
    from openpyxl import Workbook
    from openpyxl.styles import Font
    start, end, start_raw, end_raw = _perf_period()
    group, sort, order, limit = _perf_args()
    rows, totals, slow = _product_performance(start, end, group)
    rows = _perf_sorted(rows, sort, order)
    wb = Workbook()
    ws = wb.active
    ws.title = "أداء المنتجات"
    ws.sheet_view.rightToLeft = True
    ws.append([f"أداء المنتجات حسب {PERF_GROUPS[group]} - الفترة {start_raw or 'من البداية'} إلى {end_raw or 'اليوم'}"])
    # في التجميع حسب المنتج: عمود الباركود بجانب الاسم (يميّز المنتجات متشابهة الاسم)
    bc = (lambda r: [r.get("barcode", "")]) if group == "product" else (lambda r: [])
    bc_head = ["الباركود"] if group == "product" else []
    ws.append([PERF_GROUPS[group]] + bc_head + ["صافي الكمية", "المُرجَع", "صافي المبيعات", "التكلفة", "الربح", "هامش %",
                                                "الحصة %", "المخزون الحالي", "يكفي (يوم)"])
    for c in ws[2]:
        c.font = Font(bold=True)
    for r in rows:
        ws.append([r["label"]] + bc(r) + [r["net_qty"], r["returned"], r["revenue"], r["cost"], r["profit"], r["margin"],
                                          r["share"], r["stock"], r["cover_days"]])
    ws.append(["الإجمالي"] + [""] * len(bc_head) + [totals["net_qty"], totals["returned"], totals["revenue"],
                                                     totals["cost"], totals["profit"], totals["margin"]])
    ws2 = wb.create_sheet("بضاعة راكدة")
    ws2.sheet_view.rightToLeft = True
    ws2.append([PERF_GROUPS[group]] + bc_head + ["المخزون", "قيمة المخزون بالتكلفة"])
    for r in slow:
        ws2.append([r["label"]] + bc(r) + [r["stock"], r["stock_value"]])
    ws.column_dimensions["A"].width = 38
    ws2.column_dimensions["A"].width = 38
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    log_action("export_top_products_excel", entity_type="report", details=f"{group} {start_raw}→{end_raw}")
    db.session.commit()
    return send_file(buf, as_attachment=True, download_name=f"top_products_{datetime.now():%Y%m%d_%H%M}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/reports/financial")
@login_required
@permission_required("financial_report")
def financial_report():
    start = _parse_date(request.args.get("start"))
    end = _parse_date(request.args.get("end"), end=True)
    shift_id = request.args.get("shift_id", type=int)
    payment_method = request.args.get("payment_method", "")  # cash / bank / '' (الكل)

    invoices_q = Invoice.query.filter(Invoice.status != "voided")
    entries_q = AccountEntry.query
    if start:
        invoices_q = invoices_q.filter(Invoice.date >= start)
        entries_q = entries_q.filter(AccountEntry.date >= start)
    if end:
        invoices_q = invoices_q.filter(Invoice.date <= end)
        entries_q = entries_q.filter(AccountEntry.date <= end)
    if shift_id:
        invoices_q = invoices_q.filter(Invoice.shift_id == shift_id)
    if payment_method in ("cash", "bank"):
        entries_q = entries_q.filter(AccountEntry.payment_method == payment_method)

    invoices = invoices_q.order_by(Invoice.date).all()
    if payment_method == "cash":
        filtered_invoices = [i for i in invoices if (i.cash_amount or 0) > 0]
    elif payment_method == "bank":
        filtered_invoices = [i for i in invoices if (i.bank_amount or 0) > 0]
    else:
        filtered_invoices = invoices
    entries = entries_q.order_by(AccountEntry.date).all()

    sales_returns_q = SalesReturn.query
    if start:
        sales_returns_q = sales_returns_q.filter(SalesReturn.date >= start)
    if end:
        sales_returns_q = sales_returns_q.filter(SalesReturn.date <= end)
    if shift_id:
        sales_returns_q = sales_returns_q.filter(SalesReturn.shift_id == shift_id)
    sales_returns = sales_returns_q.all()

    period = _financial_breakdown(filtered_invoices, entries)
    profit = calculate_net_profit(filtered_invoices, entries, sales_returns)

    # الرصيد التراكمي النهائي حتى تاريخ نهاية الفترة (أو الآن) منذ بداية السجلات
    balance_end = end or datetime.utcnow()
    all_invoices = Invoice.query.filter(Invoice.status != "voided", Invoice.date <= balance_end).all()
    all_entries = AccountEntry.query.filter(AccountEntry.date <= balance_end).all()
    final_balance = _financial_breakdown(all_invoices, all_entries)

    # حركات مرتبطة (فواتير + حركات حسابات) لعرضها بالتفصيل مع روابطها
    transactions = sorted(
        [{"date": i.date, "type": "فاتورة مبيعات", "ref": i.invoice_number, "invoice_id": i.id,
          "method": i.payment_label(), "amount": i.total} for i in filtered_invoices] +
        [{"date": e.date, "type": {"income": "إيراد", "expense": "مصروف", "customer_payment": "تحصيل من عميل",
                                    "customer_debt": "دين يدوي", "supplier_payment": "دفع لمورد", "employee_advance": "سلفة موظف", "employee_repayment": "تسديد سلفة موظف", "employee_draw": "سحب على الحساب (موظف)", "external_advance": "سلفة لشخص خارجي", "external_repayment": "تسديد سلفة شخص خارجي", "salary_payment": "دفع راتب", "customer_discount": "خصم عميل", "inventory_loss": "خسارة مخزون", "loan_received": "قرض/سلفة من جهة خارجية", "loan_repayment": "تسديد قرض لجهة خارجية", "sales_return": "إرجاع فاتورة بيع", "owner_withdrawal": "سحب مالك", "purchase_cost": "مصاريف شراء (شحن/جمارك)"}.get(e.entry_type, e.entry_type),
          "ref": e.note or (e.employee.name if e.employee else (e.external_borrower.name if e.external_borrower else "-")), "invoice_id": e.invoice_id, "method": ("نقدي" if e.payment_method == "cash" else ("بنكي" if e.payment_method == "bank" else "-")),
          "amount": e.amount} for e in entries],
        key=lambda x: x["date"], reverse=True
    )

    expenses_detail = [e for e in entries if e.entry_type in ("expense", "salary_payment")]

    return render_template("financial_report.html", period=period, final_balance=final_balance, profit=profit,
                            transactions=transactions, expenses_detail=expenses_detail,
                            currency=app.config["CURRENCY"], shifts=Shift.query.order_by(Shift.opened_at.desc()).limit(100).all(),
                            start=request.args.get("start", ""), end=request.args.get("end", ""),
                            shift_id=shift_id, payment_method=payment_method)


@app.route("/reports/financial/export/excel")
@login_required
@permission_required("financial_report")
def export_financial_excel_route():
    from export_service import export_generic_excel
    headers = ["التاريخ", "النوع", "المرجع", "طريقة الدفع", "المبلغ"]
    rows = []
    for t in _financial_transactions_for_export():
        rows.append([to_local(t["date"]).strftime("%Y-%m-%d %H:%M"), t["type"], t["ref"], t["method"], round(t["amount"], 2)])
    buf = export_generic_excel("التقرير المالي", headers, rows)
    log_action("export_financial_excel", entity_type="report")
    db.session.commit()
    filename = f"financial_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return send_file(buf, as_attachment=True, download_name=filename,
                      mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/reports/financial/export/pdf")
@login_required
@permission_required("financial_report")
def export_financial_pdf_route():
    from export_service import export_generic_pdf
    headers = ["التاريخ", "النوع", "المرجع", "طريقة الدفع", "المبلغ"]
    rows = []
    for t in _financial_transactions_for_export():
        rows.append([to_local(t["date"]).strftime("%Y-%m-%d %H:%M"), t["type"], t["ref"], t["method"], round(t["amount"], 2)])
    buf = export_generic_pdf("التقرير المالي", headers, rows, company=app.config["COMPANY_NAME"])
    log_action("export_financial_pdf", entity_type="report")
    db.session.commit()
    filename = f"financial_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    return send_file(buf, as_attachment=True, download_name=filename, mimetype="application/pdf")


def _financial_transactions_for_export():
    """يعيد بناء نفس قائمة الحركات المفلترة الحالية من معطيات الطلب (query string) لتصديرها."""
    start = _parse_date(request.args.get("start"))
    end = _parse_date(request.args.get("end"), end=True)
    shift_id = request.args.get("shift_id", type=int)
    payment_method = request.args.get("payment_method", "")

    invoices_q = Invoice.query.filter(Invoice.status != "voided")
    entries_q = AccountEntry.query
    if start:
        invoices_q = invoices_q.filter(Invoice.date >= start)
        entries_q = entries_q.filter(AccountEntry.date >= start)
    if end:
        invoices_q = invoices_q.filter(Invoice.date <= end)
        entries_q = entries_q.filter(AccountEntry.date <= end)
    if shift_id:
        invoices_q = invoices_q.filter(Invoice.shift_id == shift_id)
    if payment_method in ("cash", "bank"):
        entries_q = entries_q.filter(AccountEntry.payment_method == payment_method)

    invoices = invoices_q.order_by(Invoice.date).all()
    if payment_method == "cash":
        filtered_invoices = [i for i in invoices if (i.cash_amount or 0) > 0]
    elif payment_method == "bank":
        filtered_invoices = [i for i in invoices if (i.bank_amount or 0) > 0]
    else:
        filtered_invoices = invoices
    entries = entries_q.order_by(AccountEntry.date).all()

    transactions = sorted(
        [{"date": i.date, "type": "فاتورة مبيعات", "ref": i.invoice_number,
          "method": i.payment_label(), "amount": i.total} for i in filtered_invoices] +
        [{"date": e.date, "type": {"income": "إيراد", "expense": "مصروف", "customer_payment": "تحصيل من عميل",
                                    "customer_debt": "دين يدوي", "supplier_payment": "دفع لمورد", "employee_advance": "سلفة موظف", "employee_repayment": "تسديد سلفة موظف", "employee_draw": "سحب على الحساب (موظف)", "external_advance": "سلفة لشخص خارجي", "external_repayment": "تسديد سلفة شخص خارجي", "salary_payment": "دفع راتب", "customer_discount": "خصم عميل", "inventory_loss": "خسارة مخزون", "loan_received": "قرض/سلفة من جهة خارجية", "loan_repayment": "تسديد قرض لجهة خارجية", "sales_return": "إرجاع فاتورة بيع", "owner_withdrawal": "سحب مالك", "purchase_cost": "مصاريف شراء (شحن/جمارك)"}.get(e.entry_type, e.entry_type),
          "ref": e.note or (e.employee.name if e.employee else (e.external_borrower.name if e.external_borrower else "-")), "method": ("نقدي" if e.payment_method == "cash" else ("بنكي" if e.payment_method == "bank" else "-")),
          "amount": e.amount} for e in entries],
        key=lambda x: x["date"], reverse=True
    )
    return transactions


# ---------------------------------------------------------------------------
# تصدير تقرير الجرد (Excel / PDF)
# ---------------------------------------------------------------------------
@app.route("/reports/export/excel")
@login_required
@permission_required("reports")
def export_inventory_excel_route():
    from export_service import export_inventory_excel
    products = Product.query.filter_by(active=True).order_by(Product.name).all()
    buf = export_inventory_excel(products, currency=app.config["CURRENCY"])
    log_action("export_inventory_excel", entity_type="report", details=f"{len(products)} منتج")
    db.session.commit()
    filename = f"inventory_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return send_file(buf, as_attachment=True, download_name=filename,
                      mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/reports/export/pdf")
@login_required
@permission_required("reports")
def export_inventory_pdf_route():
    from export_service import export_inventory_pdf
    products = Product.query.filter_by(active=True).order_by(Product.name).all()
    buf = export_inventory_pdf(products, company=app.config["COMPANY_NAME"], currency=app.config["CURRENCY"])
    log_action("export_inventory_pdf", entity_type="report", details=f"{len(products)} منتج")
    db.session.commit()
    filename = f"inventory_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    return send_file(buf, as_attachment=True, download_name=filename, mimetype="application/pdf")


# ---------------------------------------------------------------------------
# سجل الأنشطة (Audit Log) - للأدمن فقط
# ---------------------------------------------------------------------------
@app.route("/audit-log")
@login_required
@permission_required("audit_log")
def audit_log():
    logs = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(300).all()
    return render_template("audit_log.html", logs=logs)


# ---------------------------------------------------------------------------
# إدارة المستخدمين (للمدير فقط)
# ---------------------------------------------------------------------------


@app.route("/api/reports/employee")
@api_login_required
def api_employee_report():
    user = request.api_user
    if not user.has_permission("reports"):
        return jsonify({"error": "لا تملك صلاحية التقارير"}), 403
    start = _parse_date(request.args.get("start"))
    end = _parse_date(request.args.get("end"), end=True)
    user_id = request.args.get("user_id", type=int)
    q = Invoice.query.filter(Invoice.status == "completed")
    if start: q = q.filter(Invoice.date >= start)
    if end: q = q.filter(Invoice.date <= end)
    if user_id: q = q.filter(Invoice.user_id == user_id)
    rows=q.order_by(Invoice.date.asc()).all()
    return jsonify({
        "count": len(rows),
        "total": round(sum(i.total for i in rows),2),
        "cash": round(sum(i.cash_amount for i in rows),2),
        "bank": round(sum(i.bank_amount for i in rows),2),
        "credit": round(sum(i.credit_amount for i in rows),2),
        "invoices": [{"invoice":i.invoice_number,"date":i.date.isoformat(),
                      "employee":i.user.full_name if i.user else None,"total":i.total,
                      "cash":i.cash_amount,"bank":i.bank_amount,"credit":i.credit_amount} for i in rows]
    })

# ---------------------------------------------------------------------------
# النسخة المتقدمة: جرد قديم، خزائن متعددة، تعديل الفواتير، تقارير الموظفين
# ---------------------------------------------------------------------------
def _parse_date(value, end=False):
    if not value:
        return None
    try:
        dt = datetime.strptime(value, "%Y-%m-%d")
        dt = dt.replace(hour=23, minute=59, second=59) if end else dt.replace(hour=0, minute=0, second=0)
        return to_utc(dt)  # التاريخ الذي يكتبه المستخدم بتوقيت المحل
    except ValueError:
        return None


def apply_invoice_edit(invoice, payload):
    """يطبّق تعديل الفاتورة مع إعادة ضبط المخزون والذمم ضمن معاملة واحدة."""
    if invoice.status == "voided":
        raise ValueError("لا يمكن تعديل فاتورة ملغاة.")
    new_items = payload.get("items", [])
    if not new_items:
        raise ValueError("الفاتورة يجب أن تحتوي على صنف واحد على الأقل.")
    # فاتورة عليها إرجاع لا تُعدَّل: إعادة كمياتها كاملة كانت تضاعف المخزون المرتجع سابقًا
    if SalesReturn.query.filter_by(invoice_id=invoice.id).first():
        raise ValueError("على هذه الفاتورة إرجاع مسجّل - لا يمكن تعديلها. استخدم الإرجاع أو الاستبدال بدل التعديل.")
    warehouse_id = int(payload.get("warehouse_id") or invoice.warehouse_id)
    discount = max(float(payload.get("discount", 0) or 0), 0)
    affects_stock = invoice.stock_affected is not False
    old_costs = {it.product_id: it.unit_cost for it in invoice.items if it.unit_cost is not None}
    old_cash = invoice.cash_amount or 0

    # إعادة الكميات القديمة
    for old in invoice.items:
        if not affects_stock:
            continue
        stock = get_or_create_stock(old.product_id, invoice.warehouse_id)
        stock.quantity += old.quantity
        db.session.add(StockMovement(product_id=old.product_id, warehouse_id=invoice.warehouse_id,
                                     movement_type="in", quantity=old.quantity,
                                     reference=f"إعادة ضبط قبل تعديل {invoice.invoice_number}"))
    if invoice.credit_amount and invoice.customer_id:
        old_customer = Customer.query.get(invoice.customer_id)
        if old_customer:
            old_customer.balance -= invoice.credit_amount

    subtotal = 0
    resolved = []
    for row in new_items:
        product = Product.query.get(int(row["product_id"]))
        if not product or not product.active:
            raise ValueError("يوجد منتج غير صالح في الفاتورة.")
        qty = int(row.get("quantity", 0))
        price = float(row.get("unit_price", product.sale_price))
        if qty <= 0 or price < 0:
            raise ValueError("الكمية والسعر يجب أن يكونا صالحين.")
        resolved.append((product, qty, price))
        subtotal += qty * price

    total = round(subtotal - discount, 2)
    if total < 0:
        raise ValueError("الخصم لا يمكن أن يتجاوز قيمة الفاتورة.")

    payment_method = payload.get("payment_method", invoice.payment_method)
    cash = float(payload.get("cash_amount", 0) or 0)
    bank = float(payload.get("bank_amount", 0) or 0)
    credit = float(payload.get("credit_amount", 0) or 0)
    if payment_method == "cash": cash, bank, credit = total, 0, 0
    elif payment_method == "bank": cash, bank, credit = 0, total, 0
    elif payment_method == "credit": cash, bank, credit = 0, 0, total
    if round(cash + bank + credit, 2) != round(total, 2):
        raise ValueError("مجموع وسائل الدفع يجب أن يساوي إجمالي الفاتورة.")

    customer_id = payload.get("customer_id") or None
    if credit > 0 and not customer_id:
        raise ValueError("البيع الآجل يتطلب تحديد عميل.")
    if credit > 0:
        customer = Customer.query.get(int(customer_id))
        if not customer:
            raise ValueError("العميل غير موجود.")
        if customer.would_exceed_limit(credit):
            raise ValueError("التعديل سيتجاوز سقف ائتمان العميل.")

    # حذف البنود وإعادة إنشائها
    invoice.items[:] = []
    invoice.warehouse_id = warehouse_id
    invoice.customer_id = int(customer_id) if customer_id else None
    invoice.subtotal = subtotal
    invoice.discount = discount
    invoice.total = total
    invoice.payment_method = payment_method
    invoice.cash_amount = cash
    invoice.bank_amount = bank
    invoice.credit_amount = credit
    invoice.bank_name = payload.get("bank_name") or None
    for product, qty, price in resolved:
        if affects_stock:
            stock = get_or_create_stock(product.id, warehouse_id)
            if stock.quantity < qty:
                raise ValueError(f"الكمية غير متوفرة: {product.display_name()}")
            stock.quantity -= qty
            db.session.add(StockMovement(product_id=product.id, warehouse_id=warehouse_id,
                                         movement_type="out", quantity=qty,
                                         reference=f"تعديل {invoice.invoice_number}"))
        db.session.add(InvoiceItem(invoice_id=invoice.id, product_id=product.id,
                                   quantity=qty, unit_price=price, total=round(qty * price, 2),
                                   unit_cost=old_costs.get(product.id, product.purchase_price or 0)))
    if credit > 0:
        Customer.query.get(int(customer_id)).balance += credit
    # فرق النقد بين قبل وبعد التعديل يُرحَّل لخزينة الوردية (إن كانت الفاتورة مرحّلة نقديًا)
    if invoice.cash_posted and invoice.shift and invoice.shift.cashbox:
        box = invoice.shift.cashbox
        box.balance = round((box.balance or 0) + (cash - old_cash), 2)


@app.route("/inventory/import", methods=["GET", "POST"])
@login_required
@permission_required("inventory_import")
def inventory_import():
    if request.method == "POST":
        text = request.form.get("csv_text", "").strip()
        if not text and request.files.get("file"):
            raw = request.files["file"].read()
            text = raw.decode("utf-8-sig", errors="replace")
        if not text:
            flash("أدخل بيانات الجرد أو ارفع ملف CSV.", "warning")
            return redirect(url_for("inventory_import"))
        warehouse_id = int(request.form.get("warehouse_id") or 0)
        if not Warehouse.query.get(warehouse_id):
            flash("اختر مخزنًا صحيحًا.", "danger")
            return redirect(url_for("inventory_import"))
        # عند تفعيل هذا الخيار: يتم استيراد بيانات المنتجات (الاسم، الباركود، الأسعار...)
        # فقط دون التأثير على كمية المخزون الفعلية في المخزن (لا تُعدَّل الكمية ولا تُسجَّل حركة مخزون).
        affect_stock = request.form.get("affect_stock") == "on"
        reader = csv.DictReader(io.StringIO(text))
        required = {"barcode", "name", "quantity"}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            flash("الأعمدة المطلوبة: barcode,name,quantity. ويمكن إضافة purchase_price,sale_price,size,length,color.", "danger")
            return redirect(url_for("inventory_import"))
        added = updated = 0
        try:
            for row in reader:
                barcode = (row.get("barcode") or "").strip()
                name = (row.get("name") or "").strip()
                qty = int(float(row.get("quantity") or 0))
                if not name or qty < 0:
                    continue
                product = Product.query.filter_by(barcode=barcode).first() if barcode else None
                if not product:
                    product = Product(name=name, barcode=barcode or None,
                                      purchase_price=float(row.get("purchase_price") or 0),
                                      sale_price=float(row.get("sale_price") or 0),
                                      size=(row.get("size") or "").strip(),
                                      length=(row.get("length") or "").strip(),
                                      color=(row.get("color") or "").strip())
                    db.session.add(product); db.session.flush()
                    added += 1
                else:
                    if row.get("purchase_price"): product.purchase_price = float(row["purchase_price"])
                    if row.get("sale_price"): product.sale_price = float(row["sale_price"])
                    if "size" in row and row.get("size") is not None: product.size = row.get("size").strip()
                    if "length" in row and row.get("length") is not None: product.length = row.get("length").strip()
                    if "color" in row and row.get("color") is not None: product.color = row.get("color").strip()
                    updated += 1
                # لا نلمس المخزون الفعلي إذا كان خيار "عدم التأثير على المخزن" مفعّلاً.
                # يتم فقط التأكد من وجود سجل مخزون (بكمية 0 إن كان منتجًا جديدًا) دون تغييرها.
                if affect_stock:
                    stock = get_or_create_stock(product.id, warehouse_id)
                    delta = qty - stock.quantity
                    stock.quantity = qty
                    if delta:
                        db.session.add(StockMovement(product_id=product.id, warehouse_id=warehouse_id,
                                                     movement_type="adjustment", quantity=delta,
                                                     reference="استيراد الجرد القديم"))
                else:
                    get_or_create_stock(product.id, warehouse_id)
            mode_note = "مع تحديث المخزون" if affect_stock else "بدون التأثير على المخزون (بيانات فقط)"
            log_action("import_old_inventory", "inventory", details=f"إضافة {added}، تحديث {updated} ({mode_note})")
            db.session.commit()
            if affect_stock:
                flash(f"تم استيراد الجرد بنجاح: {added} منتجات جديدة و{updated} منتجات محدثة، وتم تحديث كمية المخزون.", "success")
            else:
                flash(f"تم استيراد بيانات الجرد بنجاح: {added} منتجات جديدة و{updated} منتجات محدثة، بدون التأثير على كمية المخزون الفعلية.", "success")
        except Exception as exc:
            db.session.rollback()
            flash(f"فشل الاستيراد: {exc}", "danger")
        return redirect(url_for("inventory_import"))
    return render_template("inventory_import.html", warehouses=Warehouse.query.all())



@app.route("/sales/old", methods=["GET", "POST"])
@login_required
@permission_required("old_sales_import")
def old_sales_import():
    """إدخال فاتورة بيع تاريخية مع تاريخها الحقيقي، مع خيار تأثيرها على المخزون."""
    if request.method == "POST":
        invoice_number = (request.form.get("invoice_number") or "").strip()
        if not invoice_number:
            invoice_number = generate_number("OLD", Invoice)
        if Invoice.query.filter_by(invoice_number=invoice_number).first():
            flash("رقم الفاتورة موجود مسبقًا. اختر رقمًا مختلفًا.", "danger")
            return redirect(url_for("old_sales_import"))
        try:
            date_value = request.form.get("invoice_date", "").strip()
            invoice_date = to_utc(datetime.strptime(date_value, "%Y-%m-%dT%H:%M")) if date_value else datetime.utcnow()
            warehouse_id = int(request.form.get("warehouse_id") or 0)
            if not Warehouse.query.get(warehouse_id):
                raise ValueError("اختر مخزنًا صحيحًا.")
            user_id = request.form.get("user_id", type=int) or current_user.id
            if not User.query.get(user_id):
                user_id = current_user.id
            customer_id = request.form.get("customer_id", type=int) or None
            if customer_id and not Customer.query.get(customer_id):
                raise ValueError("العميل المحدد غير موجود.")
            discount = max(float(request.form.get("discount") or 0), 0)
            payment_method = request.form.get("payment_method") or "cash"
            cash = float(request.form.get("cash_amount") or 0)
            bank = float(request.form.get("bank_amount") or 0)
            credit = float(request.form.get("credit_amount") or 0)
            affect_stock = request.form.get("affect_stock") == "1"

            product_ids = request.form.getlist("product_id[]")
            quantities = request.form.getlist("quantity[]")
            prices = request.form.getlist("unit_price[]")
            resolved = []
            subtotal = 0
            for pid, qty, price in zip(product_ids, quantities, prices):
                if not pid or not qty:
                    continue
                product = Product.query.get(int(pid))
                if not product:
                    raise ValueError(f"المنتج رقم {pid} غير موجود.")
                qty = int(qty)
                price = float(price or product.sale_price)
                if qty <= 0 or price < 0:
                    raise ValueError("الكمية والسعر يجب أن يكونا صالحين.")
                resolved.append((product, qty, price))
                subtotal += qty * price
            if not resolved:
                raise ValueError("أضف صنفًا واحدًا على الأقل إلى الفاتورة.")
            total = round(subtotal - discount, 2)
            if total < 0:
                raise ValueError("الخصم لا يمكن أن يتجاوز إجمالي الفاتورة.")
            if payment_method == "cash": cash, bank, credit = total, 0, 0
            elif payment_method == "bank": cash, bank, credit = 0, total, 0
            elif payment_method == "credit": cash, bank, credit = 0, 0, total
            if round(cash + bank + credit, 2) != total:
                raise ValueError("مجموع وسائل الدفع يجب أن يساوي إجمالي الفاتورة.")
            if credit > 0 and not customer_id:
                raise ValueError("الفاتورة الآجلة تتطلب تحديد العميل.")
            if credit > 0 and Customer.query.get(customer_id).would_exceed_limit(credit):
                raise ValueError("الفاتورة ستتجاوز سقف ائتمان العميل.")
            if affect_stock:
                for product, qty, _ in resolved:
                    stock = get_or_create_stock(product.id, warehouse_id)
                    if stock.quantity < qty:
                        raise ValueError(f"المخزون غير كافٍ للمنتج: {product.display_name()}.")

            invoice = Invoice(invoice_number=invoice_number, customer_id=customer_id,
                              warehouse_id=warehouse_id, user_id=user_id, shift_id=None,
                              date=invoice_date, subtotal=subtotal, discount=discount, total=total,
                              payment_method=payment_method, cash_amount=cash, bank_amount=bank,
                              bank_name=(request.form.get("bank_name") or None) if payment_method in ("bank", "split") else None,
                              credit_amount=credit, status="completed", stock_affected=affect_stock)
            db.session.add(invoice)
            db.session.flush()
            for product, qty, price in resolved:
                db.session.add(InvoiceItem(invoice_id=invoice.id, product_id=product.id,
                                           quantity=qty, unit_price=price, total=round(qty * price, 2),
                                           unit_cost=product.purchase_price or 0))
                if affect_stock:
                    stock = get_or_create_stock(product.id, warehouse_id)
                    stock.quantity -= qty
                    db.session.add(StockMovement(product_id=product.id, warehouse_id=warehouse_id,
                                                 movement_type="out", quantity=qty,
                                                 reference=f"فاتورة قديمة {invoice.invoice_number}"))
            if credit > 0:
                Customer.query.get(customer_id).balance += credit
            log_action("import_old_sale", "invoice", invoice.id,
                       f"فاتورة قديمة {invoice.invoice_number} بتاريخ {invoice_date:%Y-%m-%d} | تأثير المخزون={affect_stock}")
            db.session.commit()
            flash(f"تم حفظ فاتورة البيع القديمة {invoice.invoice_number} بنجاح.", "success")
            return redirect(url_for("sales"))
        except Exception as exc:
            db.session.rollback()
            flash(f"تعذر حفظ الفاتورة القديمة: {exc}", "danger")
    return render_template("old_sales_import.html", warehouses=Warehouse.query.all(),
                           customers=Customer.query.all(), users=User.query.filter_by(active=True).order_by(User.full_name).all(),
                           products=Product.query.filter_by(active=True).order_by(Product.name).all())

@app.route("/cashboxes", methods=["GET", "POST"])
@login_required
@permission_required("cashboxes")
def cashboxes():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        location = request.form.get("location", "").strip()
        initial_balance = float(request.form.get("initial_balance") or 0)
        default_warehouse_id = request.form.get("default_warehouse_id") or None
        if name:
            box = Cashbox(name=name, location=location, balance=0, default_warehouse_id=default_warehouse_id)
            db.session.add(box)
            db.session.flush()
            if initial_balance:
                _apply_cash_transfer(from_box=None, to_box=box, amount=initial_balance,
                                      note="رصيد افتتاحي عند إنشاء الصندوق/الخزنة")
            log_action("create_cashbox", "cashbox", details=f"{name} - رصيد ابتدائي: {initial_balance}")
            db.session.commit()
            flash("تمت إضافة الخزينة.", "success")
        return redirect(url_for("cashboxes"))
    return render_template("cashboxes.html", cashboxes=Cashbox.query.order_by(Cashbox.id).all(),
                           users=User.query.filter_by(active=True).order_by(User.full_name).all(),
                           warehouses=Warehouse.query.all(),
                           transfers=CashTransfer.query.order_by(CashTransfer.created_at.desc()).limit(100).all(),
                           currency=app.config["CURRENCY"])


@app.route("/cashboxes/edit/<int:cid>", methods=["POST"])
@login_required
@permission_required("cashboxes")
def edit_cashbox(cid):
    box = Cashbox.query.get_or_404(cid)
    box.name = request.form.get("name") or box.name
    box.location = request.form.get("location")
    box.default_warehouse_id = request.form.get("default_warehouse_id") or None

    new_balance_raw = request.form.get("new_balance", "").strip()
    if new_balance_raw != "" and not current_user.has_permission("cashbox_balance_edit"):
        flash("لا تملك صلاحية تعديل رصيد الخزينة يدويًا.", "danger")
        return redirect(url_for("cashboxes"))
    if new_balance_raw != "":
        new_balance = float(new_balance_raw)
        diff = round(new_balance - (box.balance or 0), 2)
        if diff != 0:
            # نسجّل الفرق كتحويل (إيداع/سحب من خارج النظام) بدل تعديل الرصيد مباشرة
            # بصمت، حتى يبقى له أثر واضح في سجل التحويلات لأي تصحيح يدوي.
            if diff > 0:
                _apply_cash_transfer(from_box=None, to_box=box, amount=diff,
                                      note="تصحيح يدوي لرصيد الخزينة من صفحة التعديل")
            else:
                _apply_cash_transfer(from_box=box, to_box=None, amount=abs(diff),
                                      note="تصحيح يدوي لرصيد الخزينة من صفحة التعديل")

    log_action("edit_cashbox", "cashbox", cid, box.name)
    db.session.commit()
    flash("تم تعديل بيانات الخزينة", "success")
    return redirect(url_for("cashboxes"))


@app.route("/cashboxes/delete/<int:cid>")
@login_required
@permission_required("cashboxes")
def delete_cashbox(cid):
    box = Cashbox.query.get_or_404(cid)
    if round(box.balance or 0, 2) != 0:
        flash(f"لا يمكن حذف '{box.name}' وبه رصيد {box.balance:.2f} {app.config['CURRENCY']}. "
              "حوّل الرصيد إلى صندوق آخر أولاً من خانة التحويل.", "danger")
        return redirect(url_for("cashboxes"))
    if Shift.query.filter_by(cashbox_id=box.id).count() > 0:
        flash(f"لا يمكن حذف '{box.name}' لوجود ورديات سابقة مرتبطة به. عطّله بدل حذفه.", "danger")
        return redirect(url_for("cashboxes"))
    User.query.filter_by(cashbox_id=box.id).update({"cashbox_id": None})
    log_action("delete_cashbox", "cashbox", cid, box.name)
    db.session.delete(box)
    db.session.commit()
    flash("تم حذف الخزينة", "info")
    return redirect(url_for("cashboxes"))


@app.route("/cashboxes/<int:cid>/assign/<int:uid>", methods=["POST"])
@login_required
@permission_required("cashboxes")
def assign_cashbox(cid, uid):
    box = Cashbox.query.get_or_404(cid)
    user = User.query.get_or_404(uid)
    user.cashbox_id = box.id
    log_action("assign_cashbox", "cashbox", cid, f"user={uid}")
    db.session.commit()
    flash("تم ربط المستخدم بالخزينة.", "success")
    return redirect(url_for("cashboxes"))


def _apply_cash_transfer(from_box, to_box, amount, note="", prefix="CTR"):
    """يطبّق تحويلًا نقديًا بين صندوقين (أو إيداع/سحب خارجي إن كان أحدهما فارغًا)
    ويُسجّله في سجل CashTransfer. لا يستدعي commit بنفسه. البادئة CLS = فرق عدّ عند فتح/إغلاق وردية."""
    transfer = CashTransfer(
        transfer_number=generate_number(prefix, CashTransfer),
        from_cashbox_id=from_box.id if from_box else None,
        to_cashbox_id=to_box.id if to_box else None,
        amount=amount, note=note,
        user_id=current_user.id if current_user.is_authenticated else None,
    )
    db.session.add(transfer)
    if from_box:
        from_box.balance = round((from_box.balance or 0) - amount, 2)
    if to_box:
        to_box.balance = round((to_box.balance or 0) + amount, 2)
    return transfer


@app.route("/cashboxes/transfer", methods=["POST"])
@login_required
@permission_required("cashboxes")
def transfer_cash():
    amount = float(request.form.get("amount") or 0)
    note = request.form.get("note", "").strip()
    from_id = request.form.get("from_cashbox_id") or None
    to_id = request.form.get("to_cashbox_id") or None

    if amount <= 0:
        flash("المبلغ يجب أن يكون أكبر من صفر.", "danger")
        return redirect(url_for("cashboxes"))
    if not from_id and not to_id:
        flash("يجب تحديد صندوق مصدر أو صندوق وجهة على الأقل.", "danger")
        return redirect(url_for("cashboxes"))
    if from_id and to_id and from_id == to_id:
        flash("لا يمكن التحويل من نفس الصندوق إلى نفسه.", "danger")
        return redirect(url_for("cashboxes"))

    from_box = Cashbox.query.get(from_id) if from_id else None
    to_box = Cashbox.query.get(to_id) if to_id else None
    if from_id and not from_box:
        flash("صندوق المصدر غير صحيح.", "danger")
        return redirect(url_for("cashboxes"))
    if to_id and not to_box:
        flash("صندوق الوجهة غير صحيح.", "danger")
        return redirect(url_for("cashboxes"))
    if from_box and from_box.balance < amount:
        flash(f"رصيد '{from_box.name}' الحالي ({from_box.balance:.2f}) أقل من المبلغ المطلوب تحويله.", "danger")
        return redirect(url_for("cashboxes"))

    transfer = _apply_cash_transfer(from_box, to_box, amount, note)
    log_action("transfer_cash", "cash_transfer", transfer.id if transfer.id else None,
               f"{transfer.transfer_number}: {from_box.name if from_box else 'خارج النظام'} → "
               f"{to_box.name if to_box else 'خارج النظام'} : {amount}")
    db.session.commit()
    flash(f"تم تسجيل التحويل النقدي ({transfer.transfer_number}) بنجاح.", "success")
    return redirect(url_for("cashboxes"))


@app.route("/cashboxes/transfer/<int:tid>/delete", methods=["POST"])
@login_required
@permission_required("cashboxes")
def delete_cash_transfer(tid):
    """حذف/تراجع عن تحويل نقدي سابق: يعكس أثره بالضبط على رصيد الصندوقين المعنيين."""
    transfer = CashTransfer.query.get_or_404(tid)
    if (transfer.transfer_number or "").startswith("FND-"):
        # طرف نقدي لتحويل مع حساب بنكي/خزنة عملة: حذفه من هنا يترك الطرف الآخر بلا مقابل
        flash("هذا التحويل جزء من عملية مع حساب بنكي أو صرف عملة. احذفه من صفحة الحسابات البنكية.", "warning")
        return redirect(url_for("cashboxes"))
    if transfer.from_cashbox:
        transfer.from_cashbox.balance = round((transfer.from_cashbox.balance or 0) + transfer.amount, 2)
    if transfer.to_cashbox:
        transfer.to_cashbox.balance = round((transfer.to_cashbox.balance or 0) - transfer.amount, 2)
    log_action("delete_cash_transfer", "cash_transfer", tid,
               f"{transfer.transfer_number}: عكس مبلغ {transfer.amount}")
    db.session.delete(transfer)
    db.session.commit()
    flash("تم حذف التحويل وعكس أثره على أرصدة الصناديق المعنية.", "info")
    return redirect(url_for("cashboxes"))


# ---------------------------------------------------------------------------
# الحسابات البنكية وخزائن العملات (على طريقة دفاتر البنك والنقد في Odoo: لكل حساب عملته ورصيده)
# ---------------------------------------------------------------------------
ENTRY_TYPE_LABELS = {
    "income": "إيراد", "expense": "مصروف", "customer_payment": "تحصيل من عميل", "customer_debt": "دين يدوي",
    "supplier_payment": "دفع لمورد", "employee_advance": "سلفة موظف", "employee_repayment": "تسديد سلفة موظف",
    "employee_draw": "سحب على الحساب (موظف)", "external_advance": "سلفة لشخص خارجي",
    "external_repayment": "تسديد سلفة شخص خارجي", "salary_payment": "دفع راتب", "customer_discount": "خصم عميل",
    "inventory_loss": "خسارة مخزون", "loan_received": "قرض/سلفة من جهة خارجية",
    "loan_repayment": "تسديد قرض لجهة خارجية", "sales_return": "إرجاع فاتورة بيع", "owner_withdrawal": "سحب مالك",
    "purchase_cost": "مصاريف شراء (شحن/جمارك)",
}


@event.listens_for(db.session, "before_flush")
def _assign_default_fund(session, flush_context, instances):
    """أي مبيعات أو حركة بنكية بالدينار لم يُحدَّد لها حساب تدخل الحساب البنكي الافتراضي. يُنفَّذ هنا في مكان
    واحد بدل تعديل كل مسار ينشئ فاتورة أو حركة (نقطة البيع، الاستبدال، الإرجاع، الشراء، الرواتب...).
    ما تاريخه قبل إنشاء الحساب لا يُربط: رصيد الحساب الافتتاحي يشمله أصلًا."""
    targets = [o for o in list(session.new) + list(session.dirty)
               if isinstance(o, (Invoice, AccountEntry)) and o.fund_account_id is None]
    if not targets:
        return
    with session.no_autoflush:
        default = session.query(FundAccount).filter_by(is_default=True, active=True).first()
    if not default:
        return
    for o in targets:
        when = o.date or datetime.utcnow()
        if when < (default.created_at or when):
            continue
        if isinstance(o, Invoice):
            if (o.bank_amount or 0) > 0:
                o.fund_account_id = default.id
        elif o in session.new and o.payment_method == "bank" and o.entry_type in CASHBOX_CASH_EFFECT:
            o.fund_account_id = default.id


def _fund_movements(fund):
    """كل ما دخل وخرج من حساب بنكي/خزنة عملة مرتبًا زمنيًا. amount بعملة الحساب (موجب = داخل)،
    و local مقابله بالدينار إن عُرف (لحساب متوسط سعر شراء العملة)."""
    foreign = (fund.currency_code or "LYD") != "LYD"
    rows = []
    for e in AccountEntry.query.filter_by(fund_account_id=fund.id).all():
        effect = CASHBOX_CASH_EFFECT.get(e.entry_type)
        if not effect:
            continue
        party = (e.supplier or e.customer or e.employee or e.external_borrower)
        rows.append(dict(date=e.date, kind="entry", entry_id=e.id,
                         label=ENTRY_TYPE_LABELS.get(e.entry_type, e.entry_type),
                         desc=" — ".join(b for b in ((party.name if party else ""), e.note or "") if b),
                         amount=effect * (e.supplier_amount() if foreign else (e.amount or 0)),
                         local=effect * (e.amount or 0)))
    if not foreign:
        for inv in Invoice.query.filter(Invoice.fund_account_id == fund.id, Invoice.status != "voided",
                                        Invoice.bank_amount > 0).all():
            rows.append(dict(date=inv.date, kind="invoice", label="مبيعات بنكية", desc=inv.invoice_number,
                             amount=inv.bank_amount, local=inv.bank_amount))
    for t in FundTransfer.query.filter(db.or_(FundTransfer.from_fund_id == fund.id,
                                              FundTransfer.to_fund_id == fund.id)).all():
        note = f" — {t.note}" if t.note else ""
        if t.to_fund_id == fund.id:
            rows.append(dict(date=t.date, kind="transfer", label="وارد من " + t.from_name(),
                             desc=t.transfer_number + note, amount=t.to_amount,
                             local=t.from_amount if t.from_currency() == "LYD" else None))
        if t.from_fund_id == fund.id:
            rows.append(dict(date=t.date, kind="transfer", label="صادر إلى " + t.to_name(),
                             desc=t.transfer_number + note, amount=-t.from_amount,
                             local=-t.to_amount if t.to_currency() == "LYD" else None))
    rows.sort(key=lambda r: r["date"] or datetime.min)
    return rows


def _fund_balance(fund, exclude_entry_id=None):
    return round((fund.opening_balance or 0) + sum(r["amount"] for r in _fund_movements(fund)
                                                    if not (exclude_entry_id and r.get("entry_id") == exclude_entry_id)), 2)


def _fund_avg_rate(fund):
    """متوسط تكلفة وحدة العملة الموجودة في الحساب بالدينار (متوسط مرجّح متحرك مثل تكلفة المخزون):
    الشراء بسعره يدخل في المتوسط، والخروج يخرج بالمتوسط الحالي. None لحساب بالدينار."""
    if (fund.currency_code or "LYD") == "LYD":
        return None
    avg = fund.opening_rate or 0
    qty = fund.opening_balance or 0
    value = qty * avg
    for r in _fund_movements(fund):
        a = r["amount"]
        if a > 0:
            value += r["local"] if r["kind"] == "transfer" and r["local"] else a * avg
            qty += a
        else:
            value += a * avg
            qty += a
        if qty > 0.005 and value > 0:
            avg = value / qty
    return round(avg, 4) if avg else None


def _fund_view(fund):
    return {"fund": fund, "balance": _fund_balance(fund), "avg_rate": _fund_avg_rate(fund)}


@app.route("/funds", methods=["GET", "POST"])
@login_required
@permission_required("cashboxes")
def fund_accounts():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        currency = request.form.get("currency_code") or "LYD"
        try:
            opening = float(request.form.get("opening_balance") or 0)
            opening_rate = float(request.form.get("opening_rate") or 0)
        except ValueError:
            opening, opening_rate = 0, 0
        if not name or FundAccount.query.filter_by(name=name).first():
            flash("اكتب اسمًا للحساب غير مستعمل من قبل.", "danger")
        elif currency not in _active_currency_codes():
            flash("اختر عملة صحيحة.", "danger")
        elif currency != "LYD" and opening and opening_rate <= 0:
            flash(f"اكتب بكم دينار اشتريت الرصيد الافتتاحي (كم دينار = 1 {currency}).", "danger")
        else:
            kind = "cash" if request.form.get("kind") == "cash" else "bank"
            is_default = (currency == "LYD" and kind == "bank"
                          and not FundAccount.query.filter_by(is_default=True).first())
            db.session.add(FundAccount(name=name, kind=kind, currency_code=currency, opening_balance=opening,
                                       opening_rate=opening_rate if currency != "LYD" else 1,
                                       is_default=is_default))
            log_action("create_fund_account", "fund_account", details=f"{name} {currency} افتتاحي {opening}")
            db.session.commit()
            flash("تمت إضافة الحساب." + (" وصار الحساب الافتراضي للمبيعات والحركات البنكية." if is_default else ""),
                  "success")
        return redirect(url_for("fund_accounts"))
    funds = [_fund_view(f) for f in FundAccount.query.order_by(FundAccount.active.desc(), FundAccount.id).all()]
    return render_template("fund_accounts.html", funds=funds,
                           cashboxes=Cashbox.query.filter_by(active=True).order_by(Cashbox.id).all(),
                           currency_codes=_active_currency_codes(),
                           transfers=FundTransfer.query.order_by(FundTransfer.date.desc()).limit(100).all())


@app.route("/funds/<int:fid>")
@login_required
@permission_required("cashboxes")
def fund_statement(fid):
    fund = FundAccount.query.get_or_404(fid)
    rows = _fund_movements(fund)
    running = fund.opening_balance or 0
    for r in rows:
        running = round(running + r["amount"], 2)
        r["balance"] = running
    return render_template("fund_statement.html", rows=rows, **_fund_view(fund))


@app.route("/funds/<int:fid>/edit", methods=["POST"])
@login_required
@permission_required("cashboxes")
def edit_fund_account(fid):
    fund = FundAccount.query.get_or_404(fid)
    name = (request.form.get("name") or "").strip()
    if name and name != fund.name and not FundAccount.query.filter_by(name=name).first():
        fund.name = name
    fund.active = bool(request.form.get("active"))
    try:
        new_opening = float(request.form.get("opening_balance") or fund.opening_balance or 0)
        new_rate = float(request.form.get("opening_rate") or fund.opening_rate or 1)
    except ValueError:
        db.session.rollback()
        flash("الرصيد الافتتاحي يجب أن يكون رقمًا.", "danger")
        return redirect(url_for("fund_accounts"))
    if (fund.currency_code or "LYD") == "LYD":
        new_rate = 1
    if abs(new_opening - (fund.opening_balance or 0)) > 0.005 or abs(new_rate - (fund.opening_rate or 1)) > 0.00005:
        if not current_user.has_permission("cashbox_balance_edit"):
            db.session.rollback()
            flash("لا تملك صلاحية تعديل الرصيد الافتتاحي.", "danger")
            return redirect(url_for("fund_accounts"))
        fund.opening_balance, fund.opening_rate = new_opening, new_rate
    if request.form.get("is_default") and (fund.currency_code or "LYD") == "LYD" and fund.active:
        FundAccount.query.filter(FundAccount.id != fund.id).update({"is_default": False})
        fund.is_default = True
    elif not request.form.get("is_default") or not fund.active:
        fund.is_default = False
    log_action("edit_fund_account", "fund_account", fid, f"{fund.name} افتتاحي {fund.opening_balance}")
    db.session.commit()
    flash("تم تعديل الحساب.", "success")
    return redirect(url_for("fund_accounts"))


@app.route("/funds/<int:fid>/delete", methods=["POST"])
@login_required
@permission_required("cashboxes")
def delete_fund_account(fid):
    fund = FundAccount.query.get_or_404(fid)
    if _fund_movements(fund):
        flash(f"\"{fund.name}\" عليه حركات فلا يُحذف. أوقفه بدل الحذف.", "danger")
        return redirect(url_for("fund_accounts"))
    log_action("delete_fund_account", "fund_account", fid, fund.name)
    db.session.delete(fund)
    db.session.commit()
    flash("تم حذف الحساب.", "info")
    return redirect(url_for("fund_accounts"))


def _parse_money_place(raw):
    """"box:3" أو "fund:2" → (خزينة، حساب). أحدهما None."""
    kind, _, ident = (raw or "").partition(":")
    try:
        ident = int(ident)
    except ValueError:
        return None, None
    if kind == "box":
        return Cashbox.query.get(ident), None
    if kind == "fund":
        return None, FundAccount.query.get(ident)
    return None, None


@app.route("/funds/transfer", methods=["POST"])
@login_required
@permission_required("cashboxes")
def fund_transfer():
    """إيداع/سحب بين خزينة وحساب بنكي، تحويل بين حسابين، أو صرف عملة (المبلغان يُكتبان كما حصلا فعلًا
    والسعر يُستنتج منهما، لأن سعر السوق يتغير كل يوم)."""
    from_box, from_fund = _parse_money_place(request.form.get("from_place"))
    to_box, to_fund = _parse_money_place(request.form.get("to_place"))
    try:
        from_amount = float(request.form.get("from_amount") or 0)
        to_amount = float(request.form.get("to_amount") or 0)
    except ValueError:
        from_amount, to_amount = 0, 0
    src, dst = from_box or from_fund, to_box or to_fund
    error = None
    if not src or not dst:
        error = "اختر المصدر والوجهة."
    elif from_box and to_box:
        error = "التحويل بين خزينتين نقديتين يتم من صفحة الخزائن."
    elif from_fund and to_fund and from_fund.id == to_fund.id:
        error = "المصدر والوجهة نفس الحساب."
    elif from_amount <= 0:
        error = "المبلغ يجب أن يكون أكبر من صفر."
    if not error:
        from_cur = (from_fund.currency_code or "LYD") if from_fund else "LYD"
        to_cur = (to_fund.currency_code or "LYD") if to_fund else "LYD"
        if from_cur == to_cur:
            to_amount = from_amount
        elif to_amount <= 0:
            error = f"صرف عملة: اكتب المبلغ المستلم بـ{to_cur} (السعر يُحسب من المبلغين)."
        if not error:
            have = (from_box.balance or 0) if from_box else _fund_balance(from_fund)
            if from_amount > have + 0.005:
                error = f"رصيد \"{src.name}\" ({have:,.2f} {from_cur}) أقل من المبلغ المطلوب."
    if error:
        flash(error, "danger")
        return redirect(url_for("fund_accounts"))

    note = (request.form.get("note") or "").strip()
    t = FundTransfer(transfer_number=generate_number("FTR", FundTransfer), from_fund_id=from_fund.id if from_fund else None,
                     to_fund_id=to_fund.id if to_fund else None, from_cashbox_id=from_box.id if from_box else None,
                     to_cashbox_id=to_box.id if to_box else None, from_amount=round(from_amount, 2),
                     to_amount=round(to_amount, 2), note=note, user_id=current_user.id, date=datetime.utcnow())
    db.session.add(t)
    if from_box or to_box:
        # طرف الخزينة النقدية: إيداع/سحب خارجي في سجل الخزائن، فتحسبه الوردية ضمن "المتوقع"
        ct = _apply_cash_transfer(from_box=from_box, to_box=to_box, amount=round(from_amount if from_box else to_amount, 2),
                                  note=f"{t.transfer_number}: {src.name} ← {dst.name}" + (f" — {note}" if note else ""),
                                  prefix="FND")
        db.session.flush()
        t.cash_transfer_id = ct.id
    log_action("fund_transfer", "fund_transfer", details=f"{t.transfer_number}: {src.name} {from_amount} {from_cur} → "
                                                         f"{dst.name} {to_amount} {to_cur}")
    db.session.commit()
    msg = f"تم تسجيل العملية {t.transfer_number}."
    if from_cur != to_cur:
        lyd, other, cur = (from_amount, to_amount, to_cur) if from_cur == "LYD" else (to_amount, from_amount, from_cur)
        if "LYD" in (from_cur, to_cur) and other:
            msg += f" السعر: 1 {cur} = {lyd / other:.4f} د.ل"
    flash(msg, "success")
    return redirect(url_for("fund_accounts"))


@app.route("/funds/transfer/<int:tid>/delete", methods=["POST"])
@login_required
@permission_required("cashboxes")
def delete_fund_transfer(tid):
    t = FundTransfer.query.get_or_404(tid)
    if t.to_fund and _fund_balance(t.to_fund) - t.to_amount < -0.005:
        flash(f"لا يُحذف: رصيد \"{t.to_fund.name}\" يصير سالبًا لأن المبلغ صُرف منه بعدها.", "danger")
        return redirect(url_for("fund_accounts"))
    ct = CashTransfer.query.get(t.cash_transfer_id) if t.cash_transfer_id else None
    if ct:
        if ct.from_cashbox:
            ct.from_cashbox.balance = round((ct.from_cashbox.balance or 0) + ct.amount, 2)
        if ct.to_cashbox:
            ct.to_cashbox.balance = round((ct.to_cashbox.balance or 0) - ct.amount, 2)
        db.session.delete(ct)
    log_action("delete_fund_transfer", "fund_transfer", tid, f"{t.transfer_number}: {t.from_amount} → {t.to_amount}")
    db.session.delete(t)
    db.session.commit()
    flash("تم حذف العملية وعكس أثرها.", "info")
    return redirect(url_for("fund_accounts"))


# ---------------------------------------------------------------------------
# المركز المالي (ما نملك وما علينا) وفروقات الصرف
# ---------------------------------------------------------------------------
def _last_rates():
    """آخر سعر فعلي استُعمل لكل عملة (كم دينار = 1): من فاتورة شراء، دفعة مورد، أو صرف عملة.
    هو أقرب تقدير لسعر السوق اليوم، ويُستعمل لتقييم الأرصدة الأجنبية بالدينار."""
    seen = {}

    def note(cur, when, rate):
        if cur and cur != "LYD" and rate and rate > 0 and when and (cur not in seen or when > seen[cur][0]):
            seen[cur] = (when, rate)

    for p in Purchase.query.filter(Purchase.currency_code != "LYD").all():
        note(p.currency_code, p.date, p.exchange_rate)
    for e in AccountEntry.query.filter(AccountEntry.entry_type == "supplier_payment",
                                       AccountEntry.currency_code != "LYD").all():
        fa = e.supplier_amount()
        note(e.currency_code, e.date, (e.amount / fa) if fa else None)
    for t in FundTransfer.query.all():
        a, b = t.from_currency(), t.to_currency()
        if a == "LYD" and b != "LYD" and t.to_amount:
            note(b, t.date, t.from_amount / t.to_amount)
        elif b == "LYD" and a != "LYD" and t.from_amount:
            note(a, t.date, t.to_amount / t.from_amount)
    return {c: round(v[1], 4) for c, v in seen.items()}


def _fx_differences(rates):
    """فرق الصرف لكل مورد بعملة أجنبية: البضاعة دخلت المخزون بسعر يوم الفاتورة، والدفع تم بسعر يوم الدفع.
    المحقَّق = ما خرج فعلًا بالدينار − (المدفوع بعملته × متوسط سعر فواتيره): موجب خسارة، سالب ربح.
    غير المحقَّق = الباقي عليه × (سعر اليوم − متوسط سعر فواتيره)."""
    rows = []
    for s in Supplier.query.filter(Supplier.currency_code != "LYD").order_by(Supplier.name).all():
        pf = pl = 0.0
        for p in Purchase.query.filter_by(supplier_id=s.id).all():
            pf += p.foreign_total or 0
            pl += p.local_total or 0
        for pr in PurchaseReturn.query.filter_by(supplier_id=s.id).all():
            pf -= pr.total or 0
            pl -= pr.local_total or 0
        payf = payl = 0.0
        for e in AccountEntry.query.filter_by(supplier_id=s.id, entry_type="supplier_payment").all():
            payf += e.supplier_amount()
            payl += e.amount or 0
        if pf <= 0.005 and payf <= 0.005:
            continue
        buy_rate = (pl / pf) if pf > 0.005 else None
        now_rate = rates.get(s.currency_code)
        bal = s.balance or 0
        rows.append({
            "supplier": s, "purchases_foreign": round(pf, 2), "buy_rate": round(buy_rate, 4) if buy_rate else None,
            "paid_foreign": round(payf, 2), "paid_local": round(payl, 2),
            "pay_rate": round(payl / payf, 4) if payf > 0.005 else None,
            "realized": round(payl - payf * buy_rate, 2) if buy_rate and payf > 0.005 else None,
            "balance": round(bal, 2), "now_rate": now_rate,
            "unrealized": round(bal * (now_rate - buy_rate), 2) if buy_rate and now_rate and bal > 0.005 else None,
        })
    return rows


@app.route("/reports/position")
@login_required
@permission_required("financial_report")
def financial_position():
    rates = _last_rates()
    assets, liabilities, unpriced = [], [], []

    def add(bucket, label, amount, detail="", url=None):
        if abs(amount) > 0.005:
            bucket.append({"label": label, "amount": round(amount, 2), "detail": detail, "url": url})

    add(assets, "نقد في الخزائن", sum(b.balance or 0 for b in Cashbox.query.all()), url=url_for("cashboxes"))
    for f in FundAccount.query.order_by(FundAccount.id).all():
        bal = _fund_balance(f)
        cur = f.currency_code or "LYD"
        if cur == "LYD":
            add(assets if bal >= 0 else liabilities, f.name, abs(bal), "حساب بنكي" + (" (مكشوف)" if bal < 0 else ""),
                url_for("fund_statement", fid=f.id))
        else:
            rate = _fund_avg_rate(f) or rates.get(cur)
            if rate:
                add(assets, f.name, bal * rate, f"{bal:,.2f} {cur} × {rate:g}", url_for("fund_statement", fid=f.id))
            elif abs(bal) > 0.005:
                unpriced.append(f"{f.name}: {bal:,.2f} {cur}")
    stock_value = db.session.query(db.func.coalesce(db.func.sum(Stock.quantity * Product.purchase_price), 0)) \
        .join(Product, Stock.product_id == Product.id).scalar() or 0
    add(assets, "بضاعة في المخازن (بالتكلفة)", stock_value, url=url_for("inventory_overview"))
    cust = Customer.query.all()
    add(assets, "ديون على العملاء", sum(c.balance for c in cust if (c.balance or 0) > 0), url=url_for("debts"))
    add(liabilities, "أرصدة دائنة للعملاء", sum(-c.balance for c in cust if (c.balance or 0) < 0))
    add(assets, "سلف على الموظفين", sum(e.balance for e in Employee.query.all() if (e.balance or 0) > 0))
    ext = ExternalBorrower.query.all()
    add(assets, "سلف لأشخاص وجهات خارجية", sum(b.balance for b in ext if (b.balance or 0) > 0))
    add(liabilities, "قروض وسلف أخذها المحل", sum(-b.balance for b in ext if (b.balance or 0) < 0))
    for s in Supplier.query.order_by(Supplier.name).all():
        bal, cur = s.balance or 0, s.currency_code or "LYD"
        if abs(bal) <= 0.005:
            continue
        rate = 1 if cur == "LYD" else rates.get(cur)
        if not rate:
            unpriced.append(f"{s.name}: {bal:,.2f} {cur}")
            continue
        detail = "" if cur == "LYD" else f"{abs(bal):,.2f} {cur} × {rate:g}"
        if bal > 0:
            add(liabilities, f"مستحق للمورد {s.name}", bal * rate, detail, url_for("supplier_statement", sid=s.id))
        else:
            add(assets, f"لنا عند المورد {s.name}", -bal * rate, detail, url_for("supplier_statement", sid=s.id))
    total_assets = round(sum(a["amount"] for a in assets), 2)
    total_liabilities = round(sum(l["amount"] for l in liabilities), 2)
    fx_rows = _fx_differences(rates)
    return render_template("financial_position.html", assets=assets, liabilities=liabilities, unpriced=unpriced,
                           total_assets=total_assets, total_liabilities=total_liabilities,
                           net=round(total_assets - total_liabilities, 2), rates=rates, fx_rows=fx_rows,
                           fx_realized=round(sum(r["realized"] or 0 for r in fx_rows), 2),
                           fx_unrealized=round(sum(r["unrealized"] or 0 for r in fx_rows), 2),
                           printed_at=datetime.now(), currency=app.config["CURRENCY"])


@app.route("/reports/employee")
@login_required
@permission_required("reports")
def employee_report():
    start = _parse_date(request.args.get("start"))
    end = _parse_date(request.args.get("end"), end=True)
    user_id = request.args.get("user_id", type=int)
    query = Invoice.query.filter(Invoice.status == "completed")
    if start: query = query.filter(Invoice.date >= start)
    if end: query = query.filter(Invoice.date <= end)
    if user_id: query = query.filter(Invoice.user_id == user_id)
    invoices = query.order_by(Invoice.date.asc()).all()
    return render_template("employee_report.html", invoices=invoices,
                           users=User.query.filter_by(active=True).all(),
                           selected_user=user_id, start=request.args.get("start",""),
                           end=request.args.get("end",""), currency=app.config["CURRENCY"])


@app.route("/sales/edit/<int:iid>", methods=["GET", "POST"])
@login_required
@permission_required("sales_edit")
def edit_invoice(iid):
    invoice = Invoice.query.get_or_404(iid)
    if invoice.status == "voided":
        flash("لا يمكن تعديل فاتورة ملغاة.", "danger")
        return redirect(url_for("sales"))
    blocked = _exchange_lock_reason(invoice)
    if not blocked and SalesReturn.query.filter_by(invoice_id=invoice.id).first():
        blocked = (f"على الفاتورة {invoice.invoice_number} إرجاع مسجّل - لا يمكن تعديلها. "
                   "استخدم الإرجاع أو الاستبدال بدل التعديل.")
    if blocked:
        flash(blocked, "danger")
        return redirect(url_for("sales"))
    if request.method == "POST":
        payload = {
            "warehouse_id": request.form.get("warehouse_id"),
            "customer_id": request.form.get("customer_id"),
            "discount": request.form.get("discount"),
            "payment_method": request.form.get("payment_method"),
            "cash_amount": request.form.get("cash_amount"),
            "bank_amount": request.form.get("bank_amount"),
            "credit_amount": request.form.get("credit_amount"),
            "bank_name": request.form.get("bank_name"),
            "items": []
        }
        product_ids = request.form.getlist("product_id")
        quantities = request.form.getlist("quantity")
        prices = request.form.getlist("unit_price")
        for pid, qty, price in zip(product_ids, quantities, prices):
            payload["items"].append({"product_id": pid, "quantity": qty, "unit_price": price})
        try:
            if not current_user.is_manager_or_admin():
                existing = ApprovalRequest.query.filter_by(invoice_id=iid, request_type="edit_invoice", status="pending").first()
                if existing:
                    flash("يوجد طلب تعديل معلّق لهذه الفاتورة.", "warning")
                else:
                    db.session.add(ApprovalRequest(request_type="edit_invoice", invoice_id=iid,
                                                    requested_by=current_user.id,
                                                    reason=request.form.get("reason",""),
                                                    proposed_data=json.dumps(payload, ensure_ascii=False)))
                    log_action("request_edit_invoice", "invoice", iid, request.form.get("reason",""))
                    db.session.commit()
                    flash("تم إرسال طلب تعديل الفاتورة للموافقة.", "info")
            else:
                apply_invoice_edit(invoice, payload)
                log_action("edit_invoice", "invoice", iid, "تعديل مباشر")
                db.session.commit()
                flash("تم تعديل الفاتورة بنجاح.", "success")
        except Exception as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        return redirect(url_for("sales"))
    return render_template("invoice_edit.html", invoice=invoice, warehouses=Warehouse.query.all(),
                           customers=Customer.query.all(), currency=app.config["CURRENCY"])


@app.route("/settings", methods=["GET", "POST"])
@login_required
@admin_required
def system_settings():
    if request.method == "POST":
        keys = [
            "company_name", "company_subtitle", "company_phone",
            "company_address", "invoice_header_note", "invoice_footer",
            "currency", "low_stock_threshold", "export_path", "local_currency",
            "usd_exchange_rate", "gemini_api_key", "claude_api_key", "ai_provider",
        ]
        for key in keys:
            Setting.set(key, request.form.get(key, "").strip())
        db.session.commit()
        log_action("update_settings", "settings", details="تحديث إعدادات النظام")
        db.session.commit()
        flash("تم حفظ الإعدادات بنجاح", "success")
        return redirect(url_for("system_settings"))
    s = get_settings()
    return render_template("settings.html", s=s)


@app.route("/backup", methods=["GET", "POST"])
@login_required
@permission_required("backup")
def backup_page():
    import backup_service as bs
    if request.method == "POST":
        action = request.form.get("action")
        if action == "run_now":
            result = bs.backup_database(app, triggered_by="manual", upload=True)
            log_action("manual_backup", entity_type="backup", details=result.get("message", "")[:250])
            db.session.commit()
            flash(result["message"], "success" if result["ok"] else "danger")
        elif action == "save_google":
            if not current_user.is_admin():
                flash("ربط Google Drive للمالك (admin) فقط.", "danger")
                return redirect(url_for("backup_page"))
            client_id = (request.form.get("client_id") or "").strip()
            client_secret = (request.form.get("client_secret") or "").strip()
            if not client_id.endswith(".apps.googleusercontent.com"):
                flash("Client ID غير صحيح - ينتهي عادة بـ .apps.googleusercontent.com", "danger")
                return redirect(url_for("backup_page"))
            Setting.set("gdrive_client_id", client_id)
            if client_secret:
                Setting.set("gdrive_client_secret", client_secret)
            log_action("gdrive_credentials_saved", entity_type="backup")
            db.session.commit()
            flash("تم حفظ بيانات تطبيق Google. اضغط الآن \"ربط حساب Google Drive\".", "success")
        elif action == "disconnect":
            if not current_user.is_admin():
                flash("للمالك (admin) فقط.", "danger")
                return redirect(url_for("backup_page"))
            bs.disconnect()
            log_action("gdrive_disconnected", entity_type="backup")
            db.session.commit()
            flash("تم فك ربط Google Drive. النسخ المحلية مستمرة.", "info")
        elif action == "save_extra_dir":
            Setting.set("backup_extra_dir", (request.form.get("extra_dir") or "").strip())
            db.session.commit()
            flash("تم حفظ مجلد النسخة الإضافية.", "success")
        return redirect(url_for("backup_page"))
    logs = BackupLog.query.order_by(BackupLog.created_at.desc()).limit(10).all()
    return render_template("backup.html", logs=logs, status=bs.get_backup_status(app))


@app.route("/backup/google/connect", methods=["POST"])
@login_required
@admin_required
def backup_google_connect():
    """يفتح صفحة موافقة Google في متصفح الجهاز (Google ترفض تسجيل الدخول داخل نافذة البرنامج المدمجة).
    Google تعيد المتصفح إلى http://127.0.0.1:<المنفذ>/ ومعه الرمز، فيلتقطه _google_oauth_callback."""
    import backup_service as bs
    import webbrowser
    port = request.host.rsplit(":", 1)[-1] if ":" in request.host else "80"
    redirect_uri = f"http://127.0.0.1:{port}/"
    try:
        url = bs.build_auth_url(redirect_uri)
    except RuntimeError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("backup_page"))
    opened = False
    if request.remote_addr in ("127.0.0.1", "::1"):
        try:
            opened = webbrowser.open(url)
        except Exception:
            opened = False
    return render_template("backup_connect.html", auth_url=url, opened=opened)


@app.before_request
def _google_oauth_callback():
    """عودة المتصفح من Google: http://127.0.0.1:<port>/?state=...&code=...  لا يتطلب تسجيل دخول لأن المتصفح
    الخارجي ليس مسجّلًا في البرنامج؛ الحماية بمطابقة state السري الذي أنشأناه قبل دقائق فقط."""
    if request.path != "/" or "state" not in request.args or not (
            "code" in request.args or "error" in request.args):
        return None
    import backup_service as bs
    if not bs.is_pending_callback(request.args.get("state")):
        return None
    if request.args.get("error"):
        Setting.set("gdrive_oauth_state", "")
        db.session.commit()
        return render_template("backup_connected.html", ok=False,
                               message="تم إلغاء الربط من صفحة Google. يمكنك المحاولة مرة أخرى من صفحة النسخ الاحتياطي.")
    try:
        account = bs.finish_auth(request.args.get("code"))
    except Exception as exc:
        return render_template("backup_connected.html", ok=False, message=str(exc))
    db.session.add(AuditLog(action="gdrive_connected", entity_type="backup", details=account or "",
                            ip_address=request.remote_addr))
    db.session.commit()
    bs.request_drive_upload()  # أول نسخة تُرفع خلال دقائق
    return render_template("backup_connected.html", ok=True, message=account)


@app.route("/backup/download/<path:name>")
@login_required
@permission_required("backup")
def backup_download(name):
    import backup_service as bs
    safe = os.path.basename(name)
    path = os.path.join(bs.backup_dir(app), safe)
    if not (safe.startswith("ZG_ERP_") and safe.endswith(".zip") and os.path.isfile(path)):
        flash("الملف غير موجود.", "danger")
        return redirect(url_for("backup_page"))
    return send_file(path, as_attachment=True, download_name=safe, mimetype="application/zip")


@app.route("/users", methods=["GET", "POST"])
@login_required
@admin_required
def users():
    if request.method == "POST":
        user = User(username=request.form.get("username"), full_name=request.form.get("full_name"),
                    role=request.form.get("role") or "cashier", can_override_price=bool(request.form.get("can_override_price")))
        user.set_password(request.form.get("password"))
        selected_permissions = request.form.getlist("permissions")
        user.set_permissions(selected_permissions)
        db.session.add(user)
        log_action("create_user", "user", details=user.username)
        db.session.commit()
        flash("تمت إضافة المستخدم", "success")
        return redirect(url_for("users"))
    return render_template("users.html", users=User.query.all(), permissions=PERMISSIONS)


@app.route("/users/<int:uid>/toggle", methods=["POST"])
@login_required
@admin_required
def toggle_user(uid):
    user = User.query.get_or_404(uid)
    if user.id == current_user.id:
        flash("لا يمكنك تعطيل حسابك الخاص", "warning")
    else:
        user.active = not user.active
        log_action("toggle_user", "user", uid, f"active={user.active}")
        db.session.commit()
        flash("تم تحديث حالة المستخدم", "success")
    return redirect(url_for("users"))


@app.route("/users/<int:uid>/reset-password", methods=["POST"])
@login_required
@admin_required
def reset_user_password(uid):
    user = User.query.get_or_404(uid)
    new_pw = request.form.get("new_password")
    if new_pw and len(new_pw) >= 4:
        user.set_password(new_pw)
        log_action("reset_password", "user", uid)
        db.session.commit()
        flash(f"تم إعادة تعيين كلمة مرور {user.username}", "success")
    else:
        flash("كلمة المرور قصيرة جدًا", "danger")
    return redirect(url_for("users"))


@app.route("/users/<int:uid>/edit", methods=["POST"])
@login_required
@admin_required
def edit_user(uid):
    user = User.query.get_or_404(uid)
    before = {"full_name": user.full_name, "role": user.role,
              "permissions": user.get_permissions(), "can_override_price": user.can_override_price}

    full_name = request.form.get("full_name", "").strip()
    if full_name:
        user.full_name = full_name
    if user.id != current_user.id:
        # لا نسمح للمستخدم بتغيير دوره التقني الخاص لتفادي تعطيل نفسه بالخطأ من هذه الشاشة
        user.role = request.form.get("role") or user.role
    user.can_override_price = bool(request.form.get("can_override_price"))
    selected_permissions = request.form.getlist("permissions")
    user.set_permissions(selected_permissions)

    log_action("edit_user_permissions", "user", uid,
               f"قبل: {before} | بعد: full_name={user.full_name}, role={user.role}, "
               f"permissions={user.get_permissions()}, can_override_price={user.can_override_price}")
    db.session.commit()
    flash(f"تم تحديث بيانات وصلاحيات {user.username}", "success")
    return redirect(url_for("users"))


@app.route("/users/<int:uid>/delete", methods=["POST"])
@login_required
@admin_required
def delete_user(uid):
    user = User.query.get_or_404(uid)
    if user.id == current_user.id:
        flash("لا يمكنك حذف حسابك الخاص وأنت مسجّل الدخول به.", "danger")
        return redirect(url_for("users"))

    # حماية: لا نحذف مستخدمًا له سجل عمليات فعلي (فواتير، ورديات، حركات مالية...) حتى لا
    # تنكسر تقارير سابقة أو تُفقد نسبة العملية لصاحبها؛ الأفضل تعطيل الحساب بدل حذفه في هذه الحالة.
    linked_counts = {
        "فواتير مبيعات": Invoice.query.filter_by(user_id=uid).count(),
        "ورديات": Shift.query.filter_by(user_id=uid).count(),
        "فواتير مشتريات": Purchase.query.filter_by(user_id=uid).count(),
        "حركات مالية (حسابات)": AccountEntry.query.filter_by(user_id=uid).count(),
        "تحويلات مخزون": StockTransfer.query.filter_by(user_id=uid).count(),
        "تسويات مخزون": InventoryAdjustment.query.filter_by(user_id=uid).count(),
        "خصومات عملاء": CustomerDiscount.query.filter_by(user_id=uid).count(),
        "طلبات موافقة": ApprovalRequest.query.filter(
            (ApprovalRequest.requested_by == uid) | (ApprovalRequest.reviewed_by == uid)
        ).count(),
    }
    blocking = {k: v for k, v in linked_counts.items() if v > 0}
    if blocking:
        details = "، ".join(f"{k}: {v}" for k, v in blocking.items())
        flash(f"لا يمكن حذف هذا المستخدم لأن له سجلًا فعليًا مرتبطًا به ({details}). "
              "قم بتعطيل حسابه من زر 'مفعّل/معطّل' بدل حذفه للحفاظ على سلامة التقارير والسجلات.", "danger")
        return redirect(url_for("users"))

    username = user.username
    log_action("delete_user", "user", uid, f"username={username}")
    db.session.delete(user)
    db.session.commit()
    flash(f"تم حذف المستخدم {username} نهائيًا.", "info")
    return redirect(url_for("users"))


# ---------------------------------------------------------------------------
# تشغيل التطبيق + إنشاء بيانات أولية + ترقية قاعدة بيانات قديمة (SQLite)
# ---------------------------------------------------------------------------
def migrate_sqlite_columns():
    """يضيف الأعمدة الجديدة إلى قاعدة بيانات موجودة مسبقًا دون حذف البيانات."""
    import sqlite3
    db_path = os.path.join(Config.BASE_DIR, "database", "erp.db")
    if not os.path.exists(db_path):
        return
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    def add_column_if_missing(table, column, coltype):
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        if not cur.fetchone():
            return
        cur.execute(f"PRAGMA table_info({table})")
        existing = [row[1] for row in cur.fetchall()]
        if column not in existing:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")

    try:
        add_column_if_missing("users", "can_override_price", "BOOLEAN DEFAULT 0")
        add_column_if_missing("users", "permission_data", "TEXT DEFAULT '[]'")
        add_column_if_missing("users", "cashbox_id", "INTEGER")
        add_column_if_missing("account_entries", "external_borrower_id", "INTEGER")
        add_column_if_missing("cashboxes", "default_warehouse_id", "INTEGER")
        add_column_if_missing("products", "length", "VARCHAR(30)")
        add_column_if_missing("customers", "credit_limit", "FLOAT DEFAULT 0")
        add_column_if_missing("invoices", "shift_id", "INTEGER")
        add_column_if_missing("invoices", "payment_method", "VARCHAR(20) DEFAULT 'cash'")
        add_column_if_missing("invoices", "cash_amount", "FLOAT DEFAULT 0")
        add_column_if_missing("invoices", "bank_amount", "FLOAT DEFAULT 0")
        add_column_if_missing("invoices", "bank_name", "VARCHAR(80)")
        add_column_if_missing("invoices", "credit_amount", "FLOAT DEFAULT 0")
        add_column_if_missing("invoices", "void_reason", "VARCHAR(250)")
        add_column_if_missing("shifts", "cashbox_id", "INTEGER")
        add_column_if_missing("suppliers", "currency_code", "VARCHAR(10) DEFAULT 'LYD'")
        add_column_if_missing("purchases", "currency_code", "VARCHAR(10) DEFAULT 'LYD'")
        add_column_if_missing("purchases", "exchange_rate", "FLOAT DEFAULT 1")
        add_column_if_missing("purchases", "foreign_total", "FLOAT DEFAULT 0")
        add_column_if_missing("purchases", "local_total", "FLOAT DEFAULT 0")
        add_column_if_missing("account_entries", "payment_method", "VARCHAR(20)")
        add_column_if_missing("account_entries", "cashbox_id", "INTEGER")
        add_column_if_missing("account_entries", "currency_code", "VARCHAR(10) DEFAULT 'LYD'")
        add_column_if_missing("account_entries", "exchange_rate", "FLOAT DEFAULT 1")
        add_column_if_missing("account_entries", "local_amount", "FLOAT DEFAULT 0")
        add_column_if_missing("account_entries", "foreign_amount", "FLOAT")
        # المراحل 2-3: حسابات بنكية/خزائن عملات، مصاريف الشراء الإضافية
        add_column_if_missing("account_entries", "fund_account_id", "INTEGER")
        add_column_if_missing("invoices", "fund_account_id", "INTEGER")
        add_column_if_missing("purchases", "extra_cost", "FLOAT DEFAULT 0")
        add_column_if_missing("purchases", "extra_cost_note", "VARCHAR(200)")
        add_column_if_missing("purchase_items", "extra_unit_cost", "FLOAT DEFAULT 0")
        add_column_if_missing("account_entries", "reference", "VARCHAR(120)")
        add_column_if_missing("account_entries", "scope", "VARCHAR(20) DEFAULT 'general'")
        add_column_if_missing("account_entries", "invoice_id", "INTEGER")
        add_column_if_missing("account_entries", "purchase_id", "INTEGER")
        add_column_if_missing("account_entries", "employee_id", "INTEGER")
        add_column_if_missing("approval_requests", "proposed_data", "TEXT")
        add_column_if_missing("cashboxes", "balance", "FLOAT DEFAULT 0")
        # Z.G ERP: الاستبدال + ربط المرتجعات بالوردية التي نُفّذت فيها
        add_column_if_missing("invoices", "exchange_credit", "FLOAT DEFAULT 0")
        add_column_if_missing("sales_returns", "shift_id", "INTEGER")
        add_column_if_missing("sales_returns", "exchange_invoice_id", "INTEGER")
        # الفحص الحسابي: تأثير المخزون، ترحيل النقد للخزينة لحظيًا، وتكلفة القطعة وقت البيع
        add_column_if_missing("invoices", "stock_affected", "BOOLEAN DEFAULT 1")
        add_column_if_missing("invoices", "cash_posted", "BOOLEAN DEFAULT 0")
        item_cols = [r[1] for r in cur.execute("PRAGMA table_info(invoice_items)").fetchall()]
        if item_cols and "unit_cost" not in item_cols:
            cur.execute("ALTER TABLE invoice_items ADD COLUMN unit_cost FLOAT")
            # الفواتير القديمة لا تُعرف تكلفتها الأصلية: تُثبَّت مرة واحدة بتكلفة المنتج الحالية
            cur.execute("UPDATE invoice_items SET unit_cost = (SELECT purchase_price FROM products "
                        "WHERE products.id = invoice_items.product_id) WHERE unit_cost IS NULL")
        # فواتير "بدون تأثير مخزون" القديمة: لا حركة مخزون مسجّلة لها
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='stock_movements'")
        if cur.fetchone():
            cur.execute("""UPDATE invoices SET stock_affected = 0 WHERE stock_affected = 1 AND shift_id IS NULL
                           AND id IN (SELECT id FROM invoices i WHERE NOT EXISTS (
                               SELECT 1 FROM stock_movements m WHERE m.reference = i.invoice_number
                                  OR m.reference = 'فاتورة قديمة ' || i.invoice_number))
                           AND EXISTS (SELECT 1 FROM audit_logs a WHERE a.action = 'import_old_sale' AND a.entity_id = invoices.id
                                       AND a.details LIKE '%تأثير المخزون=False%')""")
        # نقل البيانات القديمة: paid_amount/payment_type -> الأعمدة الجديدة إن وُجدت
        cur.execute("PRAGMA table_info(invoices)")
        cols = [row[1] for row in cur.fetchall()]
        if "payment_type" in cols and "paid_amount" in cols:
            cur.execute("SELECT id, payment_type, paid_amount, total FROM invoices WHERE cash_amount=0 AND bank_amount=0 AND credit_amount=0")
            for iid, ptype, paid, total in cur.fetchall():
                if ptype == "credit":
                    remaining = (total or 0) - (paid or 0)
                    cur.execute("UPDATE invoices SET payment_method='credit', cash_amount=?, credit_amount=? WHERE id=?",
                                (paid or 0, remaining, iid))
                else:
                    cur.execute("UPDATE invoices SET payment_method='cash', cash_amount=? WHERE id=?", (paid or 0, iid))
        conn.commit()
    finally:
        conn.close()


def init_db():
    with app.app_context():
        migrate_sqlite_columns()
        db.create_all()
        # V9 currencies and export defaults
        for code, name, symbol, local in [("LYD","دينار ليبي","د.ل",True),("USD","دولار أمريكي","$",False),("EUR","يورو","€",False),("GBP","جنيه إسترليني","£",False),
                                      ("EGP","جنيه مصري","ج.م",False),("CNY","يوان صيني","¥",False),
                                      ("TRY","ليرة تركية","₺",False),("AED","درهم إماراتي","د.إ",False)]:
            if not Currency.query.filter_by(code=code).first():
                db.session.add(Currency(code=code, name=name, symbol=symbol, is_local=local))
        Setting.set("export_path", Setting.get("export_path", os.path.join(Config.BASE_DIR, "exports")))
        Setting.set("local_currency", Setting.get("local_currency", "LYD"))
        db.session.commit()
        admin_existing = User.query.filter_by(username="admin").first()
        defaults = {
            "admin": list(PERMISSIONS.keys()),
            "manager": ["dashboard","pos","sales_view","sales_edit","sales_void","sales_exchange","inventory_view","products","categories","inventory_import",
                        "warehouses","purchases","transfers","customers","suppliers","employees","accounts","reports","financial_report",
                        "inventory_adjustment","shifts","approvals","cashboxes","backup","old_sales_import"],
            "cashier": ["dashboard","pos","sales_view","shifts","sales_exchange","inventory_view"]
        }
        changed = False
        for existing_user in User.query.all():
            if not existing_user.get_permissions():
                existing_user.set_permissions(defaults.get(existing_user.role, ["dashboard","pos"]))
                changed = True
        if changed:
            db.session.commit()
        if not admin_existing:
            admin = User(username="admin", full_name="مدير النظام", role="admin")
            admin.set_password("admin123")
            admin.set_permissions(list(PERMISSIONS.keys()))
            db.session.add(admin)
            db.session.add(Warehouse(name="المخزن الرئيسي", location="طرابلس"))
            db.session.add(Cashbox(name="الخزينة الرئيسية", location="الفرع الرئيسي"))
            db.session.commit()
            print("تم إنشاء مستخدم افتراضي: admin / admin123")


if __name__ == "__main__":
    os.makedirs(os.path.join(Config.BASE_DIR, "database"), exist_ok=True)
    init_db()
    from backup_service import start_auto_backup
    start_auto_backup(app, interval_minutes=int(os.getenv("BACKUP_INTERVAL_MINUTES", "60")))
    # وضع التطوير (debug) يُفعَّل فقط بطلب صريح: تشغيله على شبكة المحل يعرّض أداة تصحيح الأخطاء لأي جهاز متصل
    app.run(debug=os.getenv("ERP_DEBUG") == "1", host="0.0.0.0", port=5000)
