"""
PokéStock Monitor
=================
Tracks Pokemon TCG products at Target, Walmart, Pokemon Center & Amazon.
Sends Discord webhook alerts when stock is detected (online or in-store).

Setup:
  1. Copy .env.example to .env and fill in your values
  2. Edit products.json with the products you want to track
  3. Run: python monitor.py
"""

import json
import time
import os
import sys
import hashlib
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Playwright is used for Best Buy on local machines.
# On Railway/cloud it falls back to requests automatically.
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    from playwright_stealth import Stealth
    PLAYWRIGHT_AVAILABLE = True
except (ImportError, Exception):
    PLAYWRIGHT_AVAILABLE = False

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("monitor.log"),
    ],
)
log = logging.getLogger(__name__)

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")
CHECK_INTERVAL  = int(os.getenv("CHECK_INTERVAL", "60"))   # seconds between full scan
PRODUCTS_FILE   = os.getenv("PRODUCTS_FILE", "products.json")

# ── HTTP session with automatic retries ──────────────────────────────────────

def make_session():
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    })
    return s

SESSION = make_session()

# ── Discord ───────────────────────────────────────────────────────────────────

def send_discord_alert(product: dict, store: str, status: str, url: str, extra: str = "", live_price=None):
    if not DISCORD_WEBHOOK:
        log.warning("No Discord webhook set — skipping alert.")
        return

    color = 0x57F287 if "IN STOCK" in status.upper() else 0xED4245

    # Show live scraped price if available, otherwise fall back to products.json price
    if live_price:
        try:
            price_display = f"${float(live_price):.2f}"
        except (ValueError, TypeError):
            price_display = str(live_price)
    else:
        price_display = product.get("price", "N/A")

    embed = {
        "title": f"🎴 {status}",
        "description": f"**{product['name']}**\n{extra}",
        "color": color,
        "fields": [
            {"name": "Retailer",    "value": store,         "inline": True},
            {"name": "Live Price",  "value": price_display, "inline": True},
            {"name": "Link",        "value": f"[Buy Now]({url})", "inline": False},
        ],
        "footer": {"text": f"PokéStock Monitor • {datetime.now().strftime('%H:%M:%S')}"},
        "thumbnail": {"url": product.get("image", "")},
    }

    payload = {"content": "@everyone", "embeds": [embed]}
    try:
        r = SESSION.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        r.raise_for_status()
        log.info(f"Discord alert sent: {product['name']} @ {store}")
    except Exception as e:
        log.error(f"Discord alert failed: {e}")


# ── Price filter ─────────────────────────────────────────────────────────────

import re

def parse_price(price_str) -> float | None:
    """Extract a float from a price string like '$24.99' or '24.99'."""
    if price_str is None:
        return None
    match = re.search(r"[\d]+\.?\d*", str(price_str).replace(",", ""))
    return float(match.group()) if match else None


def price_ok(product: dict, live_price=None) -> bool:
    """
    Returns True if the product should trigger an alert based on price.
    - If no max_price is set on the product, always alert.
    - If live_price is provided, compare against that.
    - Falls back to the product's listed 'price' field if no live price.
    """
    max_price = parse_price(product.get("max_price"))
    if max_price is None:
        return True   # no limit set — always alert

    check_price = parse_price(live_price) or parse_price(product.get("price"))
    if check_price is None:
        return True   # can't determine price — alert to be safe

    if check_price <= max_price:
        return True

    log.info(
        f"[Price Filter] {product['name']}: "
        f"${check_price:.2f} exceeds max ${max_price:.2f} — skipping alert"
    )
    return False


# ── Target ────────────────────────────────────────────────────────────────────
# Target exposes a public fulfillment API used by their own website.
# TCIN = Target's internal product ID (found in the product URL).

TARGET_API = (
    "https://api.target.com/fulfillment_aggregator/v1/fiats/{tcin}"
    "?key=ff457966e64d5e877fdbad070f276d18ecec4a01"
    "&nearby={zip}&limit=20&requested_quantity=1&radius={radius}"
)

