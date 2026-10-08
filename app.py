import os
import re
import sqlite3
import zipfile
from datetime import datetime, date, timedelta
from flask import Flask, request, redirect, url_for, render_template_string, flash

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(APP_DIR, "market_fares.db")
UPLOAD_DIR = os.path.join(APP_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "skydays-market-fare-v9")

MONTHS = "JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC"
MONTH_NUM = {m:i for i,m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
AIRLINE_NAMES = {
    "IX":"Air India Express", "6E":"IndiGo", "AI":"Air India", "FZ":"flydubai",
    "SV":"Saudia", "XY":"flynas", "OV":"SalamAir", "J9":"Jazeera Airways",
    "QP":"Akasa Air", "3L":"Air Arabia Abu Dhabi", "G9":"Air Arabia", "WY":"Oman Air",
    "EK":"Emirates", "QR":"Qatar Airways", "EY":"Etihad Airways", "GF":"Gulf Air",
    "KU":"Kuwait Airways", "SG":"SpiceJet", "G8":"Go First", "AK":"AirAsia",
}
AIRLINE_CODES = set(AIRLINE_NAMES)
CITY_TO_IATA = {
    "CALICUT":"CCJ", "KOZHIKODE":"CCJ", "KOZHIKOD":"CCJ", "CCJ":"CCJ",
    "KOCHI":"COK", "COCHIN":"COK", "COK":"COK", "KANNUR":"CNN", "CNN":"CNN",
    "TRIVANDRUM":"TRV", "THIRUVANANTHAPURAM":"TRV", "TRV":"TRV",
    "MANGALORE":"IXE", "MANGALURU":"IXE", "IXE":"IXE", "RIYADH":"RUH", "RUH":"RUH",
    "JEDDAH":"JED", "JED":"JED", "DUBAI":"DXB", "DXB":"DXB", "SHARJAH":"SHJ", "SHJ":"SHJ",
    "ABU DHABI":"AUH", "ABUDHABI":"AUH", "AUH":"AUH", "MUSCAT":"MCT", "MCT":"MCT",
    "DOHA":"DOH", "DOH":"DOH", "KUWAIT":"KWI", "KWI":"KWI", "BAHRAIN":"BAH", "BAH":"BAH",
    "DAMMAM":"DMM", "DMM":"DMM", "ALAIN":"AAN", "AL AIN":"AAN", "AAN":"AAN",
    "RAS AL KHAIMAH":"RKT", "RKT":"RKT", "COLOMBO":"CMB", "CMB":"CMB", "JEDDAH":"JED",
}
AIRPORT_CODES = set(CITY_TO_IATA.values())


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = db()
    c.execute("""CREATE TABLE IF NOT EXISTS fares(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_group TEXT, sender TEXT, message_datetime TEXT, updated_time TEXT,
        sector TEXT, travel_dates TEXT, airline TEXT, flight TEXT, fare REAL,
        baggage TEXT, seats TEXT, conditions TEXT, original_message TEXT,
        created_at TEXT, fare_type TEXT DEFAULT '', timing TEXT DEFAULT '',
        b2b_discount REAL DEFAULT 0, airline_code TEXT DEFAULT '',
        infant_fare REAL, infant_baggage TEXT DEFAULT '', import_date TEXT DEFAULT ''
    )""")
    existing = {r[1] for r in c.execute("PRAGMA table_info(fares)").fetchall()}
    additions = {
        "updated_time":"TEXT", "fare_type":"TEXT DEFAULT ''", "timing":"TEXT DEFAULT ''",
        "b2b_discount":"REAL DEFAULT 0", "airline_code":"TEXT DEFAULT ''", "infant_fare":"REAL", "infant_baggage":"TEXT DEFAULT ''", "import_date":"TEXT DEFAULT ''"
    }
    for col, typ in additions.items():
        if col not in existing:
            c.execute(f"ALTER TABLE fares ADD COLUMN {col} {typ}")
    c.execute("""CREATE TABLE IF NOT EXISTS agencies(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL, starred INTEGER DEFAULT 0,
        deduction REAL DEFAULT 0, created_at TEXT
    )""")
    existing_a = {r[1] for r in c.execute("PRAGMA table_info(agencies)").fetchall()}
    if "deduction" not in existing_a:
        c.execute("ALTER TABLE agencies ADD COLUMN deduction REAL DEFAULT 0")
    c.commit(); c.close()


def clean_text(s):
    s = (s or "").replace("\u200e", "").replace("\u200f", "").replace("\ufeff", "")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    # OCR often splits month names: OC T, S EP, O CT.
    for mon in MONTH_NUM:
        s = re.sub(r"(?i)" + r"\s*".join(mon), mon, s)
    return s.strip()


def parse_whatsapp_export(text):
    text = clean_text(text)
    patterns = [
        re.compile(r"(?m)^\[(\d{1,2}/\d{1,2}/\d{2,4}),\s*([^\]]+)\]\s*([^:]+):\s?(.*)$"),
        re.compile(r"(?m)^(\d{1,2}/\d{1,2}/\d{2,4}),\s*([0-9:]+\s*(?:AM|PM)?)\s*-\s*([^:]+):\s?(.*)$", re.I),
    ]
    for pat in patterns:
        matches = list(pat.finditer(text))
        if matches:
            out=[]
            for i,m in enumerate(matches):
                end = matches[i+1].start() if i+1 < len(matches) else len(text)
                out.append({"date":m.group(1),"time":m.group(2).strip(),"sender":m.group(3).strip(),"body":(m.group(4)+text[m.end():end]).strip()})
            return out
    return [{"date":"","time":"","sender":"","body":text}] if text else []


def parse_datetime(date_text, time_text):
    if not date_text: return ""
    value=f"{date_text} {time_text}".strip()
    for fmt in ["%m/%d/%y %I:%M:%S %p","%m/%d/%y %I:%M %p","%m/%d/%Y %I:%M:%S %p","%m/%d/%Y %I:%M %p","%m/%d/%y %H:%M","%m/%d/%Y %H:%M"]:
        try: return datetime.strptime(value,fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError: pass
    return value


def normalize_place(value):
    v = re.sub(r"[^A-Z ]", "", (value or "").upper()).strip()
    v = re.sub(r"\s+", " ", v)
    return CITY_TO_IATA.get(v, v if re.fullmatch(r"[A-Z]{3}", v) else "")


def normalize_route(text):
    u = clean_text(text).upper()
    u = re.sub(r"[*_~`|]", " ", u)
    u = re.sub(r"\s+", " ", u).strip()
    # Named city route: CALICUT TO RIYADH, etc.
    m = re.search(r"([A-Z][A-Z ]{2,35}?)\s+(?:TO|T0)\s+([A-Z][A-Z ]{2,35}?)(?=\s*(?:\(|[-–—:=]|\b(?:IX|6E|J9|SV|XY|FZ|OV|3L|QP|G9|WY)\b|$))", u)
    if m:
        a,b=normalize_place(m.group(1)),normalize_place(m.group(2))
        if a and b:return f"{a}-{b}"
    # Explicit arrow/hyphen. Do not interpret a fare hyphen as a route unless
    # at least two valid airport/city parts exist.
    if re.search(r"→|➝|➡", u):
        parts=re.split(r"\s*(?:→|➝|➡)\s*",u); vals=[]
        for p in parts:
            n=normalize_place(re.split(r"[(:]",p,1)[0].strip())
            if n: vals.append(n); continue
            codes=[x for x in re.findall(r"\b[A-Z]{3}\b",p) if x not in MONTH_NUM]
            if codes: vals.append(codes[0])
        if len(vals)>=2:return "-".join(vals)
    # Santorian/Airguide style: CCJ - SHJ IX ... or COK KWI J9 ...
    before_date = re.split(r"\b(?:\d{1,2}\s*(?:TO|-|–)\s*\d{1,2}\s*[-/.]?\s*(?:"+MONTHS+r")|\d{1,2}\s*[-/.]?\s*(?:"+MONTHS+r")|(?:"+MONTHS+r")\s+\d{1,2})\b", u, maxsplit=1)[0]
    named=[]
    for city in sorted(CITY_TO_IATA,key=len,reverse=True):
        if re.search(r"\b"+re.escape(city)+r"\b",before_date):
            code=CITY_TO_IATA[city]
            if code not in named:named.append(code)
    if len(named)>=2:return "-".join(named[:5])
    codes=[x for x in re.findall(r"\b[A-Z]{3}\b",before_date) if x in AIRPORT_CODES]
    # Only accept airport codes; this prevents IX/J9/MEAL from becoming route legs.
    if len(codes)>=2:
        out=[]
        for x in codes:
            if x not in out:out.append(x)
        return "-".join(out[:5])
    return ""


def parse_date_tokens(text):
    u=clean_text(text).upper()
    u=re.sub(r"[\u2010-\u2015\u2212]","-",u)
    # Range: 15 TO 20 OCT / 15-20 OCT.
    m=re.search(r"\b(\d{1,2})\s*(?:TO|-)\s*(\d{1,2})\s*[-/.]?\s*("+MONTHS+r")\b",u)
    if m:
        a,b,mon=int(m.group(1)),int(m.group(2)),m.group(3)
        vals=[f"{d} {mon}" for d in range(a,b+1)] if a<=b and b-a<=31 else [f"{a} {mon}",f"{b} {mon}"]
        return vals,m.group(0)
    # Month first: OCT 12,14,15
    m=re.search(r"\b("+MONTHS+r")\s+([0-9]{1,2}(?:\s*[,/&]\s*[0-9]{1,2})*(?:\s+(?:AND\s+)?[0-9]{1,2})*)\b",u)
    if m:
        return [f"{int(x)} {m.group(1)}" for x in re.findall(r"\d{1,2}",m.group(2))],m.group(0)
    # Day first. Allows 14 OCT, 14-OCT, 14 OC T after clean_text.
    m=re.search(r"\b(\d{1,2})\s*[-/.]?\s*("+MONTHS+r")\b",u)
    if m:return [f"{int(m.group(1))} {m.group(2)}"],m.group(0)
    return [],""


def parse_money(text):
    u=clean_text(text)
    # Explicit currency first.
    m=re.search(r"(?:₹|INR|RS\.?)[\s]*([\d,]{4,7})(?:\s*/-)?",u,re.I)
    if m:return float(m.group(1).replace(",",""))
    # Fare after a travel date.
    m=re.search(r"\b\d{1,2}\s*[-/.]?\s*(?:"+MONTHS+r")\s*[-–—:=]?\s*([\d,]{4,6})\b",u,re.I)
    if m:return float(m.group(1).replace(",",""))
    # Range date fare: 15 TO 20 OCT - 19000.
    m=re.search(r"\b\d{1,2}\s*(?:TO|-)\s*\d{1,2}\s*(?:"+MONTHS+r")\s*[-:=]\s*([\d,]{4,6})\b",u,re.I)
    if m:return float(m.group(1).replace(",",""))
    # Separator fare. Must be 4-6 digits to avoid flight numbers.
    m=re.search(r"[-–—=:=]\s*([\d,]{4,6})(?:\s*/-)?(?=\s*(?:[-–—*]|[A-Za-z]|$))",u)
    if m:return float(m.group(1).replace(",",""))
    return None


def parse_airline_flight(text):
    u=re.sub(r"\s+"," ",clean_text(text).upper())
    codes="|".join(sorted(AIRLINE_CODES,key=len,reverse=True))
    # Do not take a date number as flight: J9 09 OCT must not become J9/09.
    m=re.search(r"\b("+codes+r")\s*[- ]?\s*(\d{2,5}(?:\s*(?:/|&|,)\s*(?:[A-Z0-9]{2,3}\s*)?\d{2,5})*)\b",u)
    if m:
        after=u[m.end():]
        if re.match(r"\s*(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\b",after):
            return m.group(1),""
        return m.group(1),re.sub(r"\s+","",m.group(2))
    m=re.search(r"\b("+codes+r")\b",u)
    return (m.group(1),"") if m else ("","")


def parse_suffix_flight(text):
    """Read a 2-5 digit flight number placed after a fare, e.g. 22500-351.
    Do not treat -02SEAT as a flight number."""
    u=clean_text(text).upper()
    m=re.search(r"(?:₹|INR|RS\.?|[-:=]\s*)[\d,]{4,6}\s*(?:/-)?\s*[-–—*]\s*(\d{3,5})(?=\s*(?:[*)]|$|\s+))",u)
    if m:return m.group(1)
    return ""


def parse_baggage(text):
    u=re.sub(r"\s+"," ",clean_text(text).upper())
    patterns=[
        r"\b\d{1,3}\s*\*\s*\d{1,3}\s*\+\s*\d{1,3}\s*KG(?:\s*\([^)]*PC[^)]*\))?",
        r"\b\d{1,3}\s*KG\s*\+\s*\d{1,3}\s*KG\b",
        r"\b\d{1,3}\s*\+\s*\d{1,3}\s*KG(?:\s*\+\s*STD\s*MEAL)?\b",
        r"\b\d{1,3}\s*\+\s*\d{1,3}\s*KG\s*\+?\s*STD\s*MEAL\b",
        r"\b\d{1,3}\s*(?:KG|KGS|BG)\b",
        r"\b\d{1,3}\s*PC(?:S)?\b",
    ]
    for p in patterns:
        m=re.search(p,u)
        if m:return re.sub(r"\s+"," ",m.group(0)).strip()
    return ""


def parse_timing(text):
    m=re.search(r"\b(\d{1,2}:\d{2})\s*[-–—]\s*(\d{1,2}:\d{2})\b",clean_text(text))
    return f"{m.group(1)}-{m.group(2)}" if m else ""


def parse_fare_type(text):
    u=re.sub(r"\s+"," ",clean_text(text).upper())
    if re.search(r"\bDEAL\s*FARE\b",u):return "DEAL FARE"
    if re.search(r"\bV?\s*SPECIAL\s*FARE\s*0?\b",u):return "SPECIAL FARE"
    if re.search(r"\bLOWEST\s*FARE\b",u):return "LOWEST FARE"
    if re.search(r"\bSPL\b",u):return "SPL"
    return ""


def parse_seats(text):
    m=re.search(r"(?:\b(\d+)\s*SEAT(?:S)?\b|\b(\d+)\s*💺|[-*](\d+)SEAT(?:S)?\b)",clean_text(text),re.I)
    return next((x for x in m.groups() if x),"") if m else ""


def parse_b2b_discount(text):
    u=clean_text(text).upper()
    m=re.search(r"(?:B2B\s*)?(\d{2,5})\s*(?:/-\s*)?LESS\b",u)
    if not m:m=re.search(r"B2B\s+(\d{2,5})\b",u)
    return float(m.group(1)) if m else 0


def parse_infant_fare(text):
    m=re.search(r"INFANT\s+FARE\s*[:=-]?\s*(?:₹|INR|RS\.?\s*)?([\d,]{3,6})",clean_text(text),re.I)
    return float(m.group(1).replace(",","")) if m else None


def parse_file(path):
    if path.lower().endswith(".zip"):
        out=[]
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if name.lower().endswith((".txt",".md")):
                    raw=z.read(name).decode("utf-8","replace")
                    out.extend(parse_whatsapp_export(raw))
        return out
    with open(path,"r",encoding="utf-8",errors="replace") as f:return parse_whatsapp_export(f.read())


def extract_fare_records(msg):
    body=clean_text(msg.get("body",""))
    lines=[x.strip() for x in body.splitlines() if x.strip()]
    global_b2b=parse_b2b_discount(body)
    infant=parse_infant_fare(body)
    global_conditions=[]
    ub=body.upper()
    for tag in ("NON-REFUNDABLE","SUBJECT TO AVAILABILITY"):
        if tag in ub:global_conditions.append(tag)
    ctx={"sector":"","airline_code":"","flight":"","baggage":"","timing":"","fare_type":"","b2b":global_b2b,"conditions":list(global_conditions),"infant_baggage":""}
    records=[]

    def emit(line, dates):
        if not dates or not ctx["sector"]:return
        line_code,line_flight=parse_airline_flight(line)
        suffix_flight=parse_suffix_flight(line)
        code=line_code or ctx["airline_code"]
        flight=line_flight or suffix_flight or ctx["flight"]
        # A line containing only J9 + date has no new flight; keep header flight.
        if line_code and not line_flight: code=ctx["airline_code"] or line_code
        fare=parse_money(line)
        baggage=parse_baggage(line) or ctx["baggage"]
        timing=parse_timing(line) or ctx["timing"]
        ftype=parse_fare_type(line) or ctx["fare_type"]
        seats=parse_seats(line)
        b2b=parse_b2b_discount(line) or ctx["b2b"]
        conditions=list(ctx["conditions"])
        for tag in ("MEAL","MRNG","EVNG"):
            if re.search(r"\b"+tag+r"\b",line.upper()) and tag not in conditions:conditions.append(tag)
        if infant is not None and f"INFANT FARE ₹{infant:,.0f}" not in conditions: conditions.append(f"INFANT FARE ₹{infant:,.0f}")
        if ctx.get("infant_baggage") and f"INFANT BAGGAGE {ctx["infant_baggage"]}" not in conditions: conditions.append(f"INFANT BAGGAGE {ctx["infant_baggage"]}")
        for d in dates:
            records.append({
                "sector":ctx["sector"],"travel_dates":d,"airline":AIRLINE_NAMES.get(code,code),"airline_code":code,
                "flight":flight,"fare":fare,"baggage":baggage,"seats":seats,"conditions":", ".join(conditions),
                "fare_type":ftype,"timing":timing,"b2b_discount":b2b,"infant_fare":infant,"infant_baggage":ctx.get("infant_baggage",""),
                "original_message":body
            })

    for raw in lines:
        line=re.sub(r"[*_~`|]"," ",raw)
        u=re.sub(r"\s+"," ",line.upper()).strip()
        if not u or u.startswith(("HTTP://","HTTPS://")):continue
        if re.search(r"@(?:ALHINDONLINE|TRAVELWALLET|FLYUNITEDTRIP)",u):continue
        route=normalize_route(line)
        dates=parse_date_tokens(line)[0]
        code,flight=parse_airline_flight(line)
        bg=parse_baggage(line); tm=parse_timing(line); ft=parse_fare_type(line); bd=parse_b2b_discount(line)
        if route:
            ctx["sector"]=route
            # A route line may contain airline, flight and baggage together.
            if code:ctx["airline_code"]=code
            if flight:ctx["flight"]=flight
            if bg and "CHECK IN BAGGAGE" in u and "INFANT" not in u:
                ctx["infant_baggage"]=bg
            elif bg and "INFANT" not in u:ctx["baggage"]=bg
            if tm:ctx["timing"]=tm
            if ft:ctx["fare_type"]=ft
            if bd:ctx["b2b"]=bd
            if dates:emit(line,dates)
            continue
        if code:
            ctx["airline_code"]=code
            if flight:ctx["flight"]=flight
        if bg and "CHECK IN BAGGAGE" in u and "INFANT" not in u:
            ctx["infant_baggage"]=bg
        elif bg and "INFANT" not in u:ctx["baggage"]=bg
        if tm:ctx["timing"]=tm
        if ft:ctx["fare_type"]=ft
        if bd:ctx["b2b"]=bd
        for tag in ("NON-REFUNDABLE","SUBJECT TO AVAILABILITY"):
            if tag in u and tag not in ctx["conditions"]:ctx["conditions"].append(tag)
        if dates and ctx["sector"]:emit(line,dates)
    return records


def is_fare_message(body):
    u=clean_text(body)
    return bool(normalize_route(u) and parse_date_tokens(u)[0] and (
        parse_money(u) is not None or parse_fare_type(u) or re.search(r"\b(?:DEAL|SPECIAL|LOWEST)\s+FARE\b|\bSPL\b",u,re.I)
    ))


def records_from_messages(messages):
    out=[]
    for msg in messages:
        body=msg.get("body","")
        # Do not discard an entire message because the top-level body does not
        # contain the same date/route expression; extract_fare_records handles blocks.
        recs=extract_fare_records(msg)
        dt=parse_datetime(msg.get("date",""),msg.get("time",""))
        for rec in recs:
            rec["sender"]=msg.get("sender",""); rec["message_datetime"]=dt; out.append(rec)
    return out


def save_records(records, agency, updated_time, filename=""):
    c=db(); source=agency.strip() if agency.strip() else filename
    import_day=date.today().isoformat(); count=0
    for x in records:
        if not source:source="Unspecified"
        c.execute("""INSERT INTO fares(source_group,sender,message_datetime,updated_time,sector,travel_dates,airline,flight,fare,baggage,seats,conditions,original_message,created_at,fare_type,timing,b2b_discount,airline_code,infant_fare,infant_baggage,import_date)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (source,x.get("sender",""),x.get("message_datetime",""),updated_time,x.get("sector",""),x.get("travel_dates",""),
                   x.get("airline",""),x.get("flight",""),x.get("fare"),x.get("baggage",""),x.get("seats",""),x.get("conditions",""),
                   x.get("original_message",""),datetime.now().strftime("%Y-%m-%d %H:%M:%S"),x.get("fare_type",""),x.get("timing",""),
                   x.get("b2b_discount",0),x.get("airline_code",""),x.get("infant_fare"),x.get("infant_baggage",""),import_day))
        c.execute("INSERT OR IGNORE INTO agencies(name,starred,deduction,created_at) VALUES(?,?,?,?)",(source,0,0,datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        count+=1
    c.commit();c.close();return count


def comparison_fare(row, agency_row=None):
    if row is None or row["fare"] is None:return None
    deduction=float(agency_row["deduction"] or 0) if agency_row else 0
    b2b=float(row["b2b_discount"] or 0)
    return max(0,float(row["fare"])-deduction-b2b)


def date_sort_key(s):
    m=re.match(r"(\d{1,2})\s+([A-Z]{3})$",(s or "").upper())
    if not m:return (99,99)
    return (MONTH_NUM.get(m.group(2),99),int(m.group(1)))


def next_8_dates():
    today=date.today(); return [today+timedelta(days=i) for i in range(8)]


def display_date(d):return d.strftime("%d %b").upper()


def agency_map(c):
    return {r["name"]:r for r in c.execute("SELECT * FROM agencies ORDER BY starred DESC,name").fetchall()}


BASE=r"""
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SkyDays Market Fare Intelligence</title>
<style>
*{box-sizing:border-box}body{font-family:Arial,sans-serif;background:#f4f7fb;margin:0;color:#172334}header{background:#102d48;color:#fff;padding:14px 22px}.wrap{max-width:1500px;margin:auto;padding:16px}.topbar{display:flex;align-items:center;gap:14px}.brand{font-size:20px;font-weight:800;white-space:nowrap}.nav{display:flex;gap:7px;flex-wrap:wrap;margin-left:10px}.nav a{color:#fff;text-decoration:none;padding:8px 11px;border-radius:7px;background:#214766;font-size:13px}.top-search{margin-left:auto;width:250px;max-width:38vw;padding:9px 12px;border-radius:8px;border:1px solid #5d7790;background:#fff;color:#172334}.card{background:#fff;border-radius:12px;padding:15px;margin-bottom:13px;box-shadow:0 2px 10px #0001}h2,h3{margin:0 0 9px}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:11px}.kpi{padding:13px;border-radius:10px;background:#edf3ff}.kpi b{font-size:21px}input,select,button,textarea{font:inherit;padding:9px;border:1px solid #cbd5df;border-radius:7px}button{background:#1261a0;color:#fff;border:0;cursor:pointer}button.secondary{background:#64748b}button.danger{background:#b42318}button.star{background:#f4f7fb;color:#172334;border:1px solid #ccd6e0}.flex{display:flex;gap:8px;align-items:center;flex-wrap:wrap}table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:8px;border-bottom:1px solid #e5e9ee;text-align:left;vertical-align:top}th{background:#eef2f6}.scroll{overflow:auto;max-height:650px}.low{font-weight:800;color:#087443}.muted{color:#687586;font-size:12px}.pill{display:inline-block;padding:4px 7px;border-radius:20px;background:#edf3ff;margin:2px;font-size:12px}.agency-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}.agency{padding:10px;border:1px solid #d9e1e8;border-radius:8px;background:#fff}.dropzone{border:2px dashed #9db2c7;border-radius:10px;padding:18px;text-align:center;background:#f9fbfd}.compare-wrap{overflow:auto;border:1px solid #e2e8f0;border-radius:10px;margin-top:12px}.compare-table{min-width:820px}.compare-table th,.compare-table td{border-right:1px solid #e7ebf0}.compare-table .date-col{position:sticky;left:0;background:#f8fafc;z-index:2;min-width:95px}.agency-head{min-width:150px;text-align:center}.fare-cell{text-align:center;min-width:150px;background:#fff}.fare-cell.best-fare{background:#e9f8ef;box-shadow:inset 0 0 0 2px #0b7a45}.fare-big{font-size:19px;font-weight:800;color:#0d5f38}.fare-special{font-weight:800;color:#7c3aed}.fare-meta{font-size:11px;color:#6b7280;margin-top:3px}.fare-cell.empty{color:#a0a9b4}.check{display:inline-block;padding:5px 8px;border-radius:6px;background:#fff3cd;color:#7a4b00;font-weight:800;font-size:11px;text-decoration:none}.market-low{display:inline-block;padding:5px 8px;border-radius:6px;background:#e7f7ed;color:#087443;font-weight:800;font-size:11px}.option{margin-top:5px;font-size:11px}.agency-select{min-width:190px;max-width:320px}.import-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}textarea{width:100%;min-height:190px}.notice{padding:10px;border-radius:8px;background:#edf7ee}.sector-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:14px}.sector-card{display:flex;min-height:132px;align-items:center;justify-content:center;text-decoration:none;color:#172334;background:linear-gradient(145deg,#fff,#f7fafc);border:1px solid #dbe4ec;border-radius:16px;box-shadow:0 4px 14px #17324a10;transition:.16s}.sector-card:hover{transform:translateY(-2px);box-shadow:0 8px 22px #17324a18}.sector-main{text-align:center}.sector-route{font-size:31px;font-weight:800;letter-spacing:.5px;white-space:nowrap}.sector-arrow{display:inline-block;margin:0 7px;font-weight:500;color:#597087}.sector-airlines{margin-top:12px;color:#718096;font-size:11px;font-weight:700;letter-spacing:1.4px;text-transform:uppercase}.sector-search{width:300px;max-width:100%}.toolbar{display:flex;justify-content:space-between;gap:10px;align-items:center;flex-wrap:wrap}.small{font-size:11px}.form-grid{display:grid;grid-template-columns:1.2fr .8fr .6fr auto;gap:8px;align-items:end}@media(max-width:1000px){.grid{grid-template-columns:repeat(2,1fr)}.agency-grid,.import-grid{grid-template-columns:1fr 1fr}.form-grid{grid-template-columns:1fr 1fr}}@media(max-width:650px){.grid,.agency-grid,.import-grid,.form-grid{grid-template-columns:1fr}.wrap{padding:10px}.topbar{align-items:flex-start;flex-wrap:wrap}.top-search{order:3;width:100%;max-width:none;margin-left:0}.nav{margin-left:0}.sector-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.sector-card{min-height:105px}.sector-route{font-size:24px}.sector-arrow{margin:0 4px}.sector-airlines{font-size:10px}}
</style></head><body><header><div class="wrap" style="padding-top:0;padding-bottom:0"><div class="topbar"><div class="brand">SkyDays Market Fare Intelligence</div><nav class="nav"><a href="/">Sectors</a><a href="/agencies">Agency Master</a><a href="/import">Import</a><a href="/daily">Daily Data</a></nav><form method="get" action="/search" style="margin-left:auto"><input class="top-search" name="q" placeholder="Search destination / sector e.g. DXB" value="{{search_q|default('')}}"></form></div></div></header><div class="wrap">{% with messages=get_flashed_messages() %}{% for m in messages %}<div class="card notice">{{m}}</div>{% endfor %}{% endwith %}{{content|safe}}</div></body></html>
"""


def render_base(content, **ctx):return render_template_string(BASE,content=content,**ctx)


@app.route("/")
def home():
    c=db(); rows=c.execute("SELECT sector,COUNT(*) n FROM fares WHERE sector<>'' GROUP BY sector ORDER BY sector").fetchall(); total=c.execute("SELECT COUNT(*) FROM fares").fetchone()[0]
    cards=[]
    for r in rows:
        codes=c.execute("SELECT DISTINCT airline_code FROM fares WHERE sector=? AND airline_code<>'' ORDER BY airline_code",(r["sector"],)).fetchall()
        cards.append({"sector":r["sector"],"airlines":[x["airline_code"] for x in codes][:6]})
    c.close()
    content=render_template_string(r"""
<div class="toolbar card"><div><h3>Market Inventory</h3><div class="muted">{{total}} records · select a sector to compare the next 8 travel dates</div></div><a href="/import"><button>+ Import</button></a></div>
<div class="card"><div class="flex"><input id="sectorSearch" class="sector-search" placeholder="Search sector or destination e.g. DXB / CCJ" autocomplete="off"><span class="muted">{{cards|length}} sectors</span></div></div>
<div id="sectorGrid" class="sector-grid">{% for s in cards %}<a class="sector-card" data-sector="{{s.sector|replace('-','')|upper}}" href="/sector/{{s.sector|urlencode}}"><div class="sector-main">{% set p=s.sector.split('-') %}<div class="sector-route">{{p[0]}} <span class="sector-arrow">→</span> {{p[-1]}}</div><div class="sector-airlines">{{' · '.join(s.airlines)}}</div></div></a>{% endfor %}</div>
{% if not cards %}<div class="sector-empty">No market sectors yet. Import your first WhatsApp fare update.</div>{% endif %}
<script>const q=document.getElementById('sectorSearch'),g=document.getElementById('sectorGrid');q&&q.addEventListener('input',()=>{const v=q.value.replace(/[^A-Za-z0-9]/g,'').toUpperCase();[...g.querySelectorAll('.sector-card')].forEach(x=>x.style.display=(!v||x.dataset.sector.includes(v))?'flex':'none')});</script>
""",cards=cards,total=total)
    return render_base(content)


@app.route("/search")
def search():
    q=clean_text(request.args.get("q","")); norm=q.upper().replace(" ","")
    c=db(); sectors=c.execute("SELECT DISTINCT sector FROM fares WHERE sector<>'' ORDER BY sector").fetchall(); c.close()
    hits=[]
    for r in sectors:
        s=r["sector"].upper().replace("-","")
        if norm and (norm in s or any(part==norm for part in r["sector"].split("-"))):hits.append(r["sector"])
    content=render_template_string(r"""<div class="card"><h3>Search results</h3><div class="muted">Search: {{q}}</div></div><div class="sector-grid">{% for s in hits %}<a class="sector-card" href="/sector/{{s|urlencode}}"><div class="sector-main"><div class="sector-route">{{s.split('-')[0]}} <span class="sector-arrow">→</span> {{s.split('-')[-1]}}</div></div></a>{% else %}<div class="sector-empty">No matching sector.</div>{% endfor %}</div>""",q=q,hits=hits)
    return render_base(content,search_q=q)


@app.route("/sector/<path:sector>")
def sector_detail(sector):
    try:
        skyfare=float(request.args.get("skyfare", "")) if request.args.get("skyfare", "").strip() else None
    except ValueError:
        skyfare=None
    c=db(); agencies=list(c.execute("SELECT * FROM agencies ORDER BY starred DESC,name").fetchall())
    amap={a["name"]:a for a in agencies}
    starred_names=[a["name"] for a in agencies if a["starred"]]
    selected=[x for x in request.args.getlist("agency") if x in amap]
    if selected: compare_names=selected
    else: compare_names=starred_names
    # If there are no starred agencies yet, show available agencies rather than a blank comparison.
    if not compare_names: compare_names=[a["name"] for a in agencies]
    days=next_8_dates()
    day_labels=[display_date(d) for d in days]
    rows=c.execute("SELECT * FROM fares WHERE sector=? ORDER BY travel_dates, CASE WHEN fare IS NULL THEN 1 ELSE 0 END, fare, id",(sector,)).fetchall()
    all_by_day={label:[] for label in day_labels}
    for r in rows:
        if r["travel_dates"].upper() in all_by_day:all_by_day[r["travel_dates"].upper()].append(r)
    comparison=[]
    for label in day_labels:
        cells=[]; selected_low=[]; all_low=[]
        for name in compare_names:
            opts=[r for r in all_by_day[label] if r["source_group"]==name]
            opts=sorted(opts,key=lambda r:(comparison_fare(r,amap[name]) is None, comparison_fare(r,amap[name]) or 10**9,r["id"]))
            best=opts[0] if opts else None
            cells.append({"name":name,"row":best,"options":opts})
            if best and comparison_fare(best,amap[name]) is not None:selected_low.append(comparison_fare(best,amap[name]))
        for r in all_by_day[label]:
            cf=comparison_fare(r,amap.get(r["source_group"]))
            if cf is not None:all_low.append((cf,r))
        lowest=min(selected_low) if selected_low else None
        market_low=min(all_low,key=lambda x:x[0]) if all_low else None
        comparison.append({"date":label,"cells":cells,"lowest":lowest,"market_low":market_low})
    overall=[d["lowest"] for d in comparison if d["lowest"] is not None]
    c.close()
    content=render_template_string(r"""
<div class="card"><div class="toolbar"><div><h3>{{sector.replace('-', ' → ')}}</h3><div class="muted">Today + next 7 days · starred agencies first · adjusted comparison fares</div></div><div class="flex"><form method="get" class="flex"><input name="skyfare" type="number" min="0" step="1" placeholder="SkyDays fare ₹" value="{{skyfare if skyfare is not none else ''}}"><button type="submit">Compare SkyDays</button></form><a href="/"><button type="button" class="secondary">← Back</button></a><a href="/import"><button type="button">Import</button></a></div></div></div>
<div class="grid"><div class="kpi">Lowest selected fare<br><b>{{fmt(lowest)}}</b></div><div class="kpi">Agencies shown<br><b>{{compare_names|length}}</b></div><div class="kpi">Dates checked<br><b>8</b></div><div class="kpi">Reference<br><b>Adjusted</b></div></div>
<div class="card"><div class="toolbar"><h3>Market comparison</h3><form method="get" class="flex"><select name="agency" multiple size="1" class="agency-select" title="Choose agencies"><option disabled>Selected agencies</option>{% for a in agencies %}<option value="{{a.name}}" {% if a.name in compare_names %}selected{% endif %}>{{'★ ' if a.starred else ''}}{{a.name}}{% if a.deduction %} (−₹{{'{:,.0f}'.format(a.deduction)}}){% endif %}</option>{% endfor %}</select><button>Compare</button><a href="/sector/{{sector|urlencode}}"><button type="button" class="secondary">Starred</button></a></form></div>
<div class="muted">Original marketed fares are preserved. Comparison = marketed fare − agency deduction − B2B discount.</div>
<div class="compare-wrap"><table class="compare-table"><thead><tr><th class="date-col">Travel date</th>{% for n in compare_names %}<th class="agency-head">{{n}}{% if amap[n].deduction %}<div class="muted">− ₹{{'{:,.0f}'.format(amap[n].deduction)}}</div>{% endif %}</th>{% endfor %}<th class="agency-head">Market check</th></tr></thead><tbody>
{% for d in comparison %}<tr><th class="date-col">{{d.date}}</th>{% for cell in d.cells %}<td class="fare-cell">{% if cell.row %}{% set cf=cmp(cell.row,amap[cell.name]) %}{% if cf is not none %}<div class="fare-big">₹{{'{:,.0f}'.format(cf)}}</div>{% else %}<div class="fare-special">{{cell.row.fare_type or '—'}}</div>{% endif %}<div class="fare-meta">{{cell.row.airline_code or cell.row.airline}}{% if cell.row.flight %} · {{cell.row.flight}}{% endif %}</div>{% if cell.row.fare is not none and cf is not none and cell.row.fare != cf %}<div class="fare-meta">Marketed ₹{{'{:,.0f}'.format(cell.row.fare)}}</div>{% endif %}{% if skyfare is not none and cf is not none %}{% set diff=cf-skyfare %}<div class="fare-meta">{% if diff < 0 %}₹{{'{:,.0f}'.format(-diff)}} lower{% elif diff > 0 %}₹{{'{:,.0f}'.format(diff)}} higher{% else %}Same as SkyDays{% endif %}</div>{% endif %}{% if cell.row.baggage %}<div class="fare-meta">{{cell.row.baggage}}</div>{% endif %}{% if cell.options|length>1 %}<div class="option">{{cell.options|length}} options</div>{% endif %}{% if cell.row.seats %}<div class="fare-meta">{{cell.row.seats}} seat{% if cell.row.seats|int != 1 %}s{% endif %}</div>{% endif %}{% else %}<div class="fare-meta">—</div>{% endif %}</td>{% endfor %}<td class="fare-cell">{% if d.market_low %}{% set mn=d.market_low[1] %}{% if d.lowest is not none and d.market_low[0] < d.lowest %}<a class="check" href="/sector/{{sector|urlencode}}?agency={{mn.source_group|urlencode}}{% if skyfare is not none %}&skyfare={{skyfare}}{% endif %}#market-records">CHECK · {{mn.source_group}}</a><div class="fare-meta">₹{{'{:,.0f}'.format(d.market_low[0])}}</div>{% else %}<span class="market-low">MARKET LOWEST</span>{% endif %}{% else %}—{% endif %}</td></tr>{% endfor %}</tbody></table></div></div>
<div class="card"><details id="market-records"><summary><b>All underlying market records</b> <span class="muted">({{rows|length}} records in this sector)</span></summary><div class="scroll"><table><tr><th>Agency</th><th>Travel date</th><th>Airline</th><th>Flight</th><th>Baggage</th><th>Timing</th><th>Marketed</th><th>Adjusted</th><th>Type</th><th>B2B</th><th>Seats</th><th>Infant</th></tr>{% for r in rows %}<tr><td><b>{{r.source_group}}</b></td><td>{{r.travel_dates}}</td><td>{{r.airline_code or r.airline}}</td><td>{{r.flight or '—'}}</td><td>{{r.baggage or '—'}}</td><td>{{r.timing or '—'}}</td><td>{{fmt(r.fare)}}</td><td>{{fmt(cmp(r,amap.get(r.source_group)))}}</td><td>{{r.fare_type or '—'}}</td><td>{{fmt(r.b2b_discount) if r.b2b_discount else '—'}}</td><td>{{r.seats or '—'}}</td><td>{% if r.infant_fare %}₹{{'{:,.0f}'.format(r.infant_fare)}}{% if r.infant_baggage %} · {{r.infant_baggage}}{% endif %}{% else %}—{% endif %}</td></tr>{% endfor %}</table></div></details></div>
""",sector=sector,agencies=agencies,amap=amap,compare_names=compare_names,comparison=comparison,rows=rows,lowest=min(overall) if overall else None,skyfare=skyfare,fmt=lambda v:"—" if v is None else f"₹{v:,.0f}",cmp=comparison_fare)
    return render_base(content)


@app.route("/agencies")
def agencies_page():
    c=db(); agencies=c.execute("SELECT * FROM agencies ORDER BY starred DESC,name").fetchall(); c.close()
    content=render_template_string(r"""
<div class="card"><h3>Agency Master</h3><div class="muted">Maintain agency names, comparison deductions and starred status. These settings do not alter the original marketed fare.</div></div>
<div class="card"><form method="post" action="/agency/save" class="form-grid"><div><label class="small">Agency name</label><input name="name" placeholder="Agency name" required></div><div><label class="small">Deduction ₹</label><input name="deduction" type="number" min="0" step="1" value="0"></div><div><label class="small">Star</label><select name="starred"><option value="0">☆ No</option><option value="1">★ Yes</option></select></div><button>Save agency</button></form></div>
<div class="card"><table><tr><th>Agency</th><th>Star</th><th>Deduction</th><th>Action</th></tr>{% for a in agencies %}<tr><td><b>{{a.name}}</b></td><td>{{'★ Starred' if a.starred else '☆'}}</td><td>₹{{'{:,.0f}'.format(a.deduction or 0)}}</td><td><form method="post" action="/agency/save" class="flex"><input type="hidden" name="name" value="{{a.name}}"><input name="deduction" type="number" min="0" step="1" value="{{a.deduction or 0}}" style="width:100px"><select name="starred"><option value="0" {% if not a.starred %}selected{% endif %}>☆</option><option value="1" {% if a.starred %}selected{% endif %}>★</option></select><button>Update</button></form></td></tr>{% else %}<tr><td colspan="4">No agencies yet. Agencies are added automatically during import.</td></tr>{% endfor %}</table></div>
""",agencies=agencies)
    return render_base(content)


@app.post("/agency/save")
def agency_save():
    name=clean_text(request.form.get("name",""));
    if not name:flash("Agency name is required.");return redirect(url_for("agencies_page"))
    try:ded=float(request.form.get("deduction") or 0)
    except ValueError:ded=0
    starred=1 if request.form.get("starred")=="1" else 0
    c=db();c.execute("INSERT INTO agencies(name,starred,deduction,created_at) VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET starred=excluded.starred,deduction=excluded.deduction",(name,starred,max(0,ded),datetime.now().isoformat(timespec="seconds")));c.commit();c.close();flash(f"Agency Master updated: {name}");return redirect(url_for("agencies_page"))


@app.route("/import")
def import_page():
    c=db(); agencies=c.execute("SELECT * FROM agencies ORDER BY starred DESC,name").fetchall(); c.close()
    content=render_template_string(r"""
<div class="card"><h3>Import Market Fare Data</h3><div class="muted">Select the source agency from Agency Master. Updated Time is optional and never blocks import. Numeric fare is left blank when the message only says DEAL/SPECIAL/SPL/LOWEST.</div></div>
<div class="import-grid"><div class="card"><form action="/import_file" method="post" enctype="multipart/form-data"><div class="dropzone" id="dropzone"><strong>Drag & Drop WhatsApp TXT / ZIP</strong><br><span class="muted">or choose a file</span><br><br><input id="fileInput" type="file" name="file" accept=".txt,.zip,.md" required></div><br><select name="agency" required><option value="">Select agency</option>{% for a in agencies %}<option value="{{a.name}}">{{'★ ' if a.starred else ''}}{{a.name}}{% if a.deduction %} · −₹{{'{:,.0f}'.format(a.deduction)}}{% endif %}</option>{% endfor %}</select><br><br><input name="updated_time" placeholder="Updated Time (optional)"><br><br><button>Import File</button></form></div><div class="card"><form action="/import_paste" method="post"><textarea name="text" placeholder="Paste WhatsApp marketing text here..."></textarea><br><br><div class="flex"><select name="agency" required><option value="">Select agency</option>{% for a in agencies %}<option value="{{a.name}}">{{'★ ' if a.starred else ''}}{{a.name}}</option>{% endfor %}</select><input name="updated_time" placeholder="Updated Time (optional)"><button>Import Pasted Text</button></div></form></div></div>
<script>const dz=document.getElementById('dropzone'),fi=document.getElementById('fileInput');if(dz){['dragenter','dragover'].forEach(e=>dz.addEventListener(e,x=>{x.preventDefault();dz.classList.add('drag')}));['dragleave','drop'].forEach(e=>dz.addEventListener(e,x=>{x.preventDefault();dz.classList.remove('drag')}));dz.addEventListener('drop',e=>{if(e.dataTransfer.files.length)fi.files=e.dataTransfer.files})}</script>
""",agencies=agencies)
    return render_base(content)


@app.post("/import_file")
def import_file():
    f=request.files.get("file");agency=clean_text(request.form.get("agency",""));updated=clean_text(request.form.get("updated_time",""))
    if not f or not f.filename:flash("Please select a file.");return redirect(url_for("import_page"))
    if not agency:flash("Select an agency from Agency Master first.");return redirect(url_for("import_page"))
    safe=re.sub(r"[^A-Za-z0-9_.-]","_",f.filename);path=os.path.join(UPLOAD_DIR,safe);f.save(path)
    try:
        records=records_from_messages(parse_file(path));count=save_records(records,agency,updated,os.path.splitext(safe)[0]);flash(f"Imported {count} market records for {agency}. Updated Time: {updated or 'not entered'}")
    except Exception as e:flash("Import error: "+str(e))
    return redirect(url_for("home"))


@app.post("/import_paste")
def import_paste():
    text=request.form.get("text","");agency=clean_text(request.form.get("agency",""));updated=clean_text(request.form.get("updated_time",""))
    if not text.strip():flash("Paste WhatsApp marketing text first.");return redirect(url_for("import_page"))
    if not agency:flash("Select an agency from Agency Master first.");return redirect(url_for("import_page"))
    try:
        records=records_from_messages(parse_whatsapp_export(text));count=save_records(records,agency,updated,agency);flash(f"Imported {count} market records for {agency}.")
    except Exception as e:flash("Paste import error: "+str(e))
    return redirect(url_for("home"))


@app.route("/daily")
def daily_page():
    default=(date.today()-timedelta(days=1)).isoformat(); day=request.args.get("day",default)
    c=db(); count=c.execute("SELECT COUNT(*) FROM fares WHERE import_date=?",(day,)).fetchone()[0]; agencies=c.execute("SELECT COUNT(*) FROM agencies").fetchone()[0]; c.close()
    content=render_template_string(r"""
<div class="card"><h3>Daily Market Data</h3><div class="muted">Clear market fare records by <b>market-data/import date</b>, not travel date. Agency Master, stars and deductions are never deleted.</div></div>
<div class="card"><form method="get" class="flex"><label>Import date</label><input type="date" name="day" value="{{day}}"><button>Check</button></form><br><div class="kpi">Records imported on {{day}}: <b>{{count}}</b></div></div>
{% if count %}<div class="card"><form method="post" action="/daily/clear" onsubmit="return confirm('Clear all market fare records imported on {{day}}? Agency Master will not be affected.');"><input type="hidden" name="day" value="{{day}}"><button class="danger">Clear {{count}} market records for {{day}}</button></form></div>{% else %}<div class="card notice">No market records found for this import date.</div>{% endif %}
<div class="muted">Agency Master entries currently retained: {{agencies}}</div>
""",day=day,count=count,agencies=agencies)
    return render_base(content)


@app.post("/daily/clear")
def daily_clear():
    day=request.form.get("day","")
    c=db();cur=c.execute("DELETE FROM fares WHERE import_date=?",(day,));deleted=cur.rowcount;c.commit();c.close();flash(f"Cleared {deleted} market records imported on {day}. Agency Master was kept unchanged.");return redirect(url_for("daily_page",day=day))


init_db()

if __name__ == "__main__":
    port=int(os.environ.get("PORT",8765))
    app.run(host="0.0.0.0",port=port,debug=False)
