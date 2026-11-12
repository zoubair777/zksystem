from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
import secrets
import json

db = SQLAlchemy()


# ---------------------------------------------------------------------------
# المستخدمون
# role: admin (مالك/مدير عام) | manager (مدير فرع) | cashier (كاشير)
# can_override_price: صلاحية إضافية تُمنح لكاشير معيّن للتعديل اليدوي على السعر
# ---------------------------------------------------------------------------
class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(20), default="cashier")  # admin / manager / cashier
    can_override_price = db.Column(db.Boolean, default=False)
    permission_data = db.Column(db.Text, default="[]")  # صلاحيات مخصصة يحددها المالك يدويًا
    cashbox_id = db.Column(db.Integer, db.ForeignKey("cashboxes.id"), nullable=True)
    api_token = db.Column(db.String(64), unique=True)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def generate_token(self):
        self.api_token = secrets.token_hex(24)
        return self.api_token

    def has_permission(self, permission):
        if self.role == "admin":
            return True
        try:
            permissions = json.loads(self.permission_data or "[]")
        except (TypeError, ValueError):
            permissions = []
        return permission in permissions

    def set_permissions(self, permissions):
        self.permission_data = json.dumps(sorted(set(permissions or [])), ensure_ascii=False)

    def get_permissions(self):
        try:
            return json.loads(self.permission_data or "[]")
        except (TypeError, ValueError):
            return []

    def is_admin(self):
        return self.role == "admin"

    def is_manager_or_admin(self):
        return self.role in ("admin", "manager")



class Cashbox(db.Model):
    __tablename__ = "cashboxes"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    location = db.Column(db.String(200))
    active = db.Column(db.Boolean, default=True)
    balance = db.Column(db.Float, default=0)  # الرصيد النقدي الفعلي الحالي داخل هذا الصندوق/الخزنة
    # المخزن الافتراضي المرتبط بهذه الخزينة (مثلاً: خزينة المبيعات ↔ مخزن المعرض) - يُستخدم لتعبئة
    # شاشة نقطة البيع تلقائيًا بدل ما يضل الكاشير يختار المخزن يدويًا كل مرة وينسى يبدّله بعد نقل.
    default_warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    default_warehouse = db.relationship("Warehouse")


class CashTransfer(db.Model):
    """تحويل نقدي بين صندوقين (مثلاً: من صندوق البيع إلى الخزنة)، أو إيداع/سحب
    مباشر عندما يكون أحد الطرفين خارج النظام (من الجيب الشخصي، أو مصروف شخصي خارجي)."""
    __tablename__ = "cash_transfers"
    id = db.Column(db.Integer, primary_key=True)
    transfer_number = db.Column(db.String(40), unique=True, nullable=False)
    from_cashbox_id = db.Column(db.Integer, db.ForeignKey("cashboxes.id"), nullable=True)
    to_cashbox_id = db.Column(db.Integer, db.ForeignKey("cashboxes.id"), nullable=True)
    amount = db.Column(db.Float, nullable=False)
    note = db.Column(db.String(250))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    from_cashbox = db.relationship("Cashbox", foreign_keys=[from_cashbox_id])
    to_cashbox = db.relationship("Cashbox", foreign_keys=[to_cashbox_id])
    user = db.relationship("User")


class FundAccount(db.Model):
    """حساب مالي غير صندوق الكاشير: حساب بنكي، أو خزنة بعملة أجنبية (دولار مثلًا).
    الرصيد لا يُخزَّن بل يُحسب من الحركات (الرصيد الافتتاحي + كل ما دخل وخرج)، حتى لا يختلّ عند
    تعديل أو إلغاء فاتورة أو حركة: لا يوجد رقم مخزَّن يحتاج عكسًا في كل مسار."""
    __tablename__ = "fund_accounts"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    kind = db.Column(db.String(10), default="bank")  # bank / cash (خزنة عملة)
    currency_code = db.Column(db.String(10), default="LYD")
    opening_balance = db.Column(db.Float, default=0)
    # لحساب بعملة أجنبية: بكم دينار اشتُري الرصيد الافتتاحي (كم دينار = 1 وحدة)
    opening_rate = db.Column(db.Float, default=1)
    # الحساب الذي تدخله تلقائيًا المبيعات والحركات البنكية بالدينار إن لم يُحدَّد حساب آخر
    is_default = db.Column(db.Boolean, default=False)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class FundTransfer(db.Model):
    """تحويل بين خزينة/حساب وحساب آخر: إيداع نقد في البنك، سحب من البنك، أو صرف عملة (دينار ↔ دولار).
    from_amount بعملة المصدر و to_amount بعملة الوجهة؛ سعر الصرف = from ÷ to أو العكس حسب الاتجاه.
    طرف الخزينة النقدية يُسجَّل أيضًا كـ CashTransfer (رقم يبدأ بـ FND-) كي تحسبه الوردية ورصيد الخزينة."""
    __tablename__ = "fund_transfers"
    id = db.Column(db.Integer, primary_key=True)
    transfer_number = db.Column(db.String(40), unique=True, nullable=False)
    from_fund_id = db.Column(db.Integer, db.ForeignKey("fund_accounts.id"), nullable=True)
    to_fund_id = db.Column(db.Integer, db.ForeignKey("fund_accounts.id"), nullable=True)
    from_cashbox_id = db.Column(db.Integer, db.ForeignKey("cashboxes.id"), nullable=True)
    to_cashbox_id = db.Column(db.Integer, db.ForeignKey("cashboxes.id"), nullable=True)
    from_amount = db.Column(db.Float, nullable=False)
    to_amount = db.Column(db.Float, nullable=False)
    cash_transfer_id = db.Column(db.Integer, db.ForeignKey("cash_transfers.id"), nullable=True)
    note = db.Column(db.String(250))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    date = db.Column(db.DateTime, default=datetime.utcnow)

    from_fund = db.relationship("FundAccount", foreign_keys=[from_fund_id])
    to_fund = db.relationship("FundAccount", foreign_keys=[to_fund_id])
    from_cashbox = db.relationship("Cashbox", foreign_keys=[from_cashbox_id])
    to_cashbox = db.relationship("Cashbox", foreign_keys=[to_cashbox_id])
    user = db.relationship("User")

    def from_name(self):
        return self.from_fund.name if self.from_fund else (self.from_cashbox.name if self.from_cashbox else "-")

    def to_name(self):
        return self.to_fund.name if self.to_fund else (self.to_cashbox.name if self.to_cashbox else "-")

    def from_currency(self):
        return (self.from_fund.currency_code or "LYD") if self.from_fund else "LYD"

    def to_currency(self):
        return (self.to_fund.currency_code or "LYD") if self.to_fund else "LYD"