TARGET_ONLINE_API = (
    "https://api.target.com/products/v3/{tcin}"
    "?fields=buy_url,price,available_to_promise_quantity"
    "&key=ff457966e64d5e877fdbad070f276d18ecec4a01"
)

def check_target_online(product: dict) -> bool:
    """Returns True and fires alert if online stock found."""
    tcin = product.get("target_tcin")
    if not tcin:
        return False
    url = f"https://www.target.com/p/-/A-{tcin}"
    try:
        # Small random stagger so parallel threads don't all hit at once
        time.sleep(round(__import__('random').uniform(0.1, 1.5), 2))
        r = SESSION.get(
            TARGET_ONLINE_API.format(tcin=tcin),
            timeout=10,
        )
        data = r.json()
        qty        = data.get("available_to_promise_quantity", 0)
        live_price = data.get("price", {}).get("current_retail") if isinstance(data.get("price"), dict) else None
        if qty and int(qty) > 0:
            if not price_ok(product, live_price):
                return False
            send_discord_alert(product, "Target (Online)", "🟢 IN STOCK ONLINE", url,
                               f"Quantity: {qty}", live_price=live_price)
            return True
        log.info(f"[Target Online] {product['name']}: out of stock (qty={qty})")
    except ValueError:
        log.info(f"[Target Online] {product['name']}: empty response from Target API — will retry next pass")
    except requests.exceptions.Timeout:
        log.info(f"[Target Online] {product['name']}: timed out — will retry next pass")
    except Exception as e:
        log.warning(f"[Target Online] {product['name']}: {e}")
    return False

def check_target_instore(product: dict, zip_code: str, radius: int = 50) -> list[dict]:
    """Returns list of stores that have the product."""
    tcin = product.get("target_tcin")
    if not tcin:
        return []
    found = []
    try:
        r = SESSION.get(
            TARGET_API.format(tcin=tcin, zip=zip_code, radius=radius),
            timeout=10,
        )
        data = r.json()
        locations = data.get("products", [{}])[0].get("locations", [])
        for loc in locations:
            qty = loc.get("available_to_promise_quantity", 0)
            if qty and int(qty) > 0:
                store_name  = loc.get("store_name", "Unknown Store")
                store_city  = loc.get("city", "")
                store_state = loc.get("state", "")
                found.append({
                    "name":  f"{store_name} — {store_city}, {store_state}",
                    "qty":   qty,
                    "address": loc.get("address", ""),
                })
        if found:
            if not price_ok(product):
                return []
            stores_str = "\n".join(
                f"📍 {s['name']}\n    {s['address']} — qty: {s['qty']}" for s in found
            )
            url = f"https://www.target.com/p/-/A-{tcin}"
            send_discord_alert(product, "Target (In-Store)", "🟢 IN STORE", url, stores_str)
        else:
            log.info(f"[Target In-Store] {product['name']}: none in {zip_code}")
    except Exception as e:
        log.warning(f"[Target In-Store] {product['name']}: {e}")
    return found


# ── Walmart ───────────────────────────────────────────────────────────────────
# Walmart's store availability API (same endpoint powering their site).

WALMART_API = (
    "https://www.walmart.com/store/ajax/get-availability"
    "?itemId={item_id}&storeId={store_id}"
)

WALMART_SEARCH_API = (
    "https://www.walmart.com/search/api/preso"
    "?query={query}&facet=retailer_type%3AStore"
)

def check_walmart_online(product: dict) -> bool:
    item_id = product.get("walmart_item_id")
    if not item_id:
        return False
    url = f"https://www.walmart.com/ip/{item_id}"
    try:
        page = SESSION.get(url, timeout=10)
        in_stock = (
            '"availabilityStatus":"IN_STOCK"' in page.text
            or '"availability":"In Stock"' in page.text
        )
        live_price = None
        price_match = re.search(r'"price"\s*:\s*"?([\d]+\.?\d*)"?', page.text)
        if price_match:
            live_price = price_match.group(1)

        if in_stock:
            if not price_ok(product, live_price):
                return False
            send_discord_alert(product, "Walmart (Online)", "🟢 IN STOCK ONLINE", url,
                               "", live_price=live_price)
            return True
        log.info(f"[Walmart Online] {product['name']}: out of stock")
    except Exception as e:
        log.warning(f"[Walmart Online] {product['name']}: {e}")
    return False


