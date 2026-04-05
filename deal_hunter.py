import requests
import time
import json
import os
import re
import hashlib
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup

# ============================================================
#   DEAL HUNTER — VERSION GITHUB ACTIONS
#   Lance un scan, envoie les deals, et arrête
#   GitHub Actions le relance toutes les 5 minutes
# ============================================================

# Clés depuis variables d'environnement GitHub Secrets
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8565261834:AAG9cnWMpAuVLSsGyvPBCTApfg2Y0RSTO1Q")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT", "1252259498")
SERPAPI_KEY    = os.environ.get("SERPAPI_KEY", "407fca2bc9bc6dd3ecdc7fda39d7183a2b10039def40d3ef8454a842a2458715")

MIN_DISCOUNT      = 30
MIN_PRICE         = 5
MAX_PRICE         = 1000
MIN_SAVINGS       = 8
SCORE_HOT         = 9
SCORE_DEAL        = 5
MAX_ALERTS        = 20
MAX_WORKERS       = 4
MAX_ORIGINAL_MULT = 10.0
ERROR_PRICE_RATIO = 0.25
HISTORY_FILE      = "deal_hunter_history.json"

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "fr-CA,fr;q=0.9,en-CA;q=0.8",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

CANADA_SITES = [
    "walmart.ca", "canadiantire.ca", "bestbuy.ca", "staples.ca",
    "homedepot.ca", "rona.ca", "sportchek.ca", "londondrugs.com",
    "shoppersdrugmart.ca", "costco.ca", "mec.ca", "sail.ca",
]
BLOCKED = ["ebay","aliexpress","temu","wish","alibaba","shein","banggood","etsy","amazon"]

GOOGLE_CA = [
    "clearance -70% canada",
    "clearance -60% canada",
    "liquidation -50% canada",
    "price error clearance canada",
    "site:walmart.ca clearance -60%",
    "site:canadiantire.ca clearance -50%",
    "site:bestbuy.ca open box -50%",
    "electronics clearance -60% canada",
    "toys clearance -60% canada",
    "tools clearance -50% canada",
]
GOOGLE_US = [
    "clearance -70% usa",
    "target clearance -70%",
    "walmart clearance -60% rollback",
    "home depot clearance -50%",
    "best buy clearance -60%",
]
WALMART_CA = ["clearance", "liquidation", "deals electronics", "toys clearance"]
WALMART_US = ["clearance rollback", "electronics clearance", "toys clearance"]

# ============================================================
#   UTILITAIRES
# ============================================================

def make_id(link, price):
    return hashlib.md5(f"{link}_{price}".encode()).hexdigest()[:16]

def is_fake(price, original):
    if not price or not original or price <= 0 or original <= 0:
        return True
    if price >= original:
        return True
    if (original - price) < MIN_SAVINGS:
        return True
    if price < 0.5:
        return True
    if (original / price) > MAX_ORIGINAL_MULT:
        return True
    return False

def is_valid_product(name, price):
    if not name or len(name) < 5:
        return False
    blocked_words = ["gift card","carte cadeau","warranty","garantie",
                     "subscription","abonnement","digital download","activation code"]
    for b in blocked_words:
        if b in name.lower():
            return False
    return MIN_PRICE <= price <= MAX_PRICE

def is_blocked_store(store):
    return any(b in store.lower() for b in BLOCKED)

def is_canada_store(store):
    return any(s.replace(".ca","").replace(".com","") in store.lower() for s in CANADA_SITES)

def detect_price_error(price, original):
    if original <= 0 or price <= 0:
        return False, ""
    ratio    = price / original
    discount = ((original - price) / original) * 100
    if ratio < ERROR_PRICE_RATIO and original >= 20:
        return True, f"ERREUR: ${price:.2f} au lieu de ${original:.2f} (-{discount:.0f}%)"
    return False, ""

