import io
import json
import math
import re
import sqlite3
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

import pandas as pd
import requests
import streamlit as st

APP_VERSION = "PRO 1.0"
APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "tender_scout.db"
TED_SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"

st.set_page_config(page_title="Tender Scout Pro", page_icon="📦", layout="wide", initial_sidebar_state="collapsed")

st.markdown(r"""
<style>
:root { --radius:18px; }
.block-container {max-width:1180px; padding-top:.7rem; padding-bottom:5rem;}
header[data-testid="stHeader"] {background:rgba(0,0,0,0);}
[data-testid="stMetric"] {border:1px solid rgba(128,128,128,.23); border-radius:16px; padding:12px 14px;}
[data-testid="stMetricValue"] {font-size:1.45rem;}
div.stButton > button, div.stLinkButton > a {min-height:3rem; border-radius:14px; font-weight:750;}
div[data-testid="stExpander"] {border-radius:16px; border:1px solid rgba(128,128,128,.22);}
.tsp-hero {border:1px solid rgba(128,128,128,.24); border-radius:22px; padding:20px; margin-bottom:14px; background:linear-gradient(135deg,rgba(212,170,65,.12),rgba(40,80,160,.08));}
.tsp-card {border:1px solid rgba(128,128,128,.24); border-radius:18px; padding:16px; margin:10px 0;}
.tsp-muted {opacity:.7; font-size:.9rem;}
.tsp-badge {display:inline-block; padding:4px 9px; border-radius:999px; border:1px solid rgba(128,128,128,.25); margin:2px 3px 2px 0; font-size:.80rem;}
.tsp-good {border-left:5px solid #22a06b;}
.tsp-warn {border-left:5px solid #d6a400;}
.tsp-bad {border-left:5px solid #d64045;}
.small {font-size:.86rem; opacity:.75;}
@media (max-width:700px){.block-container{padding-left:.65rem;padding-right:.65rem} h1{font-size:1.7rem!important} h2{font-size:1.25rem!important} .tsp-hero{padding:14px}}
</style>
""", unsafe_allow_html=True)

