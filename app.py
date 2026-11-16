from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, send_file
from models import db, Bill, InventoryItem, BillItem
from datetime import datetime, date, timedelta
import calendar
import os
import io
import urllib.parse
import webbrowser
import threading
import requests
from dotenv import load_dotenv
from reportlab.lib.pagesizes import landscape, A5, A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable, Image, PageBreak, KeepTogether
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
from reportlab.pdfgen import canvas
import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.utils import get_column_letter

# Load environment variables
load_dotenv(override=True)

import platform
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

font_registered = False
if platform.system() == 'Windows':
    arial_path = 'C:/Windows/Fonts/arial.ttf'
    arial_bold_path = 'C:/Windows/Fonts/arialbd.ttf'
    arial_italic_path = 'C:/Windows/Fonts/ariali.ttf'
    arial_bi_path = 'C:/Windows/Fonts/arialbi.ttf'
    if os.path.exists(arial_path):
        try:
            pdfmetrics.registerFont(TTFont('Arial', arial_path))
            if os.path.exists(arial_bold_path):
                pdfmetrics.registerFont(TTFont('Arial-Bold', arial_bold_path))
            if os.path.exists(arial_italic_path):
                pdfmetrics.registerFont(TTFont('Arial-Italic', arial_italic_path))
            if os.path.exists(arial_bi_path):
                pdfmetrics.registerFont(TTFont('Arial-BoldItalic', arial_bi_path))
            font_registered = True
        except Exception as e:
            print("Failed to register Arial font:", e)

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'lakshmi_srinivasa_jewellery_2026')

@app.errorhandler(500)
def internal_server_error(e):
    import traceback
    error_details = traceback.format_exc()
    print("500 INTERNAL SERVER ERROR:", error_details)
    return f"""
    <h1>Internal Server Error (500)</h1>
    <p>An error occurred in your application:</p>
    <pre style="background:#f4f4f4;padding:12px;border:1px solid #ccc;border-radius:4px;overflow-x:auto;">{error_details if 'Traceback' in error_details else str(e)}</pre>
    """, 500

# Database configuration

database_url = os.getenv('DATABASE_URL')

if database_url:
    # Ensure SQLAlchemy uses psycopg2 driver or handles postgres URLs cleanly
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql+psycopg2://", 1)
    elif database_url.startswith("postgresql://") and not database_url.startswith("postgresql+"):
        database_url = database_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
else:
    if os.getenv('VERCEL'):
        # Vercel serverless functions have a read-only filesystem except for /tmp
        db_path = '/tmp/billing.db'
    else:
        basedir = os.path.abspath(os.path.dirname(__file__))
        DATA_DIR = os.path.join(basedir, "databases")
        os.makedirs(DATA_DIR, exist_ok=True)
        db_path = os.path.join(DATA_DIR, "billing.db")
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

with app.app_context():
    try:
        db.create_all()
    except Exception as e:
        print(f"DATABASE INITIALIZATION ERROR: {str(e)}")
        
    # Check if total_weight column exists in inventory_item table, if not add it
    try:
        from sqlalchemy import text
        db.session.execute(text("ALTER TABLE inventory_item ADD COLUMN total_weight FLOAT DEFAULT 0.0"))
        db.session.commit()
    except Exception as e:
        db.session.rollback()


# ===== PURITY MULTIPLIERS FOR CALCULATIONS =====
PURITY_MULTIPLIERS = {
    '24K': 1.0, '22K': 0.916, '18K': 0.750, '14K': 0.585,
    '999': 1.0, '925': 0.925, '900': 0.90,
    '950': 0.95
}

def get_purity_multiplier(purity_str):
    if not purity_str:
        return 1.0
        
    # Normalize purity string
    clean = str(purity_str).strip().upper().replace(' ', '').replace('KT', 'K').rstrip('%')
    
    # Check predefined multipliers first
    if clean in PURITY_MULTIPLIERS:
        return PURITY_MULTIPLIERS[clean]
        
    # Fallback parsing logic
    if clean.endswith('K'):
        try:
            num = float(clean[:-1])
            if 0 < num <= 24:
                return round(num / 24.0, 6)
        except ValueError:
            pass
            
    try:
        num = float(clean)
        if num > 0:
            if num <= 1.0:
                return num
            elif num <= 24:
                return round(num / 24.0, 6)
            elif num <= 100:
                return round(num / 100.0, 6)
            elif num <= 1000:
                return round(num / 1000.0, 6)
    except ValueError:
        pass
        
    return 1.0

# ===== LIVE METAL PRICES =====
GOLD_API_KEY = os.getenv('GOLD_API_KEY', 'goldapi-40e507f3c996c0a795b777471aba53b8-io')
if GOLD_API_KEY and not GOLD_API_KEY.startswith('goldapi-'):
    GOLD_API_KEY = f"goldapi-{GOLD_API_KEY}"

# Cache for metal prices (avoid hitting API on every request)
_price_cache = {
    'prices': None,
    'last_fetched': None,
    'cache_duration': 300  # 5 minutes cache
}

def fetch_live_metal_prices():
    """Fetch live gold, silver, and platinum prices from Yahoo Finance or GoldAPI"""
    import time
    now = time.time()

    # Return cached prices if still valid
    if (_price_cache['prices'] and _price_cache['last_fetched'] and
            now - _price_cache['last_fetched'] < _price_cache['cache_duration']):
        return _price_cache['prices']

    prices = {}
    retail_multiplier = 1.15

    # 1. Attempt to fetch from Yahoo Finance (Free, keyless, reliable)
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        
        # Get USD/INR rate
        r_inr = requests.get('https://query1.finance.yahoo.com/v8/finance/chart/INR=X', headers=headers, timeout=5)
        if r_inr.status_code == 200:
            inr_meta = r_inr.json()['chart']['result'][0]['meta']
            usd_inr = inr_meta['regularMarketPrice']
            usd_inr_prev = inr_meta['previousClose']
            
            tickers = {
                'gold': 'GC=F',
                'silver': 'SI=F',
                'platinum': 'PL=F'
            }
            
            for metal, ticker in tickers.items():
                r_metal = requests.get(f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}', headers=headers, timeout=5)
                if r_metal.status_code != 200:
                    raise Exception(f"Yahoo Finance {metal} failed")
                
                meta = r_metal.json()['chart']['result'][0]['meta']
                price_usd = meta['regularMarketPrice']
                prev_usd = meta['previousClose']
                
                # Convert to INR (per troy ounce)
                price_inr_oz = price_usd * usd_inr
                prev_inr_oz = prev_usd * usd_inr_prev
                
                # Convert to INR per gram (1 troy ounce = 31.1034768 grams) and apply retail multiplier
                price_gram = round((price_inr_oz / 31.1034768) * retail_multiplier, 2)
                prev_gram = round((prev_inr_oz / 31.1034768) * retail_multiplier, 2)
                
                change = round(price_gram - prev_gram, 2)
                change_pct = round((change / prev_gram) * 100, 2) if prev_gram else 0
                
                prices[metal] = {
                    'price_per_gram': price_gram,
                    'price_per_ounce': round(price_inr_oz * retail_multiplier, 2),
                    'change': change,
                    'change_pct': change_pct,
                    'direction': 'up' if change >= 0 else 'down',
                    'timestamp': '',
                    'currency': 'INR'
                }
            print("Successfully fetched live metal prices from Yahoo Finance.")
            # Update cache and return
            _price_cache['prices'] = prices
            _price_cache['last_fetched'] = now
            return prices
        else:
            raise Exception("Failed to get USD/INR rate from Yahoo Finance")
            
    except Exception as ey:
        print(f"Yahoo Finance fetch failed: {ey}. Trying GoldAPI.io fallback...")

    # 2. Attempt to fetch from GoldAPI.io as a secondary fallback
    try:
        headers = {
            "x-access-token": GOLD_API_KEY,
            "Content-Type": "application/json"
        }
        
        # Gold
        gold_req = requests.get("https://www.goldapi.io/api/XAU/INR", headers=headers, timeout=5)
        if gold_req.status_code != 200: raise Exception("Gold API failed")
        gold_data = gold_req.json()
        
        prices['gold'] = {
            'price_per_gram': round(gold_data.get('price_gram_24k', 0) * retail_multiplier, 2),
            'price_per_ounce': round(gold_data.get('price', 0) * retail_multiplier, 2),
            'change': round(gold_data.get('ch', 0), 2),
            'change_pct': round(gold_data.get('chp', 0), 2),
            'direction': 'up' if gold_data.get('ch', 0) >= 0 else 'down',
            'timestamp': '', 'currency': 'INR'
        }
        
        # Silver
        silver_req = requests.get("https://www.goldapi.io/api/XAG/INR", headers=headers, timeout=5)
        if silver_req.status_code != 200: raise Exception("Silver API failed")
        silver_data = silver_req.json()
        
        prices['silver'] = {
            'price_per_gram': round(silver_data.get('price_gram_24k', 0) * retail_multiplier, 2),
            'price_per_ounce': round(silver_data.get('price', 0) * retail_multiplier, 2),
            'change': round(silver_data.get('ch', 0), 2),
            'change_pct': round(silver_data.get('chp', 0), 2),
            'direction': 'up' if silver_data.get('ch', 0) >= 0 else 'down',
            'timestamp': '', 'currency': 'INR'
        }
        
        # Platinum
        plat_req = requests.get("https://www.goldapi.io/api/XPT/INR", headers=headers, timeout=5)
        if plat_req.status_code != 200: raise Exception("Platinum API failed")
        plat_data = plat_req.json()
        
        prices['platinum'] = {
            'price_per_gram': round(plat_data.get('price_gram_24k', 0) * retail_multiplier, 2),
            'price_per_ounce': round(plat_data.get('price', 0) * retail_multiplier, 2),
            'change': round(plat_data.get('ch', 0), 2),
            'change_pct': round(plat_data.get('chp', 0), 2),
            'direction': 'up' if plat_data.get('ch', 0) >= 0 else 'down',
            'timestamp': '', 'currency': 'INR'
        }
        print("Successfully fetched live metal prices from GoldAPI.io fallback.")
        _price_cache['prices'] = prices
        _price_cache['last_fetched'] = now
        return prices
    except Exception as eg:
        print(f"GoldAPI.io fetch failed: {eg}. Falling back to updated 2026 hardcoded rates...")
        prices = get_fallback_prices()

    # Update cache
    _price_cache['prices'] = prices
    _price_cache['last_fetched'] = now

    return prices


def get_fallback_price(metal_name):
    """Fallback prices when API is unavailable (Updated to 2026 average market rates)"""
    fallback = {
        'gold': {'price_per_gram': 15600.00, 'price_per_ounce': 485000.00, 'change': 0, 'change_pct': 0, 'direction': 'up', 'timestamp': '', 'currency': 'INR'},
        'silver': {'price_per_gram': 290.00, 'price_per_ounce': 9000.00, 'change': 0, 'change_pct': 0, 'direction': 'up', 'timestamp': '', 'currency': 'INR'},
        'platinum': {'price_per_gram': 6500.00, 'price_per_ounce': 202000.00, 'change': 0, 'change_pct': 0, 'direction': 'up', 'timestamp': '', 'currency': 'INR'},
    }
    return fallback.get(metal_name, fallback['gold'])


def get_fallback_prices():
    """Return all fallback prices"""
    return {
        'gold': get_fallback_price('gold'),
        'silver': get_fallback_price('silver'),
        'platinum': get_fallback_price('platinum'),
    }