def score_deal(discount, price, original, is_canada, price_error, multi_site=False):
    score   = 0
    reasons = []
    if discount >= 80:
        score += 6; reasons.append(f"💣 -{discount:.0f}%")
    elif discount >= 70:
        score += 5; reasons.append(f"💣 -{discount:.0f}%")
    elif discount >= 60:
        score += 4; reasons.append(f"🔥 -{discount:.0f}%")
    elif discount >= 50:
        score += 3; reasons.append(f"💰 -{discount:.0f}%")
    elif discount >= 40:
        score += 2; reasons.append(f"✅ -{discount:.0f}%")
    elif discount >= 30:
        score += 1; reasons.append(f"📊 -{discount:.0f}%")
    if price_error:
        score += 4; reasons.append("💣 ERREUR DE PRIX")
    savings = original - price
    if savings >= 200:
        score += 3; reasons.append(f"💸 -${savings:.0f}")
    elif savings >= 100:
        score += 2; reasons.append(f"💸 -${savings:.0f}")
    elif savings >= 30:
        score += 1; reasons.append(f"💸 -${savings:.0f}")
    score += 2 if is_canada else 1
    reasons.append("🇨🇦" if is_canada else "🇺🇸")
    if multi_site:
        score += 2; reasons.append("✅ multi-site")
    if 15 <= price <= 80:
        score += 1; reasons.append("📦 prix OA")
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
    except:
        return False

def format_deal(deal):
    score    = deal["score"]
    discount = deal["discount"]
    name     = deal["name"][:65]
    price    = deal["price"]
    original = deal["original_price"]
    savings  = original - price
    link     = deal["link"]
    store    = deal["store"]
    market   = deal["market"]
    reason   = deal["reason"]
    is_error = deal.get("price_error", False)
    err_msg  = deal.get("error_msg", "")
    multi    = deal.get("multi_site", False)

    if is_error and score >= SCORE_HOT:
        emoji, tag = "💣", "ERREUR DE PRIX"
    elif score >= SCORE_HOT:
        emoji, tag = "🔥", "HOT DEAL"
    elif score >= 7:
        emoji, tag = "✅", "DEAL"
    else:
        emoji, tag = "📊", "BON RABAIS"

    multi_line = "\n✅ <b>Confirmé multi-site!</b>" if multi else ""
    error_line = f"\n⚠️ <b>{err_msg}</b>" if err_msg else ""

    return (
        f"{emoji} <b>{tag}</b> — Score {score}/18\n\n"
        f"📦 <b>{name}</b>\n\n"
        f"💰 Prix: <b>${price:.2f}</b>\n"
        f"📉 Normal: <s>${original:.2f}</s>\n"
        f"💸 Économie: <b>-{discount:.0f}% (${savings:.2f})</b>\n"
        f"{error_line}{multi_line}\n\n"
        f"🏪 {store} {market}\n"
        f"📡 {deal.get('source','')}\n\n"
        f"💡 <i>{reason}</i>\n\n"
        f"🔗 <a href='{link}'>Voir le deal →</a>\n"
        f"⏰ {datetime.now().strftime('%H:%M:%S')}"
    )

# ============================================================
#   HISTORIQUE
# ============================================================

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_history(pid, deal):
    h     = load_history()
    entry = h.get(pid, {})
    h[pid] = {
        "last_seen":  datetime.now().isoformat(),
        "last_price": deal["price"],
        "last_score": deal["score"],
        "name":       deal["name"][:50],
    }
    if len(h) > 3000:
        keys = sorted(h, key=lambda k: h[k].get("last_seen",""))
        for k in keys[:500]:
            del h[k]
    with open(HISTORY_FILE, "w") as f:
        json.dump(h, f, indent=2)

def should_alert(pid, price):
    h     = load_history()
    entry = h.get(pid, {})
    if not entry:
        return True
    last_price = entry.get("last_price", 0)
    last_seen  = entry.get("last_seen", "")
    if last_price > 0 and price < last_price * 0.85:
        return True
    try:
        if datetime.now() - datetime.fromisoformat(last_seen) > timedelta(hours=24):
            return True
    except:
        pass
    return False

# ============================================================
#   MULTI-SITE TRACKER
# ============================================================

