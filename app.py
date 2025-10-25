import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from flask import (
    Flask, render_template, request, redirect, url_for, flash
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user, login_required, current_user
)
from passlib.hash import pbkdf2_sha256
from dotenv import load_dotenv

# --------------------------------------------------
# ENV & APP
# --------------------------------------------------
load_dotenv()
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret")
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.join(BASE_DIR, 'ctr_stock.db')}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Email config (SMTP)
MAIL_SERVER = os.getenv("MAIL_SERVER", "")
MAIL_PORT = int(os.getenv("MAIL_PORT", "587"))           # 587 = STARTTLS
MAIL_USERNAME = os.getenv("MAIL_USERNAME", "")
MAIL_PASSWORD = os.getenv("MAIL_PASSWORD", "")
MAIL_USE_TLS = os.getenv("MAIL_USE_TLS", "true").lower() == "true"
MAIL_SENDER = os.getenv("MAIL_SENDER", MAIL_USERNAME or "no-reply@example.com")

# Token serializer for reset password
SECURITY_PASSWORD_SALT = os.getenv("SECURITY_PASSWORD_SALT", "salt-for-reset")
serializer = URLSafeTimedSerializer(app.secret_key)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login_get"

# --------------------------------------------------
# MODELS
# --------------------------------------------------
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    items = db.relationship("Item", backref="category", lazy=True)

class Item(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey("category.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class StockLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"))
    quantity = db.Column(db.Integer, nullable=False)
    type = db.Column(db.String(20), nullable=False)  # receive / issue
    expiry_date = db.Column(db.Date)
    batch_code = db.Column(db.String(50))
    note = db.Column(db.String(200))
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# --------------------------------------------------
# HELPERS
# --------------------------------------------------
def send_email(subject: str, recipient: str, html_body: str, text_body: str = "") -> bool:
    """ส่งอีเมลผ่าน SMTP (STARTTLS)"""
    if not (MAIL_SERVER and MAIL_USERNAME and MAIL_PASSWORD):
        app.logger.error("EMAIL NOT CONFIGURED: missing MAIL_SERVER/MAIL_USERNAME/MAIL_PASSWORD")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = MAIL_SENDER
    msg["To"] = recipient
    if text_body:
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    try:
        with smtplib.SMTP(MAIL_SERVER, MAIL_PORT) as server:
            if MAIL_USE_TLS:
                server.starttls()
            server.login(MAIL_USERNAME, MAIL_PASSWORD)
            server.sendmail(MAIL_SENDER, [recipient], msg.as_string())
        return True
    except Exception as e:
        app.logger.exception(f"Failed to send email: {e}")
        return False

def generate_reset_token(email: str) -> str:
    return serializer.dumps(email, salt=SECURITY_PASSWORD_SALT)

def verify_reset_token(token: str, max_age_seconds: int = 3600) -> str | None:
    """คืนค่า email ถ้า token ถูกต้องและยังไม่หมดอายุ (ดีฟอลต์ 1 ชม.)"""
    try:
        return serializer.loads(token, salt=SECURITY_PASSWORD_SALT, max_age=max_age_seconds)
    except (SignatureExpired, BadSignature):
        return None

# --------------------------------------------------
# LOGIN MANAGER
# --------------------------------------------------
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --------------------------------------------------
# AUTH
# --------------------------------------------------
@app.get("/login")
def login_get():
    return render_template("login.html", title="เข้าสู่ระบบ")

@app.post("/login")
def login_post():
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    user = User.query.filter_by(email=email).first()
    if not user or not pbkdf2_sha256.verify(password, user.password_hash):
        flash("อีเมลหรือรหัสผ่านไม่ถูกต้อง", "danger")
        return redirect(url_for("login_get"))
    login_user(user)
    flash("เข้าสู่ระบบสำเร็จ", "success")
    return redirect(url_for("dashboard"))

@app.get("/register")
def register_get():
    return render_template("register.html", title="สมัครสมาชิก")

@app.post("/register")
def register_post():
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    confirm = request.form.get("confirm", "")
    if password != confirm:
        flash("รหัสผ่านไม่ตรงกัน", "danger")
        return redirect(url_for("register_get"))
    if User.query.filter_by(email=email).first():
        flash("อีเมลนี้ถูกใช้งานแล้ว", "warning")
        return redirect(url_for("register_get"))
    user = User(email=email, password_hash=pbkdf2_sha256.hash(password))
    db.session.add(user)
    db.session.commit()
    flash("สมัครสมาชิกสำเร็จ! เข้าสู่ระบบได้เลย", "success")
    return redirect(url_for("login_get"))

