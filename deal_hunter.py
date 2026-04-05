import requests
import time
import json
import os
import re
import hashlib
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
#   DEAL HUNTER — 100% SERPAPI
#   Tous les sites Canada + USA + erreurs de prix
#   Critères larges pour capturer le maximum
# ============================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8565261834:AAG9cnWMpAuVLSsGyvPBCTApfg2Y0RSTO1Q")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT", "1252259498")
SERPAPI_KEY    = os.environ.get("SERPAPI_KEY", "407fca2bc9bc6dd3ecdc7fda39d7183a2b10039def40d3ef8454a842a2458715")

MIN_DISCOUNT      = 25
MIN_PRICE         = 3
MAX_PRICE         = 2000
MIN_SAVINGS       = 5
SCORE_DEAL        = 4
MAX_ALERTS        = 25
MAX_WORKERS       = 8
ERROR_PRICE_RATIO = 0.30
HISTORY_FILE      = "deal_hunter_history.json"

TRUSTED_CA = [
    "walmart","canadian tire","best buy","staples","home depot","rona",
    "sport chek","london drugs","shoppers","costco","mec","sail",
    "bureau en gros","winners","homesense","pharmaprix","superstore",
    "loblaws","metro","maxi","dollarama","giant tiger","structube","simons",
]
TRUSTED_US = [
    "target","walmart","home depot","lowes","best buy","staples",
    "walgreens","cvs","costco","sams club","kohls","macys","nordstrom",
    "tj maxx","marshalls","ross","overstock","wayfair","chewy",
    "petco","petsmart","office depot","dick's sporting","rei",
]
BLOCKED = ["ebay","aliexpress","temu","wish","alibaba","shein","banggood","dhgate","etsy"]

SEARCHES_CA = [
    "price error listing canada","erreur de prix canada",
    "clearance -80% canada","clearance -70% canada","clearance -60% canada",
    "liquidation -50% canada","clearance sale -80% site:walmart.ca",
    "clearance -70% site:canadiantire.ca","open box -60% site:bestbuy.ca",
    "clearance -60% site:homedepot.ca","clearance -50% site:staples.ca",
    "electronics clearance -60% canada","laptop clearance -50% canada",
    "tv clearance -60% canada","appliances clearance -50% canada",
    "furniture clearance -60% canada","toys clearance -70% canada",
    "clothing clearance -70% canada","tools clearance -50% canada",
    "sports clearance -50% canada","baby clearance -60% canada",
    "beauty clearance -60% canada","gaming clearance -50% canada",
    "headphones clearance -60% canada","kitchen clearance -50% canada",
]
SEARCHES_US = [
    "price error listing usa","pricing error clearance",
    "clearance -80%","clearance -70%","clearance -60%",
    "target clearance -70%","walmart clearance -60% rollback",
    "home depot clearance -60%","best buy clearance -70%",
    "lowes clearance -50%","kohls clearance -70%",
    "electronics clearance -70%","tv clearance -70%",
    "appliances clearance -60%","furniture clearance -70%",
    "toys clearance -80%","clothing clearance -80%",
    "tools clearance -60%","baby clearance -70%",
    "gaming clearance -60%","beauty clearance -70%",
]
WALMART_CA = [
    "clearance","liquidation","deals","electronics clearance",
    "furniture clearance","toys clearance","clothing clearance",
    "tools clearance","sports clearance","baby clearance","gaming clearance",
]
WALMART_US = [
    "clearance rollback","electronics clearance","furniture clearance",
    "toys clearance","clothing clearance","tools clearance","baby clearance",
]

def make_id(link, price):
    return hashlib.md5(f"{link}_{price}".encode()).hexdigest()[:16]

def is_fake(price, original):
    if not price or not original or price <= 0 or original <= 0: return True
    if price >= original: return True
    if (original - price) < MIN_SAVINGS: return True
    if price < 0.5: return True
    if (original / price) > 50: return True
    return False

def is_valid(name, price):
    if not name or len(name) < 3: return False
    for b in ["gift card","carte cadeau","warranty","garantie","subscription","abonnement","digital download","activation code"]:
        if b in name.lower(): return False
    return MIN_PRICE <= price <= MAX_PRICE

def is_blocked(store):
    return any(b in store.lower() for b in BLOCKED)

def detect_error(price, original):
    if original <= 0 or price <= 0: return False, ""
    ratio = price / original
    disc  = ((original - price) / original) * 100
    if ratio < 0.10 and original >= 20:
        return True, f"💣 ERREUR EXTRÊME: ${price:.2f} au lieu de ${original:.2f}"
    if ratio < ERROR_PRICE_RATIO and original >= 15:
        return True, f"⚠️ ERREUR DE PRIX: ${price:.2f} au lieu de ${original:.2f} (-{disc:.0f}%)"
    return False, ""