class MultiSiteTracker:
    def __init__(self):
        self.products = {}
    def add(self, deal):
        key = hashlib.md5(deal["name"][:20].lower().encode()).hexdigest()[:8]
        if key not in self.products:
            self.products[key] = []
        self.products[key].append(deal)
    def is_multi(self, deal):
        key     = hashlib.md5(deal["name"][:20].lower().encode()).hexdigest()[:8]
        entries = self.products.get(key, [])
        return any(o["id"] != deal["id"] and o["store"] != deal["store"] for o in entries)

tracker = MultiSiteTracker()

# ============================================================
#   SCRAPERS
# ============================================================

def scrape_walmart_ca():
    deals = []
    urls  = [
        "https://www.walmart.ca/en/cp/clearance/N-4059",
        "https://www.walmart.ca/en/cp/deals/N-4023",
        "https://www.walmart.ca/en/cp/electronics/N-3944",
        "https://www.walmart.ca/en/cp/home/N-3752",
        "https://www.walmart.ca/en/cp/toys/N-3813",
    ]
    for url in urls:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code != 200: continue
            prices   = re.findall(r'"currentPrice"\s*:\s*(\d+\.?\d*)', resp.text)
            was      = re.findall(r'"wasPrice"\s*:\s*(\d+\.?\d*)', resp.text)
            names    = re.findall(r'"name"\s*:\s*"([^"]{5,100})"', resp.text)
            item_ids = re.findall(r'"usItemId"\s*:\s*"(\d+)"', resp.text)
            for i in range(min(len(prices), len(was), 30)):
                try:
                    price = float(prices[i]); original = float(was[i])
                    name  = names[i] if i < len(names) else ""
                    iid   = item_ids[i] if i < len(item_ids) else ""
                    if not is_valid_product(name, price): continue
                    discount = ((original - price) / original) * 100 if original > price else 0
                    if discount < MIN_DISCOUNT or is_fake(price, original): continue
                    link          = f"https://www.walmart.ca/en/ip/{iid}" if iid else url
                    pid           = make_id(link, price)
                    is_error, em  = detect_price_error(price, original)
                    sc, sr        = score_deal(discount, price, original, True, is_error)
                    if sc >= SCORE_DEAL:
                        deals.append({"id":pid,"name":name,"price":price,"original_price":original,
                            "discount":discount,"link":link,"store":"Walmart.ca","market":"🇨🇦",
                            "score":sc,"reason":sr,"price_error":is_error,"error_msg":em,
                            "source":"Direct Walmart.ca"})
                except: continue
        except: pass
        time.sleep(0.3)
    return deals

def scrape_canadian_tire():
    deals = []
    urls  = [
        "https://www.canadiantire.ca/en/promotions/clearance.html",
        "https://www.canadiantire.ca/en/auto.html",
        "https://www.canadiantire.ca/en/sporting-goods.html",
        "https://www.canadiantire.ca/en/home-garden.html",
        "https://www.canadiantire.ca/en/tools-hardware.html",
    ]
    for url in urls:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code != 200: continue
            prices = re.findall(r'"currentPrice"\s*:\s*(\d+\.?\d*)', resp.text)
            was    = re.findall(r'"regularPrice"\s*:\s*(\d+\.?\d*)', resp.text)
            names  = re.findall(r'"name"\s*:\s*"([^"]{5,100})"', resp.text)
            urls_p = re.findall(r'"url"\s*:\s*"(/en/[^"]+\.html)"', resp.text)
            for i in range(min(len(prices), len(was), 30)):
                try:
                    price = float(prices[i]); original = float(was[i])
                    name  = names[i] if i < len(names) else ""
                    purl  = "https://www.canadiantire.ca" + urls_p[i] if i < len(urls_p) else url
                    if not is_valid_product(name, price): continue
                    discount = ((original - price) / original) * 100 if original > price else 0
                    if discount < MIN_DISCOUNT or is_fake(price, original): continue
                    pid          = make_id(purl, price)
                    is_error, em = detect_price_error(price, original)
                    sc, sr       = score_deal(discount, price, original, True, is_error)
                    if sc >= SCORE_DEAL:
                        deals.append({"id":pid,"name":name,"price":price,"original_price":original,
                            "discount":discount,"link":purl,"store":"Canadian Tire","market":"🇨🇦",
                            "score":sc,"reason":sr,"price_error":is_error,"error_msg":em,
                            "source":"Direct Canadian Tire"})
                except: continue
        except: pass
        time.sleep(0.3)
    return deals

