"""PostgreSQL: spritniveau (historik i sprit_tilstand) og video (sessions).

Forbindelse styres med miljøvariabler (eller linjer i filen .env ved siden af db.py):
  PG_HOST (localhost), PG_PORT (5432), PG_USER (postgres), PG_PASS,
  PG_DB_INVENTORY (spritbeholdning), PG_DB_VIDEO (video)
Identifikation af enheden (skrives i hver række):
  BEHOLDER_ID (1), HOSPITAL ("Hospital"), LOKALE ("Lokale 1")
Grænse for "ved at løbe tør" i procent:
  LAV_GRAENSE (20)
"""
import os
from contextlib import closing

import psycopg2


def _load_env():
    """Læser KEY=værdi fra filen .env ved siden af db.py (hvis den findes).
    Miljøvariabler, der allerede er sat i terminalen, vinder over filen."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:]
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))
    except FileNotFoundError:
        pass


_load_env()

PG = dict(
    host=os.getenv("PG_HOST", "localhost"),
    port=int(os.getenv("PG_PORT", "5432")),
    user=os.getenv("PG_USER", "postgres"),
    password=os.getenv("PG_PASS", ""),
)
INV_DB = os.getenv("PG_DB_INVENTORY", "spritbeholdning")
VID_DB = os.getenv("PG_DB_VIDEO", "video")

BEHOLDER_ID = int(os.getenv("BEHOLDER_ID", "1"))
HOSPITAL = os.getenv("HOSPITAL", "Rigshospitalet")
LOKALE = os.getenv("LOKALE", "Stue 42, Biokemisk afd.")
LAV_GRAENSE = int(os.getenv("LAV_GRAENSE", "20"))


def _conn(dbname):
    return psycopg2.connect(dbname=dbname, connect_timeout=5, **PG)


def init():
    """Opretter manglende tabeller og viewet sprit_status.
    sprit_tilstand bliver en historik: primærnøglen på beholder_id fjernes, så samme
    beholder kan have mange målinger."""
    with closing(_conn(INV_DB)) as c, c, c.cursor() as cur:
        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS sprit_tilstand(
            beholder_id integer,
            hospital    text,
            lokale      text,
            indhold     integer,
            created_at  timestamp without time zone DEFAULT now());
        ALTER TABLE sprit_tilstand DROP CONSTRAINT IF EXISTS sprit_tilstand_pkey;
        CREATE INDEX IF NOT EXISTS sprit_tilstand_beholder_tid
            ON sprit_tilstand(beholder_id, created_at DESC);
        -- seneste måling pr. beholder; lav = ved at løbe tør
        CREATE OR REPLACE VIEW sprit_status AS
            SELECT DISTINCT ON (beholder_id)
                   beholder_id, hospital, lokale, indhold, created_at,
                   indhold <= {LAV_GRAENSE} AS lav
            FROM sprit_tilstand
            ORDER BY beholder_id, created_at DESC;
        """)
    with closing(_conn(VID_DB)) as c, c, c.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions(
            id          BIGSERIAL PRIMARY KEY,
            beholder_id integer,
            hospital    text,
            lokale      text,
            ts_start    timestamp without time zone DEFAULT now(),
            ts_end      timestamp without time zone,
            rub_seconds double precision,
            approved    boolean);
        -- en ældre sessions-tabel kan mangle disse kolonner
        ALTER TABLE sessions ADD COLUMN IF NOT EXISTS beholder_id integer;
        ALTER TABLE sessions ADD COLUMN IF NOT EXISTS hospital text;
        ALTER TABLE sessions ADD COLUMN IF NOT EXISTS lokale text;
        ALTER TABLE sessions ADD COLUMN IF NOT EXISTS dispense_event_id bigint;

        -- ét tryk på pumpen = én række; compliance_status = blev hænderne gnidet godt nok
        CREATE TABLE IF NOT EXISTS dispenser_telemetry(
            id                bigserial,
            beholder_id       varchar,
            hospital          varchar,
            lokale            varchar,
            compliance_status boolean,
            created_at        timestamp without time zone DEFAULT now());
        ALTER TABLE dispenser_telemetry ADD COLUMN IF NOT EXISTS id bigserial;
        CREATE UNIQUE INDEX IF NOT EXISTS dispenser_telemetry_id_uq ON dispenser_telemetry(id);
        -- en ældre nøgle på beholder_id tillader kun ét tryk pr. beholder: fjern den, id bliver nøglen
        ALTER TABLE dispenser_telemetry DROP CONSTRAINT IF EXISTS dispenser_telemetry_pkey;
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint
                           WHERE conrelid = 'dispenser_telemetry'::regclass AND contype = 'p') THEN
                ALTER TABLE dispenser_telemetry ADD PRIMARY KEY (id);
            END IF;
        END $$;

        -- sessions peger på det tryk, der udløste den
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'sessions_dispense_event_fk') THEN
                -- gamle sessions kan pege på tryk, der ikke findes: ryd dem, ellers kan koblingen ikke oprettes
                UPDATE sessions SET dispense_event_id = NULL
                 WHERE dispense_event_id IS NOT NULL
                   AND NOT EXISTS (SELECT 1 FROM dispenser_telemetry d WHERE d.id = sessions.dispense_event_id);
                ALTER TABLE sessions ADD CONSTRAINT sessions_dispense_event_fk
                    FOREIGN KEY (dispense_event_id) REFERENCES dispenser_telemetry(id);
            END IF;
        END $$;
        """)


# ---------- Spritniveau ----------
def log_level(indhold):
    """Gemmer en ny måling (historik). indhold = fyldning i procent (0-100)."""
    with closing(_conn(INV_DB)) as c, c, c.cursor() as cur:
        cur.execute("INSERT INTO sprit_tilstand(beholder_id, hospital, lokale, indhold, created_at) "
                    "VALUES (%s, %s, %s, %s, now())",
                    (BEHOLDER_ID, HOSPITAL, LOKALE, int(indhold)))


# ---------- Video ----------
def log_dispense():
    """Ét tryk på pumpen. Returnerer id'et på rækken i dispenser_telemetry."""
    with closing(_conn(VID_DB)) as c, c, c.cursor() as cur:
        cur.execute("INSERT INTO dispenser_telemetry(beholder_id, hospital, lokale, created_at) "
                    "VALUES (%s, %s, %s, now()) RETURNING id",
                    (str(BEHOLDER_ID), HOSPITAL, LOKALE))
        return cur.fetchone()[0]


