// ---------------------------------------------------------------------------
// نقطة البيع - إدارة السلة، قراءة الباركود، وطرق الدفع (نقدي/بنكي/آجل/مزدوج)
// ---------------------------------------------------------------------------
const cart = {};
// ترتيب إضافة الأصناف (مفاتيح الكائن الرقمية تُرتَّب تلقائيًا بالرقم وليس بالإضافة، فنحفظ الترتيب هنا ليظهر الأحدث آخر الجدول)
let cartOrder = [];
function cartItems() { return cartOrder.filter((id) => cart[id]).map((id) => cart[id]); }
const barcodeInput = document.getElementById("barcodeInput");
const scanMessage = document.getElementById("scanMessage");
const cartTableBody = document.querySelector("#cartTable tbody");
const subtotalLabel = document.getElementById("subtotalLabel");
const totalLabel = document.getElementById("totalLabel");
const discountInput = document.getElementById("discountInput");
const paymentMethod = document.getElementById("paymentMethod");
const bankNameField = document.getElementById("bankNameField");
const splitFields = document.getElementById("splitFields");
const splitCash = document.getElementById("splitCash");
const splitBank = document.getElementById("splitBank");
const splitCredit = document.getElementById("splitCredit");
const splitCheck = document.getElementById("splitCheck");
const customerSelect = document.getElementById("customerSelect");
const customerSearchInput = document.getElementById("customerSearchInput");
const customerSearchResults = document.getElementById("customerSearchResults");
const customerClearBtn = document.getElementById("customerClearBtn");
const creditInfo = document.getElementById("creditInfo");

function showMessage(text, type) {
    scanMessage.innerHTML = `<span class="text-${type}">${text}</span>`;
    setTimeout(() => (scanMessage.innerHTML = ""), 2500);
}

// اسم المنتج مع باركوده (طلب المالك: الباركود يظهر أينما ظهر الاسم)، مهرَّب لأنه يدخل innerHTML
function productLabel(p) {
    return escapeHtml(p.name) + (p.barcode ? ` (${escapeHtml(p.barcode)})` : "");
}

function addToCart(product) {
    if (cart[product.id]) {
        if (cart[product.id].qty + 1 > product.quantity) {
            showMessage(`الكمية المتوفرة من "${productLabel(product)}" غير كافية`, "danger");
            return;
        }
        cart[product.id].qty += 1;
    } else {
        if (product.quantity <= 0) {
            showMessage(`المنتج "${productLabel(product)}" غير متوفر بالمخزون`, "danger");
            return;
        }
        cart[product.id] = { id: product.id, name: product.name, barcode: product.barcode, price: product.price, qty: 1, maxQty: product.quantity };
        cartOrder = cartOrder.filter((id) => id !== String(product.id) && id !== product.id);
        cartOrder.push(product.id);
    }
    renderCart();
    const lastRow = cartTableBody.querySelector(`tr[data-row="${product.id}"]`);
    if (lastRow) lastRow.scrollIntoView({ block: "nearest" });
    showMessage(`تمت إضافة: ${productLabel(product)}`, "success");
}

