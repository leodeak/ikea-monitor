#!/usr/bin/env python3
"""Monitor dell'Angolo della circolarità IKEA (Corsico, San Giuliano, Carugate).

Uso:
    python monitor.py          # controllo normale, avvisa su Telegram
    python monitor.py test     # invia solo un messaggio Telegram di prova
    python monitor.py debug    # non avvisa; salva screenshot e dati in ./debug

Variabili d'ambiente: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

PAGE_URL = (
    "https://www.ikea.com/it/it/second-hand/buy-from-ikea/"
    "#/milano+carugate-milano+corsico-milano+san+giuliano?sort=id-desc"
)
TARGET_LABEL = "VESTERÖY materasso a molle insacchettate, rigido/azzurro, 140x200 cm"
TARGET_PRODUCT_URL = (
    "https://www.ikea.com/it/it/p/"
    "vesteroey-materasso-a-molle-insacchettate-rigido-azzurro-30450609/"
)

STATE_FILE = Path("state.json")
DEBUG_DIR = Path("debug")
MAX_SEEN = 5000
MAX_ITEMS_PER_MESSAGE = 10

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Chiavi probabili nei dati JSON della pagina (si prova la prima che esiste).
ID_KEYS = ["id", "uid", "productId", "itemId", "articleId", "articleNumber",
           "articleNo", "itemNo", "itemNumber", "sku", "code"]
NAME_KEYS = ["name", "title", "productName", "productTitle", "itemName", "typeName"]
PRICE_KEYS = ["price", "salesPrice", "sellingPrice", "currentPrice",
              "priceNumeral", "prices", "amount"]
STORE_KEYS = ["store", "storeName", "shopName", "shop", "location", "storeId"]
IMG_KEYS = ["image", "imageUrl", "img", "images", "mainImage", "picture", "photo"]
URL_KEYS = ["url", "pipUrl", "link", "href"]


# ----------------------------------------------------------------- utilità

def norm(text):
    text = str(text).lower()
    for a, b in (("ö", "o"), ("ä", "a"), ("å", "a")):
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", text)


def first(d, keys):
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return None


def to_text(v):
    if v is None:
        return ""
    if isinstance(v, dict):
        for k in ("name", "text", "formatted", "value", "current", "label"):
            if k in v and v[k] not in (None, "", {}):
                return to_text(v[k])
        return json.dumps(v, ensure_ascii=False)[:80]
    if isinstance(v, list):
        return to_text(v[0]) if v else ""
    return str(v)


def is_target(item):
    """True se l'articolo è il materasso VESTERÖY 140x200 (cod. 304.506.09)."""
    blob = norm(json.dumps(item["raw"], ensure_ascii=False)) + " " + norm(item["name"])
    if re.search(r"(?<!\d)304[.\s]?506[.\s]?09(?!\d)", blob):
        return True
    compact = re.sub(r"\s*x\s*", "x", blob)
    return bool(re.search(r"vester(oe|o)y", compact)) and "140x200" in compact


def item_key(item):
    return f"{item['id']}|{item['store']}"


def absolute(url):
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://www.ikea.com" + url
    return url


# ------------------------------------------------- lettura dati dalla pagina

def looks_like_item(d):
    return (
        first(d, ID_KEYS) is not None
        and first(d, NAME_KEYS) is not None
        and (first(d, PRICE_KEYS) is not None or first(d, IMG_KEYS) is not None)
    )


