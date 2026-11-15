#!/usr/bin/env python3
"""Monitor dell'Angolo della circolarità IKEA -> avvisi Telegram.

Uso:
    python monitor.py          # controllo normale, avvisa su Telegram
    python monitor.py test     # invia solo un messaggio Telegram di prova
    python monitor.py debug    # non avvisa; salva i dati letti in ./debug

Variabili d'ambiente: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# Negozi da controllare (codici IKEA). Togli una riga per escludere un negozio.
STORES = {
    "455": "Corsico",
    "295": "San Giuliano",
    "233": "Carugate",
}

PAGE_URL = (
    "https://www.ikea.com/it/it/second-hand/buy-from-ikea/"
    "#/milano+carugate-milano+corsico-milano+san+giuliano?sort=id-desc"
)
# Pagina della singola offerta: elenco dei 3 negozi + "/" + numero offerta
OFFER_BASE_URL = (
    "https://www.ikea.com/it/it/second-hand/buy-from-ikea/"
    "#/milano+carugate-milano+corsico-milano+san+giuliano/"
)
API_URL = "https://web-api.ikea.com/circular/circular-asis/offers/grouped/search"
API_PAGES = 3        # 3 pagine x 32 = i 96 gruppi più recenti
API_PAGE_SIZE = 32

TARGET_ARTICLE = "30450609"
TARGET_LABEL = "VESTERÖY materasso a molle insacchettate, rigido/azzurro, 140x200 cm"

STATE_FILE = Path("state.json")
DEBUG_DIR = Path("debug")
MAX_SEEN = 5000
MAX_ITEMS_PER_MESSAGE = 10
MAX_PHOTOS = 5  # fino a 5 nuovi articoli per giro: foto singole; oltre, elenco di testo

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# ----------------------------------------------------------------- utilità

def norm(text):
    text = str(text).lower()
    for a, b in (("ö", "o"), ("ä", "a"), ("å", "a")):
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", text)


def money(v):
    if v is None:
        return ""
    s = f"{float(v):.2f}".replace(".", ",")
    if s.endswith(",00"):
        s = s[:-3]
    return "€" + s


def is_target(item):
    """True se è il materasso VESTERÖY 140x200 (articolo 304.506.09)."""
    if TARGET_ARTICLE in item["articles"]:
        return True
    text = norm(item["title"] + " " + item["description"])
    text = re.sub(r"\s*x\s*", "x", text)
    return "vester" in text and "materasso" in text and "140x200" in text


def flatten(groups):
    """Trasforma i gruppi dell'API in una lista di offerte singole."""
    items = {}
    for g in groups:
        store_id = str(g.get("storeId", ""))
        if STORES and store_id not in STORES:
            continue
        for o in g.get("offers") or []:
            oid = str(o.get("id"))
            if oid in ("None", ""):
                continue
            items[oid] = {
                "id": oid,
                "store": STORES.get(store_id, store_id),
                "title": g.get("title") or "",
                "description": o.get("description") or g.get("description") or "",
                "articles": [str(a) for a in g.get("articleNumbers") or []],
                "price": o.get("price"),
                "original": g.get("originalPrice"),
                "condition": o.get("productConditionTitle") or "",
                "extra": o.get("additionalInfo") or "",
                "image": g.get("heroImage") or "",
                "link": (OFFER_BASE_URL + str(o["offerNumber"])
                         if o.get("offerNumber") else PAGE_URL),
            }
    return list(items.values())


def telegram_photo(image_url, caption):
    """Invia una foto con didascalia. Se non riesce, ripiega sul solo testo."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat and image_url):
        return telegram(caption)
    data = urllib.parse.urlencode({
        "chat_id": chat, "photo": image_url, "caption": caption[:1000],
    }).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendPhoto", data=data)
    try:
        urllib.request.urlopen(req, timeout=25).read()
        return True
    except Exception as e:
        print("Foto non inviata, mando solo testo:", type(e).__name__)
        return telegram(caption)


def describe(item):
    head = item["title"]
    if item["description"]:
        head += " — " + item["description"]
    price = money(item["price"])
    if item["original"] and item["price"] is not None and item["original"] > item["price"]:
        price += f" (era {money(item['original'])})"
    details = " · ".join(x for x in (price, item["condition"], item["store"]) if x)
    line = f"• {head}\n  {details}"
    if item["extra"]:
        line += f"\n  Nota: {item['extra']}"
    if item.get("link"):
        line += f"\n  {item['link']}"
    return line


# -------------------------------------------------------- lettura dei dati

def fetch_api():
    """Metodo veloce: chiede i dati direttamente a IKEA. Restituisce i gruppi o None."""
    groups = []
    for page in range(API_PAGES):
        qs = urllib.parse.urlencode({
            "languageCode": "it", "size": API_PAGE_SIZE,
            "storeIds": ",".join(STORES), "column": "id",
            "direction": "desc", "page": page,
        })
        req = urllib.request.Request(f"{API_URL}?{qs}", headers={
            "User-Agent": UA,
            "Accept": "application/json",
            "Accept-Language": "it-IT,it;q=0.9",
            "Origin": "https://www.ikea.com",
            "Referer": "https://www.ikea.com/",
        })
        try:
            data = json.loads(urllib.request.urlopen(req, timeout=25).read())
        except Exception as e:
            print(f"API diretta non raggiungibile (pagina {page}): {type(e).__name__} {e}")
            return None if page == 0 else groups
        groups += data.get("content") or []
        if data.get("last"):
            break
    return groups


def fetch_browser(debug=False):
    """Piano B: apre la pagina come un browser e legge la risposta dell'API."""
    from playwright.sync_api import sync_playwright

    def launch(p):
        return p.chromium.launch(headless=True, args=["--no-sandbox"])

    groups, seen_urls = [], []
    with sync_playwright() as p:
        try:
            browser = launch(p)
        except Exception:
            print("Installo Chromium...")
            subprocess.run([sys.executable, "-m", "playwright", "install", "--with-deps",
                            "chromium"], check=True)
            browser = launch(p)
        ctx = browser.new_context(locale="it-IT", timezone_id="Europe/Rome",
                                  viewport={"width": 1366, "height": 900}, user_agent=UA)
        page = ctx.new_page()
        captured = []

        def on_response(r):
            if "offers/grouped/search" in r.url:
                try:
                    captured.append((r.url, r.json()))
                except Exception:
                    pass

        page.on("response", on_response)
        page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
        for sel in ("#onetrust-accept-btn-handler", "button:has-text('Accetta tutti')"):
            try:
                page.click(sel, timeout=2500)
                break
            except Exception:
                pass
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        page.wait_for_timeout(3000)

        for url, data in captured:
            seen_urls.append(url)
            groups += data.get("content") or []

        if debug:
            DEBUG_DIR.mkdir(exist_ok=True)
            page.screenshot(path=str(DEBUG_DIR / "pagina.png"), full_page=True)
            (DEBUG_DIR / "testo_pagina.txt").write_text(page.inner_text("body")[:20000],
                                                        encoding="utf-8")
        browser.close()
    print(f"Risposte API catturate dal browser: {len(seen_urls)}")
    return groups