# Walmart store finder + per-store availability API (powers their "Check store" button)
WALMART_STORES_API = (
    "https://www.walmart.com/store/electrode/api/stores"
    "?singleLineAddr={zip}&distance={radius}&limit=15"
)
WALMART_STORE_STATUS_API = (
    "https://www.walmart.com/store/{store_id}/product/{item_id}/status"
)
WALMART_STORE_HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "WM_QOS.CORRELATION_ID": "walmart-store-check",
}


def _get_walmart_nearby_stores(zip_code: str, radius: int = 50) -> list[dict]:
    """Fetch Walmart store IDs near a ZIP code."""
    try:
        r = SESSION.get(
            WALMART_STORES_API.format(zip=zip_code, radius=radius),
            headers=WALMART_STORE_HEADERS,
            timeout=10,
        )
        if not r.text.strip():
            log.info(f"[Walmart Stores] Empty response for {zip_code} — will retry next pass")
            return []
        if r.status_code in (403, 429):
            log.info(f"[Walmart Stores] Rate limited ({r.status_code}) — will retry next pass")
            return []
        data = r.json()
        stores = data.get("payload", {}).get("stores", []) or data.get("stores", [])
        result = []
        for s in stores:
            result.append({
                "id":      str(s.get("id") or s.get("storeId", "")),
                "name":    s.get("displayName") or s.get("name", "Walmart"),
                "city":    s.get("address", {}).get("city", ""),
                "state":   s.get("address", {}).get("state", ""),
                "address": s.get("address", {}).get("address", ""),
            })
        return result
    except ValueError:
        log.info(f"[Walmart Stores] Empty response for {zip_code} — will retry next pass")
        return []
    except requests.exceptions.Timeout:
        log.info(f"[Walmart Stores] Timed out for {zip_code} — will retry next pass")
        return []
    except Exception as e:
        log.warning(f"[Walmart Stores] Could not fetch stores for {zip_code}: {e}")
        return []


def check_walmart_instore(product: dict, zip_code: str, radius: int = 50) -> list[dict]:
    """Check Walmart in-store availability near a ZIP code."""
    item_id = product.get("walmart_item_id")
    if not item_id:
        return []

    stores = _get_walmart_nearby_stores(zip_code, radius)
    if not stores:
        log.warning("[Walmart In-Store] No stores found — skipping in-store check.")
        return []

    found = []
    url   = f"https://www.walmart.com/ip/{item_id}"

    for store in stores[:8]:
        store_id = store["id"]
        if not store_id:
            continue
        try:
            r = SESSION.get(
                WALMART_STORE_STATUS_API.format(store_id=store_id, item_id=item_id),
                headers=WALMART_STORE_HEADERS,
                timeout=10,
            )
            data = r.json()
            status = (
                data.get("status", "")
                or data.get("availabilityStatus", "")
                or data.get("pickupOption", "")
            )
            qty = data.get("quantity", 0) or data.get("availableQuantity", 0)
            in_stock = (
                str(status).upper() in ("IN_STOCK", "AVAILABLE", "PICK_UP_TODAY", "PICK_UP_TOMORROW")
                or (isinstance(qty, int) and qty > 0)
            )
            if in_stock:
                found.append({
                    "name":    f"{store['name']} — {store['city']}, {store['state']}",
                    "address": store["address"],
                    "qty":     qty or "✓",
                })
            time.sleep(0.5)
        except Exception as e:
            log.debug(f"[Walmart In-Store] Store {store_id}: {e}")

    if found:
        if not price_ok(product):
            return []
        stores_str = "\n".join(
            f"📍 {s['name']}\n    {s['address']} — qty: {s['qty']}" for s in found
        )
        send_discord_alert(product, "Walmart (In-Store)", "🟢 IN STORE", url, stores_str)
    else:
        log.info(f"[Walmart In-Store] {product['name']}: none in stock near {zip_code}")

    return found


