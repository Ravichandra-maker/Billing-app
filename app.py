from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, send_file
from models import db, Bill, InventoryItem, BillItem
from datetime import datetime
import calendar
import os
import io
import webbrowser
import threading
import requests
from dotenv import load_dotenv
from reportlab.lib.pagesizes import landscape, A5
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'lakshmi_srinivasa_jewellery_2026')

# Database configuration

database_url = os.getenv('DATABASE_URL')

if database_url:
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
else:
    basedir = os.path.abspath(os.path.dirname(__file__))
    DATA_DIR = os.path.join(basedir, "databases")
    os.makedirs(DATA_DIR, exist_ok=True)
    db_path = os.path.join(DATA_DIR, "billing.db")
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

with app.app_context():
    db.create_all()
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
    page_w, page_h = 200 * mm, 150 * mm
    doc = SimpleDocTemplate(buf, pagesize=(page_w, page_h),
                            leftMargin=10*mm, rightMargin=10*mm,
                            topMargin=8*mm, bottomMargin=6*mm)
    
    styles = getSampleStyleSheet()
    
    # Custom styles
    style_title = ParagraphStyle('InvTitle', parent=styles['Heading1'],
                                  fontSize=14, textColor=colors.HexColor('#000000'),
                                  alignment=TA_LEFT, spaceAfter=2)
    style_subtitle = ParagraphStyle('InvSub', parent=styles['Normal'],
                                     fontSize=8, textColor=colors.HexColor('#444444'),
                                     alignment=TA_LEFT, spaceAfter=4)
    style_meta = ParagraphStyle('InvMeta', parent=styles['Normal'],
                                 fontSize=9, textColor=colors.HexColor('#111111'),
                                 alignment=TA_RIGHT, spaceAfter=2)
    style_normal = ParagraphStyle('InvNormal', parent=styles['Normal'],
                                   fontSize=9, textColor=colors.HexColor('#111111'))
    style_bold = ParagraphStyle('InvBold', parent=styles['Normal'],
                                 fontSize=9, textColor=colors.HexColor('#000000'),
                                 fontName='Helvetica-Bold')
    style_footer = ParagraphStyle('InvFooter', parent=styles['Normal'],
                                   fontSize=8, textColor=colors.HexColor('#555555'),
                                   alignment=TA_CENTER, fontName='Helvetica-Oblique')
    style_grand = ParagraphStyle('InvGrand', parent=styles['Normal'],
                                  fontSize=11, textColor=colors.HexColor('#000000'),
                                  fontName='Helvetica-Bold')
    
    elements = []
    gold_color = colors.HexColor('#c5a96e')
    
    # -- Header table (brand left, meta right) --
    header_data = [[
        Paragraph('<b>Laxmi Srinivasa Jewellery</b><br/><font size="7" color="#444">Annavaram - 533406 | GSTIN: 37CKJPK2161B1ZG</font>', style_title),
        Paragraph(f'<font size="8" color="#8b6914"><b>TAX INVOICE</b></font><br/>'
                  f'<font size="9"><b>Invoice:</b> {bill.bill_number or "LSJ-" + str(bill.id)}</font><br/>'
                  f'<font size="9"><b>Date:</b> {bill.date}</font>', style_meta)
    ]]
    header_tbl = Table(header_data, colWidths=[105*mm, 75*mm])
    header_tbl.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LINEBELOW', (0, 0), (-1, 0), 1.5, gold_color),
    ]))
    elements.append(header_tbl)
    elements.append(Spacer(1, 4*mm))
    
    # -- Bill To --
    addr = f', {bill.address}' if bill.address else ''
    cust_text = f'<b>BILL TO:</b> <b>{bill.customer_name}</b>{addr} | Ph: {bill.phone}'
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
            f'Rs.{irate:,.2f}', f'Rs.{iamt:,.2f}'
        ])
    
    col_widths = [30*mm, 12*mm, 16*mm, 14*mm, 18*mm, 18*mm, 18*mm, 22*mm, 24*mm]
    items_tbl = Table(items_data, colWidths=col_widths)
    items_tbl.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#faf7f2')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#333333')),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('ALIGN', (0, 0), (0, -1), 'LEFT'),
        ('ALIGN', (1, 0), (3, -1), 'CENTER'),
        ('ALIGN', (4, 0), (-1, -1), 'RIGHT'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cccccc')),
        ('BOX', (0, 0), (-1, 0), 1, gold_color),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
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
    totals_data.append(['Total Metal Value', f'Rs.{total_metal:,.2f}'])
    totals_data.append(['Total Making Charges', f'Rs.{total_making:,.2f}'])
    totals_data.append(['Total Wastage', f'Rs.{total_wastage:,.2f}'])
    if bill.stone_charge:
        totals_data.append(['Stone/Setting Charge', f'Rs.{bill.stone_charge:,.2f}'])
    if bill.hallmark_charge:
        totals_data.append(['Hallmark Charge', f'Rs.{bill.hallmark_charge:,.2f}'])
    totals_data.append(['Subtotal', f'Rs.{(bill.subtotal or bill.total):,.2f}'])
    totals_data.append([f'CGST (1.5%)', f'Rs.{(bill.cgst or 0):,.2f}'])
    totals_data.append([f'SGST (1.5%)', f'Rs.{(bill.sgst or 0):,.2f}'])
    if bill.discount:
        totals_data.append(['Discount', f'-Rs.{bill.discount:,.2f}'])
    if bill.old_item_value:
        totals_data.append([f'Exchange ({(bill.old_item_type or "").upper()})', f'-Rs.{bill.old_item_value:,.2f}'])
    totals_data.append(['GRAND TOTAL', f'Rs.{bill.total:,.2f}'])
    
    if bill.amount_paid and bill.amount_paid != bill.total:
        totals_data.append(['Amount Paid', f'Rs.{bill.amount_paid:,.2f}'])
        totals_data.append(['Balance Due', f'Rs.{bill.balance:,.2f}'])
    
    # Build totals + payment/signature in a two-column layout
    payment_text = f'<b>Payment Mode:</b> {bill.payment_mode}<br/><b>Status:</b> {bill.status.upper()}'
    
    totals_tbl = Table(totals_data, colWidths=[45*mm, 30*mm])
    totals_style = [
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
    totals_style.append(('FONTNAME', (0, gt_idx), (-1, gt_idx), 'Helvetica-Bold'))
    totals_style.append(('FONTSIZE', (0, gt_idx), (-1, gt_idx), 10))
    totals_style.append(('LINEABOVE', (0, gt_idx), (-1, gt_idx), 1, gold_color))
    totals_style.append(('BACKGROUND', (0, gt_idx), (-1, gt_idx), colors.HexColor('#faf7f2')))
    totals_tbl.setStyle(TableStyle(totals_style))
    
    # Combine payment info + totals/signature in two columns
    sig_img_path = 'static/signature_clean.png'
    right_column_flowables = [totals_tbl]
    
    if os.path.exists(sig_img_path):
        # Insert transparent clean signature image - sized to match the signature line width
        sig_img = Image(sig_img_path, width=45*mm, height=12*mm)
        sig_img.hAlign = 'RIGHT'
        
        sig_label_style = ParagraphStyle('SigLabel', parent=styles['Normal'],
                                         fontSize=6.5, textColor=colors.HexColor('#333333'),
                                         alignment=TA_RIGHT, fontName='Helvetica-Bold')
        
        right_column_flowables.append(Spacer(1, 1*mm))
        right_column_flowables.append(sig_img)
        right_column_flowables.append(HRFlowable(width=45*mm, thickness=0.8, color=colors.HexColor('#000000'), hAlign='RIGHT', spaceBefore=0.5, spaceAfter=0.5))
        right_column_flowables.append(Paragraph('AUTHORIZED SIGNATURE', sig_label_style))
    else:
        sig_label_style = ParagraphStyle('SigLabel', parent=styles['Normal'],
                                         fontSize=6.5, textColor=colors.HexColor('#333333'),
                                         alignment=TA_RIGHT, fontName='Helvetica-Bold')
        right_column_flowables.append(Spacer(1, 8*mm))
        right_column_flowables.append(HRFlowable(width='80', thickness=0.8, color=colors.HexColor('#000000'), hAlign='RIGHT', spaceAfter=1))
        right_column_flowables.append(Paragraph('AUTHORIZED SIGNATURE', sig_label_style))

    bottom_data = [[
        Paragraph(payment_text, style_normal),
        right_column_flowables
    ]]
    bottom_tbl = Table(bottom_data, colWidths=[95*mm, 80*mm])
    bottom_tbl.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
    ]))
    elements.append(bottom_tbl)
    elements.append(Spacer(1, 2*mm))
    
    # -- Footer --
    elements.append(HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#eeeeee')))
    elements.append(Spacer(1, 2*mm))
    elements.append(Paragraph('Thank you for choosing Laxmi Srinivasa Jewellery • Prestige • Quality • Integrity', style_footer))
    
    doc.build(elements)
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