def generate_bill_number():
    """Generate unique bill number like LSJ-2026-00001"""
    now = datetime.now()
    year = now.strftime('%Y')
    with app.app_context():
        last_bill = Bill.query.order_by(Bill.id.desc()).first()
        if last_bill and last_bill.bill_number:
            try:
                last_num = int(last_bill.bill_number.split('-')[-1])
                new_num = last_num + 1
            except (ValueError, IndexError):
                new_num = 1
        else:
            new_num = 1
    return f"LSJ-{year}-{new_num:05d}"


# ===== ROUTES =====

@app.route('/')
def dashboard():
    """Dashboard with statistics and live metal prices"""
    bills = Bill.query.order_by(Bill.id.desc()).all()

    # Calculate statistics
    total_revenue = sum(b.total for b in bills) if bills else 0
    total_bills = len(bills)
    today = datetime.now().strftime('%d-%m-%Y')
    today_bills = [b for b in bills if b.date == today]
    today_revenue = sum(b.total for b in today_bills) if today_bills else 0
    pending_bills = [b for b in bills if b.status == 'Pending' or b.status == 'Partial']
    pending_amount = sum(b.balance for b in pending_bills)

    # Category stats
    gold_bills = len([b for b in bills if b.item_type == 'Gold'])
    silver_bills = len([b for b in bills if b.item_type == 'Silver'])
    diamond_bills = len([b for b in bills if b.item_type == 'Diamond'])
    other_bills = total_bills - gold_bills - silver_bills - diamond_bills

    # Inventory stats
    inventory_items = InventoryItem.query.all()
    total_inv_count = len(inventory_items)
    low_stock_count = len([i for i in inventory_items if i.quantity <= i.low_stock_alert])
    out_of_stock_count = len([i for i in inventory_items if i.quantity <= 0])

    # Live metal prices
    metal_prices = fetch_live_metal_prices()

    return render_template("dashboard.html",
        bills=bills,
        total_revenue=total_revenue,
        total_bills=total_bills,
        today_revenue=today_revenue,
        today_bills_count=len(today_bills),
        pending_amount=pending_amount,
        pending_count=len(pending_bills),
        gold_bills=gold_bills,
        silver_bills=silver_bills,
        diamond_bills=diamond_bills,
        other_bills=other_bills,
        total_inv_count=total_inv_count,
        low_stock_count=low_stock_count,
        out_of_stock_count=out_of_stock_count,
        metal_prices=metal_prices
    )


@app.route('/new-bill')
def new_bill():
    """New bill creation form with live metal prices"""
    bill_number = generate_bill_number()
    now = datetime.now()
    metal_prices = fetch_live_metal_prices()
    return render_template("new_bill.html",
        bill_number=bill_number,
        current_date=now.strftime('%d-%m-%Y'),
        current_day=calendar.day_name[now.weekday()],
        metal_prices=metal_prices
    )


@app.route('/create-bill', methods=['POST'])
def create_bill():
    """Process bill creation"""
    try:
        form = request.form
        print("DEBUG FORM:", form)
        
        item_types = form.getlist('item_type[]')
        if not item_types:
            # Fallback to single item if the form wasn't updated
            item_types = [form.get('item_type', 'Gold')]
            purities = [form.get('purity', '')]
            item_names = [form.get('item_name', '')]
            inventory_item_ids = [form.get('inventory_item_id', '')]
            gross_weights = [float(form.get('gross_weight', 0) or 0)]
            stone_weights = [float(form.get('stone_weight', 0) or 0)]
            making_charges = [float(form.get('making_charge', 0) or 0)]
            making_charge_types = [form.get('making_charge_type', 'per_gram')]
            wastage_percents = [float(form.get('wastage_percent', 0) or 0)]
            item_rates = [float(form.get('item_rate', 0) or 0)]
            stone_charges = [float(form.get('stone_charge', 0) or 0)]
            hallmark_charges = [float(form.get('hallmark_charge', 0) or 0)]
            grams_arr = [float(form.get('grams', 0) or 0)]
        else:
            purities = form.getlist('purity[]')
            item_names = form.getlist('item_name[]')
            inventory_item_ids = form.getlist('inventory_item_id[]')
            gross_weights = [float(x or 0) for x in form.getlist('gross_weight[]')]
            stone_weights = [float(x or 0) for x in form.getlist('stone_weight[]')]
            making_charges = [float(x or 0) for x in form.getlist('making_charge[]')]
            making_charge_types = form.getlist('making_charge_type[]')
            wastage_percents = [float(x or 0) for x in form.getlist('wastage_percent[]')]
            item_rates = [float(x or 0) for x in form.getlist('item_rate[]')]
            stone_charges = [float(x or 0) for x in form.getlist('stone_charge[]')]
            hallmark_charges = [float(x or 0) for x in form.getlist('hallmark_charge[]')]
            grams_arr = [float(x or 0) for x in form.getlist('grams[]')]
            
        discount = float(form.get('discount', 0) or 0)
        old_item_purity = form.get('old_item_purity', '')
        old_item_grams = float(form.get('old_item_grams', 0) or 0)
        old_item_rate = float(form.get('old_item_rate', 0) or 0)
        old_mult = get_purity_multiplier(old_item_purity)
        old_item_value = round(old_item_grams * old_item_rate * old_mult, 2)
        amount_paid = float(form.get('amount_paid', 0) or 0)

        total_subtotal = 0
        total_making = 0
        total_wastage = 0
        total_stone = 0
        total_hallmark = 0
        
        bill_items = []

        for i in range(len(item_types)):
            gw = gross_weights[i] if i < len(gross_weights) else 0
            sw = stone_weights[i] if i < len(stone_weights) else 0
            g = grams_arr[i] if i < len(grams_arr) else 0
            rate = item_rates[i] if i < len(item_rates) else 0
            making = making_charges[i] if i < len(making_charges) else 0
            making_type = making_charge_types[i] if i < len(making_charge_types) else 'per_gram'
            wastage = wastage_percents[i] if i < len(wastage_percents) else 0
            stone_charge = stone_charges[i] if i < len(stone_charges) else 0
            hallmark_charge = hallmark_charges[i] if i < len(hallmark_charges) else 0
            
            nw = gw - sw if gw > 0 else g
            
            if nw <= 0 and gw <= 0 and g <= 0:
                continue
                
            if making_type == 'per_gram':
                making_amt = nw * making
            elif making_type == 'percentage':
                making_amt = (making / 100) * rate * nw
            else:
                making_amt = making
                
            wastage_amt = (wastage / 100) * rate * nw
            metal_value = nw * rate
            item_subtotal = metal_value + making_amt + wastage_amt + stone_charge + hallmark_charge
            
            total_making += making_amt
            total_wastage += wastage_amt
            total_stone += stone_charge
            total_hallmark += hallmark_charge
            total_subtotal += item_subtotal
            
            item_name_str = item_names[i] if i < len(item_names) else ''
            
            # Inventory lookup and decrement
            inv_id_str = inventory_item_ids[i] if i < len(inventory_item_ids) else ''
            inv_item = None
            if inv_id_str and inv_id_str.strip():
                try:
                    inv_id = int(inv_id_str)
                    inv_item = InventoryItem.query.get(inv_id)
                except ValueError:
                    pass
                
            if inv_item:
                if inv_item.quantity > 0:
                    inv_item.quantity -= 1
                    # Deduct billed weight from inventory total_weight
                    if inv_item.total_weight and inv_item.total_weight > 0:
                        inv_item.total_weight = round(max(inv_item.total_weight - nw, 0), 3)
                    db.session.add(inv_item)
                else:
                    flash(f'Warning: Item {inv_item.item_code} is out of stock.', 'warning')
            
            b_item = BillItem(
                item_type=item_types[i],
                item_name=item_name_str,
                purity=purities[i] if i < len(purities) else '',
                gross_weight=gw,
                stone_weight=sw,
                net_weight=nw,
                grams=g if g > 0 else nw,
                making_charge=making,
                making_charge_type=making_type,
                wastage_percent=wastage,
                item_rate=rate,
                stone_charge=stone_charge,
                hallmark_charge=hallmark_charge,
                item_amount=item_subtotal,
                inventory_item_id=inv_item.id if inv_item else None
            )
            bill_items.append(b_item)

        cgst = round(total_subtotal * 0.015, 2)
        sgst = round(total_subtotal * 0.015, 2)
        
        total_before_discount = total_subtotal + cgst + sgst
        total = total_before_discount - discount - old_item_value
        total = round(max(total, 0), 2)
        
        balance = round(total - amount_paid, 2)
        if balance <= 0:
            status = 'Paid'
            balance = 0
        elif amount_paid > 0:
            status = 'Partial'
        else:
            status = 'Pending'
            
        if amount_paid == 0 and balance == total:
            amount_paid = total
            balance = 0
            status = 'Paid'
            
        now = datetime.now()
        
        bill = Bill(
            bill_number=generate_bill_number(),
            date=now.strftime('%d-%m-%Y'),
            day_name=calendar.day_name[now.weekday()],
            city=form.get('city', ''),
            customer_name=form.get('customer_name', ''),
            phone=form.get('phone', ''),
            address=form.get('address', ''),
            gst_number=form.get('gst_number', ''),
            
            item_type=item_types[0] if item_types else 'Gold',
            item_name=item_names[0] if item_names else '',
            purity=purities[0] if purities else '',
            gross_weight=gross_weights[0] if gross_weights else 0,
            stone_weight=stone_weights[0] if stone_weights else 0,
            net_weight=bill_items[0].net_weight if bill_items else 0,
            grams=bill_items[0].grams if bill_items else 0,
            making_charge=making_charges[0] if making_charges else 0,
            making_charge_type=making_charge_types[0] if making_charge_types else 'per_gram',
            wastage_percent=wastage_percents[0] if wastage_percents else 0,
            item_rate=item_rates[0] if item_rates else 0,
            stone_charge=total_stone,
            hallmark_charge=total_hallmark,
            
            discount=discount,
            cgst=cgst,
            sgst=sgst,
            subtotal=round(total_subtotal, 2),
            total=total,
            payment_mode=form.get('payment_mode', 'Cash'),
            old_item_type=form.get('old_item_type', ''),
            old_item_name=form.get('old_item_name', ''),
            old_item_purity=form.get('old_item_purity', ''),
            old_item_grams=old_item_grams,
            old_item_rate=old_item_rate,
            old_item_value=old_item_value,
            amount_paid=amount_paid,
            balance=balance,
            status=status,
            inventory_item_id=bill_items[0].inventory_item_id if bill_items else None,
            notes=form.get('notes', '')
        )
        
        for bi in bill_items:
            bill.items.append(bi)
            
        db.session.add(bill)
        db.session.commit()
        flash('Bill created successfully!', 'success')
        return redirect(url_for('view_bill', bill_id=bill.id))
    except Exception as e:
        db.session.rollback()
        flash(f'Error creating bill: {str(e)}', 'error')
        return redirect(url_for('new_bill'))


@app.route('/bill/<int:bill_id>')
def view_bill(bill_id):
    """View individual bill / invoice"""
    bill = Bill.query.get_or_404(bill_id)
    return render_template("view_bill.html", bill=bill)


@app.route('/edit-bill/<int:bill_id>')
def edit_bill(bill_id):
    """Load edit form pre-filled with existing bill data"""
    bill = Bill.query.get_or_404(bill_id)
    metal_prices = fetch_live_metal_prices()
    return render_template("edit_bill.html", bill=bill, metal_prices=metal_prices)