# ── Pokémon Center ────────────────────────────────────────────────────────────

def check_pokemon_center(product: dict) -> bool:
    slug = product.get("pokemon_center_slug")
    if not slug:
        return False
    url = f"https://www.pokemoncenter.com/product/{slug}"
    try:
        r = SESSION.get(url, timeout=10)
        in_stock = (
            '"availability":"InStock"' in r.text
            or "Add to Cart" in r.text
            and "Out of Stock" not in r.text
        )
        if in_stock:
            live_price = None
            pc_price = re.search(r'"price"\s*:\s*([\d]+\.?\d*)', r.text)
            if pc_price:
                live_price = pc_price.group(1)
            if not price_ok(product, live_price):
                return False
            send_discord_alert(product, "Pokémon Center", "🟢 IN STOCK ONLINE", url,
                               "", live_price=live_price)
            return True
        log.info(f"[Pokemon Center] {product['name']}: out of stock")
    except Exception as e:
        log.warning(f"[Pokemon Center] {product['name']}: {e}")
    return False


# ── Best Buy ──────────────────────────────────────────────────────────────────
# Best Buy has a public availability API used by their own site.
# SKU = the number in the Best Buy product URL (e.g. /6570699.p → SKU 6570699)
# Store availability uses their store-locator + fulfillment API.

BESTBUY_ONLINE_API = (
    "https://www.bestbuy.com/api/tcfb/model.json"
    "?paths=%5B%5B%22shop%22%2C%22buttonstate%22%2C%22v5%22%2C%22item%22%2C%22skus%22%2C"
    "{sku}%2C%22conditions%22%2C%22NONE%22%2C%22destinationZip%22%2C%22{zip}%22%5D%5D"
    "&method=get"
)

BESTBUY_INSTORE_API = (
    "https://www.bestbuy.com/api/3.0/priceBlocks?skus={sku}"
)

BESTBUY_STORES_API = (
    "https://www.bestbuy.com/store-locator/ajax/storelocator"
    "?zipCode={zip}&radius={radius}&count=15"
)

def _get_bestbuy_nearby_stores(zip_code: str, radius: int = 50) -> list[dict]:
    """Fetch Best Buy store IDs near a ZIP code."""
    try:
        r = SESSION.get(
            BESTBUY_STORES_API.format(zip=zip_code, radius=radius),
            headers={"Accept": "application/json"},
            timeout=15,
        )
        if not r.text.strip():
            log.info(f"[Best Buy Stores] Empty response for {zip_code} — will retry next pass")
            return []
        if r.status_code in (403, 429):
            log.info(f"[Best Buy Stores] Rate limited ({r.status_code}) — will retry next pass")
            return []
        data = r.json()
        stores = data.get("stores", [])
        return [
            {
                "id":      s.get("storeId") or s.get("id", ""),
                "name":    s.get("longName") or s.get("name", "Best Buy"),
                "city":    s.get("city", ""),
                "state":   s.get("state", ""),
                "address": s.get("address", ""),
            }
            for s in stores
        ]
    except ValueError:
        log.info(f"[Best Buy Stores] Empty response for {zip_code} — will retry next pass")
        return []
    except requests.exceptions.Timeout:
        log.info(f"[Best Buy Stores] Timed out for {zip_code} — will retry next pass")
        return []
    except Exception as e:
        log.warning(f"[Best Buy Stores] Could not fetch stores for {zip_code}: {e}")
        return []

BESTBUY_STORE_STOCK_API = (
    "https://www.bestbuy.com/fulfillment/cart-payloads/sku/{sku}"
    "/store/{store_id}?additionalData=true"
)


