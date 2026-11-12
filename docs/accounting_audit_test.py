"""فحص حسابي شامل (35 فحصًا): يشغّل كل عملية مالية ومخزنية بأرقام حقيقية ويقارن النتيجة بالقيمة الصحيحة.
يعمل على نسخة مؤقتة ولا يلمس بياناتك. التشغيل: python docs\\accounting_audit_test.py
"""
import sys, os, shutil
from datetime import datetime, timedelta

import tempfile

os.environ["ERP_TZ_OFFSET_HOURS"] = "2"  # محاكاة جهاز في ليبيا
# الأمان: الاختبار يعمل دائمًا على نسخة مؤقتة من الكود بقاعدة بيانات فارغة جديدة، ولا يلمس
# مجلد database أو backups الحقيقي أبدًا. التشغيل من مجلد المنظومة:
#     python docs\accounting_audit_test.py
SRC = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), ".."))
ROOT = tempfile.mkdtemp(prefix="zg_audit_")
shutil.copytree(SRC, ROOT, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(".git", "database", "backups", "logs", "exports", "__pycache__", "*.db"))
os.makedirs(os.path.join(ROOT, "database"))
print(f"الاختبار يعمل على نسخة مؤقتة: {ROOT}")
sys.path.insert(0, ROOT)
import app as m
from models import *

m.init_db()
a = m.app
results = []


def check(sid, title, expected, actual, note=""):
    ok = (abs(expected - actual) < 0.005) if isinstance(expected, (int, float)) and isinstance(actual, (int, float)) else expected == actual
    results.append((sid, ok, title, expected, actual, note))


with a.app_context():
    wh = Warehouse.query.first(); box = Cashbox.query.first(); box.default_warehouse_id = wh.id
    wh2 = Warehouse(name="مخزن 2"); db.session.add(wh2)
    def P(name, bc, price, cost, qty, w=None):
        p = Product(name=name, barcode=bc, sale_price=price, purchase_price=cost); db.session.add(p); db.session.flush()
        db.session.add(Stock(product_id=p.id, warehouse_id=(w or wh).id, quantity=qty)); return p.id
    A = P("قميص", "A", 100, 60, 50); B = P("بنطلون", "B", 150, 90, 50); C = P("جاكيت", "C", 200, 120, 50)
    cust = Customer(name="زبون آجل", credit_limit=100000); db.session.add(cust)
    sup_usd = Supplier(name="مورد تركي", currency_code="USD"); db.session.add(sup_usd)
    emp = Employee(name="موظف", salary=1000); db.session.add(emp)
    lender = ExternalBorrower(name="مصرف"); db.session.add(lender)
    db.session.commit()
    WH, WH2, BOX, CUST, SUP, EMP, LEND = wh.id, wh2.id, box.id, cust.id, sup_usd.id, emp.id, lender.id

c = a.test_client()
c.post("/login", data={"username": "admin", "password": "admin123"})
c.post("/shifts/open", data={"cashbox_id": BOX, "opening_cash": "0"})


def q(model, i):
    with a.app_context():
        return db.session.get(model, i)


def stock(pid, w=None):
    with a.app_context():
        s = Stock.query.filter_by(product_id=pid, warehouse_id=w or WH).first()
        return s.quantity if s else 0


def box_balance():
    with a.app_context():
        return db.session.get(Cashbox, BOX).balance or 0


def sale(items, **kw):
    payload = {"warehouse_id": WH, "items": items, "payment_method": "cash"}
    payload.update(kw)
    r = c.post("/pos/checkout", json=payload)
    return r.status_code, r.get_json()


# ---------------------------------------------------------------- 1) سلامة البيع من نقطة البيع
code, js = sale([{"id": A, "qty": 1}], payment_method="split", cash_amount=10, bank_amount=0, credit_amount=0)
check("S1", "بيع مزدوج مجموع دفعاته (10) لا يساوي الإجمالي (100) يجب أن يُرفض", "مرفوض", "مرفوض" if code >= 400 else "مقبول",
      "الخادم يثق بالأرقام القادمة من الشاشة")