@app.route('/save-edit-bill/<int:bill_id>', methods=['POST'])
def save_edit_bill(bill_id):
    """Save edits to an existing bill"""
    try:
        bill = Bill.query.get_or_404(bill_id)
        form = request.form

        item_types = form.getlist('item_type[]')
        purities = form.getlist('purity[]')
        item_names = form.getlist('item_name[]')
        inventory_item_ids = form.getlist('inventory_item_id[]')
        gross_weights = [float(x or 0) for x in form.getlist('gross_weight[]')]
        stone_weights = [float(x or 0) for x in form.getlist('stone_weight[]')]
        making_charges = [float(x or 0) for x in form.getlist('making_charge[]')]
        making_charge_types = form.getlist('making_charge_type[]')
        wastage_percents = [float(x or 0) for x in form.getlist('wastage_percent[]')]
        item_rates = [float(x or 0) for x in form.getlist('item_rate[]')]
        stone_charges = [float(x or 0) for x in form.getlist('stone_charge[]')]
        hallmark_charges = [float(x or 0) for x in form.getlist('hallmark_charge[]')]
        grams_arr = [float(x or 0) for x in form.getlist('grams[]')]

        discount = float(form.get('discount', 0) or 0)
        old_item_purity = form.get('old_item_purity', '')
        old_item_grams = float(form.get('old_item_grams', 0) or 0)
        old_item_rate = float(form.get('old_item_rate', 0) or 0)
        old_mult = get_purity_multiplier(old_item_purity)
        old_item_value = round(old_item_grams * old_item_rate * old_mult, 2)
        amount_paid = float(form.get('amount_paid', 0) or 0)

        # Remove old bill items
        for bi in list(bill.items):
            db.session.delete(bi)

        total_subtotal = 0
        total_stone = 0
        total_hallmark = 0
        bill_items = []

        for i in range(len(item_types)):
            gw = gross_weights[i] if i < len(gross_weights) else 0
            sw = stone_weights[i] if i < len(stone_weights) else 0
            g = grams_arr[i] if i < len(grams_arr) else 0
            rate = item_rates[i] if i < len(item_rates) else 0
            making = making_charges[i] if i < len(making_charges) else 0
            making_type = making_charge_types[i] if i < len(making_charge_types) else 'per_gram'
            wastage = wastage_percents[i] if i < len(wastage_percents) else 0
            stone_charge = stone_charges[i] if i < len(stone_charges) else 0
            hallmark_charge = hallmark_charges[i] if i < len(hallmark_charges) else 0

            nw = gw - sw if gw > 0 else g
            if nw <= 0 and gw <= 0 and g <= 0:
                continue

            if making_type == 'per_gram':
                making_amt = nw * making
            elif making_type == 'percentage':
                making_amt = (making / 100) * rate * nw
            else:
                making_amt = making

            wastage_amt = (wastage / 100) * rate * nw
            metal_value = nw * rate
            item_subtotal = metal_value + making_amt + wastage_amt + stone_charge + hallmark_charge
            total_stone += stone_charge
            total_hallmark += hallmark_charge
            total_subtotal += item_subtotal

            b_item = BillItem(
                item_type=item_types[i],
                item_name=item_names[i] if i < len(item_names) else '',
                purity=purities[i] if i < len(purities) else '',
                gross_weight=gw, stone_weight=sw, net_weight=nw,
                grams=g if g > 0 else nw,
                making_charge=making, making_charge_type=making_type,
                wastage_percent=wastage, item_rate=rate,
                stone_charge=stone_charge, hallmark_charge=hallmark_charge,
                item_amount=item_subtotal
            )
            bill_items.append(b_item)

        cgst = round(total_subtotal * 0.015, 2)
        sgst = round(total_subtotal * 0.015, 2)
        total_before_discount = total_subtotal + cgst + sgst
        total = round(max(total_before_discount - discount - old_item_value, 0), 2)
        balance = round(max(total - amount_paid, 0), 2)

        if balance <= 0:
            status = 'Paid'; balance = 0
        elif amount_paid > 0:
            status = 'Partial'
        else:
            status = 'Pending'

        # Update bill fields
        bill.customer_name = form.get('customer_name', bill.customer_name)
        bill.phone = form.get('phone', bill.phone)
        bill.address = form.get('address', bill.address)
        bill.city = form.get('city', bill.city)
        bill.gst_number = form.get('gst_number', bill.gst_number)
        bill.payment_mode = form.get('payment_mode', bill.payment_mode)
        bill.notes = form.get('notes', bill.notes)
        bill.old_item_type = form.get('old_item_type', '')
        bill.old_item_name = form.get('old_item_name', '')
        bill.old_item_purity = old_item_purity
        bill.old_item_grams = old_item_grams
        bill.old_item_rate = old_item_rate
        bill.old_item_value = old_item_value
        bill.discount = discount
        bill.cgst = cgst; bill.sgst = sgst
        bill.subtotal = round(total_subtotal, 2)
        bill.total = total
        bill.amount_paid = amount_paid
        bill.balance = balance
        bill.status = status
        bill.item_type = item_types[0] if item_types else bill.item_type
        bill.item_name = item_names[0] if item_names else bill.item_name
        bill.purity = purities[0] if purities else bill.purity
        bill.gross_weight = gross_weights[0] if gross_weights else bill.gross_weight
        bill.stone_weight = stone_weights[0] if stone_weights else bill.stone_weight
        bill.net_weight = bill_items[0].net_weight if bill_items else bill.net_weight
        bill.grams = bill_items[0].grams if bill_items else bill.grams
        bill.making_charge = making_charges[0] if making_charges else bill.making_charge
        bill.making_charge_type = making_charge_types[0] if making_charge_types else bill.making_charge_type
        bill.wastage_percent = wastage_percents[0] if wastage_percents else bill.wastage_percent
        bill.item_rate = item_rates[0] if item_rates else bill.item_rate
        bill.stone_charge = total_stone
        bill.hallmark_charge = total_hallmark

        for bi in bill_items:
            bill.items.append(bi)

        db.session.commit()
        flash('Bill updated successfully!', 'success')
        return redirect(url_for('view_bill', bill_id=bill.id))
    except Exception as e:
        db.session.rollback()
        flash(f'Error updating bill: {str(e)}', 'error')
        return redirect(url_for('edit_bill', bill_id=bill_id))


@app.route('/bills')
def all_bills():
    """View all bills with search and filters"""
    search = request.args.get('search', '')
    item_filter = request.args.get('item_type', '')
    status_filter = request.args.get('status', '')

    query = Bill.query
    if search:
        query = query.filter(
            (Bill.customer_name.ilike(f'%{search}%')) |
            (Bill.phone.ilike(f'%{search}%')) |
            (Bill.bill_number.ilike(f'%{search}%'))
        )
    if item_filter:
        query = query.filter_by(item_type=item_filter)
    if status_filter:
        query = query.filter_by(status=status_filter)

    bills = query.order_by(Bill.id.desc()).all()
    return render_template("all_bills.html", bills=bills, search=search,
                         item_filter=item_filter, status_filter=status_filter)


# ==========================================
# SALES REPORT HELPERS & PDF/EXCEL EXPORT
# ==========================================

def parse_bill_date(bill):
    """Safely parse bill date or fallback to created_at / today"""
    raw_date = getattr(bill, 'date', '') or ''
    raw_date = str(raw_date).strip()
    if raw_date:
        for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%Y/%m/%d'):
            try:
                return datetime.strptime(raw_date, fmt).date()
            except ValueError:
                pass
    created_at = getattr(bill, 'created_at', None)
    if created_at:
        if isinstance(created_at, datetime):
            return created_at.date()
        try:
            return datetime.fromisoformat(str(created_at)).date()
        except Exception:
            pass
    return datetime.today().date()


def get_timeline_dates(timeline=None, from_date_str=None, to_date_str=None):
    """Calculate date boundaries based on custom input or timeline presets"""
    today = datetime.today().date()
    
    # Preset calculations
    preset_dates = {
        'today': (today, today),
        'monthly': (today.replace(day=1), today),
        'last_month': (
            (today.replace(day=1) - timedelta(days=1)).replace(day=1),
            today.replace(day=1) - timedelta(days=1)
        ),
        'yearly': (
            date(today.year if today.month >= 4 else today.year - 1, 4, 1),
            date(today.year + 1 if today.month >= 4 else today.year, 3, 31)
        ),
        'calendar_year': (
            date(today.year, 1, 1),
            date(today.year, 12, 31)
        )
    }

    # If the user changed the from_date or to_date away from the preset values, treat as custom
    if from_date_str and to_date_str:
        try:
            custom_start = datetime.strptime(from_date_str, '%Y-%m-%d').date()
            custom_end = datetime.strptime(to_date_str, '%Y-%m-%d').date()
            
            # If timeline is empty or custom, or dates differ from the preset's expected dates
            if not timeline or timeline == 'custom':
                return 'custom', custom_start, custom_end
            
            if timeline in preset_dates:
                p_start, p_end = preset_dates[timeline]
                if custom_start != p_start or custom_end != p_end:
                    # User picked custom dates while having a preset selected
                    return 'custom', custom_start, custom_end
                else:
                    return timeline, p_start, p_end
            else:
                return 'custom', custom_start, custom_end
        except Exception:
            pass

    if timeline in preset_dates:
        p_start, p_end = preset_dates[timeline]
        return timeline, p_start, p_end

    # Default to Financial Year (matches reference PDF: 01/04/2026 to 31/03/2027)
    p_start, p_end = preset_dates['yearly']
    return 'yearly', p_start, p_end