def _bestbuy_url(product: dict) -> str:
    """
    Build the correct Best Buy product URL.
    Accepts either:
      - Alphanumeric ID:  JJG2TL34H9  → bestbuy.com/product/-/JJG2TL34H9
      - Numeric SKU:      6570699     → bestbuy.com/site/-/6570699.p  (legacy)
    """
    bb_id = product.get("bestbuy_sku", "")
    if bb_id.isdigit():
        return f"https://www.bestbuy.com/site/-/{bb_id}.p"
    return f"https://www.bestbuy.com/product/-/{bb_id}"


def _extract_bestbuy_numeric_sku(page_text: str) -> str | None:
    """Pull the numeric SKU from a Best Buy product page (shown as 'SKU: 1234567')."""
    match = re.search(r'"skuId"\s*:\s*"?(\d{6,8})"?', page_text)
    if match:
        return match.group(1)
    match = re.search(r'[Ss][Kk][Uu][\s:#]*(\d{6,8})', page_text)
    return match.group(1) if match else None


BESTBUY_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
    "DNT": "1",
}

def _parse_bestbuy_page(text: str, product: dict, url: str) -> bool:
    """Shared logic to parse a Best Buy page for stock signals."""
    bb_id = product.get("bestbuy_sku", "")
    if not bb_id.isdigit():
        numeric = _extract_bestbuy_numeric_sku(text)
        if numeric:
            product["_bestbuy_numeric_sku"] = numeric
            log.info(f"[Best Buy] Resolved numeric SKU for {product['name']}: {numeric}")

    out_signals = ["sold out", "coming soon", '"buttonState":"SOLD_OUT"',
                   '"buttonState":"COMING_SOON"', '"buttonState":"PRE_ORDER"',
                   "Currently unavailable"]
    in_signals  = ['"buttonState":"ADD_TO_CART"', '"buttonState":"SHIP_IT"',
                   "Add to Cart", '"availability":"InStock"']

    is_out = any(s.lower() in text.lower() for s in out_signals)
    is_in  = any(s.lower() in text.lower() for s in in_signals)

    if is_in and not is_out:
        live_price = None
        m = re.search(r'"currentPrice"\s*:\s*([\d]+\.?\d*)', text)
        if m:
            live_price = m.group(1)
        if not price_ok(product, live_price):
            return False
        send_discord_alert(product, "Best Buy (Online)", "🟡 IN STOCK ONLINE", url,
                           "", live_price=live_price)
        return True
    return False


