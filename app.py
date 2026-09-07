import io, json, re, sqlite3, urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from pypdf import PdfReader

APP_VERSION = "2026.09.07 Rebuild"
TED_API = "https://api.ted.europa.eu/v3/notices/search"
DB_PATH = Path("tender_scout.db")
TODAY = date.today()

st.set_page_config(page_title="Tender Scout Pro", page_icon="📦", layout="wide", initial_sidebar_state="collapsed")

st.markdown("""
<style>
:root{--bg:#07111f;--card:#0d1a2b;--line:#223751;--muted:#8ea0b8;--gold:#e9bd55;--good:#31ce79;--warn:#ffb84a;--bad:#ff6677;--white:#f6f8fb}
.stApp{background:linear-gradient(180deg,#07111f,#08131f 65%,#06101a);color:var(--white)}
.block-container{max-width:1180px;padding-top:.8rem;padding-bottom:5rem}
#MainMenu,footer,header{visibility:hidden}.stDeployButton{display:none}
.hero{background:linear-gradient(135deg,#10243b,#0a1727 65%,#132236);border:1px solid #29425e;border-radius:22px;padding:20px;margin-bottom:14px;box-shadow:0 15px 38px #0005}
.hero h1{margin:0;font-size:1.9rem}.hero p{color:#9caec2;margin:.45rem 0 0}
.brand{display:flex;align-items:center;gap:11px;margin-bottom:12px}.logo{width:44px;height:44px;border-radius:13px;background:#10233a;border:1px solid #2a4562;display:flex;align-items:center;justify-content:center;font-size:24px}.bt{font-weight:850;font-size:1.3rem}.bs{font-size:.77rem;color:#879bb5}
.kpi{background:#0d1a2b;border:1px solid #223751;border-radius:16px;padding:14px;min-height:92px}.kl{font-size:.73rem;color:#8fa2b9;text-transform:uppercase}.kv{font-size:1.45rem;font-weight:850;margin-top:4px}.ks{font-size:.73rem;color:#73879f}
.card{background:linear-gradient(160deg,#0d1b2d,#091522);border:1px solid #213850;border-radius:18px;padding:16px;margin:10px 0}.good{border-left:4px solid #31ce79}.warn{border-left:4px solid #ffb84a}.bad{border-left:4px solid #ff6677}
.badge{display:inline-block;border:1px solid #304860;background:#102339;border-radius:999px;padding:4px 9px;font-size:.7rem;font-weight:750;margin:1px 4px 1px 0}.bg{color:#61e69c;border-color:#266c49;background:#0c2b1e}.bw{color:#ffc873;border-color:#745128;background:#2d2313}.bb{color:#ff8793;border-color:#6e3440;background:#2d171d}.gold{color:#f1cb70;border-color:#65532e;background:#2b2516}
.muted{color:#8da0b7}.label{font-size:.72rem;color:#8498b0;text-transform:uppercase}.value{font-weight:750}.divider{height:1px;background:#20354b;margin:12px 0}
.stButton>button{border-radius:13px!important;min-height:44px;font-weight:750}.stTextInput input,.stNumberInput input{border-radius:12px!important}
[data-testid="stExpander"]{background:#0b1928;border:1px solid #21364c;border-radius:14px}
@media(max-width:700px){.block-container{padding-left:.7rem;padding-right:.7rem}.hero{padding:16px}.hero h1{font-size:1.55rem}.bt{font-size:1.12rem}.kpi{min-height:82px}.kv{font-size:1.25rem}}
</style>
""", unsafe_allow_html=True)

# ---------------- state / db ----------------
def defaults():
    vals={"nav":"Dashboard","capital":2000.0,"reserve":500.0,"margin":22.0,"notices":[],"last_scan":None,"scan_error":None,"selected":None,"keyword":"","min_days":5,"max_age":120,"scan_limit":75,"min_score":0,"strict_deadline":True}
    for k,v in vals.items(): st.session_state.setdefault(k,v)
defaults()