function renderCart() {
    cartTableBody.innerHTML = "";
    let subtotal = 0;
    const items = cartItems();
    const cartCount = document.getElementById("cartCount"), cartPieces = document.getElementById("cartPieces");
    if (cartCount) cartCount.textContent = items.length;
    if (cartPieces) cartPieces.textContent = items.reduce((a, i) => a + i.qty, 0);
    items.forEach((item) => {
        const lineTotal = item.qty * item.price;
        subtotal += lineTotal;
        const tr = document.createElement("tr");
        tr.dataset.row = item.id;
        const priceCell = (typeof CAN_OVERRIDE_PRICE !== "undefined" && CAN_OVERRIDE_PRICE)
            ? `<input type="number" step="0.01" min="0" class="form-control form-control-sm price-input" data-id="${item.id}" value="${item.price}" style="width:90px">`
            : `${item.price.toFixed(2)}`;
        tr.innerHTML = `
            <td>${item.name}<div class="small text-muted"><span class="font-monospace" dir="ltr">${item.barcode || ""}</span>
                <span class="${item.maxQty - item.qty <= 2 ? "text-danger" : ""}"> · متبقي ${item.maxQty - item.qty}</span></div></td>
            <td>${priceCell}</td>
            <td>
                <div class="input-group input-group-sm" style="width:120px">
                    <button class="btn btn-outline-secondary qty-minus" data-id="${item.id}">-</button>
                    <input type="number" class="form-control text-center qty-input" data-id="${item.id}" value="${item.qty}" min="1">
                    <button class="btn btn-outline-secondary qty-plus" data-id="${item.id}">+</button>
                </div>
            </td>
            <td>${lineTotal.toFixed(2)}</td>
            <td><button class="btn btn-sm btn-outline-danger remove-item" data-id="${item.id}"><i class="bi bi-trash"></i></button></td>
        `;
        cartTableBody.appendChild(tr);
    });
    subtotalLabel.textContent = subtotal.toFixed(2);
    updateTotal(subtotal);
}

function currentSubtotal() {
    return cartItems().reduce((sum, i) => sum + i.qty * i.price, 0);
}

function currentTotal() {
    const subtotal = currentSubtotal();
    const discount = parseFloat(discountInput.value) || 0;
    return Math.max(subtotal - discount, 0);
}

function updateTotal() {
    const total = currentTotal();
    totalLabel.textContent = total.toFixed(2);
    const big = document.getElementById("bigTotal");
    if (big) big.textContent = total.toFixed(2);
    const pieces = document.getElementById("bigPieces");
    if (pieces) pieces.textContent = cartItems().reduce((a, i) => a + i.qty, 0);
    autoFillSplit();
    updateChange();
}

// ---- المبلغ المستلم والباقي (للعرض فقط، لا يُحفظ) ----
const tenderedInput = document.getElementById("tenderedInput");
const changeLabel = document.getElementById("changeLabel");
const cashTenderBox = document.getElementById("cashTenderBox");
function updateChange() {
    if (!tenderedInput) return;
    const total = currentTotal();
    const given = parseFloat(tenderedInput.value);
    if (isNaN(given) || given <= 0 || !total) {
        changeLabel.textContent = "-";
        changeLabel.className = "fs-3 fw-bold";
        return;
    }
    const change = Math.round((given - total) * 100) / 100;
    if (change < 0) {
        changeLabel.textContent = `ناقص ${(-change).toFixed(2)}`;
        changeLabel.className = "fs-3 fw-bold text-danger";
    } else {
        changeLabel.textContent = change.toFixed(2);
        changeLabel.className = "fs-3 fw-bold text-success";
    }
}
if (tenderedInput) {
    tenderedInput.addEventListener("input", updateChange);
    tenderedInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); document.getElementById("checkoutBtn").click(); }
    });
    document.getElementById("quickCash").addEventListener("click", (e) => {
        const btn = e.target.closest("button");
        if (!btn) return;
        const total = currentTotal();
        if (btn.dataset.exact) {
            tenderedInput.value = total.toFixed(2);
        } else {
            // أقرب مضاعف للفئة يغطي الإجمالي (مثلًا إجمالي 37 وفئة 20 = 40)
            const v = parseFloat(btn.dataset.v);
            tenderedInput.value = (Math.max(1, Math.ceil(total / v)) * v).toFixed(2);
        }
        updateChange();
    });
}

function autoFillSplit() {
    const total = currentTotal();
    if (paymentMethod.value === "split" && !splitFields.dataset.userEdited) {
        splitCash.value = total.toFixed(2);
        splitBank.value = "0";
        splitCredit.value = "0";
    }
    validateSplit();
}

