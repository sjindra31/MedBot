import discord
import re
import gspread
import os
import json
import time
import threading
import asyncio
import urllib.request
import urllib.error
import html
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta
from collections import defaultdict

load_dotenv()

# Portal notifications + Discord-authoritative rank sync + payout tools: v3.9.1
# ================= CONFIG =================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = 1439473833273856120  # text channel if needed
SPREADSHEET_ID = "1aXhvKbXqXlHEu94dQctSJP8jk6tLvNWkrYHZyDYcI0c"
GUILD_ID = 861362652710174740  # your real server (guild) ID

# Optional WordPress push sync. Leave blank until LVMC Core v0.3+ is configured.
WORDPRESS_SYNC_URL = os.getenv("LVMC_WORDPRESS_SYNC_URL", "").strip()
WORDPRESS_SYNC_SECRET = os.getenv("LVMC_WORDPRESS_SYNC_SECRET", "").strip()
WORDPRESS_NOTIFICATION_URL = os.getenv("LVMC_WORDPRESS_NOTIFICATION_URL", "").strip()
if not WORDPRESS_NOTIFICATION_URL and WORDPRESS_SYNC_URL:
    WORDPRESS_NOTIFICATION_URL = WORDPRESS_SYNC_URL.rsplit("/", 1)[0] + "/discord-notifications"
WORDPRESS_NOTIFICATION_ACK_URL = WORDPRESS_NOTIFICATION_URL.rstrip("/") + "/ack" if WORDPRESS_NOTIFICATION_URL else ""
WORDPRESS_ORG_URL = WORDPRESS_SYNC_URL.rsplit("/", 1)[0] + "/medbot-org" if WORDPRESS_SYNC_URL else ""
WORDPRESS_RANK_URL = WORDPRESS_SYNC_URL.rsplit("/", 1)[0] + "/medbot-ranks" if WORDPRESS_SYNC_URL else ""
WORDPRESS_NOTIFICATION_POLL_SECONDS = max(30, int(os.getenv("LVMC_WORDPRESS_NOTIFICATION_POLL_SECONDS", "60") or 60))
WORDPRESS_NOTIFICATION_CHANNEL_ID = int(os.getenv("LVMC_WORDPRESS_NOTIFICATION_CHANNEL_ID", "0") or 0)
WORDPRESS_ORG_SYNC_SECONDS = max(60, int(os.getenv("LVMC_WORDPRESS_ORG_SYNC_SECONDS", "300") or 300))
WORDPRESS_RANK_SYNC_SECONDS = max(60, int(os.getenv("LVMC_WORDPRESS_RANK_SYNC_SECONDS", "300") or 300))

VALID_RANKS = [
    # Intern Medic is stored as "Unranked" in the medical spreadsheet.
    "Unranked",
    "Field Medic",
    "Junior Medic",
    "Senior Medic",
    "Paramedic",
    "Doctor",
]

# ================= SHEET HEADERS =================
# Reports sheet uses columns A:O.
# Column O stores the Discord IDs for the medics listed in column B, in the
# same order. Existing A:N report data remains in the same columns.
REPORT_HEADERS = [
    "Timestamp",
    "Medics",
    "Job Name",
    "Duration",
    "Points",
    "Clients",
    "Participant Names",
    "Description",
    "Report Date",
    "Message Link",
    "Reporter ID",
    "Reporter Name",
    "Message ID",
    "Channel ID",
    "Medic Discord IDs",
]

# Master Log keeps the existing A:Q columns and appends identity/promotion
# counters in R:W so old formulas/data are not shifted.
MASTER_HEADERS = [
    "Medic",
    "Rank",
    "Total Jobs",
    "Total Raw Points",
    "Total Adjusted Points",
    "Total Hours",
    "Raid",
    "LMPF",
    "Healing",
    "Rev/Spar",
    "Escort",
    "World Boss",
    "Arc",
    "Mission",
    "Hosted Event",
    "Host Training Event",
    "Participate In Training Event",
    "Discord ID",
    "Total Clients",
    "Raid/Defense Count",
    "Hosted Event Count",
    "Host Training Count",
    "Training Participation Count",
]

# Monthly leaderboard should only use columns A:I
LEADERBOARD_HEADERS = [
    "Rank",
    "Medic",
    "Raw Points",
    "Jobs Logged",
    "Rank Title",
    "Bonus Multiplier",
    "Adjusted Points",
    "Total Pay",
    "Total Ryo",
]

# Shared job options for report submission and report editing.
JOB_OPTIONS = [
    ("Raid / Defend", "Raid / Defend"),
    ("Duty with LMPF", "LMPF"),
    ("Healing Lowbies", "Healing Lowbies"),
    ("Rev Spar", "Rev Spar"),
    ("Escort", "Escort"),
    ("World Boss", "World Boss"),
    ("ARC I (15, 20, 27, 30)", "ARC I"),
    ("ARC II (30, 40, 50)", "ARC II"),
    ("ARC III (60)", "ARC III"),
    ("Mission", "Daily Mission"),
    ("Run An Event", "Hosted Event"),
    ("Host Training Event", "Host Training Event"),
    ("Participate In Training Event", "Participate In Training Event"),
]


# ================= RYO / PAYOUT ELIGIBILITY (PER-MONTH) =================
RYO_FILE = "monthly_ryo.json"
# Backward compatible formats:
#   Legacy: {"2026-08": 25000}
#   v3.9+:  {"2026-08": {"pool": 25000, "opted_out": ["123456789..."]}}
monthly_ryo = {}
DEFAULT_RYO = 25000  # fallback if month not set


def load_monthly_ryo():
    global monthly_ryo
    if os.path.exists(RYO_FILE):
        try:
            with open(RYO_FILE, "r") as f:
                monthly_ryo = json.load(f) or {}
        except Exception:
            monthly_ryo = {}
    else:
        monthly_ryo = {}


def save_monthly_ryo():
    with open(RYO_FILE, "w") as f:
        json.dump(monthly_ryo, f, indent=2)


def get_month_key(year: int, month: int) -> str:
    return f"{year}-{month:02d}"


def _get_month_payout_config(year: int, month: int) -> dict:
    """Return normalized payout config without mutating legacy JSON entries."""
    key = get_month_key(year, month)
    value = monthly_ryo.get(key, DEFAULT_RYO)

    if isinstance(value, dict):
        try:
            pool = int(value.get("pool", DEFAULT_RYO))
        except (TypeError, ValueError):
            pool = DEFAULT_RYO

        opted_out = []
        for discord_id in value.get("opted_out", []) or []:
            did = clean_sheet_id(discord_id)
            if did and did not in opted_out:
                opted_out.append(did)

        return {"pool": pool, "opted_out": opted_out}

    try:
        pool = int(value)
    except (TypeError, ValueError):
        pool = DEFAULT_RYO

    return {"pool": pool, "opted_out": []}


def _save_month_payout_config(year: int, month: int, *, pool=None, opted_out=None):
    """Write the new structured format while preserving unspecified values."""
    current = _get_month_payout_config(year, month)

    if pool is not None:
        current["pool"] = int(pool)

    if opted_out is not None:
        current["opted_out"] = sorted({
            clean_sheet_id(discord_id)
            for discord_id in opted_out
            if clean_sheet_id(discord_id)
        })

    monthly_ryo[get_month_key(year, month)] = current
    save_monthly_ryo()
    return current


def get_bank_ryo(year: int, month: int) -> int:
    return int(_get_month_payout_config(year, month)["pool"])


def get_payout_optouts(year: int, month: int) -> set:
    """Discord IDs that declined payment for this month."""
    return set(_get_month_payout_config(year, month)["opted_out"])


def set_bank_ryo(year: int, month: int, amount: int):
    return _save_month_payout_config(year, month, pool=amount)


def set_medic_payout_eligibility(year: int, month: int, payout_identity, eligible: bool):
    """
    Store monthly payout eligibility using a stable Discord ID when available.
    Legacy/unlinked former Medics can fall back to a name:<normalized-name> key.
    """
    identity = clean_sheet_id(payout_identity)
    if not identity:
        raise ValueError("A valid payout identity is required.")

    opted_out = get_payout_optouts(year, month)
    if eligible:
        opted_out.discard(identity)
    else:
        opted_out.add(identity)

    return _save_month_payout_config(year, month, opted_out=opted_out)


# Load on startup so payout settings persist across restarts.
load_monthly_ryo()

# ================= GOOGLE SHEETS AUTH =================
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
CREDS = Credentials.from_service_account_file(
    os.getenv("GOOGLE_APPLICATION_CREDENTIALS"),
    scopes=SCOPES,
)

GC = gspread.authorize(CREDS)

# IMPORTANT: open spreadsheet ONCE and reuse it
SS = GC.open_by_key(SPREADSHEET_ID)
SHEET = SS.worksheet("Reports")  # raw report log worksheet


# ================= SHEET HELPERS =================
def ensure_reports_sheet_shape():
    """Keep the Reports sheet locked to the expected A:O structure."""
    SHEET.update("A1:O1", [REPORT_HEADERS])
    SHEET.resize(cols=len(REPORT_HEADERS))


def ensure_master_sheet_shape():
    """
    Safely ensure the Master Log has the canonical A:W header row.

    Important:
    - If row 1 is already a header row, refresh it in place.
    - If row 1 contains actual medic data, INSERT a new row above it so no
      medic data is overwritten.
    - If row 1 is blank, simply write the headers there.
    """
    try:
        master = SS.worksheet("Leaf Master Medical Log")
        master.resize(cols=len(MASTER_HEADERS))

        first_row_values = master.get("A1:W1")
        first_row = first_row_values[0] if first_row_values else []
        padded = list(first_row[:len(MASTER_HEADERS)])
        if len(padded) < len(MASTER_HEADERS):
            padded.extend([""] * (len(MASTER_HEADERS) - len(padded)))

        first_cell = str(padded[0] or "").strip()
        second_cell = str(padded[1] or "").strip()
        normalized_first = first_cell.lower()
        normalized_second = second_cell.lower()

        header_names_lower = {str(h).strip().lower() for h in MASTER_HEADERS}
        header_hits = sum(
            1 for value in padded
            if str(value or "").strip().lower() in header_names_lower
        )
        looks_like_header = normalized_first == "medic" or header_hits >= 3

        known_rank_values = {
            "unranked",
            "intern medic",
            "field",
            "field medic",
            "junior",
            "junior medic",
            "senior",
            "senior medic",
            "paramedic",
            "doctor",
        }
        looks_like_data = bool(first_cell) and normalized_second in known_rank_values

        if looks_like_header:
            master.update("A1:W1", [MASTER_HEADERS])
            print("✅ Master Log header row refreshed.")
        elif looks_like_data:
            master.insert_row(
                MASTER_HEADERS,
                index=1,
                value_input_option="USER_ENTERED",
            )
            print("🛠️ Master Log headers were missing; inserted a new header row without overwriting medic data.")
        elif not any(str(v or "").strip() for v in padded):
            master.update("A1:W1", [MASTER_HEADERS])
            print("✅ Master Log header row created.")
        else:
            raise RuntimeError(
                "Leaf Master Medical Log row 1 is neither a recognizable header nor a recognizable medic data row. "
                "No automatic header repair was performed."
            )

        invalidate_master_cache()

    except gspread.exceptions.WorksheetNotFound:
        pass


def _column_letter(column_number: int) -> str:
    """Convert a 1-based column number to an A1-style column letter."""
    result = ""
    n = int(column_number)
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        result = chr(65 + remainder) + result
    return result


def get_records_with_expected_headers(ws, expected_headers):
    """
    Read a fixed-width worksheet without relying on gspread's strict
    ``expected_headers`` validation.

    The bot owns the physical column layout for Reports and the Master Log,
    so each value is mapped by column position to ``expected_headers``. This
    keeps reads working if Google Sheets/gspread temporarily sees stale, blank,
    duplicated, or partially-updated header metadata. The header row itself is
    ignored for mapping; the canonical header list in the bot is authoritative.
    """
    if not expected_headers:
        return []

    end_col = _column_letter(len(expected_headers))
    values = ws.get(f"A1:{end_col}")
    if not values or len(values) <= 1:
        return []

    records = []
    width = len(expected_headers)
    for raw_row in values[1:]:
        row = list(raw_row[:width])
        if len(row) < width:
            row.extend([""] * (width - len(row)))

        # Ignore completely blank rows.
        if not any(str(value).strip() for value in row):
            continue

        records.append(dict(zip(expected_headers, row)))

    return records


# ================= API READ CACHING (QUOTA FIX) =================
_RAW_CACHE = {"ts": 0.0, "records": []}
_RAW_CACHE_TTL = 30  # seconds
_RAW_LOCK = threading.Lock()

_MASTER_CACHE = {"ts": 0.0, "records": []}
_MASTER_TTL = 30  # seconds
_MASTER_LOCK = threading.Lock()


def get_raw_records_cached(force: bool = False):
    """Return Reports records, but cache for TTL seconds to avoid quota spikes."""
    now = time.time()
    with _RAW_LOCK:
        if (not force) and _RAW_CACHE["records"] and (now - _RAW_CACHE["ts"] < _RAW_CACHE_TTL):
            return _RAW_CACHE["records"]

        records = get_records_with_expected_headers(SHEET, REPORT_HEADERS)
        _RAW_CACHE["records"] = records
        _RAW_CACHE["ts"] = now
        return records


def invalidate_raw_cache():
    with _RAW_LOCK:
        _RAW_CACHE["ts"] = 0.0
        _RAW_CACHE["records"] = []


def get_master_records_cached(master_ws, force: bool = False):
    """Cache master worksheet get_all_records too."""
    now = time.time()
    with _MASTER_LOCK:
        if (not force) and _MASTER_CACHE["records"] and (now - _MASTER_CACHE["ts"] < _MASTER_TTL):
            return _MASTER_CACHE["records"]

        recs = get_records_with_expected_headers(master_ws, MASTER_HEADERS)
        _MASTER_CACHE["records"] = recs
        _MASTER_CACHE["ts"] = now
        return recs


def invalidate_master_cache():
    with _MASTER_LOCK:
        _MASTER_CACHE["ts"] = 0.0
        _MASTER_CACHE["records"] = []


# ================= REBUILD THROTTLE (QUOTA FIX) =================
_LAST_REBUILD = {"master": 0.0, "leaderboard": 0.0}
REBUILD_COOLDOWN = 60  # seconds


def should_run(key: str) -> bool:
    now = time.time()
    if now - _LAST_REBUILD[key] >= REBUILD_COOLDOWN:
        _LAST_REBUILD[key] = now
        return True
    return False


# ================= NAME NORMALIZATION =================
def load_medic_normalization():
    """Reads medic names from cached raw records & builds a normalization map."""
    records = get_raw_records_cached()
    mapping = {}

    for row in records:
        medics = row.get("Medics", "")
        for m in [x.strip() for x in medics.split(",") if x.strip()]:
            key = m.lower()
            if key not in mapping:
                mapping[key] = m  # store original capitalization
    return mapping


