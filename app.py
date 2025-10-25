# app.py
import os
import re
import smtplib
from datetime import datetime, timedelta, date
from uuid import uuid4

from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from flask import (
    Flask, render_template_string, request, redirect,
    url_for, flash, jsonify, abort
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, login_required,
    logout_user, current_user
)
from passlib.hash import pbkdf2_sha256
from sqlalchemy import func, and_, or_

# -----------------------------
# Config
# -----------------------------
APP_NAME = "CTR STOCK SYSTEM"
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
    "DATABASE_URL",
    "sqlite:///" + os.path.join(BASE_DIR, "ctr_stock.db")
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# SMTP (สำหรับรีเซ็ตรหัสผ่าน)
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER or "no-reply@example.com")

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"

# -----------------------------
# Models
# -----------------------------
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    phone = db.Column(db.String(50), unique=False, nullable=True)
    name = db.Column(db.String(120), nullable=False, default="User")
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, raw):
        self.password_hash = pbkdf2_sha256.hash(raw)

    def check_password(self, raw):
        return pbkdf2_sha256.verify(raw, self.password_hash)


class PasswordResetToken(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    token = db.Column(db.String(64), unique=True, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used = db.Column(db.Boolean, default=False)
    via = db.Column(db.String(20), default="email")  # email/phone

    user = db.relationship("User")


class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Item(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(180), nullable=False, unique=True)
    category_id = db.Column(db.Integer, db.ForeignKey("category.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    category = db.relationship("Category")
    batches = db.relationship("StockBatch", backref="item", lazy="dynamic")
    movements = db.relationship("StockMovement", backref="item", lazy="dynamic")


class StockBatch(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=False)
    qty_received = db.Column(db.Integer, nullable=False)
    qty_remaining = db.Column(db.Integer, nullable=False)
    expiry_date = db.Column(db.Date, nullable=True)
    received_at = db.Column(db.DateTime, default=datetime.utcnow)


class StockMovement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=False)
    type = db.Column(db.String(10), nullable=False)  # 'receive' | 'issue'
    quantity = db.Column(db.Integer, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    note = db.Column(db.String(255), nullable=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("stock_batch.id"), nullable=True)

    batch = db.relationship("StockBatch")


# -----------------------------
# Helpers
# -----------------------------
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def init_db_with_admin():
    db.create_all()
    if not User.query.filter_by(email="admin@example.com").first():
        admin = User(email="admin@example.com", name="Admin", is_admin=True)
        admin.set_password("admin123")
        db.session.add(admin)
        db.session.commit()
        print(">> Created default admin: admin@example.com / admin123")


def send_email(to_email: str, subject: str, html: str, text: str = ""):
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASS:
        print("\n[DEV] Email not configured. Below is the message you would receive:")
        print(f"TO: {to_email}\nSUBJECT: {subject}\n{text or ''}\n{html}\n")
        return True
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to_email
    part1 = MIMEText(text or "", "plain", "utf-8")
    part2 = MIMEText(html, "html", "utf-8")
    msg.attach(part1)
    msg.attach(part2)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_FROM, to_email, msg.as_string())
    return True


def now_utc():
    return datetime.utcnow()


def fifo_issue(item: Item, qty: int) -> int:
    """ตัดสต็อคด้วย FIFO: คืนจำนวนที่ตัดได้จริง"""
    remain = qty
    # batches ที่ยังมีของ เรียงตามวันหมดอายุต่ำสุดก่อน, แล้วตามวันรับเข้า
    batches = (
        StockBatch.query.filter_by(item_id=item.id)
        .filter(StockBatch.qty_remaining > 0)
        .order_by(StockBatch.expiry_date.asc().nulls_last(), StockBatch.received_at.asc())
        .all()
    )
    for b in batches:
        if remain <= 0:
            break
        take = min(b.qty_remaining, remain)
        b.qty_remaining -= take
        remain -= take
        m = StockMovement(item_id=item.id, type="issue", quantity=take, batch_id=b.id)
        db.session.add(m)
    db.session.commit()
    return qty - remain  # issued


def item_summary_row(item: Item):
    # คงเหลือ = รวม qty_remaining ของทุก batch
    total_remain = db.session.query(func.coalesce(func.sum(StockBatch.qty_remaining), 0))\
        .filter(StockBatch.item_id == item.id).scalar() or 0

    # วันหมดอายุถัดไป (FIFO)
    next_exp = (
        StockBatch.query.filter_by(item_id=item.id)
        .filter(StockBatch.qty_remaining > 0, StockBatch.expiry_date.isnot(None))
        .order_by(StockBatch.expiry_date.asc())
        .first()
    )
    next_expiry = next_exp.expiry_date.strftime("%Y-%m-%d") if next_exp else "-"

    # วันที่รับเข้าล่าสุด/เบิกล่าสุด
    last_receive = (
        StockMovement.query.filter_by(item_id=item.id, type="receive")
        .order_by(StockMovement.timestamp.desc()).first()
    )
    last_issue = (
        StockMovement.query.filter_by(item_id=item.id, type="issue")
        .order_by(StockMovement.timestamp.desc()).first()
    )
    last_receive_at = last_receive.timestamp.strftime("%Y-%m-%d %H:%M") if last_receive else "-"
    last_issue_at = last_issue.timestamp.strftime("%Y-%m-%d %H:%M") if last_issue else "-"

    return {
        "id": item.id,
        "name": item.name,
        "category": item.category.name if item.category else "-",
        "remain": total_remain,
        "next_expiry": next_expiry,
        "last_receive": last_receive_at,
        "last_issue": last_issue_at,
    }


# -----------------------------
# Routes - Auth
# -----------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        user = User.query.filter(func.lower(User.email) == email).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for("items"))
        flash("อีเมลหรือรหัสผ่านไม่ถูกต้อง", "danger")
    return render_template_string(TPL_BASE, **tpl_ctx(title="เข้าสู่ระบบ", body=TPL_LOGIN))


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("ออกจากระบบแล้ว", "success")
    return redirect(url_for("login"))


# -----------------------------
# Routes - Password Reset
# -----------------------------
@app.route("/reset/request", methods=["GET", "POST"])
def reset_request():
    if request.method == "POST":
        via = request.form.get("via", "email")
        identifier = (request.form.get("identifier") or "").strip()
        q = User.query
        if via == "email":
            user = q.filter(func.lower(User.email) == identifier.lower()).first()
        else:
            user = q.filter(User.phone == identifier).first()
        if not user:
            flash("ไม่พบบัญชีผู้ใช้", "danger")
            return redirect(url_for("reset_request"))

        token = uuid4().hex
        t = PasswordResetToken(
            user_id=user.id, token=token, via=via,
            expires_at=now_utc() + timedelta(hours=1)
        )
        db.session.add(t)
        db.session.commit()
        reset_link = url_for("reset_form", token=token, _external=True)
        subject = f"{APP_NAME} – รีเซ็ตรหัสผ่าน"
        html = f"<p>กดลิงก์เพื่อรีเซ็ตรหัสผ่าน:</p><p><a href='{reset_link}'>{reset_link}</a></p>"
        send_email(user.email, subject, html, f"Reset Link: {reset_link}")
        flash("ส่งลิงก์รีเซ็ตรหัสผ่านแล้ว (ถ้าไม่ได้ตั้งค่า SMTP จะพิมพ์ลิงก์ใน console)", "success")
        return redirect(url_for("login"))

    return render_template_string(TPL_BASE, **tpl_ctx(title="รีเซ็ตรหัสผ่าน", body=TPL_RESET_REQUEST))


@app.route("/reset/<token>", methods=["GET", "POST"])
def reset_form(token):
    rec = PasswordResetToken.query.filter_by(token=token, used=False).first()
    if not rec or rec.expires_at < now_utc():
        flash("โทเค็นไม่ถูกต้องหรือหมดอายุ", "danger")
        return redirect(url_for("login"))
    if request.method == "POST":
        p1 = request.form.get("password") or ""
        p2 = request.form.get("password2") or ""
        if len(p1) < 6 or p1 != p2:
            flash("รหัสผ่านไม่ถูกต้องหรือไม่ตรงกัน", "danger")
        else:
            rec.user.set_password(p1)
            rec.used = True
            db.session.commit()
            flash("ตั้งรหัสผ่านใหม่เรียบร้อย", "success")
            return redirect(url_for("login"))
    return render_template_string(TPL_BASE, **tpl_ctx(title="ตั้งรหัสผ่านใหม่", body=TPL_RESET_FORM))


# -----------------------------
# Routes - Profile
# -----------------------------
@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        current_user.name = (request.form.get("name") or current_user.name).strip()
        current_user.phone = (request.form.get("phone") or "").strip() or None
        email = (request.form.get("email") or current_user.email).strip().lower()
        if email and email != current_user.email:
            if User.query.filter(func.lower(User.email) == email).first():
                flash("อีเมลนี้ถูกใช้แล้ว", "danger")
                return redirect(url_for("profile"))
            current_user.email = email
        new_pass = request.form.get("new_password") or ""
        if new_pass:
            if len(new_pass) < 6:
                flash("รหัสผ่านใหม่อย่างน้อย 6 ตัวอักษร", "danger")
                return redirect(url_for("profile"))
            current_user.set_password(new_pass)
        db.session.commit()
        flash("บันทึกโปรไฟล์แล้ว", "success")
        return redirect(url_for("profile"))
    return render_template_string(TPL_BASE, **tpl_ctx(title="แก้ไขโปรไฟล์", body=TPL_PROFILE))


# -----------------------------
# Routes - Items / Movement / Receive / Issue
# -----------------------------
@app.route("/")
@login_required
def root():
    return redirect(url_for("items"))


@app.route("/items")
@login_required
def items():
    # ค้นหาแบบ keyword
    q = (request.args.get("q") or "").strip()
    category_id = request.args.get("category_id") or ""
    qry = Item.query
    if q:
        like = f"%{q}%"
        qry = qry.filter(Item.name.ilike(like))
    if category_id.isdigit():
        qry = qry.filter(Item.category_id == int(category_id))
    items = qry.order_by(Item.name.asc()).all()

    # ตารางสรุป
    rows = [item_summary_row(i) for i in items]

    # กลุ่มตามประเภท
    categories = Category.query.order_by(Category.name.asc()).all()
    grouped = {}
    for c in categories:
        grouped[c.name] = [item_summary_row(i) for i in Item.query.filter_by(category_id=c.id).order_by(Item.name.asc()).all()]

    return render_template_string(
        TPL_BASE,
        **tpl_ctx(
            title="รายการสินค้า",
            body=TPL_ITEMS,
            rows=rows, categories=categories, grouped=grouped, q=q, category_id=category_id
        )
    )


@app.route("/movements")
@login_required
def movements():
    # ปุ่มค้นหาตามช่วงเวลา + ประเภท รับเข้า/เบิกออก
    t = request.args.get("type") or "all"  # all/receive/issue
    start = request.args.get("start") or ""
    end = request.args.get("end") or ""
    keyword = (request.args.get("q") or "").strip()

    qry = StockMovement.query.join(Item)
    if t in ("receive", "issue"):
        qry = qry.filter(StockMovement.type == t)
    if start:
        try:
            dt = datetime.strptime(start, "%Y-%m-%d")
            qry = qry.filter(StockMovement.timestamp >= dt)
        except:
            pass
    if end:
        try:
            dt2 = datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)
            qry = qry.filter(StockMovement.timestamp < dt2)
        except:
            pass
    if keyword:
        like = f"%{keyword}%"
        qry = qry.filter(or_(Item.name.ilike(like), StockMovement.note.ilike(like)))

    logs = qry.order_by(StockMovement.timestamp.desc()).all()
    return render_template_string(
        TPL_BASE,
        **tpl_ctx(title="ประวัติรับเข้า/เบิกออก", body=TPL_MOVEMENTS, logs=logs, t=t, start=start, end=end, q=keyword)
    )


@app.route("/items/new", methods=["GET", "POST"])
@login_required
def item_new():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        category_id = request.form.get("category_id")
        if not name:
            flash("กรุณากรอกชื่อสินค้า", "danger")
            return redirect(url_for("item_new"))
        if not (category_id and category_id.isdigit()):
            flash("กรุณาเลือกประเภท/กลุ่มสินค้า", "danger")
            return redirect(url_for("item_new"))
        if Item.query.filter(func.lower(Item.name) == name.lower()).first():
            flash("มีรายการนี้อยู่แล้ว", "danger")
            return redirect(url_for("item_new"))
        item = Item(name=name, category_id=int(category_id))
        db.session.add(item)
        db.session.commit()
        flash("เพิ่มรายการสำเร็จ", "success")
        return redirect(url_for("items"))
    cats = Category.query.order_by(Category.name.asc()).all()
    return render_template_string(TPL_BASE, **tpl_ctx(title="เพิ่มรายการ", body=TPL_ITEM_FORM, cats=cats, item=None))


@app.route("/items/<int:item_id>/edit", methods=["GET", "POST"])
@login_required
def item_edit(item_id):
    item = Item.query.get_or_404(item_id)
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        category_id = request.form.get("category_id")
        if not name:
            flash("กรุณากรอกชื่อสินค้า", "danger")
            return redirect(url_for("item_edit", item_id=item.id))
        if not (category_id and category_id.isdigit()):
            flash("กรุณาเลือกประเภท/กลุ่มสินค้า", "danger")
            return redirect(url_for("item_edit", item_id=item.id))
        # ตรวจชื่อซ้ำ
        dup = Item.query.filter(func.lower(Item.name) == name.lower(), Item.id != item.id).first()
        if dup:
            flash("มีชื่อสินค้านี้อยู่แล้ว", "danger")
            return redirect(url_for("item_edit", item_id=item.id))
        item.name = name
        item.category_id = int(category_id)
        db.session.commit()
        flash("บันทึกการแก้ไขแล้ว", "success")
        return redirect(url_for("items"))
    cats = Category.query.order_by(Category.name.asc()).all()
    return render_template_string(TPL_BASE, **tpl_ctx(title="แก้ไขรายการ", body=TPL_ITEM_FORM, cats=cats, item=item))


@app.route("/items/<int:item_id>/delete", methods=["POST"])
@login_required
def item_delete(item_id):
    item = Item.query.get_or_404(item_id)
    # จำกัดสิทธิ์ลบทั้งหมดเฉพาะแอดมิน (ปรับได้)
    if not current_user.is_admin:
        flash("เฉพาะผู้ดูแลระบบที่ลบได้", "danger")
        return redirect(url_for("items"))
    # ลบทุกรายการที่เกี่ยวข้อง
    StockMovement.query.filter_by(item_id=item.id).delete()
    StockBatch.query.filter_by(item_id=item.id).delete()
    db.session.delete(item)
    db.session.commit()
    flash("ลบรายการแล้ว", "success")
    return redirect(url_for("items"))


@app.route("/receive", methods=["GET", "POST"])
@login_required
def receive():
    if request.method == "POST":
        item_id = request.form.get("item_id")
        qty = int(request.form.get("qty") or 0)
        expiry = request.form.get("expiry") or ""
        note = request.form.get("note") or ""
        if not (item_id and item_id.isdigit()):
            flash("กรุณาเลือกสินค้า", "danger"); return redirect(url_for("receive"))
        if qty <= 0:
            flash("จำนวนต้องมากกว่า 0", "danger"); return redirect(url_for("receive"))
        exp = None
        if expiry:
            try: exp = datetime.strptime(expiry, "%Y-%m-%d").date()
            except: flash("รูปแบบวันหมดอายุไม่ถูกต้อง", "danger"); return redirect(url_for("receive"))
        batch = StockBatch(item_id=int(item_id), qty_received=qty, qty_remaining=qty, expiry_date=exp)
        db.session.add(batch); db.session.flush()
        mov = StockMovement(item_id=int(item_id), type="receive", quantity=qty, batch_id=batch.id, note=note)
        db.session.add(mov); db.session.commit()
        flash("บันทึกรับเข้าสำเร็จ", "success")
        return redirect(url_for("movements"))
    items = Item.query.order_by(Item.name.asc()).all()
    return render_template_string(TPL_BASE, **tpl_ctx(title="รับเข้าสินค้า", body=TPL_RECEIVE, items=items))


@app.route("/issue", methods=["GET", "POST"])
@login_required
def issue():
    if request.method == "POST":
        item_id = request.form.get("item_id")
        qty = int(request.form.get("qty") or 0)
        note = request.form.get("note") or ""
        if not (item_id and item_id.isdigit()):
            flash("กรุณาเลือกสินค้า", "danger"); return redirect(url_for("issue"))
        if qty <= 0:
            flash("จำนวนต้องมากกว่า 0", "danger"); return redirect(url_for("issue"))
        item = Item.query.get_or_404(int(item_id))
        issued = fifo_issue(item, qty)
        if issued < qty:
            flash(f"สต็อคไม่พอ ตัดได้เพียง {issued}", "warning")
        else:
            # เพิ่มโน้ตให้รายการ issue ล่าสุดก้อนสุดท้าย
            last = StockMovement.query.filter_by(item_id=item.id, type="issue").order_by(StockMovement.id.desc()).first()
            if last: last.note = note; db.session.commit()
            flash("บันทึกการเบิกเรียบร้อย", "success")
        return redirect(url_for("movements"))
    items = Item.query.order_by(Item.name.asc()).all()
    return render_template_string(TPL_BASE, **tpl_ctx(title="เบิกสินค้า", body=TPL_ISSUE, items=items))


# -----------------------------
# API (JSON)
# -----------------------------
@app.route("/api/items")
@login_required
def api_items():
    items = Item.query.order_by(Item.name.asc()).all()
    return jsonify([item_summary_row(i) for i in items])


# -----------------------------
# Category (minimal)
# -----------------------------
@app.route("/categories", methods=["GET", "POST"])
@login_required
def categories():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("กรุณากรอกชื่อประเภท", "danger")
        elif Category.query.filter(func.lower(Category.name) == name.lower()).first():
            flash("มีประเภทนี้แล้ว", "danger")
        else:
            db.session.add(Category(name=name)); db.session.commit()
            flash("เพิ่มประเภทแล้ว", "success")
        return redirect(url_for("categories"))
    cats = Category.query.order_by(Category.name.asc()).all()
    return render_template_string(TPL_BASE, **tpl_ctx(title="ประเภท/กลุ่มสินค้า", body=TPL_CATEGORIES, cats=cats))


# -----------------------------
# Templates (Jinja2 inline)
# -----------------------------
def tpl_ctx(title, body, **kwargs):
    ctx = dict(app_name=APP_NAME, title=title, body=body)
    ctx.update(kwargs); return ctx


TPL_BASE = r"""
{% set kebab_css = "dropdown" %}
<!doctype html>
<html lang="th">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ title }} | {{ app_name }}</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <style>
    body { background: #0f1218; color: #e9eef5; }
    .navbar, .dropdown-menu { background: #151a22; }
    a, .nav-link, .dropdown-item { color: #e9eef5; }
    .dropdown-item:hover { background:#1f2633; }
    .card { background: #151a22; border: 1px solid #252c3a; }
    .form-control, .form-select { background:#121722; border-color:#2b3447; color:#e9eef5; }
    .btn-primary { background:#5865f2; border-color:#5865f2; }
    .btn-outline-light { border-color:#394258; color:#d3dbef; }
    .table thead th { color:#98a2c4; }
    .table { color:#cfd8ff; }
    .kebab { border:none; background:transparent; color:#cfd8ff; }
    .kebab:after { display:none; }
    .badge-soft { background:#21293a; color:#a7b2d9; }
  </style>
</head>
<body>
<nav class="navbar navbar-expand-lg navbar-dark mb-4">
  <div class="container-fluid">
    <a class="navbar-brand" href="{{ url_for('items') }}">{{ app_name }}</a>
    <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#nav"><span class="navbar-toggler-icon"></span></button>
    <div id="nav" class="collapse navbar-collapse">
      <ul class="navbar-nav me-auto">
        <li class="nav-item"><a class="nav-link" href="{{ url_for('items') }}">รายการสินค้า</a></li>
        <li class="nav-item"><a class="nav-link" href="{{ url_for('movements') }}">รับเข้า/เบิกออก</a></li>
        <li class="nav-item"><a class="nav-link" href="{{ url_for('receive') }}">รับเข้า</a></li>
        <li class="nav-item"><a class="nav-link" href="{{ url_for('issue') }}">เบิกออก</a></li>
        <li class="nav-item"><a class="nav-link" href="{{ url_for('categories') }}">ประเภทสินค้า</a></li>
      </ul>
      <ul class="navbar-nav">
        {% if current_user.is_authenticated %}
          <li class="nav-item"><a class="nav-link" href="{{ url_for('profile') }}">โปรไฟล์</a></li>
          <li class="nav-item"><a class="nav-link" href="{{ url_for('logout') }}">ออกจากระบบ</a></li>
        {% else %}
          <li class="nav-item"><a class="nav-link" href="{{ url_for('login') }}">เข้าสู่ระบบ</a></li>
        {% endif %}
      </ul>
    </div>
  </div>
</nav>

<div class="container mb-5">
  {% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}
      <div class="mb-3">
        {% for cat,msg in messages %}
          <div class="alert alert-{{ 'warning' if cat=='warning' else ('danger' if cat=='danger' else 'success') }}">{{ msg }}</div>
        {% endfor %}
      </div>
    {% endif %}
  {% endwith %}
  {{ body }}
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script>
  // ยืนยันก่อนลบ
  function confirmDelete(formId, text='ยืนยันที่จะลบใช่หรือไม่?') {
    if (confirm(text)) document.getElementById(formId).submit();
  }
</script>
</body>
</html>
"""

TPL_LOGIN = r"""
<div class="row justify-content-center">
  <div class="col-lg-5">
    <div class="card p-4">
      <h3 class="mb-3">เข้าสู่ระบบ</h3>
      <form method="post">
        <div class="mb-3"><label class="form-label">อีเมล</label><input class="form-control" name="email" type="email" required></div>
        <div class="mb-3"><label class="form-label">รหัสผ่าน</label><input class="form-control" name="password" type="password" required></div>
        <button class="btn btn-primary w-100">เข้าสู่ระบบ</button>
      </form>
      <div class="mt-3"><a href="{{ url_for('reset_request') }}">ลืมรหัสผ่าน?</a></div>
    </div>
  </div>
</div>
"""

TPL_RESET_REQUEST = r"""
<div class="row justify-content-center">
  <div class="col-lg-6">
    <div class="card p-4">
      <h4 class="mb-3">ขอรีเซ็ตรหัสผ่าน</h4>
      <form method="post">
        <div class="mb-2">
          <label class="form-label">วิธีการ</label>
          <select name="via" class="form-select">
            <option value="email">อีเมล</option>
            <option value="phone">เบอร์โทรศัพท์</option>
          </select>
        </div>
        <div class="mb-3">
          <label class="form-label">อีเมลหรือเบอร์</label>
          <input class="form-control" name="identifier" placeholder="example@domain.com หรือ 08xxxxxxx" required>
        </div>
        <button class="btn btn-primary">ส่งลิงก์รีเซ็ต</button>
      </form>
      <p class="text-secondary mt-3 small">* หากไม่ตั้งค่า SMTP ระบบจะพิมพ์ลิงก์รีเซ็ตใน console เพื่อทดสอบ</p>
    </div>
  </div>
</div>
"""

TPL_RESET_FORM = r"""
<div class="row justify-content-center">
  <div class="col-lg-5">
    <div class="card p-4">
      <h4 class="mb-3">ตั้งรหัสผ่านใหม่</h4>
      <form method="post">
        <div class="mb-3"><label class="form-label">รหัสผ่านใหม่</label><input class="form-control" name="password" type="password" required></div>
        <div class="mb-3"><label class="form-label">ยืนยันรหัสผ่านใหม่</label><input class="form-control" name="password2" type="password" required></div>
        <button class="btn btn-primary w-100">บันทึก</button>
      </form>
    </div>
  </div>
</div>
"""

TPL_PROFILE = r"""
<div class="row justify-content-center">
  <div class="col-lg-7">
    <div class="card p-4">
      <h4 class="mb-3">โปรไฟล์ของฉัน</h4>
      <form method="post">
        <div class="row g-3">
          <div class="col-md-6"><label class="form-label">ชื่อที่แสดง</label><input class="form-control" name="name" value="{{ current_user.name }}" required></div>
          <div class="col-md-6"><label class="form-label">อีเมล</label><input class="form-control" name="email" type="email" value="{{ current_user.email }}" required></div>
          <div class="col-md-6"><label class="form-label">เบอร์โทรศัพท์</label><input class="form-control" name="phone" value="{{ current_user.phone or '' }}"></div>
          <div class="col-md-6"><label class="form-label">รหัสผ่านใหม่ (ถ้าต้องการเปลี่ยน)</label><input class="form-control" name="new_password" type="password"></div>
        </div>
        <div class="mt-3 d-flex gap-2">
          <button class="btn btn-primary">บันทึก</button>
          <a class="btn btn-outline-light" href="{{ url_for('items') }}">กลับ</a>
        </div>
      </form>
    </div>
  </div>
</div>
"""

TPL_ITEMS = r"""
<div class="card p-3 mb-3">
  <form method="get" class="row g-2">
    <div class="col-md-4"><input class="form-control" name="q" value="{{ q }}" placeholder="ค้นหาชื่อสินค้า..."></div>
    <div class="col-md-4">
      <select name="category_id" class="form-select">
        <option value="">ทุกประเภท</option>
        {% for c in categories %}
          <option value="{{ c.id }}" {% if str(c.id)==str(category_id) %}selected{% endif %}>{{ c.name }}</option>
        {% endfor %}
      </select>
    </div>
    <div class="col-md-4"><button class="btn btn-primary w-100">ค้นหา</button></div>
  </form>
</div>

<div class="card p-3 mb-4">
  <div class="d-flex justify-content-between align-items-center">
    <h5 class="m-0">รายการสินค้าทั้งหมด (สรุป)</h5>
    <div class="d-flex gap-2">
      <a class="btn btn-sm btn-outline-light" href="{{ url_for('item_new') }}">เพิ่มรายการ</a>
    </div>
  </div>
  <div class="table-responsive mt-3">
    <table class="table table-dark table-hover align-middle">
      <thead><tr>
        <th>ชื่อรายการ</th><th>ประเภท</th><th class="text-end">คงเหลือ</th><th>วันหมดอายุ (FIFO)</th><th>รับเข้าล่าสุด</th><th>เบิกล่าสุด</th><th class="text-end">จัดการ</th>
      </tr></thead>
      <tbody>
      {% for r in rows %}
        <tr>
          <td>{{ r.name }}</td>
          <td><span class="badge badge-soft">{{ r.category }}</span></td>
          <td class="text-end">{{ r.remain }}</td>
          <td>{{ r.next_expiry }}</td>
          <td>{{ r.last_receive }}</td>
          <td>{{ r.last_issue }}</td>
          <td class="text-end">
            <div class="dropdown">
              <button class="btn kebab" data-bs-toggle="dropdown" aria-expanded="false">⋮</button>
              <ul class="dropdown-menu dropdown-menu-end">
                <li><a class="dropdown-item" href="{{ url_for('item_edit', item_id=r.id) }}">แก้ไข</a></li>
                <li><hr class="dropdown-divider"></li>
                <li>
                  <form id="del-{{ r.id }}" method="post" action="{{ url_for('item_delete', item_id=r.id) }}">
                    <a class="dropdown-item text-danger" href="javascript:void(0)" onclick="confirmDelete('del-{{ r.id }}')">ลบ</a>
                  </form>
                </li>
              </ul>
            </div>
          </td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</div>

<div class="card p-3">
  <h5 class="mb-3">รายการสินค้าแยกตามประเภท/กลุ่ม</h5>
  {% for cname, items in grouped.items() %}
    <div class="mb-3">
      <h6 class="text-uppercase text-secondary">{{ cname }}</h6>
      <div class="table-responsive">
        <table class="table table-dark table-sm">
          <thead><tr>
            <th>ชื่อรายการ</th><th class="text-end">คงเหลือ</th><th>วันหมดอายุ (FIFO)</th><th>รับเข้าล่าสุด</th><th>เบิกล่าสุด</th>
          </tr></thead>
          <tbody>
          {% for r in items %}
            <tr>
              <td>{{ r.name }}</td>
              <td class="text-end">{{ r.remain }}</td>
              <td>{{ r.next_expiry }}</td>
              <td>{{ r.last_receive }}</td>
              <td>{{ r.last_issue }}</td>
            </tr>
          {% else %}
            <tr><td colspan="5" class="text-center text-secondary">ไม่มีสินค้าในกลุ่มนี้</td></tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
  {% endfor %}
</div>
"""

TPL_MOVEMENTS = r"""
<div class="card p-3 mb-3">
  <form method="get" class="row g-2">
    <div class="col-md-3">
      <label class="form-label">ประเภท</label>
      <select name="type" class="form-select">
        <option value="all" {% if t=='all' %}selected{% endif %}>ทั้งหมด</option>
        <option value="receive" {% if t=='receive' %}selected{% endif %}>รับเข้า</option>
        <option value="issue" {% if t=='issue' %}selected{% endif %}>เบิกออก</option>
      </select>
    </div>
    <div class="col-md-3">
      <label class="form-label">เริ่ม</label>
      <input class="form-control" type="date" name="start" value="{{ start }}">
    </div>
    <div class="col-md-3">
      <label class="form-label">สิ้นสุด</label>
      <input class="form-control" type="date" name="end" value="{{ end }}">
    </div>
    <div class="col-md-3">
      <label class="form-label">คำค้น</label>
      <div class="input-group">
        <input class="form-control" name="q" value="{{ q }}" placeholder="ชื่อสินค้า/หมายเหตุ">
        <button class="btn btn-primary">ค้นหา</button>
      </div>
    </div>
  </form>
</div>

<div class="card p-3">
  <h5 class="mb-3">ประวัติรับเข้า/เบิกออก</h5>
  <div class="table-responsive">
    <table class="table table-dark table-hover align-middle">
      <thead><tr><th>เวลา</th><th>ประเภท</th><th>สินค้า</th><th class="text-end">จำนวน</th><th>Batch</th><th>หมายเหตุ</th></tr></thead>
      <tbody>
      {% for m in logs %}
        <tr>
          <td>{{ m.timestamp.strftime('%Y-%m-%d %H:%M') }}</td>
          <td>
            {% if m.type=='receive' %}<span class="badge bg-success">รับเข้า</span>
            {% else %}<span class="badge bg-danger">เบิกออก</span>{% endif %}
          </td>
          <td>{{ m.item.name }}</td>
          <td class="text-end">{{ m.quantity }}</td>
          <td>{{ m.batch_id or '-' }}</td>
          <td>{{ m.note or '-' }}</td>
        </tr>
      {% else %}
        <tr><td colspan="6" class="text-center text-secondary">ไม่พบข้อมูล</td></tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</div>
"""

TPL_ITEM_FORM = r"""
<div class="row justify-content-center">
  <div class="col-lg-6">
    <div class="card p-4">
      <h4 class="mb-3">{{ 'แก้ไขรายการ' if item else 'เพิ่มรายการใหม่' }}</h4>
      <form method="post">
        <div class="mb-3"><label class="form-label">ชื่อสินค้า</label>
          <input class="form-control" name="name" value="{{ item.name if item else '' }}" required>
        </div>
        <div class="mb-3"><label class="form-label">ประเภท/กลุ่มสินค้า</label>
          <select class="form-select" name="category_id" required>
            <option value="">-- เลือกประเภท --</option>
            {% for c in cats %}
              <option value="{{ c.id }}" {% if item and item.category_id==c.id %}selected{% endif %}>{{ c.name }}</option>
            {% endfor %}
          </select>
        </div>
        <div class="d-flex gap-2">
          <button class="btn btn-primary">บันทึก</button>
          <a class="btn btn-outline-light" href="{{ url_for('items') }}">ยกเลิก</a>
        </div>
      </form>
    </div>
  </div>
</div>
"""

TPL_RECEIVE = r"""
<div class="row justify-content-center">
  <div class="col-lg-7">
    <div class="card p-4">
      <h4 class="mb-3">รับเข้าสินค้า</h4>
      <form method="post" class="row g-3">
        <div class="col-md-6">
          <label class="form-label">สินค้า</label>
          <select name="item_id" class="form-select" required>
            <option value="">-- เลือกสินค้า --</option>
            {% for it in items %}
              <option value="{{ it.id }}">{{ it.name }}</option>
            {% endfor %}
          </select>
        </div>
        <div class="col-md-3">
          <label class="form-label">จำนวน</label>
          <input class="form-control" name="qty" type="number" min="1" required>
        </div>
        <div class="col-md-3">
          <label class="form-label">วันหมดอายุ (ถ้ามี)</label>
          <input class="form-control" name="expiry" type="date">
        </div>
        <div class="col-12">
          <label class="form-label">หมายเหตุ</label>
          <input class="form-control" name="note" placeholder="เช่น Lot, Ref, ผู้รับเข้า ฯลฯ">
        </div>
        <div class="col-12 d-flex gap-2">
          <button class="btn btn-primary">บันทึก</button>
          <a class="btn btn-outline-light" href="{{ url_for('movements') }}">ดูประวัติ</a>
        </div>
      </form>
    </div>
  </div>
</div>
"""

TPL_ISSUE = r"""
<div class="row justify-content-center">
  <div class="col-lg-7">
    <div class="card p-4">
      <h4 class="mb-3">เบิกสินค้า</h4>
      <form method="post" class="row g-3">
        <div class="col-md-6">
          <label class="form-label">สินค้า</label>
          <select name="item_id" class="form-select" required>
            <option value="">-- เลือกสินค้า --</option>
            {% for it in items %}
              <option value="{{ it.id }}">{{ it.name }}</option>
            {% endfor %}
          </select>
        </div>
        <div class="col-md-3">
          <label class="form-label">จำนวน</label>
          <input class="form-control" name="qty" type="number" min="1" required>
        </div>
        <div class="col-md-3">
          <label class="form-label">หมายเหตุ</label>
          <input class="form-control" name="note" placeholder="ผู้เบิก/เหตุผล/เอกสารอ้างอิง">
        </div>
        <div class="col-12 d-flex gap-2">
          <button class="btn btn-primary">บันทึก</button>
          <a class="btn btn-outline-light" href="{{ url_for('movements') }}">ดูประวัติ</a>
        </div>
      </form>
    </div>
  </div>
</div>
"""

TPL_CATEGORIES = r"""
<div class="row">
  <div class="col-lg-6">
    <div class="card p-3">
      <h5 class="mb-3">เพิ่มประเภท</h5>
      <form method="post" class="d-flex gap-2">
        <input class="form-control" name="name" placeholder="เช่น วัตถุดิบ / บรรจุภัณฑ์" required>
        <button class="btn btn-primary">บันทึก</button>
      </form>
    </div>
  </div>
  <div class="col-lg-6">
    <div class="card p-3">
      <h5 class="mb-3">รายการประเภท</h5>
      <ul class="list-group">
        {% for c in cats %}
          <li class="list-group-item d-flex justify-content-between align-items-center">
            <span>{{ c.name }}</span>
            <span class="badge bg-secondary">{{ c.id }}</span>
          </li>
        {% else %}
          <li class="list-group-item text-secondary">ยังไม่มีประเภท</li>
        {% endfor %}
      </ul>
    </div>
  </div>
</div>
"""

# -----------------------------
# Main
# -----------------------------
if __name__ == "__main__":
    with app.app_context():
        init_db_with_admin()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