def generate_sales_report_pdf_bytes(bills, from_date_str, to_date_str, username="All Users"):
    """
    Generate Sales Report PDF with balanced sizing to fit 3 sales perfectly per A4 page.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=8*mm,
        rightMargin=8*mm,
        topMargin=7*mm,
        bottomMargin=8*mm
    )
    
    f_body = 'Arial' if font_registered else 'Helvetica'
    f_bold = 'Arial-Bold' if font_registered else 'Helvetica-Bold'
    
    # ── Header styles ──
    style_shop_name = ParagraphStyle(
        'SRShopName', fontName=f_bold, fontSize=12.5, leading=15,
        alignment=TA_RIGHT, textColor=colors.black
    )
    style_shop_info = ParagraphStyle(
        'SRShopInfo', fontName=f_body, fontSize=7.5, leading=10,
        alignment=TA_RIGHT, textColor=colors.HexColor('#333333')
    )
    style_report_title = ParagraphStyle(
        'SRTitle', fontName=f_bold, fontSize=11.5, leading=14,
        alignment=TA_CENTER, textColor=colors.black
    )
    style_meta = ParagraphStyle(
        'SRMeta', fontName=f_bold, fontSize=8, leading=10.5,
        alignment=TA_LEFT, textColor=colors.black
    )
    
    # ── Table styles (balanced 7.5pt font to fit page height nicely) ──
    f_sz = 7.5
    f_lead = 9.8
    th_style_c = ParagraphStyle('SRTHC', fontName=f_bold, fontSize=f_sz, leading=f_lead, alignment=TA_CENTER, textColor=colors.black)
    th_style_l = ParagraphStyle('SRTHL', fontName=f_bold, fontSize=f_sz, leading=f_lead, alignment=TA_LEFT, textColor=colors.black)
    th_style_r = ParagraphStyle('SRTHR', fontName=f_bold, fontSize=f_sz, leading=f_lead, alignment=TA_RIGHT, textColor=colors.black)
    
    td_style_c = ParagraphStyle('SRTDC', fontName=f_body, fontSize=f_sz, leading=f_lead, alignment=TA_CENTER, textColor=colors.black)
    td_style_l = ParagraphStyle('SRTDL', fontName=f_body, fontSize=f_sz, leading=f_lead, alignment=TA_LEFT, textColor=colors.black)
    td_style_r = ParagraphStyle('SRTDR', fontName=f_body, fontSize=f_sz, leading=f_lead, alignment=TA_RIGHT, textColor=colors.black)
    
    td_bold_r = ParagraphStyle('SRTDBR', fontName=f_bold, fontSize=f_sz, leading=f_lead, alignment=TA_RIGHT, textColor=colors.black)
    td_bold_l = ParagraphStyle('SRTDBL', fontName=f_bold, fontSize=f_sz, leading=f_lead, alignment=TA_LEFT, textColor=colors.black)
    td_bold_c = ParagraphStyle('SRTDBC', fontName=f_bold, fontSize=f_sz, leading=f_lead, alignment=TA_CENTER, textColor=colors.black)
    
    elements = []
    
    # ── 1. Store Header ──
    logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'logo.png')
    logo_img = None
    if os.path.exists(logo_path):
        logo_img = Image(logo_path, width=13*mm, height=13*mm)
        logo_img.hAlign = 'LEFT'
    
    shop_info_p = [
        Paragraph("<b>Laxmi Srinivasa Jewellery</b>", style_shop_name),
        Paragraph("GROUND 6-162 Annavaram Main Road Annavaram", style_shop_info),
        Paragraph("Phone no.: 9951050648 &nbsp;|&nbsp; GSTIN: 37CKJPK2161B1ZG, State: 37-Andhra Pradesh", style_shop_info),
    ]
    
    header_data = [[logo_img if logo_img else "", shop_info_p]]
    header_table = Table(header_data, colWidths=[15*mm, 179*mm])
    header_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-1), 0),
        ('BOTTOMPADDING', (0,0), (-1,-1), 0),
    ]))
    elements.append(header_table)
    elements.append(HRFlowable(width='100%', thickness=0.8, color=colors.HexColor('#333333'), spaceBefore=2*mm, spaceAfter=2.5*mm))
    
    # ── 2. Title + Meta ──
    elements.append(Paragraph("<u><b>Sale Report</b></u>", style_report_title))
    elements.append(Spacer(1, 1.5*mm))
    meta_text = f"<b>Username:</b> {username} &nbsp;&nbsp;|&nbsp;&nbsp; <b>Duration:</b> From {from_date_str} to {to_date_str}"
    elements.append(Paragraph(meta_text, style_meta))
    elements.append(Spacer(1, 3*mm))
    
    # ── Column widths (194mm total) ──
    w_meta = [
        14*mm, 13*mm, 10*mm, 25*mm, 18*mm, 16*mm,
        11*mm, 14*mm, 11*mm, 16*mm, 17*mm, 16*mm, 13*mm
    ]
    w_item = [
        8*mm, 48*mm, 14*mm, 16*mm, 12*mm, 24*mm, 25*mm, 18*mm, 29*mm
    ]
    
    grand_total_sale = 0.0
    pad_cell_y = 6.2  # Balanced vertical cell padding to fill page perfectly
    
    for idx, bill in enumerate(bills):
        sale_elements = []
        grand_total_sale += float(bill.total or 0)
        
        b_date = getattr(bill, 'date', '') or (bill.created_at.strftime('%d/%m/%Y') if getattr(bill, 'created_at', None) else '')
        b_no = getattr(bill, 'bill_number', '') or str(getattr(bill, 'id', ''))
        c_name = getattr(bill, 'customer_name', '') or ''
        phone = getattr(bill, 'phone', '') or ''
        gstin = getattr(bill, 'gst_number', '') or ''
        status = getattr(bill, 'status', 'Paid') or 'Paid'
        total = float(getattr(bill, 'total', 0) or 0)
        pay_type = getattr(bill, 'payment_mode', 'Cash') or 'Cash'
        amt_paid = float(getattr(bill, 'amount_paid', total) or total)
        bal = float(getattr(bill, 'balance', 0) or 0)
        
        meta_headers = [
            Paragraph("Date", th_style_l),
            Paragraph("Order<br/>No.", th_style_c),
            Paragraph("Ref<br/>No.", th_style_c),
            Paragraph("Party<br/>Name", th_style_l),
            Paragraph("Phone<br/>No", th_style_l),
            Paragraph("Party's<br/>GSTIN<br/>No.", th_style_l),
            Paragraph("Txn<br/>Type", th_style_c),
            Paragraph("Due<br/>Date.", th_style_c),
            Paragraph("Status", th_style_c),
            Paragraph("Total<br/>Amount", th_style_r),
            Paragraph("Payment<br/>Type", th_style_c),
            Paragraph("Received/<br/>Paid Amt", th_style_r),
            Paragraph("Balance<br/>Amt", th_style_r)
        ]
        
        meta_row = [
            Paragraph(b_date, td_style_l),
            Paragraph(b_no, td_style_c),
            Paragraph("", td_style_c),
            Paragraph(c_name, td_style_l),
            Paragraph(phone, td_style_l),
            Paragraph(gstin, td_style_l),
            Paragraph("Sale", td_style_c),
            Paragraph(b_date, td_style_c),
            Paragraph(status, td_style_c),
            Paragraph(f"Rs {total:,.0f}", td_style_r),
            Paragraph(pay_type, td_style_c),
            Paragraph(f"Rs {amt_paid:,.0f}", td_style_r),
            Paragraph(f"Rs {bal:,.0f}", td_style_r)
        ]
        
        b_meta_tbl = Table([meta_headers, meta_row], colWidths=w_meta)
        b_meta_tbl.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#d9d9d9')),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('TOPPADDING', (0,0), (-1,-1), pad_cell_y),
            ('BOTTOMPADDING', (0,0), (-1,-1), pad_cell_y),
            ('LEFTPADDING', (0,0), (-1,-1), 1.5),
            ('RIGHTPADDING', (0,0), (-1,-1), 1.5),
            ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#d0d0d0')),
        ]))
        sale_elements.append(b_meta_tbl)
        sale_elements.append(Spacer(1, 1.8*mm))
        
        item_rows = [[
            Paragraph("#", th_style_c),
            Paragraph("Item name", th_style_l),
            Paragraph("HSN/<br/>SAC", th_style_c),
            Paragraph("Quantity", th_style_c),
            Paragraph("Grams", th_style_c),
            Paragraph("Price/<br/>unit", th_style_r),
            Paragraph("GST", th_style_r),
            Paragraph("Ad. CESS", th_style_r),
            Paragraph("Amount", th_style_r)
        ]]
        
        b_items = getattr(bill, 'items', None)
        if not b_items:
            b_items = [bill]
            
        tot_qty = 0.0
        tot_gst = (getattr(bill, 'cgst', 0) or 0) + (getattr(bill, 'sgst', 0) or 0)
        tot_cess = getattr(bill, 'making_charge', 0) or 0
        
        for i_idx, item in enumerate(b_items, start=1):
            i_name = getattr(item, 'item_name', '') or getattr(bill, 'item_name', '')
            i_grams = float(getattr(item, 'grams', 0) or getattr(bill, 'grams', 0) or 0)
            i_rate = float(getattr(item, 'item_rate', 0) or getattr(bill, 'item_rate', 0) or 0)
            i_amt = float(getattr(item, 'item_amount', 0) or (i_grams * i_rate) or total)
            
            item_gst = tot_gst / len(b_items) if tot_gst else (i_amt * 0.03)
            item_cess = tot_cess / len(b_items) if tot_cess else 0.0
            tot_qty += i_grams if i_grams > 0 else 1.0
            
            gst_str = f"Rs {item_gst:,.1f} (3%)" if item_gst > 0 else "Rs 0"
            cess_str = f"Rs {item_cess:,.0f}" if item_cess > 0 else "Rs 0"
            
            item_rows.append([
                Paragraph(str(i_idx), td_style_c),
                Paragraph(i_name, td_style_l),
                Paragraph("", td_style_c),
                Paragraph(f"{i_grams:.1f}" if i_grams > 0 else "1", td_style_c),
                Paragraph("Gm" if i_grams > 0 else "", td_style_c),
                Paragraph(f"Rs {i_rate:,.1f}" if i_rate > 0 else "-", td_style_r),
                Paragraph(gst_str, td_style_r),
                Paragraph(cess_str, td_style_r),
                Paragraph(f"Rs {i_amt:,.0f}", td_style_r)
            ])
            
        tot_gst_display = f"Rs {tot_gst:,.1f}" if tot_gst > 0 else "Rs 0"
        tot_cess_display = f"Rs {tot_cess:,.0f}" if tot_cess > 0 else "Rs 0"
        item_rows.append([
            "",
            Paragraph("Total", td_bold_l),
            "",
            Paragraph(f"{tot_qty:.1f}", td_bold_c),
            "", "",
            Paragraph(tot_gst_display, td_bold_r),
            Paragraph(tot_cess_display, td_bold_r),
            Paragraph(f"Rs {total:,.0f}", td_bold_r)
        ])
        
        b_item_tbl = Table(item_rows, colWidths=w_item)
        b_item_tbl.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#e8e8e8')),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('TOPPADDING', (0,0), (-1,-1), pad_cell_y),
            ('BOTTOMPADDING', (0,0), (-1,-1), pad_cell_y),
            ('LEFTPADDING', (0,0), (-1,-1), 1.5),
            ('RIGHTPADDING', (0,0), (-1,-1), 1.5),
            ('LINEABOVE', (0,-1), (-1,-1), 0.5, colors.HexColor('#999999')),
            ('LINEBELOW', (0,-1), (-1,-1), 0.5, colors.HexColor('#999999')),
        ]))
        sale_elements.append(b_item_tbl)
        sale_elements.append(Spacer(1, 1.5*mm))
        
        sub_total_tbl = Table([
            ["", Paragraph("<b>Sub Total:</b>", td_bold_r), Paragraph(f"<b>Rs {total:,.0f}</b>", td_bold_r)]
        ], colWidths=[120*mm, 35*mm, 39*mm])
        sub_total_tbl.setStyle(TableStyle([
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('TOPPADDING', (0,0), (-1,-1), pad_cell_y * 0.7),
            ('BOTTOMPADDING', (0,0), (-1,-1), pad_cell_y * 0.7),
            ('LINEABOVE', (1,0), (-1,-1), 0.5, colors.HexColor('#cccccc')),
            ('LINEBELOW', (1,0), (-1,-1), 0.5, colors.HexColor('#cccccc')),
        ]))
        sale_elements.append(sub_total_tbl)
        sale_elements.append(Spacer(1, 5.5*mm))
        sale_elements.append(HRFlowable(width='100%', thickness=0.4, color=colors.HexColor('#cccccc'), spaceAfter=5.5*mm))
        
        elements.append(KeepTogether(sale_elements))
        
        # ── Page break after every 3 sales ──
        if (idx + 1) % 3 == 0 and (idx + 1) < len(bills):
            elements.append(PageBreak())
            
    # ── Grand Total ──
    grand_total_tbl = Table([
        ["", Paragraph(f"<b>Total Sale: Rs {grand_total_sale:,.0f}</b>", ParagraphStyle('SRGT', parent=td_bold_r, fontSize=10, leading=12))]
    ], colWidths=[110*mm, 84*mm])
    grand_total_tbl.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-1), 3),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3),
    ]))
    elements.append(grand_total_tbl)
    
    # ── Page-number footer ──
    class NumberedCanvas(canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._saved_page_states = []

        def showPage(self):
            self._saved_page_states.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            num_pages = len(self._saved_page_states)
            for state in self._saved_page_states:
                self.__dict__.update(state)
                self.draw_page_decorations(num_pages)
                super().showPage()
            super().save()

        def draw_page_decorations(self, page_count):
            self.saveState()
            self.setFont(f_body, 7)
            self.setFillColor(colors.HexColor("#777777"))
            now_str = datetime.now().strftime("Generated on %b %d, %Y at %I:%M %p")
            self.drawString(8*mm, 5*mm, now_str)
            page_text = f"Page {self._pageNumber} of {page_count}"
            self.drawRightString(202*mm, 5*mm, page_text)
            self.restoreState()
            
    doc.build(elements, canvasmaker=NumberedCanvas)
    return buf.getvalue()


def generate_sales_report_excel_bytes(bills, from_date_str, to_date_str, username="All Users"):
    """Generate formatted Excel report matching the sales transactions"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sale Report"
    
    title_font = Font(name="Calibri", size=16, bold=True, color="FFFFFF")
    title_fill = PatternFill(start_color="1B365D", end_color="1B365D", fill_type="solid")
    
    sub_font = Font(name="Calibri", size=9, italic=True, color="555555")
    meta_font = Font(name="Calibri", size=10, bold=True, color="333333")
    meta_fill = PatternFill(start_color="F0F4F8", end_color="F0F4F8", fill_type="solid")
    
    header_font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="2C3E50", end_color="2C3E50", fill_type="solid")
    
    data_font = Font(name="Calibri", size=10)
    data_bold = Font(name="Calibri", size=10, bold=True)
    
    thin_border_side = Side(border_style="thin", color="CCCCCC")
    data_border = Border(left=thin_border_side, right=thin_border_side, top=thin_border_side, bottom=thin_border_side)
    
    summary_fill = PatternFill(start_color="FFF3CD", end_color="FFF3CD", fill_type="solid")
    summary_border = Border(
        top=Side(border_style="thin", color="000000"),
        bottom=Side(border_style="double", color="000000")
    )
    
    # Title Rows
    ws.merge_cells("A1:P1")
    cell_a1 = ws["A1"]
    cell_a1.value = "LAXMI SRINIVASA JEWELLERY - SALE REPORT"
    cell_a1.font = title_font
    cell_a1.fill = title_fill
    cell_a1.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 35
    
    ws.merge_cells("A2:P2")
    cell_a2 = ws["A2"]
    cell_a2.value = "GROUND 6-162 Annavaram Main Road Annavaram | Phone: 9951050648 | GSTIN: 37CKJPK2161B1ZG"
    cell_a2.font = sub_font
    cell_a2.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[2].height = 18
    
    ws.merge_cells("A3:P3")
    cell_a3 = ws["A3"]
    now_str = datetime.now().strftime("%d/%m/%Y %I:%M %p")
    cell_a3.value = f"Duration: From {from_date_str} to {to_date_str}   |   User: {username}   |   Generated On: {now_str}"
    cell_a3.font = meta_font
    cell_a3.fill = meta_fill
    cell_a3.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[3].height = 22
    
    ws.row_dimensions[4].height = 10
    
    headers = [
        "Date", "Order / Bill No.", "Customer Name", "Phone No.", "GSTIN",
        "Item Name", "Item Type", "Grams (Qty)", "Price/Unit (₹)", "GST 3% (₹)",
        "Ad. CESS / Making (₹)", "Total Amount (₹)", "Paid Amount (₹)", "Balance (₹)",
        "Payment Mode", "Status"
    ]
    
    ws.row_dimensions[5].height = 25
    for col_idx, h in enumerate(headers, start=1):
        c = ws.cell(row=5, column=col_idx, value=h)
        c.font = header_font
        c.fill = header_fill
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = Border(bottom=Side(border_style="medium", color="000000"))
        
    current_row = 6
    total_qty = 0.0
    total_gst = 0.0
    total_cess = 0.0
    grand_total_sale = 0.0
    grand_total_paid = 0.0
    grand_total_bal = 0.0
    
    alt_fill = PatternFill(start_color="F9FAFB", end_color="F9FAFB", fill_type="solid")
    
    for bill in bills:
        b_items = getattr(bill, 'items', None)
        if not b_items:
            b_items = [bill]
            
        b_date = getattr(bill, 'date', '') or (bill.created_at.strftime('%d-%m-%Y') if getattr(bill, 'created_at', None) else '')
        b_no = getattr(bill, 'bill_number', '') or str(getattr(bill, 'id', ''))
        c_name = getattr(bill, 'customer_name', '') or ''
        phone = getattr(bill, 'phone', '') or ''
        gstin = getattr(bill, 'gst_number', '') or ''
        b_total = float(getattr(bill, 'total', 0) or 0)
        pay_mode = getattr(bill, 'payment_mode', 'Cash') or 'Cash'
        b_paid = float(getattr(bill, 'amount_paid', b_total) or b_total)
        b_bal = float(getattr(bill, 'balance', 0) or 0)
        status = getattr(bill, 'status', 'Paid') or 'Paid'
        
        grand_total_sale += b_total
        grand_total_paid += b_paid
        grand_total_bal += b_bal
        
        b_gst_tot = (getattr(bill, 'cgst', 0) or 0) + (getattr(bill, 'sgst', 0) or 0)
        b_cess_tot = getattr(bill, 'making_charge', 0) or 0
        
        for item_idx, itm in enumerate(b_items):
            i_name = getattr(itm, 'item_name', '') or getattr(bill, 'item_name', '')
            i_type = getattr(itm, 'item_type', '') or getattr(bill, 'item_type', '')
            i_grams = float(getattr(itm, 'grams', 0) or getattr(bill, 'grams', 0) or 0)
            i_rate = float(getattr(itm, 'item_rate', 0) or getattr(bill, 'item_rate', 0) or 0)
            i_amt = float(getattr(itm, 'item_amount', 0) or (i_grams * i_rate) or b_total)
            
            i_gst = (b_gst_tot / len(b_items)) if b_gst_tot else (i_amt * 0.03)
            i_cess = (b_cess_tot / len(b_items)) if b_cess_tot else 0.0
            
            total_qty += i_grams
            total_gst += i_gst
            total_cess += i_cess
            
            row_data = [
                b_date,
                b_no,
                c_name,
                phone,
                gstin,
                i_name,
                i_type,
                round(i_grams, 3),
                round(i_rate, 2),
                round(i_gst, 2),
                round(i_cess, 2),
                round(i_amt, 2),
                round(b_paid, 2) if item_idx == 0 else 0.0,
                round(b_bal, 2) if item_idx == 0 else 0.0,
                pay_mode if item_idx == 0 else "",
                status if item_idx == 0 else ""
            ]
            
            for col_idx, val in enumerate(row_data, start=1):
                c = ws.cell(row=current_row, column=col_idx, value=val)
                c.font = data_font
                c.border = data_border
                
                if current_row % 2 == 0:
                    c.fill = alt_fill
                    
                if col_idx in [1, 2, 15, 16]:
                    c.alignment = Alignment(horizontal="center", vertical="center")
                elif col_idx in [3, 5, 6, 7]:
                    c.alignment = Alignment(horizontal="left", vertical="center")
                elif col_idx in [4]:
                    c.alignment = Alignment(horizontal="center", vertical="center")
                elif col_idx == 8:
                    c.alignment = Alignment(horizontal="right", vertical="center")
                    c.number_format = "0.000"
                else:
                    c.alignment = Alignment(horizontal="right", vertical="center")
                    c.number_format = "#,##0.00"
                    
            ws.row_dimensions[current_row].height = 20
            current_row += 1

    # Grand Total Row
    ws.row_dimensions[current_row].height = 24
    ws.merge_cells(start_row=current_row, start_column=1, end_row=current_row, end_column=7)
    tot_label_cell = ws.cell(row=current_row, column=1, value="GRAND TOTAL")
    tot_label_cell.font = data_bold
    tot_label_cell.alignment = Alignment(horizontal="right", vertical="center")
    tot_label_cell.fill = summary_fill
    
    for c_i in range(1, 8):
        ws.cell(row=current_row, column=c_i).fill = summary_fill
        ws.cell(row=current_row, column=c_i).border = summary_border

    tot_map = {
        8: (total_qty, "0.000"),
        9: ("", ""),
        10: (total_gst, "#,##0.00"),
        11: (total_cess, "#,##0.00"),
        12: (grand_total_sale, "#,##0.00"),
        13: (grand_total_paid, "#,##0.00"),
        14: (grand_total_bal, "#,##0.00"),
        15: ("", ""),
        16: ("", "")
    }
    
    for c_i, (val, fmt) in tot_map.items():
        c = ws.cell(row=current_row, column=c_i, value=val)
        c.font = data_bold
        c.fill = summary_fill
        c.border = summary_border
        c.alignment = Alignment(horizontal="right", vertical="center")
        if fmt:
            c.number_format = fmt

    # Auto-adjust column widths
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            if cell.row in [1, 2, 3]:
                continue
            val_str = str(cell.value or '')
            if len(val_str) > max_len:
                max_len = len(val_str)
        ws.column_dimensions[col_letter].width = max(max_len + 4, 12)
        
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ==========================================
# SALES REPORT ROUTES
# ==========================================