st0 = stock(A)
code, js = sale([{"id": A, "qty": -3}], cash_amount=-300)
check("S2", "بيع بكمية سالبة (-3) يجب أن يُرفض", "مرفوض", "مرفوض" if code >= 400 else "مقبول",
      f"المخزون صار {stock(A)} بدل {st0}")
code, js = sale([{"id": A, "qty": 1}], discount=500, cash_amount=-400)
check("S3", "خصم (500) أكبر من قيمة الفاتورة (100) يجب أن يُرفض", "مرفوض", "مرفوض" if code >= 400 else "مقبول")

# ---------------------------------------------------------------- 2) رصيد الخزينة أثناء الوردية
b0 = box_balance()
sale([{"id": A, "qty": 2}], cash_amount=200)
check("S4", "رصيد الخزينة بعد بيع نقدي بـ 200 أثناء الوردية", round(b0 + 200, 2), round(box_balance(), 2),
      "المبيعات النقدية لا تدخل رصيد الخزينة إلا عند إغلاق الوردية")

# ---------------------------------------------------------------- 3) دفعة لمورد بعملة أجنبية من صفحة الحسابات
c.post("/purchases/add", data={"warehouse_id": WH, "supplier_id": SUP, "currency_code": "USD", "exchange_rate": "7",
                               "product_id[]": [str(C)], "quantity[]": ["10"], "unit_cost[]": ["20"], "paid_amount": "0"})
sup_before = q(Supplier, SUP).balance
check("S5a", "رصيد المورد (بالدولار) بعد فاتورة شراء 200$ آجل", 200.0, sup_before)
bb = box_balance()
c.post("/accounts/add", data={"entry_type": "supplier_payment", "supplier_id": SUP, "amount": "700",
                              "payment_method": "cash", "cashbox_id": BOX, "note": "دفعة 100$ = 700 د.ل",
                              "exchange_rate": "7"})
check("S5b", "دفع 700 د.ل (= 100$) للمورد التركي: الباقي عليه بالدولار", 100.0, q(Supplier, SUP).balance,
      "المبلغ بالدينار يُخصم من رصيد المورد بالدولار مباشرة")
check("S5c", "الخزينة تنقص 700 د.ل", round(bb - 700, 2), round(box_balance(), 2))

# ---------------------------------------------------------------- 4) القرض في التقرير المالي (حركة النقد)
with a.app_context():
    before = m._financial_breakdown([], AccountEntry.query.all())["net_cash"]
c.post("/accounts/add", data={"entry_type": "loan_received", "external_borrower_id": LEND, "amount": "1000",
                              "payment_method": "cash", "cashbox_id": BOX})
with a.app_context():
    after = m._financial_breakdown([], AccountEntry.query.all())["net_cash"]
check("S6", "قرض نقدي 1000 دخل الصندوق: صافي النقد في التقرير المالي يزيد 1000", 1000.0, round(after - before, 2),
      "القرض وتسديده غير محسوبين في صافي النقد")

# ---------------------------------------------------------------- 5) تسوية المخزون: تعديل/حذف لا يصحح الخسارة
def losses():
    with a.app_context():
        return round(sum(e.amount for e in AccountEntry.query.filter_by(entry_type="inventory_loss").all()), 2)
l0 = losses(); sB = stock(B)
c.post("/inventory/adjust", data={"product_id": B, "warehouse_id": WH, "reason": "جرد", "mode": "decrease", "change_quantity": "5"})
check("S7a", "نقص 5 بنطلون (تكلفة 90) = خسارة 450", 450.0, round(losses() - l0, 2))
with a.app_context():
    adj = InventoryAdjustment.query.order_by(InventoryAdjustment.id.desc()).first(); AID = adj.id
c.post(f"/inventory/adjust/{AID}/edit", data={"new_quantity": str(sB), "reason": "تبيّن أن الجرد كان صحيحًا"})
check("S7b", "تعديل التسوية لتصبح بدون نقص: الخسارة يجب أن تصبح 0", 0.0, round(losses() - l0, 2),
      "قيد الخسارة يبقى كما هو بعد التعديل")
c.post(f"/inventory/adjust/{AID}/delete")
check("S7c", "حذف التسوية: الخسارة يجب أن تختفي", 0.0, round(losses() - l0, 2), "قيد الخسارة يبقى بعد الحذف")

