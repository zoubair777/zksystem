"""
أداة استرجاع كلمة المرور (لاستخدام المبرمج/الدعم الفني فقط)
==============================================================
تُستخدم عندما ينسى صاحب المنظومة كلمة مروره ولا يوجد حساب "admin" آخر
يمكنه تسجيل الدخول منه لإعادة تعيينها من داخل النظام نفسه (صفحة المستخدمين
تتطلب أصلاً أن تكون مسجّلاً دخولك كـ admin).

بما أن النظام يعمل بالكامل بلا انترنت (SQLite محلية)، لا يوجد "نسيت كلمة
المرور؟" عبر البريد الإلكتروني. الحل هو الوصول المباشر لملف قاعدة البيانات
على جهاز صاحب المنظومة (عن بُعد عبر أي برنامج دعم فني كـ AnyDesk، أو
بالحضور الفعلي) وتشغيل هذا الملف من نفس مجلد المشروع.

طريقة الاستخدام
----------------
1) اعرض كل المستخدمين المسجّلين (للتأكد من اسم المستخدم الصحيح):
       python reset_admin_password.py --list

2) صفّر كلمة مرور مستخدم معيّن إلى قيمة جديدة تحددها:
       python reset_admin_password.py --username admin --password "كلمة_مرور_جديدة"

3) أو أنشئ حساب admin طوارئ جديد بالكامل (في حال حُذف حساب admin نفسه بالخطأ،
   أو كانت هذه أول مرة ولا يوجد أي مستخدم بعد):
       python reset_admin_password.py --create-admin --username support --password "كلمة_مرور_جديدة"

بعد التشغيل، أخبر صاحب المنظومة بكلمة المرور الجديدة ليسجّل الدخول بها،
ثم يُفضَّل أن يغيّرها بنفسه من صفحة "تغيير كلمة المرور" داخل النظام.

⚠️ يجب إيقاف تشغيل النظام (إغلاق start.bat / start_desktop.bat / ERP.exe)
قبل تشغيل هذا الملف، حتى لا يحدث تعارض في الكتابة على ملف قاعدة البيانات.
"""
import argparse
import sys

from app import app
from models import db, User


def list_users():
    with app.app_context():
        users = User.query.all()
        if not users:
            print("لا يوجد أي مستخدمين في قاعدة البيانات حاليًا.")
            return
        print(f"{'اسم المستخدم':<20}{'الاسم الكامل':<25}{'الدور':<12}{'مفعّل؟'}")
        print("-" * 70)
        for u in users:
            print(f"{u.username:<20}{u.full_name:<25}{u.role:<12}{'نعم' if u.active else 'لا'}")


def reset_password(username, new_password):
    if len(new_password) < 4:
        print("خطأ: كلمة المرور الجديدة قصيرة جدًا (4 أحرف على الأقل).")
        sys.exit(1)
    with app.app_context():
        user = User.query.filter_by(username=username).first()
        if not user:
            print(f"خطأ: لا يوجد مستخدم باسم '{username}'. استخدم --list لعرض المستخدمين المتاحين.")
            sys.exit(1)
        user.set_password(new_password)
        user.active = True  # نضمن أن الحساب غير معطّل حتى يستطيع الدخول فعليًا
        db.session.commit()
        print(f"تم تعيين كلمة مرور جديدة للمستخدم '{username}' بنجاح.")
        print("يمكنه الآن تسجيل الدخول بكلمة المرور الجديدة.")


def create_emergency_admin(username, new_password):
    if len(new_password) < 4:
        print("خطأ: كلمة المرور الجديدة قصيرة جدًا (4 أحرف على الأقل).")
        sys.exit(1)
    with app.app_context():
        existing = User.query.filter_by(username=username).first()
        if existing:
            print(f"يوجد مستخدم بهذا الاسم أصلاً. استخدم --username {username} --password ... بدون --create-admin لتصفير كلمة مروره فقط.")
            sys.exit(1)
        admin = User(username=username, full_name="حساب طوارئ (دعم فني)", role="admin", active=True)
        admin.set_password(new_password)
        # صلاحية admin تُمنح تلقائيًا كل الصلاحيات في النظام (User.has_permission يتحقق من role=='admin')
        admin.set_permissions([])
        db.session.add(admin)
        db.session.commit()
        print(f"تم إنشاء حساب admin طوارئ باسم المستخدم '{username}' بنجاح.")
        print("يمكن تسجيل الدخول به الآن، ثم إنشاء/تصحيح باقي الحسابات من صفحة المستخدمين.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="أداة استرجاع/تصفير كلمة المرور (دعم فني - بلا انترنت)")
    parser.add_argument("--list", action="store_true", help="عرض كل المستخدمين المسجّلين")
    parser.add_argument("--username", help="اسم المستخدم المطلوب تصفير كلمة مروره أو إنشاؤه")
    parser.add_argument("--password", help="كلمة المرور الجديدة")
    parser.add_argument("--create-admin", action="store_true", help="إنشاء حساب admin طوارئ جديد بالكامل")
    args = parser.parse_args()

    if args.list:
        list_users()
    elif args.create_admin:
        if not args.username or not args.password:
            print("استخدم: python reset_admin_password.py --create-admin --username NAME --password PASS")
            sys.exit(1)
        create_emergency_admin(args.username, args.password)
    elif args.username and args.password:
        reset_password(args.username, args.password)
    else:
        parser.print_help()