def scrape_bestbuy_ca():
    deals = []
    urls  = [
        "https://www.bestbuy.ca/en-ca/collection/clearance/blt3c52a58ab1cc69fd",
        "https://www.bestbuy.ca/en-ca/collection/open-box/bltdc08e9a5c74b3c85",
    ]
    for url in urls:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code != 200: continue
            prices = re.findall(r'"salePrice"\s*:\s*(\d+\.?\d*)', resp.text)
            reg    = re.findall(r'"regularPrice"\s*:\s*(\d+\.?\d*)', resp.text)
            names  = re.findall(r'"name"\s*:\s*"([^"]{5,100})"', resp.text)
            skus   = re.findall(r'"sku"\s*:\s*"(\d+)"', resp.text)
            for i in range(min(len(prices), len(reg), 30)):
                try:
                    price = float(prices[i]); original = float(reg[i])
                    name  = names[i] if i < len(names) else ""
                    sku   = skus[i] if i < len(skus) else ""
                    if not is_valid_product(name, price): continue
                    discount = ((original - price) / original) * 100 if original > price else 0
                    if discount < MIN_DISCOUNT or is_fake(price, original): continue
                    link         = f"https://www.bestbuy.ca/en-ca/product/{sku}" if sku else url
                    pid          = make_id(link, price)
                    is_error, em = detect_price_error(price, original)
                    sc, sr       = score_deal(discount, price, original, True, is_error)
                    if sc >= SCORE_DEAL:
                        deals.append({"id":pid,"name":name,"price":price,"original_price":original,
                            "discount":discount,"link":link,"store":"Best Buy CA","market":"🇨🇦",
                            "score":sc,"reason":sr,"price_error":is_error,"error_msg":em,
                            "source":"Direct Best Buy CA"})
                except: continue
        except: pass
        time.sleep(0.3)
    return deals

def scan_google(query, market="CA"):
    gl     = "ca" if market == "CA" else "us"
    domain = "google.ca" if market == "CA" else "google.com"
    params = {"api_key": SERPAPI_KEY, "engine": "google_shopping",
              "q": query, "gl": gl, "hl": "en",
              "google_domain": domain, "num": 20, "sort_by": "1"}
    deals  = []
    trusted_ca = ["walmart","canadian tire","best buy","staples","home depot",
                  "rona","sport chek","london drugs","shoppers","costco","mec"]
    trusted_us = ["target","walmart","home depot","walgreens","cvs","costco","best buy","lowes","kohls"]
    try:
        resp = requests.get("https://serpapi.com/search", params=params, timeout=30)
        if resp.status_code != 200: return deals
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
            if not price or not original or original <= price: continue
            discount = ((original - price) / original) * 100
            if discount < MIN_DISCOUNT or is_fake(price, original): continue
            name  = r.get("title","")
            link  = r.get("link","") or r.get("product_link","")
            store = r.get("source", r.get("seller",""))
            if not name or not link or not store: continue
            if "google." in link.lower(): continue
            if is_blocked_store(store): continue
            if not is_valid_product(name, price): continue
            is_ca        = any(t in store.lower() for t in trusted_ca)
            is_us        = any(t in store.lower() for t in trusted_us)
            if not is_ca and not is_us: continue
            pid          = make_id(link, price)
            is_error, em = detect_price_error(price, original)
            sc, sr       = score_deal(discount, price, original, is_ca, is_error)
            if sc >= SCORE_DEAL:
                deals.append({"id":pid,"name":name,"price":price,"original_price":original,
                    "discount":discount,"link":link,"store":store,
                    "market":"🇨🇦" if is_ca else "🇺🇸",
                    "score":sc,"reason":sr,"price_error":is_error,"error_msg":em,
                    "source":"Google Shopping"})
    except Exception as e:
        print(f"   Google erreur: {e}")
    return deals