function validateSplit() {
    if (paymentMethod.value !== "split") return true;
    const total = currentTotal();
    const sum = (parseFloat(splitCash.value) || 0) + (parseFloat(splitBank.value) || 0) + (parseFloat(splitCredit.value) || 0);
    const diff = Math.abs(sum - total);
    if (diff > 0.01) {
        splitCheck.innerHTML = `<span class="text-danger">مجموع طرق الدفع (${sum.toFixed(2)}) لا يطابق الإجمالي (${total.toFixed(2)})</span>`;
        return false;
    }
    splitCheck.innerHTML = `<span class="text-success">مطابق للإجمالي ✓</span>`;
    return true;
}

// ---- أحداث الباركود ----
barcodeInput.addEventListener("keypress", function (e) {
    if (e.key === "Enter") {
        e.preventDefault();
        const code = barcodeInput.value.trim();
        barcodeInput.value = "";
        if (!code) return;
        fetch(`/pos/lookup?barcode=${encodeURIComponent(code)}`)
            .then((r) => r.text().then((text) => {
                let data;
                try { data = JSON.parse(text); }
                catch (e) {
                    throw new Error(`استجابة غير متوقعة من السيرفر (HTTP ${r.status}). قد تكون الجلسة انتهت، جرّب تسجيل الدخول من جديد.`);
                }
                return data;
            }))
            .then((data) => (data.error ? showMessage(data.error, "danger") : addToCart(data)))
            .catch((err) => showMessage(err.message || "تعذر الاتصال بالسيرفر المحلي. تأكد أن البرنامج ما زال يعمل.", "danger"));
    }
});

cartTableBody.addEventListener("click", function (e) {
    const id = e.target.closest("button")?.dataset.id;
    if (!id) return;
    if (e.target.closest(".qty-plus")) {
        if (cart[id].qty + 1 > cart[id].maxQty) { showMessage("الكمية المتوفرة غير كافية", "danger"); return; }
        cart[id].qty += 1;
    } else if (e.target.closest(".qty-minus")) {
        cart[id].qty = Math.max(1, cart[id].qty - 1);
    } else if (e.target.closest(".remove-item")) {
        delete cart[id];
    }
    renderCart();
    focusBarcode();
});

cartTableBody.addEventListener("change", function (e) {
    if (e.target.classList.contains("qty-input")) {
        const id = e.target.dataset.id;
        let val = parseInt(e.target.value) || 1;
        if (val > cart[id].maxQty) { val = cart[id].maxQty; showMessage("الكمية المتوفرة غير كافية", "danger"); }
        cart[id].qty = val;
        renderCart();
    } else if (e.target.classList.contains("price-input")) {
        const id = e.target.dataset.id;
        let val = parseFloat(e.target.value);
        if (isNaN(val) || val < 0) val = cart[id].price;
        cart[id].price = val;
        renderCart();
    }
    focusBarcode();
});

// إرجاع المؤشر لخانة الباركود بعد أي إجراء، حتى لا يضيع المسح التالي من القارئ
function focusBarcode() {
    setTimeout(() => barcodeInput.focus(), 0);
}

discountInput.addEventListener("input", () => updateTotal());

// ---- طريقة الدفع ----
paymentMethod.addEventListener("change", function () {
    bankNameField.style.display = (this.value === "bank" || this.value === "split") ? "block" : "none";
    splitFields.style.display = this.value === "split" ? "block" : "none";
    if (cashTenderBox) cashTenderBox.style.display = this.value === "cash" ? "block" : "none";
    delete splitFields.dataset.userEdited;
    autoFillSplit();
    checkCreditWarning();
});
[splitCash, splitBank, splitCredit].forEach((el) =>
    el.addEventListener("input", () => { splitFields.dataset.userEdited = "true"; validateSplit(); })
);

customerSelect.addEventListener("change", updateCreditInfo);