def score_deal(discount, price, original, is_canada, price_error, multi=False):
    score, reasons = 0, []
    if discount >= 80:   score += 7; reasons.append(f"💣 -{discount:.0f}%")
    elif discount >= 70: score += 6; reasons.append(f"💣 -{discount:.0f}%")
    elif discount >= 60: score += 5; reasons.append(f"🔥 -{discount:.0f}%")
    elif discount >= 50: score += 4; reasons.append(f"🔥 -{discount:.0f}%")
    elif discount >= 40: score += 3; reasons.append(f"💰 -{discount:.0f}%")
    elif discount >= 30: score += 2; reasons.append(f"✅ -{discount:.0f}%")
    elif discount >= 25: score += 1; reasons.append(f"📊 -{discount:.0f}%")
    if price_error: score += 5; reasons.append("💣 ERREUR DE PRIX")
    savings = original - price
    if savings >= 500:   score += 4; reasons.append(f"💸 -${savings:.0f}")
    elif savings >= 200: score += 3; reasons.append(f"💸 -${savings:.0f}")
    elif savings >= 100: score += 2; reasons.append(f"💸 -${savings:.0f}")
    elif savings >= 30:  score += 1; reasons.append(f"💸 -${savings:.0f}")
    score += 2 if is_canada else 1
    reasons.append("🇨🇦" if is_canada else "🇺🇸")
    if multi: score += 2; reasons.append("✅ multi-site")
    return score, " | ".join(reasons)

def send_telegram(msg):
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": False},
            timeout=15
        )
        return resp.status_code == 200
    except: return False

def format_deal(deal):
    score    = deal["score"]
    discount = deal["discount"]
    name     = deal["name"][:65]
    price    = deal["price"]
    original = deal["original_price"]
    savings  = original - price
    is_error = deal.get("price_error", False)
    err_msg  = deal.get("error_msg", "")
    multi    = deal.get("multi_site", False)

    if is_error and score >= 9:   emoji, tag = "💣", "ERREUR DE PRIX"
    elif score >= 9:               emoji, tag = "🔥", "HOT DEAL"
    elif score >= 7:               emoji, tag = "✅", "DEAL"
    else:                          emoji, tag = "📊", "BON RABAIS"

    multi_line = "\n✅ <b>Confirmé multi-site!</b>" if multi else ""
    error_line = f"\n⚠️ <b>{err_msg}</b>" if err_msg else ""

    return (
        f"{emoji} <b>{tag}</b> — Score {score}\n\n"
        f"📦 <b>{name}</b>\n\n"
        f"💰 Prix: <b>${price:.2f}</b>\n"
        f"📉 Normal: <s>${original:.2f}</s>\n"
        f"💸 Économie: <b>-{discount:.0f}% (${savings:.2f})</b>\n"
        f"{error_line}{multi_line}\n\n"
        f"🏪 {deal['store']} {deal['market']}\n"
        f"📡 {deal.get('source','')}\n\n"
        f"💡 <i>{deal['reason']}</i>\n\n"
        f"🔗 <a href='{deal['link']}'>Voir le deal →</a>\n"
        f"⏰ {datetime.now().strftime('%H:%M:%S')}"
    )

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f: return json.load(f)
        except: return {}
    return {}

def save_history(pid, deal):
    h = load_history()
    h[pid] = {"last_seen": datetime.now().isoformat(), "last_price": deal["price"]}
    if len(h) > 3000:
        keys = sorted(h, key=lambda k: h[k].get("last_seen",""))
        for k in keys[:500]: del h[k]
    with open(HISTORY_FILE, "w") as f: json.dump(h, f, indent=2)

def should_alert(pid, price):
    h     = load_history()
    entry = h.get(pid, {})
    if not entry: return True
    last_price = entry.get("last_price", 0)
    last_seen  = entry.get("last_seen", "")
    if last_price > 0 and price < last_price * 0.85: return True
    try:
        if datetime.now() - datetime.fromisoformat(last_seen) > timedelta(hours=24): return True
    except: pass
    return False

class MultiSiteTracker:
    def __init__(self): self.products = {}
    def add(self, deal):
        key = hashlib.md5(deal["name"][:20].lower().encode()).hexdigest()[:8]
        if key not in self.products: self.products[key] = []
        self.products[key].append(deal)
    def is_multi(self, deal):
        key = hashlib.md5(deal["name"][:20].lower().encode()).hexdigest()[:8]
        return any(o["id"] != deal["id"] and o["store"] != deal["store"] for o in self.products.get(key, []))

tracker = MultiSiteTracker()

def process_result(r, market, source_label):
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

    if not price or not original or original <= price: return None
    if is_fake(price, original): return None

    discount = ((original - price) / original) * 100
    if discount < MIN_DISCOUNT: return None

    name  = r.get("title","") or r.get("name","")
    link  = r.get("link","") or r.get("product_link","") or r.get("product_page_url","")
    store = r.get("source","") or r.get("seller","") or r.get("store","")

    if not name or not link or not store: return None
    if "google." in link.lower(): return None
    if is_blocked(store): return None
    if not is_valid(name, price): return None

    is_ca    = any(t in store.lower() for t in TRUSTED_CA)
    is_us    = any(t in store.lower() for t in TRUSTED_US)
    if not is_ca and not is_us and discount < 60: return None

    pid          = make_id(link, price)
    is_error, em = detect_error(price, original)
    sc, sr       = score_deal(discount, price, original, is_ca, is_error)
    if sc < SCORE_DEAL: return None

    return {
        "id": pid, "name": name, "price": price, "original_price": original,
        "discount": discount, "link": link, "store": store,
        "market": "🇨🇦" if is_ca else "🇺🇸",
        "score": sc, "reason": sr, "price_error": is_error,
        "error_msg": em, "source": source_label,
    }

