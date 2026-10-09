import os
import re
import sqlite3
import zipfile
import json
import hashlib
from datetime import datetime, timedelta
from flask import Flask, request, redirect, url_for, render_template_string, flash, send_file

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(APP_DIR, "market_fares.db")
UPLOAD_DIR = os.path.join(APP_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "skydays-market-fare-local-dev-only-change-in-render")
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "30")) * 1024 * 1024

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
            url=os.getenv("DATABASE_URL", "").strip()
            # Render may provide postgres://; normalize it for psycopg2.
            url=re.sub(r"^postgres://", "postgresql://", url)
            connect_options = {
                "cursor_factory": DictCursor,
                "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT", "10")),
                "application_name": "SkyDays Market Fare Intelligence",
            }
            # External PostgreSQL providers may require SSL; Render internal URLs
            # generally work with the provider's default settings.
            if os.getenv("DB_SSLMODE"):
                connect_options["sslmode"] = os.getenv("DB_SSLMODE")
            self.conn=psycopg2.connect(url, **connect_options)
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
        # Safe additive migrations for databases created by earlier online versions.
        # These statements never delete rows or overwrite existing fare/agency data.
        for col, typ in {
            "updated_time": "TEXT",
            "fare_type": "TEXT DEFAULT ''",
            "timing": "TEXT DEFAULT ''",
            "b2b_discount": "DOUBLE PRECISION DEFAULT 0",
            "airline_code": "TEXT DEFAULT ''",
        }.items():
            c.execute(f"ALTER TABLE fares ADD COLUMN IF NOT EXISTS {col} {typ}")
        for col, typ in {
            "starred": "INTEGER DEFAULT 0",
            "deduction": "DOUBLE PRECISION DEFAULT 0",
            "created_at": "TEXT",
        }.items():
            c.execute(f"ALTER TABLE agencies ADD COLUMN IF NOT EXISTS {col} {typ}")
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
    """Extract a canonical route from WhatsApp fare text.

    Accepts CCJ-D0H/CCJ - DOH and compact CCJ DOH headings, including
    airline/flight/baggage metadata after the route.
    """
    u = re.sub(r"[*_~`|]", " ", text or "").upper()
    u = re.sub(r"\s+", " ", u).strip()
    months = _month_words()

    # Named-city form.
    m = re.search(r"([A-Z][A-Z ]{2,35}?)\s+TO\s+([A-Z][A-Z ]{2,35}?)(?=\s*(?:\(|[-–—]|\b(?:IX|6E|AI|AK|G8|SG|UK|EK|FZ|OV|J9|QP|SV|XY|3L|WY|GF|QR|KU|EY)\b|$))", u)
    if m:
        a, b = normalize_place(m.group(1)), normalize_place(m.group(2))
        if a and b:
            return f"{a}-{b}"

    # Restrict header parsing to text before baggage/infant/fare details.
    prefix = re.split(
        r"\b(?:INFANT|INF)\s*(?:FARE|PRICE)\b|\b\d{1,2}\s*(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\b",
        u, maxsplit=1
    )[0]
    # First prefer two explicit airport codes, optionally separated by a hyphen.
    # Route codes must precede airline/flight metadata.
    route_prefix = re.split(
        r"\b(?:IX|6E|AI|AK|G8|SG|UK|EK|FZ|OV|J9|QP|SV|XY|3L|WY|GF|QR|KU|EY)\b",
        prefix, maxsplit=1
    )[0]
    route_prefix = re.sub(r"\b\d{1,3}\s*\+\s*\d{1,3}\s*KG\b", " ", route_prefix)
    codes = [c for c in re.findall(r"\b[A-Z]{3}\b", route_prefix)
             if c not in months and c not in {"THE","AND","FOR","AIR","KG","KGS","PCS","PC","BAG","FARE","TO"}]
    if len(codes) >= 2:
        return f"{codes[0]}-{codes[1]}"

    # Explicit arrow/hyphen route fallback, including multi-leg routes.
    if re.search(r"→|➝|➡|[-–—]", u):
        parts = re.split(r"\s*(?:→|➝|➡|[-–—])\s*", u)
        vals = []
        for part in parts:
            city_part = re.split(r"[\(]", part, 1)[0].strip()
            n = normalize_place(city_part)
            if n:
                vals.append(n)
                continue
            found = [c for c in re.findall(r"\b[A-Z]{3}\b", part)
                     if c not in months and c not in {"THE","AND","FOR","AIR","KG","KGS","PCS","PC","BAG","BAGGAGE","FARE"}]
            if found:
                vals.append(found[0])
        if len(vals) >= 2:
            return "-".join(vals)

    # Fallback: route tokens before first travel-date expression.
    date_match = re.search(
        r"\b(?:\d{1,2}\s*(?:TO|-)\s*\d{1,2}\s*[-/.]?\s*(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)|"
        r"\d{1,2}\s*[-/.]?\s*(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)|"
        r"(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s+\d{1,2})\b", u)
    prefix = u[:date_match.start()] if date_match else u
    codes = [c for c in re.findall(r"\b[A-Z]{3}\b", prefix)
             if c not in months and c not in {"THE","AND","FOR","AIR","KG","KGS","PCS","PC","BAG","BAGGAGE","FARE","TO"}]
    if len(codes) >= 2:
        return f"{codes[0]}-{codes[1]}"
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
    """Extract an adult fare from a line, accepting common amount formats.

    Supports ₹17,200, INR 17,200, Rs. 17,200, 17,200/-, 17200/- and 17 200.
    Avoids infant-fare lines and small numbers such as dates, flight numbers and seat counts.
    """
    raw = clean_text(text or "")
    u = raw.upper()

    # Infant fares are metadata, never the adult sector/date fare.
    if re.search(r"\bINF(?:ANT)?\s*(?:FARE|PRICE)\b", u):
        return None

    # Currency-marked amounts are unambiguous.
    currency_patterns = [
        r"(?:₹|INR\b|RS\.?\s*)\s*([\d][\d, ]{2,8})(?:\s*/\s*-)?",
    ]
    for pattern in currency_patterns:
        m = re.search(pattern, raw, re.I)
        if m:
            digits = re.sub(r"[,\s]", "", m.group(1))
            if digits.isdigit() and 4 <= len(digits) <= 7:
                amount = int(digits)
                if 1000 <= amount <= 999999:
                    return float(amount)

    # Prefer a number immediately following a travel date, optionally separated
    # by colon, dash, equals, or whitespace: 14 OCT : 17,700/-.
    month = r"(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)"
    amount = r"(\d{1,3}(?:,\d{3})+|\d{4,6})(?:\s*/\s*-)?"
    m = re.search(
        rf"\b\d{{1,2}}\s*[-/.]?\s*{month}\b\s*[:=\-–—]?\s*{amount}",
        raw, re.I
    )
    if m:
        digits = m.group(1).replace(",", "")
        if digits.isdigit() and 1000 <= int(digits) <= 999999:
            return float(int(digits))

    # Generic standalone fare candidate. Restrict to realistic fare-size numbers;
    # remove dates, flight numbers, baggage and seat labels before searching.
    cleaned = re.sub(r"\b\d{1,2}\s*[-/.]?\s*" + month + r"\b", " ", raw, flags=re.I)
    cleaned = re.sub(r"\b(?:IX|6E|AI|AK|G8|SG|UK|EK|FZ|OV|J9|QP|SV|XY|3L|WY|GF|QR|KU|EY)\s*[- ]?\s*\d{2,5}\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\b\d+\s*(?:SEATS?|SEAT|KG|KGS|PC|PCS)\b", " ", cleaned, flags=re.I)
    candidates = re.findall(r"(?<![A-Za-z0-9])(\d{1,3}(?:,\d{3})+|\d{4,6})(?:\s*/\s*-)?", cleaned, re.I)
    for candidate in candidates:
        digits = candidate.replace(",", "")
        if digits.isdigit() and 1000 <= int(digits) <= 999999:
            return float(int(digits))
    return None