function updateCreditInfo() {
    if (!customerSelect.value) { creditInfo.textContent = ""; return; }
    const balance = parseFloat(customerSelect.dataset.balance) || 0;
    const limit = parseFloat(customerSelect.dataset.limit) || 0;
    if (limit > 0) {
        creditInfo.textContent = `الدين الحالي: ${balance.toFixed(2)} / سقف الائتمان: ${limit.toFixed(2)} (المتاح: ${(limit - balance).toFixed(2)})`;
    } else {
        creditInfo.textContent = `الدين الحالي: ${balance.toFixed(2)} (بدون سقف ائتمان محدد)`;
    }
    checkCreditWarning();
}

function checkCreditWarning() {
    const method = paymentMethod.value;
    if (!customerSelect.value || (method !== "credit" && method !== "split")) return;
    const balance = parseFloat(customerSelect.dataset.balance) || 0;
    const limit = parseFloat(customerSelect.dataset.limit) || 0;
    const creditPortion = method === "credit" ? currentTotal() : (parseFloat(splitCredit.value) || 0);
    if (limit > 0 && balance + creditPortion > limit) {
        creditInfo.innerHTML = `<span class="text-danger fw-bold">تحذير: سيتجاوز هذا البيع سقف الائتمان المسموح لهذا العميل!</span>`;
    }
}

// ---- بحث العميل بالاسم (بدل القائمة الطويلة) ----
function selectCustomer(customer) {
    customerSelect.value = customer.id;
    customerSelect.dataset.balance = customer.balance;
    customerSelect.dataset.limit = customer.limit;
    customerSearchInput.value = customer.name;
    hideCustomerResults();
    updateCreditInfo();
    focusBarcode();
}

function clearCustomerSelection() {
    customerSelect.value = "";
    delete customerSelect.dataset.balance;
    delete customerSelect.dataset.limit;
    customerSearchInput.value = "";
    hideCustomerResults();
    creditInfo.textContent = "";
}

function hideCustomerResults() {
    customerSearchResults.style.display = "none";
    customerSearchResults.innerHTML = "";
}

function renderCustomerResults(query) {
    const q = query.trim().toLowerCase();
    let matches;
    if (!q) {
        matches = ALL_CUSTOMERS.slice(0, 15); // بدون كتابة: أول 15 عميل كتلميح فقط
    } else {
        matches = ALL_CUSTOMERS.filter((c) => c.name.toLowerCase().includes(q)).slice(0, 30);
    }
    if (!matches.length) {
        customerSearchResults.innerHTML = `<div class="list-group-item text-muted small">لا يوجد عميل مطابق${q ? ` لـ "${query}"` : ""}</div>`;
        customerSearchResults.style.display = "block";
        return;
    }
    customerSearchResults.innerHTML = matches.map((c) => `
        <button type="button" class="list-group-item list-group-item-action py-2" data-cid="${c.id}">
            <div class="d-flex justify-content-between">
                <span>${c.name}</span>
                <span class="small ${c.balance > 0 ? "text-danger" : "text-muted"}">${c.balance.toFixed(2)}</span>
            </div>
        </button>`).join("");
    customerSearchResults.style.display = "block";
}

customerSearchInput.addEventListener("input", function () {
    if (customerSelect.value) clearCustomerSelection(); // المستخدم بدأ يكتب من جديد فوق اختيار سابق
    renderCustomerResults(this.value);
});
customerSearchInput.addEventListener("focus", function () {
    if (!customerSelect.value) renderCustomerResults(this.value);
});
customerSearchResults.addEventListener("click", function (e) {
    const btn = e.target.closest("button[data-cid]");
    if (!btn) return;
    const customer = ALL_CUSTOMERS.find((c) => String(c.id) === btn.dataset.cid);
    if (customer) selectCustomer(customer);
});
customerClearBtn.addEventListener("click", clearCustomerSelection);
document.addEventListener("click", function (e) {
    if (!e.target.closest("#customerSearchInput") && !e.target.closest("#customerSearchResults")) {
        hideCustomerResults();
    }
});