def scan_walmart_api(query, market="CA"):
    params = {"api_key": SERPAPI_KEY, "engine": "walmart", "query": query, "ps": "40"}
    deals  = []
    try:
        resp = requests.get("https://serpapi.com/search", params=params, timeout=30)
        if resp.status_code != 200: return deals
        for r in resp.json().get("organic_results", []):
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
            discount = ((original - price) / original) * 100
            if discount < MIN_DISCOUNT or is_fake(price, original): continue
            name = r.get("title","")
            link = r.get("product_page_url","")
            if not link:
                iid = r.get("us_item_id","")
                if iid:
                    suffix = "ca" if market=="CA" else "com"
                    link   = f"https://www.walmart.{suffix}/en/ip/{iid}"
            if not name or not link: continue
            if market=="CA": link = link.replace("walmart.com","walmart.ca")
            if not is_valid_product(name, price): continue
            pid          = make_id(link, price)
            is_error, em = detect_price_error(price, original)
            sc, sr       = score_deal(discount, price, original, market=="CA", is_error)
            if sc >= SCORE_DEAL:
                deals.append({"id":pid,"name":name,"price":price,"original_price":original,
                    "discount":discount,"link":link,
                    "store":f"Walmart.{'ca' if market=='CA' else 'com'}",
                    "market":"🇨🇦" if market=="CA" else "🇺🇸",
                    "score":sc,"reason":sr,"price_error":is_error,"error_msg":em,
                    "source":"Walmart API"})
    except Exception as e:
        print(f"   Walmart API erreur: {e}")
    return deals

# ============================================================
#   SCAN UNIQUE
# ============================================================

def run_scan():
    all_deals = []
    tasks = (
        [("g_ca", q, "CA") for q in GOOGLE_CA] +
        [("g_us", q, "US") for q in GOOGLE_US] +
        [("wmt_ca", q, "CA") for q in WALMART_CA] +
        [("wmt_us", q, "US") for q in WALMART_US] +
        [("direct_wmt", None, None)] +
        [("direct_ct", None, None)] +
        [("direct_bb", None, None)]
    )

    def run(task):
        t, q, m = task
        try:
            if t == "g_ca":       return scan_google(q, "CA")
            if t == "g_us":       return scan_google(q, "US")
            if t == "wmt_ca":     return scan_walmart_api(q, "CA")
            if t == "wmt_us":     return scan_walmart_api(q, "US")
            if t == "direct_wmt": return scrape_walmart_ca()
            if t == "direct_ct":  return scrape_canadian_tire()
            if t == "direct_bb":  return scrape_bestbuy_ca()
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

    seen   = set()
    unique = []
    for d in all_deals:
        if d["id"] not in seen:
            seen.add(d["id"])
            unique.append(d)
    return unique

# ============================================================
#   MAIN — UN SEUL SCAN
# ============================================================

def main():
    print(f"Deal Hunter — Scan du {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    all_deals = run_scan()
    print(f"Total: {len(all_deals)} deals trouvés")

    new_deals = [d for d in all_deals if should_alert(d["id"], d["price"])]
    print(f"Nouveaux: {len(new_deals)}")

    # Erreurs de prix en premier
    new_deals.sort(
        key=lambda x: (x.get("price_error",False), x["score"], x.get("multi_site",False)),
        reverse=True
    )

    sent = 0
    for deal in new_deals:
        if sent >= MAX_ALERTS: break
        if send_telegram(format_deal(deal)):
            save_history(deal["id"], deal)
            sent += 1
            print(f"✓ Envoyé: {deal['name'][:40]} | Score:{deal['score']} | -{deal['discount']:.0f}%")
        time.sleep(0.5)

    print(f"Scan terminé — {sent} deals envoyés sur Telegram")

if __name__ == "__main__":
    main()