def db():
    con=sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS watchlist(pub TEXT PRIMARY KEY,title TEXT,buyer TEXT,added TEXT,payload TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS quotes(id INTEGER PRIMARY KEY AUTOINCREMENT,pub TEXT,supplier TEXT,buy REAL,shipping REAL,sell REAL,note TEXT,created TEXT)")
    return con

# ---------------- generic parsers ----------------
def first(v, default="—"):
    if v is None:return default
    if isinstance(v,dict):
        for lang in ("deu","ger","eng"):
            if lang in v and v[lang]: return first(v[lang],default)
        for x in v.values():
            if x:return first(x,default)
        return default
    if isinstance(v,list): return first(v[0],default) if v else default
    return str(v)

def text(v):
    if v is None:return ""
    if isinstance(v,dict):return " ".join(text(x) for x in v.values())
    if isinstance(v,list):return " ".join(text(x) for x in v)
    return str(v)

def to_date(v):
    if not v:return None
    if isinstance(v,(list,tuple)):
        ds=[to_date(x) for x in v]; ds=[x for x in ds if x]
        future=[x for x in ds if x>=TODAY]
        return min(future) if future else (max(ds) if ds else None)
    s=str(v).strip()[:10]
    for f in ("%Y-%m-%d","%Y%m%d","%d.%m.%Y"):
        try:return datetime.strptime(s,f).date()
        except:pass
    return None

def num(v):
    vals=[]
    if isinstance(v,list):
        for x in v: vals+=num(x)
    elif isinstance(v,dict):
        for x in v.values(): vals+=num(x)
    elif v is not None:
        try: vals.append(float(str(v).replace(" ","").replace(",",".")))
        except: pass
    return vals

def money(v,cur="EUR"):
    if v is None:return "nicht angegeben"
    try:return f"{float(v):,.0f} {cur}".replace(",",".")
    except:return "nicht angegeben"

def pub_year(n):
    p=first(n.get("publication-number"),"")
    m=re.search(r"-(20\d{2})$",p)
    return int(m.group(1)) if m else None

def publication_date(n): return to_date(n.get("publication-date"))
def deadline(n): return to_date(n.get("deadline") or n.get("deadline-receipt-tender-date-lot") or n.get("deadline-date-lot"))

def nature(n):
    x=text(n.get("contract-nature")).lower()
    if "suppl" in x:return "Lieferung"
    if "service" in x:return "Dienstleistung"
    if "work" in x:return "Bauleistung"
    return first(n.get("contract-nature"),"Nicht klassifiziert")

def value_info(n):
    cur=first(n.get("total-value-cur") or n.get("estimated-value-cur-lot") or n.get("estimated-value-cur-proc"),"EUR")
    for key,label in [("total-value","Veröffentlichter Gesamtwert"),("estimated-value-lot","Geschätzter Loswert"),("estimated-value-proc","Geschätzter Verfahrenswert")]:
        xs=num(n.get(key))
        if xs:return max(xs),cur,label
    return None,cur,"Kein belastbarer Wert veröffentlicht"

# ---------------- TED ----------------
# Minimal fields are deliberately based on fields currently documented/observed for Search API.
BASE_FIELDS=["publication-number","notice-title","notice-type","buyer-name","buyer-country","contract-nature","classification-cpv","total-value","total-value-cur","publication-date","deadline","links"]

class TEDException(Exception): pass

def ted_search(limit=75,max_age=120,keyword=""):
    start=(TODAY-timedelta(days=max_age)).strftime("%Y%m%d")
    parts=["buyer-country=DEU",f"PD>={start}"]
    if keyword.strip():
        safe=keyword.strip().replace('"','')[:90]
        parts.append(f'FT~"{safe}"')
    query=" AND ".join(parts)+" SORT BY publication-date DESC"
    # Keep request intentionally small: this matches the current public API examples.
    payload={"query":query,"fields":BASE_FIELDS,"limit":min(int(limit),100),"scope":"ACTIVE","paginationMode":"ITERATION"}
    try:
        r=requests.post(TED_API,json=payload,headers={"Content-Type":"application/json","Accept":"application/json"},timeout=35)
    except requests.RequestException as e:
        raise TEDException(f"Netzwerkfehler: {e}")
    if r.status_code!=200:
        raise TEDException(f"TED HTTP {r.status_code}: {r.text[:1800]}")
    try:data=r.json()
    except Exception:raise TEDException("TED lieferte keine gültige JSON-Antwort.")
    notices=data.get("notices") or []
    return notices,data,query

def hard_validate(n,max_age,strict_deadline=True):
    # 1) reject impossible/archive years regardless of TED scope
    y=pub_year(n)
    if y and y < TODAY.year-1:return False,"Archivjahr"
    # 2) publication freshness
    pd=publication_date(n)
    if not pd:return False,"Kein Publikationsdatum"
    if pd < TODAY-timedelta(days=max_age):return False,"Zu alt"
    if pd > TODAY+timedelta(days=2):return False,"Ungültiges Publikationsdatum"
    # 3) only supplies for this business model
    if nature(n)!="Lieferung":return False,"Keine Lieferung"
    # 4) real open deadline; strict mode removes planning/results/no-deadline notices
    d=deadline(n)
    if d is None and strict_deadline:return False,"Keine Angebotsfrist"
    if d and d < TODAY:return False,"Frist abgelaufen"
    return True,"OK"

# ---------------- product / risk ----------------
BLOCKED=["munition","ammunition","firearm","weapon","waffe","sprengstoff","explosive","missile","rakete","torpedo","gewehr","pistole","mine ","artillery"]
GOOD=["werkzeug","kabel","stecker","drucker","toner","papier","möbel","leuchte","lampe","reinigung","ersatzteil","schraube","elektro","computer","monitor","textil","büromaterial","messgerät","lagerbedarf","verbrauchsmaterial"]
HARD=["sicherheitsüberprüfung","security clearance","geheimschutz","bankbürgschaft","performance bond","iso 27001","referenzen"]

def product_intel(n,extra_text=""):
    title=first(n.get("notice-title"),"Ohne Titel")
    blob=" ".join([title,text(n.get("description-lot")),text(n.get("description-proc")),extra_text]).strip()
    quantities=[]
    for p in [r'(?<!\d)(\d{1,7}(?:[.,]\d+)?)\s*(Stück|Stk\.?|pcs\.?|Einheiten|units|Packungen|Sets|Sätze|Meter|kg|Liter)',r'(?:Menge|Quantity)\s*[:\-]?\s*(\d{1,7}(?:[.,]\d+)?)']:
        quantities += [m.group(0).strip() for m in re.finditer(p,blob,re.I)]
    articles=[]
    for p in [r'(?:Artikel(?:nummer)?|Art\.?\s*-?Nr\.?|Modell|Model|Typ|Type|Part\s*No\.?|SKU)\s*[:#\-]?\s*([A-Z0-9][A-Z0-9._/\-]{2,})',r'\b[A-Z]{2,6}-\d{3,10}(?:-[A-Z0-9]{1,8})?\b']:
        articles += [m.group(1) if m.lastindex else m.group(0) for m in re.finditer(p,blob,re.I)]
    cpv=n.get("classification-cpv") or []
    if not isinstance(cpv,list):cpv=[cpv]
    return {"title":title,"blob":blob,"quantities":list(dict.fromkeys(quantities))[:20],"articles":list(dict.fromkeys(articles))[:20],"cpv":[str(x) for x in cpv][:20]}

def score(n,capital,reserve,margin,extra_text=""):
    intel=product_intel(n,extra_text); low=intel["blob"].lower(); reasons=[]
    if any(x in low for x in BLOCKED):return 0,"NO-GO",["Regulierte/ausgeschlossene Güter erkannt"]
    s=55
    d=deadline(n); days=(d-TODAY).days if d else None
    if days is not None:
        if days>=21:s+=12;reasons.append(f"{days} Tage Restlaufzeit")
        elif days>=10:s+=7
        elif days<5:s-=18;reasons.append("Sehr kurze Restlaufzeit")
    val,cur,src=value_info(n); usable=max(0,capital-reserve)
    if val:
        maxbuy=val*(1-margin/100)
        if maxbuy<=usable:s+=18;reasons.append("Kapitalfilter passt")
        elif maxbuy<=usable*2:s+=4;reasons.append("Moderate Finanzierungslücke")
        elif maxbuy>usable*8:s-=18;reasons.append("Hohe Vorfinanzierung")
    else:s-=5;reasons.append("Kein Wert veröffentlicht")
    if any(x in low for x in GOOD):s+=8;reasons.append("Gut beschaffbare Handelsware")
    if intel["quantities"]:s+=5;reasons.append("Menge erkannt")
    if intel["articles"]:s+=4;reasons.append("Artikel/Typ erkannt")
    if any(x in low for x in HARD):s-=12;reasons.append("Eignungs-/Sicherheitsanforderung möglich")
    s=max(0,min(100,s)); grade="GO" if s>=72 else "PRÜFEN" if s>=45 else "NO-GO"
    return s,grade,reasons

# ---------------- document analysis ----------------
def pdf_text(upload):
    try:
        reader=PdfReader(upload)
        return "\n".join((p.extract_text() or "") for p in reader.pages)[:200000]
    except Exception as e:return ""

def fetch_ted_html(pub):
    url=f"https://ted.europa.eu/de/notice/{pub}/html"
    try:
        r=requests.get(url,headers={"User-Agent":"Mozilla/5.0"},timeout=25)
        if r.status_code!=200:return "",f"HTTP {r.status_code}"
        soup=BeautifulSoup(r.text,"html.parser")
        for x in soup(["script","style","noscript"]):x.decompose()
        return " ".join(soup.stripped_strings)[:150000],None
    except Exception as e:return "",str(e)

# ---------------- watchlist ----------------
def is_watched(pub):
    with db() as con:return con.execute("SELECT 1 FROM watchlist WHERE pub=?",(pub,)).fetchone() is not None

def add_watch(n):
    pub=first(n.get("publication-number"),"")
    with db() as con:con.execute("INSERT OR REPLACE INTO watchlist(pub,title,buyer,added,payload) VALUES(?,?,?,?,?)",(pub,first(n.get("notice-title"),""),first(n.get("buyer-name"),""),datetime.now().isoformat(timespec="seconds"),json.dumps(n,ensure_ascii=False)))

def remove_watch(pub):
    with db() as con:con.execute("DELETE FROM watchlist WHERE pub=?",(pub,))

# ---------------- UI ----------------
st.markdown('<div class="brand"><div class="logo">📦</div><div><div class="bt">Tender Scout Pro</div><div class="bs">Live Procurement Cockpit · Germany · TED</div></div></div>',unsafe_allow_html=True)
navs=[("Dashboard","⌂"),("Scanner","⌕"),("Deal-Akte","▤"),("Watchlist","★"),("Kalkulator","€"),("Einstellungen","⚙")]
cols=st.columns(len(navs))
for c,(name,ico) in zip(cols,navs):
    with c:
        if st.button(f"{ico} {name}",use_container_width=True,key="nav"+name):st.session_state.nav=name;st.rerun()

def filtered():
    out=[]; kw=st.session_state.keyword.strip().lower()
    for n in st.session_state.notices:
        ok,_=hard_validate(n,st.session_state.max_age,st.session_state.strict_deadline)
        if not ok:continue
        d=deadline(n); days=(d-TODAY).days if d else 999
        if days<st.session_state.min_days:continue
        if kw and kw not in text(n).lower():continue
        sc,gr,_=score(n,st.session_state.capital,st.session_state.reserve,st.session_state.margin)
        if sc<st.session_state.min_score:continue
        out.append(n)
    return sorted(out,key=lambda n:(publication_date(n) or date(1900,1,1),score(n,st.session_state.capital,st.session_state.reserve,st.session_state.margin)[0]),reverse=True)

def card(n,i):
    pub=first(n.get("publication-number")); title=first(n.get("notice-title"),"Ohne Titel"); buyer=first(n.get("buyer-name")); sc,gr,reasons=score(n,st.session_state.capital,st.session_state.reserve,st.session_state.margin); d=deadline(n); val,cur,src=value_info(n)
    cls="good" if gr=="GO" else "warn" if gr=="PRÜFEN" else "bad"; bcls="bg" if gr=="GO" else "bw" if gr=="PRÜFEN" else "bb"
    days=(d-TODAY).days if d else None
    st.markdown(f'''<div class="card {cls}"><span class="badge {bcls}">{gr} · {sc}/100</span><span class="badge">{nature(n)}</span><span class="badge gold">{publication_date(n).strftime('%d.%m.%Y') if publication_date(n) else '—'}</span><h3 style="margin:.55rem 0 .25rem">{title}</h3><div class="muted">{buyer}</div><div class="divider"></div><div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px"><div><div class="label">Frist</div><div class="value">{d.strftime('%d.%m.%Y') if d else '—'}{f' · {days} Tage' if days is not None else ''}</div></div><div><div class="label">Volumen</div><div class="value">{money(val,cur)}</div></div><div><div class="label">CPV</div><div class="value">{first(n.get('classification-cpv'),'—')}</div></div></div></div>''',unsafe_allow_html=True)
    a,b,c=st.columns([1.2,1,1])
    with a:
        if st.button("Deal-Akte öffnen",key=f"open{i}{pub}",use_container_width=True):st.session_state.selected=n;st.session_state.nav="Deal-Akte";st.rerun()
    with b:
        if st.button("✓ Gemerkt" if is_watched(pub) else "★ Merken",key=f"wat{i}{pub}",use_container_width=True):add_watch(n);st.toast("Gespeichert")
    with c:st.link_button("TED Original",f"https://ted.europa.eu/de/notice/-/detail/{pub}",use_container_width=True)

if st.session_state.nav=="Dashboard":
    st.markdown('<div class="hero"><h1>Aktuelle Beschaffungschancen statt Archivtreffer.</h1><p>Nur frische deutsche Waren-Ausschreibungen mit offener Angebotsfrist. Alte Jahre und abgelaufene Fristen werden hart verworfen.</p></div>',unsafe_allow_html=True)
    f=filtered()
    with db() as con:w=con.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
    metrics=[("Aktuell geladen",len(st.session_state.notices),"letzter Live-Scan"),("Offen & passend",len(f),"nach Hartfiltern"),("GO-Chancen",sum(score(n,st.session_state.capital,st.session_state.reserve,st.session_state.margin)[1]=="GO" for n in f),"Score ≥ 72"),("Watchlist",w,"gespeichert")]
    cs=st.columns(4)
    for c,(lab,val,sub) in zip(cs,metrics):
        with c:st.markdown(f'<div class="kpi"><div class="kl">{lab}</div><div class="kv">{val}</div><div class="ks">{sub}</div></div>',unsafe_allow_html=True)
    st.subheader("Neueste Chancen")
    if not f:st.info("Noch keine aktuellen Treffer geladen. Gehe auf **Scanner** und starte einen Live-Scan.")
    for i,n in enumerate(f[:8]):card(n,i)

elif st.session_state.nav=="Scanner":
    st.markdown('<div class="hero"><h1>Live-Scanner</h1><p>Aktive TED-Daten + zusätzlicher Veröffentlichungszeitraum + offene Angebotsfrist + Warenfilter. Dadurch können 2016er Archivtreffer nicht mehr durchrutschen.</p></div>',unsafe_allow_html=True)
    c1,c2,c3=st.columns(3)
    with c1:st.session_state.max_age=st.selectbox("Nur veröffentlicht in den letzten",[30,60,90,120,180],index=[30,60,90,120,180].index(st.session_state.max_age) if st.session_state.max_age in [30,60,90,120,180] else 3,format_func=lambda x:f"{x} Tagen")
    with c2:st.session_state.scan_limit=st.selectbox("Abrufmenge",[25,50,75,100],index=[25,50,75,100].index(st.session_state.scan_limit))
    with c3:st.session_state.strict_deadline=st.toggle("Nur mit offener Angebotsfrist",value=st.session_state.strict_deadline)
    live_kw=st.text_input("Live-Suchbegriff (optional)",placeholder="z. B. Werkzeug, Kabel, Toner, Ersatzteile")
    if st.button("🚀 Aktuelle deutsche Waren-Ausschreibungen laden",type="primary",use_container_width=True):
        with st.spinner("TED wird live abgefragt …"):
            try:
                raw,meta,q=ted_search(st.session_state.scan_limit,st.session_state.max_age,live_kw)
                cleaned=[]; rejected={}
                for n in raw:
                    ok,why=hard_validate(n,st.session_state.max_age,st.session_state.strict_deadline)
                    if ok:cleaned.append(n)
                    else:rejected[why]=rejected.get(why,0)+1
                st.session_state.notices=cleaned;st.session_state.last_scan=datetime.now().strftime("%d.%m.%Y %H:%M");st.session_state.scan_error=None
                st.success(f"{len(cleaned)} aktuelle offene Waren-Ausschreibungen geladen. {len(raw)-len(cleaned)} irrelevante/alte Treffer verworfen.")
                with st.expander("Scan-Details"):
                    st.code(q);st.write("Verworfen:",rejected)
            except Exception as e:
                st.session_state.scan_error=str(e);st.error("TED-Live-Scan fehlgeschlagen. Die genaue Servermeldung steht unten.")
    if st.session_state.scan_error:
        with st.expander("API-Diagnose",expanded=True):st.code(st.session_state.scan_error)
    st.markdown("### Ergebnisfilter")
    q1,q2,q3=st.columns([2,1,1])
    with q1:st.session_state.keyword=st.text_input("In geladenen Treffern suchen",value=st.session_state.keyword,placeholder="Produkt, Auftraggeber, CPV …")
    with q2:st.session_state.min_days=st.number_input("Mind. Resttage",0,180,int(st.session_state.min_days))
    with q3:st.session_state.min_score=st.slider("Mind. Deal-Score",0,100,int(st.session_state.min_score),5)
    f=filtered();st.markdown(f"### {len(f)} passende aktuelle Ausschreibungen")
    if st.session_state.last_scan:st.caption("Letzter Scan: "+st.session_state.last_scan)
    for i,n in enumerate(f):card(n,1000+i)

elif st.session_state.nav=="Deal-Akte":
    n=st.session_state.selected
    if not n:st.info("Öffne zuerst im Scanner eine Ausschreibung.")
    else:
        pub=first(n.get("publication-number")); title=first(n.get("notice-title"),"Ohne Titel"); buyer=first(n.get("buyer-name")); d=deadline(n); val,cur,src=value_info(n); sc,gr,reasons=score(n,st.session_state.capital,st.session_state.reserve,st.session_state.margin); usable=max(0,st.session_state.capital-st.session_state.reserve); max_buy=val*(1-st.session_state.margin/100) if val else None; gap=max(0,max_buy-usable) if max_buy is not None else None
        st.markdown(f'<div class="hero"><span class="badge gold">TED {pub}</span><span class="badge">{nature(n)}</span><h1>{title}</h1><p>{buyer}</p></div>',unsafe_allow_html=True)
        m=st.columns(4);m[0].metric("Deal-Score",f"{sc}/100",gr);m[1].metric("Angebotsfrist",d.strftime("%d.%m.%Y") if d else "—",f"{(d-TODAY).days} Tage" if d else "");m[2].metric("Veröff. Volumen",money(val,cur),src);m[3].metric("Finanzierungslücke*",money(gap,cur) if gap is not None else "—")
        tabs=st.tabs(["📦 Produkt & Menge","📄 Unterlagen analysieren","💶 Kalkulation","🏭 Beschaffung","✅ Risiken","🔗 Original"])
        with tabs[0]:
            intel=product_intel(n)
            st.markdown(f"**Beschaffungsgegenstand:** {intel['title']}")
            st.markdown("**CPV:** "+(", ".join(intel["cpv"]) if intel["cpv"] else "nicht angegeben"))
            st.write("**Mengen erkannt:**",", ".join(intel["quantities"]) if intel["quantities"] else "In den TED-Kerndaten nicht enthalten – Unterlagen analysieren.")
            st.write("**Artikel / Typ / Modell erkannt:**",", ".join(intel["articles"]) if intel["articles"] else "Nicht in den TED-Kerndaten enthalten.")
        with tabs[1]:
            st.subheader("Originalunterlagen / Leistungsbeschreibung")
            st.caption("Hier bekommst du die genaue Produkt-/Mengenanalyse, wenn TED selbst nur den Titel veröffentlicht.")
            uploaded=st.file_uploader("PDF-Leistungsbeschreibung hochladen",type=["pdf"],key="pdfdoc")
            extra=""
            if uploaded:
                extra=pdf_text(uploaded)
                if extra:
                    intel=product_intel(n,extra)
                    st.success(f"PDF gelesen · {len(extra):,} Zeichen".replace(",","."))
                    c1,c2=st.columns(2)
                    with c1:
                        st.markdown("**Erkannte Mengen**")
                        st.write("\n".join("• "+x for x in intel["quantities"]) if intel["quantities"] else "Keine eindeutige Menge erkannt")
                    with c2:
                        st.markdown("**Erkannte Artikel / Typen**")
                        st.write("\n".join("• "+x for x in intel["articles"]) if intel["articles"] else "Keine eindeutige Artikelnummer erkannt")
                    with st.expander("Extrahierter Dokumenttext"):st.text(extra[:30000])
            if st.button("TED-HTML automatisch einlesen",use_container_width=True):
                htmltxt,err=fetch_ted_html(pub)
                if err:st.warning("TED-HTML konnte nicht automatisch gelesen werden: "+err)
                else:
                    st.session_state["html_"+pub]=htmltxt;st.success("TED-HTML geladen")
            htmltxt=st.session_state.get("html_"+pub,"")
            if htmltxt:
                intel=product_intel(n,htmltxt)
                st.write("**Mengen aus TED-HTML:**",", ".join(intel["quantities"]) if intel["quantities"] else "keine erkannt")
                st.write("**Artikel/Typ aus TED-HTML:**",", ".join(intel["articles"]) if intel["articles"] else "keine erkannt")
        with tabs[2]:
            st.info("Der veröffentlichte Auftragswert ist kein garantierter Umsatz. Für Rahmenverträge kann er nur eine Obergrenze/Schätzung sein.")
            c1,c2,c3=st.columns(3);c1.metric("Theoretisches Volumen",money(val,cur));c2.metric("Max. Einkauf bei Zielmarge",money(max_buy,cur) if max_buy is not None else "—");c3.metric("Eigenkapital verfügbar",money(usable))
            if gap is not None:
                (st.success if gap<=0 else st.warning)("Aus Eigenkapital finanzierbar." if gap<=0 else f"Vorfinanzierungslücke: {money(gap,cur)}")
            st.markdown("**Score-Gründe**")
            for r in reasons:st.write("• "+r)
        with tabs[3]:
            intel=product_intel(n); q=urllib.parse.quote_plus((intel["articles"][0] if intel["articles"] else intel["title"])[:130])
            sources=[("Google B2B",f"https://www.google.com/search?q={q}+Gro%C3%9Fhandel+B2B+Deutschland"),("Unite/Mercateo",f"https://www.google.com/search?q=site%3Aunite.eu+{q}"),("RS",f"https://www.google.com/search?q=site%3Ade.rs-online.com+{q}"),("Conrad",f"https://www.google.com/search?q=site%3Aconrad.de+{q}"),("Farnell",f"https://www.google.com/search?q=site%3Ade.farnell.com+{q}"),("Distrelec",f"https://www.google.com/search?q=site%3Adistrelec.de+{q}"),("Würth",f"https://www.google.com/search?q=site%3Awuerth.de+{q}")]
            cs=st.columns(2)
            for i,(name,url) in enumerate(sources):
                with cs[i%2]:st.link_button("↗ "+name,url,use_container_width=True)
            st.caption("Das sind Recherchewege, keine bestätigten Preise. Einen Preis erst als echt behandeln, wenn ein Lieferantenangebot vorliegt.")
        with tabs[4]:
            intel=product_intel(n); low=intel["blob"].lower()
            checks=[("Aktuelle Veröffentlichung",publication_date(n) and publication_date(n)>=TODAY-timedelta(days=st.session_state.max_age)),("Offene Angebotsfrist",d is not None and d>=TODAY),("Lieferauftrag",nature(n)=="Lieferung"),("Keine regulierten Güter",not any(x in low for x in BLOCKED)),("Wert veröffentlicht",val is not None),("Menge bekannt",bool(intel["quantities"]))]
            for lab,ok in checks:st.write(("✅ " if ok else "⚠️ ")+lab)
        with tabs[5]:
            st.link_button("TED Original öffnen",f"https://ted.europa.eu/de/notice/-/detail/{pub}",use_container_width=True)
            links=n.get("links")
            if links:st.json(links,expanded=False)
        st.caption("*Kapitalbedarf ist nur ein Filter aus publiziertem Wert und Zielmarge, kein echter Einkaufspreis.")

elif st.session_state.nav=="Watchlist":
    st.markdown('<div class="hero"><h1>Watchlist</h1><p>Deine gespeicherten Chancen.</p></div>',unsafe_allow_html=True)
    with db() as con:rows=con.execute("SELECT pub,title,buyer,added,payload FROM watchlist ORDER BY added DESC").fetchall()
    if not rows:st.info("Noch nichts gespeichert.")
    for i,(_,_,_,_,payload) in enumerate(rows):card(json.loads(payload),3000+i)

elif st.session_state.nav=="Kalkulator":
    st.markdown('<div class="hero"><h1>Angebots-Kalkulator</h1><p>Mit echten Lieferantenpreisen kalkulieren.</p></div>',unsafe_allow_html=True)
    c1,c2=st.columns(2)
    with c1:buy=st.number_input("Wareneinkauf netto",0.0,1e9,1500.0,50.0);shipping=st.number_input("Fracht / Verpackung",0.0,1e9,80.0,10.0);other=st.number_input("Sonstige direkte Kosten",0.0,1e9,50.0,10.0)
    with c2:target=st.number_input("Ziel-DB % vom Verkauf",1.0,80.0,float(st.session_state.margin),1.0);vat=st.number_input("USt. %",0.0,30.0,19.0,1.0)
    cost=buy+shipping+other;sell=cost/(1-target/100);gross=sell-cost
    cs=st.columns(4);cs[0].metric("Kosten",money(cost));cs[1].metric("Mindestverkauf netto",money(sell));cs[2].metric("Deckungsbeitrag",money(gross));cs[3].metric("Brutto-Rechnung",money(sell*(1+vat/100)))

elif st.session_state.nav=="Einstellungen":
    st.markdown('<div class="hero"><h1>Einstellungen</h1><p>Dein Geschäftsprofil für den Deal-Score.</p></div>',unsafe_allow_html=True)
    st.session_state.capital=st.number_input("Geschäftskapital (€)",0.0,1e7,float(st.session_state.capital),100.0)
    st.session_state.reserve=st.number_input("Sicherheitsreserve (€)",0.0,float(st.session_state.capital),min(float(st.session_state.reserve),float(st.session_state.capital)),100.0)
    st.session_state.margin=st.slider("Zielmarge / Deckungsbeitrag (%)",5,60,int(st.session_state.margin),1)
    st.info(f"Für Wareneinkauf verfügbar: **{money(max(0,st.session_state.capital-st.session_state.reserve))}**")
    st.caption("Tender Scout Pro · "+APP_VERSION)
