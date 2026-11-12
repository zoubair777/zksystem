"""رسم باركود Code128 لملصقات الطابعة الحرارية.

لماذا لا نستعمل صورة جاهزة من مكتبة: طابعة الملصقات دقتها 203 نقطة/إنش (8 نقاط لكل مم)، وأي خط في
الباركود عرضه ليس عددًا صحيحًا من النقاط يطلع مشوّهًا فلا يقرؤه الماسح. هنا نختار عرض الوحدة (module)
بحيث يساوي 3 أو 2 نقطة بالضبط، ونرسم الخطوط كمستطيلات SVG بالمليمتر.
جدول الأنماط منسوخ هنا من reportlab ومطابق له (يُفحص في الاختبار). لا نستورد reportlab.graphics.barcode
لأن تلك الحزمة تحمّل أنواع باركود أخرى ديناميكيًا لا يضمّها PyInstaller، فكان ERP.exe ينهار عند التشغيل
(No module named reportlab.graphics.barcode.code93).
"""

# عرض كل خط/فراغ بالوحدات لكل قيمة 0..106 (106 = نهاية، 7 عناصر)
CODE128_PATTERNS = (
    '212222', '222122', '222221', '121223', '121322', '131222', '122213', '122312', '132212', '221213',
    '221312', '231212', '112232', '122132', '122231', '113222', '123122', '123221', '223211', '221132',
    '221231', '213212', '223112', '312131', '311222', '321122', '321221', '312212', '322112', '322211',
    '212123', '212321', '232121', '111323', '131123', '131321', '112313', '132113', '132311', '211313',
    '231113', '231311', '112133', '112331', '132131', '113123', '113321', '133121', '313121', '211331',
    '231131', '213113', '213311', '213131', '311123', '311321', '331121', '312113', '312311', '332111',
    '314111', '221411', '431111', '111224', '111422', '121124', '121421', '141122', '141221', '112214',
    '112412', '122114', '122411', '142112', '142211', '241211', '221114', '413111', '241112', '134111',
    '111242', '121142', '121241', '114212', '124112', '124211', '411212', '421112', '421211', '212141',
    '214121', '412121', '111143', '111341', '131141', '114113', '114311', '411113', '411311', '113141',
    '114131', '311141', '411131', '211412', '211214', '211232', '2331112',
)

START_B, START_C, CODE_C, STOP = 104, 105, 99, 106

# عرض الوحدة بالمليمتر: 3 نقاط ثم نقطتان بدقة 203 (0.125 مم للنقطة)
MODULE_WIDTHS = (0.375, 0.25)
QUIET_MODULES = 10  # فراغ أبيض لازم قبل وبعد الباركود حتى يلقط الماسح بدايته


def is_printable_code(code):
    """Code128 يقبل الحروف الإنجليزية والأرقام والرموز فقط. كود عربي (مثل "تيشرت") لا يُرسم باركود."""
    return bool(code) and all(32 <= ord(ch) <= 126 for ch in code)


def code128_values(code):
    """قيم الرموز مع البداية ورقم التحقق والنهاية. الأرقام تُضغط رقمين في رمز واحد (المجموعة C)
    فيقصر الباركود ويكبر عرض خطوطه على ملصق 40 مم."""
    import re
    if not is_printable_code(code):
        return None
    # كل سلسلة أرقام طولها 4 فأكثر تُكتب بالمجموعة C (حتى داخل كود مثل P1789144808)، والباقي بالمجموعة B.
    # بدونها الكود الطويل يطلع بخطوط عرضها نقطة واحدة فيصعب على الماسح.
    vals, cur = [], None

    def use(cset):
        nonlocal cur
        if cur != cset:
            vals.append((START_B if cset == "B" else START_C) if not vals else (100 if cset == "B" else CODE_C))
            cur = cset

    for run in re.findall(r"\d+|\D+", code):
        if run.isdigit() and len(run) >= 4:
            if len(run) % 2:
                use("B")
                vals.append(ord(run[0]) - 32)
                run = run[1:]
            use("C")
            vals.extend(int(run[i:i + 2]) for i in range(0, len(run), 2))
        else:
            use("B")
            vals.extend(ord(ch) - 32 for ch in run)
    check = (vals[0] + sum(i * v for i, v in enumerate(vals[1:], 1))) % 103
    return vals + [check, STOP]


def code128_modules(code):
    """قائمة (خط؟، عرض بالوحدات) لكل الباركود. كل نمط يبدأ بخط ثم فراغ بالتناوب."""
    vals = code128_values(code)
    if vals is None:
        return None
    out = []
    for v in vals:
        for i, ch in enumerate(CODE128_PATTERNS[v]):
            out.append((i % 2 == 0, int(ch)))
    return out


