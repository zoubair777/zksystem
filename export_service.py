"""
تصدير الجرد وسجلات الفواتير إلى ملفات Excel (.xlsx) و PDF.

ملاحظة مهمة حول العربية داخل PDF:
مكتبة reportlab لا تُشكّل الحروف العربية ولا تعكس اتجاهها تلقائيًا.
لذلك نحاول استخدام arabic_reshaper + python-bidi إن كانتا مثبّتتين
(أضفهما إلى requirements.txt وشغّل: pip install arabic-reshaper python-bidi)
مع خط TTF يدعم العربية موضوع في static/fonts/arabic.ttf (مثل Cairo أو Amiri).
إن لم تتوفر هذه المتطلبات، يستمر التصدير بدون توقف لكن قد يظهر النص العربي
غير متصل الحروف/معكوس الاتجاه، وتُستخدم خطوط PDF الافتراضية بدلاً من ذلك.
تصدير Excel لا يحتاج لأي من هذا: openpyxl يدعم العربية والـ RTL بشكل كامل.
"""
import io
import os
from datetime import datetime, timedelta
import os


def _local(dt):
    """نفس تحويل app.to_local (UTC → توقيت المحل) دون استيراد app لتفادي الاستيراد الدائري."""
    forced = os.getenv("ERP_TZ_OFFSET_HOURS")
    if forced not in (None, ""):
        return dt + timedelta(hours=float(forced))
    seconds = (datetime.now() - datetime.utcnow()).total_seconds()
    return dt + timedelta(seconds=round(seconds / 900) * 900)

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
    _AR_SHAPING_AVAILABLE = True
except ImportError:
    _AR_SHAPING_AVAILABLE = False

from config import Config

BASE_DIR = Config.RESOURCE_DIR
FONT_PATH = os.path.join(BASE_DIR, "static", "fonts", "arabic.ttf")
_FONT_NAME = "Helvetica"
_FONT_REGISTERED = False


def _register_arabic_font():
    """يسجّل خط TTF عربي مرة واحدة فقط، ويرجع اسم الخط المستخدم فعليًا."""
    global _FONT_NAME, _FONT_REGISTERED
    if _FONT_REGISTERED:
        return _FONT_NAME
    _FONT_REGISTERED = True
    if os.path.exists(FONT_PATH):
        try:
            pdfmetrics.registerFont(TTFont("ArabicFont", FONT_PATH))
            _FONT_NAME = "ArabicFont"
        except Exception:
            _FONT_NAME = "Helvetica"
    return _FONT_NAME


def ar(text):
    """يهيئ نصًا عربيًا للعرض الصحيح داخل PDF (تشكيل الحروف + اتجاه الكتابة)."""
    if text is None:
        return ""
    text = str(text)
    if _AR_SHAPING_AVAILABLE:
        try:
            reshaped = arabic_reshaper.reshape(text)
            return get_display(reshaped)
        except Exception:
            return text
    return text


