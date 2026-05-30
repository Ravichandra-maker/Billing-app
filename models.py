from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import json

db = SQLAlchemy()

class Bill(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    bill_number = db.Column(db.String(20), unique=True)
    date = db.Column(db.String(20))
    day_name = db.Column(db.String(20))
    city = db.Column(db.String(50))
    customer_name = db.Column(db.String(100))
    phone = db.Column(db.String(15))
    address = db.Column(db.String(200), default='')
    gst_number = db.Column(db.String(30))
    item_type = db.Column(db.String(20))  # Gold/Silver/Diamond/Platinum/Readymade
    item_name = db.Column(db.String(100), default='')
    purity = db.Column(db.String(20), default='')  # 24K, 22K, 18K, 925, etc.
    gross_weight = db.Column(db.Float, default=0)
    stone_weight = db.Column(db.Float, default=0)
    net_weight = db.Column(db.Float, default=0)
    grams = db.Column(db.Float)
    making_charge = db.Column(db.Float)
    making_charge_type = db.Column(db.String(20), default='per_gram')  # per_gram, fixed, percentage
    wastage_percent = db.Column(db.Float)
    item_rate = db.Column(db.Float)
    stone_charge = db.Column(db.Float, default=0)
    hallmark_charge = db.Column(db.Float, default=0)
    discount = db.Column(db.Float, default=0)
    cgst = db.Column(db.Float, default=0)
    sgst = db.Column(db.Float, default=0)
    subtotal = db.Column(db.Float, default=0)
    total = db.Column(db.Float)
    payment_mode = db.Column(db.String(20), default='Cash')  # Cash/Card/UPI/Credit
    old_item_type = db.Column(db.String(20), default='')  # Gold/Silver etc.
    old_item_name = db.Column(db.String(100), default='')
    old_item_purity = db.Column(db.String(20), default='')
    old_item_grams = db.Column(db.Float, default=0)
    old_item_rate = db.Column(db.Float, default=0)
    old_item_value = db.Column(db.Float, default=0)  # grams * rate
    amount_paid = db.Column(db.Float, default=0)
    balance = db.Column(db.Float, default=0)
    status = db.Column(db.String(20), default='Paid')  # Paid/Pending/Partial
    notes = db.Column(db.Text, default='')
    inventory_item_id = db.Column(db.Integer, db.ForeignKey('inventory_item.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    items = db.relationship('BillItem', backref='bill', lazy=True, cascade="all, delete-orphan")


class BillItem(db.Model):
    __tablename__ = 'bill_item'
    id = db.Column(db.Integer, primary_key=True)
    bill_id = db.Column(db.Integer, db.ForeignKey('bill.id'), nullable=False)
    item_type = db.Column(db.String(20))
    item_name = db.Column(db.String(100), default='')
    purity = db.Column(db.String(20), default='')
    gross_weight = db.Column(db.Float, default=0)
    stone_weight = db.Column(db.Float, default=0)
    net_weight = db.Column(db.Float, default=0)
    grams = db.Column(db.Float, default=0)
    making_charge = db.Column(db.Float, default=0)
    making_charge_type = db.Column(db.String(20), default='per_gram')
    wastage_percent = db.Column(db.Float, default=0)
    item_rate = db.Column(db.Float, default=0)
    stone_charge = db.Column(db.Float, default=0)
    hallmark_charge = db.Column(db.Float, default=0)
    item_amount = db.Column(db.Float, default=0) # Amount for this specific item
    inventory_item_id = db.Column(db.Integer, db.ForeignKey('inventory_item.id'), nullable=True)


class InventoryItem(db.Model):
    __tablename__ = 'inventory_item'
    id = db.Column(db.Integer, primary_key=True)
    item_code = db.Column(db.String(30), unique=True, nullable=False)
    item_name = db.Column(db.String(100), nullable=False)
    item_type = db.Column(db.String(20), nullable=False)  # Gold/Silver/Diamond/Platinum/Readymade
    purity = db.Column(db.String(20), default='')         # 22K, 18K, 925, etc.
    description = db.Column(db.Text, default='')
    gross_weight = db.Column(db.Float, default=0.0)
    stone_weight = db.Column(db.Float, default=0.0)
    net_weight = db.Column(db.Float, default=0.0)
    making_charge = db.Column(db.Float, default=0.0)
    making_charge_type = db.Column(db.String(20), default='per_gram')  # per_gram, fixed, percentage
    wastage_percent = db.Column(db.Float, default=0.0)
    stone_charge = db.Column(db.Float, default=0.0)
    hallmark_charge = db.Column(db.Float, default=0.0)
    total_weight = db.Column(db.Float, default=0.0)       # Total available weight in grams
    quantity = db.Column(db.Integer, default=1)           # Stock count
    low_stock_alert = db.Column(db.Integer, default=2)   # Alert when <= this value
    image_url = db.Column(db.String(200), default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