def normalize_medic_name(name: str, mapping: dict) -> str:
    """Converts a medic name to correct capitalization."""
    key = name.lower()
    if key in mapping:
        return mapping[key]  # already known medic → use canonical case

    proper = name.title()
    mapping[key] = proper
    return proper


def clean_sheet_id(value) -> str:
    """Normalize Discord/snowflake IDs read back from Google Sheets."""
    text = str(value or "").strip()
    if text.startswith("'"):
        text = text[1:]
    if text.endswith(".0"):
        text = text[:-2]
    return text


def split_names(value: str):
    """Split comma-separated names, also allowing the word 'and'."""
    return [x.strip() for x in re.split(r",|\band\b", str(value or "")) if x.strip()]


def report_medic_pairs(row: dict):
    """Return [(display_name, discord_id), ...] for a Reports row.

    Old reports have no Medic Discord IDs, so their IDs are returned as blank.
    New reports keep the names and IDs positionally aligned.
    """
    names = split_names(row.get("Medics", ""))
    raw_ids = str(row.get("Medic Discord IDs", "") or "")
    ids = [clean_sheet_id(x) for x in raw_ids.split(",")] if raw_ids else []
    return [(name, ids[i] if i < len(ids) else "") for i, name in enumerate(names)]


def serialize_medic_discord_ids(discord_ids) -> str:
    """Store multiple Discord IDs in one Sheets cell without numeric precision loss."""
    joined = ", ".join(str(x).strip() for x in discord_ids if str(x).strip())
    return f"'{joined}" if joined else ""


def build_known_discord_ids(master_records=None, raw_records=None):
    """Build a best-known name -> Discord ID map from Master Log and Reports."""
    mapping = {}

    for row in master_records or []:
        name = str(row.get("Medic", "") or "").strip()
        did = clean_sheet_id(row.get("Discord ID", ""))
        if name and did:
            mapping[name.lower()] = did

    for row in raw_records or []:
        for name, did in report_medic_pairs(row):
            if name and did:
                mapping[name.lower()] = did

    return mapping


def medic_identity_key(name: str, discord_id: str = "", known_ids=None) -> str:
    """Prefer Discord ID as the stable identity; fall back to normalized name."""
    did = clean_sheet_id(discord_id)
    if not did and known_ids:
        did = clean_sheet_id(known_ids.get(str(name).lower(), ""))
    if did:
        return f"id:{did}"
    return f"name:{str(name).strip().lower()}"


def member_display_name(member) -> str:
    """Return the best server-facing display name for a Discord user/member."""
    return (
        getattr(member, "display_name", None)
        or getattr(member, "global_name", None)
        or getattr(member, "name", None)
        or str(member)
    )


def linked_medic_for_discord_id(discord_id: int):
    """Return a linked Master Log Medic row for a Discord member, or None."""
    did = clean_sheet_id(discord_id)
    try:
        master = SS.worksheet("Leaf Master Medical Log")
        records = get_master_records_cached(master, force=True)
    except gspread.exceptions.WorksheetNotFound:
        return None

    for row in records:
        row_id = clean_sheet_id(row.get("Discord ID", ""))
        if row_id and row_id == did:
            return row
    return None


def payout_medic_rows():
    """Return Master Log Medic rows for payout selection, including former Discord members."""
    try:
        master = SS.worksheet("Leaf Master Medical Log")
        return get_master_records_cached(master, force=True)
    except gspread.exceptions.WorksheetNotFound:
        return []


def find_payout_medic_by_name(medic_name: str):
    """
    Find a Medic from the Master Medical Log by name.

    Exact case-insensitive matches win. A unique partial match is accepted so
    admins can still type names manually if Discord autocomplete is unavailable.
    """
    query = str(medic_name or "").strip()
    if not query:
        return None

    rows = payout_medic_rows()
    exact = []
    partial = []

    for row in rows:
        name = str(row.get("Medic", "") or "").strip()
        if not name:
            continue
        if name.lower() == query.lower():
            exact.append(row)
        elif query.lower() in name.lower():
            partial.append(row)

    if exact:
        return exact[0]
    if len(partial) == 1:
        return partial[0]
    return None


def payout_identity_for_medic_row(row: dict) -> str:
    """
    Prefer the Medic's stored Discord ID even if they have left the server.
    If an old Master Log row has no Discord ID, fall back to a normalized name.
    """
    did = clean_sheet_id(row.get("Discord ID", ""))
    if did:
        return did

    medic_name = str(row.get("Medic", "") or "").strip().lower()
    if medic_name:
        return f"name:{medic_name}"
    return ""


def payout_identity_is_opted_out(identity_key: str, medic_name: str, opted_out_values) -> bool:
    """Check both modern Discord-ID opt-outs and legacy name fallback opt-outs."""
    opted_out_values = set(opted_out_values or [])
    did = _identity_discord_id(identity_key)
    if did and did in opted_out_values:
        return True

    name_key = f"name:{str(medic_name or '').strip().lower()}"
    return bool(name_key != "name:" and name_key in opted_out_values)


def _identity_discord_id(identity_key: str) -> str:
    """Extract a Discord ID from an id:123 identity key."""
    if str(identity_key).startswith("id:"):
        return clean_sheet_id(str(identity_key)[3:])
    return ""


def calculate_monthly_payout_snapshot(year: int, month: int) -> dict:
    """
    Calculate payout-only data for a month.

    Medical activity is never removed. Opted-out Medics keep their BP/jobs/rank
    exactly as normal; only the payout denominator excludes them.
    """
    records = get_raw_records_cached()
    bank_ryo = get_bank_ryo(year, month)
    opted_out_ids = get_payout_optouts(year, month)

    rank_by_identity, master_display, known_ids = _load_rank_identity_maps(records)
    points_by_identity, jobs_by_identity, report_display = _collect_monthly_stats(
        records, year, month, known_ids
    )

    adjusted = {}
    for key, raw_pts in points_by_identity.items():
        rank = rank_by_identity.get(key, "Unranked")
        adjusted[key] = raw_pts * bonus_from_rank(rank)

    eligible_adjusted_total = 0.0
    for key, adj in adjusted.items():
        medic = report_display.get(key) or master_display.get(key) or key
        if payout_identity_is_opted_out(key, medic, opted_out_ids):
            continue
        eligible_adjusted_total += adj

    rows = []
    for key in sorted(adjusted, key=adjusted.get, reverse=True):
        medic = report_display.get(key) or master_display.get(key) or key
        did = _identity_discord_id(key)
        opted_out = payout_identity_is_opted_out(key, medic, opted_out_ids)
        adj = adjusted[key]

        if opted_out:
            pay = 0.0
        else:
            share = adj / eligible_adjusted_total if eligible_adjusted_total > 0 else 0
            pay = round(share * bank_ryo, 2)

        rows.append({
            "identity": key,
            "discord_id": did,
            "medic": medic,
            "raw_points": points_by_identity[key],
            "adjusted_points": adj,
            "jobs": jobs_by_identity[key],
            "rank": rank_by_identity.get(key, "Unranked"),
            "opted_out": opted_out,
            "pay": pay,
        })

    return {
        "year": year,
        "month": month,
        "pool": bank_ryo,
        "opted_out_ids": opted_out_ids,
        "eligible_adjusted_total": eligible_adjusted_total,
        "rows": rows,
        "eligible_count": sum(1 for row in rows if not row["opted_out"]),
        "opted_out_count": sum(1 for row in rows if row["opted_out"]),
    }