def check_bestbuy_online(product: dict) -> bool:
    """Check Best Buy online availability using a real browser (Playwright)."""
    bb_id = product.get("bestbuy_sku")
    if not bb_id:
        return False
    url = _bestbuy_url(product)

    # ── Playwright path (preferred — real browser with stealth mode) ──
    if PLAYWRIGHT_AVAILABLE:
        try:
            with sync_playwright() as p:
                # Try real Edge first (best fingerprint) — fall back to Chromium
                try:
                    browser = p.chromium.launch(
                        channel="msedge",   # Uses your actual installed Edge
                        headless=True,
                        args=[
                            "--disable-blink-features=AutomationControlled",
                            "--no-sandbox",
                            "--disable-gpu",
                            "--window-size=1280,800",
                            "--disable-http2",
                            "--disable-quic",
                        ]
                    )
                    log.debug("[Best Buy] Using real Chrome browser")
                except Exception:
                    browser = p.chromium.launch(
                        headless=True,
                        args=[
                            "--disable-blink-features=AutomationControlled",
                            "--no-sandbox",
                            "--disable-dev-shm-usage",
                            "--disable-gpu",
                            "--window-size=1280,800",
                            "--disable-http2",
                            "--disable-quic",
                        ]
                    )
                    log.debug("[Best Buy] Real Chrome not found — using bundled Chromium")
                ctx = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 800},
                    locale="en-US",
                    timezone_id="America/New_York",
                    java_script_enabled=True,
                    extra_http_headers={
                        "Accept-Language": "en-US,en;q=0.9",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    }
                )
                page = ctx.new_page()
                Stealth().apply_stealth_sync(page)

                # Block images/fonts/media to load faster
                page.route("**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf,otf,mp4,webm}",
                           lambda r: r.abort())

                # Step 1: Visit homepage first to pick up cookies (mimics real browsing)
                try:
                    page.goto("https://www.bestbuy.com", wait_until="domcontentloaded", timeout=20000)
                    time.sleep(2)
                except Exception:
                    pass  # If homepage fails, still try the product page

                # Step 2: Navigate to product page
                page.goto(url, wait_until="domcontentloaded", timeout=35000)

                try:
                    page.wait_for_selector(
                        ".add-to-cart-button, .btn-disabled, [data-button-state]",
                        timeout=8000
                    )
                except PWTimeout:
                    pass

                text = page.content()
                browser.close()

            log.info(f"[Best Buy] {product['name']}: page loaded via stealth browser ({len(text)} chars)")
            return _parse_bestbuy_page(text, product, url)

        except PWTimeout:
            log.warning(f"[Best Buy] {product['name']}: browser timed out — skipping this pass")
            return False
        except Exception as e:
            log.warning(f"[Best Buy] {product['name']}: browser error: {e} — falling back to requests")

    # ── Fallback: plain requests (less reliable against bot detection) ──
    try:
        r = SESSION.get(url, headers=BESTBUY_HEADERS, timeout=20)
        if r.status_code in (403, 429) or "captcha" in r.url.lower():
            log.warning(f"[Best Buy] {product['name']}: blocked — skipping this pass")
            return False
        return _parse_bestbuy_page(r.text, product, url)
    except requests.exceptions.Timeout:
        log.warning(f"[Best Buy] {product['name']}: timed out — will retry next pass")
    except Exception as e:
        log.warning(f"[Best Buy Online] {product['name']}: {e}")
    return False


def check_bestbuy_instore(product: dict, zip_code: str, radius: int = 50) -> list[dict]:
    """Check Best Buy in-store availability near a ZIP code."""
    sku = (
        product.get("bestbuy_numeric_sku")
        or product.get("_bestbuy_numeric_sku")
    )
    if not sku:
        log.info(f"[Best Buy In-Store] {product['name']}: no numeric SKU — skipping")
        return []
    if not str(sku).isdigit():
        log.info(f"[Best Buy In-Store] {product['name']}: SKU '{sku}' is not numeric — skipping")
        return []

    stores = _get_bestbuy_nearby_stores(zip_code, radius)
    if not stores:
        log.info("[Best Buy In-Store] No stores found — will retry next pass.")
        return []

    found = []
    url   = f"https://www.bestbuy.com/site/-/{sku}.p"

    for store in stores[:8]:   # cap at 8 stores to avoid rate limits
        store_id = store["id"]
        if not store_id:
            continue
        try:
            r = SESSION.get(
                BESTBUY_STORE_STOCK_API.format(sku=sku, store_id=store_id),
                headers={"Accept": "application/json"},
                timeout=10,
            )
            data = r.json()
            # Response varies — look for common availability fields
            avail = (
                data.get("availability", {}).get("storeAvailability")
                or data.get("storePickup", {}).get("availability")
                or data.get("availabilityType", "")
            )
            qty = (
                data.get("storeAvailability", {}).get("quantity")
                or data.get("quantity", 0)
            )
            in_stock = (
                str(avail).upper() in ("AVAILABLE", "IN_STOCK", "TRUE", "1")
                or (isinstance(qty, int) and qty > 0)
            )
            if in_stock:
                found.append({
                    "name":    f"{store['name']} — {store['city']}, {store['state']}",
                    "qty":     qty or "✓",
                    "address": store["address"],
                })
            time.sleep(0.5)   # be polite between store checks
        except Exception as e:
            log.debug(f"[Best Buy In-Store] Store {store_id}: {e}")

    if found:
        if not price_ok(product):
            return []
        stores_str = "\n".join(
            f"📍 {s['name']} — qty: {s['qty']}" for s in found
        )
        send_discord_alert(product, "Best Buy (In-Store)", "🟡 IN STORE", url, stores_str)
    else:
        log.info(f"[Best Buy In-Store] {product['name']}: none in stock near {zip_code}")

    return found