sB = stock(B)
c.post("/inventory/adjust", data={"product_id": B, "warehouse_id": WH, "reason": "جرد", "mode": "set", "new_quantity": str(sB + 2)})
with a.app_context():
    AID = InventoryAdjustment.query.order_by(InventoryAdjustment.id.desc()).first().id
sale([{"id": B, "qty": 3}], cash_amount=450)
c.post(f"/inventory/adjust/{AID}/edit", data={"new_quantity": str(sB + 1), "reason": "تصحيح الزيادة إلى 1"})
check("S8", "تسوية +2 ثم بيع 3 ثم تعديل التسوية إلى +1: المخزون = الأصلي +1 -3", sB + 1 - 3, stock(B),
      "تعديل التسوية يمسح المبيعات التي حصلت بعدها")

# ---------------------------------------------------------------- 6) تعديل فاتورة عليها إرجاع
sA = stock(A)
code, js = sale([{"id": A, "qty": 4}], cash_amount=400); inv = js["invoice_id"]
with a.app_context():
    item_id = db.session.get(Invoice, inv).items[0].id
c.post(f"/sales/{inv}/return", data={f"qty_{item_id}": "2", "refund_method": "cash", "cashbox_id": BOX})
c.post(f"/sales/edit/{inv}", data={"warehouse_id": WH, "customer_id": "", "discount": "0", "payment_method": "cash",
                                    "product_id": [str(A)], "quantity": ["2"], "unit_price": ["100"]})
check("S9", "بيع 4 → إرجاع 2 → تعديل الفاتورة إلى 2: المخزون = الأصلي - 2", sA - 2, stock(A),
      "التعديل يعيد الكمية كاملة رغم أن جزءًا منها رجع بالإرجاع")

# ---------------------------------------------------------------- 7) تعديل فاتورة شراء لا يعكس متوسط التكلفة
with a.app_context():
    p = Product(name="فستان", barcode="D", sale_price=300, purchase_price=0); db.session.add(p); db.session.commit(); D = p.id
c.post("/purchases/add", data={"warehouse_id": WH, "currency_code": "LYD", "exchange_rate": "1",
                               "product_id[]": [str(D)], "quantity[]": ["10"], "unit_cost[]": ["100"], "paid_amount": "0"})
with a.app_context():
    PID = Purchase.query.order_by(Purchase.id.desc()).first().id
c.post(f"/purchases/{PID}/edit", data={"warehouse_id": WH, "currency_code": "LYD", "exchange_rate": "1",
                                       "product_id[]": [str(D)], "quantity[]": ["10"], "unit_cost[]": ["150"], "paid_amount": "0"})
check("S10", "شراء 10 بـ 100 ثم تعديل الفاتورة إلى 150: التكلفة يجب أن تكون 150", 150.0, round(q(Product, D).purchase_price, 2),
      "يُحسب متوسط بين القديم والجديد كأنهما فاتورتان")

with a.app_context():
    p = Product(name="فستان2", barcode="D2", sale_price=300, purchase_price=100); db.session.add(p); db.session.flush()
    db.session.add(Stock(product_id=p.id, warehouse_id=WH, quantity=10)); db.session.commit(); D2 = p.id
c.post("/purchases/add", data={"warehouse_id": WH, "currency_code": "LYD", "exchange_rate": "1",
                               "product_id[]": [str(D2)], "quantity[]": ["10"], "unit_cost[]": ["200"], "paid_amount": "0"})
check("S10b", "10 قطع بـ100 + شراء 10 بـ200 = تكلفة 150", 150.0, round(q(Product, D2).purchase_price, 2))
with a.app_context():
    PID2 = Purchase.query.order_by(Purchase.id.desc()).first().id
c.post(f"/purchases/{PID2}/edit", data={"warehouse_id": WH, "currency_code": "LYD", "exchange_rate": "1",
                                        "product_id[]": [str(D2)], "quantity[]": ["10"], "unit_cost[]": ["100"], "paid_amount": "0"})