def barcode_svg(code, label_width_mm, bar_height_mm):
    """يعيد (svg, عرض الوحدة) أو (None, None) إن كان الكود لا يُرسم."""
    mods = code128_modules(code)
    if not mods:
        return None, None
    total = sum(w for _, w in mods)
    usable = label_width_mm - 1.0
    mw = next((m for m in MODULE_WIDTHS if (total + 2 * QUIET_MODULES) * m <= usable), None)
    if mw is None:
        # كود طويل جدًا على هذا المقاس: نصغّره ليدخل (قد يصعب على الماسح قراءته، وتُعرض ملاحظة)
        mw = round(usable / (total + 2 * QUIET_MODULES), 4)
    x, rects = 0.0, []
    for is_bar, w in mods:
        if is_bar:
            rects.append(f'<rect x="{x * mw:.4f}" y="0" width="{w * mw:.4f}" height="{bar_height_mm}"/>')
        x += w
    width = total * mw
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.3f}mm" height="{bar_height_mm}mm" '
           f'viewBox="0 0 {width:.4f} {bar_height_mm}" shape-rendering="crispEdges" fill="#000">'
           + "".join(rects) + "</svg>")
    return svg, mw


# ---------------------------------------------------------------------------
# الطباعة المباشرة للطابعة (بدون شاشة طباعة المتصفح)
# لماذا: المتصفح يطبع على مقاس ورق التعريفة لا مقاس الملصق، ويزيد عنوان الصفحة والتاريخ والرابط، فخرج
# الملصق الواحد على 4 ملصقات. هنا نرسم الملصق صورة بنقاط الطابعة نفسها (8 نقاط/مم) ونرسلها بلغة الطابعة
# (TSPL) مع مقاس الملصق والفراغ بينه وبين التالي، فتطبع الطابعة ملصقًا واحدًا بالضبط.
# ---------------------------------------------------------------------------
DOTS_PER_MM = 8  # 203 نقطة/إنش
FONT_DIR = r"C:\Windows\Fonts"


def _font(bold, size):
    from PIL import ImageFont
    import os
    for name in (("tahomabd.ttf", "arialbd.ttf") if bold else ("tahoma.ttf", "arial.ttf")):
        path = os.path.join(FONT_DIR, name)
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _shape(text):
    """Pillow هنا بدون مكتبة تشكيل الحروف، فنشكّل العربي ونرتّبه من اليمين يدويًا (نفس طريقة ملفات PDF)."""
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        return get_display(arabic_reshaper.reshape(text))
    except Exception:
        return text