def scan_google(query, market="CA"):
    gl     = "ca" if market == "CA" else "us"
    domain = "google.ca" if market == "CA" else "google.com"
    params = {
        "api_key": SERPAPI_KEY, "engine": "google_shopping",
        "q": query, "gl": gl, "hl": "en",
        "google_domain": domain, "num": 20, "sort_by": "1",
    }
    deals = []
    try:
        resp = requests.get("https://serpapi.com/search", params=params, timeout=30)
        if resp.status_code != 200: return deals
        for r in resp.json().get("shopping_results", []):
            deal = process_result(r, market, f"Google Shopping {market}")
            if deal: deals.append(deal)
    except Exception as e:
        print(f"Google erreur [{query[:20]}]: {e}")
    return deals

def scan_walmart(query, market="CA"):
    params = {"api_key": SERPAPI_KEY, "engine": "walmart", "query": query, "ps": "40"}
    deals  = []
    try:
        resp = requests.get("https://serpapi.com/search", params=params, timeout=30)
        if resp.status_code != 200: return deals
        for r in resp.json().get("organic_results", []):
            # Adapte le format Walmart
            price = 0
            pm    = r.get("primary_offer",{})
            if isinstance(pm, dict): price = pm.get("offer_price",0)
            original = 0
            for field in ["was_price","list_price","strike_through_price"]:
                val = r.get(field,"")
                if val:
                    try:
                        original = float(re.sub(r'[^\d.]','',str(val)))
                        if original > price: break
                    except: pass

            if not price or not original or original <= price: continue
            if is_fake(price, original): continue

            discount = ((original - price) / original) * 100
            if discount < MIN_DISCOUNT: continue

            name = r.get("title","")
            link = r.get("product_page_url","")
            if not link:
                iid = r.get("us_item_id","")
                if iid:
                    suffix = "ca" if market == "CA" else "com"
                    link   = f"https://www.walmart.{suffix}/en/ip/{iid}"
            if not name or not link: continue
            if market == "CA": link = link.replace("walmart.com","walmart.ca")
            if not is_valid(name, price): continue

            pid          = make_id(link, price)
            is_error, em = detect_error(price, original)
            sc, sr       = score_deal(discount, price, original, market=="CA", is_error)
            if sc >= SCORE_DEAL:
                deals.append({
                    "id": pid, "name": name, "price": price, "original_price": original,
                    "discount": discount, "link": link,
                    "store": f"Walmart.{'ca' if market=='CA' else 'com'}",
                    "market": "🇨🇦" if market=="CA" else "🇺🇸",
                    "score": sc, "reason": sr, "price_error": is_error,
                    "error_msg": em, "source": f"Walmart {market} Direct",
                })
    except Exception as e:
        print(f"Walmart erreur [{query[:20]}]: {e}")
    return deals

def run_scan():
    all_deals = []
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
            if t == "g_ca":   return scan_google(q, "CA")
            if t == "g_us":   return scan_google(q, "US")
            if t == "wmt_ca": return scan_walmart(q, "CA")
            if t == "wmt_us": return scan_walmart(q, "US")
        except: pass
        return []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(run, task): task for task in tasks}
        for future in as_completed(futures):
            try: all_deals.extend(future.result())
            except: pass

    for d in all_deals: tracker.add(d)
    for d in all_deals:
        d["multi_site"] = tracker.is_multi(d)
        if d["multi_site"]:
            d["score"]  += 2
            d["reason"] += " | ✅ multi-site"

    seen, unique = set(), []
    for d in all_deals:
        if d["id"] not in seen:
            seen.add(d["id"])
            unique.append(d)

    return unique

def main():
    print(f"Deal Hunter — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Rabais min: {MIN_DISCOUNT}% | Score min: {SCORE_DEAL}")

    all_deals = run_scan()
    errors    = [d for d in all_deals if d.get("price_error")]
    print(f"Total: {len(all_deals)} | Erreurs de prix: {len(errors)}")

    new_deals = [d for d in all_deals if should_alert(d["id"], d["price"])]
    print(f"Nouveaux: {len(new_deals)}")

    if not new_deals:
        print("Aucun nouveau deal")
        return

    new_deals.sort(
        key=lambda x: (x.get("price_error",False), x["score"], x.get("multi_site",False)),
        reverse=True
    )

    sent = 0
    for deal in new_deals:
        if sent >= MAX_ALERTS: break
        print(f"  {'💣' if deal.get('price_error') else '🔥'} Score:{deal['score']} | {deal['name'][:35]} | -{deal['discount']:.0f}% | {deal['store']}")
        if send_telegram(format_deal(deal)):
            save_history(deal["id"], deal)
            sent += 1
        time.sleep(0.3)

    print(f"Terminé — {sent} deals envoyés")

if __name__ == "__main__":
    main()