def parse_airline_flight(text):
    """Extract airline and flight number while avoiding travel-date numbers."""
    u = re.sub(r"\s+", " ", text or "").upper()
    code_pattern = "|".join(sorted(AIRLINE_CODES, key=len, reverse=True))
    month_pattern = r"JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC"

    # Prefer explicit airline+flight notation, including parenthetical suffixes after fares.
    matches = list(re.finditer(
        r"\b(" + code_pattern + r")\s*[- ]?\s*(\d{2,5}(?:\s*(?:/|&|–|-)\s*(?:[A-Z0-9]{2,3}\s*)?\d{2,5})*)\b",
        u
    ))
    for m in matches:
        tail = u[m.end():m.end()+12]
        if re.match(r"\s*" + month_pattern + r"\b", tail):
            continue
        code = m.group(1)
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


def extract_fare_records(msg, diagnostics=None):
    """Extract adult fares while retaining route/date/airline context.

    `diagnostics` is an optional dict updated with line-level reasons when a fare-looking
    line cannot be linked to a sector/date/adult amount.
    """
    body = clean_text(msg.get("body", ""))
    lines = [x.strip() for x in body.split("\n") if x.strip()]
    records = []
    global_b2b = parse_b2b_discount(body)
    global_conditions = []
    ub = body.upper()
    if "NON-REFUNDABLE" in ub: global_conditions.append("NON-REFUNDABLE")
    if "SUBJECT TO AVAILABILITY" in ub: global_conditions.append("SUBJECT TO AVAILABILITY")
    current = {"sector":"", "airline":"", "airline_code":"", "flight":"", "baggage":"",
               "timing":"", "fare_type":"", "b2b_discount":global_b2b, "conditions":[]}
    rejected = diagnostics.setdefault("rejected", []) if diagnostics is not None else None

    def emit(line, line_no):
        dates, _ = parse_date_tokens(line)
        if not dates:
            return
        if not current["sector"]:
            if rejected is not None:
                rejected.append({"line": line_no, "text": line[:100], "reason": "Travel date found, but no recognizable sector header was detected above it."})
            return
        fare = parse_money(line)
        if fare is None:
            if rejected is not None and not re.search(r"\bINF(?:ANT)?\s*(?:FARE|PRICE)\b", line, re.I):
                rejected.append({"line": line_no, "text": line[:100], "reason": "Date found, but no valid adult fare amount was detected."})
            return
        fare_type = parse_fare_type(line) or current["fare_type"]
        seats = parse_seats(line)
        baggage = parse_baggage(line) or current["baggage"]
        timing = parse_timing(line) or current["timing"]
        code, flight = parse_airline_flight(line)
        # Line-specific airline/flight (e.g. (IX 343)) overrides the header context for this record.
        row_code = code or current["airline_code"]
        row_flight = flight or current["flight"]
        if code and not flight:
            row_code = code
        conditions = list(current["conditions"])+global_conditions
        if "MEAL" in line.upper() and "MEALS" not in conditions: conditions.append("MEALS")
        for tag in ("MRNG", "EVNG"):
            if re.search(r"\b"+tag+r"\b", line.upper()) and tag not in conditions: conditions.append(tag)
        for d in dates:
            records.append({
                "group":"Unspecified", "sector":current["sector"], "travel_dates":d,
                "airline":AIRLINE_NAMES.get(row_code, current["airline"] or row_code),
                "airline_code":row_code, "flight":row_flight, "fare":fare,
                "baggage":baggage, "seats":seats,
                "conditions":", ".join(dict.fromkeys([x for x in conditions if x])),
                "fare_type":fare_type, "timing":timing,
                "b2b_discount":current["b2b_discount"] or global_b2b,
                "original_message":body
            })

    for line_no, raw in enumerate(lines, 1):
        line = re.sub(r"[*_~`|]", " ", raw)
        u = re.sub(r"\s+", " ", line.upper()).strip()
        if not u or u.startswith(("HTTP://", "HTTPS://")) or re.search(r"@ALHINDONLINE|@TRAVELWALLET|@FLYUNITEDTRIP", u):
            continue
        dates = parse_date_tokens(u)[0]
        route = normalize_route(line)
        code, flight = parse_airline_flight(line)
        bg = parse_baggage(line)
        tm = parse_timing(line)
        ft = parse_fare_type(line)
        bd = parse_b2b_discount(line)

        # Route header starts a new sector context; retain route even if airline/flight appears later.
        if route:
            current["sector"] = route
            current["airline_code"] = code or ""
            current["airline"] = AIRLINE_NAMES.get(code, code) if code else ""
            current["flight"] = flight or ""
            current["baggage"] = bg or ""
            current["timing"] = tm or ""
            current["fare_type"] = ft or ""
            current["b2b_discount"] = bd or global_b2b
            current["conditions"] = []
            if "NON-REFUNDABLE" in u: current["conditions"].append("NON-REFUNDABLE")
            if "SUBJECT TO AVAILABILITY" in u: current["conditions"].append("SUBJECT TO AVAILABILITY")
            if dates:
                emit(line, line_no)
            continue

        # Subheaders such as MORNING / EVENING FLIGHT may change only timing, not sector.
        if re.search(r"\bMRNG\b|\bMORNING\b", u):
            current["conditions"] = list(dict.fromkeys(current["conditions"] + ["MRNG"]))
        if re.search(r"\bEVNG\b|\bEVENING\b", u):
            current["conditions"] = list(dict.fromkeys(current["conditions"] + ["EVNG"]))
        if code:
            current["airline_code"] = code
            current["airline"] = AIRLINE_NAMES.get(code, code)
            if flight: current["flight"] = flight
        if bg: current["baggage"] = bg
        if tm: current["timing"] = tm
        if ft: current["fare_type"] = ft
        if bd: current["b2b_discount"] = bd
        if "NON-REFUNDABLE" in u and "NON-REFUNDABLE" not in current["conditions"]: current["conditions"].append("NON-REFUNDABLE")
        if "SUBJECT TO AVAILABILITY" in u and "SUBJECT TO AVAILABILITY" not in current["conditions"]: current["conditions"].append("SUBJECT TO AVAILABILITY")
        if dates:
            emit(line, line_no)

    if diagnostics is not None:
        diagnostics["parsed_records"] = len(records)
        diagnostics["rejected_count"] = len(rejected or [])
    return records