document.getElementById("clearCartBtn").addEventListener("click", function () {
    if (Object.keys(cart).length && !confirm("هل تريد إفراغ السلة؟")) return;
    Object.keys(cart).forEach((k) => delete cart[k]);
    cartOrder = [];
    if (tenderedInput) tenderedInput.value = "";
    renderCart();
    focusBarcode();
});

function buildPayload(force) {
    const method = paymentMethod.value;
    const total = currentTotal();
    let cash_amount = 0, bank_amount = 0, credit_amount = 0;
    if (method === "cash") cash_amount = total;
    else if (method === "bank") bank_amount = total;
    else if (method === "credit") credit_amount = total;
    else if (method === "split") {
        cash_amount = parseFloat(splitCash.value) || 0;
        bank_amount = parseFloat(splitBank.value) || 0;
        credit_amount = parseFloat(splitCredit.value) || 0;
    }
    return {
        warehouse_id: document.getElementById("warehouseSelect").value,
        customer_id: customerSelect.value || null,
        payment_method: method,
        bank_name: document.getElementById("bankNameInput").value || null,
        discount: parseFloat(discountInput.value) || 0,
        cash_amount, bank_amount, credit_amount,
        items: cartItems().map((i) => ({ id: i.id, qty: i.qty, price: i.price })),
        force: !!force,
    };
}

// منع تكرار الفاتورة: زر الإتمام يُقفل أثناء الحفظ (نقرتان سريعتان أو Enter من القارئ كانت تنشئ فاتورتين)
let checkoutInFlight = false;
const checkoutBtnEl = document.getElementById("checkoutBtn");
const checkoutBtnHtml = checkoutBtnEl.innerHTML;
function setCheckoutBusy(busy) {
    checkoutInFlight = busy;
    checkoutBtnEl.disabled = busy;
    checkoutBtnEl.innerHTML = busy
        ? '<span class="spinner-border spinner-border-sm"></span> جاري الحفظ...'
        : checkoutBtnHtml;
}

function submitCheckout(force) {
    if (checkoutInFlight) return;
    setCheckoutBusy(true);
    const payload = buildPayload(force);
    fetch("/pos/checkout", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    })
        .then((r) => r.text().then((text) => {
            let data;
            try { data = JSON.parse(text); }
            catch (e) {
                throw new Error(`استجابة غير متوقعة من السيرفر (HTTP ${r.status}). قد تكون الجلسة انتهت، جرّب تسجيل الدخول من جديد.`);
            }
            return { status: r.status, data };
        }))
        .then(({ status, data }) => {
            if (status === 409 && data.warning === "credit_limit_exceeded") {
                setCheckoutBusy(false);
                if (confirm(data.message + "\n\nهل تريد المتابعة على أي حال؟")) {
                    submitCheckout(true);
                }
                return;
            }
            if (data.error) {
                setCheckoutBusy(false);
                showMessage(data.error, "danger");
                return;
            }
            window.location.href = `/sales/invoice/${data.invoice_id}`;
            Object.keys(cart).forEach((k) => delete cart[k]);
            discountInput.value = 0;
            renderCart();
        })
        .catch((err) => {
            setCheckoutBusy(false);
            showMessage(err.message || "تعذر الاتصال بالسيرفر المحلي. تأكد أن البرنامج ما زال يعمل.", "danger");
        });
}

document.getElementById("checkoutBtn").addEventListener("click", function () {
    if (checkoutInFlight) return;
    if (!Object.keys(cart).length) { showMessage("السلة فارغة", "danger"); return; }
    if (paymentMethod.value === "credit" && !customerSelect.value) {
        showMessage("البيع الآجل يتطلب اختيار عميل", "danger");
        return;
    }
    if (paymentMethod.value === "split") {
        if (!validateSplit()) { showMessage("راجع توزيع طرق الدفع", "danger"); return; }
        const creditPortion = parseFloat(splitCredit.value) || 0;
        if (creditPortion > 0 && !customerSelect.value) {
            showMessage("الجزء الآجل من الدفع المزدوج يتطلب اختيار عميل", "danger");
            return;
        }
    }
    submitCheckout(false);
});

