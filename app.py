import os
from datetime import datetime, date
from typing import Optional, List

from flask import Flask, render_template, request, redirect, url_for, flash, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from passlib.hash import pbkdf2_sha256
from dotenv import load_dotenv

import pandas as pd

# -------------------- App & Config --------------------
load_dotenv()
app = Flask(__name__)

app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-change-me')
DATABASE_URL = os.getenv('DATABASE_URL')

# ✅ รองรับ legacy scheme postgres:// และบังคับ SSL
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)

if DATABASE_URL and "sslmode=" not in DATABASE_URL:
    sep = "&" if "?" in DATABASE_URL else "?"
    DATABASE_URL = f"{DATABASE_URL}{sep}sslmode=require"

if DATABASE_URL:
    app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///data.db'

# optional: reduce stale connections
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {"pool_pre_ping": True}

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"

# -------------------- Models --------------------
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)

    def set_password(self, pw: str):
        self.password_hash = pbkdf2_sha256.hash(pw)

    def verify_password(self, pw: str) -> bool:
        return pbkdf2_sha256.verify(pw, self.password_hash)

class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    items = db.relationship('Item', backref='category')

class Item(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), unique=True, nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'))

    lots = db.relationship('StockLot', backref='item', cascade="all, delete-orphan")
    transactions = db.relationship('Transaction', backref='item', cascade="all, delete-orphan")

    @property
    def total_qty(self):
        return sum(l.available_qty for l in self.lots)

class StockLot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False)
    quantity = db.Column(db.Float, nullable=False)
    available_qty = db.Column(db.Float, nullable=False, default=0)
    unit = db.Column(db.String(50), nullable=False)
    expiry = db.Column(db.Date, nullable=True)
    received_at = db.Column(db.DateTime, default=datetime.utcnow)

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(3), nullable=False)  # IN or OUT
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False)
    lot_id = db.Column(db.Integer, db.ForeignKey('stock_lot.id'), nullable=True)
    quantity = db.Column(db.Float, nullable=False)
    note = db.Column(db.String(255))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User')
    lot = db.relationship('StockLot')

