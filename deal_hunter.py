import requests
import time
import json
import os
import re
import hashlib
import sqlite3
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
#   DEAL HUNTER ELITE V6 — STABLE
#   Price Memory + Velocity + Arbitrage + Categories
# ============================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT", "")
SERPAPI_KEY    = os.environ.get("SERPAPI_KEY", "")

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT or not SERPAPI_KEY:
    print("ERREUR: Variables d'environnement manquantes!")
    exit(1)

SCAN_INTERVAL  = 300
MIN_PRICE      = 3
MAX_PRICE      = 2000
MAX_WORKERS    = 6
DB_FILE        = "price_memory.db"
MAX_ALERTS     = 25

ANOMALY_ERROR  = 0.50
ANOMALY_HOT    = 0.35
ANOMALY_GOOD   = 0.25
VELOCITY_ALERT = 0.20

CATEGORIES = {
    "tv":          {"keywords": ["tv","television","qled","oled","4k","monitor"],              "min_savings": 80},
    "laptop":      {"keywords": ["laptop","notebook","macbook","chromebook"],                   "min_savings": 100},
    "phone":       {"keywords": ["iphone","samsung galaxy","pixel","smartphone"],               "min_savings": 80},
    "appliance":   {"keywords": ["washer","dryer","fridge","dishwasher","microwave","vacuum"],  "min_savings": 60},
    "gaming":      {"keywords": ["ps5","xbox","nintendo","playstation","gaming"],               "min_savings": 30},
    "tool":        {"keywords": ["drill","saw","wrench","dewalt","makita","ryobi"],             "min_savings": 25},
    "furniture":   {"keywords": ["sofa","couch","desk","chair","table","shelf"],                "min_savings": 50},
    "toy":         {"keywords": ["toy","lego","puzzle","board game","playset"],                 "min_savings": 8},
    "clothing":    {"keywords": ["shirt","pants","jacket","shoes","boots","dress"],             "min_savings": 10},
    "beauty":      {"keywords": ["serum","moisturizer","shampoo","conditioner","makeup"],       "min_savings": 5},
    "baby":        {"keywords": ["baby","infant","diaper","wipes","stroller"],                  "min_savings": 8},
    "electronics": {"keywords": ["headphone","earbuds","speaker","camera","tablet"],            "min_savings": 15},
    "default":     {"keywords": [],                                                              "min_savings": 8},
}

TRUSTED_CA = [
    "walmart","canadian tire","best buy","staples","home depot","rona",
    "sport chek","london drugs","shoppers","costco","mec","sail",
    "bureau en gros","winners","homesense","pharmaprix","superstore",
    "loblaws","metro","maxi","dollarama","giant tiger","structube","simons",
]
TRUSTED_US = [
    "target","walmart","home depot","lowes","best buy","staples",
    "walgreens","cvs","costco","kohls","macys","overstock","wayfair",
    "chewy","petco","rei","tj maxx","marshalls",
]
BLOCKED = ["ebay","aliexpress","temu","wish","alibaba","shein","banggood","dhgate","etsy"]

SEARCHES_CA = [
    "price error listing canada","erreur de prix canada",
    "clearance -80% canada","clearance -70% canada","clearance -60% canada",
    "liquidation -50% canada","clearance sale site:walmart.ca",
    "clearance site:canadiantire.ca","open box site:bestbuy.ca",
    "electronics clearance canada","laptop clearance canada",
    "tv clearance canada","appliances clearance canada",
    "furniture clearance canada","toys clearance canada",
    "clothing clearance canada","tools clearance canada",
    "sports clearance canada","baby clearance canada",
    "beauty clearance canada","gaming clearance canada",
]
SEARCHES_US = [
    "price error listing usa","clearance -80%","clearance -70%",
    "target clearance","walmart clearance rollback",
    "home depot clearance","best buy clearance","lowes clearance",
    "electronics clearance","tv clearance","furniture clearance",
    "toys clearance","clothing clearance","tools clearance",
]
WALMART_CA = [
    "clearance","liquidation","electronics clearance",
    "furniture clearance","toys clearance","clothing clearance",
    "tools clearance","sports clearance","baby clearance",
]
WALMART_US = [
    "clearance rollback","electronics clearance",
    "toys clearance","clothing clearance","tools clearance",
]

# ============================================================
#   UTILITAIRES
# ============================================================

