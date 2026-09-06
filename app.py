import json
import math
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import quote_plus

import pandas as pd
import requests
import streamlit as st

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "tenders.db"
TED_SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"
DEFAULT_CAPITAL = 2000.0

st.set_page_config(page_title="Tender Scout Pro", page_icon="📦", layout="wide", initial_sidebar_state="collapsed")

st.markdown("""
<style>
.block-container {padding-top: 1rem; padding-bottom: 4rem; max-width: 1150px;}
[data-testid="stMetricValue"] {font-size: 1.55rem;}
div.stButton > button {min-height: 3rem; border-radius: 12px; font-weight: 700;}
div[data-testid="stExpander"] {border-radius: 14px;}
.tender-card {border:1px solid rgba(128,128,128,.28); border-radius:16px; padding:16px; margin:10px 0;}
.small {opacity:.72; font-size:.9rem;}
@media (max-width: 700px) {
  .block-container {padding-left:.8rem; padding-right:.8rem;}
  h1 {font-size:1.8rem !important;}
  h2 {font-size:1.35rem !important;}
}
</style>
""", unsafe_allow_html=True)


def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS tenders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT,
            external_id TEXT,
            title TEXT,
            buyer TEXT,
            country TEXT,
            publication_date TEXT,
            deadline TEXT,
            estimated_value REAL,
            description TEXT,
            url TEXT,
            raw_json TEXT,
            first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source, external_id)
        )
    """)
    # Migration from the first MVP database.
    cols = {r[1] for r in con.execute("PRAGMA table_info(tenders)").fetchall()}
    for name, ddl in [
        ("publication_date", "TEXT"),
        ("first_seen", "TEXT"),
        ("updated_at", "TEXT"),
    ]:
        if name not in cols:
            con.execute(f"ALTER TABLE tenders ADD COLUMN {name} {ddl}")
    con.commit()
    return con


def as_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return " | ".join(as_text(x) for x in v if x is not None)
    if isinstance(v, dict):
        for lang in ("deu", "de", "eng", "en"):
            if lang in v:
                return as_text(v[lang])
        return " | ".join(as_text(x) for x in v.values() if x is not None)
    return str(v)


def first_value(obj: Dict[str, Any], keys: List[str], default=""):
    for k in keys:
        if k in obj and obj[k] not in (None, "", [], {}):
            return obj[k]
    norm = {re.sub(r"[^a-z0-9]", "", str(k).lower()): k for k in obj}
    for k in keys:
        nk = re.sub(r"[^a-z0-9]", "", k.lower())
        if nk in norm:
            return obj[norm[nk]]
    return default


def parse_number(v: Any):
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, list):
        for x in v:
            n = parse_number(x)
            if n is not None:
                return n
        return None
    s = as_text(v).replace("€", "").replace("EUR", "").strip()
    s = re.sub(r"[^0-9,.-]", "", s)
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def normalize_ted_item(item: Dict[str, Any]) -> Dict[str, Any]:
    external_id = as_text(first_value(item, ["publication-number", "publicationNumber", "notice-id", "noticeId", "id"]))
    title = as_text(first_value(item, ["notice-title", "noticeTitle", "title", "procedure-title", "BT-21-Procedure"]))
    buyer = as_text(first_value(item, ["buyer-name", "buyerName", "organisation-name-buyer", "organizationName", "buyer"]))
    publication_date = as_text(first_value(item, ["publication-date", "publicationDate", "date-publication"]))
    deadline = as_text(first_value(item, ["deadline-receipt-tender", "deadline", "submission-deadline", "BT-131(d)-Lot"]))
    value = parse_number(first_value(item, ["estimated-value-procurement", "estimatedValue", "value", "BT-27-Procedure"]))
    country = as_text(first_value(item, ["place-of-performance-country-proc", "country", "buyer-country", "BT-5141-Procedure"]))
    description = as_text(first_value(item, ["description-proc", "description", "short-description", "BT-24-Procedure"]))
    urls = first_value(item, ["links", "urls", "url", "notice-url"], "")
    url = ""
    txt = as_text(urls)
    match = re.search(r"https?://[^\s|]+", txt)
    if match:
        url = match.group(0)
    if not url and external_id:
        url = f"https://ted.europa.eu/de/notice/-/detail/{external_id}"
    if not external_id:
        external_id = str(abs(hash(json.dumps(item, sort_keys=True, default=str))))
    return {
        "source": "TED",
        "external_id": external_id,
        "title": title or "Unbenannte Ausschreibung",
        "buyer": buyer,
        "country": country,
        "publication_date": publication_date,
        "deadline": deadline,
        "estimated_value": value,
        "description": description,
        "url": url,
        "raw_json": json.dumps(item, ensure_ascii=False, default=str),
    }


def ted_search(query: str, limit: int = 100) -> List[Dict[str, Any]]:
    fields = [
        "publication-number", "notice-title", "buyer-name", "publication-date",
        "deadline-receipt-tender", "estimated-value-procurement",
        "place-of-performance-country-proc", "description-proc", "links",
    ]
    payload = {
        "query": query,
        "fields": fields,
        "page": 1,
        "limit": limit,
        "scope": "ACTIVE",
        "checkQuerySyntax": False,
        "paginationMode": "PAGE_NUMBER",
    }
    r = requests.post(TED_SEARCH_URL, json=payload, timeout=30)
    if r.status_code >= 400:
        payload = {"query": query, "page": 1, "limit": limit, "paginationMode": "PAGE_NUMBER"}
        r = requests.post(TED_SEARCH_URL, json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    items = data.get("notices") or data.get("results") or data.get("items") or data.get("content") or []
    if isinstance(items, dict):
        items = items.get("items") or items.get("content") or []
    return [normalize_ted_item(x) for x in items if isinstance(x, dict)]


def save_tenders(items: List[Dict[str, Any]]) -> tuple[int, int]:
    con = db()
    new_count = 0
    updated_count = 0
    for x in items:
        existing = con.execute("SELECT id FROM tenders WHERE source=? AND external_id=?", (x.get("source", "Import"), x.get("external_id", ""))).fetchone()
        if existing:
            con.execute("""
                UPDATE tenders SET title=?, buyer=?, country=?, publication_date=?, deadline=?, estimated_value=?,
                description=?, url=?, raw_json=?, updated_at=CURRENT_TIMESTAMP WHERE id=?
            """, (x.get("title", ""), x.get("buyer", ""), x.get("country", ""), x.get("publication_date", ""),
                  x.get("deadline", ""), x.get("estimated_value"), x.get("description", ""), x.get("url", ""),
                  x.get("raw_json", "{}"), existing[0]))
            updated_count += 1
        else:
            con.execute("""
                INSERT INTO tenders (source, external_id, title, buyer, country, publication_date, deadline,
                estimated_value, description, url, raw_json, first_seen, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """, (x.get("source", "Import"), x.get("external_id", ""), x.get("title", ""), x.get("buyer", ""),
                  x.get("country", ""), x.get("publication_date", ""), x.get("deadline", ""), x.get("estimated_value"),
                  x.get("description", ""), x.get("url", ""), x.get("raw_json", "{}")))
            new_count += 1
    con.commit(); con.close()
    return new_count, updated_count


# The app is intended for ordinary commercial goods only.
BLOCKED = ["munition", "waffe", "waffen", "sprengstoff", "sprengmittel", "rakete", "torpedo", "explosivstoff"]
TRADE_GOODS = {
    "büromaterial": 10, "papier": 6, "toner": 9, "drucker": 5, "werkzeug": 9, "handwerkzeug": 8,
    "ersatzteil": 8, "reinigung": 8, "verbrauchsmaterial": 10, "zubehör": 8, "kabel": 9, "adapter": 9,
    "befestigung": 8, "schraube": 7, "lager": 6, "möbel": 6, "textil": 6, "elektro": 7,
    "hardware": 6, "computer": 5, "monitor": 6, "leuchte": 6, "batterie": 6, "verpackung": 7,
    "hygiene": 8, "filter": 7, "schlauch": 7, "stecker": 8, "netzteil": 8, "messgerät": 6,
}
RISKS = {
    "sicherheitsüberprüfung": -25, "vs-nfd": -22, "geheim": -25, "referenzen": -7, "umsatznachweis": -10,
    "bankbürgschaft": -15, "sicherheitsleistung": -12, "eigenerklärung": -3, "iso 9001": -7,
    "24 monate": -5, "36 monate": -6, "48 monate": -8, "rahmenvereinbarung": -4,
}


def parse_date(text: str):
    if not text:
        return None
    for pat in [r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", r"(\d{1,2})[.](\d{1,2})[.](20\d{2})"]:
        m = re.search(pat, str(text))
        if m:
            try:
                if pat.startswith("(20"):
                    return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            except ValueError:
                return None
    return None


def extract_search_term(title: str, description: str) -> str:
    text = re.sub(r"[^A-Za-zÄÖÜäöüß0-9+./ -]", " ", f"{title} {description}")
    text = re.sub(r"\s+", " ", text).strip()
    # Prefer recognizable product keywords plus nearby words, otherwise use the title.
    low = text.lower()
    hits = [kw for kw in TRADE_GOODS if kw in low]
    if hits:
        kw = hits[0]
        idx = low.find(kw)
        start = max(0, idx - 35); end = min(len(text), idx + len(kw) + 45)
        return text[start:end].strip()[:110]
    return (title or text)[:110]


def supplier_links(term: str) -> List[tuple[str, str]]:
    q = quote_plus(term)
    return [
        ("Google Lieferantensuche", f"https://www.google.com/search?q={q}+Gro%C3%9Fhandel+Deutschland+B2B"),
        ("Mercateo / Unite", f"https://www.google.com/search?q=site%3Aunite.eu+{q}"),
        ("RS Components", f"https://www.google.com/search?q=site%3Ade.rs-online.com+{q}"),
        ("Conrad Business", f"https://www.google.com/search?q=site%3Aconrad.de+{q}"),
        ("Würth", f"https://www.google.com/search?q=site%3Awuerth.de+{q}"),
        ("Distrelec", f"https://www.google.com/search?q=site%3Adistrelec.de+{q}"),
        ("Farnell", f"https://www.google.com/search?q=site%3Ade.farnell.com+{q}"),
    ]


def analyze(row: Dict[str, Any], capital: float, margin_pct: float, reserve_pct: float) -> Dict[str, Any]:
    text = f"{row.get('title','')} {row.get('description','')}".lower()
    blocked_hits = [kw for kw in BLOCKED if kw in text]
    if blocked_hits:
        return {
            "Score": 0, "Bewertung": "⛔ Ausgeschlossen", "Geschätzter Einkauf €": None,
            "Geschätzter Rohertrag €": None, "Finanzierungslücke €": None, "Tage bis Frist": None,
            "Pluspunkte": "", "Risiken": "Regulierte/waffenbezogene Beschaffung erkannt",
            "Beschaffungssuche": "", "blocked": True,
        }

    score = 48.0
    reasons, flags = [], []
    for kw, pts in TRADE_GOODS.items():
        if kw in text:
            score += pts
            reasons.append(f"gut beschaffbare Handelsware: {kw}")
    for kw, pts in RISKS.items():
        if kw in text:
            score += pts
            flags.append(kw)

    value = row.get("estimated_value")
    value = float(value) if value not in (None, "") and not pd.isna(value) else None
    usable = capital * (1 - reserve_pct / 100)
    est_purchase = est_profit = gap = None
    if value and value > 0:
        est_purchase = value / (1 + margin_pct / 100)
        est_profit = value - est_purchase
        gap = max(0.0, est_purchase - usable)
        if est_purchase <= usable:
            score += 25; reasons.append("Einkauf voraussichtlich aus verfügbarem Kapital finanzierbar")
        elif est_purchase <= usable * 1.5:
            score += 2; flags.append("kleine Vorfinanzierungslücke")
        else:
            score -= 24; flags.append("Kapitalbedarf voraussichtlich zu hoch")
    else:
        score -= 5; flags.append("Auftragswert nicht automatisch erkannt")

    deadline_date = parse_date(str(row.get("deadline") or ""))
    days_left = (deadline_date - date.today()).days if deadline_date else None
    if days_left is not None:
        if days_left < 0:
            score -= 60; flags.append("Frist abgelaufen")
        elif days_left <= 3:
            score -= 15; flags.append("sehr kurze Angebotsfrist")
        elif days_left >= 10:
            score += 6; reasons.append("ausreichend Zeit für Angebot/Lieferantenanfrage")

    term = extract_search_term(row.get("title", ""), row.get("description", ""))
    score = max(0, min(100, round(score)))
    verdict = "🟢 Sehr interessant" if score >= 75 else "🟡 Prüfen" if score >= 55 else "🔴 Eher überspringen"
    return {
        "Score": score, "Bewertung": verdict,
        "Geschätzter Einkauf €": round(est_purchase, 2) if est_purchase else None,
        "Geschätzter Rohertrag €": round(est_profit, 2) if est_profit else None,
        "Finanzierungslücke €": round(gap, 2) if gap is not None else None,
        "Tage bis Frist": days_left,
        "Pluspunkte": "; ".join(dict.fromkeys(reasons)),
        "Risiken": "; ".join(dict.fromkeys(flags)),
        "Beschaffungssuche": term, "blocked": False,
    }


def load_df():
    con = db()
    df = pd.read_sql_query("SELECT * FROM tenders ORDER BY first_seen DESC, id DESC", con)
    con.close()
    return df


def normalize_import(df: pd.DataFrame, source: str):
    aliases = {
        "external_id": ["external_id", "id", "publication-number", "notice_id"],
        "title": ["title", "titel", "notice-title", "bezeichnung"], "buyer": ["buyer", "auftraggeber", "buyer-name", "organisation"],
        "country": ["country", "land"], "publication_date": ["publication_date", "veroeffentlichung", "publication-date"],
        "deadline": ["deadline", "frist", "submission_deadline"], "estimated_value": ["estimated_value", "auftragswert", "value", "wert"],
        "description": ["description", "beschreibung", "text"], "url": ["url", "link"],
    }
    cols = {str(c).lower().strip(): c for c in df.columns}
    out = []
    for i, row in df.iterrows():
        x = {"source": source}
        for target, names in aliases.items():
            found = next((cols[n] for n in names if n in cols), None)
            x[target] = row[found] if found is not None and not pd.isna(row[found]) else ""
        if not x["external_id"]:
            x["external_id"] = f"{source}-{i}-{abs(hash(str(row.to_dict())))}"
        x["estimated_value"] = parse_number(x["estimated_value"])
        x["raw_json"] = json.dumps(row.to_dict(), ensure_ascii=False, default=str)
        out.append(x)
    return out


def analyzed_df(capital, margin_pct, reserve_pct):
    df = load_df()
    if df.empty:
        return df
    rows = []
    for _, r in df.iterrows():
        base = r.to_dict(); base.update(analyze(base, capital, margin_pct, reserve_pct)); rows.append(base)
    return pd.DataFrame(rows).sort_values(["Score", "first_seen"], ascending=[False, False])


# ---------- Header / profile ----------
st.title("📦 Tender Scout Pro")
st.caption("Dein mobiler Scanner für normale B2B-Waren & öffentliche Lieferaufträge")

with st.sidebar:
    st.header("💶 Dein Profil")
    capital = st.number_input("Startkapital (€)", min_value=100.0, value=DEFAULT_CAPITAL, step=100.0)
    reserve_pct = st.slider("Reserve behalten (%)", 0, 80, 40, 5)
    margin_pct = st.slider("Zielmarge auf Einkauf (%)", 5, 100, 25, 5)
    st.metric("Für Einkauf verfügbar", f"{capital*(1-reserve_pct/100):,.0f} €")
    st.caption("Nur Vorprüfung. Originalunterlagen, Eignung, Steuer, Gewerbe und Vergabebedingungen selbst prüfen.")

# Mobile-first tabs.
tab_today, tab_detail, tab_search, tab_import = st.tabs(["🔥 Heute", "🔎 Analyse", "🌍 Suche", "📥 Import"])

with tab_today:
    st.subheader("Tages-Scan")
    st.write("Ein Tap lädt aktive TED-Ausschreibungen mit Leistungsort Deutschland und sortiert sie nach deinem Kapital und Handelswaren-Fit.")
    if st.button("🚀 Neue Ausschreibungen scannen", type="primary", use_container_width=True):
        with st.spinner("TED wird durchsucht …"):
            try:
                items = ted_search("place-of-performance IN (DE)", 120)
                new_n, upd_n = save_tenders(items)
                st.success(f"{new_n} neu · {upd_n} aktualisiert")
            except Exception as e:
                st.error(f"Live-Scan fehlgeschlagen: {e}")

    out = analyzed_df(capital, margin_pct, reserve_pct)
    if out.empty:
        st.info("Noch keine Daten. Tippe oben auf „Neue Ausschreibungen scannen“.")
    else:
        safe = out[~out["blocked"]].copy()
        c1, c2, c3 = st.columns(3)
        c1.metric("Gespeichert", len(safe))
        c2.metric("Top-Chancen", int((safe["Score"] >= 75).sum()))
        c3.metric("Kapital passt", int((safe["Finanzierungslücke €"].fillna(math.inf) <= 0).sum()))

        top = safe.head(12)
        for _, r in top.iterrows():
            val = "–" if pd.isna(r.get("estimated_value")) else f"{r['estimated_value']:,.0f} €"
            gap = r.get("Finanzierungslücke €")
            gap_txt = "–" if pd.isna(gap) else f"{gap:,.0f} €"
            with st.expander(f"{int(r['Score'])}/100 · {r['title'][:95]}"):
                st.write(r["Bewertung"])
                st.caption(f"Auftraggeber: {r.get('buyer') or '–'} · Frist: {r.get('deadline') or '–'}")
                a,b,c = st.columns(3)
                a.metric("Wert", val); b.metric("Rohertrag*", "–" if pd.isna(r.get("Geschätzter Rohertrag €")) else f"{r['Geschätzter Rohertrag €']:,.0f} €"); c.metric("Finanzierungslücke", gap_txt)
                if r.get("Pluspunkte"): st.success(r["Pluspunkte"])
                if r.get("Risiken"): st.warning(r["Risiken"])
                if r.get("url"): st.link_button("Original öffnen", r["url"], use_container_width=True)
        st.caption("*Reine Modellrechnung aus deiner Zielmarge, noch kein echter Lieferantenpreis.")

with tab_detail:
    st.subheader("Professionelle Voranalyse")
    out = analyzed_df(capital, margin_pct, reserve_pct)
    if out.empty:
        st.info("Erst einen Tages-Scan ausführen.")
    else:
        out = out[~out["blocked"]].copy()
        min_score = st.slider("Mindestscore", 0, 100, 55, 5)
        search_text = st.text_input("Filtern", placeholder="z. B. Kabel, Werkzeug, Bundeswehr …")
        show = out[out["Score"] >= min_score]
        if search_text:
            needle = search_text.lower()
            show = show[show.apply(lambda r: needle in f"{r.get('title','')} {r.get('buyer','')} {r.get('description','')}".lower(), axis=1)]
        if show.empty:
            st.warning("Keine Treffer mit diesen Filtern.")
        else:
            labels = {f"{int(r['Score'])}/100 · {r['title'][:85]}": i for i,r in show.iterrows()}
            sel = st.selectbox("Ausschreibung", list(labels.keys()))
            r = show.loc[labels[sel]]
            st.markdown(f"### {r['title']}")
            st.write(r["Bewertung"])
            a,b,c = st.columns(3)
            a.metric("Score", f"{int(r['Score'])}/100")
            b.metric("Einkauf*", "–" if pd.isna(r.get("Geschätzter Einkauf €")) else f"{r['Geschätzter Einkauf €']:,.0f} €")
            c.metric("Rohertrag*", "–" if pd.isna(r.get("Geschätzter Rohertrag €")) else f"{r['Geschätzter Rohertrag €']:,.0f} €")
            st.write("**Auftraggeber:**", r.get("buyer") or "–")
            st.write("**Veröffentlicht:**", r.get("publication_date") or "–")
            st.write("**Angebotsfrist:**", r.get("deadline") or "–")
            st.write("**Beschreibung:**", r.get("description") or "Keine Kurzbeschreibung geliefert.")
            if r.get("Pluspunkte"): st.success("Pluspunkte: " + r["Pluspunkte"])
            if r.get("Risiken"): st.warning("Prüfen: " + r["Risiken"])

            st.markdown("#### 🧾 Angebots-Checkliste")
            st.markdown("1. Originalunterlagen öffnen und exakte Artikel/Mengen notieren.\n2. Muss-Nachweise, Referenzen, Lieferfrist und Vertragsstrafe prüfen.\n3. Mindestens 2–3 schriftliche Lieferantenangebote einholen.\n4. Versand, Verpackung, Zahlungsziel und Rückläufer einkalkulieren.\n5. Erst danach deinen verbindlichen Angebotspreis festlegen.")

            st.markdown("#### 🏭 Wo kann ich die Ware beschaffen?")
            term = r.get("Beschaffungssuche") or r.get("title")
            st.caption(f"Suchbegriff: {term}")
            links = supplier_links(term)
            for name, url in links:
                st.link_button(f"↗ {name}", url, use_container_width=True)
            st.info("Die Links sind Lieferanten-Recherchen, keine Preisgarantie. Entscheidend ist ein schriftliches B2B-Angebot mit Lieferzeit und Verfügbarkeit.")
            if r.get("url"): st.link_button("📄 Originalausschreibung", r["url"], type="primary", use_container_width=True)

with tab_search:
    st.subheader("Eigene TED-Suche")
    st.caption("Für spezielle Auftraggeber oder Produktbereiche. Die offizielle TED Search API benötigt für veröffentlichte Bekanntmachungen keinen API-Key.")
    query = st.text_input("TED Expert Query", value="place-of-performance IN (DE)")
    limit = st.slider("Treffer", 10, 250, 100, 10)
    if st.button("Suchen & analysieren", type="primary", use_container_width=True):
        with st.spinner("Suche läuft …"):
            try:
                items = ted_search(query, limit)
                n,u = save_tenders(items)
                st.success(f"{n} neu · {u} aktualisiert")
            except Exception as e:
                st.error(f"TED-Abfrage fehlgeschlagen: {e}")

with tab_import:
    st.subheader("Andere Portale importieren")
    source_name = st.text_input("Quellenname", value="Manueller Import")
    uploaded = st.file_uploader("CSV oder JSON", type=["csv", "json"])
    if uploaded is not None:
        try:
            if uploaded.name.lower().endswith(".csv"):
                df_imp = pd.read_csv(uploaded)
            else:
                raw = json.load(uploaded)
                if isinstance(raw, dict): raw = raw.get("items") or raw.get("results") or raw.get("notices") or [raw]
                df_imp = pd.DataFrame(raw)
            st.dataframe(df_imp.head(15), use_container_width=True)
            if st.button("Importieren", use_container_width=True):
                n,u = save_tenders(normalize_import(df_imp, source_name))
                st.success(f"{n} neu · {u} aktualisiert")
        except Exception as e:
            st.error(f"Import nicht möglich: {e}")

st.divider()
st.caption("Tender Scout Pro · MVP für frei handelbare B2B-Waren · Keine Rechts-, Steuer- oder Vergabeberatung")