@app.route('/sales-report')
def sales_report():
    """Sales report dashboard with timeline filters, print, save, and share options"""
    timeline_req = request.args.get('timeline', '')
    from_date_req = request.args.get('from_date', '')
    to_date_req = request.args.get('to_date', '')
    status_filter = request.args.get('status', '')
    payment_filter = request.args.get('payment_mode', '')
    search = request.args.get('search', '').strip()
    
    timeline, start_date, end_date = get_timeline_dates(timeline_req, from_date_req, to_date_req)
    
    query = Bill.query
    if status_filter:
        query = query.filter_by(status=status_filter)
    if payment_filter:
        query = query.filter_by(payment_mode=payment_filter)
    if search:
        query = query.filter(
            (Bill.customer_name.ilike(f'%{search}%')) |
            (Bill.phone.ilike(f'%{search}%')) |
            (Bill.bill_number.ilike(f'%{search}%'))
        )
        
    all_b = query.order_by(Bill.id.desc()).all()
    
    filtered_bills = []
    total_sales_amount = 0.0
    total_paid_amount = 0.0
    total_balance_amount = 0.0
    total_grams = 0.0
    
    for b in all_b:
        b_date = parse_bill_date(b)
        if start_date <= b_date <= end_date:
            filtered_bills.append(b)
            total_sales_amount += float(b.total or 0)
            total_paid_amount += float(b.amount_paid or b.total or 0)
            total_balance_amount += float(b.balance or 0)
            
            if b.items:
                for itm in b.items:
                    total_grams += float(itm.grams or 0)
            else:
                total_grams += float(b.grams or 0)

    from_date_formatted = start_date.strftime('%d/%m/%Y')
    to_date_formatted = end_date.strftime('%d/%m/%Y')
    now_formatted = datetime.now().strftime('%b %d, %Y at %I:%M %p')
    
    query_params = {
        'timeline': timeline,
        'from_date': start_date.strftime('%Y-%m-%d'),
        'to_date': end_date.strftime('%Y-%m-%d'),
        'status': status_filter,
        'payment_mode': payment_filter,
        'search': search
    }
    query_string = urllib.parse.urlencode({k: v for k, v in query_params.items() if v})
    
    whatsapp_text = (
        f"*Laxmi Srinivasa Jewellery - Sales Report*\n"
        f"Duration: {from_date_formatted} to {to_date_formatted}\n"
        f"Total Bills: {len(filtered_bills)}\n"
        f"Total Sales: Rs {total_sales_amount:,.2f}\n"
        f"Total Weight: {total_grams:,.2f} Gm"
    )
    whatsapp_share_text = urllib.parse.quote(whatsapp_text)
    
    username = 'LSJ Admin'
    
    return render_template(
        'sales_report.html',
        bills=filtered_bills,
        timeline=timeline,
        from_date_val=start_date.strftime('%Y-%m-%d'),
        to_date_val=end_date.strftime('%Y-%m-%d'),
        from_date_formatted=from_date_formatted,
        to_date_formatted=to_date_formatted,
        status_filter=status_filter,
        payment_filter=payment_filter,
        search=search,
        total_sales_amount=total_sales_amount,
        total_paid_amount=total_paid_amount,
        total_balance_amount=total_balance_amount,
        total_grams=total_grams,
        now_formatted=now_formatted,
        query_string=query_string,
        whatsapp_share_text=whatsapp_share_text,
        username=username
    )


