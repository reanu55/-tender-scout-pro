import streamlit as st
import requests, re, json, math, sqlite3, urllib.parse
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd

APP_VERSION = "2026.09 Production"
TED_URL = "https://api.ted.europa.eu/v3/notices/search"
DB_PATH = Path("tender_scout.db")

st.set_page_config(page_title="Tender Scout Pro", page_icon="📦", layout="wide", initial_sidebar_state="collapsed")

# ---------- visual system ----------
st.markdown(r"""
<style>
:root{--bg:#07111f;--card:#0e1b2d;--card2:#101f34;--line:#24364e;--muted:#91a1b8;--gold:#f3c75f;--good:#35d07f;--warn:#ffb84d;--bad:#ff6577;--white:#f7f9fc;}
.stApp{background:linear-gradient(180deg,#07111f 0%,#091521 55%,#08111b 100%);color:var(--white)}
.block-container{padding-top:.8rem;padding-bottom:5rem;max-width:1180px}
#MainMenu,footer,header{visibility:hidden}.stDeployButton{display:none}
h1,h2,h3{letter-spacing:-.02em}.muted{color:var(--muted)}
.brand{display:flex;align-items:center;gap:12px;margin:2px 0 14px}.brandlogo{width:46px;height:46px;border-radius:14px;background:linear-gradient(145deg,#18324f,#0a1727);display:flex;align-items:center;justify-content:center;border:1px solid #294663;font-size:25px;box-shadow:0 12px 30px #0005}.brandtitle{font-weight:800;font-size:1.35rem}.brandsub{color:#8da0b8;font-size:.8rem}
.hero{background:linear-gradient(135deg,#10243c 0%,#0a1728 60%,#152339 100%);border:1px solid #29415e;border-radius:22px;padding:22px;margin-bottom:16px;box-shadow:0 16px 38px #0004}.hero h1{margin:0;font-size:2rem}.hero p{color:#9aabc0;margin:.45rem 0 0}
.kpi{background:#0d1b2c;border:1px solid #223951;border-radius:17px;padding:15px 15px 13px;min-height:100px}.kpi-label{font-size:.78rem;color:#8ea1b8}.kpi-value{font-size:1.55rem;font-weight:800;margin-top:5px}.kpi-sub{font-size:.76rem;color:#6f839c;margin-top:3px}
.card{background:linear-gradient(160deg,#0d1b2c,#0a1625);border:1px solid #20364d;border-radius:18px;padding:16px;margin:10px 0}.deal{border-left:4px solid #4f7faf}.deal.good{border-left-color:#35d07f}.deal.warn{border-left-color:#ffb84d}.deal.bad{border-left-color:#ff6577}
.badge{display:inline-block;padding:4px 9px;border-radius:999px;font-size:.72rem;font-weight:700;border:1px solid #304760;background:#11243a;margin:2px 5px 2px 0}.badge.good{color:#5fe49a;border-color:#276a49;background:#0e2b20}.badge.warn{color:#ffc76d;border-color:#72502a;background:#2e2414}.badge.bad{color:#ff8190;border-color:#71343f;background:#30171d}.badge.gold{color:#f5ce71;border-color:#66542b;background:#2c2617}
.big-score{width:72px;height:72px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:1.45rem;font-weight:900;border:5px solid #34536e;background:#091727}
.label{font-size:.76rem;color:#8fa2b9;text-transform:uppercase;letter-spacing:.06em}.value{font-weight:700;margin-top:2px}.divider{height:1px;background:#1e3349;margin:12px 0}
.nav-note{font-size:.74rem;color:#72859e;text-align:center;margin-top:18px}
.stButton>button{border-radius:13px!important;min-height:44px;font-weight:700}.stTextInput input,.stNumberInput input,.stSelectbox div[data-baseweb="select"]>div{border-radius:12px!important}
[data-testid="stExpander"]{background:#0b1929;border:1px solid #21364c;border-radius:14px}
[data-testid="stMetric"]{background:#0d1b2c;border:1px solid #223951;border-radius:15px;padding:10px}
.stTabs [data-baseweb="tab-list"]{gap:4px;background:#091625;padding:5px;border-radius:14px}.stTabs [data-baseweb="tab"]{border-radius:10px;height:42px}
@media(max-width:700px){.block-container{padding-left:.75rem;padding-right:.75rem}.hero{padding:17px}.hero h1{font-size:1.55rem}.brandtitle{font-size:1.15rem}.kpi{min-height:88px}.kpi-value{font-size:1.3rem}}
</style>
""", unsafe_allow_html=True)

# ---------- helpers ----------
def init_state():
    defaults = {
        "nav":"Dashboard", "capital":2000.0, "reserve":500.0, "target_margin":22.0,
        "scan_limit":100, "notices":[], "scan_error":None, "last_scan":None,
        "hide_unknown_deadline":False, "min_days":1, "keyword":"", "selected":None,
        "only_supplies":True, "min_score":0
    }
    for k,v in defaults.items():
        st.session_state.setdefault(k,v)