check("S10c", "تعديل الشراء إلى 10 بـ100: التكلفة 100", 100.0, round(q(Product, D2).purchase_price, 2))
c.get(f"/purchases/{PID2}/delete")
check("S10d", "حذف فاتورة الشراء: التكلفة ترجع 100 (تكلفة القطع الأصلية)", 100.0, round(q(Product, D2).purchase_price, 2))

# ---------------------------------------------------------------- 8) فاتورة قديمة بدون أثر مخزون ثم إلغاؤها
sC = stock(C)
c.post("/sales/old", data={"invoice_number": "OLD-TEST-1", "invoice_date": "2025-01-05T10:00", "warehouse_id": WH,
                           "payment_method": "cash", "product_id[]": [str(C)], "quantity[]": ["3"], "unit_price[]": ["200"],
                           "affect_stock": "0"})
with a.app_context():
    old = Invoice.query.filter_by(invoice_number="OLD-TEST-1").first()
if old:
    c.post(f"/sales/delete/{old.id}")
check("S11", "إدخال فاتورة قديمة دون خصم مخزون ثم حذفها: المخزون لا يتغير", sC, stock(C),
      "الإلغاء يعيد 3 قطع لم تُخصم أصلًا" if old else "لم تُحفظ الفاتورة القديمة")

# ---------------------------------------------------------------- 9) تعديل/حذف قيد إرجاع من صفحة الحسابات
code, js = sale([{"id": A, "qty": 1}], cash_amount=100); inv2 = js["invoice_id"]
with a.app_context():
    it2 = db.session.get(Invoice, inv2).items[0].id
c.post(f"/sales/{inv2}/return", data={f"qty_{it2}": "1", "refund_method": "cash", "cashbox_id": BOX})
with a.app_context():
    ret = SalesReturn.query.filter_by(invoice_id=inv2).first()
    entry = AccountEntry.query.filter_by(entry_type="sales_return", reference=ret.return_number).first(); EID = entry.id
r = c.get(f"/accounts/delete/{EID}")
with a.app_context():
    still = SalesReturn.query.filter_by(invoice_id=inv2).count()
    gone = db.session.get(AccountEntry, EID) is None
check("S12", "حذف قيد مالي ناتج عن إرجاع يجب أن يُمنع (وإلا يبقى المرتجع مسجلًا بلا فلوس مقابله)", "ممنوع",
      "مسموح" if gone and still else "ممنوع")

# ---------------------------------------------------------------- 10) الربح يتغير بتغير تكلفة المنتج لاحقًا
with a.app_context():
    db.session.get(Product, D).purchase_price = 150; db.session.commit()
code, js = sale([{"id": D, "qty": 1}], cash_amount=300); PINV = js["invoice_id"]
with a.app_context():
    p1 = m.calculate_net_profit([db.session.get(Invoice, PINV)], [], [])["gross_profit"]
    db.session.get(Product, D).purchase_price = 250; db.session.commit()
    p2 = m.calculate_net_profit([db.session.get(Invoice, PINV)], [], [])["gross_profit"]
check("S13", "ربح بيعة قديمة لا يتغير لما تتغير تكلفة المنتج لاحقًا", round(p1, 2), round(p2, 2),
      "الربح يُحسب بتكلفة المنتج الحالية لا بتكلفته وقت البيع")

# ---------------------------------------------------------------- 11) مبيعات اليوم بعد منتصف الليل
with a.app_context():
    today = (datetime.utcnow() + timedelta(hours=2)).date()  # اليوم بتوقيت ليبيا
    # فاتورة الساعة 00:30 بتوقيت ليبيا (UTC+2) = 22:30 أمس بتوقيت غرينتش، وهكذا يحفظها النظام
    utc_time = datetime.combine(today, datetime.min.time()) + timedelta(minutes=30) - timedelta(hours=2)
    inv = Invoice(invoice_number="MIDNIGHT-1", warehouse_id=WH, user_id=1, date=utc_time, subtotal=50, total=50,
                  cash_amount=50, payment_method="cash")
    db.session.add(inv); db.session.commit(); MID = inv.id
    ds, de = m.local_today_range()
    counted = Invoice.query.filter(Invoice.date >= ds, Invoice.date < de, Invoice.invoice_number == "MIDNIGHT-1").count()