def normalize_name(name):
    name = name.lower()
    name = re.sub(r'[^a-z0-9 ]', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()
    name = name.replace(" go ", " gb ").replace("giga", "gb")
    stop = {"the","a","an","and","or","for","with","in","of","to","by",
            "new","sale","deal","clearance","canada","free","shipping"}
    words = [w for w in name.split() if w not in stop and len(w) > 1]
    return " ".join(words[:8])

def make_product_id(name, store):
    raw = f"{normalize_name(name)}_{store.lower().strip()}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]

def make_product_key(name):
    return hashlib.md5(normalize_name(name).encode()).hexdigest()[:12]

def detect_category(name):
    name_lower = name.lower()
    for cat, info in CATEGORIES.items():
        if cat == "default": continue
        if any(kw in name_lower for kw in info["keywords"]):
            return cat
    return "default"

def get_min_savings(name):
    cat = detect_category(name)
    return CATEGORIES[cat]["min_savings"], cat

# ============================================================
#   BASE DE DONNÉES
# ============================================================

def init_db():
    try:
        conn = sqlite3.connect(DB_FILE)
        c    = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS prices (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id  TEXT NOT NULL,
                product_key TEXT,
                name        TEXT,
                store       TEXT,
                category    TEXT,
                price       REAL,
                original    REAL,
                link        TEXT,
                market      TEXT,
                timestamp   TEXT
            )
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_pid ON prices(product_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_pkey ON prices(product_key)')
        c.execute('''
            CREATE TABLE IF NOT EXISTS alerts_sent (
                product_id  TEXT PRIMARY KEY,
                last_price  REAL,
                last_alert  TEXT,
                times_sent  INTEGER DEFAULT 0
            )
        ''')
        conn.commit()
        conn.close()
        print("DB initialisée")
    except Exception as e:
        print(f"DB erreur init: {e}")

def save_price(product_id, product_key, name, store, category, price, original, link, market):
    try:
        conn = sqlite3.connect(DB_FILE)
        c    = conn.cursor()
        c.execute('''
            INSERT INTO prices (product_id, product_key, name, store, category, price, original, link, market, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (product_id, product_key, name[:100], store[:100], category,
              price, original, link[:500], market, datetime.now().isoformat()))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"DB save erreur: {e}")

def get_price_stats(product_id):
    try:
        conn = sqlite3.connect(DB_FILE)
        c    = conn.cursor()
        d30  = (datetime.now() - timedelta(days=30)).isoformat()
        d7   = (datetime.now() - timedelta(days=7)).isoformat()

        c.execute('''
            SELECT AVG(price), MIN(price), MAX(price), COUNT(*)
            FROM prices WHERE product_id = ? AND timestamp > ?
        ''', (product_id, d30))
        row30 = c.fetchone()

        c.execute('SELECT AVG(price) FROM prices WHERE product_id = ? AND timestamp > ?', (product_id, d7))
        row7 = c.fetchone()

        # Historique récent pour velocity
        c.execute('''
            SELECT price FROM prices WHERE product_id = ?
            ORDER BY timestamp DESC LIMIT 5
        ''', (product_id,))
        history = [r[0] for r in c.fetchall()]

        conn.close()

        if not row30 or not row30[3]:
            return None

        return {
            "avg_30d":       row30[0] or 0,
            "min_30d":       row30[1] or 0,
            "max_30d":       row30[2] or 0,
            "count":         row30[3] or 0,
            "avg_7d":        row7[0] if row7 and row7[0] else row30[0],
            "price_history": history,
        }
    except Exception as e:
        print(f"DB stats erreur: {e}")
        return None

def get_cross_site_prices(product_key, exclude_store):
    try:
        conn = sqlite3.connect(DB_FILE)
        c    = conn.cursor()
        d7   = (datetime.now() - timedelta(days=7)).isoformat()
        c.execute('''
            SELECT store, MIN(price), link
            FROM prices
            WHERE product_key = ? AND timestamp > ? AND store != ?
            GROUP BY store ORDER BY price ASC
        ''', (product_key, d7, exclude_store))
        rows = c.fetchall()
        conn.close()
        return [{"store": r[0], "price": r[1], "link": r[2]} for r in rows]
    except:
        return []

def should_alert(product_id, current_price):
    try:
        conn = sqlite3.connect(DB_FILE)
        c    = conn.cursor()
        c.execute('SELECT last_price, last_alert FROM alerts_sent WHERE product_id = ?', (product_id,))
        row  = c.fetchone()
        conn.close()
        if not row: return True
        last_price, last_alert = row
        if last_price and current_price < last_price * 0.85: return True
        try:
            if datetime.now() - datetime.fromisoformat(last_alert) > timedelta(hours=24): return True
        except: pass
        return False
    except:
        return True

def mark_alerted(product_id, price):
    try:
        conn = sqlite3.connect(DB_FILE)
        c    = conn.cursor()
        # Compatible avec toutes les versions SQLite
        c.execute('SELECT product_id FROM alerts_sent WHERE product_id = ?', (product_id,))
        exists = c.fetchone()
        if exists:
            c.execute('''
                UPDATE alerts_sent SET last_price=?, last_alert=?, times_sent=times_sent+1
                WHERE product_id=?
            ''', (price, datetime.now().isoformat(), product_id))
        else:
            c.execute('''
                INSERT INTO alerts_sent (product_id, last_price, last_alert, times_sent)
                VALUES (?, ?, ?, 1)
            ''', (product_id, price, datetime.now().isoformat()))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"DB alert erreur: {e}")

def cleanup_old_data():
    try:
        conn = sqlite3.connect(DB_FILE)
        c    = conn.cursor()
        d60  = (datetime.now() - timedelta(days=60)).isoformat()
        c.execute('DELETE FROM prices WHERE timestamp < ?', (d60,))
        conn.commit()
        conn.close()
    except: pass

def get_db_stats():
    try:
        conn = sqlite3.connect(DB_FILE)
        c    = conn.cursor()
        c.execute('SELECT COUNT(DISTINCT product_id), COUNT(*) FROM prices')
        row = c.fetchone()
        conn.close()
        return row[0] or 0, row[1] or 0
    except:
        return 0, 0

# ============================================================
#   DÉTECTION
# ============================================================

def detect_velocity(price_history, current_price):
    if not price_history or len(price_history) < 2:
        return 0, False
    prev = price_history[1] if len(price_history) > 1 else price_history[0]
    if prev <= 0: return 0, False
    velocity = (prev - current_price) / prev
    return round(velocity, 3), velocity >= VELOCITY_ALERT

def detect_anomaly(product_id, current_price, original_price, stats):
    anomaly_type = None
    drop_vs_avg  = 0
    drop_vs_orig = 0
    baseline     = None
    velocity     = 0
    is_fast_drop = False

    if original_price and original_price > current_price:
        drop_vs_orig = (original_price - current_price) / original_price

    if stats and stats.get("price_history"):
        velocity, is_fast_drop = detect_velocity(stats["price_history"], current_price)

    if stats and stats["count"] >= 3:
        avg      = stats["avg_30d"]
        baseline = avg
        if avg > current_price:
            drop_vs_avg = (avg - current_price) / avg
            if drop_vs_avg >= ANOMALY_ERROR:    anomaly_type = "PRICE_ERROR"
            elif drop_vs_avg >= ANOMALY_HOT:    anomaly_type = "HOT_DEAL"
            elif drop_vs_avg >= ANOMALY_GOOD:   anomaly_type = "GOOD_DEAL"
        if current_price <= stats["min_30d"] * 0.90 and not anomaly_type:
            anomaly_type = "ALL_TIME_LOW"
        if is_fast_drop and not anomaly_type:
            anomaly_type = "FLASH_SALE"
    else:
        baseline = original_price
        if drop_vs_orig >= 0.60:   anomaly_type = "PRICE_ERROR"
        elif drop_vs_orig >= 0.40: anomaly_type = "HOT_DEAL"
        elif drop_vs_orig >= 0.30: anomaly_type = "GOOD_DEAL"

    return anomaly_type, drop_vs_avg, drop_vs_orig, baseline, velocity, is_fast_drop

def detect_arbitrage(product_key, current_store, current_price):
    others = get_cross_site_prices(product_key, current_store)
    opps   = []
    for o in others:
        if o["price"] > current_price * 1.30:
            spread = (o["price"] - current_price) / o["price"]
            opps.append({"store": o["store"], "price": o["price"], "spread": spread})
    opps.sort(key=lambda x: x["spread"], reverse=True)
    if not opps: return None, []
    if opps[0]["spread"] >= 0.50: return "ARBITRAGE_HIGH", opps
    if opps[0]["spread"] >= 0.30: return "ARBITRAGE_LOW", opps
    return None, []

def calculate_score(anomaly_type, drop_vs_avg, drop_vs_orig, price,
                    original, is_canada, stats, velocity, is_fast_drop,
                    arb_type, arb_opps, multi_site=False):
    score, reasons = 0, []

    type_scores = {
        "PRICE_ERROR": 6, "HOT_DEAL": 4, "ARBITRAGE_HIGH": 4,
        "ALL_TIME_LOW": 3, "ARBITRAGE_LOW": 3, "FLASH_SALE": 3, "GOOD_DEAL": 2,
    }
    labels = {
        "PRICE_ERROR": "💣 ERREUR DE PRIX", "HOT_DEAL": "🔥 HOT DEAL",
        "ARBITRAGE_HIGH": "🔄 ARBITRAGE", "ALL_TIME_LOW": "📉 PRIX LE PLUS BAS",
        "ARBITRAGE_LOW": "🔄 ARBITRAGE", "FLASH_SALE": "⚡ FLASH SALE",
        "GOOD_DEAL": "✅ BON DEAL",
    }

    if anomaly_type:
        score += type_scores.get(anomaly_type, 0)
        reasons.append(labels.get(anomaly_type, anomaly_type))

    if drop_vs_avg >= 0.70:   score += 4; reasons.append(f"📊 -{drop_vs_avg:.0%} vs moy")
    elif drop_vs_avg >= 0.50: score += 3; reasons.append(f"📊 -{drop_vs_avg:.0%} vs moy")
    elif drop_vs_avg >= 0.35: score += 2; reasons.append(f"📊 -{drop_vs_avg:.0%} vs moy")
    elif drop_vs_avg >= 0.25: score += 1; reasons.append(f"📊 -{drop_vs_avg:.0%} vs moy")

    if drop_vs_orig >= 0.70:   score += 3; reasons.append(f"💸 -{drop_vs_orig:.0%}")
    elif drop_vs_orig >= 0.50: score += 2; reasons.append(f"💸 -{drop_vs_orig:.0%}")
    elif drop_vs_orig >= 0.30: score += 1; reasons.append(f"💸 -{drop_vs_orig:.0%}")

    savings = (original or 0) - price
    if savings >= 500:   score += 4; reasons.append(f"💰 -${savings:.0f}")
    elif savings >= 200: score += 3; reasons.append(f"💰 -${savings:.0f}")
    elif savings >= 100: score += 2; reasons.append(f"💰 -${savings:.0f}")
    elif savings >= 30:  score += 1; reasons.append(f"💰 -${savings:.0f}")

    if is_fast_drop: score += 2; reasons.append(f"⚡ -{velocity:.0%} rapide")
    if stats and stats["count"] >= 20: score += 2; reasons.append(f"✅ {stats['count']} obs")
    elif stats and stats["count"] >= 5: score += 1

    if arb_opps:
        best = arb_opps[0]
        score += 2; reasons.append(f"🔄 vs {best['store']} ${best['price']:.2f}")

    score += 2 if is_canada else 1
    reasons.append("🇨🇦" if is_canada else "🇺🇸")
    if multi_site: score += 2; reasons.append("✅ multi-site")

    return score, " | ".join(reasons)

# ============================================================
#   TELEGRAM
# ============================================================

def send_telegram(msg):
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": msg,
                  "parse_mode": "HTML", "disable_web_page_preview": False},
            timeout=15
        )
        return resp.status_code == 200
    except: return False

def format_deal(deal):
    atype     = deal["anomaly_type"]
    score     = deal["score"]
    name      = deal["name"][:65]
    price     = deal["price"]
    original  = deal.get("original_price", 0)
    baseline  = deal.get("baseline", original or price)
    savings   = max((original or baseline or price) - price, 0)
    stats     = deal.get("stats")
    drop_avg  = deal.get("drop_vs_avg", 0)
    velocity  = deal.get("velocity", 0)
    fast_drop = deal.get("is_fast_drop", False)
    arb_opps  = deal.get("arbitrage_opps", [])
    multi     = deal.get("multi_site", False)
    cat       = deal.get("category", "")

    labels = {
        "PRICE_ERROR": ("💣","ERREUR DE PRIX"), "HOT_DEAL": ("🔥","HOT DEAL"),
        "ARBITRAGE_HIGH": ("🔄","ARBITRAGE"), "ALL_TIME_LOW": ("📉","PRIX LE PLUS BAS"),
        "ARBITRAGE_LOW": ("🔄","ARBITRAGE"), "FLASH_SALE": ("⚡","FLASH SALE"),
        "GOOD_DEAL": ("✅","BON DEAL"),
    }
    emoji, tag = labels.get(atype, ("📊","DEAL"))

    hist_line = ""
    if stats and stats["count"] >= 3:
        hist_line = (
            f"\n\n📊 <b>Historique ({stats['count']} obs):</b>\n"
            f"   Moyenne 30j: ${stats['avg_30d']:.2f}\n"
            f"   Drop vs moy: <b>-{drop_avg:.0%}</b>"
        )

    arb_line = ""
    if arb_opps:
        arb_line = "\n\n🔄 <b>Comparaison:</b>"
        for a in arb_opps[:2]:
            arb_line += f"\n   {a['store']}: ${a['price']:.2f} (+{a['spread']:.0%})"

    extras = ""
    if fast_drop: extras += f"\n⚡ <b>Chute rapide: -{velocity:.0%}!</b>"
    if multi:     extras += "\n✅ <b>Multi-site confirmé!</b>"
    if cat and cat != "default": extras += f"\n📦 {cat}"

    return (
        f"{emoji} <b>{tag}</b> — Score {score}\n\n"
        f"📦 <b>{name}</b>\n\n"
        f"💰 Prix: <b>${price:.2f}</b>\n"
        f"📉 Baseline: <s>${baseline:.2f}</s>\n"
        f"💸 Économie: <b>${savings:.2f}</b>"
        f"{extras}"
        f"{hist_line}"
        f"{arb_line}\n\n"
        f"🏪 {deal['store']} {deal['market']}\n"
        f"📡 {deal.get('source','')}\n\n"
        f"💡 <i>{deal['reason']}</i>\n\n"
        f"🔗 <a href='{deal['link']}'>Voir le deal →</a>\n"
        f"⏰ {datetime.now().strftime('%H:%M:%S')}"
    )

# ============================================================
#   SCRAPERS
# ============================================================

def scrape_google(query, market="CA"):
    gl     = "ca" if market == "CA" else "us"
    domain = "google.ca" if market == "CA" else "google.com"
    params = {
        "api_key": SERPAPI_KEY, "engine": "google_shopping",
        "q": query, "gl": gl, "hl": "en",
        "google_domain": domain, "num": 20, "sort_by": "1",
    }
    results = []
    try:
        resp = requests.get("https://serpapi.com/search", params=params, timeout=30)
        if resp.status_code != 200: return results
        for r in resp.json().get("shopping_results", []):
            price = r.get("extracted_price", 0)
            if not price:
                try: price = float(re.sub(r'[^\d.]','',str(r.get("price",""))))
                except: price = 0
            original = 0
            for field in ["old_price","was_price","original_price","list_price"]:
                val = r.get(field,"")
                if val:
                    try:
                        original = float(re.sub(r'[^\d.]','',str(val)))
                        if original > price: break
                    except: pass
            name  = r.get("title","")
            link  = r.get("link","") or r.get("product_link","")
            store = r.get("source", r.get("seller",""))
            if not name or not link or not store: continue
            if "google." in link.lower(): continue
            if any(b in store.lower() for b in BLOCKED): continue
            if not name or len(name) < 3: continue
            if not (MIN_PRICE <= (price or 0) <= MAX_PRICE): continue
            is_ca = any(t in store.lower() for t in TRUSTED_CA)
            is_us = any(t in store.lower() for t in TRUSTED_US)
            if not is_ca and not is_us: continue
            results.append({
                "name": name, "price": price, "original": original,
                "link": link, "store": store,
                "market": "CA" if is_ca else "US",
                "source": f"Google Shopping {market}",
            })
    except Exception as e:
        print(f"Google erreur [{query[:20]}]: {e}")
    return results

def scrape_walmart(query, market="CA"):
    params = {"api_key": SERPAPI_KEY, "engine": "walmart", "query": query, "ps": "40"}
    results = []
    try:
        resp = requests.get("https://serpapi.com/search", params=params, timeout=30)
        if resp.status_code != 200: return results
        for r in resp.json().get("organic_results", []):
            price = 0
            pm    = r.get("primary_offer",{})
            if isinstance(pm, dict): price = pm.get("offer_price", 0)
            original = 0
            for field in ["was_price","list_price","strike_through_price"]:
                val = r.get(field,"")
                if val:
                    try:
                        original = float(re.sub(r'[^\d.]','',str(val)))
                        if original > price: break
                    except: pass
            name = r.get("title","")
            link = r.get("product_page_url","")
            if not link:
                iid = r.get("us_item_id","")
                if iid:
                    suffix = "ca" if market == "CA" else "com"
                    link   = f"https://www.walmart.{suffix}/en/ip/{iid}"
            if not name or not link: continue
            if market == "CA": link = link.replace("walmart.com","walmart.ca")
            if not (MIN_PRICE <= (price or 0) <= MAX_PRICE): continue
            results.append({
                "name": name, "price": price, "original": original,
                "link": link,
                "store": f"Walmart.{'ca' if market=='CA' else 'com'}",
                "market": market,
                "source": f"Walmart {market}",
            })
    except Exception as e:
        print(f"Walmart erreur [{query[:20]}]: {e}")
    return results

# ============================================================
#   SCAN PRINCIPAL
# ============================================================

def run_scan():
    raw = []
    tasks = (
        [("g_ca", q, "CA") for q in SEARCHES_CA] +
        [("g_us", q, "US") for q in SEARCHES_US] +
        [("wmt_ca", q, "CA") for q in WALMART_CA] +
        [("wmt_us", q, "US") for q in WALMART_US]
    )
    print(f"   {len(tasks)} requêtes...")

    def run(task):
        t, q, m = task
        try:
            if t == "g_ca":   return scrape_google(q, "CA")
            if t == "g_us":   return scrape_google(q, "US")
            if t == "wmt_ca": return scrape_walmart(q, "CA")
            if t == "wmt_us": return scrape_walmart(q, "US")
        except: pass
        return []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(run, task): task for task in tasks}
        for future in as_completed(futures):
            try: raw.extend(future.result())
            except: pass

    print(f"   Brut: {len(raw)}")

    # Déduplique
    seen, unique = set(), []
    for r in raw:
        pid = make_product_id(r["name"], r["store"])
        if pid not in seen:
            seen.add(pid)
            unique.append(r)

    print(f"   Uniques: {len(unique)}")

    # Sauvegarde tous les prix
    for r in unique:
        try:
            pid  = make_product_id(r["name"], r["store"])
            pkey = make_product_key(r["name"])
            cat  = detect_category(r["name"])
            save_price(pid, pkey, r["name"], r["store"], cat,
                       r["price"], r.get("original", 0), r["link"], r["market"])
        except: pass

    # Analyse
    deals = []
    for r in unique:
        try:
            price    = r.get("price", 0)
            original = r.get("original", 0)
            if not price or price <= 0: continue

            pid      = make_product_id(r["name"], r["store"])
            pkey     = make_product_key(r["name"])
            stats    = get_price_stats(pid)
            min_sav, cat = get_min_savings(r["name"])

            anomaly_type, drop_vs_avg, drop_vs_orig, baseline, velocity, is_fast_drop = detect_anomaly(
                pid, price, original, stats
            )

            if not anomaly_type: continue

            savings = max((original or baseline or price * 1.3) - price, 0)
            if savings < min_sav: continue

            arb_type, arb_opps = detect_arbitrage(pkey, r["store"], price)
            if arb_type and not anomaly_type:
                anomaly_type = arb_type

            if not anomaly_type: continue

            is_ca         = r["market"] == "CA"
            score, reason = calculate_score(
                anomaly_type, drop_vs_avg, drop_vs_orig, price, original,
                is_ca, stats, velocity, is_fast_drop, arb_type, arb_opps
            )

            if score < 3: continue

            deals.append({
                "id":             pid,
                "product_key":    pkey,
                "name":           r["name"],
                "price":          price,
                "original_price": original,
                "baseline":       baseline or original or price,
                "link":           r["link"],
                "store":          r["store"],
                "market":         "🇨🇦" if is_ca else "🇺🇸",
                "score":          score,
                "reason":         reason,
                "anomaly_type":   anomaly_type,
                "drop_vs_avg":    drop_vs_avg,
                "drop_vs_orig":   drop_vs_orig,
                "stats":          stats,
                "velocity":       velocity,
                "is_fast_drop":   is_fast_drop,
                "arbitrage_opps": arb_opps,
                "category":       cat,
                "source":         r["source"],
                "multi_site":     False,
            })
        except Exception as e:
            print(f"   Analyse erreur: {e}")
            continue

    # Multi-site check
    key_map = {}
    for d in deals:
        k = d["product_key"]
        if k not in key_map: key_map[k] = []
        key_map[k].append(d)
    for d in deals:
        entries = key_map.get(d["product_key"], [])
        if any(o["id"] != d["id"] and o["store"] != d["store"] for o in entries):
            d["multi_site"] = True
            d["score"]      += 2
            d["reason"]     += " | ✅ multi-site"

    print(f"   Deals: {len(deals)}")
    return deals

# ============================================================
#   LOOP 24/7
# ============================================================

def run_bot():
    print("=" * 65)
    print("   DEAL HUNTER ELITE V6 — STABLE")
    print(f"   Scan toutes les {SCAN_INTERVAL//60} min")
    print("=" * 65)

    init_db()

    send_telegram(
        "🤖 <b>Deal Hunter Elite V6 — DÉMARRÉ!</b>\n\n"
        "🧠 <b>Systèmes actifs:</b>\n"
        "  📊 Price Memory (SQLite)\n"
        "  ⚡ Velocity Detection\n"
        "  🔄 Cross-site Arbitrage\n"
        "  📦 Category Awareness\n"
        "  💣 Anomaly Detection\n\n"
        "🌍 Canada 🇨🇦 + USA 🇺🇸\n\n"
        "📈 Le bot devient plus précis à chaque scan!"
    )

    scan_count = 0
    total_sent = 0

    while True:
        scan_count += 1
        start       = datetime.now()

        print(f"\n{'='*65}")
        print(f"SCAN #{scan_count} — {start.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*65}")

        try:
            if scan_count % 100 == 0:
                cleanup_old_data()

            deals     = run_scan()
            new_deals = [d for d in deals if should_alert(d["id"], d["price"])]
            print(f"   Nouveaux: {len(new_deals)}")

            priority = {
                "PRICE_ERROR": 6, "ARBITRAGE_HIGH": 5, "HOT_DEAL": 4,
                "FLASH_SALE": 4, "ALL_TIME_LOW": 3, "ARBITRAGE_LOW": 3, "GOOD_DEAL": 2
            }
            new_deals.sort(
                key=lambda x: (priority.get(x["anomaly_type"], 0), x["score"]),
                reverse=True
            )

            sent = 0
            for deal in new_deals:
                if sent >= MAX_ALERTS: break
                labels = {
                    "PRICE_ERROR":"💣","HOT_DEAL":"🔥","ARBITRAGE_HIGH":"🔄",
                    "FLASH_SALE":"⚡","ALL_TIME_LOW":"📉","GOOD_DEAL":"✅"
                }
                emoji = labels.get(deal["anomaly_type"], "📊")
                print(f"   {emoji} {deal['anomaly_type']} | Score:{deal['score']} | {deal['name'][:35]} | ${deal['price']:.2f}")
                if send_telegram(format_deal(deal)):
                    mark_alerted(deal["id"], deal["price"])
                    total_sent += 1
                    sent       += 1
                    print(f"   ✓ Envoyé!")
                time.sleep(0.3)

        except Exception as e:
            print(f"Erreur scan #{scan_count}: {e}")

        duration = (datetime.now() - start).seconds
        print(f"\n   Scan #{scan_count} | {duration}s | Envoyés: {sent if 'sent' in dir() else 0} | Total: {total_sent}")

        if scan_count % 12 == 0:
            nb_p, nb_pr = get_db_stats()
            send_telegram(
                f"📊 <b>Rapport horaire</b>\n\n"
                f"🔄 Scans: {scan_count}\n"
                f"💰 Deals envoyés: {total_sent}\n"
                f"🧠 Produits en mémoire: {nb_p:,}\n"
                f"📈 Prix enregistrés: {nb_pr:,}\n"
                f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            )

        wait = max(SCAN_INTERVAL - duration, 10)
        print(f"   Prochain scan dans {wait}s...")
        time.sleep(wait)

if __name__ == "__main__":
    run_bot()