# ================= POINT CALCULATOR =================
def calculate_points(job_name: str, duration: int, clients: int) -> int:
    job_name = job_name.lower().strip()

    # Training events — fixed points
    if "host training event" in job_name:
        return 35
    if "participate in training event" in job_name:
        return 20

    # Hosted Event — 30 points, must be at least 60 min and 5+ clients
    if "hosted event" in job_name:
        if duration >= 60 and clients >= 5:
            return 30
        return 0

    if "raid" in job_name or "defend" in job_name:
        return 5 + 4 * (duration // 15)
    if "criminal" in job_name or "lmpf" in job_name:
        return 3
    if "healing" in job_name or "lowbie" in job_name or "farm" in job_name:
        return clients + (duration // 15)
    if "rev" in job_name or "spar" in job_name:
        return clients + (duration // 15)
    if "escort" in job_name:
        return 2
    if "boss" in job_name or "world" in job_name:
        return clients * 3

    # Categorized arcs — points are awarded per client
    if job_name == "arc i":
        return clients * 10
    if job_name == "arc ii":
        return clients * 20
    if job_name == "arc iii":
        return clients * 30

    # Legacy Arc reports keep the old behavior if an older report is edited.
    if job_name == "arc":
        return clients * 30

    if "mission" in job_name or "daily" in job_name:
        return clients * 3

    return 0


# ================= RANK BONUS =================
def bonus_from_rank(rank: str) -> float:
    """Return bonus multiplier based on Rank string from Master Log."""
    r = (rank or "").lower()
    if "doctor" in r:
        return 3.0
    if "paramedic" in r:
        return 2.0
    if "senior" in r:
        return 1.5
    if "junior" in r:
        return 1.25
    if "field" in r:
        return 1.15
    return 1.0  # Unranked / unknown


# ================= MONTHLY LEADERBOARD =================
def _load_rank_identity_maps(records):
    """Return rank/display-name maps keyed by stable medic identity."""
    rank_by_identity = {}
    display_by_identity = {}
    master_records = []

    try:
        master = SS.worksheet("Leaf Master Medical Log")
        master_records = get_master_records_cached(master)
    except gspread.exceptions.WorksheetNotFound:
        pass

    known_ids = build_known_discord_ids(master_records, records)
    for row in master_records:
        name = str(row.get("Medic", "") or "").strip()
        if not name:
            continue
        key = medic_identity_key(name, row.get("Discord ID", ""), known_ids)
        rank_by_identity[key] = row.get("Rank", "Unranked") or "Unranked"
        display_by_identity[key] = name

    return rank_by_identity, display_by_identity, known_ids


def _collect_monthly_stats(records, year: int, month: int, known_ids):
    points_by_identity = defaultdict(int)
    jobs_by_identity = defaultdict(int)
    display_by_identity = {}

    for row in records:
        date_str = str(row.get("Report Date", "")).strip()
        if not date_str:
            continue

        try:
            d = datetime.strptime(date_str, "%m/%d/%Y")
        except ValueError:
            continue

        if d.year != year or d.month != month:
            continue

        try:
            points = int(row.get("Points", 0))
        except (ValueError, TypeError):
            points = 0

        for medic_name, discord_id in report_medic_pairs(row):
            key = medic_identity_key(medic_name, discord_id, known_ids)
            points_by_identity[key] += points
            jobs_by_identity[key] += 1
            display_by_identity[key] = medic_name

    return points_by_identity, jobs_by_identity, display_by_identity



def _collect_monthly_hours(records, year: int, month: int, known_ids):
    """Collect service hours for the requested month using the same stable medic identities."""
    hours_by_identity = defaultdict(float)

    for row in records:
        date_str = str(row.get("Report Date", "") or "").strip()
        if not date_str:
            continue
        try:
            d = datetime.strptime(date_str, "%m/%d/%Y")
        except ValueError:
            continue
        if d.year != year or d.month != month:
            continue

        duration_text = str(row.get("Duration", "0") or "0")
        match = re.search(r"\d+", duration_text)
        minutes = int(match.group(0)) if match else 0
        hours = minutes / 60.0

        for medic_name, discord_id in report_medic_pairs(row):
            key = medic_identity_key(medic_name, discord_id, known_ids)
            hours_by_identity[key] += hours

    return hours_by_identity


def update_leaderboard():
    records = get_raw_records_cached()
    now = datetime.now()
    current_month = now.month
    current_year = now.year
    current_month_name = now.strftime("%b")

    sheet_title = f"Leaderboard - {current_month_name} {current_year}"
    BANK_RYO = get_bank_ryo(current_year, current_month)

    rank_by_identity, master_display, known_ids = _load_rank_identity_maps(records)

    try:
        leaderboard_sheet = SS.worksheet(sheet_title)
    except gspread.exceptions.WorksheetNotFound:
        leaderboard_sheet = SS.add_worksheet(
            title=sheet_title, rows="200", cols=str(len(LEADERBOARD_HEADERS))
        )
        leaderboard_sheet.update("A1:I1", [LEADERBOARD_HEADERS])

    points_by_identity, jobs_by_identity, report_display = _collect_monthly_stats(
        records, current_year, current_month, known_ids
    )

    if not points_by_identity:
        print("⚠️ No data for current month — leaderboard left unchanged")
        return [], {}

    adjusted_points = {}
    for key, raw in points_by_identity.items():
        rank = rank_by_identity.get(key, "Unranked")
        adjusted_points[key] = raw * bonus_from_rank(rank)

    opted_out_ids = get_payout_optouts(current_year, current_month)
    eligible_adjusted = sum(
        adj
        for key, adj in adjusted_points.items()
        if not payout_identity_is_opted_out(
            key,
            report_display.get(key) or master_display.get(key) or key,
            opted_out_ids,
        )
    )

    sorted_keys = sorted(adjusted_points, key=adjusted_points.get, reverse=True)
    output = [LEADERBOARD_HEADERS]
    return_data = []
    return_jobs = {}

    for i, key in enumerate(sorted_keys, start=1):
        medic = report_display.get(key) or master_display.get(key) or key
        raw = points_by_identity[key]
        jobs = jobs_by_identity[key]
        rank_title = rank_by_identity.get(key, "Unranked")
        mult = bonus_from_rank(rank_title)
        adj = adjusted_points[key]

        did = _identity_discord_id(key)
        opted_out = payout_identity_is_opted_out(key, medic, opted_out_ids)
        if opted_out:
            pay = "Opted Out"
        else:
            share = adj / eligible_adjusted if eligible_adjusted > 0 else 0
            pay = round(share * BANK_RYO, 2)

        output.append([
            i, medic, raw, jobs, rank_title, mult, round(adj, 2), pay,
            BANK_RYO if i == 1 else "",
        ])
        return_data.append((medic, adj))
        return_jobs[medic] = jobs

    leaderboard_sheet.clear()
    leaderboard_sheet.update("A1", output)
    leaderboard_sheet.resize(cols=len(LEADERBOARD_HEADERS))

    print(f"✅ Leaderboard updated for {current_month_name} {current_year} (Pool: {BANK_RYO})")
    return return_data, return_jobs


def update_single_leaderboard(year: int, month: int):
    records = get_raw_records_cached()
    BANK_RYO = get_bank_ryo(year, month)
    sheet_title = f"Leaderboard - {datetime(year, month, 1).strftime('%b')} {year}"

    rank_by_identity, master_display, known_ids = _load_rank_identity_maps(records)

    try:
        leaderboard_sheet = SS.worksheet(sheet_title)
    except gspread.exceptions.WorksheetNotFound:
        leaderboard_sheet = SS.add_worksheet(
            sheet_title, rows=200, cols=len(LEADERBOARD_HEADERS)
        )

    points_by_identity, jobs_by_identity, report_display = _collect_monthly_stats(
        records, year, month, known_ids
    )

    if not points_by_identity:
        print(f"⚠️ No data for {sheet_title} — skipping update")
        return

    adjusted = {}
    for key, raw_pts in points_by_identity.items():
        rank = rank_by_identity.get(key, "Unranked")
        adjusted[key] = raw_pts * bonus_from_rank(rank)

    opted_out_ids = get_payout_optouts(year, month)
    eligible_adj = sum(
        adj
        for key, adj in adjusted.items()
        if not payout_identity_is_opted_out(
            key,
            report_display.get(key) or master_display.get(key) or key,
            opted_out_ids,
        )
    )

    output = [LEADERBOARD_HEADERS]
    sorted_keys = sorted(adjusted, key=adjusted.get, reverse=True)

    for i, key in enumerate(sorted_keys, start=1):
        medic = report_display.get(key) or master_display.get(key) or key
        raw_pts = points_by_identity[key]
        jobs = jobs_by_identity[key]
        rank_title = rank_by_identity.get(key, "Unranked")
        mult = bonus_from_rank(rank_title)
        adj_pts = adjusted[key]

        did = _identity_discord_id(key)
        opted_out = payout_identity_is_opted_out(key, medic, opted_out_ids)
        if opted_out:
            pay = "Opted Out"
        else:
            share = adj_pts / eligible_adj if eligible_adj else 0
            pay = round(share * BANK_RYO, 2)

        output.append([
            i, medic, raw_pts, jobs, rank_title, mult, round(adj_pts, 2), pay,
            BANK_RYO if i == 1 else "",
        ])

    leaderboard_sheet.clear()
    leaderboard_sheet.update("A1", output)
    leaderboard_sheet.resize(cols=len(LEADERBOARD_HEADERS))
    print(f"✅ Updated leaderboard: {sheet_title} (Pool: {BANK_RYO})")


def update_all_leaderboards():
    """Rebuild leaderboard sheets for every month found in the raw log."""
    records = get_raw_records_cached()

    months = set()
    for row in records:
        date_str = str(row.get("Report Date", "")).strip()
        if not date_str:
            continue
        try:
            d = datetime.strptime(date_str, "%m/%d/%Y")
            months.add((d.year, d.month))
        except ValueError:
            continue

    for year, month in sorted(months):
        title = f"Leaderboard - {datetime(year, month, 1).strftime('%b')} {year}"
        print(f"📅 Updating leaderboard for: {title}")
        update_single_leaderboard(year, month)


# ================= MASTER LOG (LIFETIME) =================
def update_master_log():
    """Rebuild lifetime stats, preferring Discord ID as each medic's identity."""
    master_was_created = False

    try:
        master = SS.worksheet("Leaf Master Medical Log")
        existing_records = get_master_records_cached(master)
    except gspread.exceptions.WorksheetNotFound:
        master = SS.add_worksheet(
            title="Leaf Master Medical Log", rows="300", cols=str(len(MASTER_HEADERS))
        )
        master.update("A1:W1", [MASTER_HEADERS])
        invalidate_master_cache()
        existing_records = []
        master_was_created = True

    records = get_raw_records_cached()
    name_map = load_medic_normalization()
    known_ids = build_known_discord_ids(existing_records, records)

    # Preserve manually-set ranks, but key them by Discord ID whenever one is known.
    existing_ranks = {}
    existing_display = {}
    for row in existing_records:
        raw_name = str(row.get("Medic", "") or "").strip()
        if not raw_name:
            continue
        normalized = normalize_medic_name(raw_name, name_map)
        key = medic_identity_key(normalized, row.get("Discord ID", ""), known_ids)
        existing_ranks[key] = row.get("Rank", "Unranked") or "Unranked"
        existing_display[key] = normalized

    print("DEBUG: raw rows =", len(records))
    print("DEBUG: first row keys =", records[0].keys() if records else "NO ROWS")

    raw_points = defaultdict(int)
    jobs = defaultdict(int)
    hours = defaultdict(float)
    hours_by_type = defaultdict(lambda: defaultdict(float))
    counts_by_type = defaultdict(lambda: defaultdict(int))
    total_clients = defaultdict(int)
    display_names = dict(existing_display)
    discord_ids = {}

    for row in records:
        job_name = str(row.get("Job Name", "")).lower()
        try:
            points = int(row.get("Points", 0))
        except (ValueError, TypeError):
            points = 0

        duration_str = str(row.get("Duration", "0 min"))
        try:
            minutes = int(duration_str.split()[0])
        except (ValueError, IndexError):
            minutes = 0
        job_hours = minutes / 60.0
        try:
            clients_for_job = int(row.get("Clients", 0) or 0)
        except (ValueError, TypeError):
            clients_for_job = parse_clients_count(row.get("Participant Names", ""))

        for medic_name, row_discord_id in report_medic_pairs(row):
            normalized = normalize_medic_name(medic_name, name_map)
            key = medic_identity_key(normalized, row_discord_id, known_ids)
            did = clean_sheet_id(row_discord_id) or clean_sheet_id(known_ids.get(normalized.lower(), ""))

            raw_points[key] += points
            jobs[key] += 1
            hours[key] += job_hours
            total_clients[key] += clients_for_job
            display_names[key] = normalized
            if did:
                discord_ids[key] = did

            if "raid" in job_name or "defend" in job_name:
                hours_by_type[key]["Raid"] += job_hours
                counts_by_type[key]["Raid/Defense"] += 1
            elif "lmpf" in job_name:
                hours_by_type[key]["LMPF"] += job_hours
            elif "healing" in job_name or "lowbie" in job_name:
                hours_by_type[key]["Healing"] += job_hours
            elif "rev" in job_name or "spar" in job_name:
                hours_by_type[key]["Rev/Spar"] += job_hours
            elif "escort" in job_name:
                hours_by_type[key]["Escort"] += job_hours
            elif "world" in job_name:
                hours_by_type[key]["World Boss"] += job_hours
            elif "arc" in job_name:
                hours_by_type[key]["Arc"] += job_hours
            elif "mission" in job_name:
                hours_by_type[key]["Mission"] += job_hours
            elif "hosted event" in job_name:
                hours_by_type[key]["Hosted Event"] += job_hours
                counts_by_type[key]["Hosted Event"] += 1
            elif "host training event" in job_name:
                hours_by_type[key]["Host Training Event"] += job_hours
                counts_by_type[key]["Host Training Event"] += 1
            elif "participate in training event" in job_name:
                hours_by_type[key]["Participate In Training Event"] += job_hours
                counts_by_type[key]["Participate In Training Event"] += 1

    output = [MASTER_HEADERS]

    for key in sorted(jobs.keys(), key=lambda k: display_names.get(k, k).lower()):
        medic = display_names.get(key, key)
        rank = existing_ranks.get(key, "Unranked")
        bonus_mult = bonus_from_rank(rank)
        adjusted = raw_points[key] * bonus_mult
        did = discord_ids.get(key, key[3:] if key.startswith("id:") else "")

        output.append([
            medic,
            rank,
            jobs[key],
            raw_points[key],
            adjusted,
            round(hours[key], 2),
            round(hours_by_type[key]["Raid"], 2),
            round(hours_by_type[key]["LMPF"], 2),
            round(hours_by_type[key]["Healing"], 2),
            round(hours_by_type[key]["Rev/Spar"], 2),
            round(hours_by_type[key]["Escort"], 2),
            round(hours_by_type[key]["World Boss"], 2),
            round(hours_by_type[key]["Arc"], 2),
            round(hours_by_type[key]["Mission"], 2),
            round(hours_by_type[key]["Hosted Event"], 2),
            round(hours_by_type[key]["Host Training Event"], 2),
            round(hours_by_type[key]["Participate In Training Event"], 2),
            f"'{did}" if did else "",
            total_clients[key],
            counts_by_type[key]["Raid/Defense"],
            counts_by_type[key]["Hosted Event"],
            counts_by_type[key]["Host Training Event"],
            counts_by_type[key]["Participate In Training Event"],
        ])

    if len(output) <= 1:
        print("🚫 Master log rebuild aborted — no parsed data")
        return False

    # If an existing populated sheet somehow yields no rank map, avoid wiping it.
    # A brand-new sheet is safe to initialize with Unranked (= Intern Medic).
    if existing_records and not existing_ranks and not master_was_created:
        print("🚫 Aborting master log rebuild — existing ranks could not be preserved")
        return False

    master.clear()
    master.update("A1", output)
    master.resize(cols=len(MASTER_HEADERS))
    invalidate_master_cache()
    print("✅ Leaf Master Medical Log updated")
    return True


def set_rank_in_master_log(medic_name: str, rank: str) -> str:
    master = SS.worksheet("Leaf Master Medical Log")

    # Normalize medic name
    name_map = load_medic_normalization()
    medic = normalize_medic_name(medic_name.strip(), name_map)

    # Normalize rank to canonical value
    rank = next((r for r in VALID_RANKS if r.lower() == rank.lower()), None)
    if not rank:
        raise ValueError("Invalid rank")

    records = get_master_records_cached(master)

    target_row = None
    for i, row in enumerate(records, start=2):
        existing = str(row.get("Medic", "")).strip()
        if not existing:
            continue

        existing_norm = normalize_medic_name(existing, name_map)
        if existing_norm.lower() == medic.lower():
            target_row = i
            break

    if target_row is None:
        master.append_row([medic, rank], value_input_option="USER_ENTERED")
        master.resize(cols=len(MASTER_HEADERS))
        invalidate_master_cache()
        return f"Added **{medic}** with rank **{rank}**."
    else:
        master.update_cell(target_row, 2, rank)
        invalidate_master_cache()
        return f"Updated **{medic}** rank to **{rank}**."


def link_medic_discord_id(medic_name: str, discord_id: int) -> str:
    """Link an existing/legacy Master Log medic name to a stable Discord ID."""
    # Refresh the canonical A:W header row before reading. This is safe for
    # existing data and prevents stale sheet headers from blocking /linkmedic.
    ensure_master_sheet_shape()
    master = SS.worksheet("Leaf Master Medical Log")
    invalidate_master_cache()
    records = get_master_records_cached(master, force=True)
    name_map = load_medic_normalization()
    medic = normalize_medic_name(medic_name.strip(), name_map)

    target_row = None
    for i, row in enumerate(records, start=2):
        existing = str(row.get("Medic", "") or "").strip()
        if existing and existing.lower() == medic.lower():
            target_row = i
            break

    if target_row is None:
        raise ValueError(f"No Master Log medic found matching '{medic_name}'.")

    discord_col = MASTER_HEADERS.index("Discord ID") + 1
    master.update_cell(target_row, discord_col, f"'{discord_id}")
    invalidate_master_cache()
    return f"Linked **{medic}** to Discord user ID `{discord_id}`."



# ================= WORDPRESS / LVMC CORE SYNC =================
def wordpress_sync_configured() -> bool:
    return bool(WORDPRESS_SYNC_URL and WORDPRESS_SYNC_SECRET)


def _report_url(value) -> str:
    text = str(value or "")
    match = re.search(r'https://discord\\.com/channels/[^"\\s)]+', text)
    return match.group(0) if match else (text if text.startswith("https://") else "")


def _collect_recent_activity(records, known_ids, limit: int = 8):
    """Collect each linked Medic's most recent raw reports for website profiles."""
    out = defaultdict(list)
    for row in reversed(records):
        for medic_name, discord_id in report_medic_pairs(row):
            key = medic_identity_key(medic_name, discord_id, known_ids)
            if len(out[key]) >= limit:
                continue
            points = row.get("Points", 0) or 0
            try:
                points = float(points)
            except (TypeError, ValueError):
                points = 0
            clients = row.get("Clients", 0) or 0
            try:
                clients = int(float(clients))
            except (TypeError, ValueError):
                clients = 0
            out[key].append({
                "date": str(row.get("Report Date", "") or "").strip(),
                "job": str(row.get("Job Name", "") or "").strip(),
                "duration": str(row.get("Duration", "") or "").strip(),
                "points": points,
                "clients": clients,
                "url": _report_url(row.get("Message Link", "")),
            })
    return out


def build_wordpress_sync_payload():
    """Build one batch payload with lifetime, current-month, and previous-month stats."""
    master = SS.worksheet("Leaf Master Medical Log")
    records = get_master_records_cached(master, force=True)
    raw_records = get_raw_records_cached(force=True)

    now = datetime.now()
    monthly_period = now.strftime("%Y-%m")
    previous_date = now.replace(day=1) - timedelta(days=1)
    previous_month_period = previous_date.strftime("%Y-%m")

    rank_by_identity, _master_display, known_ids = _load_rank_identity_maps(raw_records)
    monthly_raw, monthly_jobs, _monthly_display = _collect_monthly_stats(
        raw_records, now.year, now.month, known_ids
    )
    monthly_hours = _collect_monthly_hours(raw_records, now.year, now.month, known_ids)
    previous_raw, previous_jobs, _previous_display = _collect_monthly_stats(
        raw_records, previous_date.year, previous_date.month, known_ids
    )
    previous_hours = _collect_monthly_hours(
        raw_records, previous_date.year, previous_date.month, known_ids
    )
    recent_activity = _collect_recent_activity(raw_records, known_ids, limit=8)

    medics = []
    skipped_without_id = []

    for row in records:
        medic_name = str(row.get("Medic", "") or "").strip()
        discord_id = clean_sheet_id(row.get("Discord ID", ""))
        if not medic_name:
            continue
        if not discord_id:
            skipped_without_id.append(medic_name)
            continue

        identity_key = medic_identity_key(medic_name, discord_id, known_ids)
        sheet_rank = str(row.get("Rank", "Unranked") or "Unranked")
        month_raw_points = monthly_raw.get(identity_key, 0)
        month_jobs = monthly_jobs.get(identity_key, 0)
        month_adjusted_points = month_raw_points * bonus_from_rank(sheet_rank)
        month_hours = round(monthly_hours.get(identity_key, 0.0), 2)

        previous_raw_points = previous_raw.get(identity_key, 0)
        previous_jobs_count = previous_jobs.get(identity_key, 0)
        previous_adjusted_points = previous_raw_points * bonus_from_rank(sheet_rank)
        previous_hours_count = round(previous_hours.get(identity_key, 0.0), 2)

        medics.append({
            "discord_id": discord_id,
            "medic_name": medic_name,
            "sheet_rank": sheet_rank,
            "official_rank": sheet_rank,
            "total_jobs": row.get("Total Jobs", 0) or 0,
            "raw_points": row.get("Total Raw Points", 0) or 0,
            "adjusted_points": row.get("Total Adjusted Points", 0) or 0,
            "total_hours": row.get("Total Hours", 0) or 0,
            "current_month": {
                "period": monthly_period,
                "raw_points": month_raw_points,
                "adjusted_points": round(month_adjusted_points, 2),
                "jobs": month_jobs,
                "hours": month_hours,
            },
            "previous_month": {
                "period": previous_month_period,
                "raw_points": previous_raw_points,
                "adjusted_points": round(previous_adjusted_points, 2),
                "jobs": previous_jobs_count,
                "hours": previous_hours_count,
            },
            "total_clients": row.get("Total Clients", 0) or 0,
            "raid_defense_count": row.get("Raid/Defense Count", 0) or 0,
            "hosted_event_count": row.get("Hosted Event Count", 0) or 0,
            "host_training_count": row.get("Host Training Count", 0) or 0,
            "training_participation_count": row.get("Training Participation Count", 0) or 0,
            "raid_hours": row.get("Raid", 0) or 0,
            "lmpf_hours": row.get("LMPF", 0) or 0,
            "healing_hours": row.get("Healing", 0) or 0,
            "rev_spar_hours": row.get("Rev/Spar", 0) or 0,
            "escort_hours": row.get("Escort", 0) or 0,
            "world_boss_hours": row.get("World Boss", 0) or 0,
            "arc_hours": row.get("Arc", 0) or 0,
            "mission_hours": row.get("Mission", 0) or 0,
            "hosted_event_hours": row.get("Hosted Event", 0) or 0,
            "host_training_hours": row.get("Host Training Event", 0) or 0,
            "training_participation_hours": row.get("Participate In Training Event", 0) or 0,
            "recent_activity": recent_activity.get(identity_key, []),
        })

    return {
        "source": "medbot",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "current_month_period": monthly_period,
        "previous_month_period": previous_month_period,
        "medics": medics,
    }, skipped_without_id


def push_wordpress_sync():
    """Push lifetime medic statistics to LVMC Core's authenticated REST endpoint."""
    if not wordpress_sync_configured():
        return {
            "ok": False,
            "skipped": True,
            "message": "WordPress sync is not configured (LVMC_WORDPRESS_SYNC_URL / SECRET).",
        }

    try:
        payload, skipped_without_id = build_wordpress_sync_payload()
        if not payload["medics"]:
            return {
                "ok": False,
                "skipped": True,
                "message": "No Master Log medics currently have Discord IDs.",
                "without_discord_id": skipped_without_id,
            }

        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            WORDPRESS_SYNC_URL,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-LVMC-Sync-Secret": WORDPRESS_SYNC_SECRET,
                "User-Agent": "LVMC-MedBot/1.0",
            },
        )

        with urllib.request.urlopen(request, timeout=15) as response:
            response_body = response.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(response_body) if response_body else {}
            except json.JSONDecodeError:
                parsed = {"raw_response": response_body}

        result = {
            "ok": True,
            "sent": len(payload["medics"]),
            "without_discord_id": skipped_without_id,
            "wordpress": parsed,
        }
        print(
            "✅ WordPress sync complete:",
            f"sent={result['sent']}",
            f"unlinked-sheet-medics={len(skipped_without_id)}",
        )
        return result

    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
        print(f"⚠️ WordPress sync HTTP {e.code}: {error_body}")
        return {"ok": False, "message": f"HTTP {e.code}", "response": error_body}
    except Exception as e:
        print(f"⚠️ WordPress sync failed: {e}")
        return {"ok": False, "message": str(e)}




# ================= DISCORD -> MASTER LOG / WORDPRESS RANK SYNC =================
RANK_ORDER = [
    ("Intern Medic", "Unranked"),
    ("Field Medic", "Field Medic"),
    ("Junior Medic", "Junior Medic"),
    ("Senior Medic", "Senior Medic"),
    ("Paramedic", "Paramedic"),
    ("Doctor", "Doctor"),
]
_rank_sync_lock = asyncio.Lock()


def wordpress_rank_configured() -> bool:
    return bool(WORDPRESS_RANK_URL and WORDPRESS_SYNC_SECRET)


def wordpress_rank_status():
    if not wordpress_rank_configured():
        return {
            "ok": False,
            "configured": False,
            "url": WORDPRESS_RANK_URL or "(not configured)",
            "error": "WordPress sync URL / secret are not fully configured.",
        }
    try:
        data = _wordpress_json_request(WORDPRESS_RANK_URL, "GET")
        return {
            "ok": True,
            "configured": True,
            "url": WORDPRESS_RANK_URL,
            "guild_id": str(data.get("guild_id", "") or ""),
            "rank_roles": data.get("rank_roles", {}) if isinstance(data.get("rank_roles", {}), dict) else {},
            "last_sync": data.get("last_sync", {}) if isinstance(data.get("last_sync", {}), dict) else {},
        }
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
        return {"ok": False, "configured": True, "url": WORDPRESS_RANK_URL, "error": f"HTTP {e.code}: {body[:300]}"}
    except Exception as e:
        return {"ok": False, "configured": True, "url": WORDPRESS_RANK_URL, "error": str(e)}


def _master_rank_links():
    ensure_master_sheet_shape()
    master = SS.worksheet("Leaf Master Medical Log")
    invalidate_master_cache()
    records = get_master_records_cached(master, force=True)

    linked = []
    without_id = 0
    for sheet_row, row in enumerate(records, start=2):
        medic_name = str(row.get("Medic", "") or "").strip()
        if not medic_name:
            continue
        discord_id = clean_sheet_id(row.get("Discord ID", ""))
        if not discord_id:
            without_id += 1
            continue
        linked.append({
            "sheet_row": sheet_row,
            "medic_name": medic_name,
            "discord_id": discord_id,
            "sheet_rank": str(row.get("Rank", "Unranked") or "Unranked"),
        })
    return linked, without_id


def _apply_master_rank_changes(changes):
    if not changes:
        return

    data = [
        {
            "range": f"'Leaf Master Medical Log'!B{item['sheet_row']}",
            "values": [[item["new_sheet_rank"]]],
        }
        for item in changes
    ]
    SS.values_batch_update({
        "valueInputOption": "USER_ENTERED",
        "data": data,
    })
    invalidate_master_cache()


def _discord_rank_for_member(member, rank_roles):
    member_role_ids = {str(role.id) for role in getattr(member, "roles", [])}
    best = None

    # Ascending order means a higher matching role replaces a lower one.
    for public_rank, sheet_rank in RANK_ORDER:
        configured = {
            str(role_id).strip()
            for role_id in (rank_roles.get(public_rank, []) or [])
            if str(role_id).strip().isdigit()
        }
        if configured and member_role_ids.intersection(configured):
            best = (public_rank, sheet_rank)

    return best


async def sync_discord_ranks_once(target_discord_ids=None, push_website=True):
    if not wordpress_rank_configured():
        return {"ok": False, "skipped": True, "message": "WordPress rank sync is not configured."}

    async with _rank_sync_lock:
        config = await asyncio.to_thread(wordpress_rank_status)
        if not config.get("ok"):
            return {"ok": False, "message": str(config.get("error", "Could not read WordPress rank settings."))}

        rank_roles = config.get("rank_roles", {})
        if not any(rank_roles.get(rank) for rank, _sheet_rank in RANK_ORDER):
            return {
                "ok": False,
                "skipped": True,
                "message": "No Discord Medic rank role IDs are configured in WordPress → LVMC Portal.",
            }

        try:
            guild_id = int(str(config.get("guild_id", "") or GUILD_ID))
        except ValueError:
            return {"ok": False, "message": "WordPress returned an invalid Medical Corps Discord guild ID."}

        guild = bot.get_guild(guild_id)
        if guild is None:
            return {"ok": False, "message": f"MedBot is not connected to Medical Corps Discord guild {guild_id}."}

        links, without_id = await asyncio.to_thread(_master_rank_links)
        targets = {str(x) for x in target_discord_ids} if target_discord_ids else None
        if targets is not None:
            links = [row for row in links if row["discord_id"] in targets]

        changes = []
        checked = 0
        not_in_guild = 0
        without_rank_role = 0

        for row in links:
            try:
                discord_id_int = int(row["discord_id"])
            except ValueError:
                continue

            member = guild.get_member(discord_id_int)
            if member is None:
                try:
                    member = await guild.fetch_member(discord_id_int)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    member = None

            if member is None:
                not_in_guild += 1
                continue

            checked += 1
            detected = _discord_rank_for_member(member, rank_roles)
            if not detected:
                # Avoid destructive demotions if a role mapping is temporarily missing.
                without_rank_role += 1
                continue

            public_rank, new_sheet_rank = detected
            old_sheet_rank = str(row["sheet_rank"] or "Unranked")
            normalized_old = {
                "Field": "Field Medic",
                "Junior": "Junior Medic",
                "Senior": "Senior Medic",
                "Intern Medic": "Unranked",
            }.get(old_sheet_rank, old_sheet_rank)

            if normalized_old != new_sheet_rank:
                changes.append({
                    **row,
                    "public_rank": public_rank,
                    "new_sheet_rank": new_sheet_rank,
                })

        if changes:
            await asyncio.to_thread(_apply_master_rank_changes, changes)

            # Rebuild lifetime adjusted totals from the new official ranks.
            master_updated = await asyncio.to_thread(update_master_log)
            if master_updated:
                await asyncio.to_thread(update_leaderboard)

        # Always push after a rank check. This also repairs stale WordPress ranks
        # when the spreadsheet already had the correct value.
        wp_result = None
        if push_website and wordpress_sync_configured():
            wp_result = await asyncio.to_thread(push_wordpress_sync)

        summary = {
            "checked": checked,
            "changed": len(changes),
            "without_discord_id": without_id,
            "not_in_guild": not_in_guild,
            "without_rank_role": without_rank_role,
        }

        try:
            await asyncio.to_thread(_wordpress_json_request, WORDPRESS_RANK_URL, "POST", summary)
        except Exception as e:
            print(f"⚠️ Could not save Discord rank sync status to WordPress: {e}")

        if changes:
            printable = ", ".join(
                f"{item['medic_name']}: {item['sheet_rank']} → {item['new_sheet_rank']}"
                for item in changes[:10]
            )
            if len(changes) > 10:
                printable += f", +{len(changes)-10} more"
            print(f"🎖️ Discord rank sync changed {len(changes)} medic(s): {printable}")
        else:
            print(f"🎖️ Discord rank sync checked {checked} linked medic(s); no rank changes.")

        return {
            "ok": True,
            **summary,
            "changes": changes,
            "wordpress": wp_result,
        }


async def wordpress_rank_worker():
    while not bot.is_closed():
        try:
            result = await sync_discord_ranks_once()
            if not result.get("ok") and not result.get("skipped"):
                print(f"⚠️ Discord rank sync failed: {result.get('message','Unknown error')}")
        except Exception as e:
            print(f"⚠️ Discord rank worker error: {e}")
        await asyncio.sleep(WORDPRESS_RANK_SYNC_SECONDS)


# ================= IN-GAME MEDICAL CORP ORGANIZATION SYNC =================
def wordpress_org_configured() -> bool:
    return bool(WORDPRESS_ORG_URL and WORDPRESS_SYNC_SECRET)


def wordpress_org_status():
    if not wordpress_org_configured():
        return {
            "ok": False,
            "configured": False,
            "url": WORDPRESS_ORG_URL or "(not configured)",
            "error": "WordPress sync URL / secret are not fully configured.",
        }
    try:
        data = _wordpress_json_request(WORDPRESS_ORG_URL, "GET")
        return {
            "ok": True,
            "configured": True,
            "url": WORDPRESS_ORG_URL,
            "guild_id": str(data.get("guild_id", "") or ""),
            "role_id": str(data.get("role_id", "") or ""),
            "max_slots": int(data.get("max_slots", 20) or 20),
            "current_count": int(data.get("current_count", 0) or 0),
            "last_sync": int(data.get("last_sync", 0) or 0),
        }
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
        return {"ok": False, "configured": True, "url": WORDPRESS_ORG_URL, "error": f"HTTP {e.code}: {body[:300]}"}
    except Exception as e:
        return {"ok": False, "configured": True, "url": WORDPRESS_ORG_URL, "error": str(e)}


async def sync_ingame_org_once():
    """Read the configured Medical Corp Discord role and push its complete membership to WordPress."""
    if not wordpress_org_configured():
        return {"ok": False, "skipped": True, "message": "WordPress organization sync is not configured."}

    config = await asyncio.to_thread(wordpress_org_status)
    if not config.get("ok"):
        return {"ok": False, "message": str(config.get("error", "Could not read WordPress organization settings."))}

    role_id_text = str(config.get("role_id", "") or "").strip()
    guild_id_text = str(config.get("guild_id", "") or "").strip()
    if not role_id_text:
        return {"ok": False, "skipped": True, "message": "Set the Medical Corp in-game organization role ID in WordPress → LVMC Portal."}

    try:
        role_id = int(role_id_text)
        guild_id = int(guild_id_text or GUILD_ID)
    except ValueError:
        return {"ok": False, "message": "Invalid guild or role ID returned by WordPress."}

    guild = bot.get_guild(guild_id)
    if guild is None:
        return {"ok": False, "message": f"MedBot is not connected to Discord guild {guild_id}."}

    role = guild.get_role(role_id)
    if role is None:
        return {"ok": False, "message": f"Could not find Discord role {role_id} in {guild.name}."}

    # With Server Members Intent enabled, role.members is the fastest path.
    role_members = list(role.members)

    # If the cache is empty, try an explicit member fetch. Discord still requires
    # the privileged Server Members Intent for reliable full-roster access.
    if not role_members:
        try:
            fetched = []
            async for member in guild.fetch_members(limit=None):
                if role in member.roles:
                    fetched.append(member)
            role_members = fetched
        except Exception as e:
            if getattr(role, "members", None) == []:
                return {
                    "ok": False,
                    "message": (
                        "Could not read the Medical Corp role membership. Enable Server Members Intent "
                        f"for MedBot in the Discord Developer Portal and restart the bot. ({e})"
                    ),
                }

    members_payload = []
    for member in role_members:
        members_payload.append({
            "discord_id": str(member.id),
            "display_name": member.display_name,
            "username": str(member),
            "avatar_url": str(member.display_avatar.url) if member.display_avatar else "",
        })

    try:
        response = await asyncio.to_thread(
            _wordpress_json_request,
            WORDPRESS_ORG_URL,
            "POST",
            {"members": members_payload},
        )
        result = {
            "ok": True,
            "count": len(members_payload),
            "max_slots": int(response.get("max_slots", config.get("max_slots", 20)) or 20) if isinstance(response, dict) else config.get("max_slots", 20),
            "matched_medics": int(response.get("matched_medics", 0) or 0) if isinstance(response, dict) else 0,
            "wordpress": response,
        }
        print(
            "🏥 In-game Medical Corp sync:",
            f"{result['count']}/{result['max_slots']} slots",
            f"matched-profiles={result['matched_medics']}",
        )
        return result
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
        return {"ok": False, "message": f"WordPress HTTP {e.code}: {body[:500]}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


async def wordpress_org_worker():
    while not bot.is_closed():
        try:
            result = await sync_ingame_org_once()
            if not result.get("ok") and not result.get("skipped"):
                print(f"⚠️ In-game Medical Corp sync failed: {result.get('message','Unknown error')}")
        except Exception as e:
            print(f"⚠️ In-game Medical Corp worker error: {e}")
        await asyncio.sleep(WORDPRESS_ORG_SYNC_SECONDS)


# ================= WORDPRESS -> DISCORD NOTIFICATIONS =================
def wordpress_notification_configured() -> bool:
    return bool(WORDPRESS_NOTIFICATION_URL and WORDPRESS_NOTIFICATION_ACK_URL and WORDPRESS_SYNC_SECRET)


def _wordpress_json_request(url: str, method: str = "GET", payload=None, timeout: int = 15):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-LVMC-Sync-Secret": WORDPRESS_SYNC_SECRET,
            "User-Agent": "LVMC-MedBot/3.4",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else {}


def pull_wordpress_notifications():
    if not wordpress_notification_configured():
        return []
    try:
        data = _wordpress_json_request(WORDPRESS_NOTIFICATION_URL, "GET")
        items = data.get("notifications", []) if isinstance(data, dict) else []
        return items if isinstance(items, list) else []
    except Exception as e:
        print(f"⚠️ Could not pull WordPress notifications: {e}")
        return []


def wordpress_notification_status():
    """Diagnostic check used by /portalstatus; does not acknowledge/delete notifications."""
    if not wordpress_notification_configured():
        return {
            "ok": False,
            "configured": False,
            "url": WORDPRESS_NOTIFICATION_URL or "(not configured)",
            "error": "LVMC_WORDPRESS_SYNC_URL / LVMC_WORDPRESS_SYNC_SECRET are not fully configured.",
        }
    try:
        data = _wordpress_json_request(WORDPRESS_NOTIFICATION_URL, "GET")
        items = data.get("notifications", []) if isinstance(data, dict) else []
        return {
            "ok": True,
            "configured": True,
            "url": WORDPRESS_NOTIFICATION_URL,
            "pending": len(items) if isinstance(items, list) else 0,
        }
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
        return {"ok": False, "configured": True, "url": WORDPRESS_NOTIFICATION_URL, "error": f"HTTP {e.code}: {body[:300]}"}
    except Exception as e:
        return {"ok": False, "configured": True, "url": WORDPRESS_NOTIFICATION_URL, "error": str(e)}


def ack_wordpress_notifications(sent_ids, failed_ids=None):
    if not wordpress_notification_configured():
        return
    try:
        _wordpress_json_request(
            WORDPRESS_NOTIFICATION_ACK_URL,
            "POST",
            {"ids": list(sent_ids or []), "failed_ids": list(failed_ids or [])},
        )
    except Exception as e:
        print(f"⚠️ Could not acknowledge WordPress notifications: {e}")


async def deliver_wordpress_notification(item: dict) -> bool:
    try:
        discord_id = int(str(item.get("discord_id", "")).strip())
    except (TypeError, ValueError):
        return False

    title = html.unescape(str(item.get("title", "LVMC update") or "LVMC update")).strip()[:200]
    message = html.unescape(str(item.get("message", "") or "")).strip()[:3000]
    url = str(item.get("url", "") or "").strip()
    kind = str(item.get("kind", "general") or "general")
    icons = {
        "reply": "💬", "offer": "💰", "assignment": "💼", "status": "📌",
        "completion": "✅", "promotion": "🏅", "new_request": "💰",
    }
    icon = icons.get(kind, "🔔")
    body = f"{icon} **{title}**"
    if message:
        body += f"\n{message}"
    # Angle brackets suppress Discord's large link preview, keeping job DMs compact.
    if url:
        body += f"\n<{url}>"

    try:
        user = bot.get_user(discord_id) or await bot.fetch_user(discord_id)
        if not user:
            return False

        await user.send(body[:3900])
        return True
    except discord.Forbidden:
        print(f"⚠️ Cannot DM Discord user {discord_id}; DMs may be disabled or the bot may not share a server with that user.")
        if WORDPRESS_NOTIFICATION_CHANNEL_ID:
            try:
                channel = bot.get_channel(WORDPRESS_NOTIFICATION_CHANNEL_ID) or await bot.fetch_channel(WORDPRESS_NOTIFICATION_CHANNEL_ID)
                if channel:
                    fallback = f"<@{discord_id}> {body[:1500]}"
                    await channel.send(fallback)
                    print(f"🔔 Used notification-channel fallback for Discord user {discord_id}.")
                    return True
            except Exception as fallback_error:
                print(f"⚠️ Notification-channel fallback failed for {discord_id}: {fallback_error}")
        return False
    except Exception as e:
        print(f"⚠️ Discord notification delivery failed for {discord_id}: {e}")
        return False


async def wordpress_notification_worker():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            items = await asyncio.to_thread(pull_wordpress_notifications)
            sent_ids, failed_ids = [], []
            for item in items:
                notification_id = item.get("id")
                ok = await deliver_wordpress_notification(item)
                if notification_id:
                    (sent_ids if ok else failed_ids).append(int(notification_id))
                # Keep Discord API use gentle if several users are queued at once.
                await asyncio.sleep(0.35)
            if sent_ids or failed_ids:
                await asyncio.to_thread(ack_wordpress_notifications, sent_ids, failed_ids)
                print(f"🔔 Website notifications: delivered={len(sent_ids)} failed={len(failed_ids)}")
        except Exception as e:
            print(f"⚠️ WordPress notification worker error: {e}")
        await asyncio.sleep(WORDPRESS_NOTIFICATION_POLL_SECONDS)


_notification_worker_task = None
_org_worker_task = None
_rank_worker_task = None

# ================= REPORT EDIT HELPERS =================
def parse_report_duration_to_minutes(duration_value) -> int:
    """Convert values like '60 min' or '60' into an integer minute count."""
    text = str(duration_value or "0").strip()
    match = re.search(r"\d+", text)
    return int(match.group(0)) if match else 0


def parse_clients_count(clients_value) -> int:
    """Accept either a number or a comma-separated client list."""
    text = str(clients_value or "").strip()
    if not text:
        return 0
    if text.isdigit():
        return int(text)
    return len(split_names(text))


def parse_report_date_value(value):
    """Accept MM/DD/YYYY or YYYY-MM-DD."""
    text = str(value or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def build_report_embed(
    job_type: str,
    desc: str,
    report_date,
    medic_list,
    duration: int,
    clients_count: int,
    points: int,
):
    """Build the public Discord embed for a submitted or edited report."""
    embed = discord.Embed(
        title=f"Medic Report — {job_type}",
        description=desc,
        color=0x00FFAA,
    )
    embed.add_field(name="Date", value=report_date.strftime("%B %d, %Y"))
    embed.add_field(name="Medics", value=", ".join(medic_list), inline=False)
    embed.add_field(name="Duration", value=f"{duration} min")
    embed.add_field(name="Clients", value=str(clients_count))
    embed.add_field(name="Points", value=str(points))
    embed.timestamp = datetime.now()
    return embed


def find_report_row_by_message_id(message_id: str):
    """
    Find a report by Discord Message ID.
    Returns (sheet_row_number, row_dict), or (None, None).
    """
    records = get_raw_records_cached(force=True)
    target = str(message_id).strip()

    for index, row in enumerate(records, start=2):
        if clean_sheet_id(row.get("Message ID", "")) == target:
            return index, row

    return None, None


def get_recent_reports_for_user(user_id: int, limit: int = 10):
    """Return the user's recent reports, newest first, with sheet row numbers."""
    records = get_raw_records_cached(force=True)
    user_id_text = str(user_id)
    matches = []

    for index, row in enumerate(records, start=2):
        if clean_sheet_id(row.get("Reporter ID", "")) == user_id_text:
            matches.append((index, row))

    return list(reversed(matches))[:limit]


def user_can_edit_report(interaction: discord.Interaction, row: dict) -> bool:
    """Original reporter or server admin can edit a report."""
    if interaction.user.guild_permissions.administrator:
        return True
    return clean_sheet_id(row.get("Reporter ID", "")) == str(interaction.user.id)


async def rebuild_after_report_change():
    """Rebuild derived sheets after a report is added/edited, then push fresh lifetime stats."""
    master_updated = False
    if should_run("master"):
        master_updated = bool(update_master_log())
    if should_run("leaderboard"):
        update_leaderboard()
    if master_updated and wordpress_sync_configured():
        await asyncio.to_thread(push_wordpress_sync)

# ================= DISCORD BOT =================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # Required to sync everyone who holds the in-game Medical Corp role.

bot = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(bot)


# ================= COMMANDS =================
# Command interface consolidated in MedBot v3.7.
# Monthly payout opt-out/redistribution added in v3.9; former-Medic payout selection added in v3.9.1. Previous-month website stats added in v3.8.
@tree.command(
    name="setrank",
    description="Ranks are controlled by Discord roles"
)
@discord.app_commands.guilds(discord.Object(id=GUILD_ID))
async def setrank(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("🚫 Admins only.")
        return
    await interaction.followup.send(
        "ℹ️ Medic ranks are now controlled by **Discord roles**.\n"
        "Change the Medic's Discord rank role, then use `/medadmin sync` → **Discord ranks** if you want an immediate update."
    )


@tree.command(
    name="syncranks",
    description="Sync Discord Medic ranks to Sheets and the website (admin only)"
)
@discord.app_commands.guilds(discord.Object(id=GUILD_ID))
async def syncranks(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("🚫 Admins only.")
        return

    result = await sync_discord_ranks_once()
    if not result.get("ok"):
        await interaction.followup.send(
            f"⚠️ Rank sync failed: {result.get('message','Unknown error')}"
        )
        return

    await interaction.followup.send(
        "🎖️ **Discord rank sync complete**\n"
        f"Linked medics checked: **{result.get('checked',0)}**\n"
        f"Ranks changed: **{result.get('changed',0)}**\n"
        f"Master rows without Discord ID: **{result.get('without_discord_id',0)}**\n"
        f"Members not found in Med Corps Discord: **{result.get('not_in_guild',0)}**\n"
        f"Linked members without a configured rank role: **{result.get('without_rank_role',0)}**"
    )


@tree.command(
    name="linkmedic",
    description="Link a Master Log medic to their Discord account (admin only)"
)
@discord.app_commands.describe(
    medic="Medic name exactly as it appears in the Master Medical Log",
    member="The Discord member who owns this medic profile",
)
@discord.app_commands.guilds(discord.Object(id=GUILD_ID))
async def linkmedic(
    interaction: discord.Interaction,
    medic: str,
    member: discord.Member,
):
    await interaction.response.defer(ephemeral=True)

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("🚫 Admins only.")
        return

    try:
        msg = link_medic_discord_id(medic, member.id)
        rank_result = await sync_discord_ranks_once(target_discord_ids={str(member.id)})
        rank_note = (
            f" Rank sync: **{rank_result.get('changed',0)} change(s)**."
            if rank_result.get("ok")
            else " Rank sync could not run yet."
        )
        await interaction.followup.send(
            f"✅ {msg}{rank_note}\nUse `/medadmin sync` with **Full Rebuild** only if you also need to rebuild older legacy report identity data."
        )
    except Exception as e:
        await interaction.followup.send(f"⚠️ Error linking medic: {e}")


@tree.command(name="updatelogs", description="Force update ALL leaderboard sheets and the master log.")
@discord.app_commands.guilds(discord.Object(id=GUILD_ID))
async def update_logs(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        # Discord is authoritative for rank before any adjusted BP rebuild.
        await sync_discord_ranks_once(push_website=False)

        # force fresh cache reads for a manual rebuild
        get_raw_records_cached(force=True)
        master_updated = bool(update_master_log())
        update_all_leaderboards()
        sync_note = ""
        if master_updated and wordpress_sync_configured():
            sync_result = await asyncio.to_thread(push_wordpress_sync)
            sync_note = " Website sync complete." if sync_result.get("ok") else " Website sync failed/skipped."
        await interaction.followup.send(f"✅ All logs and leaderboards updated!{sync_note}")
    except Exception as e:
        await interaction.followup.send(f"⚠️ Error: {e}")


@tree.command(
    name="rebuildleaderboard",
    description="Rebuild a specific month's leaderboard (admin use)"
)
@discord.app_commands.describe(year="Year (e.g. 2026)", month="Month number (1–12)")
@discord.app_commands.guilds(discord.Object(id=GUILD_ID))
async def rebuild_leaderboard(interaction: discord.Interaction, year: int, month: int):
    await interaction.response.defer(ephemeral=True)

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("🚫 Admins only.")
        return

    if month < 1 or month > 12:
        await interaction.followup.send("❌ Month must be between 1 and 12.")
        return

    try:
        get_raw_records_cached(force=True)
        update_single_leaderboard(year, month)
        month_name = datetime(year, month, 1).strftime("%B")
        await interaction.followup.send(f"✅ Rebuilt leaderboard for **{month_name} {year}**")
    except Exception as e:
        await interaction.followup.send(f"⚠️ Error rebuilding leaderboard: {e}")


@tree.command(
    name="setryo",
    description="Set Ryo payout for a specific month (admin only)"
)
@discord.app_commands.describe(
    year="Year (e.g. 2026)",
    month="Month (1–12)",
    amount="Total Ryo for that month"
)
@discord.app_commands.guilds(discord.Object(id=GUILD_ID))
async def set_ryo(interaction: discord.Interaction, year: int, month: int, amount: int):
    await interaction.response.defer(ephemeral=True)

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("🚫 Admins only.")
        return

    if month < 1 or month > 12:
        await interaction.followup.send("❌ Month must be 1–12.")
        return

    if amount <= 0:
        await interaction.followup.send("❌ Ryo must be positive.")
        return

    set_bank_ryo(year, month, amount)

    month_name = datetime(year, month, 1).strftime("%B")
    await interaction.followup.send(
        f"💰 Ryo for **{month_name} {year}** set to **{amount:,} Ryo**"
    )


@tree.command(name="syncwebsite", description="Push current Master Medical Log stats to the LVMC website (admin only)")
@discord.app_commands.guilds(discord.Object(id=GUILD_ID))
async def syncwebsite(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("🚫 Admins only.")
        return
    if not wordpress_sync_configured():
        await interaction.followup.send(
            "⚠️ Website sync is not configured. Set `LVMC_WORDPRESS_SYNC_URL` and `LVMC_WORDPRESS_SYNC_SECRET` on the VM."
        )
        return

    rank_result = await sync_discord_ranks_once(push_website=False)
    if not rank_result.get("ok") and not rank_result.get("skipped"):
        await interaction.followup.send(
            f"⚠️ Discord rank sync failed before website sync: {rank_result.get('message','Unknown error')}"
        )
        return

    result = await asyncio.to_thread(push_wordpress_sync)
    if not result.get("ok"):
        await interaction.followup.send(f"⚠️ Website stats sync failed/skipped: {result.get('message','Unknown error')}")
        return

    org_result = await sync_ingame_org_once()
    wp = result.get("wordpress", {}) if isinstance(result.get("wordpress"), dict) else {}
    org_line = (
        f"• In-game organization: **{org_result.get('count',0)}/{org_result.get('max_slots',20)}**"
        if org_result.get("ok")
        else f"• In-game organization: ⚠️ {org_result.get('message','not synced')}"
    )
    await interaction.followup.send(
        "✅ Website sync complete.\n"
        f"• Sent from Master Log: **{result.get('sent',0)}**\n"
        f"• WordPress matched: **{wp.get('matched','?')}**\n"
        f"• WordPress unmatched: **{len(wp.get('unmatched',[])) if isinstance(wp.get('unmatched'),list) else '?'}**\n"
        f"• Master rows without Discord ID: **{len(result.get('without_discord_id',[]))}**\n"
        + org_line
    )



@tree.command(name="syncorg", description="Sync the in-game Medical Corp Discord role to the website")
@discord.app_commands.guilds(discord.Object(id=GUILD_ID))
async def syncorg(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("🚫 Admins only.", ephemeral=True)
        return
    result = await sync_ingame_org_once()
    if result.get("ok"):
        await interaction.followup.send(
            f"🏥 In-game Medical Corp synced: **{result.get('count',0)}/{result.get('max_slots',20)}** slots filled. "
            f"**{result.get('matched_medics',0)}** members matched to website Medic profiles.",
            ephemeral=True,
        )
    else:
        await interaction.followup.send(
            f"⚠️ In-game organization sync failed: {result.get('message','Unknown error')}",
            ephemeral=True,
        )


@tree.command(name="portalstatus", description="Check WordPress portal sync and Discord notification delivery")
@discord.app_commands.guilds(discord.Object(id=GUILD_ID))
async def portalstatus(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("🚫 Admins only.", ephemeral=True)
        return

    notify = await asyncio.to_thread(wordpress_notification_status)
    org = await asyncio.to_thread(wordpress_org_status)
    ranks = await asyncio.to_thread(wordpress_rank_status)
    sync_ready = bool(WORDPRESS_SYNC_URL and WORDPRESS_SYNC_SECRET)
    lines = [
        f"**Stats sync configured:** {'✅' if sync_ready else '❌'}",
        f"**In-game org sync:** {'✅' if org.get('configured') and org.get('role_id') else '❌'}",
        f"**Discord notification polling:** {'✅' if notify.get('configured') else '❌'}",
        f"**Notification endpoint:** `{notify.get('url', '(not configured)')}`",
    ]
    if notify.get("ok"):
        lines.append(f"**Endpoint reachable:** ✅")
        lines.append(f"**Pending DMs visible to MedBot:** {notify.get('pending', 0)}")
    else:
        lines.append("**Endpoint reachable:** ❌")
        lines.append(f"**Error:** `{str(notify.get('error','Unknown error'))[:500]}`")
    if org.get("ok"):
        lines.append(f"**In-game organization snapshot:** {org.get('current_count',0)}/{org.get('max_slots',20)}")
        if not org.get("role_id"):
            lines.append("**Organization role:** Not configured in LVMC Portal")
    elif org.get("configured"):
        lines.append(f"**Organization sync error:** `{str(org.get('error','Unknown error'))[:300]}`")
    if ranks.get("ok"):
        configured_rank_roles = sum(
            1 for rank_name, _sheet_rank in RANK_ORDER
            if ranks.get("rank_roles", {}).get(rank_name)
        )
        lines.append(f"**Discord rank sync:** ✅ ({configured_rank_roles}/6 rank roles mapped)")
    else:
        lines.append(f"**Discord rank sync:** ❌ `{str(ranks.get('error','not configured'))[:300]}`")

    if WORDPRESS_NOTIFICATION_CHANNEL_ID:
        lines.append(f"**DM fallback channel:** <#{WORDPRESS_NOTIFICATION_CHANNEL_ID}>")
    else:
        lines.append("**DM fallback channel:** Not configured")
    await interaction.followup.send("\n".join(lines), ephemeral=True)


@tree.command(name="leaderboard", description="Show this month's medic leaderboard")
@discord.app_commands.guilds(discord.Object(id=GUILD_ID))
async def leaderboard_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    try:
        sorted_data, jobs_by_medic = update_leaderboard()

        if not sorted_data:
            await interaction.followup.send("📋 No medic data found for this month.")
            return

        lines = []
        payout_snapshot = calculate_monthly_payout_snapshot(
            datetime.now().year, datetime.now().month
        )
        opted_out_names = {
            row["medic"] for row in payout_snapshot["rows"] if row["opted_out"]
        }

        for i, (medic, points) in enumerate(sorted_data[:10], start=1):
            job_count = jobs_by_medic.get(medic, 0)
            payout_note = " — 🚫 pay opted out" if medic in opted_out_names else ""
            lines.append(
                f"**{i}. {medic}** — {points} pts ({job_count} jobs){payout_note}"
            )

        embed = discord.Embed(
            title=f"🏆 Medic Leaderboard — {datetime.now().strftime('%B %Y')}",
            description="\n".join(lines),
            color=0xFFD700,
        )
        embed.set_footer(text="Data pulled from Google Sheets")
        await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"⚠️ Error loading leaderboard: {e}")


@tree.command(name="medicstats", description="View lifetime stats for a specific medic")
@discord.app_commands.describe(name="The medic's name")
@discord.app_commands.guilds(discord.Object(id=GUILD_ID))
async def medicstats(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=False)

    try:
        master = SS.worksheet("Leaf Master Medical Log")
        records = get_master_records_cached(master)

        if not records:
            await interaction.followup.send("⚠️ No lifetime data found.")
            return

        target = None
        for row in records:
            medic_name = row.get("Medic", "")
            if name.lower() in str(medic_name).lower():
                target = row
                break

        if not target:
            await interaction.followup.send(f"❌ No medic found matching: **{name}**")
            return

        medic = target.get("Medic", "Unknown")
        rank = target.get("Rank", "Unranked")
        rank_display = "Intern Medic" if str(rank).lower() == "unranked" else rank
        jobs = target.get("Total Jobs", 0)
        raw = target.get("Total Raw Points", 0)
        adj = target.get("Total Adjusted Points", 0)
        hours = target.get("Total Hours", 0)

        raid_h = target.get("Raid", 0)
        lmpf_h = target.get("LMPF", 0)
        heal_h = target.get("Healing", 0)
        rev_h = target.get("Rev/Spar", 0)
        escort_h = target.get("Escort", 0)
        boss_h = target.get("World Boss", 0)
        arc_h = target.get("Arc", 0)
        mission_h = target.get("Mission", 0)
        event_h = target.get("Hosted Event", 0)
        host_training_h = target.get("Host Training Event", 0)
        participate_training_h = target.get("Participate In Training Event", 0)
        total_clients = target.get("Total Clients", 0)
        raid_count = target.get("Raid/Defense Count", 0)
        hosted_event_count = target.get("Hosted Event Count", 0)
        host_training_count = target.get("Host Training Count", 0)
        training_participation_count = target.get("Training Participation Count", 0)

        embed = discord.Embed(title=f"💠 Lifetime Stats — {medic}", color=0x3498DB)
        embed.add_field(name="Rank", value=rank_display, inline=True)
        embed.add_field(name="Total Jobs", value=jobs, inline=True)
        embed.add_field(name="Total Raw Points", value=raw, inline=True)
        embed.add_field(name="Total Adjusted Points", value=adj, inline=True)
        embed.add_field(name="Total Hours", value=hours, inline=True)
        embed.add_field(name="Total Clients", value=total_clients, inline=True)
        embed.add_field(
            name="Promotion Activity",
            value=(
                f"• **Raids / Defenses:** {raid_count}\n"
                f"• **Hosted Events:** {hosted_event_count}\n"
                f"• **Hosted Trainings:** {host_training_count}\n"
                f"• **Training Participation:** {training_participation_count}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Hours Breakdown",
            value=(
                f"• **Raid:** {raid_h}\n"
                f"• **LMPF:** {lmpf_h}\n"
                f"• **Healing:** {heal_h}\n"
                f"• **Rev/Spar:** {rev_h}\n"
                f"• **Escort:** {escort_h}\n"
                f"• **World Boss:** {boss_h}\n"
                f"• **Arc:** {arc_h}\n"
                f"• **Mission:** {mission_h}\n"
                f"• **Hosted Event:** {event_h}\n"
                f"• **Host Training Event:** {host_training_h}\n"
                f"• **Participate In Training Event:** {participate_training_h}"
            ),
            inline=False,
        )
        embed.set_footer(text="Lifetime stats from the Master Medical Log")
        await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"⚠️ Error: {e}")



@tree.command(name="myreports", description="Show your recent medic reports that can be edited")
@discord.app_commands.guilds(discord.Object(id=GUILD_ID))
async def myreports(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        reports = get_recent_reports_for_user(interaction.user.id, limit=10)

        if not reports:
            await interaction.followup.send(
                "I could not find any editable reports for you. Only reports with stored reporter IDs can be edited here.",
                ephemeral=True,
            )
            return

        lines = []
        for sheet_row, row in reports:
            message_id = clean_sheet_id(row.get("Message ID", ""))
            report_date = str(row.get("Report Date", "")).strip()
            job_name = str(row.get("Job Name", "")).strip()
            medics = str(row.get("Medics", "")).strip()
            points = str(row.get("Points", "")).strip()

            lines.append(
                f"**Report ID:** `{message_id}`\n"
                f"• **Date:** {report_date}\n"
                f"• **Job:** {job_name}\n"
                f"• **Medics:** {medics}\n"
                f"• **Points:** {points}\n"
            )

        await interaction.followup.send(
            "Use `/reports edit report_id:` with one of these Report IDs:\n\n" + "\n".join(lines),
            ephemeral=True,
        )

    except Exception as e:
        await interaction.followup.send(f"⚠️ Error loading your reports: {e}", ephemeral=True)


@tree.command(name="editreport", description="Edit one of your medic reports by Report ID")
@discord.app_commands.describe(report_id="Use /reports recent to find the Report ID")
@discord.app_commands.guilds(discord.Object(id=GUILD_ID))
async def editreport(interaction: discord.Interaction, report_id: str):
    row_number, existing_row = find_report_row_by_message_id(report_id)

    if row_number is None or existing_row is None:
        await interaction.response.send_message(
            "❌ I could not find that report. Use `/reports recent` to get the correct Report ID.",
            ephemeral=True,
        )
        return

    if not user_can_edit_report(interaction, existing_row):
        await interaction.response.send_message(
            "🚫 You can only edit reports you submitted. Admins can edit any report.",
            ephemeral=True,
        )
        return

    current_job = str(existing_row.get("Job Name", "")).strip() or "Raid / Defend"
    existing_pairs = report_medic_pairs(existing_row)
    current_medic_names = [name for name, _ in existing_pairs]
    current_medic_ids = [did for _, did in existing_pairs]

    async def open_edit_modal(
        trigger_interaction: discord.Interaction,
        selected_job_type: str,
        selected_members=None,
    ):
        """Open the edit modal after optional job/medic changes are selected."""
        if selected_members:
            medic_list = [member_display_name(m) for m in selected_members]
            medic_ids = [str(m.id) for m in selected_members]
        else:
            medic_list = current_medic_names
            medic_ids = current_medic_ids

        if not medic_list:
            await trigger_interaction.response.send_message(
                "⚠️ This report has no medic names. Select at least one medic first.",
                ephemeral=True,
            )
            return

        class EditReportModal(discord.ui.Modal, title="Edit Medic Report"):
            report_date = discord.ui.TextInput(
                label="Report Date (MM/DD/YYYY)",
                default=str(existing_row.get("Report Date", ""))[:4000],
            )
            duration = discord.ui.TextInput(
                label="Duration in minutes",
                default=str(parse_report_duration_to_minutes(existing_row.get("Duration", ""))),
            )
            clients = discord.ui.TextInput(
                label="Clients (names or number)",
                default=str(existing_row.get("Participant Names", "") or existing_row.get("Clients", ""))[:4000],
            )
            description = discord.ui.TextInput(
                label="Description",
                style=discord.TextStyle.long,
                default=str(existing_row.get("Description", ""))[:4000],
            )

            async def on_submit(self, modal_interaction: discord.Interaction):
                try:
                    await modal_interaction.response.defer(ephemeral=True)

                    date_obj = parse_report_date_value(self.report_date.value)
                    if not date_obj:
                        await modal_interaction.followup.send(
                            "⚠️ Invalid date format. Use `MM/DD/YYYY` or `YYYY-MM-DD`.",
                            ephemeral=True,
                        )
                        return

                    try:
                        duration_minutes = int(str(self.duration.value).strip())
                    except ValueError:
                        await modal_interaction.followup.send(
                            "⚠️ Duration must be a number of minutes.",
                            ephemeral=True,
                        )
                        return

                    if duration_minutes < 0:
                        await modal_interaction.followup.send(
                            "⚠️ Duration cannot be negative.", ephemeral=True
                        )
                        return

                    clients_text = str(self.clients.value).strip()
                    if clients_text.isdigit():
                        clients_count = int(clients_text)
                        participant_names = clients_text
                    else:
                        client_list = split_names(clients_text)
                        clients_count = len(client_list)
                        participant_names = ", ".join(client_list)

                    job_type = selected_job_type
                    points = calculate_points(job_type, duration_minutes, clients_count)
                    desc = str(self.description.value).strip()

                    old_link = str(existing_row.get("Message Link", "")).strip()
                    old_reporter_id = clean_sheet_id(existing_row.get("Reporter ID", "")).strip()
                    old_reporter_name = str(existing_row.get("Reporter Name", "")).strip()
                    old_message_id = clean_sheet_id(existing_row.get("Message ID", "")).strip()
                    old_channel_id = clean_sheet_id(existing_row.get("Channel ID", "")).strip()

                    updated_row = [
                        str(existing_row.get("Timestamp", "")).strip()
                        or datetime.now().strftime("%m/%d/%Y %H:%M"),
                        ", ".join(medic_list),
                        job_type,
                        f"{duration_minutes} min",
                        points,
                        clients_count,
                        participant_names,
                        desc,
                        date_obj.strftime("%m/%d/%Y"),
                        old_link,
                        f"'{old_reporter_id}",
                        old_reporter_name,
                        f"'{old_message_id}",
                        f"'{old_channel_id}",
                        serialize_medic_discord_ids(medic_ids),
                    ]

                    if len(updated_row) != len(REPORT_HEADERS):
                        raise ValueError(
                            f"Edited report row has {len(updated_row)} columns, expected {len(REPORT_HEADERS)}"
                        )

                    SHEET.update(
                        f"A{row_number}:O{row_number}",
                        [updated_row],
                        value_input_option="USER_ENTERED",
                    )
                    SHEET.resize(cols=len(REPORT_HEADERS))
                    invalidate_raw_cache()

                    embed_edit_note = ""
                    try:
                        channel = bot.get_channel(int(old_channel_id)) or await bot.fetch_channel(int(old_channel_id))
                        msg = await channel.fetch_message(int(old_message_id))
                        embed = build_report_embed(
                            job_type, desc, date_obj, medic_list,
                            duration_minutes, clients_count, points,
                        )
                        await msg.edit(embed=embed)
                    except Exception as embed_error:
                        embed_edit_note = "\n⚠️ Sheet updated, but I could not edit the original Discord embed."
                        print(f"⚠️ Sheet updated, but could not edit Discord embed: {embed_error}")

                    await rebuild_after_report_change()
                    await modal_interaction.followup.send(
                        f"✅ Report `{old_message_id}` updated. Job: **{job_type}**. "
                        f"Medics: **{', '.join(medic_list)}**. New points: **{points}**.{embed_edit_note}",
                        ephemeral=True,
                    )
                except Exception as e:
                    await modal_interaction.followup.send(
                        f"⚠️ Error editing report: {e}", ephemeral=True
                    )

        await trigger_interaction.response.send_modal(EditReportModal())

    class EditJobSelect(discord.ui.Select):
        def __init__(self, parent_view):
            self.parent_view = parent_view
            options = [
                discord.SelectOption(label=label, value=value, default=(value == current_job))
                for label, value in JOB_OPTIONS
            ]
            super().__init__(
                placeholder="Optional: choose a different job type...",
                options=options,
            )

        async def callback(self, select_interaction: discord.Interaction):
            self.parent_view.selected_job = self.values[0]
            await select_interaction.response.defer()

    class EditMedicSelect(discord.ui.UserSelect):
        def __init__(self, parent_view):
            self.parent_view = parent_view
            super().__init__(
                placeholder="Optional: select replacement medic(s)...",
                min_values=1,
                max_values=25,
            )

        async def callback(self, select_interaction: discord.Interaction):
            selected = list(self.values)
            if any(getattr(member, "bot", False) for member in selected):
                await select_interaction.response.send_message(
                    "⚠️ Bots cannot be selected as medics.", ephemeral=True
                )
                return
            self.parent_view.selected_members = selected
            await select_interaction.response.defer()

    class EditReportView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=180)
            self.selected_job = current_job
            self.selected_members = None
            self.add_item(EditJobSelect(self))
            self.add_item(EditMedicSelect(self))

        async def interaction_check(self, view_interaction: discord.Interaction) -> bool:
            if view_interaction.user.id != interaction.user.id:
                await view_interaction.response.send_message(
                    "🚫 This edit menu is not for you.", ephemeral=True
                )
                return False
            return True

        @discord.ui.button(label="Continue to edit details", style=discord.ButtonStyle.primary)
        async def continue_edit(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            await open_edit_modal(
                button_interaction,
                self.selected_job,
                self.selected_members,
            )

    current_ids_note = (
        "Discord IDs are already attached to this report."
        if any(current_medic_ids)
        else "This is a legacy report with no medic Discord IDs yet. Select the medic(s) above if you want to attach IDs while editing."
    )
    await interaction.response.send_message(
        f"Current job: **{current_job}**\n"
        f"Current medics: **{', '.join(current_medic_names) or 'None'}**\n"
        f"{current_ids_note}\n\n"
        "Optionally change the job and/or medic selection, then click **Continue to edit details**.",
        view=EditReportView(),
        ephemeral=True,
    )



# ================= GROUPED COMMAND INTERFACE (v3.7) =================
# These groups keep the Discord command picker small while reusing the
# already-tested command callbacks and sync functions above.

reports_group = discord.app_commands.Group(
    name="reports",
    description="View or edit your Medic reports",
)

medic_group = discord.app_commands.Group(
    name="medic",
    description="Medic information and statistics",
)

medadmin_group = discord.app_commands.Group(
    name="medadmin",
    description="Medical Corps administration and synchronization",
)

payout_group = discord.app_commands.Group(
    name="payout",
    description="Manage monthly Medic payout eligibility",
)
medadmin_group.add_command(payout_group)


@reports_group.command(name="recent", description="View your recent editable Medic reports")
async def reports_recent(interaction: discord.Interaction):
    await myreports.callback(interaction)


@reports_group.command(name="edit", description="Edit one of your Medic reports")
@discord.app_commands.describe(report_id="Use /reports recent to find the Report ID")
async def reports_edit(interaction: discord.Interaction, report_id: str):
    await editreport.callback(interaction, report_id)


@medic_group.command(name="stats", description="View a Medic's lifetime activity")
@discord.app_commands.describe(name="The Medic's name")
async def medic_stats_group(interaction: discord.Interaction, name: str):
    await medicstats.callback(interaction, name)


@medadmin_group.command(name="status", description="Check portal, Discord, Sheets, and notification health")
async def medadmin_status(interaction: discord.Interaction):
    await portalstatus.callback(interaction)


@medadmin_group.command(name="link", description="Link a Master Log Medic to a Discord member")
@discord.app_commands.describe(
    medic="Medic name exactly as it appears in the Master Medical Log",
    member="The Discord member who owns this Medic profile",
)
async def medadmin_link(
    interaction: discord.Interaction,
    medic: str,
    member: discord.Member,
):
    await linkmedic.callback(interaction, medic, member)


@medadmin_group.command(name="sync", description="Synchronize Discord, Sheets, and/or the website")
@discord.app_commands.describe(mode="Choose how much of the MedBot ecosystem to synchronize")
@discord.app_commands.choices(
    mode=[
        discord.app_commands.Choice(name="All systems", value="all"),
        discord.app_commands.Choice(name="Website stats", value="website"),
        discord.app_commands.Choice(name="Discord ranks", value="ranks"),
        discord.app_commands.Choice(name="In-game organization", value="organization"),
        discord.app_commands.Choice(name="Full rebuild", value="full_rebuild"),
    ]
)
async def medadmin_sync(
    interaction: discord.Interaction,
    mode: discord.app_commands.Choice[str],
):
    await interaction.response.defer(ephemeral=True)

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("🚫 Admins only.")
        return

    selected = mode.value

    try:
        if selected == "ranks":
            result = await sync_discord_ranks_once()
            if not result.get("ok"):
                await interaction.followup.send(
                    f"⚠️ Rank sync failed: {result.get('message', 'Unknown error')}"
                )
                return

            await interaction.followup.send(
                "🎖️ **Rank sync complete**\n"
                f"• Linked medics checked: **{result.get('checked', 0)}**\n"
                f"• Ranks changed: **{result.get('changed', 0)}**\n"
                f"• Missing Discord IDs: **{result.get('without_discord_id', 0)}**\n"
                f"• Missing configured rank role: **{result.get('without_rank_role', 0)}**"
            )
            return

        if selected == "organization":
            result = await sync_ingame_org_once()
            if result.get("ok"):
                await interaction.followup.send(
                    "🏥 **In-game organization sync complete**\n"
                    f"• Slots filled: **{result.get('count', 0)}/{result.get('max_slots', 20)}**\n"
                    f"• Website Medic profiles matched: **{result.get('matched_medics', 0)}**"
                )
            else:
                await interaction.followup.send(
                    f"⚠️ Organization sync failed: {result.get('message', 'Unknown error')}"
                )
            return

        if selected == "website":
            if not wordpress_sync_configured():
                await interaction.followup.send(
                    "⚠️ Website sync is not configured on the VM."
                )
                return

            result = await asyncio.to_thread(push_wordpress_sync)
            if not result.get("ok"):
                await interaction.followup.send(
                    f"⚠️ Website sync failed: {result.get('message', 'Unknown error')}"
                )
                return

            wp = result.get("wordpress", {}) if isinstance(result.get("wordpress"), dict) else {}
            unmatched = wp.get("unmatched", [])
            await interaction.followup.send(
                "🌐 **Website stats sync complete**\n"
                f"• Master Log medics sent: **{result.get('sent', 0)}**\n"
                f"• WordPress matched: **{wp.get('matched', '?')}**\n"
                f"• WordPress unmatched: **{len(unmatched) if isinstance(unmatched, list) else '?'}**\n"
                f"• Master rows without Discord ID: **{len(result.get('without_discord_id', []))}**"
            )
            return

        if selected == "all":
            rank_result = await sync_discord_ranks_once(push_website=False)
            if not rank_result.get("ok") and not rank_result.get("skipped"):
                await interaction.followup.send(
                    f"⚠️ Rank sync failed: {rank_result.get('message', 'Unknown error')}"
                )
                return

            website_result = None
            if wordpress_sync_configured():
                website_result = await asyncio.to_thread(push_wordpress_sync)

            org_result = await sync_ingame_org_once()

            lines = ["✅ **All-system sync complete**"]
            if rank_result.get("ok"):
                lines.append(
                    f"• Discord ranks: **{rank_result.get('changed', 0)} changed / "
                    f"{rank_result.get('checked', 0)} checked**"
                )
            else:
                lines.append("• Discord ranks: skipped")

            if website_result and website_result.get("ok"):
                wp = website_result.get("wordpress", {}) if isinstance(website_result.get("wordpress"), dict) else {}
                lines.append(
                    f"• Website: **{wp.get('matched', '?')} Medic profiles matched**"
                )
            elif wordpress_sync_configured():
                lines.append("• Website: ⚠️ sync failed")
            else:
                lines.append("• Website: not configured")

            if org_result.get("ok"):
                lines.append(
                    f"• In-game organization: **{org_result.get('count', 0)}/"
                    f"{org_result.get('max_slots', 20)}**"
                )
            else:
                lines.append("• In-game organization: ⚠️ sync failed")

            await interaction.followup.send("\n".join(lines))
            return

        if selected == "full_rebuild":
            # Rank first so all adjusted BP is rebuilt from the authoritative Discord rank.
            rank_result = await sync_discord_ranks_once(push_website=False)
            if not rank_result.get("ok") and not rank_result.get("skipped"):
                await interaction.followup.send(
                    f"⚠️ Rank sync failed before rebuild: {rank_result.get('message', 'Unknown error')}"
                )
                return

            await asyncio.to_thread(get_raw_records_cached, True)
            master_updated = bool(await asyncio.to_thread(update_master_log))
            await asyncio.to_thread(update_all_leaderboards)

            website_result = None
            if wordpress_sync_configured():
                website_result = await asyncio.to_thread(push_wordpress_sync)

            org_result = await sync_ingame_org_once()

            lines = [
                "🛠️ **Full rebuild complete**",
                f"• Master Log rebuilt: **{'Yes' if master_updated else 'No changes / safely skipped'}**",
                "• Monthly leaderboard sheets rebuilt: **Yes**",
            ]
            if rank_result.get("ok"):
                lines.append(
                    f"• Discord ranks: **{rank_result.get('changed', 0)} changed**"
                )

            if website_result and website_result.get("ok"):
                wp = website_result.get("wordpress", {}) if isinstance(website_result.get("wordpress"), dict) else {}
                lines.append(f"• Website matched: **{wp.get('matched', '?')}**")
            elif wordpress_sync_configured():
                lines.append("• Website: ⚠️ sync failed")

            if org_result.get("ok"):
                lines.append(
                    f"• In-game organization: **{org_result.get('count', 0)}/"
                    f"{org_result.get('max_slots', 20)}**"
                )

            await interaction.followup.send("\n".join(lines))
            return

        await interaction.followup.send("⚠️ Unknown sync mode.")

    except Exception as e:
        await interaction.followup.send(f"⚠️ Sync error: {e}")



@payout_group.command(name="optout", description="Exclude a Medic from pay for one month")
@discord.app_commands.describe(
    medic="Medic name from the Master Log (works even if they left Discord)",
    year="Year, e.g. 2026",
    month="Month number, 1-12",
)
async def medadmin_payout_optout(
    interaction: discord.Interaction,
    medic: str,
    year: int,
    month: int,
):
    await interaction.response.defer(ephemeral=True)

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("🚫 Admins only.")
        return

    if month < 1 or month > 12:
        await interaction.followup.send("❌ Month must be 1–12.")
        return

    linked = await asyncio.to_thread(find_payout_medic_by_name, medic)
    if not linked:
        await interaction.followup.send(
            f"⚠️ I could not uniquely match **{medic}** in the Master Medical Log. "
            "Choose a name from autocomplete or type the full Medic name."
        )
        return

    medic_name = str(linked.get("Medic", "") or medic).strip()
    payout_identity = payout_identity_for_medic_row(linked)
    if not payout_identity:
        await interaction.followup.send(
            f"⚠️ **{medic_name}** has no usable payout identity in the Master Medical Log."
        )
        return

    await asyncio.to_thread(
        set_medic_payout_eligibility, year, month, payout_identity, False
    )
    await asyncio.to_thread(get_raw_records_cached, True)
    await asyncio.to_thread(update_single_leaderboard, year, month)

    snapshot = await asyncio.to_thread(calculate_monthly_payout_snapshot, year, month)
    month_name = datetime(year, month, 1).strftime("%B")
    identity_note = (
        "stored Discord ID"
        if not str(payout_identity).startswith("name:")
        else "Master Log name"
    )

    await interaction.followup.send(
        f"✅ **{medic_name}** opted out of the **{month_name} {year}** payout.\n"
        "Their reports, BP, jobs, leaderboard position, and rank progress are unchanged.\n"
        f"Identity used: **{identity_note}** — they do not need to still be in Discord.\n"
        f"💰 **{snapshot['pool']:,} Ryo** is now redistributed across "
        f"**{snapshot['eligible_count']} eligible Medic(s)**."
    )


@medadmin_payout_optout.autocomplete("medic")
async def payout_optout_medic_autocomplete(
    interaction: discord.Interaction,
    current: str,
):
    rows = await asyncio.to_thread(payout_medic_rows)
    query = str(current or "").strip().lower()

    names = []
    for row in rows:
        name = str(row.get("Medic", "") or "").strip()
        if not name:
            continue
        if query and query not in name.lower():
            continue
        if name not in names:
            names.append(name)

    names.sort(key=str.lower)
    return [
        discord.app_commands.Choice(name=name[:100], value=name[:100])
        for name in names[:25]
    ]


@payout_group.command(name="optin", description="Restore a Medic's pay eligibility for one month")
@discord.app_commands.describe(
    medic="Medic name from the Master Log (works even if they left Discord)",
    year="Year, e.g. 2026",
    month="Month number, 1-12",
)
async def medadmin_payout_optin(
    interaction: discord.Interaction,
    medic: str,
    year: int,
    month: int,
):
    await interaction.response.defer(ephemeral=True)

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("🚫 Admins only.")
        return

    if month < 1 or month > 12:
        await interaction.followup.send("❌ Month must be 1–12.")
        return

    linked = await asyncio.to_thread(find_payout_medic_by_name, medic)
    if not linked:
        await interaction.followup.send(
            f"⚠️ I could not uniquely match **{medic}** in the Master Medical Log. "
            "Choose a name from autocomplete or type the full Medic name."
        )
        return

    medic_name = str(linked.get("Medic", "") or medic).strip()
    payout_identity = payout_identity_for_medic_row(linked)
    if not payout_identity:
        await interaction.followup.send(
            f"⚠️ **{medic_name}** has no usable payout identity in the Master Medical Log."
        )
        return

    await asyncio.to_thread(
        set_medic_payout_eligibility, year, month, payout_identity, True
    )
    await asyncio.to_thread(get_raw_records_cached, True)
    await asyncio.to_thread(update_single_leaderboard, year, month)

    snapshot = await asyncio.to_thread(calculate_monthly_payout_snapshot, year, month)
    month_name = datetime(year, month, 1).strftime("%B")

    await interaction.followup.send(
        f"✅ **{medic_name}** is eligible again for the **{month_name} {year}** payout.\n"
        f"💰 The **{snapshot['pool']:,} Ryo** pool has been recalculated across "
        f"**{snapshot['eligible_count']} eligible Medic(s)**."
    )


@medadmin_payout_optin.autocomplete("medic")
async def payout_optin_medic_autocomplete(
    interaction: discord.Interaction,
    current: str,
):
    rows = await asyncio.to_thread(payout_medic_rows)
    query = str(current or "").strip().lower()

    names = []
    for row in rows:
        name = str(row.get("Medic", "") or "").strip()
        if not name:
            continue
        if query and query not in name.lower():
            continue
        if name not in names:
            names.append(name)

    names.sort(key=str.lower)
    return [
        discord.app_commands.Choice(name=name[:100], value=name[:100])
        for name in names[:25]
    ]


@payout_group.command(name="status", description="Show payout eligibility and distribution for one month")
@discord.app_commands.describe(
    year="Year, e.g. 2026",
    month="Month number, 1-12",
)
async def medadmin_payout_status(
    interaction: discord.Interaction,
    year: int,
    month: int,
):
    await interaction.response.defer(ephemeral=True)

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("🚫 Admins only.")
        return

    if month < 1 or month > 12:
        await interaction.followup.send("❌ Month must be 1–12.")
        return

    snapshot = await asyncio.to_thread(calculate_monthly_payout_snapshot, year, month)
    month_name = datetime(year, month, 1).strftime("%B")

    if not snapshot["rows"]:
        await interaction.followup.send(
            f"📋 No Medic reports found for **{month_name} {year}**."
        )
        return

    opted_rows = [row for row in snapshot["rows"] if row["opted_out"]]
    lines = [
        f"💰 **Payout Status — {month_name} {year}**",
        f"**Pool:** {snapshot['pool']:,} Ryo",
        f"**Eligible:** {snapshot['eligible_count']}",
        f"**Opted out:** {snapshot['opted_out_count']}",
    ]

    if opted_rows:
        lines.append("")
        lines.append("**Opted out:**")
        for row in opted_rows[:20]:
            lines.append(
                f"• {row['medic']} — {row['adjusted_points']:.2f} adjusted BP — **0 Ryo**"
            )
        if len(opted_rows) > 20:
            lines.append(f"• +{len(opted_rows) - 20} more")

    lines.append("")
    lines.append("**Eligible payout preview:**")
    eligible_rows = [row for row in snapshot["rows"] if not row["opted_out"]]
    for row in eligible_rows[:15]:
        lines.append(
            f"• {row['medic']} — **{row['pay']:,.2f} Ryo** "
            f"({row['adjusted_points']:.2f} adjusted BP)"
        )
    if len(eligible_rows) > 15:
        lines.append(f"• +{len(eligible_rows) - 15} more")

    await interaction.followup.send("\n".join(lines))


@payout_group.command(name="recalculate", description="Recalculate one month's payout without changing activity")
@discord.app_commands.describe(
    year="Year, e.g. 2026",
    month="Month number, 1-12",
)
async def medadmin_payout_recalculate(
    interaction: discord.Interaction,
    year: int,
    month: int,
):
    await interaction.response.defer(ephemeral=True)

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("🚫 Admins only.")
        return

    if month < 1 or month > 12:
        await interaction.followup.send("❌ Month must be 1–12.")
        return

    await asyncio.to_thread(get_raw_records_cached, True)
    await asyncio.to_thread(update_single_leaderboard, year, month)
    snapshot = await asyncio.to_thread(calculate_monthly_payout_snapshot, year, month)

    month_name = datetime(year, month, 1).strftime("%B")
    await interaction.followup.send(
        f"🔄 **{month_name} {year} payout recalculated**\n"
        f"• Pool: **{snapshot['pool']:,} Ryo**\n"
        f"• Eligible Medics: **{snapshot['eligible_count']}**\n"
        f"• Opted out: **{snapshot['opted_out_count']}**\n"
        "• Reports/BP/rank progress: **unchanged**"
    )


@medadmin_group.command(name="leaderboard", description="Rebuild one month's leaderboard sheet")
@discord.app_commands.describe(year="Year, e.g. 2026", month="Month number, 1-12")
async def medadmin_leaderboard(
    interaction: discord.Interaction,
    year: int,
    month: int,
):
    await rebuild_leaderboard.callback(interaction, year, month)


@medadmin_group.command(name="ryo", description="Set the monthly Medic payout pool")
@discord.app_commands.describe(
    year="Year, e.g. 2026",
    month="Month number, 1-12",
    amount="Total Ryo for that month",
)
async def medadmin_ryo(
    interaction: discord.Interaction,
    year: int,
    month: int,
    amount: int,
):
    await set_ryo.callback(interaction, year, month, amount)


# Register only the grouped replacements in the guild command picker.
_guild = discord.Object(id=GUILD_ID)
tree.add_command(reports_group, guild=_guild)
tree.add_command(medic_group, guild=_guild)
tree.add_command(medadmin_group, guild=_guild)

# Remove the old top-level commands from Discord's visible command list.
# Their Command objects remain available above so the grouped wrappers can reuse
# the same callbacks without duplicating the tested command logic.
for _legacy_command_name in (
    "setrank",
    "syncranks",
    "linkmedic",
    "updatelogs",
    "rebuildleaderboard",
    "setryo",
    "syncwebsite",
    "syncorg",
    "portalstatus",
    "medicstats",
    "myreports",
    "editreport",
):
    tree.remove_command(_legacy_command_name, guild=_guild)



@tree.command(name="report", description="Submit a medic report")
@discord.app_commands.guilds(discord.Object(id=GUILD_ID))
async def report(interaction: discord.Interaction):
    """Submit a report using Discord member selection so every medic gets a stable ID."""

    class ReportSetupView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=180)
            self.owner_id = interaction.user.id
            self.selected_members = []
            self.selected_job = None
            self.add_item(MedicSelect(self))
            self.add_item(JobSelect(self))

        async def interaction_check(self, view_interaction: discord.Interaction) -> bool:
            if view_interaction.user.id != self.owner_id:
                await view_interaction.response.send_message(
                    "🚫 This report menu is not for you.", ephemeral=True
                )
                return False
            return True

        @discord.ui.button(label="Continue to report", style=discord.ButtonStyle.primary)
        async def continue_report(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            if not self.selected_members:
                await button_interaction.response.send_message(
                    "⚠️ Select at least one medic first.", ephemeral=True
                )
                return
            if not self.selected_job:
                await button_interaction.response.send_message(
                    "⚠️ Choose a job type first.", ephemeral=True
                )
                return

            medic_list = [member_display_name(m) for m in self.selected_members]
            medic_ids = [str(m.id) for m in self.selected_members]
            job_type = self.selected_job

            class ReportModal(discord.ui.Modal, title="Medic Job Report"):
                date = discord.ui.TextInput(
                    label="Date (blank = today, MM/DD/YYYY)",
                    required=False,
                    placeholder="01/15/2025",
                )
                time_range = discord.ui.TextInput(
                    label="Time Range (HH:MM or H:MM AM/PM)",
                    placeholder="5:00 pm - 6:00 pm",
                )
                clients = discord.ui.TextInput(
                    label="Clients (Separate by ,)",
                    placeholder="Example: PlayerOne, PlayerTwo",
                )
                description = discord.ui.TextInput(
                    label="Description", style=discord.TextStyle.long
                )

                def parse_time(self, t):
                    for fmt in ("%H:%M", "%I:%M %p"):
                        try:
                            return datetime.strptime(t.strip(), fmt)
                        except ValueError:
                            pass
                    return None

                def parse_date(self, d):
                    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
                        try:
                            return datetime.strptime(d.strip(), fmt).date()
                        except ValueError:
                            pass
                    return None

                async def on_submit(self, modal_interaction: discord.Interaction):
                    try:
                        await modal_interaction.response.defer(ephemeral=True)

                        clients_list = [p.strip() for p in split_names(self.clients.value)]
                        date_obj = (
                            self.parse_date(self.date.value)
                            if self.date.value.strip()
                            else datetime.now().date()
                        )

                        if not date_obj:
                            await modal_interaction.followup.send(
                                "⚠️ Invalid date format. Use `MM/DD/YYYY`, `YYYY-MM-DD`, or leave it blank for today.",
                                ephemeral=True,
                            )
                            return

                        t = re.split(r"-|to", self.time_range.value)
                        if len(t) < 2:
                            await modal_interaction.followup.send(
                                "⚠️ Invalid time range. Use something like `5:00 pm - 6:00 pm`.",
                                ephemeral=True,
                            )
                            return

                        start = self.parse_time(t[0])
                        end = self.parse_time(t[1])
                        if not start or not end:
                            await modal_interaction.followup.send(
                                "⚠️ Invalid time format. Use `HH:MM` or `H:MM AM/PM` with `-` or `to`.",
                                ephemeral=True,
                            )
                            return

                        start_dt = datetime.combine(date_obj, start.time())
                        end_dt = datetime.combine(date_obj, end.time())
                        if end_dt < start_dt:
                            end_dt += timedelta(days=1)

                        duration = int((end_dt - start_dt).total_seconds() // 60)
                        points = calculate_points(job_type, duration, len(clients_list))
                        desc = self.description.value.strip()

                        embed = build_report_embed(
                            job_type, desc, date_obj, medic_list,
                            duration, len(clients_list), points,
                        )
                        msg = await modal_interaction.channel.send(embed=embed)

                        link = f"https://discord.com/channels/{modal_interaction.guild.id}/{modal_interaction.channel.id}/{msg.id}"
                        hyperlink = f'=HYPERLINK("{link}", "View Report")'

                        report_row = [
                            datetime.now().strftime("%m/%d/%Y %H:%M"),
                            ", ".join(medic_list),
                            job_type,
                            f"{duration} min",
                            points,
                            len(clients_list),
                            ", ".join(clients_list),
                            desc,
                            date_obj.strftime("%m/%d/%Y"),
                            hyperlink,
                            f"'{modal_interaction.user.id}",
                            str(modal_interaction.user),
                            f"'{msg.id}",
                            f"'{modal_interaction.channel.id}",
                            serialize_medic_discord_ids(medic_ids),
                        ]

                        if len(report_row) != len(REPORT_HEADERS):
                            raise ValueError(
                                f"Report row has {len(report_row)} columns, expected {len(REPORT_HEADERS)}"
                            )

                        SHEET.append_row(
                            report_row,
                            value_input_option="USER_ENTERED",
                            table_range="A1:O1",
                        )
                        SHEET.resize(cols=len(REPORT_HEADERS))
                        invalidate_raw_cache()
                        await rebuild_after_report_change()

                        await modal_interaction.followup.send(
                            "✅ Report logged with Discord-linked medic identities and sheets queued for update (throttled).",
                            ephemeral=True,
                        )
                    except Exception as e:
                        await modal_interaction.followup.send(f"⚠️ Error: {e}", ephemeral=True)

            await button_interaction.response.send_modal(ReportModal())

    class MedicSelect(discord.ui.UserSelect):
        def __init__(self, parent_view):
            self.parent_view = parent_view
            super().__init__(
                placeholder="Select the medic(s) who participated...",
                min_values=1,
                max_values=25,
            )

        async def callback(self, select_interaction: discord.Interaction):
            selected = list(self.values)
            if any(getattr(member, "bot", False) for member in selected):
                await select_interaction.response.send_message(
                    "⚠️ Bots cannot be selected as medics.", ephemeral=True
                )
                return
            self.parent_view.selected_members = selected
            await select_interaction.response.defer()

    class JobSelect(discord.ui.Select):
        def __init__(self, parent_view):
            self.parent_view = parent_view
            options = [discord.SelectOption(label=label, value=value) for label, value in JOB_OPTIONS]
            super().__init__(placeholder="Choose Job Type...", options=options)

        async def callback(self, select_interaction: discord.Interaction):
            self.parent_view.selected_job = self.values[0]
            await select_interaction.response.defer()

    await interaction.response.send_message(
        "Select the **medic(s)** and **job type**, then click **Continue to report**.\n"
        "The bot will save each medic's Discord ID with the report so names can change without breaking their stats.",
        view=ReportSetupView(),
        ephemeral=True,
    )


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    """Update Sheets + WordPress immediately when a Medical Corps Discord role changes."""
    if after.guild.id != GUILD_ID:
        return

    if {role.id for role in before.roles} == {role.id for role in after.roles}:
        return

    async def _sync_changed_member():
        try:
            result = await sync_discord_ranks_once(target_discord_ids={str(after.id)})
            if result.get("ok") and result.get("changed"):
                print(f"🎖️ Discord role change synchronized for {after}.")
        except Exception as e:
            print(f"⚠️ Rank sync after role change failed for {after}: {e}")

    asyncio.create_task(_sync_changed_member())


@bot.event
async def on_ready():
    try:
        ensure_reports_sheet_shape()
        ensure_master_sheet_shape()
    except Exception as e:
        print(f"⚠️ Could not lock sheet shapes: {e}")

    synced = await tree.sync(guild=discord.Object(id=GUILD_ID))
    print(f"Synced {len(synced)} commands to guild {GUILD_ID}")
    print(f"Logged in as {bot.user}")

    global _notification_worker_task, _org_worker_task, _rank_worker_task
    if wordpress_notification_configured() and (_notification_worker_task is None or _notification_worker_task.done()):
        _notification_worker_task = asyncio.create_task(wordpress_notification_worker())
        print(f"🔔 WordPress Discord notifications enabled (poll every {WORDPRESS_NOTIFICATION_POLL_SECONDS}s)")
        print(f"🔗 Notification endpoint: {WORDPRESS_NOTIFICATION_URL}")
    elif not wordpress_notification_configured():
        print("ℹ️ WordPress Discord notification polling is not configured.")

    if wordpress_org_configured() and (_org_worker_task is None or _org_worker_task.done()):
        _org_worker_task = asyncio.create_task(wordpress_org_worker())
        print(f"🏥 In-game Medical Corp role sync enabled (every {WORDPRESS_ORG_SYNC_SECONDS}s)")
    elif not wordpress_org_configured():
        print("ℹ️ In-game Medical Corp role sync is not configured.")


    if wordpress_rank_configured() and (_rank_worker_task is None or _rank_worker_task.done()):
        _rank_worker_task = asyncio.create_task(wordpress_rank_worker())
        print(
            f"🎖️ Discord Medic rank sync enabled "
            f"(every {WORDPRESS_RANK_SYNC_SECONDS}s + immediate role-change events)"
        )
    elif not wordpress_rank_configured():
        print("ℹ️ Discord Medic rank sync is not configured.")


bot.run(DISCORD_TOKEN)