# -------------------- Auth --------------------
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.route('/login', methods=['GET', 'POST'])
def login():
    allow_register = (User.query.count() == 0)
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        user = User.query.filter_by(username=username).first()
        if user and user.verify_password(password):
            login_user(user)
            flash('เข้าสู่ระบบสำเร็จ', 'success')
            return redirect(url_for('dashboard'))
        flash('ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง', 'danger')
    return render_template('login.html', allow_register=allow_register)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if User.query.count() > 0:
        flash('มีผู้ใช้งานอยู่แล้ว', 'warning')
        return redirect(url_for('login'))
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        if not username or not password:
            flash('กรุณากรอกข้อมูลให้ครบ', 'warning')
            return redirect(url_for('register'))
        u = User(username=username, is_admin=True)
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        flash('สร้างผู้ใช้แอดมินสำเร็จ ล็อกอินได้เลย', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('ออกจากระบบแล้ว', 'info')
    return redirect(url_for('login'))

# -------------------- Pages --------------------
@app.route('/')
@login_required
def dashboard():
    counts = {
        'items': Item.query.count(),
        'categories': Category.query.count(),
        'qty': sum(i.total_qty for i in Item.query.all())
    }
    txs = Transaction.query.order_by(Transaction.created_at.desc()).limit(10).all()
    return render_template('dashboard.html', counts=counts, txs=txs)

# ---- Items
@app.route('/items')
@login_required
def items():
    items = Item.query.order_by(Item.id.desc()).all()
    categories = Category.query.order_by(Category.name.asc()).all()
    return render_template('items.html', items=items, categories=categories)

@app.post('/items/add')
@login_required
def items_add():
    name = request.form['name'].strip()
    category_id = request.form.get('category_id')
    if not category_id:
        flash('ต้องเลือกหมวดหมู่ก่อนบันทึก', 'warning')
        return redirect(url_for('items'))
    if Item.query.filter_by(name=name).first():
        flash('มีชื่อสินค้านี้แล้ว', 'warning')
        return redirect(url_for('items'))
    it = Item(name=name, category_id=int(category_id))
    db.session.add(it)
    db.session.commit()
    flash('เพิ่มสินค้าแล้ว', 'success')
    return redirect(url_for('items'))

@app.post('/items/<int:item_id>/edit')
@login_required
def items_edit(item_id):
    it = Item.query.get_or_404(item_id)
    it.name = request.form['name'].strip()
    db.session.commit()
    flash('แก้ไขชื่อสินค้าแล้ว', 'success')
    return redirect(url_for('items'))

@app.route('/items/<int:item_id>/delete')
@login_required
def items_delete(item_id):
    it = Item.query.get_or_404(item_id)
    db.session.delete(it)
    db.session.commit()
    flash('ลบสินค้าและสต็อคของรายการนี้แล้ว', 'success')
    return redirect(url_for('items'))

# ---- Categories
@app.route('/categories')
@login_required
def categories():
    categories = Category.query.order_by(Category.id.desc()).all()
    return render_template('categories.html', categories=categories)

@app.post('/categories/add')
@login_required
def categories_add():
    name = request.form['name'].strip()
    if Category.query.filter_by(name=name).first():
        flash('มีหมวดหมู่นี้แล้ว', 'warning')
        return redirect(url_for('categories'))
    c = Category(name=name)
    db.session.add(c)
    db.session.commit()
    flash('เพิ่มหมวดหมู่แล้ว', 'success')
    return redirect(url_for('categories'))

@app.post('/categories/<int:category_id>/edit')
@login_required
def categories_edit(category_id):
    c = Category.query.get_or_404(category_id)
    c.name = request.form['name'].strip()
    db.session.commit()
    flash('แก้ไขหมวดหมู่แล้ว', 'success')
    return redirect(url_for('categories'))

@app.route('/categories/<int:category_id>/delete')
@login_required
def categories_delete(category_id):
    c = Category.query.get_or_404(category_id)
    # keep items but unset category
    for it in c.items:
        it.category_id = None
    db.session.delete(c)
    db.session.commit()
    flash('ลบหมวดหมู่แล้ว (สินค้ายังคงอยู่)', 'success')
    return redirect(url_for('categories'))

# ---- Receive
@app.route('/receive', methods=['GET', 'POST'])
@login_required
def receive():
    if request.method == 'POST':
        item_id = int(request.form['item_id'])
        qty = float(request.form['quantity'])
        unit = request.form['unit'].strip()
        expiry_val = request.form.get('expiry')
        note = request.form.get('note', '').strip()

        exp_date = None
        if expiry_val:
            exp_date = datetime.strptime(expiry_val, '%Y-%m-%d').date()

        lot = StockLot(item_id=item_id, quantity=qty, available_qty=qty, unit=unit, expiry=exp_date)
        db.session.add(lot)
        tx = Transaction(type='IN', item_id=item_id, lot=lot, quantity=qty, note=note, user_id=current_user.id)
        db.session.add(tx)
        db.session.commit()
        flash('รับเข้าสินค้าเรียบร้อย', 'success')
        return redirect(url_for('receive'))

    items = Item.query.order_by(Item.name.asc()).all()
    return render_template('receive.html', items=items)

# ---- Issue (FIFO)
@app.route('/issue', methods=['GET', 'POST'])
@login_required
def issue():
    if request.method == 'POST':
        item_id = int(request.form['item_id'])
        qty = float(request.form['quantity'])
        note = request.form.get('note', '').strip()

        item = Item.query.get_or_404(item_id)
        remaining = qty
        # FIFO: prioritize soonest expiry, then earliest received
        lots = StockLot.query.filter_by(item_id=item_id).order_by(StockLot.expiry.is_(None), StockLot.expiry.asc(), StockLot.received_at.asc()).all()
        deducted = 0.0
        for lot in lots:
            if remaining <= 0:
                break
            take = min(lot.available_qty, remaining)
            if take > 0:
                lot.available_qty -= take
                deducted += take
                remaining -= take
                tx = Transaction(type='OUT', item_id=item_id, lot=lot, quantity=take, note=note, user_id=current_user.id)
                db.session.add(tx)
        if remaining > 1e-9:
            db.session.rollback()
            flash('สต็อคไม่พอ', 'danger')
            return redirect(url_for('issue'))
        db.session.commit()
        flash(f'เบิกสินค้าแล้ว {deducted}', 'success')
        return redirect(url_for('issue'))

    items = Item.query.order_by(Item.name.asc()).all()
    return render_template('issue.html', items=items)

# ---- Reports & Export
@app.route('/reports')
@login_required
def reports():
    txs = Transaction.query.order_by(Transaction.created_at.desc()).limit(50).all()
    return render_template('reports.html', txs=txs)

@app.route('/export/<kind>.csv')
@login_required
def export_csv(kind):
    if kind == 'items':
        rows = [{'id': i.id, 'name': i.name, 'category': (i.category.name if i.category else None), 'total_qty': i.total_qty} for i in Item.query.all()]
    elif kind == 'lots':
        rows = [{
            'id': l.id, 'item': l.item.name, 'quantity': l.quantity, 'available_qty': l.available_qty,
            'unit': l.unit, 'expiry': l.expiry, 'received_at': l.received_at
        } for l in StockLot.query.all()]
    elif kind == 'transactions':
        rows = [{
            'id': t.id, 'type': t.type, 'item': t.item.name, 'lot_id': t.lot_id, 'quantity': t.quantity,
            'note': t.note, 'user': (t.user.username if t.user else None), 'created_at': t.created_at
        } for t in Transaction.query.order_by(Transaction.created_at.desc()).all()]
    else:
        rows = []

    df = pd.DataFrame(rows)
    buf = df.to_csv(index=False).encode('utf-8-sig')
    return send_file(
        io.BytesIO(buf),
        mimetype='text/csv; charset=utf-8',
        as_attachment=True,
        download_name=f'{kind}.csv'
    )

# ---- Admin danger zone
@app.route('/admin/delete_all')
@login_required
def admin_delete_all():
    if not current_user.is_admin:
        flash('เฉพาะแอดมินเท่านั้น', 'danger')
        return redirect(url_for('reports'))
    # delete all items (cascades lots & transactions)
    for it in Item.query.all():
        db.session.delete(it)
    db.session.commit()
    flash('ลบสินค้าทั้งหมดแล้ว', 'success')
    return redirect(url_for('reports'))

# -------------------- Init DB --------------------
@app.before_request
def ensure_db():
    db.create_all()

# -------------------- CLI run --------------------
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5050, debug=os.getenv('FLASK_ENV') != 'production')