@app.get("/logout")
@login_required
def logout():
    logout_user()
    flash("ออกจากระบบแล้ว", "info")
    return redirect(url_for("login_get"))

# ---------- Reset Password (FULL) ----------
@app.route("/reset_request", methods=["GET", "POST"])
def reset_request():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = User.query.filter_by(email=email).first()
        # เพื่อความปลอดภัย ไม่เฉลยว่าอีเมลมี/ไม่มี
        if user:
            token = generate_reset_token(user.email)
            reset_link = url_for("reset_password", token=token, _external=True)
            html = render_template("email_reset.html", reset_link=reset_link, email=user.email)
            text = f"กดลิงก์เพื่อตั้งรหัสผ่านใหม่: {reset_link}"
            ok = send_email("ตั้งรหัสผ่านใหม่ - CTR STOCK SYSTEM", user.email, html, text)
            if not ok:
                flash("ส่งอีเมลไม่สำเร็จ กรุณาตรวจสอบการตั้งค่าเมลของเซิร์ฟเวอร์", "danger")
                return redirect(url_for("reset_request"))
        flash("ถ้าอีเมลนี้อยู่ในระบบ เราได้ส่งลิงก์รีเซ็ตรหัสผ่านให้แล้ว (ลิงก์มีอายุ 60 นาที)", "info")
        return redirect(url_for("login_get"))
    return render_template("reset_request.html", title="ลืมรหัสผ่าน")