def is_fare_message(body):
    """Recognize a fare message from individual dated adult-fare lines."""
    u = body or ""
    if not normalize_route(u):
        return False
    for line in clean_text(u).splitlines():
        if parse_date_tokens(line)[0] and parse_money(line) is not None:
            return True
    # Preserve fare-type-only marketing messages.
    return bool(parse_date_tokens(u)[0]) and bool(
        parse_fare_type(u) or re.search(r"DEAL\s+FARE|SPECIAL\s+FARE|LOWEST\s+FARE|\bSPL\b", u, re.I)
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


def save_records(records, agency, updated_time, filename=""):
    c=db(); count=0
    source=agency.strip() if agency.strip() else filename
    for x in records:
        c.execute("""
            INSERT INTO fares(source_group,sender,message_datetime,updated_time,sector,travel_dates,airline,flight,fare,baggage,seats,conditions,original_message,created_at,fare_type,timing,b2b_discount,airline_code)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,(source,x.get("sender",""),x.get("message_datetime",""),updated_time,x["sector"],x["travel_dates"],x["airline"],x["flight"],x["fare"],x["baggage"],x["seats"],x["conditions"],x["original_message"],datetime.now().strftime("%Y-%m-%d %H:%M:%S"),x.get("fare_type",""),x.get("timing",""),x.get("b2b_discount",0),x.get("airline_code","")))
        if source:
            c.execute("INSERT OR IGNORE INTO agencies(name,starred,deduction,created_at) VALUES(?,?,?,?)",(source,0,0,datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        count+=1
    c.commit(); c.close(); return count


def records_from_messages(messages, diagnostics=None):
    out=[]
    diag = diagnostics if diagnostics is not None else {}
    diag.setdefault("rejected", [])
    diag["messages_seen"] = len(messages)
    diag["messages_recognized"] = 0
    for msg in messages:
        body = msg.get("body","")
        if not is_fare_message(body):
            if body.strip() and not re.search(r"\bINF(?:ANT)?\s*(?:FARE|PRICE)\b", body, re.I):
                diag["rejected"].append({"line": 0, "text": body[:100], "reason": "Message did not contain a recognizable sector, travel date, and fare combination."})
            continue
        diag["messages_recognized"] += 1
        dt=parse_datetime(msg.get("date",""),msg.get("time",""))
        for rec in extract_fare_records(msg, diag):
            rec["sender"]=msg.get("sender",""); rec["message_datetime"]=dt; out.append(rec)
    diag["parsed_records"] = len(out)
    diag["rejected_count"] = len(diag["rejected"])
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
.compare-wrap{overflow:auto;max-height:calc(100vh - 260px);min-height:320px;border:1px solid #e2e8f0;border-radius:10px;margin-top:14px;position:relative}.compare-table{min-width:760px;border-collapse:separate;border-spacing:0}.compare-table th,.compare-table td{border-right:1px solid #e7ebf0}.compare-table thead th{position:sticky;top:0;z-index:3;background:#eef2f6}.compare-table .date-col{position:sticky;left:0;background:#f8fafc;z-index:2;min-width:110px}.compare-table thead .date-col{top:0;left:0;z-index:4;background:#eef2f6}.agency-head{min-width:150px;text-align:center}.fare-cell{text-align:center;min-width:150px;background:#fff}.fare-cell.best-fare{background:#e9f8ef;box-shadow:inset 0 0 0 2px #0b7a45}.fare-big{font-size:20px;font-weight:800;color:#0d5f38}.fare-special{font-weight:800;color:#7c3aed}.fare-meta{font-size:11px;color:#6b7280;margin-top:3px}.fare-cell.empty{color:#a0a9b4}.agency-select{min-width:190px;max-width:320px;height:38px}.sector-head{padding-bottom:12px}
.import-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}textarea{width:100%;min-height:180px}.notice{padding:10px;border-radius:8px;background:#edf7ee}.sector-page{padding-top:2px}.sector-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px;align-items:stretch}.sector-card{display:flex;min-height:132px;align-items:center;justify-content:center;text-decoration:none;color:#172334;background:linear-gradient(145deg,#ffffff,#f7fafc);border:1px solid #d9e3ec;border-radius:16px;box-shadow:0 3px 12px #17324a0d;transition:transform .16s ease,box-shadow .16s ease,border-color .16s ease;overflow:hidden}.sector-card:hover{transform:translateY(-2px);box-shadow:0 8px 24px #17324a16;border-color:#9fb6c9}.sector-main{text-align:center;line-height:1;width:100%;padding:20px 14px}.sector-route{font-size:31px;font-weight:800;letter-spacing:.4px;white-space:nowrap}.sector-arrow{display:inline-block;margin:0 8px;font-weight:500;color:#64798d}.sector-airlines{margin-top:13px;color:#718096;font-size:11px;font-weight:800;letter-spacing:1.5px;text-transform:uppercase;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.sector-empty{padding:42px 20px;text-align:center;color:#687586;border:1px dashed #cbd5df;border-radius:14px;background:#fafcfe}.trust-badge{display:inline-block;padding:4px 7px;border-radius:12px;background:#e9f8ef;color:#0b7a45;font-weight:800;font-size:11px}.untrusted-badge{display:inline-block;padding:4px 7px;border-radius:12px;background:#fff3e8;color:#a34a00;font-weight:800;font-size:11px}@media(max-width:1000px){.header-search{width:100%;margin-left:0}.header-search input{flex:1;width:auto}.grid{grid-template-columns:repeat(2,1fr)}.agency-grid,.import-grid{grid-template-columns:1fr 1fr}}@media(max-width:650px){.grid,.agency-grid,.import-grid{grid-template-columns:1fr}.wrap{padding:10px}.sector-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.sector-card{min-height:105px;border-radius:13px}.sector-route{font-size:24px}.sector-arrow{margin:0 4px}.sector-airlines{margin-top:10px;font-size:10px;letter-spacing:1px}}
</style></head><body><header><div class="wrap" style="padding-top:0;padding-bottom:0"><div class="topbar"><div class="brand"><h2>SkyDays Market Fare Intelligence</h2><div class="nav"><a href="/">Sectors</a><a href="/agencies">Agency Master</a><a href="/import">Import</a><a href="/data">Data</a><a href="/repair_missing_fares">Repair Missing Fares</a><a href="/lowest">Lowest Fare</a><a href="/backup_json">Backup</a><a href="/restore">Restore</a></div></div><form class="header-search" action="/" method="get"><input name="q" value="{{ request.args.get('q','') }}" placeholder="Search sector / destination"><button type="submit">🔍</button></form></div></div></header><div class="wrap">{% with messages=get_flashed_messages() %}{% for m in messages %}<div class="card notice">{{m}}</div>{% endfor %}{% endwith %}{{content|safe}}</div></body></html>
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
    period=request.args.get("period","7")
    today=now.date()
    if period == "month":
        filtered_dates=[d for d in unique_dates if d.year == now.year and d.month == now.month]
    elif period == "all":
        filtered_dates=unique_dates
    else:
        try:
            day_count=max(1,min(366,int(period)))
        except (TypeError, ValueError):
            day_count=7
            period="7"
        filtered_dates=unique_dates[:day_count]
    next_dates=filtered_dates
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
    <div><h3>{{sector.replace("-"," → ")}}</h3><div class="muted">Market comparison by origin + final destination · {{'all available dates' if period=='all' else ('this month' if period=='month' else 'next '+period+' dates')}}</div></div>
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
  <div class="kpi">View<br><b>{{'Selected' if selected else ('Starred' if starred_names else 'All')}}</b></div>
</div>
<div class="card">
  <div class="flex">
    <h3 style="margin-right:auto">Agency comparison</h3>
    <form method="get" class="flex" style="margin:0">
      {% if not selected %}<input type="hidden" name="starred" value="{{'1' if starred_only else '0'}}">{% endif %}
      <label for="period" class="muted">Dates</label>
      <select name="period" id="period" title="Choose date range">
        <option value="3" {% if period=='3' %}selected{% endif %}>Next 3 dates</option>
        <option value="7" {% if period=='7' %}selected{% endif %}>Next 7 dates</option>
        <option value="15" {% if period=='15' %}selected{% endif %}>Next 15 dates</option>
        <option value="30" {% if period=='30' %}selected{% endif %}>Next 30 dates</option>
        <option value="month" {% if period=='month' %}selected{% endif %}>This month</option>
        <option value="all" {% if period=='all' %}selected{% endif %}>All dates</option>
      </select>
      <select name="agency" multiple size="1" class="agency-select" title="Optional: select agencies for this comparison">
        {% for a in agencies %}<option value="{{a['name']}}" {% if a['name'] in active_names %}selected{% endif %}>{{'★ ' if a['starred'] else ''}}{{a['name']}}</option>{% endfor %}
      </select>
      <button type="submit">Compare</button>
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

''',sector=sector,rows=rows8,agencies=agencies,selected=selected,starred_only=starred_only,period=period,lowest=lowest,starred_names=set(starred_names),active_names=active_names,compare_names=compare_names,comparison=comparison,adjustment_names=adjustment_names,comparison_fare=comparison_fare,route_origin=route_origin,route_destination=route_destination)
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


@app.route("/repair_missing_fares", methods=["GET", "POST"])
def repair_missing_fares():
    """Repair existing NULL/invalid fare rows from their stored original message.

    This only fills missing fare values and related blank metadata. It does not
    delete records, change agency names, or change Agency Master deductions.
    """
    c = db()
    if request.method == "GET":
        rows = c.execute(
            "SELECT id,source_group,sector,travel_dates,airline_code,flight,fare,original_message "
            "FROM fares WHERE fare IS NULL OR fare<=0 ORDER BY id"
        ).fetchall()
        c.close()
        content = render_template_string(r"""
<div class="card">
  <h3>Repair Missing Fare Records</h3>
  <p>This scans existing records with a missing/invalid fare and attempts to recover the adult fare from the original stored WhatsApp message.</p>
  <div class="notice">This repair does not delete fares or modify Agency Master settings/deductions. Review the result after running it.</div>
  <div class="grid" style="margin-top:12px">
    <div class="kpi">Missing fare rows<br><b>{{rows|length}}</b></div>
  </div>
  <form method="post" onsubmit="return confirm('Repair missing fares from the original stored messages? Existing non-empty fares and Agency Master settings will not be changed.');">
    <button type="submit">Repair Missing Fares</button>
    <a href="/"><button type="button" class="secondary">Back to Markets</button></a>
  </form>
</div>
<div class="card"><h3>Sample affected records</h3><div class="scroll"><table><tr><th>ID</th><th>Agency</th><th>Sector</th><th>Date</th><th>Flight</th><th>Fare</th></tr>
{% for r in rows[:100] %}<tr><td>{{r['id']}}</td><td>{{r['source_group']}}</td><td>{{r['sector']}}</td><td>{{r['travel_dates']}}</td><td>{{r['airline_code'] or ''}} {{r['flight'] or ''}}</td><td>{{r['fare'] if r['fare'] is not none else '—'}}</td></tr>{% endfor %}
</table></div></div>
""", rows=rows)
        return render_template_string(BASE, content=content)

    rows = c.execute(
        "SELECT id,source_group,sector,travel_dates,airline_code,flight,fare,baggage,seats,conditions,"
        "original_message,fare_type,timing,b2b_discount,airline FROM fares "
        "WHERE fare IS NULL OR fare<=0 ORDER BY id"
    ).fetchall()
    checked = updated = ambiguous = 0
    for row in rows:
        checked += 1
        body = row["original_message"] or ""
        if not body.strip():
            continue
        try:
            parsed = extract_fare_records({"body": body})
        except Exception:
            continue
        target_sector = route_market(row["sector"] or "")
        target_date = re.sub(r"\s+", " ", (row["travel_dates"] or "").upper().strip())
        target_code = (row["airline_code"] or "").upper().strip()
        target_flight = re.sub(r"\s+", "", (row["flight"] or "").upper())
        candidates = []
        for rec in parsed:
            if route_market(rec.get("sector", "")) != target_sector:
                continue
            if re.sub(r"\s+", " ", (rec.get("travel_dates") or "").upper().strip()) != target_date:
                continue
            rec_code = (rec.get("airline_code") or "").upper().strip()
            rec_flight = re.sub(r"\s+", "", (rec.get("flight") or "").upper())
            if target_code and rec_code and target_code != rec_code:
                continue
            if target_flight and rec_flight and target_flight != rec_flight:
                continue
            if rec.get("fare") is not None and float(rec["fare"]) > 0:
                candidates.append(rec)
        unique = {(float(x["fare"]), x.get("baggage",""), x.get("airline_code",""), x.get("flight","")) for x in candidates}
        if len(unique) != 1:
            if len(unique) > 1:
                ambiguous += 1
            continue
        rec = candidates[0]
        c.execute(
            "UPDATE fares SET fare=?, baggage=CASE WHEN COALESCE(baggage,'')='' THEN ? ELSE baggage END, "
            "airline_code=CASE WHEN COALESCE(airline_code,'')='' THEN ? ELSE airline_code END, "
            "flight=CASE WHEN COALESCE(flight,'')='' THEN ? ELSE flight END, "
            "airline=CASE WHEN COALESCE(airline,'')='' THEN ? ELSE airline END, "
            "fare_type=CASE WHEN COALESCE(fare_type,'')='' THEN ? ELSE fare_type END, "
            "timing=CASE WHEN COALESCE(timing,'')='' THEN ? ELSE timing END "
            "WHERE id=? AND (fare IS NULL OR fare<=0)",
            (rec["fare"], rec.get("baggage",""), rec.get("airline_code",""), rec.get("flight",""),
             rec.get("airline",""), rec.get("fare_type",""), rec.get("timing",""), row["id"])
        )
        if c.execute("SELECT 1 FROM fares WHERE id=? AND fare>0", (row["id"],)).fetchone():
            updated += 1
    c.commit()
    c.close()
    flash(f"Missing-fare repair complete. Checked {checked} rows; repaired {updated}; ambiguous matches skipped {ambiguous}. Agency Master and existing non-empty fares were not changed.")
    return redirect(url_for("repair_missing_fares"))


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


def import_diagnostic_summary(diagnostics):
    rejected = diagnostics.get("rejected", [])
    reasons = []
    for item in rejected:
        reason = item.get("reason", "")
        if reason and reason not in reasons:
            reasons.append(reason)
        if len(reasons) >= 3:
            break
    summary = f"Messages checked: {diagnostics.get('messages_seen', 0)}; recognized: {diagnostics.get('messages_recognized', 0)}; fare records parsed: {diagnostics.get('parsed_records', 0)}; lines needing attention: {diagnostics.get('rejected_count', len(rejected))}."
    if reasons:
        summary += " Possible reasons: " + " ".join(reasons)
    summary += " Check that each adult fare line has a recognizable sector header above it, a travel date (e.g. 14 OCT), and an adult amount (e.g. 17,200/-). Infant fare and baggage weight are not adult fares."
    return summary


@app.route("/import")
def import_page():
    c=db(); agencies=c.execute("SELECT name,starred,deduction FROM agencies ORDER BY starred DESC,name").fetchall(); c.close()
    content=render_template_string(r"""
<div class="card"><h3>Import Market Fare Data</h3><div class="muted">Select the agency from Agency Master. The same master name is used for starring and comparison deductions. Updated Time is optional.</div><div class="notice" style="margin-top:10px"><b>Fare recognition:</b> amounts can use commas or no commas (for example 26,200/-, 22200/-, ₹19,200). Each fare needs a recognizable sector header and travel date. Airline/flight numbers and seat counts are retained where present. Infant fares and baggage weights are not treated as adult fares. After importing, this page reports how many records were saved and likely reasons for any lines that were skipped.</div></div>
<div class="import-grid">
  <div class="card">
    <form action="/import_file" method="post" enctype="multipart/form-data" novalidate>
      <div class="dropzone" id="dropzone"><strong>Drag & Drop WhatsApp TXT / ZIP</strong><br><span class="muted">or choose a file</span><br><br><input id="fileInput" type="file" name="file" accept=".txt,.zip,.md" required></div>
      <br>
      <select name="agency" required style="width:100%"><option value="">Select Agency / Group</option>{% for a in agencies %}<option value="{{a['name']}}">{{'★ ' if a['starred'] else ''}}{{a['name']}}{% if a['deduction'] %} · ₹{{'{:,.0f}'.format(a['deduction'])}} less{% endif %}</option>{% endfor %}</select>
      <br><br><input name="updated_time" placeholder="Updated time e.g. Today 11:00 AM (optional)" style="width:100%"><br><br>
      <button>Import File</button>
    </form>
  </div>
  <div class="card">
    <form action="/import_paste" method="post">
      <textarea name="text" placeholder="Paste WhatsApp marketing text here..." required></textarea><br><br>
      <div class="flex">
        <select name="agency" required style="flex:1"><option value="">Select Agency / Group</option>{% for a in agencies %}<option value="{{a['name']}}">{{'★ ' if a['starred'] else ''}}{{a['name']}}</option>{% endfor %}</select>
        <input name="updated_time" placeholder="Updated time (optional)">
        <button>Import Pasted Text</button>
      </div>
    </form>
  </div>
</div>
<div class="card"><div class="flex"><a href="/agencies">Open Agency Master → Add agencies, edit deductions or star agencies</a><a href="/data"><button type="button" class="secondary">Clear Daily Data</button></a></div></div>
<script>const dz=document.getElementById('dropzone'),fi=document.getElementById('fileInput');['dragenter','dragover'].forEach(e=>dz.addEventListener(e,x=>{x.preventDefault();dz.classList.add('drag')}));['dragleave','drop'].forEach(e=>dz.addEventListener(e,x=>{x.preventDefault();dz.classList.remove('drag')}));dz.addEventListener('drop',e=>{if(e.dataTransfer.files.length)fi.files=e.dataTransfer.files});</script>
""",agencies=agencies)
    return render_template_string(BASE,content=content)


@app.post("/import_file")
def import_file():
    f=request.files.get("file"); agency=request.form.get("agency","").strip(); updated=request.form.get("updated_time","").strip()
    if not agency: flash("Please select an Agency / Group from Agency Master."); return redirect(url_for("import_page"))
    if not f or not f.filename: flash("Please select or drag a WhatsApp TXT/ZIP file."); return redirect(url_for("import_page"))
    safe=re.sub(r"[^A-Za-z0-9_.-]","_",f.filename); path=os.path.join(UPLOAD_DIR,safe); f.save(path)
    try:
        msgs=parse_file(path)
        diagnostics={}
        records=records_from_messages(msgs,diagnostics)
        # Default import date is today's date; preserve an explicitly entered date/time.
        if not updated:
            updated=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        count=save_records(records,agency,updated,os.path.splitext(safe)[0]) if records else 0
        if count:
            flash(f"Import successful: saved {count} fare records from {diagnostics.get('messages_recognized',0)} recognized message(s).")
        else:
            flash("No fares were saved. " + import_diagnostic_summary(diagnostics))
        if diagnostics.get("rejected_count"):
            flash("Import diagnostics: " + import_diagnostic_summary(diagnostics))
    except Exception as e: flash("Import error: "+str(e))
    return redirect(url_for("import_page"))


@app.post("/import_paste")
def import_paste():
    text=request.form.get("text",""); agency=request.form.get("agency","").strip(); updated=request.form.get("updated_time","").strip()
    if not text.strip(): flash("Please paste the WhatsApp marketing text."); return redirect(url_for("import_page"))
    if not agency: flash("Please select an Agency / Group from Agency Master."); return redirect(url_for("import_page"))
    try:
        msgs=parse_whatsapp_export(text)
        diagnostics={}
        records=records_from_messages(msgs,diagnostics)
        # Default import date is today's date; preserve an explicitly entered date/time.
        if not updated:
            updated=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        count=save_records(records,agency,updated,agency) if records else 0
        if count:
            flash(f"Import successful: saved {count} fare records from {diagnostics.get('messages_recognized',0)} recognized message(s).")
        else:
            flash("No fares were saved. " + import_diagnostic_summary(diagnostics))
        if diagnostics.get("rejected_count"):
            flash("Import diagnostics: " + import_diagnostic_summary(diagnostics))
    except Exception as e: flash("Paste import error: "+str(e))
    return redirect(url_for("import_page"))


@app.route("/health")
def health_check():
    """Render health check. Returns unhealthy if the configured database is unavailable."""
    c = None
    try:
        c = db()
        c.execute("SELECT 1").fetchone()
        return {"status": "ok", "database": "connected"}, 200
    except Exception:
        app.logger.exception("Health check failed")
        return {"status": "error", "database": "unavailable"}, 503
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass


# Initialize schema on import for Gunicorn/Render. This is additive and preserves data.
init_db()

if __name__ == "__main__":
    # Local run only. Render should start this app with: gunicorn app:app
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8765")), debug=False)
