import json
import math
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

import pandas as pd
import requests
import streamlit as st

# ============================================================
# Tender Scout Pro 2.0
# Mobile-first procurement opportunity scanner for ordinary B2B goods.
# ============================================================

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "tenders.db"
TED_SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"
APP_VERSION = "2.0.0"

st.set_page_config(
    page_title="Tender Scout Pro",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
<style>
.block-container {padding-top: .75rem; padding-bottom: 5rem; max-width: 1180px;}
[data-testid="stMetricValue"] {font-size: 1.45rem;}
div.stButton > button, div.stLinkButton > a {min-height: 3rem; border-radius: 13px; font-weight: 700;}
div[data-testid="stExpander"] {border-radius: 14px;}
.tsp-card {border:1px solid rgba(128,128,128,.28); border-radius:18px; padding:16px; margin:10px 0;}
.tsp-muted {opacity:.72; font-size:.9rem;}
.tsp-badge {display:inline-block; padding:4px 9px; border-radius:999px; border:1px solid rgba(128,128,128,.28); margin:2px 4px 2px 0; font-size:.82rem;}
@media (max-width: 700px) {
  .block-container {padding-left:.75rem; padding-right:.75rem;}
  h1 {font-size:1.8rem !important;}
  h2 {font-size:1.35rem !important;}
  h3 {font-size:1.15rem !important;}
}
</style>
""",
    unsafe_allow_html=True,
)

# ----------------------------- Data -----------------------------

def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS tenders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            external_id TEXT NOT NULL,
            title TEXT,
            buyer TEXT,
            country TEXT,
            publication_date TEXT,
            deadline TEXT,
            estimated_value REAL,
            description TEXT,
            url TEXT,
            cpv TEXT,
            contract_nature TEXT,
            notice_type TEXT,
            raw_json TEXT,
            first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source, external_id)
        )
        """
    )
    cols = {r[1] for r in con.execute("PRAGMA table_info(tenders)").fetchall()}
    migrations = {
        "publication_date": "TEXT",
        "first_seen": "TEXT",
        "updated_at": "TEXT",
        "cpv": "TEXT",
        "contract_nature": "TEXT",
        "notice_type": "TEXT",
    }
    for name, ddl in migrations.items():
        if name not in cols:
            con.execute(f"ALTER TABLE tenders ADD COLUMN {name} {ddl}")

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS watchlist (
            source TEXT NOT NULL,
            external_id TEXT NOT NULL,
            note TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source, external_id)
        )
        """
    )
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
        for lang in ("deu", "de", "ger", "eng", "en"):
            if lang in v:
                return as_text(v[lang])
        return " | ".join(as_text(x) for x in v.values() if x is not None)
    return str(v)


def first_value(obj: Dict[str, Any], keys: List[str], default="") -> Any:
    for k in keys:
        if k in obj and obj[k] not in (None, "", [], {}):
            return obj[k]
    normalized = {re.sub(r"[^a-z0-9]", "", str(k).lower()): k for k in obj.keys()}
    for k in keys:
        nk = re.sub(r"[^a-z0-9]", "", k.lower())
        if nk in normalized:
            real = normalized[nk]
            if obj[real] not in (None, "", [], {}):
                return obj[real]
    return default


def parse_number(v: Any) -> Optional[float]:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, list):
        vals = [parse_number(x) for x in v]
        vals = [x for x in vals if x is not None]
        return max(vals) if vals else None
    if isinstance(v, dict):
        for key in ("value", "amount", "val"):
            if key in v:
                return parse_number(v[key])
        vals = [parse_number(x) for x in v.values()]
        vals = [x for x in vals if x is not None]
        return max(vals) if vals else None
    s = as_text(v).replace("€", "").replace("EUR", "").replace("eur", "").strip()
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


def parse_date(text: str) -> Optional[date]:
    if not text:
        return None
    s = str(text)
    patterns = [
        (r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", "ymd"),
        (r"(\d{1,2})[.](\d{1,2})[.](20\d{2})", "dmy"),
    ]
    for pat, kind in patterns:
        m = re.search(pat, s)
        if not m:
            continue
        try:
            if kind == "ymd":
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
    return None


def clean_url(v: Any, external_id: str) -> str:
    txt = as_text(v)
    m = re.search(r"https?://[^\s|\]\[\"']+", txt)
    if m:
        return m.group(0).rstrip(",.;")
    if external_id:
        return f"https://ted.europa.eu/de/notice/-/detail/{external_id}"
    return ""


def normalize_ted_item(item: Dict[str, Any]) -> Dict[str, Any]:
    external_id = as_text(first_value(item, ["publication-number", "publicationNumber", "notice-id", "noticeId", "id"]))
    title = as_text(first_value(item, ["notice-title", "noticeTitle", "title", "procedure-title", "BT-21-Procedure"]))
    buyer = as_text(first_value(item, ["buyer-name", "buyerName", "organisation-name-buyer", "organizationName", "buyer"]))
    publication_date = as_text(first_value(item, ["publication-date", "publicationDate", "date-publication"]))
    deadline = as_text(first_value(item, ["deadline-receipt-tender", "deadline", "submission-deadline", "BT-131(d)-Lot"]))
    value = parse_number(first_value(item, ["estimated-value-procurement", "estimatedValue", "value", "BT-27-Procedure"]))
    country = as_text(first_value(item, ["place-of-performance-country-proc", "place-of-performance", "country", "buyer-country", "BT-5141-Procedure"]))
    description = as_text(first_value(item, ["description-proc", "description", "short-description", "BT-24-Procedure"]))
    cpv = as_text(first_value(item, ["classification-cpv", "main-classification-proc", "cpv", "BT-262-Procedure"]))
    contract_nature = as_text(first_value(item, ["contract-nature", "nature", "contractNature", "BT-23-Procedure"]))
    notice_type = as_text(first_value(item, ["notice-type", "form-type", "noticeType"]))
    url = clean_url(first_value(item, ["links", "urls", "url", "notice-url"], ""), external_id)
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
        "cpv": cpv,
        "contract_nature": contract_nature,
        "notice_type": notice_type,
        "raw_json": json.dumps(item, ensure_ascii=False, default=str),
    }


# ----------------------------- TED API -----------------------------

# Conservative field set: these are actual TED search fields. URLs are returned by the API
# response separately, therefore "links" is intentionally NOT requested as a field.
TED_CORE_FIELDS = [
    "publication-number",
    "notice-title",
    "buyer-name",
    "publication-date",
    "deadline-receipt-tender",
    "estimated-value-procurement",
    "description-proc",
    "classification-cpv",
    "contract-nature",
    "notice-type",
]

TED_MIN_FIELDS = ["publication-number", "notice-title", "buyer-name", "publication-date"]


def ted_request(query: str, limit: int, fields: List[str]) -> requests.Response:
    payload = {
        "query": query,
        "fields": fields,
        "page": 1,
        "limit": int(limit),
        "scope": "ACTIVE",
        "checkQuerySyntax": False,
        "paginationMode": "PAGE_NUMBER",
    }
    return requests.post(
        TED_SEARCH_URL,
        json=payload,
        headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "TenderScoutPro/2.0"},
        timeout=35,
    )


def error_detail(resp: requests.Response) -> str:
    try:
        data = resp.json()
        txt = json.dumps(data, ensure_ascii=False)
    except Exception:
        txt = resp.text
    return txt[:700]


def ted_search(query: str, limit: int = 100) -> Tuple[List[Dict[str, Any]], str]:
    """Search TED with robust fallback. Returns (items, mode_message)."""
    attempts = [
        (query, TED_CORE_FIELDS, "vollständige Felder"),
        (query, TED_MIN_FIELDS, "Basisfelder"),
    ]
    last_error = ""
    for q, fields, label in attempts:
        resp = ted_request(q, limit, fields)
        if resp.ok:
            data = resp.json()
            items = data.get("notices") or data.get("results") or data.get("items") or data.get("content") or []
            if isinstance(items, dict):
                items = items.get("items") or items.get("content") or []
            normalized = [normalize_ted_item(x) for x in items if isinstance(x, dict)]
            return normalized, label
        last_error = f"HTTP {resp.status_code}: {error_detail(resp)}"

    raise RuntimeError(last_error or "TED hat die Abfrage abgelehnt.")


def build_ted_query(profile: str, keyword: str = "") -> str:
    # TED uses ISO-3166 alpha-3 country codes in place-of-performance search, e.g. DEU.
    base = "place-of-performance IN (DEU)"
    profile_queries = {
        "Alle Lieferaufträge": "contract-nature = supplies",
        "IT & Elektro": "classification-cpv IN (30* 31* 32*)",
        "Werkzeug & Industriebedarf": "classification-cpv IN (42* 43* 44*)",
        "Büro, Möbel & Verbrauch": "classification-cpv IN (30* 39*)",
        "Reinigung & Hygiene": "classification-cpv IN (33* 39*)",
        "Textilien & Schutzkleidung": "classification-cpv IN (18*)",
        "Nur Deutschland, breit": "",
    }
    extra = profile_queries.get(profile, "")
    parts = [base]
    if extra:
        parts.append(extra)
    if keyword.strip():
        safe = re.sub(r"[^\wÄÖÜäöüß+./ -]", " ", keyword).strip()
        if safe:
            parts.append(f"FT ~ {safe}")
    return " AND ".join(f"({p})" for p in parts)


# ----------------------------- Persistence -----------------------------

def save_tenders(items: List[Dict[str, Any]]) -> Tuple[int, int]:
    con = db()
    new_count = 0
    updated_count = 0
    for x in items:
        key = (x.get("source", "Import"), x.get("external_id", ""))
        existing = con.execute("SELECT id FROM tenders WHERE source=? AND external_id=?", key).fetchone()
        vals = (
            x.get("title", ""), x.get("buyer", ""), x.get("country", ""), x.get("publication_date", ""),
            x.get("deadline", ""), x.get("estimated_value"), x.get("description", ""), x.get("url", ""),
            x.get("cpv", ""), x.get("contract_nature", ""), x.get("notice_type", ""), x.get("raw_json", "{}"),
        )
        if existing:
            con.execute(
                """
                UPDATE tenders SET title=?, buyer=?, country=?, publication_date=?, deadline=?, estimated_value=?,
                description=?, url=?, cpv=?, contract_nature=?, notice_type=?, raw_json=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                vals + (existing[0],),
            )
            updated_count += 1
        else:
            con.execute(
                """
                INSERT INTO tenders
                (source, external_id, title, buyer, country, publication_date, deadline, estimated_value,
                 description, url, cpv, contract_nature, notice_type, raw_json, first_seen, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                key + vals,
            )
            new_count += 1
    con.commit()
    con.close()
    return new_count, updated_count


def load_df() -> pd.DataFrame:
    con = db()
    df = pd.read_sql_query("SELECT * FROM tenders ORDER BY first_seen DESC, id DESC", con)
    con.close()
    return df


def is_watchlisted(source: str, external_id: str) -> bool:
    con = db()
    hit = con.execute("SELECT 1 FROM watchlist WHERE source=? AND external_id=?", (source, external_id)).fetchone()
    con.close()
    return bool(hit)


def toggle_watchlist(source: str, external_id: str) -> bool:
    con = db()
    hit = con.execute("SELECT 1 FROM watchlist WHERE source=? AND external_id=?", (source, external_id)).fetchone()
    if hit:
        con.execute("DELETE FROM watchlist WHERE source=? AND external_id=?", (source, external_id))
        state = False
    else:
        con.execute("INSERT OR IGNORE INTO watchlist(source, external_id) VALUES (?, ?)", (source, external_id))
        state = True
    con.commit()
    con.close()
    return state


# ----------------------------- Commercial analysis -----------------------------

BLOCKED_TERMS = [
    "munition", "waffe", "waffen", "sprengstoff", "sprengmittel", "rakete", "torpedo",
    "explosivstoff", "feuerwaffe", "kampfmittel",
]

TRADE_GOODS = {
    "verbrauchsmaterial": 11, "büromaterial": 10, "toner": 10, "papier": 7, "werkzeug": 9,
    "handwerkzeug": 9, "ersatzteil": 8, "reinigung": 8, "hygiene": 8, "zubehör": 8,
    "kabel": 9, "adapter": 9, "stecker": 8, "netzteil": 8, "befestigung": 8, "schraube": 7,
    "filter": 7, "schlauch": 7, "verpackung": 7, "möbel": 6, "textil": 6, "batterie": 6,
    "leuchte": 6, "monitor": 6, "hardware": 6, "computer": 5, "messgerät": 6, "elektro": 7,
    "lager": 6, "drucker": 6, "arbeitskleidung": 7, "schutzkleidung": 7,
}

BARRIER_TERMS = {
    "sicherheitsüberprüfung": (-26, "Sicherheitsüberprüfung"),
    "vs-nfd": (-24, "VS-NfD / Sicherheitsanforderung"),
    "geheim": (-25, "Geheimschutz"),
    "bankbürgschaft": (-16, "Bankbürgschaft"),
    "sicherheitsleistung": (-13, "Sicherheitsleistung"),
    "umsatznachweis": (-10, "Umsatznachweis"),
    "mindestens 3 referenzen": (-11, "mehrere Referenzen"),
    "referenzen": (-6, "Referenzen prüfen"),
    "iso 9001": (-7, "ISO 9001"),
    "zertifizierung": (-6, "Zertifizierung"),
    "vertragsstrafe": (-7, "Vertragsstrafe"),
    "rahmenvereinbarung": (-3, "Rahmenvereinbarung"),
}


def extract_search_term(title: str, description: str) -> str:
    text = re.sub(r"[^A-Za-zÄÖÜäöüß0-9+./ -]", " ", f"{title} {description}")
    text = re.sub(r"\s+", " ", text).strip()
    low = text.lower()
    for kw in TRADE_GOODS:
        if kw in low:
            idx = low.find(kw)
            return text[max(0, idx - 35): min(len(text), idx + len(kw) + 60)].strip()[:120]
    return (title or text)[:120]


def supplier_links(term: str) -> List[Tuple[str, str]]:
    q = quote_plus(term)
    return [
        ("🔎 Google B2B / Großhandel", f"https://www.google.com/search?q={q}+Gro%C3%9Fhandel+B2B+Deutschland"),
        ("🏭 Hersteller suchen", f"https://www.google.com/search?q={q}+Hersteller+Deutschland+Distributor"),
        ("🧾 Unite / Mercateo", f"https://www.google.com/search?q=site%3Aunite.eu+{q}"),
        ("⚙️ RS", f"https://www.google.com/search?q=site%3Ade.rs-online.com+{q}"),
        ("🔌 Conrad", f"https://www.google.com/search?q=site%3Aconrad.de+{q}"),
        ("🛠️ Würth", f"https://www.google.com/search?q=site%3Awuerth.de+{q}"),
        ("💡 Farnell", f"https://www.google.com/search?q=site%3Ade.farnell.com+{q}"),
        ("📦 Distrelec", f"https://www.google.com/search?q=site%3Adistrelec.de+{q}"),
    ]


def analyze(row: Dict[str, Any], capital: float, target_markup: float, reserve_pct: float,
            include_keywords: str, exclude_keywords: str) -> Dict[str, Any]:
    title = str(row.get("title") or "")
    desc = str(row.get("description") or "")
    cpv = str(row.get("cpv") or "")
    text = f"{title} {desc} {cpv}".lower()

    blocked_hits = [kw for kw in BLOCKED_TERMS if kw in text]
    if blocked_hits:
        return {
            "Score": 0, "Bewertung": "⛔ Ausgeschlossen", "Status": "NO-GO", "blocked": True,
            "Geschätzter Einkauf €": None, "Geschätzter Rohertrag €": None, "Finanzierungslücke €": None,
            "Max. Einkauf für Zielmarge €": None, "Tage bis Frist": None, "Pluspunkte": "",
            "Risiken": "Regulierte/waffenbezogene Beschaffung erkannt", "Beschaffungssuche": "",
            "Confidence": "hoch", "NextAction": "Nicht bearbeiten.",
        }

    score = 45.0
    reasons: List[str] = []
    risks: List[str] = []

    # Product-fit signal
    product_hits = []
    for kw, pts in TRADE_GOODS.items():
        if kw in text:
            product_hits.append(kw)
            score += min(pts, 8)
    if product_hits:
        reasons.append("Handelsware erkennbar: " + ", ".join(product_hits[:4]))
        score = min(score, 68)  # cap before finance/deadline/barriers

    # User inclusion/exclusion keywords
    includes = [x.strip().lower() for x in re.split(r"[,;\n]", include_keywords) if x.strip()]
    excludes = [x.strip().lower() for x in re.split(r"[,;\n]", exclude_keywords) if x.strip()]
    inc_hits = [x for x in includes if x in text]
    exc_hits = [x for x in excludes if x in text]
    if inc_hits:
        score += min(12, 4 * len(inc_hits))
        reasons.append("Deine Wunschbegriffe: " + ", ".join(inc_hits[:3]))
    if exc_hits:
        score -= min(25, 10 * len(exc_hits))
        risks.append("Von dir ausgeschlossen: " + ", ".join(exc_hits[:3]))

    # Barriers
    for kw, (pts, label) in BARRIER_TERMS.items():
        if kw in text:
            score += pts
            risks.append(label)

    # Contract nature preference
    nature = str(row.get("contract_nature") or "").lower()
    if "suppl" in nature or "liefer" in nature:
        score += 8
        reasons.append("Lieferauftrag")
    elif nature and ("service" in nature or "dienst" in nature or "works" in nature or "bau" in nature):
        score -= 10
        risks.append("Nicht primär Warenlieferung")

    # Finance model
    value = row.get("estimated_value")
    try:
        value = float(value) if value not in (None, "") and not pd.isna(value) else None
    except Exception:
        value = None

    usable = max(0.0, capital * (1 - reserve_pct / 100))
    est_purchase = est_profit = gap = max_buy = None
    if value and value > 0:
        max_buy = value / (1 + target_markup / 100)
        est_purchase = max_buy
        est_profit = value - est_purchase
        gap = max(0.0, est_purchase - usable)
        ratio = est_purchase / usable if usable > 0 else math.inf
        if ratio <= 1.0:
            score += 23
            reasons.append("Modell-Einkauf aus deinem freien Kapital finanzierbar")
        elif ratio <= 1.25:
            score += 5
            risks.append("kleine Finanzierungslücke")
        elif ratio <= 2.0:
            score -= 12
            risks.append("deutliche Vorfinanzierung nötig")
        else:
            score -= 27
            risks.append("Kapitalbedarf weit über deinem aktuellen Budget")
    else:
        score -= 4
        risks.append("Auftragswert fehlt – manuell kalkulieren")

    # Deadline
    deadline_date = parse_date(str(row.get("deadline") or ""))
    days_left = (deadline_date - date.today()).days if deadline_date else None
    if days_left is None:
        risks.append("Angebotsfrist nicht automatisch erkannt")
    elif days_left < 0:
        score -= 70
        risks.append("Frist abgelaufen")
    elif days_left <= 2:
        score -= 18
        risks.append("extrem kurze Angebotsfrist")
    elif days_left <= 5:
        score -= 8
        risks.append("kurze Angebotsfrist")
    elif days_left >= 14:
        score += 7
        reasons.append("gute Zeit für Lieferantenanfragen")

    # Confidence and recommended action
    confidence_points = sum([
        bool(title), bool(desc), bool(row.get("buyer")), bool(row.get("deadline")), bool(value), bool(cpv)
    ])
    confidence = "hoch" if confidence_points >= 5 else "mittel" if confidence_points >= 3 else "niedrig"

    score = int(max(0, min(100, round(score))))
    if score >= 78:
        verdict, status, action = "🟢 Sehr interessant", "GO", "Originalunterlagen öffnen und heute 2–3 Lieferanten anfragen."
    elif score >= 60:
        verdict, status, action = "🟡 Prüfen", "CHECK", "Muss-Kriterien und Mengen prüfen; danach Einkaufspreise einholen."
    elif score >= 40:
        verdict, status, action = "🟠 Nur bei gutem Einkaufspreis", "MAYBE", "Nur weiterverfolgen, wenn Beschaffung sehr einfach oder Finanzierung lösbar ist."
    else:
        verdict, status, action = "🔴 Eher überspringen", "NO-GO", "Zeit lieber in höher bewertete Chancen investieren."

    return {
        "Score": score,
        "Bewertung": verdict,
        "Status": status,
        "blocked": False,
        "Geschätzter Einkauf €": round(est_purchase, 2) if est_purchase is not None else None,
        "Geschätzter Rohertrag €": round(est_profit, 2) if est_profit is not None else None,
        "Finanzierungslücke €": round(gap, 2) if gap is not None else None,
        "Max. Einkauf für Zielmarge €": round(max_buy, 2) if max_buy is not None else None,
        "Tage bis Frist": days_left,
        "Pluspunkte": "; ".join(dict.fromkeys(reasons)),
        "Risiken": "; ".join(dict.fromkeys(risks)),
        "Beschaffungssuche": extract_search_term(title, desc),
        "Confidence": confidence,
        "NextAction": action,
    }


def analyzed_df(capital: float, markup: float, reserve: float, include_kw: str, exclude_kw: str) -> pd.DataFrame:
    df = load_df()
    if df.empty:
        return df
    rows = []
    for _, r in df.iterrows():
        base = r.to_dict()
        base.update(analyze(base, capital, markup, reserve, include_kw, exclude_kw))
        rows.append(base)
    out = pd.DataFrame(rows)
    return out.sort_values(["Score", "first_seen"], ascending=[False, False])


# ----------------------------- Import -----------------------------

def normalize_import(df: pd.DataFrame, source: str) -> List[Dict[str, Any]]:
    aliases = {
        "external_id": ["external_id", "id", "publication-number", "notice_id"],
        "title": ["title", "titel", "notice-title", "bezeichnung"],
        "buyer": ["buyer", "auftraggeber", "buyer-name", "organisation"],
        "country": ["country", "land"],
        "publication_date": ["publication_date", "veroeffentlichung", "publication-date"],
        "deadline": ["deadline", "frist", "submission_deadline"],
        "estimated_value": ["estimated_value", "auftragswert", "value", "wert"],
        "description": ["description", "beschreibung", "text"],
        "url": ["url", "link"],
        "cpv": ["cpv", "classification-cpv"],
        "contract_nature": ["contract_nature", "contract-nature", "auftragsart"],
        "notice_type": ["notice_type", "notice-type"],
    }
    cols = {str(c).lower().strip(): c for c in df.columns}
    out = []
    for i, row in df.iterrows():
        x: Dict[str, Any] = {"source": source}
        for target, names in aliases.items():
            found = next((cols[n] for n in names if n in cols), None)
            x[target] = row[found] if found is not None and not pd.isna(row[found]) else ""
        if not x["external_id"]:
            x["external_id"] = f"{source}-{i}-{abs(hash(str(row.to_dict())))}"
        x["estimated_value"] = parse_number(x["estimated_value"])
        x["raw_json"] = json.dumps(row.to_dict(), ensure_ascii=False, default=str)
        out.append(x)
    return out


# ----------------------------- Helpers UI -----------------------------

def euro(v: Any) -> str:
    try:
        if v is None or pd.isna(v):
            return "–"
        return f"{float(v):,.0f} €".replace(",", ".")
    except Exception:
        return "–"


def tender_label(r: pd.Series) -> str:
    title = str(r.get("title") or "Unbenannt")
    return f"{int(r['Score'])}/100 · {title[:82]}"


def render_opportunity(r: pd.Series, compact: bool = False) -> None:
    title = str(r.get("title") or "Unbenannte Ausschreibung")
    st.markdown(f"### {title}")
    st.caption(f"{r.get('buyer') or 'Auftraggeber unbekannt'} · TED {r.get('external_id') or ''}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Score", f"{int(r['Score'])}/100")
    c2.metric("Auftragswert", euro(r.get("estimated_value")))
    c3.metric("Modell-Einkauf*", euro(r.get("Geschätzter Einkauf €")))
    c4.metric("Finanzierungslücke", euro(r.get("Finanzierungslücke €")))
    st.write(r.get("Bewertung"), "· Datenqualität:", r.get("Confidence", "–"))
    if r.get("Pluspunkte"):
        st.success(r["Pluspunkte"])
    if r.get("Risiken"):
        st.warning(r["Risiken"])
    st.info("**Nächster Schritt:** " + str(r.get("NextAction") or "Originalunterlagen prüfen."))
    if not compact:
        st.write("**Veröffentlicht:**", r.get("publication_date") or "–")
        st.write("**Angebotsfrist:**", r.get("deadline") or "–")
        st.write("**CPV:**", r.get("cpv") or "–")
        st.write("**Auftragsart:**", r.get("contract_nature") or "–")
        if r.get("description"):
            with st.expander("Kurzbeschreibung"):
                st.write(r.get("description"))


# ----------------------------- User profile -----------------------------

st.title("📦 Tender Scout Pro")
st.caption(f"Opportunity Intelligence für öffentliche B2B-Waren · v{APP_VERSION}")

with st.sidebar:
    st.header("💶 Geschäftsprofil")
    capital = st.number_input("Verfügbares Startkapital (€)", min_value=100.0, value=2000.0, step=100.0)
    reserve_pct = st.slider("Liquiditätsreserve (%)", 0, 80, 40, 5)
    markup = st.slider("Zielaufschlag auf Einkauf (%)", 5, 100, 25, 5)
    st.metric("Für Ware frei", euro(capital * (1 - reserve_pct / 100)))
    st.divider()
    st.subheader("🎯 Persönlicher Filter")
    include_kw = st.text_area("Bevorzugte Begriffe", value="Kabel, Werkzeug, Verbrauchsmaterial, Ersatzteile", height=80)
    exclude_kw = st.text_area("Ausschließen", value="Bauleistung, Planung, Beratung", height=70)
    st.caption("Kommagetrennt. Diese Begriffe beeinflussen deinen Score.")

# ----------------------------- Tabs -----------------------------

tab_today, tab_analysis, tab_source, tab_watch, tab_import = st.tabs(
    ["🔥 Heute", "🔎 Deal-Analyse", "🌍 Scanner", "⭐ Watchlist", "📥 Import"]
)

with tab_today:
    st.subheader("Daily Opportunity Scan")
    st.write("Scanne aktuelle TED-Bekanntmachungen mit Leistungsort Deutschland und priorisiere sie nach Kapital, Handelswaren-Fit und Hürden.")

    col_a, col_b = st.columns([2, 1])
    with col_a:
        scan_profile = st.selectbox(
            "Scan-Profil",
            ["Alle Lieferaufträge", "IT & Elektro", "Werkzeug & Industriebedarf", "Büro, Möbel & Verbrauch", "Reinigung & Hygiene", "Textilien & Schutzkleidung", "Nur Deutschland, breit"],
        )
    with col_b:
        scan_limit = st.selectbox("Treffer", [50, 100, 150, 250], index=1)
    keyword = st.text_input("Optionaler Suchbegriff", placeholder="z. B. Kabel, Filter, Drucker, Werkzeug …")

    if st.button("🚀 Neue Ausschreibungen scannen", type="primary", use_container_width=True):
        query = build_ted_query(scan_profile, keyword)
        with st.spinner("TED wird durchsucht und Chancen werden bewertet …"):
            try:
                items, mode = ted_search(query, int(scan_limit))
                new_n, upd_n = save_tenders(items)
                st.success(f"Scan fertig: {len(items)} Treffer · {new_n} neu · {upd_n} aktualisiert · API-Modus: {mode}")
                st.caption(f"TED Query: {query}")
            except Exception as e:
                st.error("TED-Live-Scan fehlgeschlagen.")
                st.code(str(e))
                st.info("Tipp: Nutze im Tab „Scanner“ zuerst den Verbindungstest. Die App zeigt dort die genaue TED-Antwort an.")

    out = analyzed_df(capital, markup, reserve_pct, include_kw, exclude_kw)
    if out.empty:
        st.info("Noch keine Daten. Starte oben deinen ersten Scan.")
    else:
        safe = out[~out["blocked"]].copy()
        active = safe[(safe["Tage bis Frist"].isna()) | (safe["Tage bis Frist"] >= 0)]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Gespeichert", len(safe))
        c2.metric("GO-Chancen", int((active["Score"] >= 78).sum()))
        c3.metric("≥ 60 Punkte", int((active["Score"] >= 60).sum()))
        c4.metric("Kapital passt", int((active["Finanzierungslücke €"].fillna(math.inf) <= 0).sum()))

        st.markdown("#### 🏆 Beste Chancen")
        top = active.head(10)
        if top.empty:
            st.warning("Keine aktuell offenen Chancen erkannt.")
        for _, r in top.iterrows():
            days = r.get("Tage bis Frist")
            days_txt = "Frist ?" if pd.isna(days) else f"{int(days)} Tage"
            with st.expander(f"{int(r['Score'])}/100 · {r['title'][:90]} · {days_txt}"):
                render_opportunity(r, compact=True)
                if r.get("url"):
                    st.link_button("📄 Original öffnen", r["url"], use_container_width=True)

with tab_analysis:
    st.subheader("Deal-Analyse")
    out = analyzed_df(capital, markup, reserve_pct, include_kw, exclude_kw)
    if out.empty:
        st.info("Erst einen Scan durchführen.")
    else:
        out = out[~out["blocked"]].copy()
        f1, f2 = st.columns(2)
        min_score = f1.slider("Mindestscore", 0, 100, 55, 5)
        only_financeable = f2.toggle("Nur ohne Finanzierungslücke", value=False)
        search_text = st.text_input("Liste filtern", placeholder="Titel, Auftraggeber, CPV, Begriff …")
        show = out[out["Score"] >= min_score]
        if only_financeable:
            show = show[show["Finanzierungslücke €"].fillna(math.inf) <= 0]
        if search_text:
            n = search_text.lower()
            show = show[show.apply(lambda r: n in f"{r.get('title','')} {r.get('buyer','')} {r.get('description','')} {r.get('cpv','')}".lower(), axis=1)]

        if show.empty:
            st.warning("Keine Ausschreibung mit diesen Filtern.")
        else:
            options = {tender_label(r): idx for idx, r in show.iterrows()}
            selection = st.selectbox("Ausschreibung", list(options.keys()))
            r = show.loc[options[selection]]
            render_opportunity(r)

            st.markdown("#### 💰 Angebots-Kalkulator")
            value = r.get("estimated_value")
            if value is not None and not pd.isna(value):
                st.write(f"Bei {markup}% Zielaufschlag solltest du für Ware + eingerechnete Einkaufskosten **höchstens {euro(r.get('Max. Einkauf für Zielmarge €'))}** ansetzen, bevor weitere Kosten berücksichtigt werden.")
            extra_costs = st.number_input("Geschätzte Nebenkosten (€): Versand, Verpackung, Gebühren …", min_value=0.0, value=0.0, step=25.0)
            supplier_quote = st.number_input("Echtes Lieferantenangebot (€), sobald vorhanden", min_value=0.0, value=0.0, step=50.0)
            if supplier_quote > 0 and value is not None and not pd.isna(value):
                gross = float(value) - supplier_quote - extra_costs
                margin_on_sales = (gross / float(value) * 100) if float(value) else 0
                st.metric("Kalkulierter Deckungsbeitrag", euro(gross), f"{margin_on_sales:.1f}% vom Umsatz")
                if gross <= 0:
                    st.error("Mit diesen Zahlen wäre der Auftrag wirtschaftlich nicht sinnvoll.")
                elif supplier_quote + extra_costs > capital * (1 - reserve_pct / 100):
                    st.warning("Wirtschaftlich möglich, aber aktuell nicht vollständig aus deinem freien Kapital finanzierbar.")
                else:
                    st.success("Die Beispielkalkulation liegt innerhalb deines freien Kapitals. Vertragsbedingungen trotzdem vollständig prüfen.")

            st.markdown("#### 🏭 Beschaffung")
            term = r.get("Beschaffungssuche") or r.get("title")
            st.caption(f"Vorgeschlagener Suchbegriff: {term}")
            for name, url in supplier_links(term):
                st.link_button(name, url, use_container_width=True)
            st.caption("Lieferantenlinks sind Recherchewege, keine Preis- oder Liefergarantie. Für ein Angebot immer Verfügbarkeit, Lieferzeit, Spezifikation und schriftlichen Nettopreis bestätigen lassen.")

            st.markdown("#### ✅ Angebots-Checkliste")
            checklist = [
                "Exakte Positionen, Mengen, Hersteller-/Artikelvorgaben aus Originalunterlagen übernehmen.",
                "Gleichwertigkeit/Alternativprodukte nur anbieten, wenn ausdrücklich zugelassen.",
                "Eignungsnachweise, Referenzen, Zertifikate und Ausschlusskriterien prüfen.",
                "2–3 verbindliche B2B-Lieferantenangebote mit Lieferzeit einholen.",
                "Fracht, Verpackung, Retourenrisiko, Zahlungsziel und Steuern in die Kalkulation aufnehmen.",
                "Abgabefrist und elektronische Signatur/Formvorgaben kontrollieren.",
                "Erst danach verbindlichen Angebotspreis abgeben.",
            ]
            for item in checklist:
                st.checkbox(item, key=f"chk_{r.get('external_id')}_{abs(hash(item))}")

            watched = is_watchlisted(str(r.get("source")), str(r.get("external_id")))
            if st.button("⭐ Von Watchlist entfernen" if watched else "☆ Zur Watchlist", use_container_width=True):
                toggle_watchlist(str(r.get("source")), str(r.get("external_id")))
                st.rerun()
            if r.get("url"):
                st.link_button("📄 Originalausschreibung", r["url"], type="primary", use_container_width=True)

with tab_source:
    st.subheader("Scanner & Verbindungstest")
    st.caption("Hier kannst du TED-Abfragen kontrollieren. Standard für Deutschland ist DEU, nicht DE.")
    preset = st.selectbox("Preset", ["Deutschland – alle Lieferungen", "Deutschland – breit", "Eigene Query"])
    if preset == "Deutschland – alle Lieferungen":
        default_q = "(place-of-performance IN (DEU)) AND (contract-nature = supplies)"
    elif preset == "Deutschland – breit":
        default_q = "place-of-performance IN (DEU)"
    else:
        default_q = "place-of-performance IN (DEU)"
    q = st.text_area("TED Expert Query", value=default_q, height=90)
    test_limit = st.slider("Test-Treffer", 5, 100, 20, 5)
    if st.button("🧪 Verbindung testen", use_container_width=True):
        with st.spinner("TED API testen …"):
            try:
                items, mode = ted_search(q, test_limit)
                st.success(f"Verbindung OK · {len(items)} Treffer · {mode}")
                if items:
                    preview = pd.DataFrame(items)[["external_id", "title", "buyer", "publication_date"]]
                    st.dataframe(preview, use_container_width=True, hide_index=True)
            except Exception as e:
                st.error("TED hat die Abfrage abgelehnt:")
                st.code(str(e))
    st.info("Die TED Search API ist für veröffentlichte Bekanntmachungen offen zugänglich. Expert Queries müssen aber exakt der TED-Syntax und den Code-Listen entsprechen.")

with tab_watch:
    st.subheader("⭐ Watchlist")
    con = db()
    watch = pd.read_sql_query(
        """
        SELECT t.* FROM tenders t
        JOIN watchlist w ON w.source=t.source AND w.external_id=t.external_id
        ORDER BY w.created_at DESC
        """,
        con,
    )
    con.close()
    if watch.empty:
        st.info("Noch keine Ausschreibungen gespeichert. Füge interessante Deals im Tab „Deal-Analyse“ hinzu.")
    else:
        rows = []
        for _, rr in watch.iterrows():
            b = rr.to_dict(); b.update(analyze(b, capital, markup, reserve_pct, include_kw, exclude_kw)); rows.append(b)
        wdf = pd.DataFrame(rows).sort_values("Score", ascending=False)
        for _, r in wdf.iterrows():
            with st.expander(tender_label(r)):
                render_opportunity(r, compact=True)
                if r.get("url"):
                    st.link_button("Original öffnen", r["url"], use_container_width=True)

with tab_import:
    st.subheader("Andere Vergabeportale importieren")
    st.write("CSV/JSON-Exporte anderer Portale kannst du hier in denselben Deal-Scanner laden.")
    source_name = st.text_input("Quellenname", value="Manueller Import")
    uploaded = st.file_uploader("CSV oder JSON", type=["csv", "json"])
    if uploaded is not None:
        try:
            if uploaded.name.lower().endswith(".csv"):
                imp = pd.read_csv(uploaded)
            else:
                raw = json.load(uploaded)
                if isinstance(raw, dict):
                    raw = raw.get("items") or raw.get("results") or raw.get("notices") or [raw]
                imp = pd.DataFrame(raw)
            st.dataframe(imp.head(15), use_container_width=True)
            if st.button("Importieren & analysieren", use_container_width=True):
                n, u = save_tenders(normalize_import(imp, source_name))
                st.success(f"{n} neu · {u} aktualisiert")
        except Exception as e:
            st.error(f"Import nicht möglich: {e}")

    st.divider()
    out = analyzed_df(capital, markup, reserve_pct, include_kw, exclude_kw)
    if not out.empty:
        export_cols = [
            "source", "external_id", "title", "buyer", "publication_date", "deadline", "estimated_value",
            "cpv", "Score", "Bewertung", "Geschätzter Einkauf €", "Geschätzter Rohertrag €",
            "Finanzierungslücke €", "Pluspunkte", "Risiken", "url"
        ]
        csv = out[[c for c in export_cols if c in out.columns]].to_csv(index=False).encode("utf-8-sig")
        st.download_button("⬇️ Analyse als CSV sichern", data=csv, file_name=f"tender_scout_{date.today().isoformat()}.csv", mime="text/csv", use_container_width=True)

st.divider()
st.caption(
    "Tender Scout Pro · Voranalyse für frei handelbare B2B-Waren. Scores und Modellmargen ersetzen keine Prüfung der Vergabeunterlagen, Lieferfähigkeit, Steuern, Finanzierung oder rechtlichen Anforderungen. "
    "Lokale Watchlist/History kann bei Streamlit-Neustarts verloren gehen; für dauerhaften Produktivbetrieb sollte später eine Cloud-Datenbank angebunden werden."
)