def find_item_lists(obj, depth=0):
    """Cerca nei JSON le liste di oggetti che sembrano prodotti."""
    if depth > 8:
        return []
    if isinstance(obj, list):
        dicts = [x for x in obj if isinstance(x, dict)]
        good = [x for x in dicts if looks_like_item(x)]
        if good and len(good) >= max(1, len(dicts) // 2):
            return [good]
        found = []
        for x in obj:
            found += find_item_lists(x, depth + 1)
        return found
    if isinstance(obj, dict):
        found = []
        for v in obj.values():
            found += find_item_lists(v, depth + 1)
        return found
    return []


def make_item(d, source):
    return {
        "id": str(first(d, ID_KEYS)),
        "name": to_text(first(d, NAME_KEYS)),
        "price": to_text(first(d, PRICE_KEYS)),
        "store": to_text(first(d, STORE_KEYS)),
        "url": to_text(first(d, URL_KEYS)),
        "raw": d,
        "source": source,
    }


def items_from_json(bodies):
    items = {}
    for url, data in bodies:
        for lst in find_item_lists(data):
            for d in lst:
                it = make_item(d, url)
                items.setdefault(item_key(it), it)
    return list(items.values())


def items_from_dom(page):
    """Piano B: legge i link ai prodotti direttamente dalla pagina."""
    items = {}
    for a in page.query_selector_all("a[href*='/p/']"):
        href = a.get_attribute("href") or ""
        text = (a.inner_text() or "").strip()
        if not text:
            continue
        m = re.search(r"(\d{8})/?(?:[?#].*)?$", href)
        it = {
            "id": m.group(1) if m else href,
            "name": text.splitlines()[0][:120],
            "price": "",
            "store": "",
            "url": href,
            "raw": {"text": text, "href": href},
            "source": "dom",
        }
        items.setdefault(item_key(it), it)
    return list(items.values())


def fetch_items(debug=False):
    from playwright.sync_api import sync_playwright  # import qui per poter testare il resto

    responses = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(
            locale="it-IT",
            timezone_id="Europe/Rome",
            viewport={"width": 1366, "height": 900},
            user_agent=UA,
        )
        page = ctx.new_page()
        page.on("response", lambda r: responses.append(r))
        page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)

        for sel in ("#onetrust-accept-btn-handler",
                    "button:has-text('Accetta tutti')",
                    "button:has-text('Accetta')"):
            try:
                page.click(sel, timeout=2500)
                break
            except Exception:
                pass

        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        for _ in range(10):  # scorre per far caricare altri prodotti
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(1200)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

        bodies, listing = [], []
        for r in responses:
            try:
                ctype = r.headers.get("content-type", "")
                listing.append(f"{r.status} {ctype[:30]:30} {r.url}")
                if "json" not in ctype:
                    continue
                bodies.append((r.url, r.json()))
            except Exception:
                continue

        items = items_from_json(bodies)
        method = "json"
        if not items:
            items = items_from_dom(page)
            method = "dom"
        print(f"Metodo di lettura: {method}")

        if debug:
            DEBUG_DIR.mkdir(exist_ok=True)
            page.screenshot(path=str(DEBUG_DIR / "pagina.png"), full_page=True)
            (DEBUG_DIR / "testo_pagina.txt").write_text(
                page.inner_text("body")[:20000], encoding="utf-8")
            (DEBUG_DIR / "risposte.txt").write_text("\n".join(listing), encoding="utf-8")
            for i, (url, data) in enumerate(bodies[:40]):
                raw = json.dumps(data, ensure_ascii=False)[:400000]
                (DEBUG_DIR / f"json_{i:02d}.txt").write_text(url + "\n" + raw, encoding="utf-8")
            sample = [{k: v for k, v in it.items() if k != "raw"} for it in items[:15]]
            (DEBUG_DIR / "articoli_letti.json").write_text(
                json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8")

        browser.close()
    return items


# ------------------------------------------------------------------ Telegram

def telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("Telegram non configurato (mancano i segreti). Messaggio:\n" + text)
        return False
    data = urllib.parse.urlencode({
        "chat_id": chat, "text": text, "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    try:
        urllib.request.urlopen(req, timeout=20).read()
        return True
    except Exception as e:  # il token non viene mai stampato
        print("Errore Telegram:", type(e).__name__, e)
        return False


def describe(item):
    parts = [item["name"] or item["id"]]
    if item["price"]:
        parts.append(item["price"])
    if item["store"]:
        parts.append(item["store"])
    line = "• " + " — ".join(parts)
    link = absolute(item["url"])
    return line + (f"\n  {link}" if link else "")


# --------------------------------------------------------------------- stato

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"initialized": False, "seen": [], "target_alerted": [], "fail_alerted": False}


def save_state(state):
    state["seen"] = state["seen"][-MAX_SEEN:]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------------- main

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"

    if mode == "test":
        ok = telegram("✅ Test riuscito: il monitor dell'Angolo della circolarità IKEA "
                      "può scriverti su Telegram.")
        sys.exit(0 if ok else 1)

    debug = mode == "debug"
    items = fetch_items(debug=debug)
    print(f"Articoli letti: {len(items)}")
    for it in items[:3]:
        print("  esempio:", {k: v for k, v in it.items() if k != "raw"})
    if debug:
        print("Modalità debug: nessun avviso inviato, stato non modificato.")
        return

    state = load_state()

    if not items:
        print("::warning::Nessun articolo letto dalla pagina")
        if not state.get("fail_alerted"):
            if telegram("⚠️ Monitor IKEA: non riesco a leggere nessun articolo. "
                        "Può essere che l'Angolo sia vuoto o che la pagina sia cambiata. "
                        "Ti scrivo una sola volta finché non torna a funzionare."):
                state["fail_alerted"] = True
                save_state(state)
        return
    state["fail_alerted"] = False

    keyed = {item_key(i): i for i in items}
    seen = set(state.get("seen", []))
    alerted = set(state.get("target_alerted", []))
    first_run = not state.get("initialized")

    new_items = [i for k, i in keyed.items() if k not in seen]
    new_targets = [i for k, i in keyed.items() if is_target(i) and k not in alerted]
    target_keys = {item_key(i) for i in new_targets}
    new_regular = [i for i in new_items if item_key(i) not in target_keys]

    ok = True
    if first_run:
        ok &= telegram(f"✅ Monitor attivo. Ho memorizzato {len(items)} articoli già presenti. "
                       "Da ora ti avviso solo per quelli nuovi.")
    elif new_regular:
        for start in range(0, len(new_regular), MAX_ITEMS_PER_MESSAGE):
            chunk = new_regular[start:start + MAX_ITEMS_PER_MESSAGE]
            head = f"🆕 {len(new_regular)} nuovi articoli all'Angolo della circolarità"
            if start:
                head += " (continua)"
            ok &= telegram(head + ":\n\n" + "\n".join(describe(i) for i in chunk)
                           + f"\n\nApri l'elenco: {PAGE_URL}")

    for it in new_targets:
        ok &= telegram(f"🎯 TROVATO! {TARGET_LABEL}\n\n{describe(it)}\n\n"
                       f"Prenotalo subito: {PAGE_URL}\nScheda prodotto: {TARGET_PRODUCT_URL}")

    if not ok:
        print("Almeno un avviso non è partito: stato non aggiornato, riprovo al prossimo giro.")
        sys.exit(1)

    seen_list = list(state.get("seen", []))
    seen_list += [k for k in keyed if k not in seen]
    state.update({"initialized": True, "seen": seen_list,
                  "target_alerted": list(alerted | target_keys)})
    save_state(state)
    print(f"Nuovi: {len(new_regular)} | materasso: {len(new_targets)}")


if __name__ == "__main__":
    main()
