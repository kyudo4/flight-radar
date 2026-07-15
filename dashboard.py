# -*- coding: utf-8 -*-
"""Generator dashboard.html — przegląd wszystkich znalezionych ofert."""

import json
from datetime import datetime

TEMPLATE = """<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>✈️ Flight Radar — oferty</title>
<style>
:root { --bg:#0d1117; --card:#161b22; --border:#30363d; --txt:#e6edf3;
        --dim:#8b949e; --accent:#58a6ff; --gold:#e3b341; --green:#3fb950;
        --btntext:#0d1117; }
:root[data-theme="light"] { --bg:#f6f8fa; --card:#ffffff; --border:#d0d7de;
        --txt:#1f2328; --dim:#656d76; --accent:#0969da; --gold:#9a6700;
        --green:#1a7f37; --btntext:#ffffff; }
* { box-sizing:border-box; margin:0; padding:0; }
body { background:var(--bg); color:var(--txt); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; padding:16px; transition:background .2s,color .2s; }
.wrap { max-width:980px; margin:0 auto; }
h1 { font-size:22px; margin-bottom:4px; }
.sub { color:var(--dim); font-size:13px; margin-bottom:16px; }
.stats { display:flex; gap:10px; flex-wrap:wrap; margin-bottom:16px; }
.stat { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:10px 14px; }
.stat b { display:block; font-size:18px; }
.stat span { color:var(--dim); font-size:11px; text-transform:uppercase; letter-spacing:.5px; }
.filters { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:16px; align-items:center; }
select, .tgl { background:var(--card); color:var(--txt); border:1px solid var(--border);
        border-radius:8px; padding:8px 10px; font-size:14px; cursor:pointer; }
.tgl.on { border-color:var(--accent); color:var(--accent); }
.card { background:var(--card); border:1px solid var(--border); border-radius:12px;
        padding:14px 16px; margin-bottom:10px; }
.card.s5 { border-left:4px solid var(--gold); }
.card.s4 { border-left:4px solid var(--green); }
.row1 { display:flex; justify-content:space-between; align-items:baseline; gap:10px; flex-wrap:wrap; }
.route { font-size:17px; font-weight:600; }
.price { font-size:20px; font-weight:700; color:var(--gold); white-space:nowrap; }
.meta { color:var(--dim); font-size:13px; margin-top:6px; line-height:1.6; }
.badges { margin-top:8px; display:flex; gap:6px; flex-wrap:wrap; }
.badge { font-size:11px; padding:3px 8px; border-radius:20px; border:1px solid var(--border); color:var(--dim); }
.badge.hot { border-color:var(--gold); color:var(--gold); }
.badge.tg { border-color:var(--accent); color:var(--accent); }
.badge.low { border-color:var(--green); color:var(--green); }
.title { margin-top:8px; font-size:13px; color:var(--dim); font-style:italic; }
a.go { display:inline-block; margin-top:10px; background:var(--accent); color:var(--btntext);
       font-weight:600; text-decoration:none; padding:7px 14px; border-radius:8px; font-size:13px; }
.empty { color:var(--dim); text-align:center; padding:40px 0; }
.stars { letter-spacing:2px; }
.theme-btn { position:fixed; top:14px; right:14px; z-index:50; width:40px; height:40px;
        border-radius:50%; border:1px solid var(--border); background:var(--card);
        color:var(--txt); font-size:19px; line-height:1; cursor:pointer;
        display:flex; align-items:center; justify-content:center;
        box-shadow:0 2px 8px rgba(0,0,0,.25); transition:transform .15s; }
.theme-btn:hover { transform:scale(1.08); }
@media (max-width: 560px) {
  body { padding: 12px; }
  h1 { font-size: 20px; }
  .sub { padding-right: 44px; }           /* nie pod przyciskiem motywu */
  .stats { gap: 8px; }
  .stat { flex: 1 1 44%; }
  .filters { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .filters select, .filters .tgl { width: 100%; }
  .price { font-size: 18px; }
  .card { padding: 12px 13px; }
  .theme-btn { position: absolute; }  /* zostaje na górze, nie jedzie ze scrollem */
}
</style>
</head>
<body>
<button id="themeBtn" class="theme-btn" title="Przełącz motyw">☀️</button>
<div class="wrap">
<h1>✈️ Flight Radar</h1>
<div class="sub">Business/First do Azji · wygenerowano __GENERATED__</div>
<div class="stats" id="stats"></div>
<div class="filters">
  <select id="fStars"><option value="0">Wszystkie gwiazdki</option>
    <option value="5">tylko ⭐⭐⭐⭐⭐</option><option value="4" selected>od ⭐⭐⭐⭐</option>
    <option value="3">od ⭐⭐⭐</option></select>
  <select id="fOrigin"><option value="">Wszystkie lotniska</option></select>
  <select id="fDest"><option value="">Wszystkie kierunki</option></select>
  <select id="fSrc"><option value="">Wszystkie źródła</option></select>
  <select id="fSort"><option value="stars">Sortuj: najlepsze</option>
    <option value="price">Sortuj: cena</option><option value="new">Sortuj: najnowsze</option></select>
  <div class="tgl" id="fTg">📨 tylko wysłane</div>
</div>
<div id="list"></div>
</div>
<script>
const DEALS = __DATA__;
const $ = id => document.getElementById(id);
let onlyTg = false;
// motyw (zapamiętany w przeglądarce) — przycisk-słoneczko w rogu
function applyTheme(light) {
  if (light) document.documentElement.setAttribute("data-theme", "light");
  else document.documentElement.removeAttribute("data-theme");
  $("themeBtn").textContent = light ? "🌙" : "☀️";
  try { localStorage.setItem("fr-theme", light ? "light" : "dark"); } catch (e) {}
}
try { applyTheme(localStorage.getItem("fr-theme") === "light"); } catch (e) {}
$("themeBtn").onclick = () =>
  applyTheme(document.documentElement.getAttribute("data-theme") !== "light");
const origin = d => d.route.split("→")[0].trim();
const dests = [...new Set(DEALS.map(d => d.route.split("→").pop().trim()))].sort();
const origins = [...new Set(DEALS.map(origin).filter(Boolean))].sort();
const srcs = [...new Set(DEALS.map(d => d.source))].sort();
origins.forEach(x => $("fOrigin").insertAdjacentHTML("beforeend", `<option>${x}</option>`));
dests.forEach(x => $("fDest").insertAdjacentHTML("beforeend", `<option>${x}</option>`));
srcs.forEach(x => $("fSrc").insertAdjacentHTML("beforeend", `<option>${x}</option>`));
const fmtP = p => p ? p.toLocaleString("pl-PL") + " PLN" : "cena w art.";
const fmtT = iso => iso ? iso.replace("T", " ").slice(5, 16) : "";
function render() {
  const ms = +$("fStars").value, de = $("fDest").value, sr = $("fSrc").value,
        or = $("fOrigin").value;
  let rows = DEALS.filter(d => d.stars >= ms
    && (!or || origin(d) === or) && (!de || d.route.split("→").pop().trim() === de)
    && (!sr || d.source === sr) && (!onlyTg || d.notified));
  // grupowanie powtórek: ta sama trasa+linia+klasa+ocena (różne daty) → 1 karta
  const groups = {};
  rows.forEach(d => {
    const k = d.route + "|" + d.airline + "|" + d.cabin + "|" + d.stars;
    (groups[k] = groups[k] || []).push(d);
  });
  rows = Object.values(groups).map(g => {
    const rep = g.reduce((m, d) => (d.price_pln || 9e9) < (m.price_pln || 9e9) ? d : m);
    const ps = g.map(d => d.price_pln).filter(Boolean);
    const ds = [...new Set(g.map(d => d.date).filter(Boolean))];
    return Object.assign({}, rep, { _count: ds.length || g.length,
      _max: ps.length ? Math.max(...ps) : null });
  });
  const sort = $("fSort").value;
  rows.sort((a, b) => sort === "price" ? (a.price_pln || 9e9) - (b.price_pln || 9e9)
    : sort === "new" ? (b.last_seen || "").localeCompare(a.last_seen || "")
    : b.stars - a.stars || (a.price_pln || 9e9) - (b.price_pln || 9e9));
  const live = DEALS.filter(d => d.kind !== "rss" && d.price_pln);
  const best = live.length ? live.reduce((m, d) => d.price_pln < m.price_pln ? d : m) : null;
  // mediana cen business do BKK (punkt odniesienia "ile normalnie kosztuje")
  const bkk = live.filter(d => d.cabin === "BUSINESS"
    && d.route.split("→").pop().trim() === "BKK").map(d => d.price_pln).sort((a, b) => a - b);
  const medBkk = bkk.length ? bkk[Math.floor(bkk.length / 2)] : null;
  $("stats").innerHTML = `
    <div class="stat"><b>${DEALS.length}</b><span>ofert w bazie</span></div>
    <div class="stat"><b>${DEALS.filter(d => d.stars >= 4).length}</b><span>wartych uwagi</span></div>
    <div class="stat"><b>${best ? fmtP(best.price_pln) : "—"}</b><span>najtaniej live ${best ? "(" + best.route + ")" : ""}</span></div>
    <div class="stat"><b>${medBkk ? fmtP(medBkk) : "—"}</b><span>mediana business → BKK</span></div>`;
  $("list").innerHTML = rows.map(d => `
    <div class="card s${d.stars}">
      <div class="row1">
        <div><span class="stars">${"⭐".repeat(d.stars)}</span>
          <span class="route"> ${d.route}</span></div>
        <div class="price">${fmtP(d.price_pln)}</div>
      </div>
      <div class="meta">
        ${d.airline ? "✈️ " + d.airline + " · " : ""}${d.cabin === "FIRST" ? "First" : "Business"}
        ${d.date ? " · 🗓 " + d.date + (d._count > 1 ? " (najtańsza)" : "") : ""}
        ${d.stops != null ? " · " + (d.stops === 0 ? "bez przesiadek" : d.stops + " przes.") : ""}
        ${d.duration_h ? " · " + Math.floor(d.duration_h) + "h " + Math.round(d.duration_h % 1 * 60) + "m" : ""}
        · znaleziono ${fmtT(d.first_seen)}
      </div>
      <div class="badges">
        <span class="badge">${d.source}</span>
        ${d._count > 1 ? `<span class="badge">🗓 ${d._count} dat${d._max && d._max > d.price_pln ? " · do " + fmtP(d._max) : ""}</span>` : ""}
        ${d.tags.map(t => `<span class="badge hot">🏷 ${t}</span>`).join("")}
        ${d.gf_low ? `<span class="badge low">📊 taniej niż zwykle</span>` : ""}
        ${d.roundtrip ? `<span class="badge">↔️ w obie strony</span>` : ""}
        ${d.needs_feeder ? `<span class="badge">📍 z dolotem</span>` : ""}
        ${d.notified ? `<span class="badge tg">📨 wysłano</span>` : ""}
        ${d.trend ? `<span class="badge ${d.trend.includes("📉") ? "low" : ""}">${d.trend}</span>` : ""}
        ${d.min_price && d.price_pln && d.min_price < d.price_pln ? `<span class="badge low">min: ${fmtP(d.min_price)}</span>` : ""}
      </div>
      ${d.title ? `<div class="title">${d.title}</div>` : ""}
      <a class="go" href="${d.link}" target="_blank" rel="noopener">Otwórz ofertę →</a>
    </div>`).join("") || emptyMsg(de, sr, ms);
}
function emptyMsg(de, sr, ms) {
  // ile ofert byłoby po zdjęciu filtra gwiazdek (częsty powód "pustki":
  // domyślnie od ⭐⭐⭐⭐, a droższe kierunki mają niższe oceny)
  const or = $("fOrigin").value;
  const relaxed = DEALS.filter(d => (!or || origin(d) === or)
    && (!de || d.route.split("→").pop().trim() === de)
    && (!sr || d.source === sr) && (!onlyTg || d.notified));
  if (relaxed.length && ms > 0)
    return `<div class="empty">Brak ofert ${"⭐".repeat(ms)}+ dla tych filtrów.<br>
      Jest ${relaxed.length} niżej ocenionych — ustaw filtr na „Wszystkie gwiazdki".</div>`;
  return `<div class="empty">Brak ofert dla tych filtrów</div>`;
}
["fStars", "fOrigin", "fDest", "fSrc", "fSort"].forEach(id => $(id).onchange = render);
$("fTg").onclick = () => { onlyTg = !onlyTg; $("fTg").classList.toggle("on", onlyTg); render(); };
render();
</script>
</body>
</html>"""


def write_dashboard(archive, path):
    deals = sorted(archive.values(),
                   key=lambda d: d.get("last_seen", ""), reverse=True)
    page = (TEMPLATE
            .replace("__GENERATED__", datetime.now().strftime("%Y-%m-%d %H:%M"))
            .replace("__DATA__", json.dumps(deals, ensure_ascii=False)))
    with open(path, "w") as f:
        f.write(page)