# ── Generic URL scraper ───────────────────────────────────────────────────────
# For any retailer — checks for "Add to Cart" / "In Stock" keywords.

def check_generic_url(product: dict) -> bool:
    url = product.get("url")
    if not url:
        return False
    in_stock_keywords  = ["add to cart", "in stock", "add-to-cart", "addtocart"]
    out_stock_keywords = ["out of stock", "sold out", "currently unavailable",
                          "notify me when available"]
    try:
        r = SESSION.get(url, timeout=12)
        text_lower = r.text.lower()
        is_out = any(kw in text_lower for kw in out_stock_keywords)
        is_in  = any(kw in text_lower for kw in in_stock_keywords)
        if is_in and not is_out:
            retailer = product.get("retailer", url.split("/")[2])
            send_discord_alert(product, retailer, "🟢 IN STOCK ONLINE", url)
            return True
        log.info(f"[Generic] {product['name']} @ {url}: out of stock")
    except Exception as e:
        log.warning(f"[Generic] {product['name']}: {e}")
    return False


# ── GameStop ──────────────────────────────────────────────────────────────────
# GameStop's product pages include availability data in JSON-LD and page signals.
# gamestop_product_id = the number at the end of the GameStop URL
# e.g. gamestop.com/toys-games/pokemon/pokemon-.../314245.html → ID = 314245

def check_gamestop(product: dict) -> bool:
    """Check GameStop online availability."""
    gs_id = product.get("gamestop_product_id")
    if not gs_id:
        return False

    url = f"https://www.gamestop.com/products/{gs_id}.html"
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        r = SESSION.get(url, headers=headers, timeout=12)
        text = r.text

        out_signals = [
            "not available",
            "out of stock",
            "sold out",
            '"availability":"http://schema.org/OutOfStock"',
            '"availability":"OutOfStock"',
            "NotAvailable",
        ]
        in_signals = [
            '"availability":"http://schema.org/InStock"',
            '"availability":"InStock"',
            "add-to-cart",
            "Add to Cart",
            "data-buttonstate=\"addToCart\"",
        ]

        is_out = any(s.lower() in text.lower() for s in out_signals)
        is_in  = any(s.lower() in text.lower() for s in in_signals)

        # Extract live price
        live_price = None
        price_match = re.search(r'"price"\s*:\s*"?([\d]+\.?\d*)"?', text)
        if price_match:
            live_price = price_match.group(1)

        if is_in and not is_out:
            if not price_ok(product, live_price):
                return False
            send_discord_alert(product, "GameStop", "🔴 IN STOCK ONLINE", url,
                               "", live_price=live_price)
            return True

        log.info(f"[GameStop] {product['name']}: out of stock")
    except requests.exceptions.Timeout:
        log.info(f"[GameStop] {product['name']}: timed out — will retry next pass")
    except Exception as e:
        log.warning(f"[GameStop] {product['name']}: {e}")
    return False


# ── State tracker (avoid duplicate alerts) ───────────────────────────────────

_state: dict[str, str] = {}   # product_key -> last known hash

def _key(product: dict, store: str) -> str:
    return hashlib.md5(f"{product['name']}:{store}".encode()).hexdigest()

def already_alerted(product: dict, store: str, status: str) -> bool:
    k = _key(product, store)
    if _state.get(k) == status:
        return True
    _state[k] = status
    return False


# ── Main loop ─────────────────────────────────────────────────────────────────

def load_products() -> dict:
    with open(PRODUCTS_FILE) as f:
        return json.load(f)