# ---------------- DB ----------------
def con():
    c=sqlite3.connect(DB_PATH); c.row_factory=sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS tenders(
      source TEXT, external_id TEXT, title TEXT, buyer TEXT, publication_date TEXT, deadline TEXT,
      estimated_value REAL, currency TEXT, description TEXT, url TEXT, cpv TEXT, raw_json TEXT,
      first_seen TEXT DEFAULT CURRENT_TIMESTAMP, last_seen TEXT DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY(source,external_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS watchlist(
      source TEXT, external_id TEXT, note TEXT DEFAULT '', status TEXT DEFAULT 'Prüfen', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY(source,external_id))""")
    c.commit(); return c

# ---------------- generic helpers ----------------
def txt(v):
    if v is None: return ""
    if isinstance(v,str): return v.strip()
    if isinstance(v,(int,float)): return str(v)
    if isinstance(v,list): return " | ".join(x for x in [txt(i) for i in v] if x)
    if isinstance(v,dict):
        for k in ("deu","de","ger","eng","en"):
            if k in v and txt(v[k]): return txt(v[k])
        return " | ".join(x for x in [txt(i) for i in v.values()] if x)
    return str(v)

def first(d, keys, default=""):
    for k in keys:
        if isinstance(d,dict) and k in d and d[k] not in (None,"",[],{}): return d[k]
    return default

def number(v):
    if v is None or v=="": return None
    if isinstance(v,(int,float)): return float(v)
    if isinstance(v,list):
        z=[number(x) for x in v]; z=[x for x in z if x is not None]; return max(z) if z else None
    if isinstance(v,dict):
        for k in ("value","amount","val"):
            if k in v: return number(v[k])
        z=[number(x) for x in v.values()]; z=[x for x in z if x is not None]; return max(z) if z else None
    s=re.sub(r"[^0-9,.-]","",txt(v))
    if not s: return None
    if "," in s and "." in s:
        s=s.replace(".","").replace(",",".") if s.rfind(",")>s.rfind(".") else s.replace(",","")
    elif "," in s: s=s.replace(".","").replace(",",".")
    try:return float(s)
    except:return None

def date_parse(v):
    s=txt(v)
    if not s:return None
    for pattern, order in [(r"(20\d\d)[-/.](\d\d?)[-/.](\d\d?)","ymd"),(r"(\d\d?)[.](\d\d?)[.](20\d\d)","dmy")]:
        m=re.search(pattern,s)
        if m:
            try:
                return date(int(m.group(1)),int(m.group(2)),int(m.group(3))) if order=="ymd" else date(int(m.group(3)),int(m.group(2)),int(m.group(1)))
            except: pass
    return None

def eur(v):
    if v is None:return "–"
    return f"{v:,.0f} €".replace(",",".")

def clean_space(s): return re.sub(r"\s+"," ",txt(s)).strip()

def find_url(obj):
    s=txt(obj); m=re.search(r"https?://[^\s|\]\[\"']+",s)
    return m.group(0).rstrip(",.;") if m else ""

# ---------------- TED ----------------
TED_FIELDS=[
 "publication-number","notice-title","buyer-name","publication-date",
 "deadline-receipt-tender-date-lot","estimated-value-proc","estimated-value-lot",
 "description-proc","description-lot","main-classification-proc","main-classification-lot",
 "contract-nature-proc","notice-type"
]
TED_MIN=["publication-number","notice-title","buyer-name","publication-date"]

def ted_call(query, limit=100, fields=None):
    payload={"query":query,"fields":fields or TED_FIELDS,"page":1,"limit":min(limit,250),"scope":"ACTIVE","checkQuerySyntax":True,"paginationMode":"PAGE_NUMBER"}
    r=requests.post(TED_SEARCH_URL,json=payload,timeout=30)
    if r.status_code>=400:
        # Retry without optional scope/check fields because API deployments can vary.
        payload={"query":query,"fields":fields or TED_MIN,"page":1,"limit":min(limit,250)}
        r=requests.post(TED_SEARCH_URL,json=payload,timeout=30)
    r.raise_for_status(); return r.json()

def results_from_response(data):
    if isinstance(data,list):return data
    if not isinstance(data,dict):return []
    for k in ("notices","results","items"):
        if isinstance(data.get(k),list):return data[k]
    return []

def normalize_ted(item):
    eid=txt(first(item,["publication-number","notice-id","id"]))
    if not eid:eid=str(abs(hash(json.dumps(item,sort_keys=True,default=str))))
    title=txt(first(item,["notice-title","procedure-title","title"])) or "Unbenannte Ausschreibung"
    buyer=txt(first(item,["buyer-name","organisation-name-buyer","buyer"]))
    pub=txt(first(item,["publication-date","date-publication"]))
    ddl=txt(first(item,["deadline-receipt-tender-date-lot","deadline"]))
    val=number(first(item,["estimated-value-lot","estimated-value-proc","value"]))
    desc=txt(first(item,["description-lot","description-proc","description","short-description"]))
    cpv=txt(first(item,["main-classification-lot","main-classification-proc","cpv"]))
    url=find_url(first(item,["links","urls","url"],"")) or f"https://ted.europa.eu/de/notice/-/detail/{eid}"
    return dict(source="TED",external_id=eid,title=title,buyer=buyer,publication_date=pub,deadline=ddl,estimated_value=val,currency="EUR",description=desc,url=url,cpv=cpv,raw_json=json.dumps(item,ensure_ascii=False,default=str))

def save_tenders(rows):
    c=con(); new=0
    for r in rows:
        existed=c.execute("SELECT 1 FROM tenders WHERE source=? AND external_id=?",(r["source"],r["external_id"])).fetchone()
        c.execute("""INSERT INTO tenders(source,external_id,title,buyer,publication_date,deadline,estimated_value,currency,description,url,cpv,raw_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source,external_id) DO UPDATE SET
        title=excluded.title,buyer=excluded.buyer,publication_date=excluded.publication_date,deadline=excluded.deadline,
        estimated_value=excluded.estimated_value,currency=excluded.currency,description=excluded.description,url=excluded.url,cpv=excluded.cpv,
        raw_json=excluded.raw_json,last_seen=CURRENT_TIMESTAMP""",tuple(r[k] for k in ["source","external_id","title","buyer","publication_date","deadline","estimated_value","currency","description","url","cpv","raw_json"]))
        if not existed:new+=1
    c.commit();return new

def scan_ted(limit=120, days=45):
    # Broad Germany query; hard deadline filtering happens locally to avoid relying on one TED field layout.
    queries=[
      "place-of-performance IN (DEU)",
      "buyer-country IN (DEU)",
      "CY = DEU"
    ]
    errors=[]
    for q in queries:
        try:
            data=ted_call(q,limit,TED_FIELDS); items=results_from_response(data)
            if items:return [normalize_ted(x) for x in items],q,None
        except Exception as e: errors.append(str(e))
    return [],queries[0]," | ".join(errors[-2:])

# ---------------- Detail extraction ----------------
QTY_PATTERNS=[
 r"(?P<qty>\d{1,6})\s*(?:Stück|Stk\.?|pcs\.?|pieces)\s+(?P<product>.{4,100})",
 r"(?P<product>.{4,100}?)\s*[-–:]?\s*(?P<qty>\d{1,6})\s*(?:Stück|Stk\.?|pcs\.?)",
 r"Menge\s*[:=]\s*(?P<qty>\d{1,6})\s*[,;\-–]?\s*(?P<product>.{4,100})"
]
MODEL_PATTERNS=[r"(?:Art\.?[- ]?Nr\.?|Artikelnummer|Bestellnummer|Modell|Typ)\s*[:#]?\s*([A-Z0-9][A-Z0-9._/-]{2,30})"]

def extract_products(text):
    s=clean_space(text)
    out=[]
    for pat in QTY_PATTERNS:
        for m in re.finditer(pat,s,re.I):
            prod=m.group("product").strip(" .,:;-")[:120]
            try:q=int(m.group("qty"))
            except:continue
            if 0<q<1000000 and len(prod)>3: out.append({"produkt":prod,"menge":q})
    # dedupe
    seen=set(); final=[]
    for x in out:
        k=(x["produkt"].lower(),x["menge"])
        if k not in seen: seen.add(k); final.append(x)
    return final[:20]

def extract_models(text):
    r=[]
    for pat in MODEL_PATTERNS:
        r += re.findall(pat,text or "",re.I)
    return list(dict.fromkeys(r))[:20]

def detect_requirements(text):
    s=(text or "").lower()
    tests={
      "Referenzen":["referenz","vergleichbare aufträge"],
      "Bürgschaft":["bürgschaft","sicherheitseinbehalt"],
      "ISO/QM":["iso 9001","qualitätsmanagement"],
      "Sicherheitsprüfung":["sicherheitsüberprüfung","geheimschutz"],
      "Muster/Probe":["muster","probeexemplar"],
      "Nachhaltigkeit":["umweltzeichen","nachhaltigkeit","blauer engel"],
      "Rahmenvertrag":["rahmenvereinbarung","rahmenvertrag"]
    }
    return [name for name,keys in tests.items() if any(k in s for k in keys)]

def deadline_state(v):
    d=date_parse(v)
    if not d:return ("unknown",None)
    days=(d-date.today()).days
    if days<0:return ("expired",days)
    return ("active",days)

def analyze(row, capital, reserve, target_margin, max_ticket):
    value=row.get("estimated_value")
    state,days=deadline_state(row.get("deadline"))
    text=f"{row.get('title','')} {row.get('description','')}"
    products=extract_products(text); models=extract_models(text); req=detect_requirements(text)
    usable=max(0,capital-reserve)
    max_buy=value*(1-target_margin/100) if value else None
    funding=max(0,max_buy-usable) if max_buy is not None else None
    theoretical_gp=value-max_buy if value and max_buy is not None else None
    score=50
    if state=="expired":score=0
    elif state=="unknown":score-=12
    elif days is not None:
        if days>=14:score+=10
        elif days<5:score-=15
    if value:
        if value<=max_ticket:score+=10
        if max_buy and max_buy<=usable:score+=25
        elif max_buy and max_buy<=usable*2:score+=8
        else:score-=12
    else:score-=5
    if products:score+=8
    if "Rahmenvertrag" in req:score-=4
    if "Sicherheitsprüfung" in req:score-=20
    if "Bürgschaft" in req:score-=12
    score=max(0,min(100,score))
    label="GO" if score>=75 else "PRÜFEN" if score>=50 else "NO-GO"
    return dict(score=score,label=label,state=state,days=days,products=products,models=models,requirements=req,max_buy=max_buy,funding=funding,gp=theoretical_gp,usable=usable)

def sourcing_links(query):
    q=quote_plus(query)
    return [
      ("Google Shopping / Händler",f"https://www.google.com/search?q={q}+kaufen+Gro%C3%9Fhandel"),
      ("Hersteller finden",f"https://www.google.com/search?q={q}+Hersteller+Deutschland"),
      ("Unite / Mercateo",f"https://www.google.com/search?q=site%3Aunite.eu+{q}"),
      ("RS",f"https://de.rs-online.com/web/c/?searchTerm={q}"),
      ("Conrad",f"https://www.conrad.de/de/search.html?search={q}"),
      ("Farnell",f"https://de.farnell.com/search?st={q}"),
      ("Würth",f"https://www.google.com/search?q=site%3Awuerth.de+{q}")
    ]

# ---------------- import/export ----------------
def all_rows(): return [dict(r) for r in con().execute("SELECT * FROM tenders ORDER BY first_seen DESC").fetchall()]
def watch_ids(): return {(r[0],r[1]) for r in con().execute("SELECT source,external_id FROM watchlist").fetchall()}
def watch_toggle(row):
    c=con(); key=(row["source"],row["external_id"])
    ex=c.execute("SELECT 1 FROM watchlist WHERE source=? AND external_id=?",key).fetchone()
    if ex:c.execute("DELETE FROM watchlist WHERE source=? AND external_id=?",key)
    else:c.execute("INSERT INTO watchlist(source,external_id) VALUES(?,?)",key)
    c.commit()

def backup_bytes():
    c=con(); tenders=pd.read_sql_query("SELECT * FROM tenders",c); watch=pd.read_sql_query("SELECT * FROM watchlist",c)
    bio=io.BytesIO()
    with zipfile.ZipFile(bio,"w",zipfile.ZIP_DEFLATED) as z:
        z.writestr("tenders.csv",tenders.to_csv(index=False)); z.writestr("watchlist.csv",watch.to_csv(index=False))
    return bio.getvalue()

# ---------------- session/settings ----------------
def init():
    defaults={"page":"Dashboard","capital":2000.0,"reserve":500.0,"margin":20.0,"max_ticket":10000.0,"selected":None,"only_active":True,"min_days":3}
    for k,v in defaults.items(): st.session_state.setdefault(k,v)
init()

with st.sidebar:
    st.markdown("## ⚙️ Dein Profil")
    st.session_state.capital=st.number_input("Kapital",0.0,1000000.0,float(st.session_state.capital),100.0)
    st.session_state.reserve=st.number_input("Reserve",0.0,1000000.0,float(st.session_state.reserve),100.0)
    st.session_state.margin=st.slider("Ziel-Rohertrag %",5,50,int(st.session_state.margin))
    st.session_state.max_ticket=st.number_input("Max. gewünschtes Auftragsvolumen",500.0,10000000.0,float(st.session_state.max_ticket),500.0)
    st.session_state.only_active=st.toggle("Nur aktive Fristen",value=st.session_state.only_active)
    st.session_state.min_days=st.slider("Mindestens Tage bis Frist",0,60,int(st.session_state.min_days))
    st.caption("Ausschreibungswerte sind keine Umsatzgarantie. Preise und Mengen müssen in den Originalunterlagen bestätigt werden.")

st.markdown(f"""<div class='tsp-hero'><div class='small'>TENDER SCOUT</div><h1 style='margin:.1rem 0'>📦 Tender Scout Pro</h1><div class='tsp-muted'>Ausschreibungen → Deal-Akte → Beschaffung → Kalkulation</div></div>""",unsafe_allow_html=True)

menu=st.radio("Navigation",["Dashboard","Scanner","Deals","Watchlist","Kalkulator","Einstellungen"],horizontal=True,label_visibility="collapsed",key="page")
capital=st.session_state.capital; reserve=st.session_state.reserve; margin=st.session_state.margin; max_ticket=st.session_state.max_ticket

rows=all_rows(); wids=watch_ids()

def filtered_rows(rows):
    out=[]
    for r in rows:
        a=analyze(r,capital,reserve,margin,max_ticket)
        if st.session_state.only_active:
            if a["state"]=="expired":continue
            if a["state"]=="active" and a["days"] is not None and a["days"]<st.session_state.min_days:continue
        r=dict(r); r["_a"]=a; out.append(r)
    return sorted(out,key=lambda x:(x["_a"]["score"],x["publication_date"] or ""),reverse=True)

frows=filtered_rows(rows)

if menu=="Dashboard":
    st.subheader("Heute")
    c1,c2,c3,c4=st.columns(4)
    c1.metric("Aktive Chancen",len(frows))
    c2.metric("GO",sum(1 for r in frows if r["_a"]["label"]=="GO"))
    c3.metric("Watchlist",len(wids))
    c4.metric("Nutzbares Kapital",eur(max(0,capital-reserve)))
    st.markdown("### Beste Chancen")
    if not frows: st.info("Noch keine Daten. Öffne **Scanner** und starte einen Live-Scan.")
    for r in frows[:8]:
        a=r["_a"]; cls="tsp-good" if a["label"]=="GO" else "tsp-warn" if a["label"]=="PRÜFEN" else "tsp-bad"
        st.markdown(f"<div class='tsp-card {cls}'><b>{a['label']} · {a['score']}/100</b><br><b>{r['title']}</b><br><span class='tsp-muted'>{r['buyer'] or 'Auftraggeber nicht erkannt'} · {eur(r['estimated_value'])} · {'Frist unbekannt' if a['days'] is None else str(a['days'])+' Tage'}</span></div>",unsafe_allow_html=True)
        if st.button("Deal öffnen",key="open"+r["external_id"]): st.session_state.selected=(r["source"],r["external_id"]); st.session_state.page="Deals"; st.rerun()

elif menu=="Scanner":
    st.subheader("🔎 Live-Scanner")
    st.caption("TED-EU-Ausschreibungen, Deutschland. Abgelaufene Fristen werden nach dem Abruf lokal verworfen.")
    col1,col2=st.columns([1,1])
    limit=col1.selectbox("Abrufmenge",[50,100,150,250],index=1)
    if col2.button("🚀 Jetzt scannen",use_container_width=True,type="primary"):
        with st.spinner("TED wird durchsucht …"):
            data,q,err=scan_ted(limit)
            if err: st.error("TED-Scan fehlgeschlagen: "+err)
            else:
                # hard expiry filter before save only if deadline is recognized; unknown remains auditable.
                new=save_tenders(data)
                st.success(f"{len(data)} Bekanntmachungen geladen · {new} neu")
                st.caption("Verwendete TED-Abfrage: "+q)
                st.rerun()
    st.markdown("### Suchfilter")
    search=st.text_input("Produkt / Stichwort / Auftraggeber",placeholder="z. B. Werkzeug, Kabel, Drucker, Ersatzteile")
    view=frows
    if search:
        ss=search.lower(); view=[r for r in view if ss in (r['title']+' '+r['description']+' '+r['buyer']+' '+r['cpv']).lower()]
    st.write(f"**{len(view)} passende Ausschreibungen**")
    for r in view[:50]:
        a=r["_a"]
        st.markdown(f"<div class='tsp-card'><b>{a['label']} · {a['score']}/100 — {r['title']}</b><br><span class='tsp-muted'>{r['buyer'] or '–'} · Wert {eur(r['estimated_value'])} · {'Frist unbekannt' if a['days'] is None else str(a['days'])+' Tage bis Frist'}</span></div>",unsafe_allow_html=True)
        c1,c2=st.columns(2)
        if c1.button("📂 Deal-Akte",key="d"+r["external_id"]): st.session_state.selected=(r["source"],r["external_id"]); st.session_state.page="Deals"; st.rerun()
        if c2.button("⭐ Merken" if (r['source'],r['external_id']) not in wids else "★ Entfernen",key="w"+r["external_id"]): watch_toggle(r); st.rerun()

elif menu=="Deals":
    st.subheader("📂 Deal-Akte")
    if not rows: st.info("Noch keine Ausschreibungen gescannt.")
    else:
        choices={f"{r['title'][:75]} · {r['external_id']}":(r['source'],r['external_id']) for r in frows or rows}
        labels=list(choices)
        default=0
        if st.session_state.selected:
            for i,l in enumerate(labels):
                if choices[l]==st.session_state.selected: default=i; break
        label=st.selectbox("Ausschreibung",labels,index=default)
        key=choices[label]; r=next(x for x in rows if (x['source'],x['external_id'])==key); a=analyze(r,capital,reserve,margin,max_ticket)
        state_text="AKTIV" if a['state']=="active" else "FRIST UNBEKANNT" if a['state']=="unknown" else "ABGELAUFEN"
        st.markdown(f"### {r['title']}")
        st.caption(f"{r['buyer'] or 'Auftraggeber nicht erkannt'} · {r['external_id']} · {state_text}")
        m1,m2,m3,m4=st.columns(4)
        m1.metric("Deal-Score",f"{a['score']}/100",a['label'])
        m2.metric("Ausschreibungswert",eur(r['estimated_value']))
        m3.metric("Max. Einkauf*",eur(a['max_buy']))
        m4.metric("Finanzierungslücke*",eur(a['funding']))
        st.caption("*Rechenhilfe auf Basis des geschätzten Werts und deiner Zielmarge; kein garantierter Umsatz oder tatsächlicher Einkaufspreis.")
        tab1,tab2,tab3,tab4=st.tabs(["📦 Produkt & Menge","💶 Umsatz & Kapital","🏭 Beschaffung","✅ Anforderungen"])
        with tab1:
            if a['products']:
                st.success("Mengen-/Produktangaben im verfügbaren Text erkannt")
                st.dataframe(pd.DataFrame(a['products']),use_container_width=True,hide_index=True)
            else: st.warning("Keine belastbare Positionsmenge im TED-Kurztext erkannt. Originalunterlagen prüfen.")
            if a['models']: st.write("**Erkannte Artikel-/Modellnummern:** "+", ".join(a['models']))
            st.write("**CPV:**",r['cpv'] or "nicht erkannt")
            st.text_area("TED-Beschreibung",r['description'] or "Keine Beschreibung geliefert",height=180,disabled=True)
            if r['url']: st.link_button("🔗 Original auf TED öffnen",r['url'])
        with tab2:
            st.info("Der TED-Auftragswert ist ein Plan-/Schätzwert. Besonders bei Rahmenvereinbarungen ist er nicht automatisch dein Umsatz.")
            if r['estimated_value']:
                st.write(f"**Theoretisches Umsatzvolumen:** bis {eur(r['estimated_value'])} — nur falls dieser Wert tatsächlich deinem Los/Zuschlag entspricht.")
                st.write(f"**Maximaler Einkauf bei {margin:.0f}% Ziel-Rohertrag:** {eur(a['max_buy'])}")
                st.write(f"**Theoretischer Rohertrag:** {eur(a['gp'])}")
                st.write(f"**Nutzbares Eigenkapital nach Reserve:** {eur(a['usable'])}")
                if a['funding'] and a['funding']>0: st.error(f"Finanzierungslücke: {eur(a['funding'])}")
                else: st.success("Nach dieser Grobkalkulation innerhalb deines Kapitals.")
            else: st.warning("Kein belastbarer Auftragswert in den abgerufenen TED-Feldern.")
        with tab3:
            query=(a['models'][0] if a['models'] else (a['products'][0]['produkt'] if a['products'] else r['title']))[:120]
            st.write("**Beschaffungssuche für:**",query)
            for name,url in sourcing_links(query): st.link_button(name,url,use_container_width=True)
            st.markdown("#### Echtes Lieferantenangebot kalkulieren")
            unit=st.number_input("Einkaufspreis pro Einheit (€)",min_value=0.0,value=0.0,step=0.1,key="unit"+r['external_id'])
            qty=st.number_input("Menge",min_value=1,value=int(a['products'][0]['menge']) if a['products'] else 1,step=1,key="qty"+r['external_id'])
            freight=st.number_input("Fracht/Nebenkosten (€)",min_value=0.0,value=0.0,step=10.0,key="fr"+r['external_id'])
            buy=unit*qty+freight
            target_sell=buy/(1-margin/100) if margin<100 else 0
            c1,c2,c3=st.columns(3); c1.metric("Einkauf gesamt",eur(buy)); c2.metric("Mindestverkauf bei Zielmarge",eur(target_sell)); c3.metric("Kapitaldifferenz",eur(max(0,buy-a['usable'])))
        with tab4:
            if a['requirements']:
                for x in a['requirements']: st.markdown(f"<span class='tsp-badge'>{x}</span>",unsafe_allow_html=True)
            else: st.info("Im Kurztext keine typischen Zusatzanforderungen erkannt. Vergabeunterlagen bleiben maßgeblich.")
            checklist=["Leistungsbeschreibung vollständig gelesen","Mengen/Los eindeutig","Gleichwertigkeit / Marke geklärt","Lieferzeit bestätigt","Lieferantenangebot schriftlich","Fracht & Verpackung kalkuliert","Zahlungsziel / Vorfinanzierung geklärt","Eignungsnachweise vorhanden","Angebotsfrist geprüft"]
            for i,x in enumerate(checklist): st.checkbox(x,key=f"ck-{r['external_id']}-{i}")
        if st.button("⭐ Watchlist umschalten",use_container_width=True): watch_toggle(r); st.rerun()

elif menu=="Watchlist":
    st.subheader("⭐ Watchlist")
    watchrows=[r for r in rows if (r['source'],r['external_id']) in wids]
    if not watchrows:st.info("Noch keine Deals gespeichert.")
    for r in watchrows:
        a=analyze(r,capital,reserve,margin,max_ticket)
        st.markdown(f"<div class='tsp-card'><b>{r['title']}</b><br><span class='tsp-muted'>{a['label']} · {a['score']}/100 · {eur(r['estimated_value'])}</span></div>",unsafe_allow_html=True)
        if st.button("Öffnen",key="wo"+r['external_id']):st.session_state.selected=(r['source'],r['external_id']);st.session_state.page="Deals";st.rerun()

elif menu=="Kalkulator":
    st.subheader("🧮 Angebotskalkulator")
    qty=st.number_input("Menge",1,1000000,100)
    unit=st.number_input("EK je Einheit (€)",0.0,10000000.0,10.0,0.1)
    freight=st.number_input("Fracht / Verpackung (€)",0.0,10000000.0,100.0,10.0)
    extra=st.number_input("Sonstige Kosten (€)",0.0,10000000.0,0.0,10.0)
    risk=st.slider("Risikopuffer %",0,30,5)
    cost=qty*unit+freight+extra; cost_risk=cost*(1+risk/100); sale=cost_risk/(1-margin/100)
    c1,c2,c3=st.columns(3); c1.metric("Gesamtkosten inkl. Puffer",eur(cost_risk)); c2.metric("Ziel-Angebot netto",eur(sale)); c3.metric("Rohertrag",eur(sale-cost_risk))
    if cost_risk>max(0,capital-reserve): st.error(f"Vorfinanzierungslücke ca. {eur(cost_risk-max(0,capital-reserve))}")
    else: st.success("Innerhalb deines eingestellten verfügbaren Kapitals.")

elif menu=="Einstellungen":
    st.subheader("⚙️ Einstellungen & Datensicherung")
    st.write(f"**Version:** {APP_VERSION}")
    st.write("Deine Kapital-/Marge-Einstellungen findest du im Seitenmenü oben links.")
    st.download_button("⬇️ Daten-Backup herunterladen",backup_bytes(),file_name="tender-scout-backup.zip",mime="application/zip",use_container_width=True)
    st.warning("Streamlit Community Cloud hat keinen garantierten dauerhaften lokalen Speicher. Für langfristige Nutzung sollte später eine Cloud-Datenbank angebunden werden. Bis dahin regelmäßig Backup herunterladen.")
    st.markdown("### Datenqualität")
    st.write("Tender Scout unterscheidet bewusst zwischen **erkannt**, **geschätzt** und **nicht bekannt**. Fehlende Positionsdaten werden nicht erfunden.")

st.divider(); st.caption("Tender Scout Pro · Entscheidungsunterstützung für normale, frei handelbare B2B-Waren. Original-Vergabeunterlagen sind immer maßgeblich.")