@app.route('/sales-report/pdf')
def sales_report_pdf():
    """Download or stream Sales Report PDF matching exact reference format"""
    timeline_req = request.args.get('timeline', '')
    from_date_req = request.args.get('from_date', '')
    to_date_req = request.args.get('to_date', '')
    status_filter = request.args.get('status', '')
    payment_filter = request.args.get('payment_mode', '')
    search = request.args.get('search', '').strip()
    download = request.args.get('download', '')
    
    timeline, start_date, end_date = get_timeline_dates(timeline_req, from_date_req, to_date_req)
    
    query = Bill.query
    if status_filter:
        query = query.filter_by(status=status_filter)
    if payment_filter:
        query = query.filter_by(payment_mode=payment_filter)
    if search:
        query = query.filter(
            (Bill.customer_name.ilike(f'%{search}%')) |
            (Bill.phone.ilike(f'%{search}%')) |
            (Bill.bill_number.ilike(f'%{search}%'))
        )
        
    all_b = query.order_by(Bill.id.desc()).all()
    filtered_bills = [b for b in all_b if start_date <= parse_bill_date(b) <= end_date]
    
    from_date_str = start_date.strftime('%d/%m/%Y')
    to_date_str = end_date.strftime('%d/%m/%Y')
    username = 'LSJ Admin'
    
    pdf_bytes = generate_sales_report_pdf_bytes(filtered_bills, from_date_str, to_date_str, username)
    filename = f"Sale_Report_{start_date.strftime('%d_%m_%Y')}_to_{end_date.strftime('%d_%m_%Y')}.pdf"
    
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype='application/pdf',
        as_attachment=(download == '1'),
        download_name=filename
    )


@app.route('/sales-report/excel')
def sales_report_excel():
    """Download Sales Report Excel workbook"""
    timeline_req = request.args.get('timeline', '')
    from_date_req = request.args.get('from_date', '')
    to_date_req = request.args.get('to_date', '')
    status_filter = request.args.get('status', '')
    payment_filter = request.args.get('payment_mode', '')
    search = request.args.get('search', '').strip()
    
    timeline, start_date, end_date = get_timeline_dates(timeline_req, from_date_req, to_date_req)
    
    query = Bill.query
    if status_filter:
        query = query.filter_by(status=status_filter)
    if payment_filter:
        query = query.filter_by(payment_mode=payment_filter)
    if search:
        query = query.filter(
            (Bill.customer_name.ilike(f'%{search}%')) |
            (Bill.phone.ilike(f'%{search}%')) |
            (Bill.bill_number.ilike(f'%{search}%'))
        )
        
    all_b = query.order_by(Bill.id.desc()).all()
    filtered_bills = [b for b in all_b if start_date <= parse_bill_date(b) <= end_date]
    
    from_date_str = start_date.strftime('%d/%m/%Y')
    to_date_str = end_date.strftime('%d/%m/%Y')
    username = 'LSJ Admin'
    
    excel_bytes = generate_sales_report_excel_bytes(filtered_bills, from_date_str, to_date_str, username)
    filename = f"Sale_Report_{start_date.strftime('%d_%m_%Y')}_to_{end_date.strftime('%d_%m_%Y')}.xlsx"
    
    return send_file(
        io.BytesIO(excel_bytes),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=filename
    )




@app.route('/delete-bill/<int:bill_id>', methods=['POST'])
def delete_bill(bill_id):
    """Delete a bill and restore inventory weight"""
    bill = Bill.query.get_or_404(bill_id)
    
    # Restore inventory weight and quantity for each bill item
    for bi in bill.items:
        if bi.inventory_item_id:
            inv_item = InventoryItem.query.get(bi.inventory_item_id)
            if inv_item:
                inv_item.quantity += 1
                billed_weight = bi.net_weight or bi.grams or 0
                if billed_weight > 0:
                    inv_item.total_weight = round((inv_item.total_weight or 0) + billed_weight, 3)
                db.session.add(inv_item)
    
    db.session.delete(bill)
    db.session.commit()
    flash('Bill deleted successfully! Inventory restored.', 'success')
    return redirect(url_for('all_bills'))


@app.route('/update-payment/<int:bill_id>', methods=['POST'])
def update_payment(bill_id):
    """Record an additional payment against an existing bill"""
    try:
        bill = Bill.query.get_or_404(bill_id)
        payment_amount = float(request.form.get('payment_amount', 0) or 0)
        
        if payment_amount > 0:
            bill.amount_paid = round(bill.amount_paid + payment_amount, 2)
            bill.balance = round(bill.total - bill.amount_paid, 2)
            
            if bill.balance <= 0:
                bill.balance = 0
                bill.status = 'Paid'
            else:
                bill.status = 'Partial'
                
            db.session.commit()
            flash(f'Payment of Rs.{payment_amount:,.2f} recorded successfully!', 'success')
        else:
            flash('Invalid payment amount.', 'error')
            
    except Exception as e:
        db.session.rollback()
        flash(f'Error recording payment: {str(e)}', 'error')
        
    return redirect(url_for('view_bill', bill_id=bill.id))


@app.route('/api/calculate', methods=['POST'])
def calculate():
    """API endpoint for real-time bill calculation"""
    data = request.json
    grams = float(data.get('grams', 0) or 0)
    making = float(data.get('making_charge', 0) or 0)
    wastage = float(data.get('wastage_percent', 0) or 0)
    rate = float(data.get('item_rate', 0) or 0)
    stone_charge = float(data.get('stone_charge', 0) or 0)
    hallmark_charge = float(data.get('hallmark_charge', 0) or 0)
    discount = float(data.get('discount', 0) or 0)
    old_item_purity = data.get('old_item_purity', '')
    old_item_grams = float(data.get('old_item_grams', 0) or 0)
    old_item_rate = float(data.get('old_item_rate', 0) or 0)
    old_mult = get_purity_multiplier(old_item_purity)
    old_item_value = round(old_item_grams * old_item_rate * old_mult, 2)
    making_type = data.get('making_charge_type', 'per_gram')
    gross_weight = float(data.get('gross_weight', 0) or 0)
    stone_weight = float(data.get('stone_weight', 0) or 0)

    net_weight = gross_weight - stone_weight if gross_weight > 0 else grams

    if making_type == 'per_gram':
        making_amt = net_weight * making
    elif making_type == 'percentage':
        making_amt = (making / 100) * rate * net_weight
    else:
        making_amt = making

    wastage_amt = (wastage / 100) * rate * net_weight
    metal_value = net_weight * rate
    subtotal = metal_value + making_amt + wastage_amt + stone_charge + hallmark_charge
    cgst = round(subtotal * 0.015, 2)
    sgst = round(subtotal * 0.015, 2)
    total = round(subtotal + cgst + sgst - discount - old_item_value, 2)
    total = max(total, 0)

    return jsonify({
        'net_weight': round(net_weight, 3),
        'metal_value': round(metal_value, 2),
        'making_amount': round(making_amt, 2),
        'wastage_amount': round(wastage_amt, 2),
        'subtotal': round(subtotal, 2),
        'cgst': cgst,
        'sgst': sgst,
        'old_item_value': old_item_value,
        'total': total
    })


# ===== LIVE METAL PRICES API =====

@app.route('/api/metal-prices')
def get_metal_prices():
    """API endpoint to fetch live metal prices (used by frontend AJAX)"""
    prices = fetch_live_metal_prices()
    return jsonify(prices)


@app.route('/api/shorten-url', methods=['POST'])
def shorten_url():
    """API endpoint to generate a clean share URL hiding the vercel domain.
    Uses CUSTOM_DOMAIN env var if set (e.g. lsjewels.in), otherwise falls back to TinyURL."""
    data = request.json
    url = data.get('url')
    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    
    # If a custom domain is configured, replace the origin with it
    custom_domain = os.getenv('CUSTOM_DOMAIN', '').strip()
    if custom_domain:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        # Rebuild URL with custom domain
        scheme = 'https'
        clean_url = f"{scheme}://{custom_domain}{parsed.path}"
        return jsonify({'short_url': clean_url})
    
    # Fallback: use TinyURL to hide the vercel app domain
    try:
        response = requests.get(f"https://tinyurl.com/api-create.php?url={url}", timeout=5)
        if response.status_code == 200 and response.text.startswith('http'):
            return jsonify({'short_url': response.text})
        return jsonify({'short_url': url})
    except Exception as e:
        return jsonify({'short_url': url})


# ===== INVENTORY MANAGEMENT =====

@app.route('/inventory')
def inventory():
    """List all inventory items (only those in stock by default)"""
    show_all = request.args.get('all', '0') == '1'
    if show_all:
        items = InventoryItem.query.order_by(InventoryItem.updated_at.desc()).all()
    else:
        # Only show items with quantity > 0
        items = InventoryItem.query.filter(InventoryItem.quantity > 0).order_by(InventoryItem.updated_at.desc()).all()

    return render_template("inventory.html", items=items, show_all=show_all)


def generate_item_code(item_type):
    """Generate unique sequential item code like GD-00001, SV-00002 based on item type"""
    prefixes = {
        'Gold': 'GD',
        'Silver': 'SV',
        'Diamond': 'DM',
        'Platinum': 'PT',
        'Readymade': 'RM'
    }
    prefix = prefixes.get(item_type, 'ITM')
    
    # Get all items with this prefix and find the maximum sequential number
    items = InventoryItem.query.filter(InventoryItem.item_code.like(f"{prefix}-%")).all()
    max_num = 0
    for item in items:
        try:
            parts = item.item_code.split('-')
            if len(parts) > 1:
                num = int(parts[-1])
                if num > max_num:
                    max_num = num
        except ValueError:
            continue
    
    new_num = max_num + 1
    return f"{prefix}-{new_num:05d}"


@app.route('/api/inventory/next-code')
def next_item_code():
    """API endpoint to get the next sequential item code for a given item type"""
    item_type = request.args.get('item_type', 'Gold')
    code = generate_item_code(item_type)
    return jsonify({'item_code': code})