class Category(db.Model):
    __tablename__ = "categories"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    products = db.relationship("Product", backref="category", lazy=True)


class Warehouse(db.Model):
    __tablename__ = "warehouses"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    location = db.Column(db.String(200))


class Product(db.Model):
    __tablename__ = "products"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    barcode = db.Column(db.String(64), unique=True, index=True)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"))
    size = db.Column(db.String(20))
    length = db.Column(db.String(30))  # طول/قياس إضافي للمنتج، مناسب للأقمشة والملابس
    color = db.Column(db.String(40))
    unit = db.Column(db.String(20), default="قطعة")
    purchase_price = db.Column(db.Float, default=0)
    sale_price = db.Column(db.Float, default=0)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    stocks = db.relationship("Stock", backref="product", lazy=True)

    def total_quantity(self):
        return sum(s.quantity for s in self.stocks)

    def display_name(self):
        """الاسم مع الباركود: طلب المالك أن يظهر الباركود أينما ظهر اسم المنتج، لأن عدة منتجات
        قد تحمل نفس الاسم (مثل عدة "jacket") ولا يميّزها إلا الكود."""
        name = (self.name or "").strip()
        return f"{name} ({self.barcode})" if self.barcode else name


class Stock(db.Model):
    __tablename__ = "stocks"
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    quantity = db.Column(db.Integer, default=0)

    warehouse = db.relationship("Warehouse")

    __table_args__ = (db.UniqueConstraint("product_id", "warehouse_id", name="uq_product_warehouse"),)


class StockMovement(db.Model):
    __tablename__ = "stock_movements"
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    movement_type = db.Column(db.String(20))  # in / out / adjustment
    quantity = db.Column(db.Integer)
    reference = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    product = db.relationship("Product")
    warehouse = db.relationship("Warehouse")


# ---------------------------------------------------------------------------
# نقل البضاعة بين المخازن - تحويل كميات من مخزن مصدر إلى مخزن وجهة
# ---------------------------------------------------------------------------
class StockTransfer(db.Model):
    __tablename__ = "stock_transfers"
    id = db.Column(db.Integer, primary_key=True)
    transfer_number = db.Column(db.String(30), unique=True)
    from_warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    to_warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    note = db.Column(db.String(250))
    date = db.Column(db.DateTime, default=datetime.utcnow)

    from_warehouse = db.relationship("Warehouse", foreign_keys=[from_warehouse_id])
    to_warehouse = db.relationship("Warehouse", foreign_keys=[to_warehouse_id])
    user = db.relationship("User")
    items = db.relationship("StockTransferItem", backref="transfer", lazy=True, cascade="all, delete-orphan")

    def total_quantity(self):
        return sum(i.quantity for i in self.items)