def check_product(product: dict, zip_code: str, radius: int):
    """Run all store checks for a single product. Called in parallel."""
    name = product.get("name", "Unknown")
    log.info(f"Checking: {name}")

    if product.get("target_tcin"):
        check_target_online(product)
        check_target_instore(product, zip_code, radius)

    if product.get("walmart_item_id"):
        check_walmart_online(product)
        check_walmart_instore(product, zip_code, radius)

    if product.get("pokemon_center_slug"):
        check_pokemon_center(product)

    if product.get("bestbuy_sku"):
        check_bestbuy_online(product)
        time.sleep(4)
        check_bestbuy_instore(product, zip_code, radius)

    if product.get("gamestop_product_id"):
        check_gamestop(product)

    if product.get("url"):
        check_generic_url(product)


def run():
    log.info("=" * 60)
    log.info("  PokéStock Monitor started")
    log.info(f"  Interval : {CHECK_INTERVAL}s")
    log.info(f"  Products : {PRODUCTS_FILE}")
    log.info(f"  Webhook  : {'SET' if DISCORD_WEBHOOK else 'NOT SET (console only)'}")
    log.info("=" * 60)

    while True:
        try:
            config   = load_products()
            products = config.get("products", [])
            zip_code = config.get("zip_code", "10001")
            radius   = config.get("search_radius_miles", 50)
            workers  = min(len(products), 6)   # max 6 threads

            log.info(f"Scanning {len(products)} products in parallel ({workers} threads)...")
            start = time.time()

            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(check_product, product, zip_code, radius): product
                    for product in products
                }
                for future in as_completed(futures):
                    product = futures[future]
                    try:
                        future.result()
                    except Exception as e:
                        log.error(f"Error checking {product.get('name')}: {e}", exc_info=True)

            elapsed = round(time.time() - start, 1)
            log.info(f"Scan complete in {elapsed}s. Next check in {CHECK_INTERVAL}s...")

        except FileNotFoundError:
            log.error(f"Products file not found: {PRODUCTS_FILE}")
        except json.JSONDecodeError as e:
            log.error(f"Bad JSON in products file: {e}")
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            break
        except Exception as e:
            log.error(f"Unexpected error: {e}", exc_info=True)

        log.info(f"Scan complete. Next check in {CHECK_INTERVAL}s...")
        time.sleep(CHECK_INTERVAL)


def run_test_alert():
    """Fire a fake in-stock alert to Discord so you can preview what notifications look like."""
    log.info("Sending test alerts to Discord...")

    fake_product = {
        "name": "Chaos Rising Booster Bundle",
        "price": "$24.99",
        "image": "",
    }

    # Test 1 — Online restock
    send_discord_alert(
        fake_product,
        "Target (Online)",
        "🟢 IN STOCK ONLINE",
        "https://www.target.com",
        "Price: $24.99 | Quantity: 12"
    )
    time.sleep(1)

    # Test 2 — In-store restock with addresses
    stores_str = (
        "📍 Target — Charlotte, NC\n"
        "    4400 Sharon Rd — qty: 3\n"
        "📍 Target — Concord, NC\n"
        "    8280 Concord Mills Blvd — qty: 1"
    )
    send_discord_alert(
        fake_product,
        "Target (In-Store)",
        "🟢 IN STORE",
        "https://www.target.com",
        stores_str
    )
    time.sleep(1)

    # Test 3 — Walmart in-store
    walmart_str = (
        "📍 Walmart Supercenter — Charlotte, NC\n"
        "    4640 South Blvd — qty: 2\n"
        "📍 Walmart Supercenter — Pineville, NC\n"
        "    500 Towne Centre Blvd — qty: 4"
    )
    send_discord_alert(
        fake_product,
        "Walmart (In-Store)",
        "🟢 IN STORE",
        "https://www.walmart.com",
        walmart_str
    )
    time.sleep(1)

    # Test 4 — Price filter example (won't alert — just logged)
    log.info("[Price Filter] TEST: $179.99 exceeds max $29.99 — skipping alert (this is correct behavior)")

    log.info("✅ Test alerts sent! Check your Discord channel.")


if __name__ == "__main__":
    if "--test-alert" in sys.argv:
        run_test_alert()
    else:
        run()