// ---- البحث عن منتج بالاسم ----
const productSearchInput = document.getElementById("productSearchInput");
const productSearchResults = document.getElementById("productSearchResults");
let productSearchTimer = null;
let productSearchItems = [];
function hideProductResults() {
    productSearchResults.style.display = "none";
    productSearchResults.innerHTML = "";
    productSearchItems = [];
}
function escapeHtml(t) {
    return String(t).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
if (productSearchInput) {
    productSearchInput.addEventListener("input", function () {
        clearTimeout(productSearchTimer);
        const q = this.value.trim();
        if (q.length < 2) { hideProductResults(); return; }
        productSearchTimer = setTimeout(() => {
            fetch(`/pos/search?q=${encodeURIComponent(q)}`)
                .then((r) => r.json())
                .then((items) => {
                    productSearchItems = items;
                    if (!items.length) {
                        productSearchResults.innerHTML = '<div class="list-group-item text-muted small">لا يوجد منتج مطابق</div>';
                    } else {
                        productSearchResults.innerHTML = items.map((p, i) => `
                            <button type="button" class="list-group-item list-group-item-action py-2 ${p.quantity <= 0 ? "text-muted" : ""}" data-i="${i}">
                                <div class="d-flex justify-content-between">
                                    <span>${escapeHtml(p.name)} <span class="small text-muted">${escapeHtml(p.size)} ${escapeHtml(p.color)}</span></span>
                                    <span class="fw-bold">${Number(p.price).toFixed(2)}</span>
                                </div>
                                <div class="small ${p.quantity <= 0 ? "text-danger" : "text-muted"}">متوفر: ${p.quantity}
                                    <span class="font-monospace" dir="ltr">${escapeHtml(p.barcode || "")}</span></div>
                            </button>`).join("");
                    }
                    productSearchResults.style.display = "block";
                })
                .catch(() => showMessage("تعذر البحث", "danger"));
        }, 200);
    });
    productSearchInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && productSearchItems.length === 1) {
            e.preventDefault();
            addToCart(productSearchItems[0]);
            productSearchInput.value = "";
            hideProductResults();
            focusBarcode();
        }
    });
    productSearchResults.addEventListener("click", (e) => {
        const btn = e.target.closest("button[data-i]");
        if (!btn) return;
        addToCart(productSearchItems[parseInt(btn.dataset.i)]);
        productSearchInput.value = "";
        hideProductResults();
        focusBarcode();
    });
    document.addEventListener("click", (e) => {
        if (!e.target.closest("#productSearchInput") && !e.target.closest("#productSearchResults")) hideProductResults();
    });
}

// ---- اختصارات لوحة المفاتيح ----
document.addEventListener("keydown", (e) => {
    const map = {
        F2: () => barcodeInput.focus(),
        F3: () => productSearchInput && productSearchInput.focus(),
        F4: () => customerSearchInput.focus(),
        F8: () => { if (paymentMethod.value === "cash" && tenderedInput) { tenderedInput.focus(); tenderedInput.select(); } },
        F9: () => document.getElementById("checkoutBtn").click(),
    };
    if (map[e.key]) { e.preventDefault(); map[e.key](); return; }
    if (e.key === "Escape") {
        hideProductResults();
        hideCustomerResults();
        focusBarcode();
    }
});

barcodeInput.focus();


// ---- مسح الباركود بالكاميرا (WebRTC + BarcodeDetector المدمج في المتصفح) ----
const cameraBtn = document.getElementById("cameraScanBtn");
const cameraModalEl = document.getElementById("cameraModal");
const cameraSelect = document.getElementById("cameraSelect");
const barcodeVideo = document.getElementById("barcodeVideo");
const cameraMessage = document.getElementById("cameraMessage");
let cameraStream = null;
let cameraTimer = null;

