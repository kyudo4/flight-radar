# -*- coding: utf-8 -*-
"""Jednorazowe pełne przeczesanie: wszystkie lotniska EU × wszystkie kierunki
Azji (business), 2 daty z okna. Zapisuje do site/state (repo) i odświeża stronę.
Użycie: venv/bin/python fullscan.py [test]"""
import json, os, sys, time, random
from datetime import datetime, timedelta
import monitor as m
import gflights

BASE = os.path.dirname(os.path.abspath(__file__))
S = os.path.join(BASE, "site", "state")
now = datetime.now().isoformat(timespec="seconds")

cfg = json.load(open(os.path.join(BASE, "config.json")))
prefs = json.load(open(os.path.join(S, "prefs.json"))) if os.path.exists(os.path.join(S, "prefs.json")) else {}
lp = m.learned(prefs, cfg)
prices = json.load(open(os.path.join(S, "prices.json")))
archive = json.load(open(os.path.join(S, "deals.json")))
prio = set(cfg["priority_airlines"])

origins = cfg["origins"]
dests = cfg["destinations"]["priority"] + cfg["destinations"]["secondary"]
d_from = datetime.strptime(cfg["trip"]["depart_from"], "%Y-%m-%d")
d_to = datetime.strptime(cfg["trip"]["depart_to"], "%Y-%m-%d")
ndays = (d_to - d_from).days + 1
dates = [(d_from + timedelta(days=x)).strftime("%Y-%m-%d") for x in (2, 9)]  # 03.09 i 10.09

if len(sys.argv) > 1 and sys.argv[1] == "test":
    origins, dests, dates = ["WAW"], ["SIN", "HKG"], [dates[0]]
elif len(sys.argv) > 1 and len(sys.argv[1]) == 3:  # konkretne lotnisko, np. IST
    origins = [sys.argv[1].upper()]

added = 0
pairs = [(o, de) for o in origins for de in dests]
print("Pełny skan: %d par lotnisk × %d daty = do %d zapytań"
      % (len(pairs), len(dates), len(pairs) * len(dates)))

for o, de in pairs:
    for date in dates:
        time.sleep(2 + random.uniform(0, 3))
        try:
            level, flights = gflights.fetch_gf(o, de, date)
        except gflights.BlockedError:
            print("BLOKADA — przerywam"); break
        except Exception as e:
            print("  %s-%s %s: %s" % (o, de, date, e)); continue
        maxdur = cfg["limits"]["max_duration_hours"]
        excl = [c.lower() for c in cfg.get("exclude_carriers", [])]
        direct = set(cfg.get("direct_only_origins", []))
        flights = [f for f in flights
                   if (not f["duration_h"] or f["duration_h"] <= maxdur)
                   and not any(x in (f["airline_name"] or "").lower() for x in excl)
                   and not (o in direct and f["stops"] != 0)]
        key = "GF:%s-%s" % (o, de)
        hist = prices.setdefault(key, [])
        if flights:
            hist.append(min(f["price_pln"] for f in flights)); del hist[:-60]
        med = sorted(hist)[len(hist) // 2] if hist else None
        for f in gflights.cheapest_picks(flights, prio):
            deal = {"kind": "gf", "route": "%s → %s" % (o, de),
                    "origin": o, "dest": de, "date": date, "cabin": "BUSINESS",
                    "airline": f["airline"], "airline_name": f["airline_name"],
                    "price_pln": f["price_pln"], "median": med, "hist_len": len(hist),
                    "stops": f["stops"], "via": "", "duration_h": f["duration_h"],
                    "departure": f.get("departure", ""),
                    "gf_price_level": level, "origin_match": True, "tags": [],
                    "title": "", "link": f["link"],
                    "source": "Google Flights (cena na żywo)"}
            did = m.deal_id(deal)
            stars = m.score(deal, cfg, lp)
            prev = archive.get(did, {})
            pr = [p for p in (prev.get("min_price"), deal["price_pln"]) if p]
            archive[did] = {
                "id": did, "kind": "gf", "route": deal["route"], "cabin": "BUSINESS",
                "airline": f["airline_name"], "price_pln": f["price_pln"], "stars": stars,
                "tags": [], "title": "", "link": f["link"],
                "source": deal["source"], "duration_h": f["duration_h"],
                "stops": f["stops"], "departure": f.get("departure", ""), "date": date,
                "gf_low": level == "low", "roundtrip": False, "needs_feeder": False,
                "first_seen": prev.get("first_seen", now), "last_seen": now,
                "notified": prev.get("notified", False),
                "min_price": min(pr) if pr else None, "trend": prev.get("trend", "")}
            added += 1
    print("  %s → %s: OK" % (o, de))

if len(archive) > 500:
    keep = sorted(archive.values(), key=lambda d: d.get("last_seen", ""), reverse=True)[:500]
    archive = {d["id"]: d for d in keep}
json.dump(prices, open(os.path.join(S, "prices.json"), "w"), ensure_ascii=False, indent=1)
json.dump(archive, open(os.path.join(S, "deals.json"), "w"), ensure_ascii=False, indent=1)
from dashboard import write_dashboard
write_dashboard(archive, os.path.join(BASE, "site", "index.html"))
write_dashboard(archive, os.path.join(BASE, "dashboard.html"))
print("Gotowe: dopisano %d ofert, w bazie %d" % (added, len(archive)))