# ---------------------------------------------------------------------------
# Excel - تقرير الجرد
# ---------------------------------------------------------------------------
def export_inventory_excel(products, currency="د.ل"):
    wb = Workbook()
    ws = wb.active
    ws.title = "الجرد"
    ws.sheet_view.rightToLeft = True

    headers = ["المنتج", "الباركود", "الفئة", "المقاس", "اللون", "الوحدة",
               "سعر الشراء", "سعر البيع", "الكمية الحالية", "قيمة المخزون (تكلفة)"]
    header_fill = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    thin = Side(style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for col, h in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border

    row_idx = 2
    total_value = 0.0
    for p in products:
        qty = p.total_quantity()
        value = round(qty * (p.purchase_price or 0), 2)
        total_value += value
        row = [p.name, p.barcode or "", p.category.name if p.category else "",
               p.size or "", p.color or "", p.unit,
               round(p.purchase_price or 0, 2), round(p.sale_price or 0, 2), qty, value]
        for col, v in enumerate(row, start=1):
            cell = ws.cell(row=row_idx, column=col, value=v)
            cell.border = border
            cell.alignment = Alignment(horizontal="center")
        row_idx += 1

    ws.cell(row=row_idx, column=9, value="الإجمالي").font = Font(bold=True)
    total_cell = ws.cell(row=row_idx, column=10, value=round(total_value, 2))
    total_cell.font = Font(bold=True)

    for col in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(col)].width = 18

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{row_idx - 1}"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# PDF - تقرير الجرد
# ---------------------------------------------------------------------------
def export_inventory_pdf(products, company="", currency="د.ل"):
    font_name = _register_arabic_font()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=1.5 * cm, leftMargin=1.5 * cm,
                             topMargin=1.5 * cm, bottomMargin=1.5 * cm,
                             title="inventory-report")
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("ArTitle", parent=styles["Title"], fontName=font_name,
                                  alignment=TA_CENTER, fontSize=16)
    sub_style = ParagraphStyle("ArSub", parent=styles["Normal"], fontName=font_name,
                                alignment=TA_CENTER, fontSize=10, textColor=colors.grey)

    elements = [
        Paragraph(ar(company) if company else ar("تقرير الجرد"), title_style),
        Paragraph(ar("تقرير الجرد الحالي") + " - " + datetime.now().strftime("%Y-%m-%d %H:%M"), sub_style),
        Spacer(1, 14),
    ]

    headers = ["قيمة المخزون", "الكمية", "سعر البيع", "سعر الشراء", "المنتج"]
    data = [[ar(h) for h in headers]]
    total_value = 0.0
    for p in products:
        qty = p.total_quantity()
        value = round(qty * (p.purchase_price or 0), 2)
        total_value += value
        data.append([
            f"{value:,.2f}",
            str(qty),
            f"{(p.sale_price or 0):,.2f}",
            f"{(p.purchase_price or 0):,.2f}",
            ar(f"{p.name} ({p.barcode})" if p.barcode else p.name),  # الباركود يميّز المنتجات متشابهة الاسم
        ])
    data.append(["", "", "", ar("الإجمالي"), f"{total_value:,.2f}"])

    col_widths = [3.2 * cm, 2.2 * cm, 2.8 * cm, 2.8 * cm, 6.5 * cm]
    table = Table(data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font_name),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2F5496")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -2), 0.5, colors.HexColor("#CCCCCC")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#F5F7FA")]),
        ("LINEABOVE", (0, -1), (-1, -1), 1.2, colors.black),
        ("FONTNAME", (0, -1), (-1, -1), font_name),
        ("SPAN", (0, -1), (2, -1)),
    ]))
    elements.append(table)

    if not _AR_SHAPING_AVAILABLE or font_name == "Helvetica":
        elements.append(Spacer(1, 14))
        elements.append(Paragraph(
            "Note: install 'arabic-reshaper' + 'python-bidi' and place an Arabic "
            "TTF font at static/fonts/arabic.ttf for correctly shaped Arabic text.",
            styles["Normal"]))

    doc.build(elements)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Excel - سجل الفواتير (اختياري لدعم مطلب "حفظ/تصدير الفواتير")