check("S14", "بيعة الساعة 12:30 بعد منتصف الليل تُحسب ضمن مبيعات اليوم في لوحة التحكم", 1, counted,
      "تُحسب على اليوم السابق")
html = c.get(f"/sales/invoice/{MID}").get_data(as_text=True)
check("S14b", "الفاتورة المطبوعة تعرض وقت المحل (00:30) لا وقت غرينتش (22:30)", True,
      f"{today:%Y-%m-%d} 00:30" in html)

# ---------------------------------------------------------------- 12) أرصدة العملاء: مطابقة الرصيد لمجموع الحركات
sale([{"id": B, "qty": 2}], payment_method="credit", credit_amount=300, customer_id=CUST)
code, js = sale([{"id": C, "qty": 1}], payment_method="split", cash_amount=50, credit_amount=150, customer_id=CUST)
inv3 = js["invoice_id"]
c.post("/accounts/add", data={"entry_type": "customer_payment", "customer_id": CUST, "amount": "120", "payment_method": "cash", "cashbox_id": BOX})
c.post(f"/customers/{CUST}/discount", data={"amount": "30", "reason": "زبون دائم"})
with a.app_context():
    it3 = db.session.get(Invoice, inv3).items[0].id
c.post(f"/sales/{inv3}/return", data={f"qty_{it3}": "1", "refund_method": "customer_credit"})
with a.app_context():
    cu = db.session.get(Customer, CUST)
    credit_sales = sum(i.credit_amount for i in Invoice.query.filter_by(customer_id=CUST).filter(Invoice.status != "voided"))
    ents = AccountEntry.query.filter_by(customer_id=CUST).all()
    calc = credit_sales + sum(e.amount for e in ents if e.entry_type == "customer_debt") \
        - sum(e.amount for e in ents if e.entry_type in ("customer_payment", "customer_discount", "sales_return"))
check("S15", "رصيد العميل = آجل الفواتير + ديون يدوية - تحصيلات - خصومات - مرتجعات", round(calc, 2), round(cu.balance, 2))

# ---------------------------------------------------------------- 13) الإلغاء بعد الدفع النقدي
code, js = sale([{"id": A, "qty": 1}], cash_amount=100); inv4 = js["invoice_id"]
with a.app_context():
    sh = Shift.query.filter_by(status="open").first(); exp_before = sh.expected_cash()
bv = box_balance()
c.post(f"/sales/void/{inv4}", data={"reason": "العميل تراجع"})
with a.app_context():
    sh = Shift.query.filter_by(status="open").first(); exp_after = sh.expected_cash()
check("S16b", "إلغاء فاتورة نقدية بـ100: رصيد الخزينة ينقص 100 (استرجاع مسجّل)", round(bv - 100, 2), round(box_balance(), 2))
check("S16", "إلغاء فاتورة نقدية بـ 100: النقد المتوقع ينقص 100 (معناه أن الكاشير رجّع الفلوس)", -100.0, round(exp_after - exp_before, 2),
      "للتأكد من السلوك فقط: الإلغاء يفترض أن الفلوس رجعت للعميل دون تسجيل حركة استرجاع")

# ---------------------------------------------------------------- 14) الراتب مع خصم سلفة
c.post("/accounts/add", data={"entry_type": "employee_advance", "employee_id": EMP, "amount": "200", "payment_method": "cash", "cashbox_id": BOX})
bb = box_balance()
c.post(f"/employees/{EMP}/pay_salary", data={"gross_amount": "1000", "deduct_advance": "200", "payment_method": "cash", "cashbox_id": BOX})
check("S17a", "راتب 1000 مع خصم سلفة 200: الخزينة تنقص 800", round(bb - 800, 2), round(box_balance(), 2))
check("S17b", "سلفة الموظف بعد الخصم = 0", 0.0, q(Employee, EMP).balance)

# ---------------------------------------------------------------- 15) تحويل بين المخازن ثم الحذف بعد البيع
sale_wh2_before = stock(A, WH2)
c.post("/transfers/add", data={"from_warehouse_id": WH, "to_warehouse_id": WH2, "product_id[]": [str(A)], "quantity[]": ["5"]})
c.post("/pos/checkout", json={"warehouse_id": WH2, "items": [{"id": A, "qty": 4}], "payment_method": "cash", "cash_amount": 400})
with a.app_context():
    TID = StockTransfer.query.order_by(StockTransfer.id.desc()).first().id
