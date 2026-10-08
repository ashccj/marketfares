import os
import re
import sqlite3
import zipfile
import json
import hashlib
import secrets
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from flask import Flask, request, redirect, url_for, render_template_string, flash, send_file, session

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(APP_DIR, "market_fares.db")
UPLOAD_DIR = os.path.join(APP_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = "skydays-market-fare-v7"

MONTH_RE = r"(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)"
AIRLINE_CODES = {"IX","6E","6E","AI","AK","G8","SG","UK","EK","FZ","OV","J9","QP","SV","XY","3L","WY","GF","QR","KU","EY","WY"}
CITY_TO_IATA = {
    "CALICUT":"CCJ", "KOZHIKODE":"CCJ", "KOZHIKOD":"CCJ", "CCJ":"CCJ",
    "KOCHI":"COK", "COCHIN":"COK", "COK":"COK", "KANNUR":"CNN", "CNN":"CNN",
    "TRIVANDRUM":"TRV", "THIRUVANANTHAPURAM":"TRV", "TRV":"TRV", "MANGALORE":"IXE", "MANGALURU":"IXE", "IXE":"IXE",
    "RIYADH":"RUH", "RUH":"RUH", "JEDDAH":"JED", "JED":"JED", "DUBAI":"DXB", "DXB":"DXB",
    "SHARJAH":"SHJ", "SHJ":"SHJ", "ABU DHABI":"AUH", "ABUDHABI":"AUH", "ABU":"AUH", "AUH":"AUH",
    "MUSCAT":"MCT", "MCT":"MCT", "DOHA":"DOH", "DOH":"DOH", "KUWAIT":"KWI", "KWI":"KWI",
    "BAHRAIN":"BAH", "BAH":"BAH", "DAMMAM":"DMM", "DMM":"DMM", "ALAIN":"AAN", "AL AIN":"AAN", "AAN":"AAN",
    "RAS AL KHAIMAH":"RKT", "RKT":"RKT", "SRI LANKA":"CMB", "COLOMBO":"CMB", "CMB":"CMB", "BAHRAIN":"BAH",
}
AIRLINE_NAMES = {
    "IX":"Air India Express", "6E":"IndiGo", "FZ":"flydubai", "SV":"Saudia", "XY":"flynas",
    "OV":"SalamAir", "J9":"Jazeera Airways", "QP":"Akasa Air", "3L":"Air Arabia Abu Dhabi",
    "WY":"Oman Air", "EK":"Emirates", "QR":"Qatar Airways", "EY":"Etihad Airways", "GF":"Gulf Air",
}


def _using_postgres():
    return bool(os.getenv("DATABASE_URL", "").strip())


class DBConn:
    """Small compatibility wrapper so the existing SQLite-style queries also work on Postgres."""
    def __init__(self):
        self.postgres = _using_postgres()
        if self.postgres:
            try:
                import psycopg2
                from psycopg2.extras import DictCursor
            except ImportError as exc:
                raise RuntimeError("DATABASE_URL is set but psycopg2-binary is not installed. Add it to requirements.txt.") from exc
            url=os.getenv("DATABASE_URL", "")
            # Render may provide postgres://; psycopg2 accepts postgresql:// more consistently.
            url=re.sub(r"^postgres://", "postgresql://", url)
            self.conn=psycopg2.connect(url, cursor_factory=DictCursor)
        else:
            self.conn=sqlite3.connect(DB)
            self.conn.row_factory=sqlite3.Row
    def execute(self, sql, params=()):
        if self.postgres:
            sql=sql.replace("?", "%s")
            sql=re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", sql, flags=re.I)
            if "INSERT INTO" in sql.upper() and "ON CONFLICT" not in sql.upper() and re.search(r"INSERT\s+INTO", sql, re.I) and re.search(r"source_group|agencies", sql, re.I):
                sql=sql.rstrip().rstrip(";")+" ON CONFLICT DO NOTHING"
            sql=sql.replace("date(created_at)=%s", "created_at::date=%s")
            cur=self.conn.cursor()
            cur.execute(sql, params)
            return cur
        return self.conn.execute(sql, params)
    def commit(self): self.conn.commit()
    def rollback(self): self.conn.rollback()
    def close(self): self.conn.close()


def db():
    return DBConn()


def _sqlite_rows(table):
    sc=sqlite3.connect(DB)
    sc.row_factory=sqlite3.Row
    try:
        return sc.execute(f"SELECT * FROM {table}").fetchall()
    finally:
        sc.close()


def _migrate_local_sqlite_to_postgres():
    """One-time safe migration if an old local SQLite database is still present."""
    if not _using_postgres() or not os.path.exists(DB):
        return 0
    pc=db()
    try:
        marker=pc.execute("SELECT value FROM settings WHERE key='sqlite_migrated_v1'").fetchone()
        if marker:
            return 0
        count=0
        for r in _sqlite_rows("agencies"):
            pc.execute("INSERT INTO agencies(name,starred,deduction,created_at) VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET starred=EXCLUDED.starred,deduction=EXCLUDED.deduction",(r["name"],r["starred"],r["deduction"],r["created_at"]))
        for r in _sqlite_rows("settings"):
            pc.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",(r["key"],r["value"]))
        fares=_sqlite_rows("fares")
        for r in fares:
            exists=pc.execute("SELECT 1 FROM fares WHERE source_group=? AND sector=? AND travel_dates=? AND COALESCE(airline_code,'')=COALESCE(?, '') AND COALESCE(flight,'')=COALESCE(?, '') AND COALESCE(fare,-1)=COALESCE(?,-1) AND COALESCE(original_message,'')=COALESCE(?, '') AND COALESCE(created_at,'')=COALESCE(?, '') LIMIT 1",(r["source_group"],r["sector"],r["travel_dates"],r["airline_code"],r["flight"],r["fare"],r["original_message"],r["created_at"])).fetchone()
            if exists: continue
            pc.execute("""INSERT INTO fares(source_group,sender,message_datetime,updated_time,sector,travel_dates,airline,flight,fare,baggage,seats,conditions,original_message,created_at,fare_type,timing,b2b_discount,airline_code) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",tuple(r[k] for k in ["source_group","sender","message_datetime","updated_time","sector","travel_dates","airline","flight","fare","baggage","seats","conditions","original_message","created_at","fare_type","timing","b2b_discount","airline_code"]))
            count+=1
        pc.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",("sqlite_migrated_v1","1"))
        pc.commit()
        return count
    finally:
        pc.close()


def init_db():
    if _using_postgres():
        c=db()
        c.execute("""CREATE TABLE IF NOT EXISTS fares(
            id BIGSERIAL PRIMARY KEY, source_group TEXT, sender TEXT, message_datetime TEXT, updated_time TEXT,
            sector TEXT, travel_dates TEXT, airline TEXT, flight TEXT, fare DOUBLE PRECISION, baggage TEXT, seats TEXT,
            conditions TEXT, original_message TEXT, created_at TEXT, fare_type TEXT DEFAULT '', timing TEXT DEFAULT '',
            b2b_discount DOUBLE PRECISION DEFAULT 0, airline_code TEXT DEFAULT ''
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS agencies(
            id BIGSERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL, starred INTEGER DEFAULT 0, deduction DOUBLE PRECISION DEFAULT 0, created_at TEXT
        )""")
        c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
        for name,deduction in {"wallet":200,"santo":500,"scanner":500,"glanza":500,"glansa":500,"airguide":500}.items():
            c.execute("UPDATE agencies SET deduction=? WHERE lower(name)=? AND COALESCE(deduction,0)=0",(deduction,name))
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO NOTHING",("agency_deduction_seeded","1"))
        c.commit(); c.close()
        try:
            _migrate_local_sqlite_to_postgres()
        except Exception as exc:
            # Never destroy/overwrite data because migration failed. The app can still start on Postgres.
            print("SQLite→Postgres migration skipped:", exc)
        return

    c=db()
    c.execute("""CREATE TABLE IF NOT EXISTS fares(
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_group TEXT, sender TEXT, message_datetime TEXT, updated_time TEXT,
        sector TEXT, travel_dates TEXT, airline TEXT, flight TEXT, fare REAL, baggage TEXT, seats TEXT, conditions TEXT,
        original_message TEXT, created_at TEXT, fare_type TEXT DEFAULT '', timing TEXT DEFAULT '', b2b_discount REAL DEFAULT 0, airline_code TEXT DEFAULT ''
    )""")
    existing={r[1] for r in c.execute("PRAGMA table_info(fares)").fetchall()}
    for col,typ in {"updated_time":"TEXT","fare_type":"TEXT DEFAULT ''","timing":"TEXT DEFAULT ''","b2b_discount":"REAL DEFAULT 0","airline_code":"TEXT DEFAULT ''"}.items():
        if col not in existing: c.execute(f"ALTER TABLE fares ADD COLUMN {col} {typ}")
    c.execute("""CREATE TABLE IF NOT EXISTS agencies(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT UNIQUE NOT NULL,starred INTEGER DEFAULT 0,deduction REAL DEFAULT 0,created_at TEXT)""")
    c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
    if c.execute("SELECT 1 FROM settings WHERE key='agency_deduction_seeded'").fetchone() is None:
        for name,deduction in {"wallet":200,"santo":500,"scanner":500,"glanza":500,"glansa":500,"airguide":500}.items(): c.execute("UPDATE agencies SET deduction=? WHERE lower(name)=? AND COALESCE(deduction,0)=0",(deduction,name))
        c.execute("INSERT INTO settings(key,value) VALUES(?,?)",("agency_deduction_seeded","1"))
    c.commit(); c.close()

def clean_text(s):
    s = (s or "").replace("\u200e", "").replace("\u200f", "")
    return s.replace("\r\n", "\n").replace("\r", "\n").strip()


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
    v = re.sub(r"[^A-Z ]", "", value.upper()).strip()
    v = re.sub(r"\s+", " ", v)
    return CITY_TO_IATA.get(v, v if re.fullmatch(r"[A-Z]{3}", v) else "")


def _month_words():
    return {"JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"}


def normalize_route(text):
    """Extract a canonical route from WhatsApp text.
    Handles IATA codes, city names, arrows, hyphens, multi-leg routes and
    compact lines where airline/date/fare follow the route.
    """
    u = re.sub(r"[*_~`|]", " ", text or "").upper()
    u = re.sub(r"\s+", " ", u).strip()
    months = _month_words()

    # Named-city form: CALICUT TO RIYADH, KOCHI TO DUBAI, etc.
    m = re.search(r"([A-Z][A-Z ]{2,35}?)\s+TO\s+([A-Z][A-Z ]{2,35}?)(?=\s*(?:\(|[-–—]|\b(?:IX|6E|J9|SV|XY|FZ|OV|3L|QP|3L|WY)\b|$))", u)
    if m:
        a, b = normalize_place(m.group(1)), normalize_place(m.group(2))
        if a and b:
            return f"{a}-{b}"

    # Explicit arrow / hyphen route. For each side, take all recognized
    # airport codes so multi-leg routes such as CCJ-MCT-SHJ survive.
    if re.search(r"→|➝|➡|[-–—]", u):
        parts = re.split(r"\s*(?:→|➝|➡|[-–—])\s*", u)
        vals = []
        for part in parts:
            # Prefer a known city/airport name before treating a 3-letter
            # fragment inside it as an IATA code. This prevents ABU DHABI
            # from becoming ABU.
            city_part = re.split(r"[\(]", part, 1)[0].strip()
            n = normalize_place(city_part)
            if n:
                vals.append(n)
                continue
            codes = [c for c in re.findall(r"\b[A-Z]{3}\b", part)
                     if c not in months and c not in {"THE","AND","FOR","AIR","KG","KGS","PCS","PC","BAG","BAGGAGE","FARE"}]
            if codes:
                vals.append(codes[0])
        if len(vals) >= 2:
            return "-".join(vals)

    # Find the beginning of the first travel-date expression. Route tokens
    # are normally before it. This handles: COK KWI J9 09 OCT 25500.
    date_match = re.search(
        r"\b(?:\d{1,2}\s*(?:TO|-)\s*\d{1,2}\s*[-/.]?\s*(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)|"
        r"\d{1,2}\s*[-/.]?\s*(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)|"
        r"(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s+\d{1,2})\b", u)
    prefix = u[:date_match.start()] if date_match else u

    # Recognized city names can appear without IATA codes.
    named_codes = []
    for token in re.split(r"\s+", prefix):
        n = normalize_place(token)
        if n and n not in named_codes:
            named_codes.append(n)
    if len(named_codes) >= 2:
        return "-".join(named_codes[:5])

    codes = [c for c in re.findall(r"\b[A-Z]{3}\b", prefix)
             if c not in months and c not in {"THE","AND","FOR","AIR","KG","KGS","PCS","PC","BAG","BAGGAGE","FARE","TO"}]
    if len(codes) >= 2:
        return "-".join(codes[:5])
    return ""


def parse_date_tokens(text):
    """Extract dates from WhatsApp text.
    Supports 12 OCT, 12-OCT, OCT 12, OCT 12,14,15, 15 TO 20 OCT,
    15-20 OCT, and OCR spacing such as 14 OC T.
    """
    u = (text or "").upper()
    for mon in _month_words():
        spaced = r"\s*".join(mon)
        u = re.sub(r"(?<![A-Z])" + spaced + r"(?![A-Z])", mon, u)
    u = re.sub(r"[\u2010-\u2015\u2212]", "-", u)
    months = MONTH_RE

    # Day range: 15 TO 20 OCT / 15-20 OCT.
    m = re.search(r"\b(\d{1,2})\s*(?:TO|-)\s*(\d{1,2})\s*[-/.]?\s*(" + months + r")\b", u, re.I)
    if m:
        a, b, mon = int(m.group(1)), int(m.group(2)), m.group(3).upper()
        vals = [f"{d} {mon}" for d in range(a, b + 1)] if a <= b and b - a <= 31 else [f"{a} {mon}", f"{b} {mon}"]
        return vals, m.group(0).strip()

    # Month first: OCT 12,14,15 or OCT 12 13 14.
    m = re.search(r"\b(" + months + r")\s+([0-9]{1,2}(?:\s*,\s*[0-9]{1,2})*(?:\s+(?:AND\s+)?[0-9]{1,2})*)\b", u, re.I)
    if m:
        mon = m.group(1).upper()
        vals = [f"{x} {mon}" for x in re.findall(r"\d{1,2}", m.group(2))]
        return vals, m.group(0).strip()

    # Day first: 12 OCT / 12-OCT / 12 OCT.
    m = re.search(r"\b(\d{1,2})\s*[-/.]?\s*(" + months + r")\b", u, re.I)
    if m:
        return [f"{int(m.group(1))} {m.group(2).upper()}"], m.group(0).strip()
    return [], ""


def parse_money(text):
    """Extract an actual adult numeric fare. Never treat flight/date numbers as fares."""
    u = text or ""
    m = re.search(r"(?:₹|INR|RS\.?)[\s]*([\d,]{4,7})(?:\s*/-)?", u, re.I)
    if m:
        return float(m.group(1).replace(",", ""))

    # Numeric fare immediately after a date: 09 OCT 25500 / 09 OCT - 25500.
    m = re.search(
        r"\b\d{1,2}\s*[-/.]?\s*(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s*[-–—:=]?\s*(\d{4,6})(?=\s*(?:/-|[-–—*]|\b|$))",
        u, re.I)
    if m:
        return float(m.group(1).replace(",", ""))

    # Separator fare: 12 OCT - 23700, including suffixes like -351/-02SEAT.
    m = re.search(r"[-–—=]\s*([\d,]{4,6})(?:\s*/-)?(?=\s*(?:[-–—*]|[A-Za-z]|\b|$))", u)
    if m:
        return float(m.group(1).replace(",", ""))
    return None


def parse_airline_flight(text):
    """Return airline code and flight number(s), avoiding date numbers.
    Supports IX431, IX 431, IX-431, OV286/773, IX741&745 and airline-only lines.
    """
    u = re.sub(r"\s+", " ", text or "").upper()
    code_pattern = "|".join(sorted(AIRLINE_CODES, key=len, reverse=True))
    month_pattern = r"JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC"

    # Require the digits after the airline code not to be a travel date.
    m = re.search(
        r"\b(" + code_pattern + r")\s*[- ]?\s*(\d{2,5}(?:\s*(?:/|&)\s*(?:[A-Z0-9]{2,3}\s*)?\d{2,5})*)\b"
        r"(?!\s*(?:" + month_pattern + r")\b)", u)
    if m:
        code = m.group(1).replace(" ", "")
        flight = re.sub(r"\s+", "", m.group(2))
        return code, flight

    for code, name in AIRLINE_NAMES.items():
        if name.upper() in u:
            return code, ""
    m = re.search(r"\b(" + code_pattern + r")\b", u)
    return (m.group(1), "") if m else ("", "")


def parse_baggage(text):
    u = re.sub(r"\s+", " ", (text or "").upper()).strip()
    patterns = [
        r"\b\d{1,3}\s*KG\s*\+\s*\d{1,3}\s*KG\b",
        r"\b\d{1,3}\s*\+\s*\d{1,3}\s*KG\b",
        r"\b\d{1,3}\s*\+\s*\d{1,3}\s*KG\s*\+?\s*STD\s*MEAL\b",
        r"\b\d{1,3}\s*(?:KG|KGS|BG)\b",
        r"\b\d{1,3}\s*PC(?:S)?\b",
        r"\b\d{1,3}\s*\*\s*\d{1,3}\s*\+\s*\d{1,3}\s*KG(?:\s*\([^)]*PC[^)]*\))?",
        r"\b\d{1,3}\s*\+\s*\d{1,3}\s*/\s*\d{1,3}\s*\+\s*\d{1,3}\b",
    ]
    for p in patterns:
        m = re.search(p, u)
        if m:
            return re.sub(r"\s+", " ", m.group(0)).strip()
    return ""


def parse_timing(text):
    m = re.search(r"\b(\d{1,2}:\d{2})\s*[-–—]\s*(\d{1,2}:\d{2})\b", text or "")
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = re.search(r"DEP\s*(\d{1,2}:\d{2})\s*[:\-]\s*ARR\s*(\d{1,2}:\d{2})", text or "", re.I)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return ""


def parse_fare_type(text):
    u = re.sub(r"\s+", " ", (text or "").upper())
    if re.search(r"\bDEAL\s+FARE\b", u): return "DEAL FARE"
    if re.search(r"\bV?SPECIAL\s+FARE\b", u): return "SPECIAL FARE"
    if re.search(r"\bLOWEST\s+FARE\b", u): return "LOWEST FARE"
    if re.search(r"\bSPL\b", u): return "SPL"
    return ""


def parse_seats(text):
    m = re.search(r"(?:\b(\d+)\s*SEAT\b|\b(\d+)\s*💺|\b(\d+)\s*SEATS?\b|\((\d+)\s*SEAT)", text or "", re.I)
    return next((x for x in m.groups() if x), "") if m else ""


def parse_b2b_discount(text):
    u = (text or "").upper()
    m = re.search(r"(?:B2B\s*)?(\d{2,5})\s*LESS\b", u)
    if not m:
        m = re.search(r"B2B\s+(\d{2,5})", u)
    return float(m.group(1)) if m else 0


def extract_fare_records(msg):
    body = clean_text(msg.get("body", ""))
    lines = [x.strip() for x in body.split("\n") if x.strip()]
    records = []
    global_b2b = parse_b2b_discount(body)
    global_conditions = []
    ub = body.upper()
    if "NON-REFUNDABLE" in ub: global_conditions.append("NON-REFUNDABLE")
    if "SUBJECT TO AVAILABILITY" in ub: global_conditions.append("SUBJECT TO AVAILABILITY")

    current = {"sector":"", "airline":"", "airline_code":"", "flight":"", "baggage":"", "timing":"", "fare_type":"", "b2b_discount":global_b2b, "conditions":[]}

    def emit(line):
        dates, _ = parse_date_tokens(line)
        if not dates or not current["sector"]:
            return
        fare = parse_money(line)
        fare_type = parse_fare_type(line) or current["fare_type"]
        seats = parse_seats(line)
        baggage = parse_baggage(line) or current["baggage"]
        timing = parse_timing(line) or current["timing"]
        code, flight = parse_airline_flight(line)
        # A line such as COK KWI J9 09 OCT must not replace header flight J9 406
        # with the date number 09. Only use a line-level flight when it is >= 2
        # digits and is not immediately followed by a month.
        if code:
            if code != current["airline_code"] or flight:
                current["airline_code"] = code
                current["airline"] = AIRLINE_NAMES.get(code, code)
        if flight and not re.search(r"\b\d{1,2}\s*(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\b", line.upper()):
            current["flight"] = flight
        conditions = list(current["conditions"])+global_conditions
        if "MEAL" in line.upper() and "MEALS" not in conditions: conditions.append("MEALS")
        for tag in ("MRNG", "EVNG"):
            if re.search(r"\b"+tag+r"\b", line.upper()) and tag not in conditions: conditions.append(tag)
        for d in dates:
            records.append({
                "group":"Unspecified", "sector":current["sector"], "travel_dates":d,
                "airline":AIRLINE_NAMES.get(current["airline_code"], current["airline"] or current["airline_code"]),
                "airline_code":current["airline_code"], "flight":current["flight"], "fare":fare,
                "baggage":baggage, "seats":seats,
                "conditions":", ".join(dict.fromkeys([x for x in conditions if x])),
                "fare_type":fare_type, "timing":timing,
                "b2b_discount":current["b2b_discount"] or global_b2b,
                "original_message":body
            })

    for raw in lines:
        line = re.sub(r"[*_~`|]", " ", raw)
        u = re.sub(r"\s+", " ", line.upper()).strip()
        if not u:
            continue
        if u.startswith(("HTTP://", "HTTPS://")) or re.search(r"@ALHINDONLINE|@TRAVELWALLET|@FLYUNITEDTRIP", u):
            continue
        dates = parse_date_tokens(u)[0]
        route = normalize_route(line)

        # Update global conditions/context lines before processing a route/date.
        code, flight = parse_airline_flight(line)
        bg = parse_baggage(line)
        tm = parse_timing(line)
        ft = parse_fare_type(line)
        bd = parse_b2b_discount(line)
        if code:
            current["airline_code"] = code
            current["airline"] = AIRLINE_NAMES.get(code, code)
        if flight and not re.search(r"\b\d{1,2}\s*(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\b", u):
            current["flight"] = flight
        if bg: current["baggage"] = bg
        if tm: current["timing"] = tm
        if ft: current["fare_type"] = ft
        if bd: current["b2b_discount"] = bd
        if "NON-REFUNDABLE" in u and "NON-REFUNDABLE" not in current["conditions"]: current["conditions"].append("NON-REFUNDABLE")
        if "SUBJECT TO AVAILABILITY" in u and "SUBJECT TO AVAILABILITY" not in current["conditions"]: current["conditions"].append("SUBJECT TO AVAILABILITY")

        if route:
            # Do not clear airline/baggage/timing context: many Airguide-style
            # blocks put baggage/flight on the line immediately before route/date rows.
            current["sector"] = route
            if dates:
                emit(line)
            continue

        if dates and current["sector"]:
            emit(line)

    return records

def is_fare_message(body):
    u = body or ""
    return bool(normalize_route(u)) and bool(parse_date_tokens(u)[0]) and bool(
        parse_money(u) is not None or parse_fare_type(u) or re.search(r"DEAL\s+FARE|SPECIAL\s+FARE|LOWEST\s+FARE|\bSPL\b", u, re.I)
    )

def parse_file(path):
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            names=[n for n in z.namelist() if n.lower().endswith((".txt",".md"))]
            if not names:return []
            raw=z.read(names[0]).decode("utf-8","replace")
    else:
        with open(path,"r",encoding="utf-8",errors="replace") as f: raw=f.read()
    return parse_whatsapp_export(raw)


def _record_dict_for_storage(r, agency, upload_time):
    return {
        "source_group": agency,
        "sender": r.get("sender", ""),
        "message_datetime": r.get("message_datetime", ""),
        "updated_time": upload_time,
        "sector": r.get("sector", ""),
        "travel_dates": r.get("travel_dates", ""),
        "airline": r.get("airline", ""),
        "flight": r.get("flight", ""),
        "fare": r.get("fare"),
        "baggage": r.get("baggage", ""),
        "seats": r.get("seats", ""),
        "conditions": r.get("conditions", ""),
        "original_message": r.get("original_message", ""),
        "created_at": upload_time,
        "fare_type": r.get("fare_type", ""),
        "timing": r.get("timing", ""),
        "b2b_discount": r.get("b2b_discount", 0),
        "airline_code": r.get("airline_code", "")
    }


def _record_identity(r):
    return (
        route_market(r.get("sector", "")),
        (r.get("travel_dates") or "").strip().upper(),
        (r.get("airline_code") or r.get("airline") or "").strip().upper(),
        (r.get("flight") or "").strip().upper(),
    )


def _record_signature(r):
    return (
        r.get("sector", ""), r.get("travel_dates", ""), r.get("airline_code", ""),
        r.get("airline", ""), r.get("flight", ""), r.get("fare"),
        r.get("baggage", ""), r.get("seats", ""), r.get("conditions", ""),
        r.get("fare_type", ""), r.get("timing", ""), r.get("b2b_discount", 0)
    )


def _record_label(r):
    fare = "—" if r.get("fare") is None else f"₹{float(r['fare']):,.0f}"
    route = (r.get("sector") or "—").replace("-", " → ")
    airline = r.get("airline_code") or r.get("airline") or "—"
    flight = f" · {r.get('flight')}" if r.get("flight") else ""
    return f"{route} | {r.get('travel_dates') or '—'} | {airline}{flight} | {fare}"


def _pending_path(token):
    pending_dir = os.path.join(UPLOAD_DIR, "pending")
    os.makedirs(pending_dir, exist_ok=True)
    return os.path.join(pending_dir, f"{token}.json")


def _load_existing_for_agency(agency):
    c = db()
    rows = c.execute("SELECT * FROM fares WHERE source_group=? ORDER BY travel_dates,sector,id", (agency,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def _build_upload_plan(records, agency, mode, upload_time):
    new_records = [_record_dict_for_storage(r, agency, upload_time) for r in records]
    existing = _load_existing_for_agency(agency)
    affected_markets = sorted({route_market(r.get("sector", "")) for r in new_records if route_market(r.get("sector", ""))})

    if mode == "addon":
        old_scope = [r for r in existing if route_market(r.get("sector", "")) in set(affected_markets)]
    else:
        old_scope = existing

    old_by = defaultdict(list)
    new_by = defaultdict(list)
    for r in old_scope:
        old_by[_record_identity(r)].append(r)
    for r in new_records:
        new_by[_record_identity(r)].append(r)

    added, removed, changed, unchanged = [], [], [], []
    for key in sorted(set(old_by) | set(new_by), key=str):
        olds = list(old_by.get(key, []))
        news = list(new_by.get(key, []))
        used_old = set()
        used_new = set()
        # Exact matches first.
        for ni, nr in enumerate(news):
            for oi, orow in enumerate(olds):
                if oi in used_old:
                    continue
                if _record_signature(nr) == _record_signature(orow):
                    unchanged.append(nr)
                    used_old.add(oi); used_new.add(ni)
                    break
        # Pair remaining records by identity and show them as changed.
        rem_old = [orow for oi, orow in enumerate(olds) if oi not in used_old]
        rem_new = [nr for ni, nr in enumerate(news) if ni not in used_new]
        pair_count = min(len(rem_old), len(rem_new))
        for i in range(pair_count):
            changed.append((rem_old[i], rem_new[i]))
        removed.extend(rem_old[pair_count:])
        added.extend(rem_new[pair_count:])

    return {
        "agency": agency,
        "mode": mode,
        "upload_time": upload_time,
        "affected_markets": affected_markets,
        "new_records": new_records,
        "old_scope_count": len(old_scope),
        "existing_total": len(existing),
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged": unchanged,
        "total_new": len(new_records)
    }


def _apply_upload_plan(plan):
    agency = plan["agency"]
    mode = plan["mode"]
    upload_time = plan["upload_time"]
    new_records = plan["new_records"]
    c = db()
    try:
        if mode == "full":
            c.execute("DELETE FROM fares WHERE source_group=?", (agency,))
        else:
            for market in plan.get("affected_markets", []):
                rows = c.execute("SELECT id,sector FROM fares WHERE source_group=?", (agency,)).fetchall()
                ids = [r["id"] for r in rows if route_market(r["sector"]) == market]
                for rid in ids:
                    c.execute("DELETE FROM fares WHERE id=?", (rid,))

        for r in new_records:
            c.execute("""
                INSERT INTO fares(source_group,sender,message_datetime,updated_time,sector,travel_dates,airline,flight,fare,baggage,seats,conditions,original_message,created_at,fare_type,timing,b2b_discount,airline_code)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (r["source_group"], r["sender"], r["message_datetime"], upload_time,
                  r["sector"], r["travel_dates"], r["airline"], r["flight"], r["fare"],
                  r["baggage"], r["seats"], r["conditions"], r["original_message"], upload_time,
                  r["fare_type"], r["timing"], r["b2b_discount"], r["airline_code"]))
        c.execute("INSERT INTO agencies(name,starred,deduction,created_at) VALUES(?,?,?,?) ON CONFLICT(name) DO NOTHING",
                  (agency, 0, 0, upload_time))
        c.commit()
        return len(new_records)
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def records_from_messages(messages):
    out=[]
    for msg in messages:
        if not is_fare_message(msg.get("body","")): continue
        dt=parse_datetime(msg.get("date",""),msg.get("time",""))
        for rec in extract_fare_records(msg):
            rec["sender"]=msg.get("sender",""); rec["message_datetime"]=dt; out.append(rec)
    return out


def route_market(sector):
    parts=[x.strip().upper() for x in (sector or "").split("-") if x.strip()]
    return f"{parts[0]}-{parts[-1]}" if len(parts)>=2 else (parts[0] if parts else "")

def route_origin(sector):
    p=route_market(sector).split("-"); return p[0] if p else ""

def route_destination(sector):
    p=route_market(sector).split("-"); return p[-1] if p else ""

def resolve_fare_date(value, now=None):
    now=now or datetime.now()
    m=re.match(r"\s*(\d{1,2})\s+([A-Z]{3})\s*$",(value or "").upper())
    if not m: return None
    months={m:i for i,m in enumerate(["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"],1)}
    mon=months.get(m.group(2));
    if not mon: return None
    year=now.year
    # Prefer the next occurrence; this handles Dec→Jan correctly.
    try:
        d=datetime(year,mon,int(m.group(1)))
        if d.date() < (now.date()-__import__('datetime').timedelta(days=1)):
            d=datetime(year+1,mon,int(m.group(1)))
        return d
    except ValueError: return None

def fmt_fare(v):
    return "—" if v is None else f"₹{v:,.0f}"


def sector_summary(sector, starred_only=False, selected=None):
    c=db(); rows=c.execute("SELECT * FROM fares ORDER BY CASE WHEN fare IS NULL THEN 1 ELSE 0 END, fare, travel_dates").fetchall(); c.close()
    target=route_market(sector)
    names=set(selected or [])
    return [r for r in rows if route_market(r["sector"])==target and (not starred_only or r["source_group"] in names or r["source_group"] in set(names)) and (not selected or r["source_group"] in names)]


BASE=r"""
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SkyDays Market Fare Intelligence</title>
<style>
*{box-sizing:border-box}body{font-family:Arial,sans-serif;background:#f4f7fb;margin:0;color:#172334}header{background:#102d48;color:#fff;padding:16px 24px}.wrap{max-width:1500px;margin:auto;padding:18px}.topbar{display:flex;align-items:center;gap:16px;flex-wrap:wrap}.brand{flex:1;min-width:260px}.brand h2{margin:0}.header-search{display:flex;align-items:center;gap:6px;margin-left:auto}.header-search input{width:260px;background:#fff}.header-search button{background:#1f78b4}.nav{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}.nav a{color:#fff;text-decoration:none;padding:8px 12px;border-radius:7px;background:#214766}.card{background:#fff;border-radius:12px;padding:16px;margin-bottom:14px;box-shadow:0 2px 10px #0001}h2,h3{margin:0 0 10px}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.kpi{padding:14px;border-radius:10px;background:#edf3ff}.kpi b{font-size:22px}input,select,button,textarea{font:inherit;padding:9px;border:1px solid #cbd5df;border-radius:7px}button{background:#1261a0;color:#fff;border:0;cursor:pointer}button.secondary{background:#64748b}button.star{background:#f4f7fb;color:#172334;border:1px solid #ccd6e0}.flex{display:flex;gap:9px;align-items:center;flex-wrap:wrap}table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:9px;border-bottom:1px solid #e5e9ee;text-align:left;vertical-align:top}th{background:#eef2f6;position:sticky;top:0}.scroll{overflow:auto;max-height:650px}.sector{font-weight:700;font-size:16px}.low{font-weight:800;color:#087443}.muted{color:#687586;font-size:12px}.pill{display:inline-block;padding:4px 7px;border-radius:20px;background:#edf3ff;margin:2px;font-size:12px}.agency-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}.agency{padding:10px;border:1px solid #d9e1e8;border-radius:8px;background:#fff}.dropzone{border:2px dashed #9db2c7;border-radius:10px;padding:18px;text-align:center;background:#f9fbfd}
.compare-wrap{overflow:auto;border:1px solid #e2e8f0;border-radius:10px;margin-top:14px}.compare-table{min-width:760px}.compare-table th,.compare-table td{border-right:1px solid #e7ebf0}.compare-table .date-col{position:sticky;left:0;background:#f8fafc;z-index:2;min-width:110px}.agency-head{min-width:150px;text-align:center}.fare-cell{text-align:center;min-width:150px;background:#fff}.fare-cell.best-fare{background:#e9f8ef;box-shadow:inset 0 0 0 2px #0b7a45}.fare-big{font-size:20px;font-weight:800;color:#0d5f38}.fare-special{font-weight:800;color:#7c3aed}.fare-meta{font-size:11px;color:#6b7280;margin-top:3px}.fare-cell.empty{color:#a0a9b4}.agency-select{min-width:190px;max-width:320px;height:38px}.sector-head{padding-bottom:12px}
.import-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}textarea{width:100%;min-height:180px}.notice{padding:10px;border-radius:8px;background:#edf7ee}.sector-page{padding-top:2px}.sector-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px;align-items:stretch}.sector-card{display:flex;min-height:132px;align-items:center;justify-content:center;text-decoration:none;color:#172334;background:linear-gradient(145deg,#ffffff,#f7fafc);border:1px solid #d9e3ec;border-radius:16px;box-shadow:0 3px 12px #17324a0d;transition:transform .16s ease,box-shadow .16s ease,border-color .16s ease;overflow:hidden}.sector-card:hover{transform:translateY(-2px);box-shadow:0 8px 24px #17324a16;border-color:#9fb6c9}.sector-main{text-align:center;line-height:1;width:100%;padding:20px 14px}.sector-route{font-size:31px;font-weight:800;letter-spacing:.4px;white-space:nowrap}.sector-arrow{display:inline-block;margin:0 8px;font-weight:500;color:#64798d}.sector-airlines{margin-top:13px;color:#718096;font-size:11px;font-weight:800;letter-spacing:1.5px;text-transform:uppercase;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.sector-empty{padding:42px 20px;text-align:center;color:#687586;border:1px dashed #cbd5df;border-radius:14px;background:#fafcfe}.trust-badge{display:inline-block;padding:4px 7px;border-radius:12px;background:#e9f8ef;color:#0b7a45;font-weight:800;font-size:11px}.untrusted-badge{display:inline-block;padding:4px 7px;border-radius:12px;background:#fff3e8;color:#a34a00;font-weight:800;font-size:11px}@media(max-width:1000px){.header-search{width:100%;margin-left:0}.header-search input{flex:1;width:auto}.grid{grid-template-columns:repeat(2,1fr)}.agency-grid,.import-grid{grid-template-columns:1fr 1fr}}@media(max-width:650px){.grid,.agency-grid,.import-grid{grid-template-columns:1fr}.wrap{padding:10px}.sector-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.sector-card{min-height:105px;border-radius:13px}.sector-route{font-size:24px}.sector-arrow{margin:0 4px}.sector-airlines{margin-top:10px;font-size:10px;letter-spacing:1px}}
</style></head><body><header><div class="wrap" style="padding-top:0;padding-bottom:0"><div class="topbar"><div class="brand"><h2>SkyDays Market Fare Intelligence</h2><div class="nav"><a href="/">Sectors</a><a href="/agencies">Agency Master</a><a href="/import">Import</a><a href="/data">Data</a><a href="/lowest">Lowest Fare</a><a href="/backup_json">Backup</a><a href="/restore">Restore</a></div></div><form class="header-search" action="/" method="get"><input name="q" value="{{ request.args.get('q','') }}" placeholder="Search sector / destination"><button type="submit">🔍</button></form></div></div></header><div class="wrap">{% with messages=get_flashed_messages() %}{% for m in messages %}<div class="card notice">{{m}}</div>{% endfor %}{% endwith %}{{content|safe}}</div></body></html>
"""


@app.route("/")
def home():
    q=clean_text(request.args.get("q","")).upper()
    q_norm=re.sub(r"[^A-Z0-9]","",q)
    c=db(); rows=c.execute("SELECT * FROM fares").fetchall(); c.close()
    markets={}
    for r in rows:
        key=route_market(r["sector"])
        if not key: continue
        if q_norm and q_norm not in re.sub(r"[^A-Z0-9]","",key) and q_norm not in re.sub(r"[^A-Z0-9]","",(r["sector"] or "").upper()):
            continue
        item=markets.setdefault(key,{"sector":key,"airlines":[]})
        code=(r["airline_code"] or r["airline"] or "").strip().upper()
        if code and code not in item["airlines"]: item["airlines"].append(code)
    sector_cards=sorted(markets.values(),key=lambda x:x["sector"])
    content=render_template_string(r"""
<div class="sector-page">
  <div class="flex" style="justify-content:flex-end;margin-bottom:12px"><a href="/lowest"><button>Lowest Fare Search</button></a></div>
  <div id="sectorGrid" class="sector-grid">
  {% for s in sectors %}
    <a class="sector-card" data-sector="{{s['sector']|replace('-','')|upper}}" href="/sector/{{s['sector']}}" aria-label="Open {{s['sector']}} market comparison">
      <div class="sector-main">
        {% set parts=s['sector'].split('-') %}
        <div class="sector-route">{{parts[0]}} <span class="sector-arrow">→</span> {{parts[-1]}}</div>
        {% if s['airlines'] %}<div class="sector-airlines">{{s['airlines']|join(' · ')}}</div>{% endif %}
      </div>
    </a>
  {% endfor %}
  </div>
  {% if not sectors %}<div class="sector-empty">No market sectors available{% if q %} for <b>{{q}}</b>{% endif %}.</div>{% endif %}
</div>
""",sectors=sector_cards,q=q)
    return render_template_string(BASE,content=content)


@app.route("/sector/<path:sector>")
def sector_detail(sector):
    sector=sector.upper().replace("_","-")
    selected=request.args.getlist("agency")
    starred_only=request.args.get("starred","1") == "1"
    period=request.args.get("period","7").strip().lower()
    if period not in ("7","15","30","60","90","all"):
        period="7"
    c=db()
    agencies=c.execute("SELECT name,starred,COALESCE(deduction,0) AS deduction FROM agencies ORDER BY starred DESC,name").fetchall()
    c.close()
    starred_names=[a["name"] for a in agencies if a["starred"]]
    if not starred_names:
        starred_only=False
    active_names=selected if selected else (starred_names if starred_only else [])
    rows=sector_summary(sector,False,active_names if active_names else None)

    # Agency deductions are maintained in the Agency Master. They affect
    # comparison only; the original marketed fare is never changed.
    deduction_map={a["name"]:float(a["deduction"] or 0) for a in agencies}
    deduction_map_ci={(a["name"] or "").strip().lower():float(a["deduction"] or 0) for a in agencies}

    def agency_adjustment(name):
        key=(name or "").strip().lower()
        return deduction_map.get(name, deduction_map_ci.get(key, 0))

    def comparison_fare(row):
        fare=row["fare"]
        if fare is None:
            return None
        return float(fare) - agency_adjustment(row["source_group"])

    month_map={m:i for i,m in enumerate(["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"],1)}
    now=datetime.now()
    date_objs=[]
    for r in rows:
        m=re.match(r"\s*(\d{1,2})\s+([A-Z]{3})\s*$", (r["travel_dates"] or "").upper())
        if not m or m.group(2) not in month_map:
            continue
        day=int(m.group(1)); mon=month_map[m.group(2)]; year=now.year
        if mon < now.month-6:
            year += 1
        try:
            date_objs.append((datetime(year,mon,day),r))
        except ValueError:
            pass
    date_objs.sort(key=lambda x:(x[0], 10**12 if comparison_fare(x[1]) is None else comparison_fare(x[1])))
    unique_dates=[]
    for d,r in date_objs:
        if d not in unique_dates:
            unique_dates.append(d)

    # Comparison window: default next 7 calendar days, with optional 15/30/60/90
    # day views or all available retained dates.
    today=datetime.now().date()
    if period == "all":
        next_dates=unique_dates
        period_label="All available"
    else:
        days=int(period)
        end_date=today+timedelta(days=days-1)
        next_dates=[d for d in unique_dates if today <= d.date() <= end_date]
        period_label=f"Next {days} days"
    date_set=set(next_dates)
    rows8=[r for d,r in date_objs if d in date_set]

    # Order agencies by their lowest adjusted comparison fare in the displayed window.
    agency_stats=[]
    names_in_rows=[]
    for r in rows8:
        n=(r["source_group"] or "Unknown").strip() or "Unknown"
        if n not in names_in_rows:
            names_in_rows.append(n)
    for name in names_in_rows:
        vals=[comparison_fare(r) for r in rows8 if (r["source_group"] or "Unknown").strip()==name and comparison_fare(r) is not None]
        agency_stats.append((name,min(vals) if vals else None))
    agency_stats.sort(key=lambda x:(x[1] is None, x[1] if x[1] is not None else 10**12, x[0].lower()))
    compare_names=[x[0] for x in agency_stats]

    comparison=[]
    for d in next_dates:
        ds=d.strftime("%d %b").lstrip("0").upper()
        rowdata=[]
        for name in compare_names:
            matches=[r for dd,r in date_objs if dd==d and (r["source_group"] or "Unknown").strip()==name]
            numeric=[r for r in matches if comparison_fare(r) is not None]
            best=min(numeric,key=lambda r:comparison_fare(r)) if numeric else (matches[0] if matches else None)
            rowdata.append(best)
        nums=[comparison_fare(r) for r in rowdata if r and comparison_fare(r) is not None]
        comparison.append({"date":ds,"items":rowdata,"lowest":min(nums) if nums else None})

    overall=[comparison_fare(r) for r in rows8 if comparison_fare(r) is not None]
    lowest=min(overall) if overall else None
    adjustment_names={a["name"]:agency_adjustment(a["name"]) for a in agencies}
    content=render_template_string(r'''
<div class="card sector-head">
  <div class="flex">
    <div><h3>{{sector.replace("-"," → ")}}</h3><div class="muted">Market comparison by origin + final destination · {{period_label}}</div></div>
    <div style="margin-left:auto" class="flex">
      <button type="button" class="secondary" onclick="if(history.length>1){history.back()}else{location.href='/' }">← Back</button>
      <a href="/"><button type="button" class="secondary">All Sectors</button></a>
    </div>
  </div>
</div>
<div class="grid">
  <div class="kpi">Lowest comparison fare<br><b>{{'₹{:,.0f}'.format(lowest) if lowest is not none else '—'}}</b></div>
  <div class="kpi">Agencies compared<br><b>{{compare_names|length}}</b></div>
  <div class="kpi">Dates checked<br><b>{{comparison|length}}</b></div>
  <div class="kpi">View<br><b>{{period_label}}</b></div>
</div>
<div class="card">
  <div class="flex">
    <h3 style="margin-right:auto">Agency comparison</h3>
    <form method="get" class="flex" style="margin:0">
      {% if not selected %}<input type="hidden" name="starred" value="1">{% endif %}
      <label class="muted" style="white-space:nowrap">Period</label>
      <select name="period" style="min-width:145px">
        <option value="7" {% if period=='7' %}selected{% endif %}>Next 7 days</option>
        <option value="15" {% if period=='15' %}selected{% endif %}>Next 15 days</option>
        <option value="30" {% if period=='30' %}selected{% endif %}>Next 30 days</option>
        <option value="60" {% if period=='60' %}selected{% endif %}>Next 60 days</option>
        <option value="90" {% if period=='90' %}selected{% endif %}>Next 90 days</option>
        <option value="all" {% if period=='all' %}selected{% endif %}>All available</option>
      </select>
      <select name="agency" multiple size="1" class="agency-select" title="Optional: select agencies for this comparison">
        {% for a in agencies %}<option value="{{a['name']}}" {% if a['name'] in active_names %}selected{% endif %}>{{'★ ' if a['starred'] else ''}}{{a['name']}}</option>{% endfor %}
      </select>
      <button type="submit">Compare Selected</button>
      <a href="/sector/{{sector}}?starred=1&period={{period}}"><button type="button" class="secondary">Starred</button></a>
      <a href="/sector/{{sector}}?starred=0&period={{period}}"><button type="button" class="secondary">All</button></a>
    </form>
  </div>
  <div class="muted" style="margin-top:6px">Lowest comparison fare is placed first. Agency-specific comparison deductions are applied below; original marketed fares remain unchanged.</div>
  <div class="notice" style="margin-top:10px">
    <b>Agency Master adjustments:</b>
    {% for name,less in adjustment_names.items() if less > 0 %}<span class="pill">{{name}} − ₹{{'{:,.0f}'.format(less)}}</span>{% endfor %}
  </div>
  {% if compare_names %}
  <div class="compare-wrap">
    <table class="compare-table">
      <thead><tr><th class="date-col">Travel date</th>{% for name in compare_names %}<th class="agency-head">{% if name in starred_names %}<span style="color:#0b7a45;font-size:20px">★</span> {% endif %}{{name}}{% if adjustment_names.get(name,0) > 0 %}<div class="muted">− ₹{{'{:,.0f}'.format(adjustment_names[name])}}</div>{% endif %}</th>{% endfor %}</tr></thead>
      <tbody>
      {% for day in comparison %}
        <tr><th class="date-col">{{day['date']}}</th>
        {% for item in day['items'] %}
          {% if item %}
          {% set shown_fare = comparison_fare(item) %}
          <td class="fare-cell {% if shown_fare is not none and day['lowest'] is not none and shown_fare==day['lowest'] %}best-fare{% endif %}">
            {% if shown_fare is not none %}<div class="fare-big">₹{{'{:,.0f}'.format(shown_fare)}}</div>{% if item['fare'] != shown_fare %}<div class="fare-meta">Marketed ₹{{'{:,.0f}'.format(item['fare'])}}</div>{% endif %}{% else %}<div class="fare-special">{{item['fare_type'] or '—'}}</div>{% endif %}
            <div class="fare-meta">{% if item['source_group'] in starred_names %}<span style="color:#0b7a45;font-weight:900">★</span> {% endif %}{{route_origin(item['sector'])}}→{{route_destination(item['sector'])}}</div><div class="fare-meta">Route: {{item['sector']|replace("-"," → ")}}</div><div class="fare-meta">{{item['airline_code'] or item['airline'] or ''}}{% if item['flight'] %} · {{item['flight']}}{% endif %}</div>
            {% if item['baggage'] %}<div class="fare-meta">{{item['baggage']}}</div>{% endif %}
            {% if item['seats'] %}<div class="fare-meta">{{item['seats']}} seat{% if item['seats']|int != 1 %}s{% endif %}</div>{% endif %}
          </td>
          {% else %}<td class="fare-cell empty">—</td>{% endif %}
        {% endfor %}</tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
  {% else %}<div class="notice">No starred-agency records found for this sector.</div>{% endif %}
</div>
<div class="card"><details><summary><b>Full market records</b> <span class="muted">({{rows|length}} records)</span></summary>
<div class="scroll" style="margin-top:12px"><table><tr><th>★</th><th>Agency</th><th>Date</th><th>Airline</th><th>Flight</th><th>Baggage</th><th>Timing</th><th>Marketed Fare</th><th>Comparison Fare</th><th>Type</th><th>B2B</th><th>Seats</th></tr>{% for r in rows %}{% set cf=comparison_fare(r) %}<tr><td>{% if r['source_group'] in starred_names %}<span style="color:#0b7a45;font-weight:900">★</span>{% endif %}</td><td><b>{{r['source_group'] or 'Unknown'}}</b></td><td>{{r['travel_dates']}}</td><td>{{r['airline'] or r['airline_code'] or '—'}}</td><td>{{r['flight'] or '—'}}</td><td>{{r['baggage'] or '—'}}</td><td>{{r['timing'] or '—'}}</td><td class="low">{{'₹{:,.0f}'.format(r['fare']) if r['fare'] is not none else '—'}}</td><td class="low">{{'₹{:,.0f}'.format(cf) if cf is not none else '—'}}</td><td><span class="pill">{{r['fare_type'] or '—'}}</span></td><td>{{'₹{:,.0f}'.format(r['b2b_discount']) if r['b2b_discount'] else '—'}}</td><td>{{r['seats'] or '—'}}</td></tr>{% else %}<tr><td colspan="12">No records.</td></tr>{% endfor %}</table></div></details></div>
''',sector=sector,rows=rows8,agencies=agencies,selected=selected,starred_only=starred_only,lowest=lowest,starred_names=set(starred_names),active_names=active_names,compare_names=compare_names,comparison=comparison,adjustment_names=adjustment_names,comparison_fare=comparison_fare,route_origin=route_origin,route_destination=route_destination,period=period,period_label=period_label)
    return render_template_string(BASE,content=content)


@app.route("/lowest")
def lowest_fare():
    q=clean_text(request.args.get("q","")).upper()
    period=request.args.get("period","7")
    c=db(); rows=c.execute("SELECT * FROM fares").fetchall(); agencies=c.execute("SELECT name,starred,COALESCE(deduction,0) AS deduction FROM agencies ORDER BY starred DESC,name").fetchall(); c.close()
    amap={a["name"]:{"starred":bool(a["starred"]),"deduction":float(a["deduction"] or 0)} for a in agencies}
    now=datetime.now(); start=now.replace(hour=0,minute=0,second=0,microsecond=0); end=None
    if period.isdigit(): end=start+timedelta(days=max(1,int(period))-1)
    elif period=="month":
        if start.month==12: end=datetime(start.year+1,1,1)-timedelta(days=1)
        else: end=datetime(start.year,start.month+1,1)-timedelta(days=1)
    else: start=None
    tokens=[normalize_place(t) for t in re.findall(r"[A-Za-z]{3,}",q) if normalize_place(t)]
    exact_market=f"{tokens[0]}-{tokens[1]}" if len(tokens)>=2 else ""
    results=[]
    for r in rows:
        market=route_market(r["sector"])
        if not market: continue
        if q:
            hay=" ".join([market,r["sector"] or "",r["airline_code"] or "",r["airline"] or "",r["flight"] or "",r["source_group"] or ""] ).upper()
            if exact_market:
                if market!=exact_market: continue
            elif q not in hay and q.replace(" ","") not in re.sub(r"[^A-Z0-9]","",hay): continue
        d=resolve_fare_date(r["travel_dates"],now)
        if start is not None and (d is None or d < start or d > end): continue
        if r["fare"] is None: continue
        deduction=amap.get(r["source_group"],{}).get("deduction",0)
        adjusted=float(r["fare"])-deduction
        results.append({"r":r,"date":d,"market":market,"adjusted":adjusted,"deduction":deduction,"trusted":amap.get(r["source_group"],{}).get("starred",False)})
    results.sort(key=lambda x:(x["adjusted"], x["date"] or datetime.max, x["market"], (x["r"]["source_group"] or "").lower()))
    content=render_template_string(r"""
<div class="card">
  <div class="flex"><div><h3>Lowest Fare Search</h3><div class="muted">Searches the complete retained market database. No records are removed by this search.</div></div><a href="/"><button type="button" class="secondary">← Back to Markets</button></a></div>
</div>
<div class="card">
<form method="get" class="flex">
  <input name="q" value="{{q}}" placeholder="Search DXB, CCJ DXB, DMM..." style="flex:1;min-width:220px">
  <select name="period"><option value="2" {% if period=='2' %}selected{% endif %}>Next 2 days</option><option value="3" {% if period=='3' %}selected{% endif %}>Next 3 days</option><option value="7" {% if period=='7' %}selected{% endif %}>Next 7 days</option><option value="15" {% if period=='15' %}selected{% endif %}>Next 15 days</option><option value="30" {% if period=='30' %}selected{% endif %}>Next 30 days</option><option value="month" {% if period=='month' %}selected{% endif %}>This month</option><option value="all" {% if period=='all' %}selected{% endif %}>Entire data</option></select>
  <button type="submit">Find Lowest</button>
</form>
</div>
<div class="card"><div class="grid"><div class="kpi">Results<br><b>{{results|length}}</b></div><div class="kpi">View<br><b>{{'Entire retained data' if period=='all' else ('This month' if period=='month' else 'Next '+period+' days')}}</b></div><div class="kpi">Lowest<br><b>{{'₹{:,.0f}'.format(results[0]['adjusted']) if results else '—'}}</b></div><div class="kpi">Starred lowest<br><b>{{'₹{:,.0f}'.format(trusted_lowest) if trusted_lowest is not none else '—'}}</b></div></div></div>
<div class="card">
{% if results %}
<div class="scroll"><table><tr><th>Rank</th><th>Market</th><th>Date</th><th>Agency</th><th>Airline</th><th>Flight</th><th>Full Route</th><th>Timing</th><th>Baggage</th><th>Seats</th><th>Marketed</th><th>Deduction</th><th>Adjusted</th></tr>
{% for x in results %}{% set r=x['r'] %}<tr><td><b>{{loop.index}}</b></td><td><b>{{x['market'].replace('-',' → ')}}</b></td><td>{{r['travel_dates'] or '—'}}</td><td><b>{% if x['trusted'] %}<span style="color:#0b7a45;font-weight:900">★</span> {% endif %}{{r['source_group'] or 'Unknown'}}</b></td><td>{{r['airline_code'] or r['airline'] or '—'}}</td><td>{{r['flight'] or '—'}}</td><td>{{r['sector'].replace('-',' → ')}}</td><td>{{r['timing'] or '—'}}</td><td>{{r['baggage'] or '—'}}</td><td>{{r['seats'] or '—'}}</td><td>₹{{'{:,.0f}'.format(r['fare'])}}</td><td>{{'₹{:,.0f}'.format(x['deduction']) if x['deduction'] else '—'}}</td><td class="low">₹{{'{:,.0f}'.format(x['adjusted'])}}</td></tr>{% endfor %}</table></div>
{% else %}<div class="notice">No numeric fares found for this search period.</div>{% endif %}
</div>
""",q=q,period=period,results=results,trusted_lowest=min([x["adjusted"] for x in results if x["trusted"]],default=None))
    return render_template_string(BASE,content=content)


@app.route("/backup_json")
def backup_json():
    c=db(); fares=[dict(r) for r in c.execute("SELECT * FROM fares ORDER BY id").fetchall()]; agencies=[dict(r) for r in c.execute("SELECT * FROM agencies ORDER BY id").fetchall()]; settings=[dict(r) for r in c.execute("SELECT * FROM settings ORDER BY key").fetchall()]; c.close()
    payload={"format":"skydays-market-fare-backup-v1","exported_at":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),"fares":fares,"agencies":agencies,"settings":settings}
    raw=json.dumps(payload,ensure_ascii=False,indent=2,default=str).encode("utf-8")
    import io
    return send_file(io.BytesIO(raw),mimetype="application/json",as_attachment=True,download_name="skydays_market_fare_backup.json")


@app.route("/restore",methods=["GET"])
def restore_page():
    content=render_template_string(r"""
<div class="card"><h3>Restore Market Fare Data</h3><div class="muted">Restore a previous SkyDays JSON backup or SQLite database. Existing data is never deleted. Records that already exist are skipped.</div></div>
<div class="card"><form action="/restore" method="post" enctype="multipart/form-data"><input type="file" name="backup" accept=".json,.db,.sqlite,.sqlite3" required><button type="submit">Restore / Reinstate Data</button></form></div>
<div class="card notice"><b>Safety:</b> this function only adds missing records and updates Agency Master settings. It does not clear current fares, agencies, stars or deductions.</div>
""")
    return render_template_string(BASE,content=content)


@app.post("/restore")
def restore_backup():
    f=request.files.get("backup")
    if not f or not f.filename:
        flash("Please select a JSON or SQLite backup file."); return redirect(url_for("restore_page"))
    safe=re.sub(r"[^A-Za-z0-9_.-]","_",f.filename); path=os.path.join(UPLOAD_DIR,"restore_"+safe); f.save(path)
    try:
        c=db(); fare_count=agency_count=setting_count=0
        if safe.lower().endswith(".json"):
            payload=json.loads(open(path,"r",encoding="utf-8").read())
            for a in payload.get("agencies",[]):
                c.execute("INSERT INTO agencies(name,starred,deduction,created_at) VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET starred=EXCLUDED.starred,deduction=EXCLUDED.deduction",(a.get("name"),a.get("starred",0),a.get("deduction",0),a.get("created_at")))
                agency_count+=1
            for st in payload.get("settings",[]):
                c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",(st.get("key"),st.get("value"))); setting_count+=1
            for r in payload.get("fares",[]):
                exists=c.execute("SELECT 1 FROM fares WHERE source_group=? AND sector=? AND travel_dates=? AND COALESCE(airline_code,'')=COALESCE(?, '') AND COALESCE(flight,'')=COALESCE(?, '') AND COALESCE(fare,-1)=COALESCE(?,-1) AND COALESCE(original_message,'')=COALESCE(?, '') AND COALESCE(created_at,'')=COALESCE(?, '') LIMIT 1",(r.get("source_group"),r.get("sector"),r.get("travel_dates"),r.get("airline_code"),r.get("flight"),r.get("fare"),r.get("original_message"),r.get("created_at"))).fetchone()
                if exists: continue
                c.execute("""INSERT INTO fares(source_group,sender,message_datetime,updated_time,sector,travel_dates,airline,flight,fare,baggage,seats,conditions,original_message,created_at,fare_type,timing,b2b_discount,airline_code) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(r.get("source_group"),r.get("sender",""),r.get("message_datetime",""),r.get("updated_time",""),r.get("sector",""),r.get("travel_dates",""),r.get("airline",""),r.get("flight",""),r.get("fare"),r.get("baggage",""),r.get("seats",""),r.get("conditions",""),r.get("original_message",""),r.get("created_at",""),r.get("fare_type",""),r.get("timing",""),r.get("b2b_discount",0),r.get("airline_code",""))); fare_count+=1
        else:
            sc=sqlite3.connect(path); sc.row_factory=sqlite3.Row
            for a in sc.execute("SELECT * FROM agencies").fetchall():
                c.execute("INSERT INTO agencies(name,starred,deduction,created_at) VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET starred=EXCLUDED.starred,deduction=EXCLUDED.deduction",(a["name"],a["starred"],a["deduction"],a["created_at"])); agency_count+=1
            for r in sc.execute("SELECT * FROM fares").fetchall():
                exists=c.execute("SELECT 1 FROM fares WHERE source_group=? AND sector=? AND travel_dates=? AND COALESCE(airline_code,'')=COALESCE(?, '') AND COALESCE(flight,'')=COALESCE(?, '') AND COALESCE(fare,-1)=COALESCE(?,-1) AND COALESCE(original_message,'')=COALESCE(?, '') AND COALESCE(created_at,'')=COALESCE(?, '') LIMIT 1",(r["source_group"],r["sector"],r["travel_dates"],r["airline_code"],r["flight"],r["fare"],r["original_message"],r["created_at"])).fetchone()
                if exists: continue
                c.execute("""INSERT INTO fares(source_group,sender,message_datetime,updated_time,sector,travel_dates,airline,flight,fare,baggage,seats,conditions,original_message,created_at,fare_type,timing,b2b_discount,airline_code) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",tuple(r[k] for k in ["source_group","sender","message_datetime","updated_time","sector","travel_dates","airline","flight","fare","baggage","seats","conditions","original_message","created_at","fare_type","timing","b2b_discount","airline_code"])); fare_count+=1
            sc.close()
        c.commit(); c.close(); flash(f"Restore complete. Added {fare_count} fare records; processed {agency_count} agencies. Existing data was retained.")
    except Exception as e:
        try: c.rollback(); c.close()
        except Exception: pass
        flash("Restore error: "+str(e))
    finally:
        try: os.remove(path)
        except OSError: pass
    return redirect(url_for("restore_page"))


@app.route("/agencies")
def agencies_page():
    c=db()
    agencies=c.execute("SELECT * FROM agencies ORDER BY starred DESC,name").fetchall()
    c.close()
    content=render_template_string(r"""
<div class="card">
  <div class="flex" style="align-items:flex-start">
    <div><h3>Agency Master</h3><div class="muted">Maintain one agency name for imports, starring and comparison deduction. The deduction is applied only to the comparison fare.</div></div>
  </div>
</div>
<div class="card">
  <h3>Add agency</h3>
  <form method="post" action="/agency_add" class="flex">
    <input name="name" placeholder="Agency name e.g. Glansa" required style="flex:1;min-width:220px">
    <input name="deduction" type="number" min="0" step="1" value="0" placeholder="Deduction ₹" style="width:150px">
    <label class="flex" style="gap:6px"><input type="checkbox" name="starred" value="1"> Star</label>
    <button type="submit">Add Agency</button>
  </form>
</div>
<div class="card">
  <h3>Agency settings</h3>
  <div class="muted" style="margin-bottom:10px">Example: Wallet ₹200 less, Santo ₹500 less. You can change these anytime.</div>
  <div class="scroll"><table>
    <tr><th>Agency</th><th>Starred</th><th>Comparison deduction</th><th>Action</th></tr>
    {% for a in agencies %}
    <tr>
      <form method="post" action="/agency_update">
        <td><input name="name" value="{{a['name']}}" required style="width:100%"><input type="hidden" name="original_name" value="{{a['name']}}"></td>
        <td style="text-align:center"><input type="checkbox" name="starred" value="1" {% if a['starred'] %}checked{% endif %}></td>
        <td><input name="deduction" type="number" min="0" step="1" value="{{a['deduction'] or 0}}" style="width:140px"></td>
        <td><button type="submit">Save</button></td>
      </form>
    </tr>
    {% endfor %}
    {% if not agencies %}<tr><td colspan="4">No agencies yet.</td></tr>{% endif %}
  </table></div>
</div>
<div class="card">
  <h3>Delete Fare Data for One Agency</h3>
  <div class="muted">This removes only market-fare records for the selected agency. It does <b>not</b> delete the Agency Master entry, star or deduction.</div>
  <form method="post" action="/delete_agency_data" class="flex" style="margin-top:10px;flex-wrap:wrap" onsubmit="return confirm('Delete ALL fare data for the selected agency? Agency Master will remain safe.');">
    <select name="agency" required style="flex:1;min-width:240px"><option value="">Select agency</option>{% for a in agencies %}<option value="{{a['name']}}">{% if a['starred'] %}★ {% endif %}{{a['name']}}</option>{% endfor %}</select>
    <input name="confirm_delete" placeholder="Type DELETE AGENCY DATA" required style="min-width:230px">
    <button type="submit" style="background:#b42318">Delete Agency Fare Data</button>
  </form>
</div>
""",agencies=agencies)
    return render_template_string(BASE,content=content)

@app.post("/agency_add")
def agency_add():
    name=request.form.get("name","").strip()
    if not name:
        flash("Agency name is required.")
        return redirect(url_for("agencies_page"))
    try:
        deduction=max(0,float(request.form.get("deduction",0) or 0))
    except ValueError:
        deduction=0
    starred=1 if request.form.get("starred") else 0
    c=db()
    c.execute("INSERT INTO agencies(name,starred,deduction,created_at) VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET starred=excluded.starred,deduction=excluded.deduction",(name,starred,deduction,datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    c.commit(); c.close()
    flash(f"Agency master saved: {name}.")
    return redirect(url_for("agencies_page"))

@app.post("/agency_update")
def agency_update():
    original=request.form.get("original_name","").strip()
    name=request.form.get("name","").strip()
    if not original or not name:
        flash("Agency name is required.")
        return redirect(url_for("agencies_page"))
    try:
        deduction=max(0,float(request.form.get("deduction",0) or 0))
    except ValueError:
        deduction=0
    starred=1 if request.form.get("starred") else 0
    c=db()
    try:
        c.execute("UPDATE agencies SET name=?,starred=?,deduction=? WHERE name=?",(name,starred,deduction,original))
        if original != name:
            c.execute("UPDATE fares SET source_group=? WHERE source_group=?",(name,original))
        c.commit()
        flash(f"Agency updated: {name}.")
    except Exception as exc:
        # SQLite and PostgreSQL expose different IntegrityError classes.
        # Treat a duplicate Agency Master name consistently on both databases.
        msg=str(exc).lower()
        if "unique" in msg or "duplicate key" in msg or "already exists" in msg:
            c.rollback(); flash("That agency name already exists. Choose another name.")
        else:
            c.rollback(); flash("Agency update error: "+str(exc))
    finally:
        c.close()
    return redirect(url_for("agencies_page"))

@app.post("/toggle_agency")
def toggle_agency():
    name=request.form.get("name","").strip()
    c=db(); c.execute("INSERT OR IGNORE INTO agencies(name,starred,deduction,created_at) VALUES(?,?,?,?,?)" if False else "INSERT OR IGNORE INTO agencies(name,starred,deduction,created_at) VALUES(?,?,?,?)",(name,0,0,datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    c.execute("UPDATE agencies SET starred=CASE WHEN starred=1 THEN 0 ELSE 1 END WHERE name=?",(name,)); c.commit(); c.close()
    return redirect(request.referrer or url_for("agencies_page"))

@app.post("/delete_agency_data")
def delete_agency_data():
    agency=request.form.get("agency","").strip()
    if not agency:
        flash("Please select an agency.")
        return redirect(url_for("agencies_page"))
    if request.form.get("confirm_delete","").strip().upper() != "DELETE AGENCY DATA":
        flash("Agency fare data was not deleted. Type DELETE AGENCY DATA to confirm.")
        return redirect(url_for("agencies_page"))
    c=db()
    try:
        deleted=c.execute("DELETE FROM fares WHERE source_group=?",(agency,)).rowcount
        c.commit()
        flash(f"Deleted {deleted} fare records for {agency}. Agency Master, star and deduction were kept safely.")
    except Exception as e:
        c.rollback(); flash("Agency data deletion error: "+str(e))
    finally:
        c.close()
    return redirect(url_for("agencies_page"))

@app.route("/data")
def data_page():
    from datetime import timedelta
    selected_date=request.args.get("date","").strip()
    if not selected_date:
        selected_date=(datetime.now()-timedelta(days=1)).strftime("%Y-%m-%d")
    c=db()
    count=c.execute("SELECT COUNT(*) FROM fares WHERE date(created_at)=?",(selected_date,)).fetchone()[0]
    latest=c.execute("SELECT MIN(created_at),MAX(created_at) FROM fares WHERE date(created_at)=?",(selected_date,)).fetchone()
    c.close()
    content=render_template_string(r"""
<div class="card"><h3>Daily Market Data</h3><div class="muted">Clear fare records by import date. Agency Master, starred agencies and deductions are not affected.</div></div>
<div class="card">
  <form method="get" action="/data" class="flex"><label><b>Data date</b></label><input type="date" name="date" value="{{selected_date}}" onchange="this.form.submit()"></form>
  <div class="grid" style="margin-top:14px"><div class="kpi">Records on selected date<br><b>{{count}}</b></div><div class="kpi">First import<br><b>{{latest[0] or '—'}}</b></div><div class="kpi">Last import<br><b>{{latest[1] or '—'}}</b></div><div class="kpi">Quick action<br><b>{{'Ready to clear' if count else 'No records'}}</b></div></div>
</div>
<div class="card">{% if count %}<form method="post" action="/clear_daily_data" onsubmit="return confirm('This permanently deletes only the selected market-fare records. Continue?');"><input type="hidden" name="date" value="{{selected_date}}"><label class="flex">Type <b>DELETE DATA</b> to confirm: <input name="confirm_clear" placeholder="DELETE DATA" required></label><br><button type="submit" style="background:#b42318">Clear {{selected_date}} data ({{count}} records)</button></form>{% else %}<div class="notice">No market-fare records were imported on {{selected_date}}.</div>{% endif %}</div>
<div class="card flex"><a href="/data?date={{yesterday}}"><button type="button" class="secondary">Check Yesterday</button></a><a href="/data?date={{today}}"><button type="button">Check Today</button></a></div>
""",selected_date=selected_date,count=count,latest=latest,yesterday=(datetime.now()-timedelta(days=1)).strftime("%Y-%m-%d"),today=datetime.now().strftime("%Y-%m-%d"))
    return render_template_string(BASE,content=content)


@app.post("/clear_daily_data")
def clear_daily_data():
    selected_date=request.form.get("date","").strip()
    if request.form.get("confirm_clear","").strip().upper() != "DELETE DATA":
        flash("Data was not cleared. Explicit confirmation is required.")
        return redirect(url_for("data_page",date=selected_date))
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}",selected_date):
        flash("Please select a valid data date.")
        return redirect(url_for("data_page"))
    c=db()
    deleted=c.execute("DELETE FROM fares WHERE date(created_at)=?",(selected_date,)).rowcount
    c.commit(); c.close()
    flash(f"Cleared {deleted} market-fare records imported on {selected_date}. Agency Master was not changed.")
    return redirect(url_for("data_page",date=selected_date))


@app.route("/import")
def import_page():
    c=db(); agencies=c.execute("SELECT name,starred,deduction FROM agencies ORDER BY starred DESC,name").fetchall(); c.close()
    upload_now=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    content=render_template_string(r"""
<div class="card"><h3>Import Market Fare Data</h3><div class="muted">Choose <b>Full Upload</b> when the agency sends its complete fare list. Choose <b>Add-on Upload</b> when the agency updates only selected sectors such as CCJ → DXB. Upload time is taken automatically from the system.</div></div>
<div class="import-grid">
  <div class="card">
    <form action="/import_file" method="post" enctype="multipart/form-data" novalidate>
      <div class="dropzone" id="dropzone"><strong>Drag & Drop WhatsApp TXT / ZIP</strong><br><span class="muted">or choose a file</span><br><br><input id="fileInput" type="file" name="file" accept=".txt,.zip,.md" required></div>
      <br>
      <select name="agency" required style="width:100%"><option value="">Select Agency / Group</option>{% for a in agencies %}<option value="{{a['name']}}">{% if a['starred'] %}<span>★</span> {% endif %}{{a['name']}}{% if a['deduction'] %} · ₹{{'{:,.0f}'.format(a['deduction'])}} less{% endif %}</option>{% endfor %}</select>
      <br><br>
      <div class="flex" style="gap:10px;flex-wrap:wrap">
        <label style="flex:1;min-width:220px"><b>Upload type</b><br><select name="upload_mode" required style="width:100%"><option value="full">Full Upload — replace all agency fare data</option><option value="addon">Add-on Upload — replace only uploaded sectors</option></select></label>
        <div style="flex:1;min-width:220px"><b>System upload time</b><br><span class="pill">{{upload_now}}</span></div>
      </div>
      <br><button>Review Changes</button>
    </form>
  </div>
  <div class="card">
    <form action="/import_paste" method="post">
      <textarea name="text" placeholder="Paste WhatsApp marketing text here..." required></textarea><br><br>
      <div class="flex" style="flex-wrap:wrap">
        <select name="agency" required style="flex:1;min-width:220px"><option value="">Select Agency / Group</option>{% for a in agencies %}<option value="{{a['name']}}">{% if a['starred'] %}★ {% endif %}{{a['name']}}</option>{% endfor %}</select>
        <select name="upload_mode" required style="min-width:280px"><option value="full">Full Upload</option><option value="addon">Add-on Upload</option></select>
      </div>
      <br><button>Review Changes</button>
      <div class="muted" style="margin-top:10px">System upload time: <b>{{upload_now}}</b></div>
    </form>
  </div>
</div>
<div class="card"><div class="flex"><a href="/agencies">Open Agency Master → add/edit agencies, deductions or stars</a><a href="/data"><button type="button" class="secondary">Clear Daily Data</button></a></div></div>
<script>const dz=document.getElementById('dropzone'),fi=document.getElementById('fileInput');['dragenter','dragover'].forEach(e=>dz.addEventListener(e,x=>{x.preventDefault();dz.classList.add('drag')}));['dragleave','drop'].forEach(e=>dz.addEventListener(e,x=>{x.preventDefault();dz.classList.remove('drag')}));dz.addEventListener('drop',e=>{if(e.dataTransfer.files.length)fi.files=e.dataTransfer.files});</script>
""",agencies=agencies,upload_now=upload_now)
    return render_template_string(BASE,content=content)


def _create_pending_preview(agency, mode, records, filename=""):
    if not records:
        raise ValueError("No fare records could be read from this upload.")
    upload_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    plan=_build_upload_plan(records, agency, mode, upload_time)
    token=secrets.token_urlsafe(18)
    payload={"plan":plan,"filename":filename}
    with open(_pending_path(token),"w",encoding="utf-8") as f:
        json.dump(payload,f,ensure_ascii=False,default=str)
    session["pending_import_token"]=token
    return token


def _pending_plan_from_request():
    token=request.form.get("token","").strip()
    session_token=session.get("pending_import_token","")
    if not token or token != session_token:
        raise ValueError("This import preview has expired. Please upload again.")
    path=_pending_path(token)
    if not os.path.exists(path):
        raise ValueError("This import preview has expired. Please upload again.")
    with open(path,"r",encoding="utf-8") as f:
        payload=json.load(f)
    return token,payload["plan"]


@app.post("/import_file")
def import_file():
    f=request.files.get("file"); agency=request.form.get("agency","").strip(); mode=request.form.get("upload_mode","full").strip().lower()
    if not agency: flash("Please select an Agency / Group from Agency Master."); return redirect(url_for("import_page"))
    if mode not in {"full","addon"}: mode="full"
    if not f or not f.filename: flash("Please select or drag a WhatsApp TXT/ZIP file."); return redirect(url_for("import_page"))
    safe=re.sub(r"[^A-Za-z0-9_.-]","_",f.filename); path=os.path.join(UPLOAD_DIR,safe); f.save(path)
    try:
        msgs=parse_file(path); records=records_from_messages(msgs); _create_pending_preview(agency,mode,records,safe)
        return redirect(url_for("import_preview"))
    except Exception as e:
        flash("Import error: "+str(e)); return redirect(url_for("import_page"))


@app.post("/import_paste")
def import_paste():
    text=request.form.get("text",""); agency=request.form.get("agency","").strip(); mode=request.form.get("upload_mode","full").strip().lower()
    if not text.strip(): flash("Please paste the WhatsApp marketing text."); return redirect(url_for("import_page"))
    if not agency: flash("Please select an Agency / Group from Agency Master."); return redirect(url_for("import_page"))
    if mode not in {"full","addon"}: mode="full"
    try:
        msgs=parse_whatsapp_export(text); records=records_from_messages(msgs); _create_pending_preview(agency,mode,records,agency)
        return redirect(url_for("import_preview"))
    except Exception as e:
        flash("Paste import error: "+str(e)); return redirect(url_for("import_page"))


@app.route("/import_preview")
def import_preview():
    token=session.get("pending_import_token","")
    if not token or not os.path.exists(_pending_path(token)):
        flash("No pending import preview. Please upload again.")
        return redirect(url_for("import_page"))
    try:
        with open(_pending_path(token),"r",encoding="utf-8") as f: payload=json.load(f)
        plan=payload["plan"]
    except Exception as e:
        flash("Could not open import preview: "+str(e)); return redirect(url_for("import_page"))
    changed_count=len(plan["changed"]); added_count=len(plan["added"]); removed_count=len(plan["removed"]); unchanged_count=len(plan["unchanged"])
    content=render_template_string(r"""
<div class="card">
  <div class="flex" style="align-items:flex-start;flex-wrap:wrap">
    <div><h3>Review Import Changes</h3><div class="muted">Nothing has been changed in the database yet. Review this comparison and confirm only when it is correct.</div></div>
    <div style="margin-left:auto"><span class="pill">{{'FULL UPLOAD' if plan['mode']=='full' else 'ADD-ON UPLOAD'}}</span> <span class="pill">{{plan['upload_time']}}</span></div>
  </div>
</div>
<div class="card">
  <div class="grid">
    <div class="kpi">Agency<br><b>{{plan['agency']}}</b></div>
    <div class="kpi">New records<br><b>{{plan['total_new']}}</b></div>
    <div class="kpi">Changed<br><b>{{changed_count}}</b></div>
    <div class="kpi">Added / Removed<br><b>{{added_count}} / {{removed_count}}</b></div>
  </div>
  <div class="notice" style="margin-top:12px"><b>Affected sectors:</b> {{plan['affected_markets']|join(', ') if plan['affected_markets'] else '—'}}<br>{% if plan['mode']=='full' %}Full upload will replace <b>all existing fare data</b> for this agency.{% else %}Add-on upload will replace only the above sector(s). Other sectors for this agency will remain unchanged.{% endif %}</div>
</div>
{% if changed_count %}<div class="card"><h3>Changed fares</h3><div class="scroll"><table><tr><th>Old</th><th>New</th></tr>{% for old,new in plan['changed'][:200] %}<tr><td>{{label(old)}}</td><td class="low">{{label(new)}}</td></tr>{% endfor %}</table></div>{% if changed_count>200 %}<div class="muted">Showing first 200 of {{changed_count}} changes.</div>{% endif %}</div>{% endif %}
{% if added_count %}<div class="card"><h3>New records</h3><div class="scroll"><table><tr><th>New fare</th></tr>{% for r in plan['added'][:200] %}<tr><td class="low">{{label(r)}}</td></tr>{% endfor %}</table></div>{% endif %}
{% if removed_count %}<div class="card"><h3>Records that will be removed / replaced</h3><div class="scroll"><table><tr><th>Old fare</th></tr>{% for r in plan['removed'][:200] %}<tr><td>{{label(r)}}</td></tr>{% endfor %}</table></div></div>{% endif %}
{% if unchanged_count %}<div class="card"><div class="notice">{{unchanged_count}} records are unchanged.</div></div>{% endif %}
<div class="card flex" style="justify-content:flex-end">
  <a href="/import"><button type="button" class="secondary">← Cancel / Upload Again</button></a>
  <form method="post" action="/import_confirm" onsubmit="return confirm('Apply this import now? The selected agency data will be changed according to the upload type shown above.');"><input type="hidden" name="token" value="{{token}}"><button type="submit">✓ Confirm & Apply Changes</button></form>
</div>
""",plan=plan,token=token,changed_count=changed_count,added_count=added_count,removed_count=removed_count,unchanged_count=unchanged_count,label=_record_label)
    return render_template_string(BASE,content=content)


@app.post("/import_confirm")
def import_confirm():
    try:
        token,plan=_pending_plan_from_request()
        count=_apply_upload_plan(plan)
        try: os.remove(_pending_path(token))
        except OSError: pass
        session.pop("pending_import_token",None)
        flash(f"Import applied successfully: {count} fare records saved for {plan['agency']}. Upload time: {plan['upload_time']}")
    except Exception as e:
        flash("Import could not be applied: "+str(e))
    return redirect(url_for("home"))


@app.post("/import_cancel")
def import_cancel():
    token=session.pop("pending_import_token","")
    if token:
        try: os.remove(_pending_path(token))
        except OSError: pass
    return redirect(url_for("import_page"))



# Initialize schema on import for Gunicorn/Render. It never deletes fare or agency data.
init_db()

if __name__ == "__main__":
    init_db(); app.run(host="127.0.0.1",port=8765,debug=False)