def render_label(width_mm, height_mm, lines, code, price_text=None, show_code=True):
    """يرسم ملصقًا واحدًا صورة أبيض/أسود بحجم نقاط الطابعة.
    lines: [(نص، عريض؟، حجم الخط بالنقاط)] تُكتب فوق الباركود، والسعر تحته."""
    from PIL import Image, ImageDraw
    W, H = int(round(width_mm * DOTS_PER_MM)), int(round(height_mm * DOTS_PER_MM))
    img = Image.new("1", (W, H), 1)
    d = ImageDraw.Draw(img)
    margin = 2 * DOTS_PER_MM

    def fit(text, bold, size):
        text = _shape(text)
        while size > 12:
            f = _font(bold, size)
            if d.textlength(text, font=f) <= W - 2 * margin:
                return text, f
            size -= 2
        f = _font(bold, size)
        while text and d.textlength(text + "…", font=f) > W - 2 * margin:
            text = text[1:] if text and "\u0600" <= text[0] <= "\u06ff" else text[:-1]
        return text, f

    top = [fit(t, b, s) for t, b, s in lines if t]
    bottom = []
    if show_code:
        bottom.append((code, _font(False, 20)))
    if price_text:
        bottom.append(fit(price_text, True, 30))

    def h(f):
        a, dsc = f.getmetrics()
        return a + dsc

    gap = 2
    used = sum(h(f) + gap for _, f in top) + sum(h(f) + gap for _, f in bottom)
    bar_h = max(4 * DOTS_PER_MM, min(13 * DOTS_PER_MM, H - used - 2 * DOTS_PER_MM))
    total = used + bar_h + 2
    y = max(2, (H - total) // 2)
    for text, f in top:
        d.text(((W - d.textlength(text, font=f)) / 2, y), text, font=f, fill=0)
        y += h(f) + gap

    # الباركود بعرض وحدة 3 أو 2 نقطة صحيحة (أوضح للماسح)، وإلا أقصى ما يدخل
    mods = code128_modules(code) or []
    total_mods = sum(w for _, w in mods)
    unit = next((u for u in (3, 2) if (total_mods + 2 * QUIET_MODULES) * u <= W - 4), 1)
    x = (W - total_mods * unit) // 2
    for is_bar, w in mods:
        if is_bar:
            d.rectangle([x, y, x + w * unit - 1, y + bar_h - 1], fill=0)
        x += w * unit
    y += bar_h + 2

    for text, f in bottom:
        d.text(((W - d.textlength(text, font=f)) / 2, y), text, font=f, fill=0)
        y += h(f) + gap
    return img, unit


def tspl_job(labels, width_mm, height_mm, gap_mm=2.0, flip=False):
    """labels: [(صورة، عدد النسخ)] → أوامر TSPL جاهزة للإرسال. في BITMAP البت 0 = نقطة سوداء، وهو
    نفس ترميز صور Pillow بالأبيض والأسود، فتُرسل بايتات الصورة كما هي."""
    out = [f"SIZE {width_mm:g} mm,{height_mm:g} mm\r\n", f"GAP {gap_mm:g} mm,0 mm\r\n",
           f"DIRECTION {0 if flip else 1}\r\n", "REFERENCE 0,0\r\n", "DENSITY 8\r\n", "SPEED 4\r\n"]
    data = b"".join(s.encode("ascii") for s in out)
    for img, copies in labels:
        wb = (img.width + 7) // 8
        data += b"CLS\r\n" + f"BITMAP 0,0,{wb},{img.height},0,".encode("ascii") + img.tobytes() + b"\r\n"
        data += f"PRINT 1,{int(copies)}\r\n".encode("ascii")
    return data


def list_printers():
    """[(اسم الطابعة، المنفذ)] من سجل ويندوز (أسرع وأضمن من تشغيل أوامر خارجية). المنفذ مهم: تعريفة
    ملصقات على COM3 بينما الطابعة واصلة على USB لا يطلع منها شيء (حصل عند التركيب الأول)."""
    try:
        import winreg
        base = r"SYSTEM\CurrentControlSet\Control\Print\Printers"
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
        out, i = [], 0
        while True:
            try:
                name = winreg.EnumKey(key, i)
            except OSError:
                break
            i += 1
            if name.startswith("S-1-5-"):  # نسخ OneNote الخاصة بكل مستخدم
                continue
            try:
                port = winreg.QueryValueEx(winreg.OpenKey(key, name), "Port")[0]
            except OSError:
                port = ""
            out.append((name, port))
        return sorted(out)
    except Exception:
        return []


def guess_label_printer(printers):
    """أول طابعة ملصقات على منفذ USB، وإلا أول طابعة ملصقات."""
    label_like = [n for n, _ in printers
                  if any(k in n.lower() for k in ("4barcode", "barcode", "label", "xp-235", "tsc", "zebra", "godex"))]
    usb = [n for n, p in printers if n in label_like and p.upper().startswith("USB")]
    return (usb or label_like or [None])[0]


def send_raw(printer_name, data, doc_name="ZG labels"):
    """يرسل بايتات خام للطابعة عبر مخزن الطباعة في ويندوز (winspool) بنوع RAW فلا تعيد التعريفة رسمها."""
    import ctypes
    from ctypes import wintypes
    winspool = ctypes.WinDLL("winspool.drv", use_last_error=True)

    class DOC_INFO_1(ctypes.Structure):
        _fields_ = [("pDocName", wintypes.LPWSTR), ("pOutputFile", wintypes.LPWSTR), ("pDatatype", wintypes.LPWSTR)]

    h = wintypes.HANDLE()
    if not winspool.OpenPrinterW(printer_name, ctypes.byref(h), None):
        raise OSError(f"ما قدرتش نفتح الطابعة \"{printer_name}\" (خطأ {ctypes.get_last_error()}).")
    try:
        info = DOC_INFO_1(doc_name, None, "RAW")
        if not winspool.StartDocPrinterW(h, 1, ctypes.byref(info)):
            raise OSError(f"الطابعة رفضت بدء الطباعة (خطأ {ctypes.get_last_error()}).")
        try:
            winspool.StartPagePrinter(h)
            written = wintypes.DWORD()
            buf = ctypes.create_string_buffer(data, len(data))
            if not winspool.WritePrinter(h, buf, len(data), ctypes.byref(written)) or written.value != len(data):
                raise OSError(f"ما وصلتش البيانات كاملة للطابعة (خطأ {ctypes.get_last_error()}).")
            winspool.EndPagePrinter(h)
        finally:
            winspool.EndDocPrinter(h)
    finally:
        winspool.ClosePrinter(h)
