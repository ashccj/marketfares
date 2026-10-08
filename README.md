# SkyDays Market Fare Intelligence

Flask application for SkyDays B2B market-fare intelligence.

## Included
- Sector inventory cards
- Agency Master with star/deduction controls
- WhatsApp TXT/ZIP and paste import
- Generic fare/date/airline/flight/baggage/timing/B2B parser
- Multi-airline and multi-flight records
- Today + next 7 day comparison
- Agency-specific comparison deductions
- SkyDays reference fare comparison
- Daily market-data clearing by import date
- Render/Gunicorn compatible startup

## Local
Run `Start_SkyDays_Market_Fare.bat` on Windows.

## Render
Build command:

`pip install -r requirements.txt`

Start command:

`gunicorn app:app`

The SQLite database is intentionally excluded from Git. For a multi-user production deployment, migrate the database layer to PostgreSQL before relying on persistent online data.