class StockTransferItem(db.Model):
    __tablename__ = "stock_transfer_items"
    id = db.Column(db.Integer, primary_key=True)
    transfer_id = db.Column(db.Integer, db.ForeignKey("stock_transfers.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)

    product = db.relationship("Product")


# ---------------------------------------------------------------------------
# العملاء - مع سقف الائتمان (Credit Limit) لدعم الذمم المدينة
# ---------------------------------------------------------------------------
class Customer(db.Model):
    __tablename__ = "customers"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    phone = db.Column(db.String(30))
    address = db.Column(db.String(200))
    balance = db.Column(db.Float, default=0)        # موجب = دين مستحق على العميل (ذمم مدينة)
    credit_limit = db.Column(db.Float, default=0)    # 0 = بدون سقف ائتمان محدد

    def available_credit(self):
        if self.credit_limit <= 0:
            return None  # بدون حد أقصى
        return round(self.credit_limit - self.balance, 2)

    def would_exceed_limit(self, extra_amount):
        if self.credit_limit <= 0:
            return False
        return (self.balance + extra_amount) > self.credit_limit


class Supplier(db.Model):
    __tablename__ = "suppliers"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    phone = db.Column(db.String(30))
    address = db.Column(db.String(200))
    balance = db.Column(db.Float, default=0)  # موجب = مستحق للمورد
    currency_code = db.Column(db.String(10), default="LYD")

    debts = db.relationship("SupplierDebt", backref="supplier", lazy=True)


# ---------------------------------------------------------------------------
# ديون قديمة على الموردين (بدون فاتورة مشتريات) - تُسجَّل يدويًا لمتابعة
# الاستحقاق والسداد. عند الإضافة تُزاد قيمتها على رصيد المورد (balance)،
# وعند تسجيل سداد جزئي/كامل يُخصم من الرصيد ومن قيمة هذا السند تحديدًا.
# status: unpaid (غير مسدد) / partial (مسدد جزئيًا) / paid (مسدد بالكامل)
# ---------------------------------------------------------------------------
class SupplierDebt(db.Model):
    __tablename__ = "supplier_debts"
    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    paid_amount = db.Column(db.Float, default=0)
    due_date = db.Column(db.Date, nullable=True)
    note = db.Column(db.String(300))
    status = db.Column(db.String(15), default="unpaid")  # unpaid / partial / paid
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    creator = db.relationship("User")

    def remaining(self):
        return round(self.amount - self.paid_amount, 2)

    def is_overdue(self):
        return bool(self.due_date) and self.due_date < datetime.utcnow().date() and self.remaining() > 0

    def refresh_status(self):
        if self.paid_amount <= 0:
            self.status = "unpaid"
        elif self.remaining() <= 0.0001:
            self.status = "paid"
        else:
            self.status = "partial"


# ---------------------------------------------------------------------------
# الورديات (Shift Management) - لمطابقة نقدية الصندوق آخر كل وردية
# ---------------------------------------------------------------------------
class Shift(db.Model):
    __tablename__ = "shifts"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    cashbox_id = db.Column(db.Integer, db.ForeignKey("cashboxes.id"), nullable=True)
    opened_at = db.Column(db.DateTime, default=datetime.utcnow)
    closed_at = db.Column(db.DateTime, nullable=True)
    opening_cash = db.Column(db.Float, default=0)
    counted_cash = db.Column(db.Float, nullable=True)     # النقد الفعلي المعدود عند الإغلاق
    note = db.Column(db.String(250))
    status = db.Column(db.String(15), default="open")     # open / closed

    user = db.relationship("User")
    cashbox = db.relationship("Cashbox")
    invoices = db.relationship("Invoice", backref="shift", lazy=True)

    def cash_sales(self):
        return sum(i.cash_amount for i in self.invoices if i.status != "voided")

    def bank_sales(self):
        return sum(i.bank_amount for i in self.invoices if i.status != "voided")

    def credit_sales(self):
        return sum(i.credit_amount for i in self.invoices if i.status != "voided")

    def total_sales(self):
        return sum(i.total for i in self.invoices if i.status != "voided")

    def invoices_count(self):
        return len([i for i in self.invoices if i.status != "voided"])

    def _period_entries(self):
        """كل الحركات المالية المرتبطة بنفس خزينة هذه الوردية تحديدًا (بغض النظر عن أي مستخدم
        سجّلها)، خلال فترة الوردية (من وقت الفتح حتى الإغلاق أو الآن إن كانت مفتوحة).
        مهم: التصفية هنا على الخزينة (cashbox_id) لا على المستخدم - لأن حركة سجّلها مدير على
        خزينة أخرى (مثلاً سحب من "الخزينة الرئيسية") لا يجوز أن تُحتسب ضمن وردية كاشير شغّال
        على "خزينة المبيعات"، حتى لو كانت بنفس التوقيت أو نفس المستخدم."""
        if not self.cashbox_id:
            return []
        end = self.closed_at or datetime.utcnow()
        return AccountEntry.query.filter(
            AccountEntry.cashbox_id == self.cashbox_id,
            AccountEntry.date >= self.opened_at,
            AccountEntry.date <= end,
        ).all()

    # نفس خريطة الأثر النقدي المستخدمة في app.py (CASHBOX_CASH_EFFECT) لحساب رصيد الخزينة -
    # مكرّرة هنا عمدًا لتفادي استيراد دائري بين models.py و app.py. أي تعديل على اتجاه نوع
    # حركة في أحد الملفين يجب أن يُعدَّل في الآخر معه.
    _CASH_EFFECT = {
        "income": 1, "expense": -1, "customer_payment": 1, "supplier_payment": -1,
        "employee_advance": -1, "employee_repayment": 1, "employee_draw": -1,
        "external_advance": -1, "external_repayment": 1, "salary_payment": -1,
        "owner_withdrawal": -1, "loan_received": 1, "loan_repayment": -1, "sales_return": -1,
        "purchase_cost": -1,
    }

    def collected_from_customers(self):
        return sum(e.amount for e in self._period_entries() if e.entry_type == "customer_payment")

    def collected_cash(self):
        return sum(e.amount for e in self._period_entries()
                   if e.entry_type == "customer_payment" and e.payment_method == "cash")

    def other_income(self):
        return sum(e.amount for e in self._period_entries() if e.entry_type == "income")

    def other_income_cash(self):
        return sum(e.amount for e in self._period_entries()
                   if e.entry_type == "income" and e.payment_method == "cash")

    def expenses_list(self):
        return [e for e in self._period_entries() if e.entry_type == "expense"]

    def total_expenses(self):
        return sum(e.amount for e in self.expenses_list())

    def total_expenses_cash(self):
        return sum(e.amount for e in self.expenses_list() if e.payment_method == "cash")

    def supplier_payments_cash(self):
        return sum(e.amount for e in self._period_entries()
                   if e.entry_type == "supplier_payment" and e.payment_method == "cash")

    def total_receipts(self):
        """إجمالي المقبوضات خلال الوردية: مبيعات نقدية/بنكية + تحصيل من عملاء + إيرادات أخرى."""
        return round(self.cash_sales() + self.bank_sales() + self.collected_from_customers() + self.other_income(), 2)

    def operations_count(self):
        return self.invoices_count() + len(self._period_entries())

    def other_cash_movements(self):
        """صافي أي حركة نقدية أخرى مرتبطة بنفس خزينة الوردية (سلف/سحوبات/تسديدات موظفين أو
        أشخاص خارجيين، دفع رواتب، سحب مالك...) غير المحتسبة في الدوال الأخرى أعلاه - موجب إن
        دخلت الصندوق، سالب إن خرجت منه."""
        counted_types = {"customer_payment", "income", "expense", "supplier_payment", "sales_return"}
        total = 0.0
        for e in self._period_entries():
            if e.entry_type in counted_types or e.payment_method != "cash":
                continue
            total += self._CASH_EFFECT.get(e.entry_type, 0) * e.amount
        return round(total, 2)

    def _period_transfers(self):
        """التحويلات النقدية بين الخزائن التي لمست خزينة هذه الوردية خلال فترتها."""
        if not self.cashbox_id:
            return []
        end = self.closed_at or datetime.utcnow()
        return CashTransfer.query.filter(
            db.or_(CashTransfer.to_cashbox_id == self.cashbox_id,
                   CashTransfer.from_cashbox_id == self.cashbox_id),
            CashTransfer.created_at >= self.opened_at,
            CashTransfer.created_at <= end,
            # فروقات العدّ عند فتح/إغلاق الوردية (CLS) نتيجة للمطابقة وليست حركة داخلها
            db.not_(CashTransfer.transfer_number.like("CLS-%")),
        ).all()

    def transfers_in(self):
        """مبالغ محوّلة إلى خزينة الوردية (مثلاً من الخزينة الرئيسية) أثناء الوردية."""
        return round(sum(t.amount for t in self._period_transfers()
                         if t.to_cashbox_id == self.cashbox_id), 2)

    def transfers_out(self):
        """مبالغ محوّلة من خزينة الوردية إلى خزينة أخرى أثناء الوردية."""
        return round(sum(t.amount for t in self._period_transfers()
                         if t.from_cashbox_id == self.cashbox_id), 2)

    def returns_total(self):
        """إجمالي مرتجعات فواتير هذه الوردية (للعرض؛ الأثر النقدي محسوب ضمن other_cash_movements)."""
        ids = [i.id for i in self.invoices]
        if not ids:
            return 0.0
        return round(db.session.query(db.func.coalesce(db.func.sum(SalesReturn.total), 0))
                     .filter(SalesReturn.invoice_id.in_(ids)).scalar() or 0, 2)

    # ---------------- المرتجعات والاستبدال المنفَّذة خلال الوردية ----------------
    def period_returns(self):
        """عمليات الإرجاع/الاستبدال التي نُفّذت *في* هذه الوردية (لا مرتجعات فواتيرها فقط)، لأن العميل
        قد يُرجع صباحًا بضاعة اشتراها أمس. السجلات القديمة (قبل إضافة shift_id) تُطابَق بالمستخدم والفترة."""
        end = self.closed_at or datetime.utcnow()
        return SalesReturn.query.filter(db.or_(
            SalesReturn.shift_id == self.id,
            db.and_(SalesReturn.shift_id.is_(None), SalesReturn.user_id == self.user_id,
                    SalesReturn.date >= self.opened_at, SalesReturn.date <= end),
        )).order_by(SalesReturn.date).all()

    def pure_returns(self):
        return [r for r in self.period_returns() if not r.is_exchange()]

    def exchanges(self):
        return [r for r in self.period_returns() if r.is_exchange()]

    def pure_returns_total(self):
        return round(sum(r.total or 0 for r in self.pure_returns()), 2)

    def exchange_returned_total(self):
        """قيمة البضاعة التي أعادها العملاء في عمليات الاستبدال."""
        return round(sum(r.total or 0 for r in self.exchanges()), 2)

    def exchange_new_total(self):
        """قيمة البضاعة الجديدة التي أخذها العملاء في عمليات الاستبدال."""
        return round(sum((r.exchange_invoice.total or 0) for r in self.exchanges() if r.exchange_invoice), 2)

    def exchange_collected(self):
        """فروقات استبدال دفعها العملاء (البضاعة الجديدة أغلى)."""
        return round(sum(max(r.exchange_difference(), 0) for r in self.exchanges()), 2)

    def exchange_refunded(self):
        """فروقات استبدال رُدّت للعملاء (البضاعة الجديدة أرخص)."""
        return round(sum(max(-r.exchange_difference(), 0) for r in self.exchanges()), 2)

    def all_returns_value(self):
        return round(self.pure_returns_total() + self.exchange_returned_total(), 2)

    def net_sales(self):
        """صافي المبيعات للوردية = إجمالي الفواتير - كل ما أُرجع خلالها (إرجاع عادي + بضاعة استبدال)."""
        return round(self.total_sales() - self.all_returns_value(), 2)

    def refunds_by_method(self):
        """ما خرج فعليًا للعملاء خلال الوردية مقسّمًا حسب الطريقة (من قيود sales_return في الحسابات)."""
        out = {"cash": 0.0, "bank": 0.0, "customer_credit": 0.0}
        for r in self.period_returns():
            entry = AccountEntry.query.filter_by(entry_type="sales_return", reference=r.return_number).first()
            if not entry:
                continue
            if entry.payment_method in ("cash", "bank"):
                out[entry.payment_method] += entry.amount or 0
            elif entry.customer_id:
                out["customer_credit"] += entry.amount or 0
        return {k: round(v, 2) for k, v in out.items()}

    def exchange_credit_used(self):
        return round(sum(i.exchange_credit or 0 for i in self.invoices if i.status != "voided"), 2)

    def items_summary(self):
        """ملخص الأصناف المباعة في الوردية: [(المنتج, الكمية, القيمة)] مرتبة بالكمية."""
        rows = {}
        for inv in self.invoices:
            if inv.status == "voided":
                continue
            for it in inv.items:
                key = it.product_id
                if key not in rows:
                    rows[key] = [it.product, 0, 0.0]
                rows[key][1] += it.quantity or 0
                rows[key][2] += it.total or 0
        return sorted(([p, q, round(t, 2)] for p, q, t in rows.values()), key=lambda r: -r[1])

    def pieces_sold(self):
        return sum(r[1] for r in self.items_summary())

    def pieces_returned(self):
        return sum(ri.quantity or 0 for r in self.period_returns() for ri in r.items)

    def refunds_cash(self):
        """مبالغ رُدّت نقدًا من صندوق هذه الوردية للعملاء (إرجاع أو فرق استبدال)."""
        return round(sum(e.amount for e in self._period_entries()
                         if e.entry_type == "sales_return" and e.payment_method == "cash"), 2)

    def expected_cash(self):
        """الرصيد النقدي المتوقع بالصندوق: النقد الافتتاحي + مبيعات نقدية + تحصيلات نقدية
        + إيرادات نقدية أخرى - مصروفات نقدية - مدفوعات موردين نقدية ± أي حركة نقدية أخرى
        مرتبطة بنفس خزينة الوردية (سلف، سحوبات، رواتب...) + ما حُوّل إلى الخزينة أثناء الوردية
        - ما حُوّل منها (التحويل بين الخزائن لم يكن محسوبًا سابقًا فكان يظهر كفائض/عجز)."""
        return round(self.opening_cash + self.cash_sales() + self.collected_cash() + self.other_income_cash()
                     - self.total_expenses_cash() - self.supplier_payments_cash()
                     - self.refunds_cash() + self.other_cash_movements()
                     + self.transfers_in() - self.transfers_out(), 2)

    def difference(self):
        if self.counted_cash is None:
            return None
        return round(self.counted_cash - self.expected_cash(), 2)


# ---------------------------------------------------------------------------
# فواتير المبيعات - تدعم الدفع المزدوج: نقدي + بنكي + آجل في نفس الفاتورة
# payment_method: cash / bank / credit / split
# status: completed / void_requested / voided
# ---------------------------------------------------------------------------
class Invoice(db.Model):
    __tablename__ = "invoices"
    id = db.Column(db.Integer, primary_key=True)
    invoice_number = db.Column(db.String(30), unique=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    shift_id = db.Column(db.Integer, db.ForeignKey("shifts.id"), nullable=True)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    subtotal = db.Column(db.Float, default=0)
    discount = db.Column(db.Float, default=0)
    total = db.Column(db.Float, default=0)

    payment_method = db.Column(db.String(20), default="cash")  # cash / bank / credit / split
    cash_amount = db.Column(db.Float, default=0)
    bank_amount = db.Column(db.Float, default=0)
    bank_name = db.Column(db.String(80))
    credit_amount = db.Column(db.Float, default=0)
    # قيمة البضاعة المُرجَعة التي استُخدمت كرصيد لدفع هذه الفاتورة (فاتورة استبدال فقط)
    exchange_credit = db.Column(db.Float, default=0)
    # هل خُصمت أصناف الفاتورة من المخزون؟ (فاتورة قديمة أُدخلت "بدون تأثير مخزون" = False)
    stock_affected = db.Column(db.Boolean, default=True)
    # هل أُضيف نقدها لرصيد الخزينة لحظة البيع؟ الفواتير قبل هذا التحديث = False (رصيدها ضُبط عند الإغلاق)
    cash_posted = db.Column(db.Boolean, default=False)
    # الحساب البنكي الذي دخله الجزء البنكي من الفاتورة (يُعبّأ تلقائيًا بالحساب الافتراضي)
    fund_account_id = db.Column(db.Integer, db.ForeignKey("fund_accounts.id"), nullable=True)

    status = db.Column(db.String(20), default="completed")  # completed / void_requested / voided
    void_reason = db.Column(db.String(250))

    customer = db.relationship("Customer")
    warehouse = db.relationship("Warehouse")
    user = db.relationship("User")
    items = db.relationship("InvoiceItem", backref="invoice", lazy=True, cascade="all, delete-orphan")

    def returned_total(self):
        """إجمالي قيمة المرتجعات المسجّلة على هذه الفاتورة."""
        if not self.id:
            return 0.0
        return round(db.session.query(db.func.coalesce(db.func.sum(SalesReturn.total), 0))
                     .filter(SalesReturn.invoice_id == self.id).scalar() or 0, 2)

    def net_total(self):
        """صافي الفاتورة بعد خصم المرتجعات (الإجمالي الأصلي يبقى محفوظًا كما هو للتقارير)."""
        return round((self.total or 0) - self.returned_total(), 2)

    def exchange_source(self):
        """عملية الإرجاع التي نتجت عنها هذه الفاتورة إن كانت فاتورة استبدال، وإلا None."""
        if not self.id or not self.exchange_credit:
            return None
        return SalesReturn.query.filter_by(exchange_invoice_id=self.id).first()

    def returns_list(self):
        """كل عمليات الإرجاع/الاستبدال المسجّلة على هذه الفاتورة (للعرض في الفاتورة المطبوعة)."""
        if not self.id:
            return []
        return SalesReturn.query.filter_by(invoice_id=self.id).order_by(SalesReturn.date).all()

    @property
    def paid_amount(self):
        return round(self.cash_amount + self.bank_amount, 2)

    def remaining(self):
        return round(self.credit_amount, 2)

    def payment_label(self):
        labels = {"cash": "نقدًا", "bank": "بنكي/تحويل", "credit": "آجل", "split": "دفع مزدوج",
                  "exchange": "استبدال"}
        return labels.get(self.payment_method, self.payment_method)


class InvoiceItem(db.Model):
    __tablename__ = "invoice_items"
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Float, nullable=False)
    total = db.Column(db.Float, nullable=False)
    # تكلفة القطعة لحظة البيع (بالدينار) - أساس حساب الربح، حتى لا يتغير ربح بيعة قديمة إذا تغيّرت تكلفة المنتج لاحقًا
    unit_cost = db.Column(db.Float, nullable=True)

    product = db.relationship("Product")

    def cost_each(self):
        if self.unit_cost is not None:
            return self.unit_cost
        return (self.product.purchase_price or 0) if self.product else 0

    def returned_quantity(self):
        """مجموع ما أُرجع فعلاً من هذا السطر عبر كل عمليات الإرجاع المسجَّلة ضده."""
        return sum(ri.quantity for ri in self.return_items)

    def returnable_quantity(self):
        """الكمية المتبقية القابلة للإرجاع من هذا السطر (الأصلية - المُرجَع سابقًا)."""
        return self.quantity - self.returned_quantity()


class SalesReturn(db.Model):
    """إرجاع (كلي أو جزئي) لفاتورة بيع - منفصل تمامًا عن إلغاء الفاتورة: يسمح بإرجاع أصناف
    محددة بكميات جزئية، ويعيد الفلوس فعليًا (نقدًا/بنكًا من خزينة محددة، أو كخصم من دين العميل)،
    بخلاف الإلغاء الذي يبطل الفاتورة كاملة بدون التعامل مع الفلوس المدفوعة."""
    __tablename__ = "sales_returns"
    id = db.Column(db.Integer, primary_key=True)
    return_number = db.Column(db.String(30), unique=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    reason = db.Column(db.String(250))
    refund_method = db.Column(db.String(20))  # cash / bank / customer_credit / exchange
    cashbox_id = db.Column(db.Integer, db.ForeignKey("cashboxes.id"), nullable=True)
    # الوردية التي نُفّذ فيها الإرجاع (قد تختلف عن وردية الفاتورة الأصلية) - أساس تقرير الوردية
    shift_id = db.Column(db.Integer, db.ForeignKey("shifts.id"), nullable=True)
    # في الاستبدال: فاتورة البضاعة الجديدة التي أخذها العميل مقابل المُرجَع
    exchange_invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=True)
    total = db.Column(db.Float, default=0)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    date = db.Column(db.DateTime, default=datetime.utcnow)

    invoice = db.relationship("Invoice", foreign_keys=[invoice_id])
    exchange_invoice = db.relationship("Invoice", foreign_keys=[exchange_invoice_id])
    cashbox = db.relationship("Cashbox")
    user = db.relationship("User")
    items = db.relationship("SalesReturnItem", backref="sales_return", lazy=True, cascade="all, delete-orphan")


    def is_exchange(self):
        return self.refund_method == "exchange"

    def exchange_difference(self):
        """في الاستبدال: موجب = دفعه العميل، سالب = رُدّ للعميل، صفر = تبديل متكافئ."""
        if not self.is_exchange() or not self.exchange_invoice:
            return 0.0
        return round((self.exchange_invoice.total or 0) - (self.total or 0), 2)


class SalesReturnItem(db.Model):
    __tablename__ = "sales_return_items"
    id = db.Column(db.Integer, primary_key=True)
    sales_return_id = db.Column(db.Integer, db.ForeignKey("sales_returns.id"), nullable=False)
    invoice_item_id = db.Column(db.Integer, db.ForeignKey("invoice_items.id"), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Float, nullable=False)
    total = db.Column(db.Float, nullable=False)

    invoice_item = db.relationship("InvoiceItem", backref=db.backref("return_items", cascade="all, delete-orphan"))


class Purchase(db.Model):
    __tablename__ = "purchases"
    id = db.Column(db.Integer, primary_key=True)
    purchase_number = db.Column(db.String(30), unique=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"))
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    date = db.Column(db.DateTime, default=datetime.utcnow)
    total = db.Column(db.Float, default=0)
    paid_amount = db.Column(db.Float, default=0)
    currency_code = db.Column(db.String(10), default="LYD")
    exchange_rate = db.Column(db.Float, default=1)
    foreign_total = db.Column(db.Float, default=0)
    local_total = db.Column(db.Float, default=0)
    # مصاريف إضافية بالدينار (شحن، جمارك، عمولة) تُوزَّع على تكلفة القطع ولا تدخل في حساب المورد
    extra_cost = db.Column(db.Float, default=0)
    extra_cost_note = db.Column(db.String(200))

    supplier = db.relationship("Supplier")
    warehouse = db.relationship("Warehouse")
    items = db.relationship("PurchaseItem", backref="purchase", lazy=True, cascade="all, delete-orphan")
    returns = db.relationship("PurchaseReturn", backref="purchase", lazy=True)


class PurchaseItem(db.Model):
    __tablename__ = "purchase_items"
    id = db.Column(db.Integer, primary_key=True)
    purchase_id = db.Column(db.Integer, db.ForeignKey("purchases.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    unit_cost = db.Column(db.Float, nullable=False)
    total = db.Column(db.Float, nullable=False)
    # نصيب القطعة الواحدة من المصاريف الإضافية (بالدينار). تكلفة القطعة = unit_cost × سعر الصرف + هذا
    extra_unit_cost = db.Column(db.Float, default=0)

    product = db.relationship("Product")

    def returned_quantity(self):
        return sum(ri.quantity or 0 for ri in self.return_items)

    def returnable_quantity(self):
        return (self.quantity or 0) - self.returned_quantity()


class PurchaseReturn(db.Model):
    """إرجاع بضاعة للمورد من فاتورة شراء: تخرج من المخزون بتكلفة شرائها ويُخصم ثمنها من حساب المورد."""
    __tablename__ = "purchase_returns"
    id = db.Column(db.Integer, primary_key=True)
    return_number = db.Column(db.String(30), unique=True)
    purchase_id = db.Column(db.Integer, db.ForeignKey("purchases.id"), nullable=False)
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"), nullable=True)
    total = db.Column(db.Float, default=0)        # بعملة الفاتورة
    local_total = db.Column(db.Float, default=0)  # بالدينار بسعر الفاتورة
    reason = db.Column(db.String(250))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    date = db.Column(db.DateTime, default=datetime.utcnow)

    supplier = db.relationship("Supplier")
    user = db.relationship("User")
    items = db.relationship("PurchaseReturnItem", backref="purchase_return", lazy=True, cascade="all, delete-orphan")


class PurchaseReturnItem(db.Model):
    __tablename__ = "purchase_return_items"
    id = db.Column(db.Integer, primary_key=True)
    purchase_return_id = db.Column(db.Integer, db.ForeignKey("purchase_returns.id"), nullable=False)
    purchase_item_id = db.Column(db.Integer, db.ForeignKey("purchase_items.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    unit_cost = db.Column(db.Float, nullable=False)
    total = db.Column(db.Float, nullable=False)

    product = db.relationship("Product")
    purchase_item = db.relationship("PurchaseItem", backref=db.backref("return_items"))


class Employee(db.Model):
    __tablename__ = "employees"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(30))
    position = db.Column(db.String(80))       # الوظيفة/المنصب
    salary = db.Column(db.Float, default=0)   # الراتب الأساسي (مرجعي فقط)
    balance = db.Column(db.Float, default=0)  # صافي السلف المستحقة على الموظف (لم تُسدد/تُخصم بعد)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def advance_limit(self):
        """الحد الأقصى المسموح لسلفة هذا الموظف (كمضاعف من راتبه الأساسي)، أو None إن لم يكن للراتب قيمة.
        المضاعف قابل للتعديل من الإعدادات (advance_limit_multiplier)، والافتراضي = 1 (سلفة لا تتجاوز راتب شهر واحد)."""
        if not self.salary or self.salary <= 0:
            return None
        try:
            multiplier = float(Setting.get("advance_limit_multiplier", "1") or 1)
        except (TypeError, ValueError):
            multiplier = 1
        return round(self.salary * multiplier, 2)

    def would_exceed_advance_limit(self, amount):
        """True إذا كانت إضافة amount لسلفة الموظف الحالية ستتجاوز سقف السلفة المسموح."""
        limit = self.advance_limit()
        if limit is None:
            return False
        return round((self.balance or 0) + amount, 2) > limit


class ExternalBorrower(db.Model):
    """شخص أو جهة من خارج النظام (ليس موظفًا ولا عميلاً ولا موردًا) - قد يكون مدينًا للمحل (أخذ
    سلفة) أو دائنًا للمحل (المحل أخذ قرضًا/سلفة منه، مثل بنك أو شخص). balance موجب = الجهة مدينة
    للمحل (مستحق لنا). balance سالب = المحل مدين للجهة (مستحق علينا، قرض أخذه المحل)."""
    __tablename__ = "external_borrowers"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(30))
    note = db.Column(db.String(250))
    balance = db.Column(db.Float, default=0)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AccountEntry(db.Model):
    __tablename__ = "account_entries"
    id = db.Column(db.Integer, primary_key=True)
    entry_type = db.Column(db.String(30))  # income / expense / customer_payment / customer_debt / supplier_payment
    amount = db.Column(db.Float, nullable=False)
    note = db.Column(db.String(250))
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"), nullable=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=True)
    external_borrower_id = db.Column(db.Integer, db.ForeignKey("external_borrowers.id"), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    date = db.Column(db.DateTime, default=datetime.utcnow)
    payment_method = db.Column(db.String(20))  # cash / bank - خانة "بنكي أو نقد" فقط، بدون ربط بحساب بنكي فعلي
    cashbox_id = db.Column(db.Integer, db.ForeignKey("cashboxes.id"), nullable=True)
    scope = db.Column(db.String(20), default="general")  # general / invoice_only
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=True)
    purchase_id = db.Column(db.Integer, db.ForeignKey("purchases.id"), nullable=True)
    currency_code = db.Column(db.String(10), default="LYD")
    exchange_rate = db.Column(db.Float, default=1)
    local_amount = db.Column(db.Float, default=0)
    # دفعة مورد: ما وصل المورد بعملته بالضبط (مثلاً 1000 دولار). amount يبقى ما خرج بالدينار.
    # يُحفظ الرقم نفسه بدل استنتاجه من القسمة على السعر، حتى لا يضيع المبلغ الأجنبي بالتقريب.
    foreign_amount = db.Column(db.Float, nullable=True)
    # الحساب البنكي/خزنة العملة التي دخلها أو خرج منها المبلغ (للحركات غير النقدية)
    fund_account_id = db.Column(db.Integer, db.ForeignKey("fund_accounts.id"), nullable=True)
    reference = db.Column(db.String(120))

    def supplier_amount(self):
        """قيمة دفعة المورد بعملة المورد. القيود القديمة (قبل foreign_amount) تُستنتج من السعر."""
        if self.foreign_amount is not None:
            return self.foreign_amount
        return round((self.amount or 0) / (self.exchange_rate or 1), 4)

    customer = db.relationship("Customer")
    supplier = db.relationship("Supplier")
    employee = db.relationship("Employee")
    external_borrower = db.relationship("ExternalBorrower")
    invoice = db.relationship("Invoice")
    purchase = db.relationship("Purchase")


# ---------------------------------------------------------------------------
# طلبات الموافقة (Approval Workflow) - إلغاء/تعديل فاتورة يحتاج موافقة المالك
# ---------------------------------------------------------------------------
class ApprovalRequest(db.Model):
    __tablename__ = "approval_requests"
    id = db.Column(db.Integer, primary_key=True)
    request_type = db.Column(db.String(30))  # void_invoice / edit_invoice
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    requested_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    reason = db.Column(db.String(300))
    status = db.Column(db.String(15), default="pending")  # pending / approved / rejected
    reviewed_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    review_note = db.Column(db.String(300))
    proposed_data = db.Column(db.Text, nullable=True)  # بيانات تعديل الفاتورة بانتظار الموافقة
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    reviewed_at = db.Column(db.DateTime, nullable=True)

    invoice = db.relationship("Invoice")
    requester = db.relationship("User", foreign_keys=[requested_by])
    reviewer = db.relationship("User", foreign_keys=[reviewed_by])


# ---------------------------------------------------------------------------
# سجل الأنشطة (Audit Log) - من فعل ماذا ومتى
# ---------------------------------------------------------------------------
class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    action = db.Column(db.String(60))          # login / create_invoice / void_invoice / edit_price / ...
    entity_type = db.Column(db.String(40))     # invoice / product / customer / user / shift ...
    entity_id = db.Column(db.Integer)
    details = db.Column(db.String(400))
    ip_address = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User")


# ---------------------------------------------------------------------------
# سجل عمليات النسخ الاحتياطي إلى Google Drive - لعرض آخر نسخة وحالتها
# ---------------------------------------------------------------------------
class BackupLog(db.Model):
    __tablename__ = "backup_logs"
    id = db.Column(db.Integer, primary_key=True)
    ok = db.Column(db.Boolean, default=False)
    message = db.Column(db.String(300))
    sha256 = db.Column(db.String(64), nullable=True)
    triggered_by = db.Column(db.String(20), default="auto")  # auto / manual
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ---------------------------------------------------------------------------
# إعدادات النظام - مخزن مفتاح/قيمة لإعدادات قابلة للتعديل من الواجهة
# مثال: company_name, invoice_footer, invoice_header_note, ...
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# V9: العملات والحسابات البنكية والسندات والخصومات وتسويات المخزون
# ---------------------------------------------------------------------------
class Currency(db.Model):
    __tablename__ = "currencies"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(10), unique=True, nullable=False)
    name = db.Column(db.String(80), nullable=False)
    symbol = db.Column(db.String(20))
    is_local = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)
    decimal_places = db.Column(db.Integer, default=2)


class CustomerDiscount(db.Model):
    __tablename__ = "customer_discounts"
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"))
    amount = db.Column(db.Float, nullable=False)
    reason = db.Column(db.String(300), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    customer = db.relationship("Customer")
    invoice = db.relationship("Invoice")
    user = db.relationship("User")


class InventoryAdjustment(db.Model):
    __tablename__ = "inventory_adjustments"
    id = db.Column(db.Integer, primary_key=True)
    adjustment_number = db.Column(db.String(40), unique=True, nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    old_quantity = db.Column(db.Integer, nullable=False)
    new_quantity = db.Column(db.Integer, nullable=False)
    difference = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(300), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    product = db.relationship("Product")
    warehouse = db.relationship("Warehouse")
    user = db.relationship("User")


class Setting(db.Model):
    __tablename__ = "settings"
    id    = db.Column(db.Integer, primary_key=True)
    key   = db.Column(db.String(80), unique=True, nullable=False)
    value = db.Column(db.Text, default="")

    @staticmethod
    def get(key, default=""):
        row = Setting.query.filter_by(key=key).first()
        return row.value if row else default

    @staticmethod
    def set(key, value):
        row = Setting.query.filter_by(key=key).first()
        if row:
            row.value = value
        else:
            db.session.add(Setting(key=key, value=value))