@app.route("/reset/<token>", methods=["GET", "POST"])
def reset_password(token):
    email = verify_reset_token(token, max_age_seconds=3600)
    if not email:
        flash("ลิงก์ไม่ถูกต้องหรือหมดอายุแล้ว", "danger")
        return redirect(url_for("reset_request"))
    user = User.query.filter_by(email=email).first()
    if not user:
        flash("ไม่พบบัญชีผู้ใช้", "danger")
        return redirect(url_for("reset_request"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if not password or password != confirm:
            flash("รหัสผ่านไม่ตรงกัน", "danger")
            return redirect(url_for("reset_password", token=token))
        user.password_hash = pbkdf2_sha256.hash(password)
        db.session.commit()
        flash("ตั้งรหัสผ่านใหม่สำเร็จ! กรุณาเข้าสู่ระบบด้วยรหัสใหม่", "success")
        return redirect(url_for("login_get"))

    return render_template("reset_password.html", email=email, title="ตั้งรหัสผ่านใหม่")

# --------------------------------------------------
# DASHBOARD
# --------------------------------------------------
@app.get("/")
@login_required
def dashboard():
    total_items = Item.query.count()
    total_categories = Category.query.count()
    total_receive = StockLog.query.filter_by(type="receive").count()
    total_issue = StockLog.query.filter_by(type="issue").count()
    latest_logs = db.session.query(
        StockLog.id, Item.name.label("item_name"), Category.name.label("category_name"),
        StockLog.quantity, StockLog.type, StockLog.created_at.label("date")
    ).join(Item, Item.id == StockLog.item_id).outerjoin(Category, Category.id == Item.category_id) \
     .order_by(StockLog.created_at.desc()).limit(10).all()
    return render_template(
        "dashboard.html",
        total_items=total_items,
        total_categories=total_categories,
        total_receive=total_receive,
        total_issue=total_issue,
        latest_logs=latest_logs,
        title="แดชบอร์ด"
    )

# --------------------------------------------------
# ITEMS
# --------------------------------------------------
@app.get("/items")
@login_required
def items():
    q = request.args.get("q", "").strip()
    category_id = request.args.get("category_id")
    query = Item.query
    if q:
        query = query.filter(Item.name.contains(q))
    if category_id:
        query = query.filter(Item.category_id == category_id)
    items = query.order_by(Item.name).all()

    data = []
    for it in items:
        receives = StockLog.query.filter_by(item_id=it.id, type="receive").all()
        issues = StockLog.query.filter_by(item_id=it.id, type="issue").all()
        balance = sum([r.quantity for r in receives]) - sum([i.quantity for i in issues])
        next_expiry = min([r.expiry_date for r in receives if r.expiry_date], default=None)
        last_receive = max([r.created_at for r in receives], default=None)
        last_issue = max([i.created_at for i in issues], default=None)
        data.append({
            "id": it.id,
            "name": it.name,
            "category_name": it.category.name if it.category else None,
            "balance": balance,
            "next_expiry": next_expiry,
            "last_received_at": last_receive,
            "last_issued_at": last_issue,
            "low_stock": balance <= 3
        })

    return render_template(
        "items.html",
        items=data,
        categories=Category.query.order_by(Category.name).all(),
        title="รายการสินค้า"
    )

@app.route("/item/create", methods=["GET", "POST"])
@login_required
def item_create():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        category_id = request.form.get("category_id")
        if not name:
            flash("กรุณากรอกชื่อสินค้า", "warning")
            return redirect(url_for("item_create"))
        if Item.query.filter_by(name=name).first():
            flash("ชื่อสินค้านี้มีอยู่แล้ว", "danger")
            return redirect(url_for("item_create"))
        item = Item(name=name, category_id=category_id or None)
        db.session.add(item)
        db.session.commit()
        flash("เพิ่มสินค้าเรียบร้อย", "success")
        return redirect(url_for("items"))
    return render_template(
        "item_form.html", categories=Category.query.order_by(Category.name).all()
    )

@app.route("/item/update/<int:item_id>", methods=["GET", "POST"])
@login_required
def item_update(item_id):
    item = Item.query.get_or_404(item_id)
    if request.method == "POST":
        new_name = request.form.get("name", "").strip()
        item.name = new_name
        db.session.commit()
        flash("แก้ไขชื่อสินค้าเรียบร้อย", "success")
        return redirect(url_for("items"))
    return render_template("item_form.html", item=item, categories=Category.query.all())

@app.post("/item/delete/<int:item_id>")
@login_required
def item_delete(item_id):
    item = Item.query.get_or_404(item_id)
    db.session.delete(item)
    db.session.commit()
    flash(f"ลบสินค้า {item.name} แล้ว", "info")
    return redirect(url_for("items"))

# --------------------------------------------------
# RECEIVE
# --------------------------------------------------
@app.get("/receive")
@login_required
def receive():
    logs = db.session.query(
        StockLog, Item.name.label("item_name"), Category.name.label("category_name"),
        User.email.label("actor_name")
    ).join(Item, Item.id == StockLog.item_id) \
     .outerjoin(Category, Category.id == Item.category_id) \
     .outerjoin(User, User.id == StockLog.actor_id) \
     .filter(StockLog.type == "receive") \
     .order_by(StockLog.created_at.desc()).all()

    data = []
    for log, item_name, category_name, actor_name in logs:
        data.append({
            "id": log.id,
            "item_name": item_name,
            "category_name": category_name,
            "quantity": log.quantity,
            "expiry_date": log.expiry_date,
            "batch_code": log.batch_code,
            "actor_name": actor_name,
            "created_at": log.created_at
        })

    return render_template(
        "receive.html",
        receive_logs=data,
        items_select=Item.query.order_by(Item.name).all(),
        categories=Category.query.order_by(Category.name).all(),
        title="รับเข้าสินค้า"
    )

@app.post("/receive")
@login_required
def receive_post():
    item_id = request.form.get("item_id")
    qty = int(request.form.get("quantity", 0))
    expiry = request.form.get("expiry_date")
    batch = request.form.get("batch_code")
    note = request.form.get("note")

    if not item_id or qty <= 0:
        flash("กรุณากรอกข้อมูลให้ครบถ้วน", "danger")
        return redirect(url_for("receive"))

    expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date() if expiry else None
    log = StockLog(
        item_id=item_id, quantity=qty, type="receive",
        expiry_date=expiry_date, batch_code=batch, note=note,
        actor_id=current_user.id
    )
    db.session.add(log)
    db.session.commit()
    flash("บันทึกรับเข้าสินค้าเรียบร้อย", "success")
    return redirect(url_for("receive"))

# --------------------------------------------------
# ISSUE
# --------------------------------------------------
@app.get("/issue")
@login_required
def issue():
    logs = db.session.query(
        StockLog, Item.name.label("item_name"), Category.name.label("category_name"),
        User.email.label("actor_name")
    ).join(Item, Item.id == StockLog.item_id) \
     .outerjoin(Category, Category.id == Item.category_id) \
     .outerjoin(User, User.id == StockLog.actor_id) \
     .filter(StockLog.type == "issue") \
     .order_by(StockLog.created_at.desc()).all()

    data = []
    for log, item_name, category_name, actor_name in logs:
        data.append({
            "id": log.id,
            "item_name": item_name,
            "category_name": category_name,
            "quantity": log.quantity,
            "actor_name": actor_name,
            "created_at": log.created_at
        })

    # คำนวณคงเหลือของแต่ละสินค้า
    items_with_balance = []
    for it in Item.query.all():
        rec = sum([r.quantity for r in StockLog.query.filter_by(item_id=it.id, type="receive").all()])
        iss = sum([i.quantity for i in StockLog.query.filter_by(item_id=it.id, type="issue").all()])
        items_with_balance.append({"id": it.id, "name": it.name, "balance": rec - iss})

    return render_template(
        "issue.html",
        issue_logs=data,
        items_select=items_with_balance,
        categories=Category.query.order_by(Category.name).all(),
        title="เบิกออกสินค้า"
    )

@app.post("/issue")
@login_required
def issue_post():
    item_id = request.form.get("item_id")
    qty = int(request.form.get("quantity", 0))
    note = request.form.get("note", "")

    rec = sum([r.quantity for r in StockLog.query.filter_by(item_id=item_id, type="receive").all()])
    iss = sum([i.quantity for i in StockLog.query.filter_by(item_id=item_id, type="issue").all()])
    balance = rec - iss

    if qty <= 0 or qty > balance:
        flash("จำนวนเบิกไม่ถูกต้อง หรือเกินจำนวนคงเหลือ", "danger")
        return redirect(url_for("issue"))

    log = StockLog(
        item_id=item_id, quantity=qty, type="issue",
        note=note, actor_id=current_user.id
    )
    db.session.add(log)
    db.session.commit()
    flash("บันทึกการเบิกออกเรียบร้อย", "success")
    return redirect(url_for("issue"))

# --------------------------------------------------
# CATEGORY
# --------------------------------------------------
@app.route("/categories", methods=["GET"])
@login_required
def categories():
    cats = Category.query.order_by(Category.name).all()
    data = []
    for c in cats:
        data.append({"id": c.id, "name": c.name, "item_count": len(c.items)})
    return render_template("categories.html", categories=data, title="ประเภทสินค้า")

@app.post("/category/create")
@login_required
def category_create():
    name = request.form.get("name", "").strip()
    if not name:
        flash("กรุณากรอกชื่อประเภทสินค้า", "warning")
        return redirect(url_for("categories"))
    if Category.query.filter_by(name=name).first():
        flash("ชื่อประเภทนี้มีอยู่แล้ว", "danger")
        return redirect(url_for("categories"))
    db.session.add(Category(name=name))
    db.session.commit()
    flash("เพิ่มประเภทสินค้าเรียบร้อย", "success")
    return redirect(url_for("categories"))

@app.post("/category/update")
@login_required
def category_update():
    cid = request.form.get("id")
    new_name = request.form.get("name", "").strip()
    cat = Category.query.get(cid)
    if cat:
        cat.name = new_name
        db.session.commit()
        flash("แก้ไขชื่อประเภทเรียบร้อย", "success")
    return redirect(url_for("categories"))

@app.post("/category/delete/<int:category_id>")
@login_required
def category_delete(category_id):
    cat = Category.query.get_or_404(category_id)
    db.session.delete(cat)
    db.session.commit()
    flash(f"ลบประเภท {cat.name} แล้ว", "info")
    return redirect(url_for("categories"))

# --------------------------------------------------
# REPORTS
# --------------------------------------------------
@app.get("/reports")
@login_required
def reports():
    cats = Category.query.order_by(Category.name).all()
    items = Item.query.all()

    data = []
    total_received = total_issued = total_balance = 0
    for it in items:
        rec_logs = StockLog.query.filter_by(item_id=it.id, type="receive").all()
        iss_logs = StockLog.query.filter_by(item_id=it.id, type="issue").all()
        rec_sum = sum([r.quantity for r in rec_logs])
        iss_sum = sum([i.quantity for i in iss_logs])
        bal = rec_sum - iss_sum
        total_received += rec_sum
        total_issued += iss_sum
        total_balance += bal

        next_expiry = min([r.expiry_date for r in rec_logs if r.expiry_date], default=None)
        last_receive = max([r.created_at for r in rec_logs], default=None)
        last_issue = max([i.created_at for i in iss_logs], default=None)

        data.append({
            "item_name": it.name,
            "category_name": it.category.name if it.category else None,
            "balance": bal,
            "total_received": rec_sum,
            "total_issued": iss_sum,
            "next_expiry": next_expiry,
            "last_receive": last_receive,
            "last_issue": last_issue
        })

    return render_template(
        "reports.html",
        categories=cats,
        report_data=data,
        total_items=len(items),
        total_received=total_received,
        total_issued=total_issued,
        total_balance=total_balance,
        title="รายงานสรุปสต็อก"
    )

# --------------------------------------------------
# UTIL: INIT DB (ใช้ครั้งเดียว แล้วลบทิ้งได้)
# --------------------------------------------------
@app.route("/initdb")
def initdb():
    try:
        db.create_all()
        return "✅ Database initialized successfully!"
    except Exception as e:
        return f"❌ Error: {str(e)}"

# --------------------------------------------------
# MAIN
# --------------------------------------------------
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(host="0.0.0.0", port=5000, debug=True)