# ---------------------------------------------------------------------------
def export_invoices_excel(invoices, currency="د.ل"):
    wb = Workbook()
    ws = wb.active
    ws.title = "الفواتير"
    ws.sheet_view.rightToLeft = True

    headers = ["رقم الفاتورة", "التاريخ", "العميل", "طريقة الدفع", "الإجمالي الفرعي",
               "الخصم", "الإجمالي النهائي", "نقدي", "بنكي", "آجل", "الحالة"]
    header_fill = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    thin = Side(style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for col, h in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border

    status_labels = {"completed": "مكتملة", "void_requested": "بانتظار الإلغاء", "voided": "ملغاة"}
    row_idx = 2
    for inv in invoices:
        row = [
            inv.invoice_number,
            _local(inv.date).strftime("%Y-%m-%d %H:%M") if inv.date else "",
            inv.customer.name if inv.customer else "عميل نقدي",
            inv.payment_label(),
            round(inv.subtotal or 0, 2),
            round(inv.discount or 0, 2),
            round(inv.total or 0, 2),
            round(inv.cash_amount or 0, 2),
            round(inv.bank_amount or 0, 2),
            round(inv.credit_amount or 0, 2),
            status_labels.get(inv.status, inv.status),
        ]
        for col, v in enumerate(row, start=1):
            cell = ws.cell(row=row_idx, column=col, value=v)
            cell.border = border
            cell.alignment = Alignment(horizontal="center")
        row_idx += 1

    for col in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(col)].width = 16

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{row_idx - 1}"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# V9 unified export helpers
# ---------------------------------------------------------------------------
def export_generic_excel(title, headers, rows):
    """تصدير عام لأي جدول بيانات (تقرير مالي، كشف حساب، تقرير وردية...) كملف Excel في الذاكرة."""
    wb = Workbook()
    ws = wb.active
    ws.title = str(title)[:31] or "تقرير"
    ws.sheet_view.rightToLeft = True

    header_fill = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    thin = Side(style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for col, h in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border

    row_idx = 2
    for row in rows:
        for col, v in enumerate(row, start=1):
            cell = ws.cell(row=row_idx, column=col, value=v)
            cell.border = border
            cell.alignment = Alignment(horizontal="center")
        row_idx += 1

    for col in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(col)].width = 20
    ws.freeze_panes = "A2"
    if row_idx > 2:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{row_idx - 1}"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def export_generic_pdf(title, headers, rows, company=""):
    """تصدير عام لأي جدول بيانات كملف PDF في الذاكرة، مع دعم صحيح للغة العربية."""
    font_name = _register_arabic_font()
    buf = io.BytesIO()
    from reportlab.lib.pagesizes import landscape
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), rightMargin=1.2 * cm, leftMargin=1.2 * cm,
                             topMargin=1.2 * cm, bottomMargin=1.2 * cm, title="report")
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("ArTitle", parent=styles["Title"], fontName=font_name,
                                  alignment=TA_CENTER, fontSize=15)
    sub_style = ParagraphStyle("ArSub", parent=styles["Normal"], fontName=font_name,
                                alignment=TA_CENTER, fontSize=9, textColor=colors.grey)
    elements = [
        Paragraph(ar(company) if company else ar(title), title_style),
        Paragraph(ar(title) + " - " + datetime.now().strftime("%Y-%m-%d %H:%M"), sub_style),
        Spacer(1, 12),
    ]
    data = [[ar(h) for h in headers]]
    for row in rows:
        data.append([ar(x) if isinstance(x, str) else (f"{x:,.2f}" if isinstance(x, float) else str(x)) for x in row])
    # reportlab يرسم الجدول من اليسار لليمين؛ نعكس الأعمدة ليقرأ التقرير من اليمين كالعربية (أول عمود أقصى اليمين)
    data = [list(reversed(r)) for r in data]
    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font_name),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2F5496")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CCCCCC")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F7FA")]),
    ]))
    elements.append(table)
    if not _AR_SHAPING_AVAILABLE or font_name == "Helvetica":
        elements.append(Spacer(1, 12))
        elements.append(Paragraph(
            "Note: install 'arabic-reshaper' + 'python-bidi' and place an Arabic TTF font "
            "at static/fonts/arabic.ttf for correctly shaped Arabic text.", styles["Normal"]))
    doc.build(elements)
    buf.seek(0)
    return buf


def get_export_directory(app):
    try:
        from models import Setting
        path = Setting.get("export_path", "")
    except Exception:
        path = ""
    if not path:
        path = os.path.join(app.config.get("BASE_DIR", os.getcwd()), "exports")
    os.makedirs(path, exist_ok=True)
    return path

def export_rows_xlsx(app, title, headers, rows, prefix="report"):
    """Create an XLSX file in the configured ERP export folder."""
    from datetime import datetime
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment
    out_dir = os.path.join(get_export_directory(app), "Excel")
    os.makedirs(out_dir, exist_ok=True)
    filename = f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    path = os.path.join(out_dir, filename)
    wb = Workbook(); ws = wb.active; ws.title = title[:31]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True); cell.alignment = Alignment(horizontal="center")
    for row in rows: ws.append(list(row))
    ws.freeze_panes = "A2"
    for col in ws.columns:
        width = min(max(len(str(c.value or "")) for c in col) + 2, 45)
        ws.column_dimensions[col[0].column_letter].width = width
    wb.save(path)
    return path