@app.route('/inventory/add', methods=['GET', 'POST'])
def add_inventory():
    """Add new item to inventory"""
    if request.method == 'POST':
        try:
            form = request.form
            code = form.get('item_code', '').strip()
            if not code:
                code = generate_item_code(form.get('item_type', 'Gold'))
            
            item = InventoryItem(
                item_code=code,
                item_name=form.get('item_name'),
                item_type=form.get('item_type'),
                purity=form.get('purity', ''),
                description=form.get('description', ''),
                gross_weight=float(form.get('gross_weight', 0) or 0),
                stone_weight=float(form.get('stone_weight', 0) or 0),
                net_weight=float(form.get('net_weight', 0) or 0),
                making_charge=float(form.get('making_charge', 0) or 0),
                making_charge_type=form.get('making_charge_type', 'per_gram'),
                wastage_percent=float(form.get('wastage_percent', 0) or 0),
                stone_charge=float(form.get('stone_charge', 0) or 0),
                hallmark_charge=float(form.get('hallmark_charge', 0) or 0),
                total_weight=float(form.get('total_weight', 0) or 0),
                quantity=int(form.get('quantity', 1) or 1),
                low_stock_alert=int(form.get('low_stock_alert', 2) or 2)
            )
            db.session.add(item)
            db.session.commit()
            flash('Item added to inventory successfully!', 'success')
            return redirect(url_for('inventory'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error adding item: {str(e)}', 'error')
    return render_template("add_inventory.html")


@app.route('/inventory/edit/<int:item_id>', methods=['GET', 'POST'])
def edit_inventory(item_id):
    """Edit existing inventory item"""
    item = InventoryItem.query.get_or_404(item_id)
    if request.method == 'POST':
        try:
            form = request.form
            item.item_code = form.get('item_code')
            item.item_name = form.get('item_name')
            item.item_type = form.get('item_type')
            item.purity = form.get('purity', '')
            item.description = form.get('description', '')
            item.gross_weight = float(form.get('gross_weight', 0) or 0)
            item.stone_weight = float(form.get('stone_weight', 0) or 0)
            item.net_weight = float(form.get('net_weight', 0) or 0)
            item.making_charge = float(form.get('making_charge', 0) or 0)
            item.making_charge_type = form.get('making_charge_type', 'per_gram')
            item.wastage_percent = float(form.get('wastage_percent', 0) or 0)
            item.stone_charge = float(form.get('stone_charge', 0) or 0)
            item.hallmark_charge = float(form.get('hallmark_charge', 0) or 0)
            item.total_weight = float(form.get('total_weight', 0) or 0)
            item.quantity = int(form.get('quantity', 1) or 1)
            item.low_stock_alert = int(form.get('low_stock_alert', 2) or 2)

            db.session.commit()
            flash('Item updated successfully!', 'success')
            return redirect(url_for('inventory'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error updating item: {str(e)}', 'error')
    return render_template("add_inventory.html", item=item)


@app.route('/inventory/delete/<int:item_id>', methods=['POST'])
def delete_inventory(item_id):
    """Delete item from inventory"""
    item = InventoryItem.query.get_or_404(item_id)
    db.session.delete(item)
    db.session.commit()
    flash('Item deleted from inventory!', 'success')
    return redirect(url_for('inventory'))


@app.route('/api/inventory/search')
def search_inventory():
    """API for searching inventory by code or name"""
    query = request.args.get('q', '')
    if not query:
        return jsonify([])

    items = InventoryItem.query.filter(
        (InventoryItem.item_code.ilike(f'%{query}%')) |
        (InventoryItem.item_name.ilike(f'%{query}%'))
    ).all()

    return jsonify([{
        'id': item.id,
        'item_code': item.item_code,
        'item_name': item.item_name,
        'item_type': item.item_type,
        'purity': item.purity,
        'gross_weight': item.gross_weight,
        'stone_weight': item.stone_weight,
        'net_weight': item.net_weight,
        'making_charge': item.making_charge,
        'making_charge_type': item.making_charge_type,
        'wastage_percent': item.wastage_percent,
        'stone_charge': item.stone_charge,
        'hallmark_charge': item.hallmark_charge,
        'total_weight': item.total_weight,
        'quantity': item.quantity
    } for item in items])


def open_browser():
    """Open browser after short delay"""
    import time
    time.sleep(1.5)
    webbrowser.open_new_tab('http://127.0.0.1:5000')


# ===== PDF GENERATION =====

def generate_bill_pdf_bytes(bill):
    """Generate and return invoice PDF bytes for a bill"""
    buf = io.BytesIO()
    
    # Calculate dynamic height based on number of items to prevent overlap
    bill_items_list = bill.items if bill.items else [bill]
    item_count = len(bill_items_list)
    base_height_mm = 155
    # Add roughly 8mm per item beyond the first 2
    extra_height_mm = max(0, (item_count - 2) * 8)
    # Add extra height for additional totals rows (amount paid, balance due, discount, exchange)
    extra_rows = 0
    if bill.stone_charge:
        extra_rows += 1
    if bill.hallmark_charge:
        extra_rows += 1
    if bill.discount:
        extra_rows += 1
    if bill.old_item_value:
        extra_rows += 1
    if bill.amount_paid and bill.amount_paid != bill.total:
        extra_rows += 2  # Amount Paid + Balance Due
    extra_height_mm += extra_rows * 5
    page_w = 210 * mm
    page_h = (base_height_mm + extra_height_mm) * mm
    
    doc = SimpleDocTemplate(buf, pagesize=(page_w, page_h),
                            leftMargin=10*mm, rightMargin=10*mm,
                            topMargin=8*mm, bottomMargin=6*mm)
    
    # Helper for fonts depending on system registration
    f_body = 'Arial' if font_registered else 'Helvetica'
    f_bold = 'Arial-Bold' if font_registered else 'Helvetica-Bold'
    f_italic = 'Arial-Italic' if font_registered else 'Helvetica-Oblique'
    
    # Always use Rs. for PDF (avoids encoding issues)
    cur_sym = 'Rs.'
    
    def fmt_currency(val):
        return f'{cur_sym}{val:,.2f}'
    
    styles = getSampleStyleSheet()
    
    # Custom styles matching HTML
    style_title = ParagraphStyle('InvTitle', parent=styles['Heading1'],
                                  fontSize=14, textColor=colors.HexColor('#000000'),
                                  alignment=TA_LEFT, spaceAfter=2, fontName='Times-Bold')
    style_subtitle = ParagraphStyle('InvSub', parent=styles['Normal'],
                                     fontSize=8, textColor=colors.HexColor('#444444'),
                                     alignment=TA_LEFT, spaceAfter=4, fontName=f_body)
    style_meta = ParagraphStyle('InvMeta', parent=styles['Normal'],
                                 fontSize=9, textColor=colors.HexColor('#111111'),
                                 alignment=TA_RIGHT, spaceAfter=2, fontName=f_body)
    style_normal = ParagraphStyle('InvNormal', parent=styles['Normal'],
                                   fontSize=9, textColor=colors.HexColor('#111111'), fontName=f_body)
    style_bold = ParagraphStyle('InvBold', parent=styles['Normal'],
                                 fontSize=9, textColor=colors.HexColor('#000000'),
                                 fontName=f_bold)
    style_footer = ParagraphStyle('InvFooter', parent=styles['Normal'],
                                   fontSize=8, textColor=colors.HexColor('#555555'),
                                   alignment=TA_CENTER, fontName=f_italic)
    style_grand = ParagraphStyle('InvGrand', parent=styles['Normal'],
                                  fontSize=11, textColor=colors.HexColor('#000000'),
                                  fontName=f_bold)
    
    elements = []
    gold_color = colors.HexColor('#c5a96e')
    
    # -- Header table (logo + brand left, meta right) --
    logo_path = os.path.join(os.path.dirname(__file__), 'static', 'logo.png')
    brand_para = Paragraph(
        '<b>Laxmi Srinivasa Jewellery</b><br/>'
        '<font size="7" color="#444">Annavaram - 533406 | GSTIN: 37CKJPK2161B1ZG</font><br/>'
        '<font size="7" color="#444">Ph: 9951050648, 9110740648</font>',
        style_title
    )
    if os.path.exists(logo_path):
        logo_img = Image(logo_path, width=14*mm, height=14*mm)
        brand_cell = Table([[logo_img, brand_para]], colWidths=[16*mm, 88*mm])
        brand_cell.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (0, -1), 3),
            ('TOPPADDING', (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ]))
    else:
        brand_cell = brand_para

    header_data = [[
        brand_cell,
        Paragraph(f'<font size="9" color="#c5a96e"><b>TAX INVOICE</b></font><br/>'
                  f'<font size="9"><b>Invoice:</b> {bill.bill_number or "LSJ-" + str(bill.id)}</font><br/>'
                  f'<font size="9"><b>Date:</b> {bill.date}</font>', style_meta)
    ]]
    header_tbl = Table(header_data, colWidths=[108*mm, 82*mm])
    header_tbl.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LINEBELOW', (0, 0), (-1, 0), 1.5, gold_color),
    ]))
    elements.append(header_tbl)
    elements.append(Spacer(1, 4*mm))
    
    # -- Bill To --
    addr = f', {bill.address}' if bill.address else ''
    cust_text = f'<font color="#8b6914"><b>BILL TO:</b></font> <b>{bill.customer_name}</b>{addr} | Ph: {bill.phone}'
    elements.append(Paragraph(cust_text, style_normal))
    elements.append(Spacer(1, 3*mm))
    elements.append(HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#eeeeee')))
    elements.append(Spacer(1, 2*mm))
    
    # -- Items Table --
    items_header = ['Item', 'HSN', 'Type', 'Purity', 'Gross Wt', 'Stone Wt', 'Net Wt', 'Rate/g', 'Amount']
    items_data = [items_header]
    
    bill_items_list = bill.items if bill.items else [bill]
    
    for item in bill_items_list:
        iname = getattr(item, 'item_name', '') or 'Jewellery Item'
        itype = getattr(item, 'item_type', '') or ''
        ipurity = getattr(item, 'purity', '') or '-'
        igw = getattr(item, 'gross_weight', 0) or getattr(item, 'grams', 0) or 0
        isw = getattr(item, 'stone_weight', 0) or 0
        inw = getattr(item, 'net_weight', 0) or getattr(item, 'grams', 0) or 0
        irate = getattr(item, 'item_rate', 0) or 0
        iamt = round(inw * irate, 2)
        
        items_data.append([
            iname, '7113', itype.upper(), ipurity,
            f'{igw:.3f}g', f'{isw:.3f}g', f'{inw:.3f}g',
            fmt_currency(irate), fmt_currency(iamt)
        ])
    
    col_widths = [40*mm, 12*mm, 18*mm, 14*mm, 20*mm, 18*mm, 20*mm, 22*mm, 26*mm]
    items_tbl = Table(items_data, colWidths=col_widths)
    items_tbl.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.transparent),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#333333')),
        ('FONTNAME', (0, 0), (-1, 0), f_bold),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('ALIGN', (0, 0), (0, -1), 'LEFT'),
        ('ALIGN', (1, 0), (3, -1), 'CENTER'),
        ('ALIGN', (4, 0), (-1, -1), 'RIGHT'),
        ('LINEABOVE', (0, 0), (-1, 0), 1.5, gold_color),
        ('LINEBELOW', (0, 0), (-1, 0), 1.5, gold_color),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    elements.append(items_tbl)
    elements.append(Spacer(1, 4*mm))
    
    # -- Totals Section --
    total_metal = 0
    total_making = 0
    total_wastage = 0
    
    for item in bill_items_list:
        inw = getattr(item, 'net_weight', 0) or getattr(item, 'grams', 0) or 0
        irate = getattr(item, 'item_rate', 0) or 0
        imaking = getattr(item, 'making_charge', 0) or 0
        imaking_type = getattr(item, 'making_charge_type', 'per_gram') or 'per_gram'
        iwastage = getattr(item, 'wastage_percent', 0) or 0
        
        total_metal += inw * irate
        if imaking_type == 'per_gram':
            total_making += inw * imaking
        elif imaking_type == 'percentage':
            total_making += (imaking / 100) * irate * inw
        else:
            total_making += imaking
        total_wastage += (iwastage / 100) * irate * inw
    
    totals_data = []
    totals_data.append(['Total Metal Value', fmt_currency(total_metal)])
    totals_data.append(['Total Making Charges', fmt_currency(total_making)])
    totals_data.append(['Total Wastage', fmt_currency(total_wastage)])
    if bill.stone_charge:
        totals_data.append(['Stone/Setting Charge', fmt_currency(bill.stone_charge)])
    if bill.hallmark_charge:
        totals_data.append(['Hallmark Charge', fmt_currency(bill.hallmark_charge)])
    totals_data.append(['Subtotal', fmt_currency(bill.subtotal or bill.total)])
    totals_data.append([f'CGST (1.5%)', fmt_currency(bill.cgst or 0)])
    totals_data.append([f'SGST (1.5%)', fmt_currency(bill.sgst or 0)])
    if bill.discount:
        totals_data.append(['Discount', f'-{fmt_currency(bill.discount)}'])
    if bill.old_item_value:
        totals_data.append([f'Exchange ({(bill.old_item_type or "").upper()})', f'-{fmt_currency(bill.old_item_value)}'])
    totals_data.append(['GRAND TOTAL', fmt_currency(bill.total)])
    
    if bill.amount_paid and bill.amount_paid != bill.total:
        totals_data.append(['Amount Paid', fmt_currency(bill.amount_paid)])
        totals_data.append(['Balance Due', fmt_currency(bill.balance)])
    
    # Build totals + payment/signature in a two-column layout
    payment_text = f'<b>Payment Mode:</b> {bill.payment_mode}<br/><b>Status:</b> {bill.status.upper()}'
    
    totals_tbl = Table(totals_data, colWidths=[52*mm, 28*mm])
    totals_style = [
        ('FONTNAME', (0, 0), (-1, -1), f_body),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('ALIGN', (0, 0), (0, -1), 'LEFT'),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('TOPPADDING', (0, 0), (-1, -1), 1),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
    ]
    # Bold the grand total row
    gt_idx = len(totals_data) - 1
    if bill.amount_paid and bill.amount_paid != bill.total:
        gt_idx = len(totals_data) - 3
    
    totals_style.append(('FONTNAME', (0, gt_idx), (-1, gt_idx), f_bold))
    totals_style.append(('FONTSIZE', (0, gt_idx), (-1, gt_idx), 10))
    totals_style.append(('LINEABOVE', (0, gt_idx), (-1, gt_idx), 1.5, gold_color))
    totals_style.append(('LINEBELOW', (0, gt_idx), (-1, gt_idx), 1.5, gold_color))
    totals_style.append(('TOPPADDING', (0, gt_idx), (-1, gt_idx), 3))
    totals_style.append(('BOTTOMPADDING', (0, gt_idx), (-1, gt_idx), 3))
    totals_tbl.setStyle(TableStyle(totals_style))
    
    # Combine payment info + totals/signature in two columns
    right_column_flowables = [totals_tbl]

    sig_label_style = ParagraphStyle('SigLabel', parent=styles['Normal'],
                                     fontSize=6.5, textColor=colors.HexColor('#333333'),
                                     alignment=TA_CENTER, fontName=f_bold)
    right_column_flowables.append(Spacer(1, 4*mm))

    # Use real signature image if available — sits directly on the line (no gap)
    sig_img_path = os.path.join(os.path.dirname(__file__), 'static', 'signature_clean.png')
    if os.path.exists(sig_img_path):
        sig_img = Image(sig_img_path, width=42*mm, height=12*mm)
        sig_tbl = Table([
            [sig_img],
            [HRFlowable(width=45*mm, thickness=0.8, color=colors.HexColor('#000000'))],
            [Paragraph('AUTHORIZED SIGNATURE', sig_label_style)]
        ], colWidths=[45*mm], rowHeights=[12*mm, 2, None], hAlign='RIGHT')
        sig_tbl.setStyle(TableStyle([
            ('TOPPADDING',    (0, 0), (0, 0), 0),
            ('BOTTOMPADDING', (0, 0), (0, 0), -6),
            ('TOPPADDING',    (0, 1), (0, 1), 0),
            ('BOTTOMPADDING', (0, 1), (0, 1), 0),
            ('TOPPADDING',    (0, 2), (0, 2), 2),
            ('ALIGN', (0, 0), (-1, -1), 'RIGHT'),
        ]))
    else:
        sig_tbl = Table([
            [Spacer(1, 12*mm)],
            [HRFlowable(width=45*mm, thickness=0.8, color=colors.HexColor('#000000'))],
            [Paragraph('AUTHORIZED SIGNATURE', sig_label_style)]
        ], colWidths=[45*mm], hAlign='RIGHT')
    right_column_flowables.append(sig_tbl)
 
    bottom_data = [[
        Paragraph(payment_text, style_normal),
        right_column_flowables
    ]]
    bottom_tbl = Table(bottom_data, colWidths=[100*mm, 90*mm])
    bottom_tbl.setStyle(TableStyle([
        ('VALIGN', (0, 0), (0, 0), 'BOTTOM'),
        ('VALIGN', (1, 0), (1, 0), 'TOP'),
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ('LINELEFT', (1, 0), (1, 0), 1.5, gold_color),
        ('LEFTPADDING', (1, 0), (1, 0), 10*mm),
    ]))
    elements.append(bottom_tbl)
    elements.append(Spacer(1, 2*mm))
    
    # -- Footer --
    elements.append(HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#eeeeee')))
    elements.append(Spacer(1, 2*mm))
    elements.append(Paragraph('Thank you for choosing Laxmi Srinivasa Jewellery • Prestige • Quality • Integrity', style_footer))
    
    # Canvas Drawing function to add background border & watermark
    def draw_background(canvas, doc):
        canvas.saveState()
        # Gold border (outer box)
        canvas.setStrokeColor(colors.HexColor('#c5a96e'))
        canvas.setLineWidth(1.5)
        # Margin outlines (slightly inset from page edges)
        rect_width = doc.pagesize[0] - 8*mm
        rect_height = doc.pagesize[1] - 8*mm
        canvas.rect(4*mm, 4*mm, rect_width, rect_height, fill=0, stroke=1)
        
        # Watermark "LSJ" — matches HTML: centered, -15 deg tilt
        canvas.setFont('Times-Bold', 120)
        canvas.setFillColor(colors.HexColor('#000000'), alpha=0.07)

        canvas.saveState()
        canvas.translate(doc.pagesize[0] / 2, doc.pagesize[1] / 2)
        canvas.rotate(-15)
        canvas.drawCentredString(0, 0, 'LSJ')
        canvas.restoreState()
        
        canvas.restoreState()
        
    doc.build(elements, onFirstPage=draw_background)
    buf.seek(0)
    return buf.getvalue()


@app.route('/bill/<int:bill_id>/pdf')
def bill_pdf(bill_id):
    """Generate and return invoice PDF for sharing"""
    bill = Bill.query.get_or_404(bill_id)
    pdf_bytes = generate_bill_pdf_bytes(bill)
    buf = io.BytesIO(pdf_bytes)
    filename = f'Invoice_{bill.bill_number or "LSJ-" + str(bill.id)}.pdf'
    return send_file(buf, mimetype='application/pdf', download_name=filename, as_attachment=False)


def send_bill_email(bill, recipient_email):
    """Send bill PDF via SMTP mail with the actual PDF attached"""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.base import MIMEBase
    from email import encoders
    
    smtp_server = os.getenv('SMTP_SERVER')
    smtp_port = os.getenv('SMTP_PORT', '587')
    smtp_user = os.getenv('SMTP_USER')
    smtp_password = os.getenv('SMTP_PASSWORD')
    smtp_sender = os.getenv('SMTP_SENDER', smtp_user)
    
    if not smtp_server or not smtp_user or not smtp_password:
        return False, "SMTP not configured. Please set SMTP_SERVER, SMTP_USER, and SMTP_PASSWORD in your .env file."
    
    # Validate that the password looks like a Gmail App Password (16 chars, no special chars)
    # Regular Gmail passwords won't work with SMTP - need App Password from Google Account
    clean_pwd = smtp_password.strip().replace(' ', '')
    if smtp_server == 'smtp.gmail.com' and (len(clean_pwd) != 16 or not clean_pwd.isalpha()):
        return False, ("Gmail requires a 16-character App Password (not your regular Gmail password). "
                       "Go to Google Account → Security → 2-Step Verification → App Passwords, "
                       "generate one, and put it in your .env file as SMTP_PASSWORD.")
    
    try:
        msg = MIMEMultipart()
        msg['From'] = smtp_sender
        msg['To'] = recipient_email
        msg['Subject'] = f"Tax Invoice {bill.bill_number or 'LSJ-' + str(bill.id)} - Laxmi Srinivasa Jewellery"
        
        body = f"""Dear {bill.customer_name},

Please find attached your Tax Invoice ({bill.bill_number or 'LSJ-' + str(bill.id)}) in PDF format from Laxmi Srinivasa Jewellery.

Thank you for your patronage!

Best Regards,
Laxmi Srinivasa Jewellery
Annavaram - 533406
"""
        msg.attach(MIMEText(body, 'plain'))
        
        # Generate and attach PDF
        pdf_bytes = generate_bill_pdf_bytes(bill)
        
        part = MIMEBase('application', 'octet-stream')
        part.set_payload(pdf_bytes)
        encoders.encode_base64(part)
        
        filename = f"Invoice_{bill.bill_number or 'LSJ-' + str(bill.id)}.pdf"
        part.add_header('Content-Disposition', f'attachment; filename="{filename}"')
        msg.attach(part)
        
        # Connect and send
        server = smtplib.SMTP(smtp_server, int(smtp_port))
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_sender, recipient_email, msg.as_string())
        server.quit()
        
        return True, "Email sent successfully with the actual PDF attached!"
    except Exception as e:
        return False, f"Failed to send email: {str(e)}"


@app.route('/api/bill/<int:bill_id>/share/email', methods=['POST'])
def share_bill_email(bill_id):
    """API endpoint to send bill PDF via email attachment"""
    bill = Bill.query.get_or_404(bill_id)
    data = request.json or {}
    recipient_email = data.get('email', '').strip()
    
    if not recipient_email:
        return jsonify({'success': False, 'message': 'Recipient email address is required.'}), 400
        
    success, message = send_bill_email(bill, recipient_email)
    if success:
        return jsonify({'success': True, 'message': message})
    else:
        return jsonify({'success': False, 'message': message})


if __name__ == '__main__':
    # Open browser in new tab automatically
    threading.Thread(target=open_browser, daemon=True).start()
    app.run(debug=True, use_reloader=False)