def start_session(dispense_event_id=None):
    """Opretter en session knyttet til et tryk. Der gemmes ingen video eller billeder (GDPR)."""
    with closing(_conn(VID_DB)) as c, c, c.cursor() as cur:
        cur.execute("INSERT INTO sessions(beholder_id, hospital, lokale, ts_start, dispense_event_id) "
                    "VALUES (%s, %s, %s, now(), %s) RETURNING id",
                    (BEHOLDER_ID, HOSPITAL, LOKALE, dispense_event_id))
        return cur.fetchone()[0]


def end_session(session_id, rub_seconds, approved):
    """Afslutter sessionen og skriver resultatet både i sessions og på det tilhørende tryk."""
    with closing(_conn(VID_DB)) as c, c, c.cursor() as cur:
        cur.execute("UPDATE sessions SET ts_end = now(), rub_seconds = %s, approved = %s "
                    "WHERE id = %s RETURNING dispense_event_id", (rub_seconds, bool(approved), session_id))
        row = cur.fetchone()
        if row and row[0] is not None:
            cur.execute("UPDATE dispenser_telemetry SET compliance_status = %s WHERE id = %s",
                        (bool(approved), row[0]))


# ---------- Læsning til website/API ----------
def latest_levels():
    """Seneste måling pr. beholder (viewet sprit_status)."""
    with closing(_conn(INV_DB)) as c, c, c.cursor() as cur:
        cur.execute("SELECT beholder_id, hospital, lokale, indhold, created_at, lav "
                    "FROM sprit_status ORDER BY hospital, lokale, beholder_id")
        return [dict(beholder_id=r[0], hospital=r[1], lokale=r[2], indhold=r[3],
                     tidspunkt=r[4].isoformat(timespec="seconds"), lav=bool(r[5]))
                for r in cur.fetchall()]


def level_history(beholder_id, limit=200):
    """Historik for én beholder, nyeste først."""
    with closing(_conn(INV_DB)) as c, c, c.cursor() as cur:
        cur.execute("SELECT created_at, indhold FROM sprit_tilstand "
                    "WHERE beholder_id = %s ORDER BY created_at DESC LIMIT %s",
                    (beholder_id, int(limit)))
        return [dict(tidspunkt=t.isoformat(timespec="seconds"), indhold=i) for t, i in cur.fetchall()]