init_state()

def db():
    con=sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS watchlist(pub TEXT PRIMARY KEY, title TEXT, buyer TEXT, note TEXT, added TEXT, payload TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS quotes(id INTEGER PRIMARY KEY AUTOINCREMENT, pub TEXT, supplier TEXT, buy_price REAL, shipping REAL, sell_price REAL, note TEXT, created TEXT)")
    return con

def first_scalar(v, default="—"):
    if v is None: return default
    if isinstance(v, dict):
        for lang in ("deu","ger","eng"):
            x=v.get(lang)
            if x:
                return first_scalar(x, default)
        for x in v.values():
            if x: return first_scalar(x, default)
        return default
    if isinstance(v, list):
        if not v:return default
        return first_scalar(v[0], default)
    return str(v)

def all_text(v)->str:
    if v is None:return ""
    if isinstance(v, dict):return " ".join(all_text(x) for x in v.values())
    if isinstance(v, list):return " ".join(all_text(x) for x in v)
    return str(v)

def money(v, currency="EUR"):
    try:
        x=float(str(v).replace(" ","").replace(",","."))
        return f"{x:,.0f} {currency}".replace(",",".")
    except:return "nicht angegeben"

def numeric_values(v)->List[float]:
    out=[]
    if v is None:return out
    if isinstance(v,list):
        for x in v: out+=numeric_values(x)
    elif isinstance(v,dict):
        for x in v.values(): out+=numeric_values(x)
    else:
        try: out.append(float(str(v).replace(" ","").replace(",",".")))
        except: pass
    return out

def parse_date_any(v)->Optional[date]:
    if not v:return None
    vals=v if isinstance(v,list) else [v]
    parsed=[]
    for x in vals:
        s=str(x)[:10]
        for fmt in ("%Y-%m-%d","%Y%m%d","%d.%m.%Y"):
            try: parsed.append(datetime.strptime(s,fmt).date()); break
            except: pass
    future=[d for d in parsed if d>=date.today()]
    return min(future) if future else (max(parsed) if parsed else None)

def deadline_info(n):
    d=parse_date_any(n.get("deadline-receipt-tender-date-lot") or n.get("deadline-date-lot") or n.get("deadline"))
    if not d:return None,None,"Unbekannt"
    days=(d-date.today()).days
    status="Offen" if days>=0 else "Abgelaufen"
    return d,days,status

def proc_value(n)->Tuple[Optional[float],str,str]:
    cur=first_scalar(n.get("estimated-value-cur-proc") or n.get("estimated-value-cur-lot"),"EUR")
    lot=numeric_values(n.get("estimated-value-lot"))
    proc=numeric_values(n.get("estimated-value-proc"))
    if lot:
        return max(lot),cur,"Loswert (Schätzung)"
    if proc:
        return max(proc),cur,"Verfahrenswert (Schätzung)"
    return None,cur,"Kein Wert veröffentlicht"

def contract_kind(n):
    t=all_text(n.get("contract-nature") or n.get("contract-nature-main-proc") or n.get("contract-nature-main-lot")).lower()
    if "suppl" in t or "liefer" in t:return "Lieferung"
    if "service" in t or "dienst" in t:return "Dienstleistung"
    if "work" in t or "bau" in t:return "Bauleistung"
    return "Nicht klassifiziert"

BLOCKED = ["munition","ammunition","firearm","weapon","waffe","sprengstoff","explosive","missile","rakete","torpedo","mortar","gewehr","pistole","mine ","artillery"]
HARD = ["sicherheitsüberprüfung","security clearance","geheimschutz","bankbürgschaft","performance bond","referenzen der letzten","iso 27001","nato secret"]
GOOD = ["verbrauchsmaterial","werkzeug","kabel","stecker","drucker","toner","papier","möbel","leuchte","lampe","reinigung","ersatzteil","lager","schraube","elektro","it-zubehör","computer","monitor","textil","büromaterial","messgerät"]

def safety_flags(text):
    low=text.lower()
    return [x for x in BLOCKED if x in low]

def extract_product_intel(n):
    title=first_scalar(n.get("notice-title"),"")
    desc=" ".join([all_text(n.get("description-proc")),all_text(n.get("description-lot"))]).strip()
    text=(title+" "+desc).strip()
    quantities=[]
    patterns=[r'(?<!\d)(\d{1,6}(?:[.,]\d+)?)\s*(Stück|Stk\.?|pcs\.?|Einheiten|units|Packungen|Sets|Sätze|Meter|m\b)', r'(?:Menge|quantity)\s*[:\-]?\s*(\d{1,6}(?:[.,]\d+)?)']
    for p in patterns:
        for m in re.finditer(p,text,re.I):
            quantities.append(m.group(0).strip())
    article=[]
    for p in [r'(?:Artikel(?:nummer)?|Art\.?\s*-?Nr\.?|Modell|Model|Typ|Type|Part\s*No\.?|SKU)\s*[:#\-]?\s*([A-Z0-9][A-Z0-9._/\-]{2,})',r'\b[A-Z]{2,5}-\d{3,8}(?:-[A-Z0-9]{1,6})?\b']:
        article += [m.group(1) if m.lastindex else m.group(0) for m in re.finditer(p,text,re.I)]
    cpvs=n.get("classification-cpv") or []
    if not isinstance(cpvs,list):cpvs=[cpvs]
    # Product label: title is most reliable; do not fabricate exact SKU
    return {"title":title or "Produkt aus TED-Titel nicht ermittelbar","description":desc[:3500],"quantities":list(dict.fromkeys(quantities))[:12],"articles":list(dict.fromkeys(article))[:12],"cpv":[str(x) for x in cpvs][:12]}

def score_notice(n, capital, reserve, target_margin):
    intel=extract_product_intel(n); text=(intel["title"]+" "+intel["description"]).lower()
    if safety_flags(text): return 0,["Regulierte/ausgeschlossene Güter erkannt"],"NO-GO"
    score=48; reasons=[]
    d,days,status=deadline_info(n)
    if status=="Abgelaufen":return 0,["Angebotsfrist abgelaufen"],"NO-GO"
    if days is None: score-=12; reasons.append("Frist nicht strukturiert veröffentlicht")
    elif days>=21: score+=10; reasons.append(f"{days} Tage Vorlauf")
    elif days>=10: score+=5
    elif days<5: score-=15; reasons.append("Sehr kurze Restfrist")
    kind=contract_kind(n)
    if kind=="Lieferung":score+=12;reasons.append("Waren-/Lieferauftrag")
    elif kind=="Dienstleistung":score-=8
    val,cur,src=proc_value(n)
    usable=max(0,capital-reserve)
    if val:
        max_buy=val*(1-target_margin/100)
        if max_buy<=usable:score+=18;reasons.append("Theoretisch aus Eigenkapital finanzierbar")
        elif max_buy<=usable*2:score+=5;reasons.append("Moderate Finanzierungslücke")
        elif max_buy>usable*8:score-=18;reasons.append("Hohe Vorfinanzierung")
    else: score-=5;reasons.append("Kein Auftragswert veröffentlicht")
    if any(k in text for k in GOOD):score+=9;reasons.append("Gut beschaffbare Handelsware erkannt")
    hard=[k for k in HARD if k in text]
    if hard:score-=min(20,7*len(hard));reasons.append("Zusätzliche Eignungs-/Sicherheitsanforderungen")
    if intel["quantities"]:score+=5;reasons.append("Menge im Text erkannt")
    score=max(0,min(100,score))
    grade="GO" if score>=72 else "PRÜFEN" if score>=45 else "NO-GO"
    return score,reasons,grade

# ---------- TED client ----------
CORE_FIELDS=["publication-number","publication-date","notice-title","buyer-name","buyer-country","contract-nature","classification-cpv","deadline-receipt-tender-date-lot"]
DETAIL_FIELDS=CORE_FIELDS+["description-proc","description-lot","quantity-lot","quantity-unit-lot","estimated-value-proc","estimated-value-cur-proc","estimated-value-lot","estimated-value-cur-lot","submission-url-lot","selection-criterion-name-lot","selection-criterion-description-lot","framework-agreement-lot","framework-maximum-value-lot","framework-maximum-value-cur-lot"]
MIN_FIELDS=["publication-number","notice-title","buyer-name","publication-date"]

def ted_post(fields, limit=100, query="buyer-country=DEU"):
    payload={"query":query,"fields":fields,"page":1,"limit":int(limit),"scope":"ACTIVE","checkQuerySyntax":False,"paginationMode":"PAGE_NUMBER","onlyLatestVersions":True}
    r=requests.post(TED_URL,json=payload,headers={"Accept":"application/json","Content-Type":"application/json","User-Agent":"TenderScoutPro/1.0"},timeout=35)
    if r.status_code!=200:
        msg=r.text[:1200]
        raise RuntimeError(f"TED HTTP {r.status_code}: {msg}")
    data=r.json()
    return data.get("notices",[]), data

def scan_ted(limit):
    errors=[]
    for fields in (DETAIL_FIELDS,CORE_FIELDS,MIN_FIELDS):
        try:
            notices,meta=ted_post(fields,limit)
            return notices,meta,errors,fields
        except Exception as e: errors.append(str(e))
    raise RuntimeError(" | ".join(errors))

def fetch_detail(pub):
    try:
        notices,meta=ted_post(DETAIL_FIELDS,1,f"publication-number={pub}")
        return notices[0] if notices else None,None
    except Exception as e:
        try:
            notices,meta=ted_post(CORE_FIELDS,1,f"publication-number={pub}")
            return notices[0] if notices else None,str(e)
        except Exception as e2:return None,f"{e} | {e2}"

# ---------- calculations / sourcing ----------
def deal_finance(n, capital, reserve, margin):
    val,cur,src=proc_value(n); usable=max(0,capital-reserve)
    if not val:return {"revenue":None,"max_buy":None,"gross":None,"gap":None,"usable":usable,"currency":cur,"source":src}
    max_buy=val*(1-margin/100);gross=val-max_buy;gap=max(0,max_buy-usable)
    return {"revenue":val,"max_buy":max_buy,"gross":gross,"gap":gap,"usable":usable,"currency":cur,"source":src}

def procurement_queries(n):
    intel=extract_product_intel(n)
    title=intel["title"]
    query=(intel["articles"][0] if intel["articles"] else title)[:130]
    q=urllib.parse.quote_plus(query)
    return [
        ("Google B2B",f"https://www.google.com/search?q={q}+Gro%C3%9Fhandel+B2B+Deutschland"),
        ("Unite / Mercateo",f"https://www.google.com/search?q=site%3Aunite.eu+{q}"),
        ("RS",f"https://www.google.com/search?q=site%3Ade.rs-online.com+{q}"),
        ("Conrad",f"https://www.google.com/search?q=site%3Aconrad.de+{q}"),
        ("Farnell",f"https://www.google.com/search?q=site%3Ade.farnell.com+{q}"),
        ("Distrelec",f"https://www.google.com/search?q=site%3Adistrelec.de+{q}"),
        ("Würth",f"https://www.google.com/search?q=site%3Awuerth.de+{q}"),
    ]

def notice_url(pub):return f"https://ted.europa.eu/de/notice/-/detail/{pub}"

def watch_add(n):
    pub=first_scalar(n.get("publication-number"),"")
    if not pub:return
    with db() as con:
        con.execute("INSERT OR REPLACE INTO watchlist(pub,title,buyer,note,added,payload) VALUES(?,?,?,?,?,?)",(pub,first_scalar(n.get("notice-title"),""),first_scalar(n.get("buyer-name"),""),"",datetime.now().isoformat(timespec="seconds"),json.dumps(n,ensure_ascii=False)))

def watch_remove(pub):
    with db() as con:con.execute("DELETE FROM watchlist WHERE pub=?",(pub,))

def watched(pub):
    with db() as con:return con.execute("SELECT 1 FROM watchlist WHERE pub=?",(pub,)).fetchone() is not None

def save_quote(pub,supplier,buy,shipping,sell,note):
    with db() as con:con.execute("INSERT INTO quotes(pub,supplier,buy_price,shipping,sell_price,note,created) VALUES(?,?,?,?,?,?,?)",(pub,supplier,buy,shipping,sell,note,datetime.now().isoformat(timespec="seconds")))

def load_quotes(pub):
    with db() as con:return pd.read_sql_query("SELECT supplier,buy_price,shipping,sell_price,note,created FROM quotes WHERE pub=? ORDER BY id DESC",con,params=(pub,))

# ---------- header / navigation ----------
st.markdown('<div class="brand"><div class="brandlogo">📦</div><div><div class="brandtitle">Tender Scout Pro</div><div class="brandsub">Beschaffungs-Cockpit · Deutschland · EU TED</div></div></div>',unsafe_allow_html=True)
navcols=st.columns(6)
for c,label,ico in zip(navcols,["Dashboard","Scanner","Deal-Akte","Watchlist","Kalkulator","Einstellungen"],["⌂","⌕","▤","★","€","⚙"]):
    with c:
        if st.button(f"{ico} {label}",use_container_width=True,key=f"nav_{label}"):
            st.session_state.nav=label;st.rerun()

# ---------- shared filters ----------
def current_filtered():
    arr=[]
    kw=st.session_state.keyword.strip().lower()
    for n in st.session_state.notices:
        d,days,status=deadline_info(n)
        if status=="Abgelaufen": continue
        if st.session_state.hide_unknown_deadline and days is None: continue
        if days is not None and days<st.session_state.min_days:continue
        if st.session_state.only_supplies and contract_kind(n)!="Lieferung":continue
        score,_,_=score_notice(n,st.session_state.capital,st.session_state.reserve,st.session_state.target_margin)
        if score<st.session_state.min_score:continue
        if kw and kw not in all_text(n).lower():continue
        arr.append(n)
    return sorted(arr,key=lambda x:score_notice(x,st.session_state.capital,st.session_state.reserve,st.session_state.target_margin)[0],reverse=True)

def render_deal_card(n,idx):
    pub=first_scalar(n.get("publication-number"),"—"); title=first_scalar(n.get("notice-title"),"Ohne Titel"); buyer=first_scalar(n.get("buyer-name"),"—")
    score,reasons,grade=score_notice(n,st.session_state.capital,st.session_state.reserve,st.session_state.target_margin)
    d,days,status=deadline_info(n); fin=deal_finance(n,st.session_state.capital,st.session_state.reserve,st.session_state.target_margin)
    cls="good" if grade=="GO" else "warn" if grade=="PRÜFEN" else "bad"
    deadline_txt=(f"{d.strftime('%d.%m.%Y')} · {days} Tage" if d else "nicht strukturiert angegeben")
    st.markdown(f'''<div class="card deal {cls}"><span class="badge {cls}">{grade} · {score}/100</span><span class="badge">{contract_kind(n)}</span><span class="badge">TED {pub}</span><h3 style="margin:.55rem 0 .25rem">{title}</h3><div class="muted">{buyer}</div><div class="divider"></div><div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px"><div><div class="label">Frist</div><div class="value">{deadline_txt}</div></div><div><div class="label">Volumen</div><div class="value">{money(fin['revenue'],fin['currency']) if fin['revenue'] else 'nicht veröffentlicht'}</div></div><div><div class="label">Finanzierungslücke*</div><div class="value">{money(fin['gap'],fin['currency']) if fin['gap'] is not None else 'nicht berechenbar'}</div></div></div></div>''',unsafe_allow_html=True)
    c1,c2,c3=st.columns([1.25,1,1])
    with c1:
        if st.button("Deal-Akte öffnen",key=f"open{idx}_{pub}",use_container_width=True):st.session_state.selected=n;st.session_state.nav="Deal-Akte";st.rerun()
    with c2:
        if st.button("★ Merken" if not watched(pub) else "✓ Gemerkt",key=f"watch{idx}_{pub}",use_container_width=True):watch_add(n);st.toast("Zur Watchlist hinzugefügt")
    with c3:st.link_button("TED Original",notice_url(pub),use_container_width=True)

# ---------- pages ----------
if st.session_state.nav=="Dashboard":
    st.markdown('<div class="hero"><h1>Deine Ausschreibungen. Als Deals gedacht.</h1><p>Nicht möglichst viele Treffer — sondern offene Warenaufträge, die zu Kapital, Marge und Beschaffbarkeit passen.</p></div>',unsafe_allow_html=True)
    filtered=current_filtered(); total=len(st.session_state.notices); gos=sum(1 for n in filtered if score_notice(n,st.session_state.capital,st.session_state.reserve,st.session_state.target_margin)[2]=="GO")
    with db() as con:wcount=con.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
    cols=st.columns(4)
    vals=[("Geladen",str(total),"aus letztem Scan"),("Passend",str(len(filtered)),"nach deinen Filtern"),("GO-Chancen",str(gos),"Score ≥ 72"),("Watchlist",str(wcount),"gespeicherte Deals")]
    for c,(a,b,s) in zip(cols,vals):
        with c:st.markdown(f'<div class="kpi"><div class="kpi-label">{a}</div><div class="kpi-value">{b}</div><div class="kpi-sub">{s}</div></div>',unsafe_allow_html=True)
    st.subheader("Top-Chancen")
    if not st.session_state.notices:
        st.info("Noch kein Live-Scan in dieser Sitzung. Öffne **Scanner** und tippe auf **Deutschland jetzt scannen**.")
    else:
        for i,n in enumerate(filtered[:5]):render_deal_card(n,i)
    st.caption("* Finanzierungslücke basiert nur auf veröffentlichtem Schätzwert und deiner Zielmarge. Kein veröffentlichter Wert = keine erfundene Kalkulation.")

elif st.session_state.nav=="Scanner":
    st.markdown('<div class="hero"><h1>Live-Scanner</h1><p>Nur TED-Scope ACTIVE. Abgelaufene Fristen werden zusätzlich lokal entfernt. Die API fällt bei Feldänderungen automatisch auf einen stabileren Datensatz zurück.</p></div>',unsafe_allow_html=True)
    a,b,c=st.columns([1,1,1])
    with a: st.session_state.scan_limit=st.selectbox("Abrufmenge",[25,50,100,150,200],index=[25,50,100,150,200].index(st.session_state.scan_limit) if st.session_state.scan_limit in [25,50,100,150,200] else 2)
    with b: st.session_state.only_supplies=st.toggle("Nur Waren/Lieferungen",value=st.session_state.only_supplies)
    with c: st.session_state.hide_unknown_deadline=st.toggle("Unbekannte Frist ausblenden",value=st.session_state.hide_unknown_deadline)
    if st.button("🚀 Deutschland jetzt scannen",type="primary",use_container_width=True):
        with st.spinner("TED wird live abgefragt …"):
            try:
                notices,meta,errs,used=scan_ted(st.session_state.scan_limit)
                st.session_state.notices=notices;st.session_state.scan_error=None;st.session_state.last_scan=datetime.now().strftime("%d.%m.%Y %H:%M")
                st.success(f"{len(notices)} aktive TED-Bekanntmachungen geladen · API-Felder: {len(used)}")
                if errs:st.caption("Fallback wurde verwendet. Die App hat eine ungültige optionale Feldgruppe automatisch übersprungen.")
            except Exception as e:
                st.session_state.scan_error=str(e);st.error("TED konnte nicht geladen werden. Öffne unten die Diagnose — die App zeigt jetzt die echte Servermeldung statt nur ‘400 Bad Request’.")
    if st.session_state.scan_error:
        with st.expander("API-Diagnose"):
            st.code(st.session_state.scan_error)
            st.caption("Die Abfrage verwendet buyer-country=DEU, scope=ACTIVE und PAGE_NUMBER. Falls TED die Feldliste ändert, wird automatisch zweimal reduziert.")
    st.markdown("### Suchfilter")
    q1,q2,q3=st.columns([2,1,1])
    with q1:st.session_state.keyword=st.text_input("Produkt / Auftraggeber / CPV / Stichwort",value=st.session_state.keyword,placeholder="z. B. Kabel, Werkzeug, Drucker, Ersatzteile")
    with q2:st.session_state.min_days=st.number_input("Mind. Resttage",min_value=0,max_value=180,value=int(st.session_state.min_days))
    with q3:st.session_state.min_score=st.slider("Mind. Score",0,100,int(st.session_state.min_score),5)
    filtered=current_filtered();st.markdown(f"### {len(filtered)} passende Ausschreibungen")
    if st.session_state.last_scan:st.caption(f"Letzter Scan: {st.session_state.last_scan}")
    for i,n in enumerate(filtered):render_deal_card(n,1000+i)

elif st.session_state.nav=="Deal-Akte":
    n=st.session_state.selected
    if not n:
        st.info("Öffne im Scanner oder Dashboard zuerst eine Ausschreibung.")
    else:
        pub=first_scalar(n.get("publication-number"),"—")
        # refresh single detail if current payload is core/minimal
        if not n.get("description-proc") and not n.get("description-lot"):
            detail,err=fetch_detail(pub)
            if detail:
                merged=n.copy();merged.update(detail);n=merged;st.session_state.selected=n
        title=first_scalar(n.get("notice-title"),"Ohne Titel");buyer=first_scalar(n.get("buyer-name"),"—");intel=extract_product_intel(n);score,reasons,grade=score_notice(n,st.session_state.capital,st.session_state.reserve,st.session_state.target_margin);fin=deal_finance(n,st.session_state.capital,st.session_state.reserve,st.session_state.target_margin);d,days,status=deadline_info(n)
        st.markdown(f'<div class="hero"><span class="badge gold">TED {pub}</span><span class="badge">{contract_kind(n)}</span><h1>{title}</h1><p>{buyer}</p></div>',unsafe_allow_html=True)
        k=st.columns(4)
        metrics=[("Deal-Score",f"{score}/100",grade),("Angebotsfrist",d.strftime("%d.%m.%Y") if d else "unbekannt",f"{days} Tage" if days is not None else "prüfen"),("Ausschreibungswert",money(fin['revenue'],fin['currency']) if fin['revenue'] else "nicht veröffentlicht",fin['source']),("Kapitalbedarf*",money(fin['max_buy'],fin['currency']) if fin['max_buy'] is not None else "nicht berechenbar",f"Lücke {money(fin['gap'],fin['currency'])}" if fin['gap'] is not None else "")]
        for c,(lab,val,sub) in zip(k,metrics):
            with c:st.metric(lab,val,sub)
        tabs=st.tabs(["📦 Produkt","💶 Umsatz & Kapital","🏭 Beschaffung","✅ Anforderungen","📄 Original","📝 Lieferantenangebote"])
        with tabs[0]:
            st.subheader("Was wird beschafft?")
            st.markdown(f"**Produkt-/Leistungstitel:** {intel['title']}")
            if intel["quantities"]:st.markdown("**Erkannte Mengen:** "+" · ".join(intel["quantities"]))
            else:st.warning("Keine belastbare Stückzahl im veröffentlichten TED-Text erkannt. Die App erfindet keine Menge.")
            if intel["articles"]:st.markdown("**Artikel / Modell / Typ:** "+" · ".join(intel["articles"]))
            else:st.info("Keine eindeutige Artikel-/Modellnummer in der Bekanntmachung erkannt.")
            if intel["cpv"]:st.markdown("**CPV:** "+", ".join(intel["cpv"]))
            st.markdown("**Veröffentlichte Beschreibung**")
            st.write(intel["description"] or "Keine Detailbeschreibung als strukturiertes TED-Feld verfügbar. Öffne die Originalunterlagen.")
        with tabs[1]:
            st.subheader("Deal-Kalkulation")
            if fin["revenue"]:
                st.info("Der TED-Wert ist ein veröffentlichter Schätz-/Loswert. Bei Rahmenvereinbarungen ist er **kein garantierter Umsatz**.")
                c1,c2,c3,c4=st.columns(4)
                c1.metric("Theoretisches Volumen",money(fin["revenue"],fin["currency"]))
                c2.metric("Max. Einkauf bei Zielmarge",money(fin["max_buy"],fin["currency"]))
                c3.metric("Rohertragsziel",money(fin["gross"],fin["currency"]))
                c4.metric("Finanzierungslücke",money(fin["gap"],fin["currency"]))
            else:st.warning("TED veröffentlicht für diese Bekanntmachung keinen belastbaren Wert. Umsatz wird deshalb nicht geschätzt.")
            st.markdown("**Warum dieser Score?**")
            for r in reasons:st.write("• "+r)
        with tabs[2]:
            if safety_flags((intel["title"]+" "+intel["description"])):
                st.error("Beschaffungssuche deaktiviert: regulierte/ausgeschlossene Güter erkannt.")
            else:
                st.subheader("Bezugsquellen recherchieren")
                st.caption("Diese Buttons starten eine gezielte Lieferantensuche. Preise werden erst als ‘echt’ behandelt, wenn du ein Angebot einträgst.")
                sources=procurement_queries(n)
                cols=st.columns(2)
                for i,(name,url) in enumerate(sources):
                    with cols[i%2]:st.link_button(f"↗ {name}",url,use_container_width=True)
                st.markdown("#### Anfrage an Lieferanten")
                st.code(f"Betreff: Angebotsanfrage – {intel['title'][:90]}\n\nGuten Tag,\nbitte senden Sie uns Ihr bestes B2B-Angebot für die nachfolgende Position inkl. Lieferzeit, Versand, Zahlungsziel und Gültigkeit.\n\nProdukt: {intel['title']}\nMenge: {', '.join(intel['quantities']) if intel['quantities'] else '[laut Vergabeunterlagen]'}\nArtikel/Typ: {', '.join(intel['articles']) if intel['articles'] else '[falls spezifiziert]'}\n\nBitte bestätigen Sie außerdem die technische Gleichwertigkeit zur geforderten Spezifikation.",language=None)
        with tabs[3]:
            st.subheader("Prüfmatrix")
            reqtext=intel["description"].lower()
            checks=[("Frist belastbar",d is not None),("Wert veröffentlicht",fin["revenue"] is not None),("Menge erkannt",bool(intel["quantities"])),("Artikel/Typ erkannt",bool(intel["articles"])),("Keine regulierten Güter",not safety_flags(reqtext+" "+intel["title"].lower())),("Keine offensichtliche Sicherheitsanforderung",not any(x in reqtext for x in HARD))]
            for lab,ok in checks:st.write(("✅" if ok else "⚠️")+" "+lab)
            criteria=all_text(n.get("selection-criterion-name-lot"))+" "+all_text(n.get("selection-criterion-description-lot"))
            if criteria:st.markdown("**Eignungs-/Auswahlkriterien**");st.write(criteria[:5000])
            else:st.info("Keine Eignungskriterien im geladenen strukturierten Feld. Originalunterlagen prüfen.")
        with tabs[4]:
            st.link_button("TED Originalbekanntmachung öffnen",notice_url(pub),use_container_width=True)
            sub=first_scalar(n.get("submission-url-lot"),"")
            if sub and sub!="—":st.link_button("Vergabe-/Einreichungsportal öffnen",sub,use_container_width=True)
            links=n.get("links")
            if links:st.json(links,expanded=False)
            st.caption("Die Leistungsbeschreibung kann außerhalb von TED auf einem nationalen Vergabeportal liegen. Tender Scout zeigt nur Daten als sicher an, die tatsächlich geladen wurden.")
        with tabs[5]:
            st.subheader("Echte Einkaufspreise hinterlegen")
            with st.form("quote_form"):
                supplier=st.text_input("Lieferant")
                qbuy=st.number_input("Waren-Einkauf netto",0.0,1e9,0.0,step=10.0)
                ship=st.number_input("Versand / Nebenkosten netto",0.0,1e9,0.0,step=10.0)
                sell=st.number_input("Geplanter Verkauf netto",0.0,1e9,float(fin['revenue'] or 0),step=10.0)
                note=st.text_area("Notiz / Lieferzeit / Zahlungsziel")
                if st.form_submit_button("Angebot speichern",use_container_width=True) and supplier:
                    save_quote(pub,supplier,qbuy,ship,sell,note);st.success("Gespeichert")
            qdf=load_quotes(pub)
            if not qdf.empty:
                qdf["DB €"]=qdf["sell_price"]-qdf["buy_price"]-qdf["shipping"]
                qdf["DB %"]=(qdf["DB €"]/qdf["sell_price"].replace(0,float("nan"))*100).round(1)
                st.dataframe(qdf,use_container_width=True,hide_index=True)
        st.markdown("---")
        wc1,wc2=st.columns(2)
        with wc1:
            if watched(pub):
                if st.button("Von Watchlist entfernen",use_container_width=True):watch_remove(pub);st.rerun()
            else:
                if st.button("★ Zur Watchlist",type="primary",use_container_width=True):watch_add(n);st.rerun()
        with wc2:st.link_button("↗ TED öffnen",notice_url(pub),use_container_width=True)
        st.caption("* Kapitalbedarf = veröffentlichter Wert × (1 − Zielmarge). Das ist ein Filter, kein echter Einkaufspreis. Echte Preise entstehen erst aus Lieferantenangeboten.")

elif st.session_state.nav=="Watchlist":
    st.markdown('<div class="hero"><h1>Watchlist</h1><p>Deine vorgemerkten Chancen und Lieferantenkalkulationen.</p></div>',unsafe_allow_html=True)
    with db() as con:rows=con.execute("SELECT pub,title,buyer,note,added,payload FROM watchlist ORDER BY added DESC").fetchall()
    if not rows:st.info("Noch nichts gemerkt.")
    for i,(pub,title,buyer,note,added,payload) in enumerate(rows):
        n=json.loads(payload);render_deal_card(n,3000+i)

elif st.session_state.nav=="Kalkulator":
    st.markdown('<div class="hero"><h1>Angebots-Kalkulator</h1><p>Vom echten Einkaufspreis zu deinem Mindestverkaufspreis und Deckungsbeitrag.</p></div>',unsafe_allow_html=True)
    c1,c2=st.columns(2)
    with c1:
        buy=st.number_input("Wareneinkauf netto",0.0,1e9,1500.0,step=50.0)
        shipping=st.number_input("Fracht / Verpackung",0.0,1e9,80.0,step=10.0)
        other=st.number_input("Sonstige direkte Kosten",0.0,1e9,50.0,step=10.0)
    with c2:
        target=st.number_input("Ziel-DB in % vom Verkauf",1.0,80.0,float(st.session_state.target_margin),step=1.0)
        vat=st.number_input("USt. % (nur Anzeige)",0.0,30.0,19.0,step=1.0)
        payment_days=st.number_input("Erwartetes Zahlungsziel (Tage)",0,180,30)
    cost=buy+shipping+other
    sell=cost/(1-target/100) if target<100 else 0
    dbv=sell-cost
    cols=st.columns(4);cols[0].metric("Gesamtkosten",money(cost));cols[1].metric("Mindestverkauf netto",money(sell));cols[2].metric("Deckungsbeitrag",money(dbv));cols[3].metric("Brutto-Rechnung",money(sell*(1+vat/100)))
    usable=max(0,st.session_state.capital-st.session_state.reserve)
    if cost<=usable:st.success(f"Mit deiner aktuellen Liquiditätsreserve finanzierbar. Puffer: {money(usable-cost)}")
    else:st.warning(f"Finanzierungslücke vor Zahlung des Kunden: {money(cost-usable)}")

elif st.session_state.nav=="Einstellungen":
    st.markdown('<div class="hero"><h1>Einstellungen</h1><p>Dein persönlicher Deal-Filter. Änderungen wirken sofort auf Scores und Kapitalprüfung.</p></div>',unsafe_allow_html=True)
    st.session_state.capital=st.number_input("Verfügbares Geschäftskapital (€)",0.0,1e7,float(st.session_state.capital),step=100.0)
    st.session_state.reserve=st.number_input("Davon Sicherheitsreserve (€)",0.0,float(st.session_state.capital),min(float(st.session_state.reserve),float(st.session_state.capital)),step=100.0)
    st.session_state.target_margin=st.slider("Zielmarge / DB-Filter (%)",5,60,int(st.session_state.target_margin),1)
    st.info(f"Für Waren reserviert Tender Scout aktuell maximal **{money(max(0,st.session_state.capital-st.session_state.reserve))}** Eigenkapital.")
    st.markdown("### Datensicherung")
    with db() as con:
        w=pd.read_sql_query("SELECT * FROM watchlist",con);q=pd.read_sql_query("SELECT * FROM quotes",con)
    backup=json.dumps({"watchlist":w.to_dict(orient="records"),"quotes":q.to_dict(orient="records"),"exported":datetime.now().isoformat()},ensure_ascii=False,indent=2)
    st.download_button("Backup herunterladen",backup,file_name="tender_scout_backup.json",mime="application/json",use_container_width=True)
    st.warning("Streamlit Community Cloud kann lokale Dateien bei Neustarts zurücksetzen. Für langfristig garantiert persistente Daten sollte später eine Cloud-Datenbank (z. B. Supabase/Postgres) verbunden werden.")
    st.caption(f"Tender Scout Pro · {APP_VERSION}")