def fetch_items(debug=False):
    groups = fetch_api()
    method = "api"
    if not groups:
        groups = fetch_browser(debug=debug)
        method = "browser"
    print(f"Metodo di lettura: {method} | gruppi: {len(groups)}")
    items = flatten(groups)
    if debug:
        DEBUG_DIR.mkdir(exist_ok=True)
        (DEBUG_DIR / "articoli_letti.json").write_text(
            json.dumps(items[:40], ensure_ascii=False, indent=2), encoding="utf-8")
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
        print("Errore Telegram:", type(e).__name__)
        return False


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
    print(f"Offerte lette: {len(items)}")
    for it in items[:3]:
        print("  esempio:", it["store"], it["title"], it["price"], it["id"])
    if debug:
        print("Modalità debug: nessun avviso inviato, stato non modificato.")
        return

    state = load_state()

    if not items:
        print("::warning::Nessuna offerta letta")
        if not state.get("fail_alerted"):
            if telegram("⚠️ Monitor IKEA: non riesco a leggere nessun articolo. "
                        "Può essere che l'Angolo sia vuoto o che IKEA abbia cambiato qualcosa. "
                        "Ti scrivo una sola volta finché non torna a funzionare."):
                state["fail_alerted"] = True
                save_state(state)
        return
    state["fail_alerted"] = False

    seen = set(state.get("seen", []))
    alerted = set(state.get("target_alerted", []))
    first_run = not state.get("initialized")

    new_items = [i for i in items if i["id"] not in seen]
    new_targets = [i for i in items if is_target(i) and i["id"] not in alerted]
    target_ids = {i["id"] for i in new_targets}
    new_regular = [i for i in new_items if i["id"] not in target_ids]

    ok = True
    if first_run:
        ok &= telegram(f"✅ Monitor attivo. Ho memorizzato {len(items)} offerte già presenti. "
                       "Da ora ti avviso solo per quelle nuove.")
    elif new_regular and len(new_regular) <= MAX_PHOTOS:
        # pochi articoli: un messaggio con foto per ciascuno
        for i in new_regular:
            ok &= telegram_photo(i["image"], "🆕 Nuovo all'Angolo della circolarità\n\n"
                                 + describe(i))
    elif new_regular:
        # molti articoli insieme: elenco di testo
        for start in range(0, len(new_regular), MAX_ITEMS_PER_MESSAGE):
            chunk = new_regular[start:start + MAX_ITEMS_PER_MESSAGE]
            head = f"🆕 {len(new_regular)} nuovi articoli all'Angolo della circolarità"
            if start:
                head += " (continua)"
            ok &= telegram(head + ":\n\n" + "\n".join(describe(i) for i in chunk)
                           + f"\n\nApri l'elenco: {PAGE_URL}")

    for it in new_targets:
        ok &= telegram_photo(it["image"], f"🎯 TROVATO! {TARGET_LABEL}\n\n{describe(it)}\n\n"
                             f"Elenco completo: {PAGE_URL}")

    if not ok:
        print("Almeno un avviso non è partito: stato non aggiornato, riprovo al prossimo giro.")
        sys.exit(1)

    seen_list = list(state.get("seen", []))
    seen_list += [i["id"] for i in items if i["id"] not in seen]
    state.update({"initialized": True, "seen": seen_list,
                  "target_alerted": list(alerted | target_ids)})
    save_state(state)
    print(f"Nuovi: {len(new_regular)} | materasso: {len(new_targets)}")


if __name__ == "__main__":
    main()