def export_rows_pdf(app, title, headers, rows, prefix="report"):
    """Create a simple PDF with correctly shaped Arabic text (RTL + connected letters)."""
    from datetime import datetime
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER
    font_name = _register_arabic_font()
    out_dir = os.path.join(get_export_directory(app), "PDF")
    os.makedirs(out_dir, exist_ok=True)
    filename = f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    path = os.path.join(out_dir, filename)
    doc = SimpleDocTemplate(path, pagesize=landscape(A4))
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("ArTitle", parent=styles["Title"], fontName=font_name, alignment=TA_CENTER)
    data = [[ar(h) for h in headers]] + [[ar(x) if isinstance(x, str) else str(x) for x in row] for row in rows]
    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font_name),
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ]))
    doc.build([Paragraph(ar(title), title_style), Spacer(1, 12), table])
    return path


# ---------------------------------------------------------------------------
# PDF - قائمة المنتجات مع كود كل منتج (نص فقط)
# ---------------------------------------------------------------------------
def _pdf_text(text):
    """Paragraph في reportlab يقرأ النص كـ XML، فاسم فيه & أو < كان يُسقط التقرير كله. نهرّبه بعد التشكيل."""
    from xml.sax.saxutils import escape
    return escape(ar(text))


def export_products_barcode_pdf(rows, company="", currency="د.ل", show_cost=False):
    """rows: قوائم dict فيها name, barcode, size, color, category, qty, price, cost.
    الجدول من اليمين لليسار (الاسم أقصى اليمين) وآخر عمود كود المنتج كنص.
    صورة الباركود أُلغيت بطلب المالك: مكتبة الرسم (code128) لم تكن داخل نسخة exe فكان التقرير يفشل."""
    font_name = _register_arabic_font()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=1 * cm, leftMargin=1 * cm,
                            topMargin=1.2 * cm, bottomMargin=1.2 * cm, title="products-barcodes")
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("ArTitle", parent=styles["Title"], fontName=font_name, alignment=TA_CENTER, fontSize=15)
    sub_style = ParagraphStyle("ArSub", parent=styles["Normal"], fontName=font_name, alignment=TA_CENTER,
                               fontSize=9, textColor=colors.grey)
    cell_style = ParagraphStyle("ArCell", parent=styles["Normal"], fontName=font_name, alignment=TA_RIGHT,
                                fontSize=9, leading=12)
    # خط عربي لا Courier: بعض الأكواد قد تحتوي أرقامًا أو حروفًا عربية
    code_style = ParagraphStyle("Code", parent=styles["Normal"], fontName=font_name, alignment=TA_CENTER, fontSize=10)

    total_qty = sum(max(r["qty"], 0) for r in rows)
    elements = [
        Paragraph(_pdf_text(company or "قائمة المنتجات"), title_style),
        Paragraph(_pdf_text(f"قائمة المنتجات والأكواد - {len(rows)} صنف - إجمالي القطع {total_qty}") + " - "
                  + datetime.now().strftime("%Y-%m-%d %H:%M"), sub_style),
        Spacer(1, 10),
    ]

    headers = [ar("الكود"), ar("الكمية"), ar(f"سعر البيع ({currency})")]
    widths = [3.6 * cm, 1.6 * cm, 2.4 * cm]
    if show_cost:
        headers.append(ar("التكلفة"))
        widths.append(2 * cm)
    headers += [ar("المقاس / اللون"), ar("المنتج"), "#"]
    widths += [3 * cm, (19 - sum(w / cm for w in widths) - 3 - 0.8) * cm, 0.8 * cm]
    data = [headers]
    for i, r in enumerate(rows, 1):
        code_cell = Paragraph(_pdf_text(r["barcode"] or "-"), code_style)
        line = [code_cell, str(r["qty"]), f"{r['price']:,.2f}"]
        if show_cost:
            line.append(f"{r['cost']:,.2f}")
        size_color = " / ".join(x for x in (r["size"], r["color"]) if x) or "-"
        name = r["name"] + (f" ({r['category']})" if r.get("category") else "")
        line += [Paragraph(_pdf_text(size_color), cell_style), Paragraph(_pdf_text(name), cell_style), str(i)]
        data.append(line)
    total_line = ["", str(total_qty), ""] + ([""] if show_cost else []) + ["", Paragraph(ar("الإجمالي"), cell_style), ""]
    data.append(total_line)

    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font_name),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2F6B4F")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CCCCCC")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#F5F7FA")]),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#E9ECEF")),
        ("TOPPADDING", (0, 1), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 4),
    ]))
    elements.append(table)
    doc.build(elements)
    buf.seek(0)
    return buf