async function loadCameras() {
    const devices = await navigator.mediaDevices.enumerateDevices();
    const cameras = devices.filter(d => d.kind === "videoinput");
    cameraSelect.innerHTML = "";
    cameras.forEach((d, i) => {
        const opt = document.createElement("option");
        opt.value = d.deviceId;
        opt.textContent = d.label || `الكاميرا ${i + 1}`;
        cameraSelect.appendChild(opt);
    });
}

async function startCamera(deviceId) {
    if (!("BarcodeDetector" in window)) {
        cameraMessage.textContent = "هذا المتصفح لا يدعم قارئ الباركود المدمج. استخدم إدخال الباركود أو قارئ USB.";
        return;
    }
    if (cameraStream) cameraStream.getTracks().forEach(t => t.stop());
    try {
        cameraStream = await navigator.mediaDevices.getUserMedia({
            video: deviceId ? {deviceId: {exact: deviceId}} : {facingMode: "environment"}
        });
        barcodeVideo.srcObject = cameraStream;
        const wanted = ["ean_13","ean_8","code_128","code_39","upc_a","upc_e","itf"];
        const supported = BarcodeDetector.getSupportedFormats ? await BarcodeDetector.getSupportedFormats() : wanted;
        const formats = wanted.filter(f => supported.includes(f));
        const detector = formats.length ? new BarcodeDetector({formats}) : new BarcodeDetector();
        clearInterval(cameraTimer);
        cameraTimer = setInterval(async () => {
            if (barcodeVideo.readyState < 2) return;
            try {
                const codes = await detector.detect(barcodeVideo);
                if (!codes.length) return;
                const code = codes[0].rawValue;
                if (code) {
                    barcodeInput.value = code;
                    barcodeInput.dispatchEvent(new KeyboardEvent("keypress", {key:"Enter"}));
                    cameraMessage.textContent = `تمت قراءة: ${code}`;
                    setTimeout(() => stopCamera(), 500);
                }
            } catch (_) {}
        }, 250);
    } catch (err) {
        cameraMessage.textContent = "تعذر الوصول إلى الكاميرا. تأكد من إذن الكاميرا وأن الهاتف ظاهر ككاميرا ويب في Windows.";
    }
}

function stopCamera() {
    clearInterval(cameraTimer);
    cameraTimer = null;
    if (cameraStream) {
        cameraStream.getTracks().forEach(t => t.stop());
        cameraStream = null;
    }
    if (cameraModalEl) bootstrap.Modal.getOrCreateInstance(cameraModalEl).hide();
}

if (cameraBtn) {
    cameraBtn.addEventListener("click", async () => {
        if (!navigator.mediaDevices?.getUserMedia) {
            cameraMessage.textContent = "الكاميرا غير متاحة في هذا السياق. استخدم HTTPS أو localhost.";
            bootstrap.Modal.getOrCreateInstance(cameraModalEl).show();
            return;
        }
        try {
            // طلب الإذن أولاً كي تظهر أسماء الكاميرات.
            const stream = await navigator.mediaDevices.getUserMedia({video: true});
            stream.getTracks().forEach(t => t.stop());
            await loadCameras();
            bootstrap.Modal.getOrCreateInstance(cameraModalEl).show();
            await startCamera(cameraSelect.value);
        } catch (_) {
            bootstrap.Modal.getOrCreateInstance(cameraModalEl).show();
            cameraMessage.textContent = "يجب السماح للمتصفح باستخدام الكاميرا.";
        }
    });
}
if (cameraSelect) cameraSelect.addEventListener("change", () => startCamera(cameraSelect.value));
if (cameraModalEl) cameraModalEl.addEventListener("hidden.bs.modal", stopCamera);