c.get(f"/transfers/delete/{TID}")
check("S18", "نقل 5 لمخزن 2 ثم بيع 4 منها ثم حذف النقل: مخزن 2 لا يصير سالبًا", True, stock(A, WH2) >= 0,
      f"مخزن 2 صار {stock(A, WH2)}")

# ---------------------------------------------------------------- 16) التقرير المالي يطابق الخزينة بعد إغلاق الوردية
with a.app_context():
    sh = Shift.query.filter_by(status="open").first(); expected = sh.expected_cash(); SHID = sh.id
c.post("/shifts/close", data={"counted_cash": str(expected)})
with a.app_context():
    diff = db.session.get(Shift, SHID).difference()
check("S19", "إغلاق الوردية بالمبلغ المتوقع تمامًا: الفرق = 0", 0.0, diff)
check("S19b", "رصيد الخزينة بعد الإغلاق = المعدود", round(expected, 2), round(box_balance(), 2))
# وردية ثانية بعجز 30
c.post("/shifts/open", data={"cashbox_id": BOX, "opening_cash": str(round(box_balance(), 2))})
sale([{"id": B, "qty": 1}], cash_amount=150)
with a.app_context():
    sh2 = Shift.query.filter_by(status="open").first(); SH2 = sh2.id; exp2 = sh2.expected_cash()
c.post("/shifts/close", data={"counted_cash": str(exp2 - 30)})
with a.app_context():
    d2 = db.session.get(Shift, SH2).difference()
    cls = CashTransfer.query.filter(CashTransfer.transfer_number.like("CLS-%")).order_by(CashTransfer.id.desc()).first()
    cls_amt = cls.amount if cls else None
check("S19c", "وردية بعجز 30: الفرق = −30 ومسجّل كقيد عجز صندوق", (-30.0, 30.0), (d2, cls_amt))
check("S19d", "رصيد الخزينة بعد الإغلاق بعجز = المعدود", round(exp2 - 30, 2), round(box_balance(), 2))
c.post("/shifts/open", data={"cashbox_id": BOX, "opening_cash": str(round(box_balance(), 2))})

# ---------------------------------------------------------------- دين قديم لمورد أجنبي وسداده بسعر صرف
c.post("/suppliers/debts/add", data={"supplier_id": SUP, "amount": "50", "note": "دين قديم 50$"})
sb = q(Supplier, SUP).balance; bb = box_balance()
with a.app_context():
    DID = SupplierDebt.query.order_by(SupplierDebt.id.desc()).first().id
c.post(f"/suppliers/debts/{DID}/pay", data={"amount": "50", "exchange_rate": "7", "payment_method": "cash", "cashbox_id": BOX})
check("S22a", "سداد دين 50$ بسعر 7: رصيد المورد ينقص 50$", round(sb - 50, 2), round(q(Supplier, SUP).balance, 2))
check("S22b", "والخزينة تنقص 350 د.ل", round(bb - 350, 2), round(box_balance(), 2))

# ---------------------------------------------------------------- 17) صفحة التقارير: إجمالي المبيعات بعد المرتجعات
with a.app_context():
    inv_all = Invoice.query.filter(Invoice.status != "voided").all()
    rets = round(sum(r.total for r in SalesReturn.query.all()), 2)
html = c.get("/reports").get_data(as_text=True)
check("S20", "صفحة التقارير توضّح المرتجعات (لا تعرض الإجمالي قبل المرتجعات وكأنه صافي)", True, ("مرتجع" in html) or rets == 0,
      f"المرتجعات {rets} غير ظاهرة في الصفحة")

print()
fails = 0
for sid, ok, title, exp, act, note in results:
    mark = "✅" if ok else "❌"
    if not ok:
        fails += 1
    print(f"{mark} {sid}: {title}\n      المتوقع: {exp} | الفعلي: {act}" + (f"\n      ← {note}" if not ok and note else ""))
print(f"\nنتيجة: {len(results) - fails} سليم، {fails} خطأ")
