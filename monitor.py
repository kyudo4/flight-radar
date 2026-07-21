#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Flight Radar — monitor lotów premium do Azji (business/first).

Uruchamiany co godzinę przez launchd. Bez zewnętrznych zależności (czysty stdlib).

Komendy:
  python3 monitor.py run      — pojedynczy przebieg (domyślna)
  python3 monitor.py setup    — wykrywa chat_id po wysłaniu /start do bota
  python3 monitor.py test     — wysyła testowe powiadomienie na Telegram
  python3 monitor.py prefs    — pokazuje wyuczone preferencje
"""

import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(BASE, "state")
CONFIG_PATH = os.path.join(BASE, "config.json")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")


# ---------------------------------------------------------------- utilities

def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        path = os.path.join(STATE, "log.txt")
        if os.path.exists(path) and os.path.getsize(path) > 2_000_000:
            os.rename(path, path + ".old")
        with open(path, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def state_file(name, default):
    return load_json(os.path.join(STATE, name), default)


def save_state(name, data):
    save_json(os.path.join(STATE, name), data)


def http_get(url, headers=None, timeout=25, data=None, method=None):
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "*/*")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


# ---------------------------------------------------------------- FX rates

def get_fx(cfg):
    """Kursy walut -> PLN, odświeżane raz na dobę, z fallbackiem z configu."""
    cache = state_file("fx.json", {})
    if cache.get("date") == datetime.now().strftime("%Y-%m-%d"):
        return cache["rates"]
    rates = dict(cfg.get("fx_fallback", {}))
    try:
        _, body = http_get(
            "https://api.frankfurter.dev/v1/latest?base=PLN&symbols="
            + ",".join(rates.keys()), timeout=15)
        data = json.loads(body)
        for cur, val in data.get("rates", {}).items():
            if val:
                rates[cur] = round(1.0 / val, 4)  # 1 jednostka waluty w PLN
        save_state("fx.json", {"date": datetime.now().strftime("%Y-%m-%d"),
                               "rates": rates})
    except Exception as e:
        log("FX: nie udało się pobrać kursów (%s), używam fallback" % e)
    return rates


# ---------------------------------------------------------------- Telegram

def tg_api(cfg, method, payload):
    token = cfg["telegram"].get("bot_token", "")
    if not token:
        return None
    url = "https://api.telegram.org/bot%s/%s" % (token, method)
    body = json.dumps(payload).encode()
    try:
        _, resp = http_get(url, headers={"Content-Type": "application/json"},
                           data=body, method="POST", timeout=20)
        return json.loads(resp)
    except Exception as e:
        log("Telegram %s: błąd %s" % (method, e))
        return None


def chat_ids(cfg):
    """Odbiorcy: sekret/config (chat_ids, chat_id) + samodzielnie zapisani
    w state/recipients.json (każdy, kto kliknął Start — np. kumpel)."""
    tg = cfg["telegram"]
    ids = list(tg.get("chat_ids") or [])
    if tg.get("chat_id") and tg["chat_id"] not in ids:
        ids.append(tg["chat_id"])
    for cid in state_file("recipients.json", {}).get("ids", []):
        if cid not in ids:
            ids.append(cid)
    return ids


FEEDBACK_BUTTONS = [
    [{"text": "👍 Kupiłbym", "cb": "buy"}, {"text": "💸 Za drogo", "cb": "expensive"}],
    [{"text": "🙅 Nie interesuje", "cb": "skip"}, {"text": "⏱ Za długo", "cb": "toolong"},
     {"text": "✈️ Zła linia", "cb": "badairline"}],
]


def tg_send_deal(cfg, text, deal_id):
    keyboard = [[{"text": b["text"], "callback_data": "fb|%s|%s" % (deal_id[:24], b["cb"])}
                 for b in row] for row in FEEDBACK_BUTTONS]
    ok = None
    for cid in chat_ids(cfg):
        resp = tg_api(cfg, "sendMessage", {
            "chat_id": cid,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
            "reply_markup": {"inline_keyboard": keyboard},
        })
        ok = resp if ok is None or (resp and resp.get("ok")) else ok
    return ok


def tg_send_plain(cfg, text):
    ok = None
    for cid in chat_ids(cfg):
        ok = tg_api(cfg, "sendMessage", {
            "chat_id": cid, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": True}) or ok
    return ok


# ------------------------------------------------------- feedback / uczenie

def poll_feedback(cfg):
    """Odbiera kliknięcia przycisków z Telegrama i aktualizuje preferencje."""
    if not cfg["telegram"].get("bot_token"):
        return
    tgs = state_file("telegram.json", {"offset": 0})
    resp = tg_api(cfg, "getUpdates", {"offset": tgs["offset"] + 1, "timeout": 0})
    if not resp or not resp.get("ok"):
        return
    prefs = state_file("prefs.json", {"feedback": [], "airline_bad": {},
                                      "airline_good": {}, "too_expensive": [],
                                      "too_long": []})
    seen = state_file("seen.json", {})
    rec = state_file("recipients.json", {"ids": []})
    known = set(chat_ids(cfg))
    for upd in resp.get("result", []):
        tgs["offset"] = max(tgs["offset"], upd["update_id"])
        # samodzielna rejestracja: ktokolwiek napisze do bota (np. kumpel)
        chat = (upd.get("message") or upd.get("callback_query", {})
                .get("message") or {}).get("chat", {})
        cid = str(chat.get("id", "")) if chat else ""
        if cid and cid not in known:
            rec.setdefault("ids", []).append(cid)
            known.add(cid)
            save_state("recipients.json", rec)
            tg_api(cfg, "sendMessage", {"chat_id": cid, "parse_mode": "HTML",
                   "text": "✅ <b>Asia Flight Radar</b> podłączony. Będę pisał, "
                           "gdy trafi się naprawdę dobra okazja business/first "
                           "do Azji."})
            log("Nowy odbiorca zarejestrowany: %s (%s)"
                % (chat.get("first_name", "?"), cid))
        cq = upd.get("callback_query")
        if not cq or not cq.get("data", "").startswith("fb|"):
            continue
        _, short_id, verdict = cq["data"].split("|", 2)
        deal = next((d for k, d in seen.items() if k.startswith(short_id)), {})
        entry = {"when": datetime.now().isoformat(timespec="seconds"),
                 "verdict": verdict,
                 "airline": deal.get("airline", ""),
                 "price_pln": deal.get("price_pln"),
                 "duration_h": deal.get("duration_h"),
                 "route": deal.get("route", "")}
        prefs["feedback"].append(entry)
        if verdict == "badairline" and entry["airline"]:
            prefs["airline_bad"][entry["airline"]] = \
                prefs["airline_bad"].get(entry["airline"], 0) + 1
        if verdict == "buy" and entry["airline"]:
            prefs["airline_good"][entry["airline"]] = \
                prefs["airline_good"].get(entry["airline"], 0) + 1
        if verdict == "expensive" and entry["price_pln"]:
            prefs["too_expensive"].append(entry["price_pln"])
        if verdict == "toolong" and entry["duration_h"]:
            prefs["too_long"].append(entry["duration_h"])
        tg_api(cfg, "answerCallbackQuery",
               {"callback_query_id": cq["id"], "text": "Zapisane — dzięki! 🧠"})
        log("Feedback: %s / %s (%s)" % (verdict, entry["route"], entry["airline"]))
    save_state("telegram.json", tgs)
    save_state("prefs.json", prefs)


def learned(prefs, cfg):
    """Przekłada zebrany feedback na aktywne korekty filtrów."""
    out = {"blocked_airlines": set(), "boost_airlines": set(),
           "max_price_pln": None, "max_duration_h": cfg["limits"]["max_duration_hours"]}
    for airline, n in prefs.get("airline_bad", {}).items():
        if n >= 2:
            out["blocked_airlines"].add(airline)
    for airline, n in prefs.get("airline_good", {}).items():
        if n >= 1:
            out["boost_airlines"].add(airline)
    exp = sorted(p for p in prefs.get("too_expensive", []) if p)
    if len(exp) >= 2:
        out["max_price_pln"] = exp[0] * 0.95  # poniżej najtańszej "za drogiej"
    tl = sorted(d for d in prefs.get("too_long", []) if d)
    if len(tl) >= 2:
        out["max_duration_h"] = min(out["max_duration_h"], tl[0] - 1)
    return out


# ---------------------------------------------------------------- keywords

DEST_WORDS = {
    "BKK": ["bangkok", "bkk", "tajlandi", "thailand"],
    "SIN": ["singapore", "singapur"],
    "KUL": ["kuala lumpur", "malezj", "malaysia"],
    "HKG": ["hong kong", "hongkong"],
    "HAN": ["hanoi"],
    "SGN": ["ho chi minh", "saigon", "sajgon"],
    "TYO": ["tokyo", "tokio", "haneda", "narita", "japan", "japoni"],
    "SEL": ["seoul", "seul", "incheon", "korea"],
}
# Twoje lotniska ze specyfikacji — oferta stąd nie wymaga dolotu
ORIGIN_CORE = ["poland", "polsk", "warsaw", "warszaw", "gdansk", "gdańsk",
               "poznan", "poznań", "oslo", "stockholm", "sztokholm",
               "copenhagen", "kopenhag", "vienna", "wiedn", "wiedeń",
               "budapest", "budapeszt", "milan", "mediolan", "milano"]
# Reszta Europy — warta uwagi, ale z dopiskiem o dolocie
ORIGIN_EU = ["krakow", "kraków", "scandinavia", "skandynaw", "europe", "europ",
             "germany", "berlin", "munich", "prague", "pragi", "praga",
             "amsterdam", "brussels", "paris", "frankfurt", "zurich", "geneva",
             "madrid", "barcelona", "rome", "lisbon", "helsinki", "riga",
             "vilnius", "london", "dublin", "athens"]
ORIGIN_POS = ORIGIN_CORE + ORIGIN_EU
ORIGIN_NEG = ["new york", "los angeles", "san francisco", "chicago", "miami",
              "boston", "dallas", "houston", "seattle", "atlanta", "toronto",
              "vancouver", "montreal", "sydney", "melbourne", "perth",
              "auckland", "from the us", "from us cities", "from usa",
              "from canada", "from australia", "from india", "from johannesburg",
              "from cairo", "from dubai", "from the uk only",
              "egypt", "alexandria", "tunisia", "from tunis", "morocco",
              "casablanca", "algeria", "from istanbul", "from ankara",
              "jeddah", "riyadh", "from tel aviv", "from amman", "from beirut",
              "from baku", "from tashkent", "from almaty", "from delhi",
              "from mumbai", "from karachi", "from lagos", "from nairobi",
              "from accra", "from sao paulo", "from buenos aires",
              "from mexico", "from bogota", "from santiago", "from lima"]
ROUNDTRIP_WORDS = ["roundtrip", "round trip", "round-trip", "w obie strony",
                   "obie strony"]
MONTH_WORDS = {
    "january": 1, "stycz": 1, "february": 2, "lut": 2, "march": 3, "marc": 3,
    "april": 4, "kwiet": 4, "may": 5, "maj": 5, "june": 6, "czerw": 6,
    "july": 7, "lip": 7, "august": 8, "sierp": 8,
    "september": 9, "sept": 9, "wrzes": 9, "wrześ": 9,
    "october": 10, "październik": 10, "pazdziernik": 10,
    "november": 11, "listopad": 11, "december": 12, "grud": 12}
CLASS_WORDS = {"BUSINESS": ["business", "biznes"],
               "FIRST": ["first class", "first-class", "pierwsza klasa", "la première"]}
MILES_WORDS = ["miles", "avios", "points", "award", "mqm", "za mile",
               "punkty", "milami", "redemption"]
TAG_WORDS = [("Error Fare", ["error fare", "errorfare", "błąd ceny", "blad ceny"]),
             ("Mistake Fare", ["mistake fare", "fuel dump"]),
             ("Flash Sale", ["flash sale", "24 hour", "48 hour", "wyprzedaż"]),
             ("Companion Fare", ["companion"]),
             ("Promo Code", ["promo code", "kod promocyjny", "coupon"]),
             ("Upgrade Offer", ["upgrade offer", "upgrade to business"])]

AIRCRAFT = {"359": "Airbus A350-900", "351": "Airbus A350-1000",
            "388": "Airbus A380", "77W": "Boeing 777-300ER",
            "772": "Boeing 777-200", "773": "Boeing 777-300",
            "788": "Boeing 787-8", "789": "Boeing 787-9", "78X": "Boeing 787-10",
            "333": "Airbus A330-300", "332": "Airbus A330-200",
            "339": "Airbus A330-900neo", "343": "Airbus A340-300",
            "764": "Boeing 767-400", "744": "Boeing 747-400"}

SEAT_NOTES = {("QR", "77W"): "możliwy Qsuite", ("QR", "359"): "możliwy Qsuite",
              ("JL", "77W"): "Apex Suite", ("EY", "789"): "Business Studio",
              ("EK", "388"): "pokład A380 z barem", ("SQ", "359"): "nowy produkt SQ",
              ("NH", "77W"): "możliwy The Room", ("BR", "77W"): "Royal Laurel",
              ("CX", "351"): "możliwa Aria Suite", ("TK", "359"): "Crystal Business"}

CUR_RE = re.compile(
    r"(?:(€|\$|£)\s?([\d][\d.,]{1,8})|([\d][\d.,]{1,8})\s?"
    r"(zł|pln|eur|usd|gbp|euro|chf|sek|nok|dkk)|(pln|eur|usd|gbp)\s?([\d][\d.,]{1,8}))",
    re.IGNORECASE)
SYMBOL = {"€": "EUR", "$": "USD", "£": "GBP", "zł": "PLN", "euro": "EUR"}


def find_words(text, words):
    return [w for w in words if w in text]


def extract_price_pln(text, fx):
    """Najniższa sensowna cena z tekstu, przeliczona na PLN."""
    best = None
    for m in CUR_RE.finditer(text):
        if m.group(1):
            cur, raw = SYMBOL[m.group(1)], m.group(2)
        elif m.group(3):
            cur, raw = m.group(4).lower(), m.group(3)
        else:
            cur, raw = m.group(5).lower(), m.group(6)
        cur = SYMBOL.get(cur, cur.upper())
        raw = raw.replace(" ", "")
        # 1.299,00 vs 1,299.00 vs 1299
        if "," in raw and "." in raw:
            raw = raw.replace(",", "") if raw.rfind(".") > raw.rfind(",") \
                else raw.replace(".", "").replace(",", ".")
        elif "," in raw:
            parts = raw.split(",")
            raw = raw.replace(",", "") if len(parts[-1]) == 3 else raw.replace(",", ".")
        try:
            val = float(raw)
        except ValueError:
            continue
        pln = val if cur == "PLN" else val * fx.get(cur, 0)
        if 800 <= pln <= 40000 and (best is None or pln < best):
            best = round(pln)
    return best


# ---------------------------------------------------------------- RSS deals

def strip_html(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def dates_match_trip(text, cfg):
    """Twardy filtr dat dla ofert z blogów: tekst musi wskazywać na miesiąc
    podróży (lub zakres miesięcy go obejmujący) i nie przeczyć rokowi.
    Brak jakiejkolwiek daty => odrzucamy (decyzja użytkownika)."""
    d_from = datetime.strptime(cfg["trip"]["depart_from"], "%Y-%m-%d")
    d_to = datetime.strptime(cfg["trip"]["depart_to"], "%Y-%m-%d")
    want_months = set()
    d = d_from
    while d <= d_to:
        want_months.add(d.month)
        d += timedelta(days=27)
    want_months.add(d_to.month)
    years = {int(y) for y in re.findall(r"\b(20\d{2})\b", text)}
    if years and d_from.year not in years:
        return False  # mowa o innym roku
    # daty numeryczne 2026-09-05 / 05.09.2026 / 5/9
    for m in re.findall(r"\b20\d{2}-(\d{2})-\d{2}\b", text):
        if int(m) in want_months:
            return True
    for d_, m in re.findall(r"\b(\d{1,2})[./](\d{1,2})(?:[./]20\d{2})?\b", text):
        if int(m) in want_months and 1 <= int(d_) <= 31:
            return True
    months = {num for name, num in MONTH_WORDS.items()
              if re.search(r"\b" + name, text)}
    if not months:
        return False  # brak daty w treści => nie podajemy
    if months & want_months:
        return True
    if len(months) >= 2 and min(months) <= min(want_months) <= max(months):
        return True  # zakres w stylu "August - October"
    return False


MAX_ITEM_AGE_DAYS = 14  # wpisy starsze niż 2 tyg. = nieaktualne okazje


def _item_fresh(date_str):
    """False dla wpisów starszych niż MAX_ITEM_AGE_DAYS (Google News potrafi
    zwracać artykuły sprzed lat — to wyniki wyszukiwania, nie nowości)."""
    if not date_str:
        return True  # brak daty w feedzie — nie przesądzamy
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(date_str)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except ValueError:
            return True
    age = datetime.now(dt.tzinfo) - dt
    return age.days <= MAX_ITEM_AGE_DAYS


def fetch_feed(feed):
    try:
        status, body = http_get(feed["url"], timeout=25)
        if status != 200:
            return []
        root = ET.fromstring(body)
        items = []
        for it in root.iter("item"):  # RSS 2.0
            if not _item_fresh(it.findtext("pubDate")):
                continue
            items.append({
                "title": strip_html(it.findtext("title", "")),
                "link": (it.findtext("link") or "").strip(),
                "desc": strip_html(it.findtext("description", ""))[:600],
                "source": feed["name"],
            })
        ns = "{http://www.w3.org/2005/Atom}"
        for it in root.iter(ns + "entry"):  # Atom (np. Reddit)
            if not _item_fresh(it.findtext(ns + "updated")
                               or it.findtext(ns + "published")):
                continue
            link = it.find(ns + "link")
            items.append({
                "title": strip_html(it.findtext(ns + "title", "")),
                "link": link.get("href", "") if link is not None else "",
                "desc": strip_html(it.findtext(ns + "content", "")
                                   or it.findtext(ns + "summary", ""))[:600],
                "source": feed["name"],
            })
        return items[:60]
    except Exception as e:
        log("Feed %s: błąd (%s)" % (feed["name"], e))
        return []


def rss_deals(cfg, fx):
    deals = []
    for feed in cfg["feeds"]:
        for it in fetch_feed(feed):
            text = (it["title"] + " " + it["desc"]).lower()
            dests = [c for c, ws in DEST_WORDS.items() if find_words(text, ws)]
            if not dests:
                continue
            cabin = next((c for c, ws in CLASS_WORDS.items()
                          if find_words(text, ws)), None)
            if not cabin:
                continue
            if find_words(text, MILES_WORDS):
                continue  # oferty za mile/punkty odpadają
            if find_words(text, ORIGIN_NEG) and not find_words(text, ORIGIN_POS):
                continue  # wylot spoza Europy
            if cfg["trip"].get("strict_rss_dates", True) \
                    and not dates_match_trip(text, cfg):
                continue  # brak dat z zakresu podróży w treści
            airline_code = next((code for code, name
                                 in cfg["priority_airlines"].items()
                                 if name.lower() in text), "")
            tags = [t for t, ws in TAG_WORDS if find_words(text, ws)]
            deals.append({
                "kind": "rss",
                "route": "→ " + "/".join(dests),
                "dest": dests[0],
                "cabin": cabin,
                "airline": airline_code,
                "airline_name": cfg["priority_airlines"].get(airline_code, ""),
                "price_pln": extract_price_pln(it["title"] + " " + it["desc"], fx),
                "origin_match": bool(find_words(text, ORIGIN_POS)),
                "needs_feeder": bool(find_words(text, ORIGIN_EU))
                                and not find_words(text, ORIGIN_CORE),
                "roundtrip": bool(find_words(text, ROUNDTRIP_WORDS)),
                "tags": tags,
                "title": it["title"],
                "link": it["link"],
                "source": it["source"],
                "duration_h": None,
                "stops": None,
            })
    log("RSS: %d dopasowanych ofert" % len(deals))
    return deals


# ---------------------------------------------------------------- Amadeus

def amadeus_token(cfg):
    am = cfg["amadeus"]
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": am["api_key"], "client_secret": am["api_secret"]}).encode()
    _, resp = http_get(am["base_url"] + "/v1/security/oauth2/token",
                       headers={"Content-Type": "application/x-www-form-urlencoded"},
                       data=body, method="POST")
    return json.loads(resp)["access_token"]


def amadeus_search(cfg, token, origin, dest, date):
    q = urllib.parse.urlencode({
        "originLocationCode": origin, "destinationLocationCode": dest,
        "departureDate": date, "adults": 1, "travelClass": "BUSINESS",
        "currencyCode": "PLN", "max": 3})
    try:
        _, resp = http_get(cfg["amadeus"]["base_url"]
                           + "/v2/shopping/flight-offers?" + q,
                           headers={"Authorization": "Bearer " + token}, timeout=30)
        return json.loads(resp).get("data", [])
    except Exception as e:
        log("Amadeus %s-%s %s: %s" % (origin, dest, date, e))
        return []


def parse_iso_duration(s):
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", s or "")
    if not m:
        return None
    return round(int(m.group(1) or 0) + int(m.group(2) or 0) / 60, 1)


def plan_queries(cfg, max_queries):
    """Zrównoważony plan zapytań: BKK (priorytet) ze wszystkich portów +
    3 rotujące kierunki dodatkowe z większości portów. Każdy kierunek
    dodatkowy trafia do skanu co ~3 h, a nie raz na 8 h jak wcześniej."""
    d_from = datetime.strptime(cfg["trip"]["depart_from"], "%Y-%m-%d")
    d_to = datetime.strptime(cfg["trip"]["depart_to"], "%Y-%m-%d")
    ndays = (d_to - d_from).days + 1
    tick = int(time.time() // 3600)  # zmienia się co godzinę
    origins = cfg["origins"]

    def rot_date(off):
        return (d_from + timedelta(days=(tick * 3 + off) % ndays)).strftime("%Y-%m-%d")

    queries = []
    # priorytet BKK — wszystkie porty, 1 rotująca data na godzinę
    for origin in origins:
        for dest in cfg["destinations"]["priority"]:
            queries.append((origin, dest, rot_date(0)))
    # kierunki dodatkowe — 3 rotujące co godzinę, z większości portów
    sec = cfg["destinations"]["secondary"]
    sec_per_run = cfg.get("google_flights", {}).get("secondary_per_run", 3)
    for i in range(min(sec_per_run, len(sec))):
        dest = sec[(tick * sec_per_run + i) % len(sec)]
        for origin in origins[:7]:
            queries.append((origin, dest, rot_date(i + 1)))
    return queries[:max_queries]


def gflights_link(origin, dest, date, cabin):
    q = "Flights from %s to %s on %s one way %s class" % (
        origin, dest, date, cabin.lower())
    return "https://www.google.com/travel/flights?q=" + urllib.parse.quote(q)


def amadeus_deals(cfg, fx):
    if not cfg["amadeus"].get("api_key"):
        return []
    try:
        token = amadeus_token(cfg)
    except Exception as e:
        log("Amadeus: logowanie nieudane (%s)" % e)
        return []
    prices = state_file("prices.json", {})
    deals = []
    for origin, dest, date in plan_queries(cfg, cfg["amadeus"]["max_calls_per_run"]):
        offers = amadeus_search(cfg, token, origin, dest, date)
        time.sleep(0.4)  # limit ~10 req/s w darmowym planie
        for off in offers[:1]:  # najtańsza oferta z każdej trasy
            price = round(float(off["price"]["grandTotal"]))
            itin = off["itineraries"][0]
            segs = itin["segments"]
            carrier = segs[0]["carrierCode"]
            aircraft = segs[0].get("aircraft", {}).get("code", "")
            dur = parse_iso_duration(itin.get("duration"))
            key = "%s-%s" % (origin, dest)
            hist = prices.setdefault(key, [])
            hist.append(price)
            del hist[:-60]
            deals.append({
                "kind": "amadeus",
                "route": "%s → %s" % (origin, dest),
                "origin": origin, "dest": dest, "date": date,
                "cabin": "BUSINESS",
                "airline": carrier,
                "airline_name": cfg["priority_airlines"].get(carrier, carrier),
                "price_pln": price,
                "median": sorted(hist)[len(hist) // 2],
                "hist_len": len(hist),
                "stops": len(segs) - 1,
                "via": "/".join(s["arrival"]["iataCode"] for s in segs[:-1]),
                "duration_h": dur,
                "aircraft": AIRCRAFT.get(aircraft, aircraft),
                "seat_note": SEAT_NOTES.get((carrier, aircraft), ""),
                "origin_match": True,
                "tags": [],
                "title": "",
                "link": gflights_link(origin, dest, date, "business"),
                "source": "Amadeus (cena na żywo)",
            })
    save_state("prices.json", prices)
    log("Amadeus: %d ofert" % len(deals))
    return deals


# ------------------------------------------------------- dolot (Aviasales)

def feeder_price_pln(cfg, hub):
    """Żywa cena dolotu hub→BKK (economy, Travelpayouts). Cache 12 h.

    Uwaga: to API nie ma już danych business (stan 07.2026) — używamy go
    wyłącznie do taniego dolotu w alternatywie hubowej.
    """
    ha = cfg["hub_alternative"]
    token = cfg.get("travelpayouts", {}).get("token")
    if not (ha.get("live_feeder") and token):
        return ha["feeder_estimate_pln"], False
    cache = state_file("feeder.json", {})
    ent = cache.get(hub)
    if ent and time.time() - ent["ts"] < 12 * 3600:
        return ent["price"], True
    q = urllib.parse.urlencode({
        "currency": "pln", "origin": hub, "destination": "BKK",
        "beginning_of_period": cfg["trip"]["depart_from"][:7] + "-01",
        "period_type": "month", "one_way": "true", "sorting": "price",
        "limit": 20, "token": token})
    try:
        _, body = http_get(
            "https://api.travelpayouts.com/v2/prices/latest?" + q, timeout=20)
        rows = json.loads(body).get("data") or []
        d_to = (datetime.strptime(cfg["trip"]["depart_to"], "%Y-%m-%d")
                + timedelta(days=6)).strftime("%Y-%m-%d")
        prices = [int(r["value"]) for r in rows
                  if cfg["trip"]["depart_from"] <= r.get("depart_date", "") <= d_to
                  and 50 <= r.get("value", 0) <= 3000]
        if not prices:
            return ha["feeder_estimate_pln"], False
        best = min(prices)
        cache[hub] = {"price": best, "ts": time.time()}
        save_state("feeder.json", cache)
        return best, True
    except Exception as e:
        log("Feeder %s-BKK: %s" % (hub, e))
        return ha["feeder_estimate_pln"], False


# ------------------------------------------------------------ Google Flights

def gflights_deals(cfg):
    """Pełny skan rynku przez Google Flights (nieoficjalnie, patrz gflights.py)."""
    gf_cfg = cfg.get("google_flights", {})
    if not gf_cfg.get("enabled", True):
        return []
    try:
        import gflights
    except ImportError as e:
        log("Google Flights: brak biblioteki fast-flights (%s) — "
            "uruchom przez venv/bin/python" % e)
        return []
    import random
    prices = state_file("prices.json", {})
    prio = set(cfg["priority_airlines"])
    maxdur = cfg["limits"]["max_duration_hours"]
    excluded = [c.lower() for c in cfg.get("exclude_carriers", [])]
    direct_only = set(cfg.get("direct_only_origins", []))
    deals = []
    blocked = False

    def within_time(flights, origin=None):
        """Odrzuca loty > limitu czasu, tanie linie (business != lie-flat),
        a z wybranych lotnisk (np. IST) tylko bezpośrednie."""
        out = []
        for f in flights:
            if f["duration_h"] and f["duration_h"] > maxdur:
                continue
            name = (f["airline_name"] or "").lower()
            if any(x in name for x in excluded):
                continue
            if origin in direct_only and f["stops"] != 0:
                continue
            out.append(f)
        return out

    def gf_deal(f, origin, dest, date, cabin, level, hist):
        return {
            "kind": "gf",
            "route": "%s → %s" % (origin, dest),
            "origin": origin, "dest": dest, "date": date,
            "cabin": cabin,
            "airline": f["airline"],
            "airline_name": f["airline_name"],
            "price_pln": f["price_pln"],
            "median": sorted(hist)[len(hist) // 2] if hist else None,
            "hist_len": len(hist),
            "stops": f["stops"],
            "via": "",
            "duration_h": f["duration_h"],
            "departure": f.get("departure", ""),
            "gf_price_level": level,
            "origin_match": True,
            "tags": [],
            "title": "",
            "link": f["link"],
            "source": "Google Flights (cena na żywo)",
        }

    queries = plan_queries(cfg, gf_cfg.get("max_queries_per_run", 22))
    for i, (origin, dest, date) in enumerate(queries):
        if i:
            time.sleep(gf_cfg.get("min_delay_s", 2) + random.uniform(0, 3))
        try:
            level, flights = gflights.fetch_gf(origin, dest, date)
        except gflights.BlockedError as e:
            log("Google Flights: blokada (%s) — przerywam skan do "
                "następnego przebiegu" % e)
            blocked = True
            break
        except Exception as e:
            log("Google Flights %s-%s %s: %s" % (origin, dest, date, e))
            continue
        flights = within_time(flights, origin)  # twardy limit czasu lotu
        key = "GF:%s-%s" % (origin, dest)
        hist = prices.setdefault(key, [])
        if flights:
            hist.append(min(f["price_pln"] for f in flights))
            del hist[:-60]
        for f in gflights.cheapest_picks(flights, prio):
            deals.append(gf_deal(f, origin, dest, date, "BUSINESS", level, hist))

    # First class: rotacyjne sondy (spec: "każda wyjątkowa okazja").
    # Rotujemy zarówno lotnisko wylotu, jak i kierunek (BKK + dodatkowe),
    # żeby First był sprawdzany na całej Azji, nie tylko do Bangkoku.
    tick = int(time.time() // 3600)
    d_from = datetime.strptime(cfg["trip"]["depart_from"], "%Y-%m-%d")
    ndays = (datetime.strptime(cfg["trip"]["depart_to"], "%Y-%m-%d")
             - d_from).days + 1
    f_date = (d_from + timedelta(days=tick % ndays)).strftime("%Y-%m-%d")
    first_dests = cfg["destinations"]["priority"] + cfg["destinations"]["secondary"]
    for i in range(0 if blocked else gf_cfg.get("first_class_queries", 2)):
        origin = cfg["origins"][(tick + i) % len(cfg["origins"])]
        dest = first_dests[(tick + i) % len(first_dests)]
        time.sleep(gf_cfg.get("min_delay_s", 2) + random.uniform(0, 3))
        try:
            level, flights = gflights.fetch_gf(origin, dest, f_date, seat="first")
        except Exception as e:
            log("Google Flights first %s-%s: %s" % (origin, dest, e))
            continue
        flights = within_time(flights, origin)  # twardy limit czasu lotu
        key = "GF1:%s-%s" % (origin, dest)
        hist = prices.setdefault(key, [])
        if flights:
            hist.append(min(f["price_pln"] for f in flights))
            del hist[:-60]
        prio_f = [f for f in flights if f["airline"] in prio] or flights
        for f in gflights.cheapest_picks(prio_f, prio)[:1]:
            deals.append(gf_deal(f, origin, dest, f_date, "FIRST", level, hist))

    save_state("prices.json", prices)
    log("Google Flights: %d ofert z %d zapytań" % (len(deals), len(queries)))
    return deals


def hub_alternative(cfg, deals):
    """Jeśli SIN/KUL/SGN wychodzi taniej niż BKK + tani dolot — zgłoś alternatywę."""
    ha = cfg["hub_alternative"]
    if not ha["enabled"]:
        return None
    am = [d for d in deals if d["kind"] in ("amadeus", "gf")]
    bkk = [d for d in am if d["dest"] == "BKK"]
    hubs = [d for d in am if d["dest"] in ha["hubs"]]
    if not bkk or not hubs:
        return None
    best_bkk = min(bkk, key=lambda d: d["price_pln"])
    best_hub = min(hubs, key=lambda d: d["price_pln"])
    feeder, live = feeder_price_pln(cfg, best_hub["dest"])
    if best_hub["price_pln"] + feeder < best_bkk["price_pln"]:
        return (best_hub, best_bkk, feeder, live)
    return None


# ---------------------------------------------------------------- scoring

def score(deal, cfg, lp):
    """Ocena zakotwiczona na CENIE. Bonusy (linia, 'taniej niż zwykle')
    to sygnały miękkie — mogą podnieść o 1★, ale NIE robią 5★ z oferty
    droższej niż budżet 4★. 5★ = albo świetna cena, albo wyjątkowa wartość
    (error fare / wyraźnie poniżej mediany rynkowej)."""
    b = cfg["budget_pln"]
    p = deal["price_pln"]
    if deal["airline"] in lp["blocked_airlines"]:
        return 0
    if lp["max_price_pln"] and p and p > lp["max_price_pln"]:
        return min(2, _base_stars(p, deal["cabin"], b))

    base = _base_stars(p, deal["cabin"], b)  # kotwica: sama cena
    stops = deal.get("stops")

    # kary jakościowe za trasę (liczymy osobno, odejmiemy na końcu)
    penalty = 0
    if deal["kind"] in ("amadeus", "gf"):
        if (stops or 0) > cfg["limits"]["max_stops"]:
            penalty += 2
        elif stops == 2:
            penalty += 1  # 2 przesiadki na locie premium — gorsze doświadczenie
        if deal.get("duration_h") and deal["duration_h"] > lp["max_duration_h"]:
            penalty += 1
    elif not deal["origin_match"]:
        penalty += 1

    # 5★ ("KUPUJ NATYCHMIAST") = tylko WYJĄTKOWA okazja. Powiadomienia idą
    # jedynie na 5★. Zasada twarda: POWYŻEJ budżetu (business_4star / first_4star)
    # NIC nie dostaje 5★ — jedynie prawdziwy error/mistake fare (błąd ceny może
    # być dowolnie wysoki). 5★ przyznajemy gdy:
    #   • error/mistake fare (dowolna cena), lub
    #   • cena ≤ business_5star (naprawdę niska), lub
    #   • cena W BUDŻECIE i wyraźnie poniżej mediany rynkowej trasy.
    budget4 = b["business_4star"] if deal["cabin"] == "BUSINESS" else b["first_4star"]
    error_fare = bool(set(deal.get("tags", [])) & {"Error Fare", "Mistake Fare"})
    below_market = bool(deal.get("median") and p and p < 0.75 * deal["median"]
                        and deal.get("hist_len", 0) >= 10)

    if error_fare:
        raw = 5  # błąd ceny to okazja niezależnie od kwoty
    elif base >= 5 or (below_market and p is not None and p <= budget4):
        raw = 5  # naprawdę niska cena albo poniżej rynku i w budżecie
    else:
        raw = min(base, 4)  # powyżej budżetu / zwykła cena — nigdy 5★

    stars = raw - penalty
    if p is None:
        stars = min(stars, 3)
    return max(1, min(5, stars))


def _base_stars(p, cabin, b):
    if p is None:
        return 2
    if cabin == "FIRST":
        if p <= b["first_5star"]:
            return 5
        if p <= b["first_4star"]:
            return 4
        return 2
    if p <= b["business_5star"]:
        return 5
    if p <= b["business_4star"]:
        return 4
    if p <= b["business_3star"]:
        return 3
    if p <= b["business_2star"]:
        return 2
    return 1


# ---------------------------------------------------------------- verify

def verify(deal):
    """Sprawdza, czy oferta wciąż żyje (dotyczy ofert z blogów)."""
    if deal["kind"] != "rss":
        return True  # amadeus/gf: cena z zapytania na żywo
    link = deal["link"]
    if not link:
        return False
    if "news.google.com" in link:
        return True  # strona źródłowa blokuje boty; link zweryfikuje się u użytkownika
    try:
        status, body = http_get(link, timeout=20)
        if status != 200:
            return False
        low = body[:120000].decode("utf-8", "ignore").lower()
        for marker in ["deal is expired", "offer has expired", "no longer available",
                       "oferta wygasła", "deal expired", "wyprzedane"]:
            if marker in low:
                return False
        return True
    except Exception:
        return True  # błąd techniczny ≠ martwa oferta; nie odrzucamy


# ---------------------------------------------------------------- notify

def publish_dashboard():
    """Wypycha dashboard na GitHub Pages (repo w katalogu site/)."""
    import shutil
    import subprocess
    site = os.path.join(BASE, "site")
    if not os.path.isdir(os.path.join(site, ".git")):
        return
    try:
        shutil.copy(os.path.join(BASE, "dashboard.html"),
                    os.path.join(site, "index.html"))
        subprocess.run(["git", "-C", site, "add", "index.html"],
                       check=True, capture_output=True)
        r = subprocess.run(["git", "-C", site, "commit", "-qm",
                            "auto-update"], capture_output=True)
        if r.returncode == 0:  # commit tylko gdy są zmiany
            subprocess.run(["git", "-C", site, "push", "-q"],
                           check=True, capture_output=True, timeout=90)
            log("Dashboard opublikowany: kyudo4.github.io/flight-radar")
    except Exception as e:
        log("Publikacja dashboardu: %s" % e)


STAR = {5: "⭐⭐⭐⭐⭐ KUPUJ NATYCHMIAST", 4: "⭐⭐⭐⭐ Bardzo dobra",
        3: "⭐⭐⭐ Dobra", 2: "⭐⭐ Przeciętna", 1: "⭐ Ignoruj"}


def deal_id(deal):
    # Ta sama linia może mieć kilka różnych lotów tego samego dnia. Godzina
    # wylotu i czas podróży są częścią wariantu, aby jeden nie nadpisywał
    # drugiego w bazie.
    key = "%s|%s|%s|%s|%s|%s|%s" % (
        deal["route"], deal["airline"], deal["cabin"],
        deal.get("title") or deal.get("date", ""), deal.get("departure", ""),
        deal.get("duration_h", ""), deal.get("stops", ""))
    return hashlib.sha1(key.encode()).hexdigest()


def carrier_key(deal):
    """Stabilny kod przewoźnika dla porównywania dat tej samej linii.

    Starsze wpisy archiwum mają wyłącznie pełną nazwę linii, a świeże oferty
    z Google Flights mają kod IATA. Normalizujemy oba formaty, aby np. nowy
    Turkish Airlines nie był traktowany jako zupełnie nowy przewoźnik.
    """
    raw = (deal.get("airline_code") or deal.get("airline")
           or deal.get("airline_name") or "").strip()
    if len(raw) == 2 and raw.isalpha():
        return raw.upper()
    try:
        import gflights
        return gflights.airline_code(raw) or raw.lower()
    except Exception:
        return raw.lower()


def route_carrier_key(deal):
    return "%s|%s|%s" % (deal.get("route", ""), carrier_key(deal),
                          deal.get("cabin", ""))


def low_price(value):
    """Obsługuje zarówno stary zapis liczbowy, jak i nowy zapis ze znacznikiem."""
    return value.get("price") if isinstance(value, dict) else value


def fmt_price(p):
    return "{:,.0f} PLN".format(p).replace(",", " ") if p else "brak w treści"


def fmt_deal(deal, stars, drop_note="", trend=""):
    e = html.escape
    lines = ["<b>%s</b>" % STAR[stars]]
    if drop_note:
        lines.append("📉 <b>%s</b>" % drop_note)
    lines.append("🧭 <b>%s</b> · klasa: %s" % (e(deal["route"]),
                 "Business" if deal["cabin"] == "BUSINESS" else "First"))
    if deal.get("airline_name"):
        lines.append("✈️ %s" % e(deal["airline_name"]))
    lines.append("💰 %s" % fmt_price(deal["price_pln"]))
    if deal.get("date"):
        d = "🗓 %s" % deal["date"]
        if deal.get("stops") is not None:
            d += " · %s" % ("bez przesiadek" if deal["stops"] == 0
                            else "%d przes.%s" % (deal["stops"],
                                 " (%s)" % deal["via"] if deal.get("via") else ""))
        if deal.get("duration_h"):
            d += " · %.0fh %.0fm" % (deal["duration_h"] // 1,
                                     (deal["duration_h"] % 1) * 60)
        lines.append(d)
    if deal.get("aircraft"):
        a = "🛩 %s" % e(deal["aircraft"])
        if deal.get("seat_note"):
            a += " (%s)" % e(deal["seat_note"])
        lines.append(a)
    if trend:
        lines.append(trend if trend[0] in "📉📈≈" else "📊 " + trend)
    if deal.get("gf_price_level") == "low":
        lines.append("📊 Google: ceny na tej trasie niższe niż zwykle")
    if deal.get("roundtrip"):
        lines.append("↔️ Cena za lot w obie strony")
    if deal.get("needs_feeder"):
        lines.append("📍 Wylot spoza Twoich lotnisk — potrzebny dolot")
    for t in deal.get("tags", []):
        lines.append("🏷 <b>%s</b>" % t)
    if deal.get("title"):
        lines.append("📰 %s" % e(deal["title"][:160]))
    lines.append('🔗 <a href="%s">Otwórz ofertę</a> · źródło: %s'
                 % (e(deal["link"]), e(deal["source"])))
    return "\n".join(lines)


# ---------------------------------------------------------------- main run

# --------------------------------------------------- trend / heartbeat / digest

def should_notify(deal, cfg):
    """Reguły cenowe powiadomień (niezależne od gwiazdek):
      1) z Poznania (POZ) dokądkolwiek w Azji poniżej `poznan_max`
      2) skądkolwiek do BKK poniżej `bkk_max`
      3) do innych miast Azji (nie-BKK) skądkolwiek poniżej `other_max`
    Error/mistake fare z blogów przechodzi zawsze (to okazja z definicji)."""
    p = deal.get("price_pln")
    if not p:
        return False
    if set(deal.get("tags", [])) & {"Error Fare", "Mistake Fare"}:
        return True
    r = cfg["notify"]["rules"]
    origin = deal.get("origin", "")
    dest = deal.get("dest", "")
    if origin == "POZ" and p < r["poznan_max"]:
        return True
    if dest == "BKK" and p < r["bkk_max"]:
        return True
    if dest and dest != "BKK" and p < r["other_max"]:
        return True
    return False


def update_trends(deals):
    """Dzienny minimalny koszt na trasę (30 dni historii) -> state/trends.json."""
    trends = state_file("trends.json", {})
    today = datetime.now().strftime("%Y-%m-%d")
    for d in deals:
        if d["kind"] not in ("gf", "amadeus") or not d.get("price_pln"):
            continue
        key = "%s|%s" % (d["route"], d["cabin"])
        series = trends.setdefault(key, {})
        series[today] = min(series.get(today, 10 ** 9), d["price_pln"])
        for old in sorted(series)[:-30]:  # zostaw ostatnie 30 dni
            del series[old]
    save_state("trends.json", trends)
    return trends


def trend_for(trends, route, cabin):
    """Zwraca (procent_zmiany, dawna_cena, dni) porównując dziś vs ~7 dni temu."""
    series = trends.get("%s|%s" % (route, cabin), {})
    if len(series) < 2:
        return None
    days = sorted(series)
    today_p = series[days[-1]]
    target = (datetime.strptime(days[-1], "%Y-%m-%d") - timedelta(days=7))
    ref = min(days, key=lambda d:
              abs((datetime.strptime(d, "%Y-%m-%d") - target).days))
    old_p = series[ref]
    span = (datetime.strptime(days[-1], "%Y-%m-%d")
            - datetime.strptime(ref, "%Y-%m-%d")).days
    if not old_p or span < 1:
        return None
    return (round((today_p - old_p) / old_p * 100), old_p, span)


def fmt_trend(t):
    if not t:
        return ""
    pct, old_p, days = t
    if abs(pct) < 3:
        return "≈ cena stabilna (%d dni)" % days
    arrow = "📉" if pct < 0 else "📈"
    return "%s %+d%% w %d dni (było %s)" % (arrow, pct, days, fmt_price(old_p))


def heartbeat(cfg, deals):
    """Alarm na Telegram, gdy skan przestaje zwracać oferty (blokada/awaria)."""
    hb_hours = cfg.get("heartbeat_hours", 6)
    health = state_file("health.json", {})
    now = datetime.now()
    gf_ok = any(d["kind"] == "gf" for d in deals)
    if gf_ok:
        if health.get("warned_at"):  # powrót do działania
            if cfg["telegram"].get("bot_token"):
                tg_send_plain(cfg, "✅ <b>Radar znów działa</b> — oferty "
                                   "znowu spływają.")
            health["warned_at"] = None
        health["last_gf_ok"] = now.isoformat(timespec="seconds")
    else:
        last = health.get("last_gf_ok")
        gap_h = ((now - datetime.fromisoformat(last)).total_seconds() / 3600
                 if last else hb_hours + 1)
        if gap_h >= hb_hours and not health.get("warned_at"):
            if cfg["telegram"].get("bot_token"):
                tg_send_plain(cfg, "⚠️ <b>Radar milczy od ~%d h</b> — Google "
                              "Flights nie zwraca ofert (możliwa blokada lub "
                              "awaria). Sprawdź działanie, jeśli cisza się "
                              "przedłuża." % round(gap_h))
            health["warned_at"] = now.isoformat(timespec="seconds")
            log("Heartbeat: wysłano alarm (przerwa %d h)" % round(gap_h))
    save_state("health.json", health)


def weekly_digest(cfg, archive):
    """W niedzielę raz: najtańszy business live na każdy kierunek z ost. 24 h."""
    dc = cfg.get("digest", {})
    if not dc.get("enabled", True):
        return
    now = datetime.now()
    if now.weekday() != dc.get("weekday", 6) or now.hour < dc.get("hour", 9):
        return
    health = state_file("health.json", {})
    if health.get("last_digest") == now.strftime("%Y-%m-%d"):
        return
    cutoff = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    best = {}
    for d in archive.values():
        if d["kind"] not in ("gf", "amadeus") or d["cabin"] != "BUSINESS":
            continue
        if d.get("last_seen", "") < cutoff or not d.get("price_pln"):
            continue
        dest = d["route"].split("→")[-1].strip()
        if dest not in best or d["price_pln"] < best[dest]["price_pln"]:
            best[dest] = d
    if best:
        lines = ["📅 <b>Podsumowanie tygodnia — najtaniej business teraz:</b>"]
        for dest, d in sorted(best.items(), key=lambda kv: kv[1]["price_pln"]):
            lines.append("• <b>%s</b> — %s (%s)" % (
                dest, fmt_price(d["price_pln"]), d["route"].split("→")[0].strip()))
        lines.append("\n🔗 Wszystko: kyudo4.github.io/flight-radar")
        if cfg["telegram"].get("bot_token"):
            tg_send_plain(cfg, "\n".join(lines))
        else:
            print(re.sub(r"<[^>]+>", "", "\n".join(lines)))
    health["last_digest"] = now.strftime("%Y-%m-%d")
    save_state("health.json", health)


def run(cfg):
    poll_feedback(cfg)
    fx = get_fx(cfg)
    prefs = state_file("prefs.json", {})
    lp = learned(prefs, cfg)
    if lp["blocked_airlines"]:
        log("Uczenie: pomijam linie %s" % ", ".join(lp["blocked_airlines"]))

    deals = rss_deals(cfg, fx) + amadeus_deals(cfg, fx) + gflights_deals(cfg)
    seen = state_file("seen.json", {})
    archive = state_file("deals.json", {})
    # Trwałe minima nie są ograniczane do 500 kart widocznych na stronie.
    # Dzięki temu stara, lepsza cena nadal blokuje droższy alert za tę samą
    # trasę, linię i klasę na inny dzień.
    route_lows = state_file("route_lows.json", {})
    trends = update_trends(deals)
    drop_ratio = cfg["notify"]["renotify_price_drop_ratio"]
    now = datetime.now().isoformat(timespec="seconds")
    sent = 0

    # Najniższa znana cena trasy danej linii i klasy. Droższa data tej samej
    # linii nie jest nową okazją; nowa linia na trasie może być nią nadal.
    route_carrier_min = {}
    for d in archive.values():
        if d.get("price_pln"):
            rk = route_carrier_key(d)
            route_carrier_min[rk] = min(
                route_carrier_min.get(rk, 10 ** 9), d["price_pln"])
    # seen.json przechowuje poprzednie powiadomienia dłużej niż karta może
    # pozostać w archiwum. Brak cabin dotyczy tylko dawnych wpisów; monitor
    # zapisywał praktycznie wyłącznie Business, więc to bezpieczna migracja.
    for d in seen.values():
        if d.get("price_pln") and d.get("route") and d.get("airline"):
            legacy = dict(d)
            legacy.setdefault("cabin", "BUSINESS")
            rk = route_carrier_key(legacy)
            route_carrier_min[rk] = min(
                route_carrier_min.get(rk, 10 ** 9), d["price_pln"])
    for rk, entry in route_lows.items():
        p = low_price(entry)
        if p:
            route_carrier_min[rk] = min(route_carrier_min.get(rk, 10 ** 9), p)
    # Od najtańszych: w jednym przebiegu zostaje tylko najlepsza cena każdej
    # linii na danej trasie.
    deals.sort(key=lambda d: d["price_pln"] if d.get("price_pln") else 10 ** 9)

    for deal in deals:
        did = deal_id(deal)
        stars = score(deal, cfg, lp)
        old = seen.get(did)
        drop_note = ""
        send = False
        notify = should_notify(deal, cfg)
        p = deal["price_pln"]
        rk = route_carrier_key(deal)
        prev_min = route_carrier_min.get(rk)
        is_new_low = bool(p) and (prev_min is None or p < prev_min)
        if old:
            # ten sam lot (ta sama data) — ponowny alert tylko przy spadku o próg
            if notify and p and old.get("price_pln") \
                    and p < old["price_pln"] * drop_ratio:
                drop_note = "Cena spadła: %s → %s" % (
                    fmt_price(old["price_pln"]), fmt_price(p))
                send = True
        else:
            # Nowa data tej samej linii tylko gdy jest tańsza. Linia, której
            # wcześniej nie było na trasie, dostaje pierwszy alert, jeśli
            # przechodzi zwykłe progi cenowe should_notify().
            send = notify and is_new_low
        if p:
            route_carrier_min[rk] = min(
                prev_min if prev_min is not None else 10 ** 9, p)
            old_low = low_price(route_lows.get(rk))
            route_lows[rk] = {"price": min(old_low, p) if old_low else p,
                              "updated_at": now}
        if send and not verify(deal):
            log("Odrzucono martwą ofertę: %s" % deal.get("title", deal["route"]))
            send = False
        trend = trend_for(trends, deal["route"], deal["cabin"])
        if send:
            text = fmt_deal(deal, stars, drop_note, fmt_trend(trend))
            if cfg["telegram"].get("bot_token") and chat_ids(cfg):
                resp = tg_send_deal(cfg, text, did)
                send = bool(resp and resp.get("ok"))
                if send:
                    sent += 1
                    log("Wysłano: %s %s (%d⭐)" % (deal["route"],
                        fmt_price(deal["price_pln"]), stars))
            else:
                print("\n--- POWIADOMIENIE (dry-run, brak tokenu Telegrama) ---")
                print(re.sub(r"<[^>]+>", "", text))
                sent += 1
        seen[did] = {"price_pln": deal["price_pln"], "stars": stars,
                     "airline": deal["airline"], "route": deal["route"],
                     "cabin": deal["cabin"],
                     "duration_h": deal.get("duration_h"),
                     "when": now}
        prev = archive.get(did, {})
        pr = [p for p in (prev.get("min_price"), deal["price_pln"]) if p]
        archive[did] = {
            "id": did, "kind": deal["kind"], "route": deal["route"],
            "cabin": deal["cabin"],
            "airline": deal.get("airline_name") or deal.get("airline", ""),
            "airline_code": deal.get("airline", ""),
            "price_pln": deal["price_pln"], "stars": stars,
            "tags": deal.get("tags", []), "title": deal.get("title", ""),
            "link": deal.get("link", ""), "source": deal.get("source", ""),
            "duration_h": deal.get("duration_h"), "stops": deal.get("stops"),
            "departure": deal.get("departure", ""),
            "date": deal.get("date", ""),
            "gf_low": deal.get("gf_price_level") == "low",
            "roundtrip": deal.get("roundtrip", False),
            "needs_feeder": deal.get("needs_feeder", False),
            "first_seen": prev.get("first_seen", now), "last_seen": now,
            "notified": bool(send or prev.get("notified")),
            "min_price": min(pr) if pr else None,
            "trend": fmt_trend(trend),
        }

    alt = hub_alternative(cfg, deals)
    if alt:
        hub, bkk, feeder, live = alt
        aid = "alt-" + deal_id(hub)
        if aid not in seen:
            msg = ("💡 <b>Alternatywa przez huba</b>\n"
                   "%s za %s jest tańsze niż BKK (%s).\n"
                   "Dolot %s→BKK %s %s — razem i tak taniej.\n"
                   '🔗 <a href="%s">Sprawdź</a>') % (
                hub["route"], fmt_price(hub["price_pln"]),
                fmt_price(bkk["price_pln"]), hub["dest"],
                ("od " + fmt_price(feeder)) if live else ("~" + fmt_price(feeder)),
                "(żywa cena Aviasales)" if live else "(szacunek)",
                hub["link"])
            if cfg["telegram"].get("bot_token"):
                tg_send_plain(cfg, msg)
            else:
                print(re.sub(r"<[^>]+>", "", msg))
            seen[aid] = {"when": datetime.now().isoformat(timespec="seconds")}

    # porządek w seen.json (starsze niż 60 dni wypadają)
    cutoff = (datetime.now() - timedelta(days=60)).isoformat()
    seen = {k: v for k, v in seen.items() if v.get("when", "9999") > cutoff}
    save_state("seen.json", seen)
    save_state("route_lows.json", route_lows)

    # archiwum + strona z ofertami — usuń oferty >limitu czasu i tanie linie
    maxdur = cfg["limits"]["max_duration_hours"]
    excl = [c.lower() for c in cfg.get("exclude_carriers", [])]
    archive = {k: v for k, v in archive.items()
               if (not v.get("duration_h") or v["duration_h"] <= maxdur)
               and not any(x in (v.get("airline") or "").lower() for x in excl)}
    if len(archive) > 500:
        keep = sorted(archive.values(), key=lambda d: d.get("last_seen", ""),
                      reverse=True)[:500]
        archive = {d["id"]: d for d in keep}
    save_state("deals.json", archive)
    heartbeat(cfg, deals)
    weekly_digest(cfg, archive)
    try:
        from dashboard import write_dashboard
        write_dashboard(archive, os.path.join(BASE, "dashboard.html"))
        publish_dashboard()
    except Exception as e:
        log("Dashboard: błąd generowania (%s)" % e)
    log("Koniec przebiegu: %d powiadomień, %d ofert przeanalizowanych"
        % (sent, len(deals)))


# ---------------------------------------------------------------- commands

def cmd_setup(cfg):
    """Rejestruje każdego, kto napisał do bota (Ty, kumpel…), jako odbiorcę."""
    if not cfg["telegram"].get("bot_token"):
        print("Najpierw wklej bot_token do config.json (sekcja telegram).")
        return
    resp = tg_api(cfg, "getUpdates", {"timeout": 0})
    ids = cfg["telegram"].setdefault("chat_ids", [])
    added = []
    for upd in (resp.get("result", []) if resp else []):
        msg = upd.get("message", {})
        chat = msg.get("chat")
        if chat and str(chat["id"]) not in ids:
            ids.append(str(chat["id"]))
            added.append("%s (%s)" % (chat.get("first_name", "?"),
                                      chat.get("username", chat["id"])))
    if added:
        save_json(CONFIG_PATH, cfg)
        tg_send_plain(cfg, "✅ Flight Radar podłączony. Będę pisał tylko, "
                           "gdy trafi się coś naprawdę dobrego.")
        print("Dodano odbiorców: " + ", ".join(added))
    else:
        print("Brak nowych czatów — odbiorca musi najpierw wysłać /start "
              "do bota. Zarejestrowani: %s" % (ids or "nikt"))


def cmd_test(cfg):
    demo = {"kind": "amadeus", "route": "WAW → BKK", "cabin": "BUSINESS",
            "airline": "QR", "airline_name": "Qatar Airways", "price_pln": 4890,
            "date": "2026-09-05", "stops": 1, "via": "DOH", "duration_h": 14.5,
            "aircraft": "Boeing 777-300ER", "seat_note": "możliwy Qsuite",
            "tags": ["Flash Sale"], "title": "",
            "link": gflights_link("WAW", "BKK", "2026-09-05", "business"),
            "source": "TEST"}
    resp = tg_send_deal(cfg, fmt_deal(demo, 5), "testtesttesttesttesttest")
    print("OK — sprawdź Telegram." if resp and resp.get("ok")
          else "Błąd wysyłki: %s" % resp)


def cmd_dashboard(cfg):
    from dashboard import write_dashboard
    path = os.path.join(BASE, "dashboard.html")
    write_dashboard(state_file("deals.json", {}), path)
    os.system('open "%s"' % path)


def cmd_prefs(cfg):
    prefs = state_file("prefs.json", {})
    lp = learned(prefs, cfg)
    print(json.dumps({"feedback_count": len(prefs.get("feedback", [])),
                      "blocked_airlines": sorted(lp["blocked_airlines"]),
                      "boost_airlines": sorted(lp["boost_airlines"]),
                      "max_price_pln": lp["max_price_pln"],
                      "max_duration_h": lp["max_duration_h"]},
                     indent=2, ensure_ascii=False))


def apply_env(cfg):
    """Sekrety z zmiennych środowiskowych (GitHub Actions) nadpisują config —
    dzięki temu config.json w publicznym repo nie zawiera tokenów."""
    if os.environ.get("TG_BOT_TOKEN"):
        cfg["telegram"]["bot_token"] = os.environ["TG_BOT_TOKEN"]
    if os.environ.get("TG_CHAT_IDS"):
        cfg["telegram"]["chat_ids"] = [
            c.strip() for c in os.environ["TG_CHAT_IDS"].split(",") if c.strip()]
    if os.environ.get("TP_TOKEN"):
        cfg.setdefault("travelpayouts", {})["token"] = os.environ["TP_TOKEN"]
    return cfg


if __name__ == "__main__":
    os.makedirs(STATE, exist_ok=True)
    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        print("Brak config.json!")
        sys.exit(1)
    cfg = apply_env(cfg)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    {"run": run, "setup": cmd_setup, "test": cmd_test, "prefs": cmd_prefs,
     "dashboard": cmd_dashboard}[cmd](cfg)
