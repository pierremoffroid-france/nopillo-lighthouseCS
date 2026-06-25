#!/usr/bin/env python3
"""
📡 Lighthouse Customer Operations — refresh script
===================================================
Fetches ticket + contact data from HubSpot, computes all metrics,
and regenerates the Lighthouse dashboard HTML.

Runs hourly via launchd on macOS. Zero external dependencies (stdlib only).

Usage:
    python3 refresh.py

Configuration:
    Edit config.json with your HubSpot Private App token.
"""

import json
import os
import sys
import time
import math
import datetime
import pathlib
import logging
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from collections import defaultdict
from typing import Optional

# ---------- FUSEAU HORAIRE ----------
# Force l'heure de Paris pour TOUS les datetime.now() du script, quel que soit
# l'endroit où il tourne (Mac = déjà Paris ; GitHub Actions = UTC par défaut).
# Sans ça, l'horodatage "Dernière MAJ" s'affiche en UTC (-2h en été) dans le cloud.
os.environ['TZ'] = 'Europe/Paris'
try:
    time.tzset()  # applique le TZ au process (POSIX : Linux/macOS)
except AttributeError:
    pass  # Windows n'a pas tzset ; sans effet (on tourne sur Linux/Mac de toute façon)

# ---------- PATHS ----------
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / 'config.json'
TEMPLATE_PATH = SCRIPT_DIR / 'template.html'
OUTPUT_PATH = SCRIPT_DIR / 'lighthouse.html'
DATA_PATH = SCRIPT_DIR / 'data.json'  # Last computed data (for debugging)
LOG_PATH = SCRIPT_DIR / 'refresh.log'

# ---------- LOGGING ----------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

# ---------- CONSTANTS ----------
HS_API_BASE = 'https://api.hubapi.com'
CC_PIPELINE_ID = '253837526'  # Customer Care
FETCH_FROM_YEAR = 2025          # Fetch all tickets since Jan 1st 2025

# IC mapping: owner_id → (display_name, team_leader, level)
# v9.4 (#4 — 18/06/2026) : la table peut désormais être surchargée par un
# fichier ic_config.json externe (édité via l'onglet Paramètres de Lighthouse).
# Si le fichier existe et est valide, il REMPLACE la table ci-dessous.
# Sinon, on retombe sur ce mapping codé en dur (fallback de sécurité).
#
# Format ic_config.json :
#   { "ic_map": [ {"owner_id": 31560323, "name": "Aaron", "tl": "cyrielle", "level": "N1"}, ... ] }
#
# tl ∈ {"cyrielle", "enzo", "pierre"} ; level libre (N1, N2, Immat, Retention, ...)

_IC_MAP_FALLBACK = {
    31560323: ('Aaron', 'cyrielle', 'N1'),
    32138611: ('Alexis', 'cyrielle', 'N1'),
    32138600: ('Athalia', 'cyrielle', 'N1'),
    32837724: ('Emeline', 'cyrielle', 'N1'),
    31643141: ('Ivana', 'cyrielle', 'N1'),
    32138628: ('Manon', 'cyrielle', 'N1'),
    29996724: ('Priscilla', 'cyrielle', 'N1+Liasses'),
    844737572: ('Hamda', 'cyrielle', 'Immat'),
    76990561: ('Alexia', 'enzo', 'N1'),
    31560329: ('Alice', 'enzo', 'N1'),
    30815127: ('Andrea', 'enzo', 'N1'),
    30815153: ('Angeline', 'enzo', 'N1'),
    31560320: ('Gaëlle', 'enzo', 'N1'),
    32834520: ('Jordan', 'enzo', 'N1'),
    29996718: ('Jade', 'enzo', 'N2'),
    1990978149: ('Magda', 'enzo', 'N2'),
    75453551: ('Lilian', 'pierre', 'Retention'),
    650299108: ('Mathieu', 'pierre', 'Retention'),
}


def _load_ic_map():
    """
    Charge IC_MAP depuis ic_config.json (à côté de refresh.py) si présent et valide.
    Sinon retourne le fallback codé en dur. Ne lève jamais : sécurité avant tout.
    """
    path = Path(__file__).resolve().parent / 'ic_config.json'
    if not path.exists():
        return dict(_IC_MAP_FALLBACK)
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
        entries = raw.get('ic_map', [])
        out = {}
        for e in entries:
            oid = int(e['owner_id'])
            out[oid] = (str(e['name']), str(e['tl']), str(e.get('level', 'N1')))
        if not out:
            raise ValueError("ic_map vide")
        # log différé (log pas encore configuré à l'import) : on stocke un flag
        _load_ic_map._source = f"ic_config.json ({len(out)} IC)"
        return out
    except Exception as e:
        _load_ic_map._source = f"FALLBACK codé en dur (ic_config.json invalide : {e})"
        return dict(_IC_MAP_FALLBACK)


_load_ic_map._source = "FALLBACK codé en dur (ic_config.json absent)"
IC_MAP = _load_ic_map()

# Pipeline stages internal IDs for "LOST" detection
# The ticket status "LOST [NE PAS UTILISER]" has a specific stage ID
# Plus we need to detect tickets that passed through Email-Lost or Callback-Lost stages
# Detection via: current stage = LOST + presence of time_in_stage for Email-Lost or Callback-Lost
EMAIL_LOST_STAGE_LABEL = 'Email - Lost'
CALLBACK_LOST_STAGE_LABEL = 'Callback - Lost'
LOST_STATUS_LABEL = 'LOST [NE PAS UTILISER]'

# Month ranges (ISO week-based, approx)
# Maps week number → month index for 2025 and 2026
def week_to_month(year, week):
    """Approximate ISO week to month mapping."""
    # Jan=0 ... Déc=11
    # Use the Thursday of the ISO week as the reference (ISO-standard)
    d = datetime.datetime.fromisocalendar(year, week, 4)
    return (year, d.month - 1)

# ---------- CONFIG ----------
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        log.error(f"config.json introuvable à {CONFIG_PATH}")
        log.error("Copie config.json.example vers config.json et mets-y ton token HubSpot")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    if not cfg.get('hubspot_token'):
        log.error("config.json n'a pas de champ 'hubspot_token' renseigné")
        sys.exit(1)
    return cfg

# ---------- HUBSPOT API ----------
class HubSpotClient:
    def __init__(self, token: str):
        self.token = token
        self.session_headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        }

    def _request(self, method: str, path: str, body: Optional[dict] = None, max_retries: int = 3) -> dict:
        url = HS_API_BASE + path
        data = json.dumps(body).encode('utf-8') if body else None
        req = urllib.request.Request(url, data=data, method=method, headers=self.session_headers)
        for attempt in range(max_retries):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read().decode('utf-8'))
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    # Rate limited; back off and retry
                    retry_after = int(e.headers.get('Retry-After', '2'))
                    log.warning(f"Rate limited; retrying after {retry_after}s (attempt {attempt+1}/{max_retries})")
                    time.sleep(retry_after)
                    continue
                elif 500 <= e.code < 600 and attempt < max_retries - 1:
                    log.warning(f"HubSpot server error {e.code}; retrying in {2**attempt}s")
                    time.sleep(2 ** attempt)
                    continue
                else:
                    body_text = e.read().decode('utf-8', errors='replace')
                    log.error(f"HubSpot API error {e.code} on {method} {path}: {body_text[:300]}")
                    raise
            except urllib.error.URLError as e:
                if attempt < max_retries - 1:
                    log.warning(f"Network error {e}; retrying in {2**attempt}s")
                    time.sleep(2 ** attempt)
                    continue
                raise
        raise RuntimeError(f"Failed after {max_retries} retries")

    def search(self, object_type: str, filters: list, properties: list, sort: Optional[list] = None, limit: int = 100) -> list:
        """Paginated search. Returns all matching objects."""
        all_results = []
        after = None
        while True:
            body = {
                'filterGroups': filters,
                'properties': properties,
                'limit': limit,
            }
            if sort:
                body['sorts'] = sort
            if after:
                body['after'] = after
            resp = self._request('POST', f'/crm/v3/objects/{object_type}/search', body)
            results = resp.get('results', [])
            all_results.extend(results)
            paging = resp.get('paging', {})
            next_page = paging.get('next', {})
            after = next_page.get('after')
            if not after:
                break
            # Safety: HubSpot limits search to 10k results per query
            if len(all_results) >= 10000:
                log.warning(f"Search result limit (10k) reached for {object_type}, may be truncated")
                break
        return all_results

    def search_count(self, object_type: str, filters: list) -> int:
        """Return total count matching filters (uses 'total' field of the search API,
        which is NOT plafonné à 10k contrairement à la pagination des résultats).
        """
        body = {
            'filterGroups': filters,
            'properties': ['hs_object_id'],  # minimal payload
            'limit': 1,
        }
        resp = self._request('POST', f'/crm/v3/objects/{object_type}/search', body)
        return int(resp.get('total', 0))

# ---------- FETCH DATA ----------
def fetch_cc_tickets(client: HubSpotClient) -> list:
    """Fetch all Customer Care tickets created since Jan 1 2025."""
    log.info("Fetching CC tickets from HubSpot...")
    properties = [
        'createdate', 'closed_date',
        'hs_pipeline', 'hs_pipeline_stage', 'hubspot_owner_id',
        'niveau_de_ticket',
        'subject',
        'hs_time_to_first_response_sla_status',
        'time_to_first_agent_reply',
        'hs_time_to_first_response_in_operating_hours',
        'hs_feedback_last_ces_rating',
        'hs_num_times_contacted',
        'hs_ticket_reopened_at',
        'hs_last_closed_date',
        'hs_all_associated_contact_emails',
    ]
    # HubSpot search is paginated at 100 results/page, 10k results max per query
    # We chunk by month to stay under the 10k limit
    all_tickets = []
    start_date = datetime.datetime(FETCH_FROM_YEAR, 1, 1)
    now = datetime.datetime.now()
    cur = start_date
    while cur < now:
        next_cur = cur + datetime.timedelta(days=30)
        if next_cur > now:
            next_cur = now
        ts_start = int(cur.timestamp() * 1000)
        ts_end = int(next_cur.timestamp() * 1000)
        filters = [{
            'filters': [
                {'propertyName': 'hs_pipeline', 'operator': 'EQ', 'value': CC_PIPELINE_ID},
                {'propertyName': 'createdate', 'operator': 'GTE', 'value': str(ts_start)},
                {'propertyName': 'createdate', 'operator': 'LT', 'value': str(ts_end)},
            ]
        }]
        chunk = client.search('tickets', filters, properties, sort=[{'propertyName': 'createdate', 'direction': 'ASCENDING'}])
        log.info(f"  {cur.strftime('%Y-%m-%d')} → {next_cur.strftime('%Y-%m-%d')}: {len(chunk)} tickets")
        all_tickets.extend(chunk)
        cur = next_cur
    log.info(f"Total CC tickets fetched: {len(all_tickets)}")
    return all_tickets

def fetch_contacts_for_liasses(client: HubSpotClient) -> list:
    """Fetch all contacts relevant to liasses campaign.

    HubSpot report 'Evolution liasses 2025 validées' counts every contact with
    statut_declaration_fiscale_2025 = Done (regardless of lifecyclestage).
    We replicate that scope here: ALL contacts that are either customers,
    or have ANY liasse declaration in flight (2024 or 2025 status set, OR
    timestamp set).

    PAGINATION FIX (v7, mai 2026) : HubSpot search API plafonne à 10k résultats
    par requête. Avec ~9056 customers + 1-3k contacts en pipeline, on dépassait
    le plafond → certains contacts étaient tronqués → bug `n_customers=10000`
    et bucket "Inconnu" gonflé dans Nouveau vs Récurrent.

    Solution : on chunk par tranches de createdate de 6 mois, en commençant
    par les plus récentes et en remontant jusqu'à 2020. Dédup par hs_object_id.
    """
    log.info("Fetching contacts (customers + liasses pipeline) from HubSpot, by date chunks...")
    properties = [
        'email', 'createdate', 'lifecyclestage',
        'hs_v2_date_entered_customer',
        'hs_v2_date_exited_customer',  # v8.2: pour calculer la base clients historique exacte (Option 1 Pierre)
        'client_status',  # v8: actif/passif/overdue/pending — pour distinguer la base totale vs base "actifs"
        'timestamp__teledeclaration_liasse_2025_finalisee',
        'timestamp___teledeclaration_liasse_2024_finalisee',
        'timestamp___teledeclaration_finalisee',
        'statut_declaration_fiscale_2025',
        'statut_declaration_fiscale_2024',
    ]
    # OR groups for "interesting" contacts (a été customer un jour OR a une liasse)
    # v8.2 : ajout du critère HAS_PROPERTY hs_v2_date_entered_customer qui capture
    # TOUS les contacts qui ont été client un jour (customer + Churned + Canceled
    # actuels). Plus simple et exhaustif que de lister tous les lifecyclestages.
    or_groups_base = [
        {'filters': [{'propertyName': 'hs_v2_date_entered_customer', 'operator': 'HAS_PROPERTY'}]},
        {'filters': [{'propertyName': 'statut_declaration_fiscale_2025', 'operator': 'HAS_PROPERTY'}]},
        {'filters': [{'propertyName': 'statut_declaration_fiscale_2024', 'operator': 'HAS_PROPERTY'}]},
        {'filters': [{'propertyName': 'timestamp__teledeclaration_liasse_2025_finalisee', 'operator': 'HAS_PROPERTY'}]},
        {'filters': [{'propertyName': 'timestamp___teledeclaration_liasse_2024_finalisee', 'operator': 'HAS_PROPERTY'}]},
    ]

    # Chunks de 6 mois sur createdate, de aujourd'hui à 2020-01-01
    seen_ids = set()
    all_contacts = []
    now = datetime.datetime.now()
    chunk_end = now + datetime.timedelta(days=1)  # inclusive de today
    fetch_floor = datetime.datetime(2020, 1, 1)
    while chunk_end > fetch_floor:
        chunk_start = chunk_end - datetime.timedelta(days=183)  # ~6 mois
        if chunk_start < fetch_floor:
            chunk_start = fetch_floor
        ts_start = int(chunk_start.timestamp() * 1000)
        ts_end = int(chunk_end.timestamp() * 1000)
        # Add createdate filter to each OR group
        chunked_filters = []
        for g in or_groups_base:
            new_filters = list(g['filters']) + [
                {'propertyName': 'createdate', 'operator': 'GTE', 'value': str(ts_start)},
                {'propertyName': 'createdate', 'operator': 'LT', 'value': str(ts_end)},
            ]
            chunked_filters.append({'filters': new_filters})
        try:
            chunk = client.search('contacts', chunked_filters, properties)
        except Exception as e:
            log.warning(f"  Chunk {chunk_start.date()} → {chunk_end.date()} échec : {e}")
            chunk_end = chunk_start
            continue
        added = 0
        for c in chunk:
            cid = c.get('id') or c.get('properties', {}).get('hs_object_id')
            if cid and cid not in seen_ids:
                seen_ids.add(cid)
                all_contacts.append(c)
                added += 1
        log.info(f"  {chunk_start.date()} → {chunk_end.date()}: {len(chunk)} contacts ({added} nouveaux)")
        chunk_end = chunk_start
    log.info(f"Total contacts fetched (déduplique) : {len(all_contacts)}")
    return all_contacts

# ---------- HELPERS ----------
def parse_ts(ts_str):
    """Parse HubSpot ISO timestamp or millisecond timestamp."""
    if not ts_str:
        return None
    try:
        # HubSpot returns ISO-8601 strings in search results
        return datetime.datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        try:
            return datetime.datetime.fromtimestamp(int(ts_str) / 1000)
        except (ValueError, TypeError):
            return None

def iso_week(dt: datetime.datetime) -> tuple:
    """Return (year, week) tuple (ISO)."""
    iso = dt.isocalendar()
    return (iso[0], iso[1])

def safe_float(v):
    try:
        return float(v) if v not in (None, '') else None
    except (ValueError, TypeError):
        return None

# ---------- ENRICH TICKETS ----------


# ═══════════════════════════════════════════════════════════════════════
# CES HISTORIQUE NOPILLO (source: cs_performance.csv vue WoW Q4.25 V2)
# 
# Pourquoi hardcodé : le CES "Customer Support Survey 3" sur HubSpot a été
# lancé le 20 mars 2026 (= W12 2026). Avant cette date, les CES étaient
# collectés via un autre outil (Typeform / autre survey HubSpot ancien) et
# ne sont pas accessibles via l'API actuelle. Pour préserver l'historique
# 1+ an, on hardcode les valeurs depuis le CSV de référence Pierre.
#
# Cutoff : 31 mars 2026 (= fin W13 2026 inclus)
#   → Avant : valeurs du CSV (CES_HEBDO_HARDCODED)
#   → À partir W14 2026 : calcul dynamique depuis HubSpot tickets
# ═══════════════════════════════════════════════════════════════════════

# Cutoff : la dernière semaine où on prend la valeur hardcodée (incluse)
HARDCODE_CUTOFF_YEAR = 2026
HARDCODE_CUTOFF_WEEK = 13  # W13 2026 = 23-29 mars 2026 inclus

# CES %Promoteurs hebdomadaire — source CSV Pierre
# Format : (year, iso_week) → % promoteurs
CES_HEBDO_HARDCODED = {
    # 2025 (W1 → W52)
    (2025, 1): 79, (2025, 2): 78, (2025, 3): 91, (2025, 4): 90,
    (2025, 5): 83, (2025, 6): 80, (2025, 7): 88, (2025, 8): 85,
    (2025, 9): 83, (2025, 10): 76, (2025, 11): 85, (2025, 12): 86,
    (2025, 13): 76, (2025, 14): 85, (2025, 15): 79, (2025, 16): 79,
    (2025, 17): 80, (2025, 18): 83, (2025, 19): 80, (2025, 20): 74,
    (2025, 21): 83, (2025, 22): 81, (2025, 23): 80, (2025, 24): 81,
    (2025, 25): 84, (2025, 26): 87, (2025, 27): 58, (2025, 28): 82,
    (2025, 29): 77, (2025, 30): 81, (2025, 31): 96, (2025, 32): 83,
    (2025, 33): 83, (2025, 34): 83, (2025, 35): 67, (2025, 36): 73,
    (2025, 37): 68, (2025, 38): 80, (2025, 39): 92, (2025, 40): 77,
    (2025, 41): 86, (2025, 42): 75, (2025, 43): 81, (2025, 44): 81,
    (2025, 45): 81, (2025, 46): 58, (2025, 47): 84, (2025, 48): 74,
    (2025, 49): 72, (2025, 50): 75, (2025, 51): 78, (2025, 52): 79,
    # 2026 (W1 → W13 inclus)
    (2026, 1): 78, (2026, 2): 80, (2026, 3): 87, (2026, 4): 74,
    (2026, 5): 78, (2026, 6): 87, (2026, 7): 89, (2026, 8): 86,
    (2026, 9): 84, (2026, 10): 86, (2026, 11): 75, (2026, 12): 74,
    (2026, 13): 78,
    # W14 2026 → futur : valeurs dynamiques HubSpot (compute_weekly_ces)
}

# CES %Promoteurs mensuel — agrégé depuis hebdo CSV Pierre
# Format : (year, month) → % promoteurs (Jan25 → Mar26 = 15 mois)
CES_MENSUEL_HARDCODED = {
    (2025, 1): 84.5,  # Jan25
    (2025, 2): 84.0,  # Fév25
    (2025, 3): 81.2,  # Mar25
    (2025, 4): 80.8,  # Avr25
    (2025, 5): 80.2,  # Mai25
    (2025, 6): 83.0,  # Jui25
    (2025, 7): 78.8,  # Jul25
    (2025, 8): 79.0,  # Aoû25
    (2025, 9): 78.2,  # Sep25
    (2025, 10): 80.0, # Oct25
    (2025, 11): 74.2, # Nov25
    (2025, 12): 76.0, # Déc25
    (2026, 1): 79.4,  # Jan26
    (2026, 2): 86.5,  # Fév26
    (2026, 3): 78.2,  # Mar26
    # Avr26 → futur : dynamique HubSpot
}


def is_week_hardcoded(year: int, iso_week: int) -> bool:
    """True si cette semaine doit utiliser la valeur CSV hardcoded."""
    if year < HARDCODE_CUTOFF_YEAR:
        return True
    if year == HARDCODE_CUTOFF_YEAR and iso_week <= HARDCODE_CUTOFF_WEEK:
        return True
    return False


def is_month_hardcoded(year: int, month: int) -> bool:
    """True si ce mois doit utiliser la valeur CSV hardcoded."""
    if year < 2026:
        return True
    if year == 2026 and month <= 3:  # Jan, Fév, Mar 2026 inclus
        return True
    return False


def enrich_ticket(t: dict) -> dict:
    """Normalize a HubSpot ticket into a flat dict for easy computation."""
    p = t.get('properties', {})
    created = parse_ts(p.get('createdate'))
    closed = parse_ts(p.get('closed_date'))
    owner = safe_float(p.get('hubspot_owner_id'))
    owner_id = int(owner) if owner else None
    ic_info = IC_MAP.get(owner_id, (None, None, None))
    sla_status = (p.get('hs_time_to_first_response_sla_status') or '').strip()
    ces = safe_float(p.get('hs_feedback_last_ces_rating'))
    stage = (p.get('hs_pipeline_stage') or '').strip()
    # Ticket status in HubSpot is derived from stage label,
    # but stage names are stored as IDs, not labels, in the API.
    # We'll need a separate mapping lookup (see compute_stats).
    # Extract ALL associated contact emails (for new-vs-existing customer attribution).
    # Avant v7 : on prenait split(';')[0] = 1er email seulement → matching raté quand
    # un client a plusieurs emails (alias work + perso, ancien email migré, etc.).
    # Depuis v7 : on garde la liste complète, et compute_new_vs_existing_monthly
    # essaye chaque email jusqu'à trouver un match contact.
    emails_str = (p.get('hs_all_associated_contact_emails') or '').strip()
    contact_emails = [e.strip().lower() for e in emails_str.split(';') if e.strip()]
    # contact_email reste exposé pour rétrocompat (autres fonctions qui l'utilisent)
    contact_email = contact_emails[0] if contact_emails else None
    # "Demande de callback" tickets are auto-prefixed by the callback web form.
    # Pattern verified on HubSpot: every ticket where the user filled the
    # "Faire une demande de rappel" form has subject starting with this prefix.
    subject = (p.get('subject') or '').strip()
    is_callback = subject.lower().startswith('demande de callback')
    return {
        'id': t.get('id'),
        'created': created,
        'closed': closed,
        'owner_id': owner_id,
        'ic_name': ic_info[0],
        'ic_tl': ic_info[1],
        'ic_level': ic_info[2],
        'niveau': (p.get('niveau_de_ticket') or '').lower(),
        'sla_status': sla_status,
        'sla_ok': sla_status == '3',
        'sla_has': sla_status in ('3', '4'),
        'ces': ces,
        'ces_has': ces is not None,
        'ces_promoter': ces is not None and ces >= 6,
        'touches': safe_float(p.get('hs_num_times_contacted')),
        'reopened_at': parse_ts(p.get('hs_ticket_reopened_at')),
        'stage_id': stage,
        # Note: 'in_operating_hours' is misleadingly named — HubSpot returns this in MILLISECONDS, not hours
        'resp_time_h': (safe_float(p.get('hs_time_to_first_response_in_operating_hours')) or 0) / 3600000.0 if p.get('hs_time_to_first_response_in_operating_hours') else None,
        'year_created': created.year if created else None,
        'week_created': created.isocalendar()[1] if created else None,
        'contact_email': contact_email,
        'contact_emails': contact_emails,  # v7: liste complète pour matching robuste
        'subject': subject,
        'is_callback': is_callback,
    }

def detect_lost_flags(tickets: list, pipeline_info: dict) -> None:
    """
    For each ticket, detect if it ended as Email-Lost or Callback-Lost.
    pipeline_info maps stage ID → stage label.
    We detect a ticket as "lost via email" if its final stage is a LOST stage
    and it passed through Email-Lost at some point.
    Without per-ticket stage history (too expensive), we approximate:
    - If current stage label contains "Lost", treat it as lost.
    - Split between Email-Lost and Callback-Lost based on label.
    """
    for t in tickets:
        stage_id = t['stage_id']
        stage_label = pipeline_info.get(stage_id, '') or ''
        label_low = stage_label.lower()
        t['ended_email_lost'] = 'email' in label_low and 'lost' in label_low
        t['ended_callback_lost'] = ('callback' in label_low or 'call-back' in label_low) and 'lost' in label_low
        t['is_lost'] = t['ended_email_lost'] or t['ended_callback_lost'] or (
            'lost' in label_low and 'ne pas utiliser' in label_low
        )
        t['is_resolved'] = 'résolution' in label_low or 'resolution' in label_low

def fetch_calls_by_ic(client: HubSpotClient, ref_now: datetime.datetime) -> dict:
    """
    #3.1 — Compte les appels OUTBOUND réellement passés par IC (owner ∈ IC_MAP),
    par période. N'inclut PAS les owners hors IC_MAP (Sales/prospection exclus).

    Utilise search_count (champ 'total', non plafonné à 10k) plutôt que de
    paginer les ~18k calls/30j : 1 requête count par IC × période.

    Filtres : hubspot_owner_id = IC, hs_call_direction = OUTBOUND, hs_timestamp ∈ période.

    Returns: { period: { ic_name: n_calls_outbound } }
    """
    def period_bounds(period):
        d0 = ref_now.replace(hour=0, minute=0, second=0, microsecond=0)
        if period == 'today':
            return d0, None
        if period == 'yesterday':
            return d0 - datetime.timedelta(days=1), d0
        if period == 'thisweek':
            return d0 - datetime.timedelta(days=d0.weekday()), None
        if period == 'lastweek':
            this_mon = d0 - datetime.timedelta(days=d0.weekday())
            return this_mon - datetime.timedelta(days=7), this_mon
        if period == '30d':
            return ref_now - datetime.timedelta(days=30), None
        if period == '3m':
            return ref_now - datetime.timedelta(days=90), None
        return None, None

    def ms(dt):
        return str(int(dt.timestamp() * 1000))

    periods = ['today', 'yesterday', 'thisweek', 'lastweek', '30d', '3m']
    result = {p: {} for p in periods}
    for period in periods:
        start, end = period_bounds(period)
        for owner_id, (ic_name, tl, level) in IC_MAP.items():
            filters = [{
                'propertyName': 'hubspot_owner_id', 'operator': 'EQ', 'value': str(owner_id)
            }, {
                'propertyName': 'hs_call_direction', 'operator': 'EQ', 'value': 'OUTBOUND'
            }, {
                'propertyName': 'hs_timestamp', 'operator': 'GTE', 'value': ms(start)
            }]
            if end is not None:
                filters.append({
                    'propertyName': 'hs_timestamp', 'operator': 'LT', 'value': ms(end)
                })
            try:
                n = client.search_count('calls', [{'filters': filters}])
            except Exception as e:
                log.warning(f"calls count KO ({ic_name}/{period}): {e}")
                n = 0
            # agrège par nom d'IC (un IC peut avoir plusieurs owner_id, rare)
            result[period][ic_name] = result[period].get(ic_name, 0) + n
    total_30 = sum(result['30d'].values())
    log.info(f"Calls OUTBOUND par IC chargés · {total_30} calls CS sur 30j")
    return result


def fetch_pipeline_info(client: HubSpotClient) -> dict:
    """Return {stage_id: stage_label} for the Customer Care pipeline."""
    resp = client._request('GET', f'/crm/v3/pipelines/tickets/{CC_PIPELINE_ID}')
    stages = resp.get('stages', [])
    return {s['id']: s.get('label', '') for s in stages}

# ---------- COMPUTE STATS ----------
def filter_period_created(tickets: list, period: str, ref_now: datetime.datetime) -> list:
    """Filter tickets CREATED in a given period."""
    ref_year, ref_week = iso_week(ref_now)
    if period == 'today':
        d0 = ref_now.replace(hour=0, minute=0, second=0, microsecond=0)
        return [t for t in tickets if t['created'] and t['created'].replace(tzinfo=None) >= d0]
    elif period == 'yesterday':
        d0 = (ref_now - datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        d1 = ref_now.replace(hour=0, minute=0, second=0, microsecond=0)
        return [t for t in tickets if t['created'] and d0 <= t['created'].replace(tzinfo=None) < d1]
    elif period == 'thisweek':
        return [t for t in tickets if t['year_created'] == ref_year and t['week_created'] == ref_week]
    elif period == 'lastweek':
        last = ref_now - datetime.timedelta(days=7)
        y, w = iso_week(last)
        return [t for t in tickets if t['year_created'] == y and t['week_created'] == w]
    elif period == '30d':
        cutoff = ref_now - datetime.timedelta(days=30)
        return [t for t in tickets if t['created'] and t['created'].replace(tzinfo=None) >= cutoff]
    elif period == '3m':
        cutoff = ref_now - datetime.timedelta(days=90)
        return [t for t in tickets if t['created'] and t['created'].replace(tzinfo=None) >= cutoff]
    return tickets


def filter_period_closed(tickets: list, period: str, ref_now: datetime.datetime) -> list:
    """Filter tickets CLOSED in a given period (logique Yassine)."""
    def closed_iso(t):
        if not t.get('closed'):
            return None
        d = t['closed'].replace(tzinfo=None) if t['closed'].tzinfo else t['closed']
        return d, iso_week(d)

    ref_year, ref_week = iso_week(ref_now)
    out = []
    for t in tickets:
        info = closed_iso(t)
        if not info:
            continue
        d, (cy, cw) = info
        if period == 'today':
            d0 = ref_now.replace(hour=0, minute=0, second=0, microsecond=0)
            if d >= d0: out.append(t)
        elif period == 'yesterday':
            d0 = (ref_now - datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            d1 = ref_now.replace(hour=0, minute=0, second=0, microsecond=0)
            if d0 <= d < d1: out.append(t)
        elif period == 'thisweek':
            if cy == ref_year and cw == ref_week: out.append(t)
        elif period == 'lastweek':
            last = ref_now - datetime.timedelta(days=7)
            ly, lw = iso_week(last)
            if cy == ly and cw == lw: out.append(t)
        elif period == '30d':
            cutoff = ref_now - datetime.timedelta(days=30)
            if d >= cutoff: out.append(t)
        elif period == '3m':
            cutoff = ref_now - datetime.timedelta(days=90)
            if d >= cutoff: out.append(t)
        else:
            out.append(t)
    return out


# Backward compatibility alias
def filter_period(tickets: list, period: str, ref_now: datetime.datetime) -> list:
    return filter_period_created(tickets, period, ref_now)

def _median(sorted_vals: list):
    """Médiane d'une liste DÉJÀ triée. None si vide."""
    n = len(sorted_vals)
    if n == 0:
        return None
    mid = n // 2
    if n % 2:
        return sorted_vals[mid]
    return (sorted_vals[mid - 1] + sorted_vals[mid]) / 2.0


def compute_ic_stats_for_period(tickets_period: list, ic_name: str) -> dict:
    """Compute stats for a single IC over a ticket list."""
    ic_tickets = [t for t in tickets_period if t['ic_name'] == ic_name]
    total = len(ic_tickets)
    if total == 0:
        return {
            'name': ic_name, 'total': 0, 'created': 0, 'closed': 0, 'resolved': 0, 'lost': 0,
            'email_lost_n': 0, 'callback_lost_n': 0, 'lost_pct': 0,
            'email_lost_pct': 0, 'callback_lost_pct': 0,
            'sla_pct': None, 'sla_n': 0, 'ces_pct': None, 'ces_n': 0,
            'touch_avg': None, 'tl': None, 'level': None,
        }
    closed = sum(1 for t in ic_tickets if t['is_resolved'] or t['is_lost'])
    resolved = sum(1 for t in ic_tickets if t['is_resolved'])
    lost = sum(1 for t in ic_tickets if t['is_lost'])
    el = sum(1 for t in ic_tickets if t['ended_email_lost'])
    cl = sum(1 for t in ic_tickets if t['ended_callback_lost'])
    sla_has = sum(1 for t in ic_tickets if t['sla_has'])
    sla_ok = sum(1 for t in ic_tickets if t['sla_ok'])
    ces_has = sum(1 for t in ic_tickets if t['ces_has'])
    ces_prom = sum(1 for t in ic_tickets if t['ces_promoter'])
    touch_vals = [t['touches'] for t in ic_tickets if t['touches'] is not None]
    tl = ic_tickets[0]['ic_tl']
    level = ic_tickets[0]['ic_level']
    return {
        'name': ic_name,
        'total': total, 'created': total,
        'closed': closed, 'resolved': resolved, 'lost': lost,
        'email_lost_n': el, 'callback_lost_n': cl,
        'lost_pct': round(lost / total * 100, 1),
        'email_lost_pct': round(el / total * 100, 1),
        'callback_lost_pct': round(cl / total * 100, 1),
        'sla_pct': round(sla_ok / sla_has * 100, 1) if sla_has > 0 else None,
        'sla_n': sla_has,
        'ces_pct': round(ces_prom / ces_has * 100, 1) if ces_has > 0 else None,
        'ces_n': ces_has,
        'touch_avg': round(sum(touch_vals) / len(touch_vals), 1) if touch_vals else None,
        'tl': tl, 'level': level,
    }

def compute_ic_stats_split(created_tickets: list, closed_tickets: list, ic_name: str) -> dict:
    """
    Compute stats for a single IC, split by 'created' bucket and 'closed' bucket.

    - created_count = nb tickets créés par cet IC dans la période
    - closed_count = nb tickets fermés par cet IC dans la période (par close_date)
    - SLA / CES / touchpoints / lost = mesurés sur les tickets fermés (logique Yassine)
    """
    created_for_ic = [t for t in created_tickets if t['ic_name'] == ic_name]
    closed_for_ic = [t for t in closed_tickets if t['ic_name'] == ic_name]
    created_count = len(created_for_ic)
    closed_count = len(closed_for_ic)
    # #3.1 — demandes de callback reçues par l'IC sur la période (tickets créés)
    callback_req = sum(1 for t in created_for_ic if t.get('is_callback'))

    if closed_count == 0 and created_count == 0:
        return {
            'name': ic_name, 'total': 0, 'created': 0, 'closed': 0, 'resolved': 0, 'lost': 0,
            'email_lost_n': 0, 'callback_lost_n': 0, 'lost_pct': 0,
            'email_lost_pct': 0, 'callback_lost_pct': 0,
            'sla_pct': None, 'sla_n': 0, 'ces_pct': None, 'ces_n': 0,
            'touch_avg': None, 'resp_median_h': None, 'close_median_h': None,
            'callback_req': 0,
            'tl': None, 'level': None,
        }

    # Stats based on CLOSED tickets (Yassine logic)
    resolved = sum(1 for t in closed_for_ic if t['is_resolved'])
    lost = sum(1 for t in closed_for_ic if t['is_lost'])
    el = sum(1 for t in closed_for_ic if t['ended_email_lost'])
    cl = sum(1 for t in closed_for_ic if t['ended_callback_lost'])
    sla_has = sum(1 for t in closed_for_ic if t['sla_has'])
    sla_ok = sum(1 for t in closed_for_ic if t['sla_ok'])
    ces_has = sum(1 for t in closed_for_ic if t['ces_has'])
    ces_prom = sum(1 for t in closed_for_ic if t['ces_promoter'])
    touch_vals = [t['touches'] for t in closed_for_ic if t['touches'] is not None]

    # #3.2 — médiane temps de 1re réponse (heures ouvrées) sur tickets fermés
    resp_vals = sorted([t['resp_time_h'] for t in closed_for_ic
                        if t.get('resp_time_h') is not None and t['resp_time_h'] > 0])
    resp_median_h = round(_median(resp_vals), 2) if resp_vals else None
    # médiane temps de résolution (création → fermeture) en heures
    close_vals = []
    for t in closed_for_ic:
        if t.get('created') and t.get('closed'):
            c = t['created'].replace(tzinfo=None) if t['created'].tzinfo else t['created']
            cl_ = t['closed'].replace(tzinfo=None) if t['closed'].tzinfo else t['closed']
            delta_h = (cl_ - c).total_seconds() / 3600.0
            if delta_h >= 0:
                close_vals.append(delta_h)
    close_vals.sort()
    close_median_h = round(_median(close_vals), 1) if close_vals else None

    return {
        'name': ic_name,
        'total': created_count,
        'created': created_count,
        'closed': closed_count,
        'resolved': resolved,
        'lost': lost,
        'email_lost_n': el,
        'callback_lost_n': cl,
        'lost_pct': round(lost / closed_count * 100, 1) if closed_count > 0 else 0,
        'email_lost_pct': round(el / closed_count * 100, 1) if closed_count > 0 else 0,
        'callback_lost_pct': round(cl / closed_count * 100, 1) if closed_count > 0 else 0,
        'sla_pct': round(sla_ok / sla_has * 100, 1) if sla_has > 0 else None,
        'sla_n': sla_has,
        'ces_pct': round(ces_prom / ces_has * 100, 1) if ces_has > 0 else None,
        'ces_n': ces_has,
        'touch_avg': round(sum(touch_vals) / len(touch_vals), 1) if touch_vals else None,
        'resp_median_h': resp_median_h,
        'close_median_h': close_median_h,
        'callback_req': callback_req,
    }


def compute_ic_data(tickets: list, ref_now: datetime.datetime, calls_by_ic: dict = None) -> dict:
    """
    Compute IC_DATA: {period: [ic_stats, ...]}.

    IMPORTANT (alignement Yassine):
    - 'created' = tickets créés cette période
    - 'closed' / SLA / CES / touchpoints / lost = tickets FERMÉS cette période
      (le CES et le SLA sont mesurés à la fermeture, pas à la création)
    - calls_out = appels OUTBOUND passés par l'IC (si calls_by_ic fourni)
    """
    calls_by_ic = calls_by_ic or {}
    result = {}
    for period in ['today', 'yesterday', 'thisweek', 'lastweek', '30d', '3m']:
        created_tickets = filter_period_created(tickets, period, ref_now)
        closed_tickets = filter_period_closed(tickets, period, ref_now)
        period_calls = calls_by_ic.get(period, {})
        period_stats = []
        for owner_id, (ic_name, tl, level) in IC_MAP.items():
            stats = compute_ic_stats_split(created_tickets, closed_tickets, ic_name)
            stats['name'] = ic_name
            stats['tl'] = tl
            stats['level'] = level
            stats['calls_out'] = period_calls.get(ic_name, 0)
            # ratio calls passés / demandes de callback reçues (callback_lost_n = callbacks non aboutis)
            # callbacks demandés = on s'appuie sur les tickets callback (cf. frontend) ;
            # ici on expose juste le nb de calls, le ratio est calculé côté template.
            period_stats.append(stats)
        result[period] = period_stats
    return result

def compute_ic_history(tickets: list, min_tickets: int = 5) -> dict:
    """
    Compute IC_HISTORY: {ic_name: {weeks: [...], closed: [...], ...}}.

    Logic (alignment Yassine):
    - 'closed' = tickets fermés cette semaine (par close_date)
    - 'total' = tickets créés cette semaine (par create_date)
    - SLA / CES / lost_pct = sur les tickets fermés cette semaine
    """
    # Bucket by close week (for closed/SLA/CES/lost)
    closed_buckets = defaultdict(list)
    # Bucket by create week (for total)
    created_buckets = defaultdict(int)
    for t in tickets:
        if t['ic_name']:
            if t['year_created'] and t['week_created']:
                created_buckets[(t['ic_name'], t['year_created'], t['week_created'])] += 1
            if t['closed'] and (t['is_resolved'] or t['is_lost']):
                cd = t['closed'].replace(tzinfo=None) if t['closed'].tzinfo else t['closed']
                cy, cw = iso_week(cd)
                closed_buckets[(t['ic_name'], cy, cw)].append(t)

    history = {}
    now = datetime.datetime.now()
    ref_year, ref_week = iso_week(now)
    weeks_range = []
    for w in range(36, 53):
        weeks_range.append((2025, w))
    if ref_year == 2026:
        for w in range(1, ref_week + 1):
            weeks_range.append((2026, w))

    for ic_name in set(v[0] for v in IC_MAP.values()):
        h = {'weeks': [], 'closed': [], 'sla': [], 'ces': [], 'lost_pct': [], 'total': []}
        for (y, w) in weeks_range:
            closed_wk = closed_buckets.get((ic_name, y, w), [])
            created_count = created_buckets.get((ic_name, y, w), 0)
            # Filter ramp-up: include only weeks with enough activity
            if len(closed_wk) < min_tickets and created_count < min_tickets:
                continue
            yr_short = str(y)[-2:]
            h['weeks'].append(f"{yr_short}W{w}")
            h['total'].append(created_count)
            h['closed'].append(len(closed_wk))
            sla_has = sum(1 for t in closed_wk if t['sla_has'])
            sla_ok = sum(1 for t in closed_wk if t['sla_ok'])
            h['sla'].append(round(sla_ok / sla_has * 100, 1) if sla_has > 0 else None)
            ces_has = sum(1 for t in closed_wk if t['ces_has'])
            ces_prom = sum(1 for t in closed_wk if t['ces_promoter'])
            h['ces'].append(round(ces_prom / ces_has * 100, 1) if ces_has > 0 else None)
            lost_n = sum(1 for t in closed_wk if t['is_lost'])
            h['lost_pct'].append(round(lost_n / len(closed_wk) * 100, 1) if len(closed_wk) > 0 else 0)
        history[ic_name] = h
    return history

def compute_global_weekly(tickets: list) -> dict:
    """
    Compute global weekly aggregates for the whole team.

    IMPORTANT (alignement Yassine):
    - 'total' = tickets CRÉÉS cette semaine (par create_date) → numérateur volume
    - 'closed' = tickets FERMÉS cette semaine (par close_date) → numérateur volume
    - SLA / CES / Lost % / Email-Lost / CB-Lost / resp_median / n2_pct
      = calculés sur les tickets FERMÉS cette semaine (car SLA et CES mesurés à fermeture)
    """
    now = datetime.datetime.now()
    ref_year, ref_week = iso_week(now)
    weeks_range = [(2025, w) for w in range(36, 53)]
    if ref_year == 2026:
        weeks_range += [(2026, w) for w in range(1, ref_week + 1)]

    # Bucket par create week (pour total)
    buckets_created = defaultdict(int)
    # Bucket par close week (pour closed et qualité)
    buckets_closed = defaultdict(list)
    for t in tickets:
        if t['year_created'] and t['week_created']:
            buckets_created[(t['year_created'], t['week_created'])] += 1
        if t['closed'] and (t['is_resolved'] or t['is_lost']):
            cd = t['closed'].replace(tzinfo=None) if t['closed'].tzinfo else t['closed']
            cy, cw = iso_week(cd)
            buckets_closed[(cy, cw)].append(t)

    result = {'weeks': [], 'total': [], 'closed': [], 'lost_pct': [], 'email_lost_pct': [],
              'callback_lost_pct': [], 'sla': [], 'ces': [], 'resp_median_h': [],
              'n2_pct': []}
    for (y, w) in weeks_range:
        total_created = buckets_created.get((y, w), 0)
        closed_wk = buckets_closed.get((y, w), [])
        if total_created < 10 and len(closed_wk) < 10:
            continue
        yr_short = str(y)[-2:]
        result['weeks'].append(f"{yr_short}W{w}")
        result['total'].append(total_created)
        result['closed'].append(len(closed_wk))
        # All quality metrics on CLOSED tickets
        closed_n = len(closed_wk)
        if closed_n > 0:
            lost = sum(1 for t in closed_wk if t['is_lost'])
            el = sum(1 for t in closed_wk if t['ended_email_lost'])
            cl = sum(1 for t in closed_wk if t['ended_callback_lost'])
            result['lost_pct'].append(round(lost / closed_n * 100, 1))
            result['email_lost_pct'].append(round(el / closed_n * 100, 1))
            result['callback_lost_pct'].append(round(cl / closed_n * 100, 1))
            sla_has = sum(1 for t in closed_wk if t['sla_has'])
            sla_ok = sum(1 for t in closed_wk if t['sla_ok'])
            result['sla'].append(round(sla_ok / sla_has * 100, 1) if sla_has > 0 else None)
            ces_has = sum(1 for t in closed_wk if t['ces_has'])
            ces_prom = sum(1 for t in closed_wk if t['ces_promoter'])
            # CES hybrid : hardcode CSV avant cutoff, dynamique HubSpot après
            if is_week_hardcoded(y, w):
                hc_val = CES_HEBDO_HARDCODED.get((y, w))
                result['ces'].append(float(hc_val) if hc_val is not None else None)
            else:
                result['ces'].append(round(ces_prom / ces_has * 100, 1) if ces_has > 0 else None)
            resp_vals = sorted([t['resp_time_h'] for t in closed_wk if t['resp_time_h'] is not None and t['resp_time_h'] > 0])
            if resp_vals:
                # resp_time_h is ALREADY in hours (cf ligne 387 enrich_ticket)
                med = resp_vals[len(resp_vals) // 2]
                result['resp_median_h'].append(round(med, 2))
            else:
                result['resp_median_h'].append(None)
            n2_count = sum(1 for t in closed_wk if t['niveau'] == 'n2')
            result['n2_pct'].append(round(n2_count / closed_n * 100, 1))
        else:
            for k in ['lost_pct','email_lost_pct','callback_lost_pct','n2_pct']:
                result[k].append(0)
            for k in ['sla','ces','resp_median_h']:
                result[k].append(None)
    return result

def compute_liasses(contacts: list, ref_now: datetime.datetime = None, client=None) -> dict:
    """
    Compute liasses campaign stats with DYNAMIC client base.

    For each week, the % is calculated on the client base AS OF END OF THAT WEEK,
    not the current count. This gives a fair comparison between 2024 (~6500 clients)
    and 2025 (~9700 clients) campaigns.

    INCOMPLETE WEEK HANDLING: For the 2025 campaign (current), the last point of
    the curve is ONLY shown if the corresponding ISO week is COMPLETE (i.e. Sunday
    23:59:59 of that week has passed). If we're mid-week, last point is the
    previous (complete) week. Returns `last_complete_week_2025` so the template
    can decide.

    DENOMINATOR FIX (mai 2026): n_customers utilise désormais search_count() qui
    retourne le 'total' réel de l'API HubSpot (non plafonné à 10k contrairement à
    la pagination des résultats). Si client est None, on retombe sur len(contacts)
    (rétrocompat).
    """
    if ref_now is None:
        ref_now = datetime.datetime.now()

    # Compte réel de clients lifecyclestage=customer via search_count (non plafonné 10k)
    if client is not None:
        try:
            n_customers = client.search_count('contacts', [
                {'filters': [{'propertyName': 'lifecyclestage', 'operator': 'EQ', 'value': 'customer'}]}
            ])
            log.info(f"Liasses: n_customers réel via search_count = {n_customers}")
        except Exception as e:
            log.warning(f"search_count failed ({e}), fallback to len(contacts)={len(contacts)}")
            n_customers = len(contacts)
    else:
        n_customers = len(contacts)

    # v9.3 (26/05/2026) : "clients actifs" = clients en activité avec abo qui tourne
    # On élargit à client_status IN (actif, overdue, pending) :
    #   - actif    : abo Stripe actif, paye à jour
    #   - overdue  : abo actif mais paiement en retard (= toujours client, relancé)
    #   - pending  : nouveau client, abo pas encore débité
    # On exclut "passif" qui regroupe des anciens clients non encore marqués Churned.
    # Validé Pierre 26/05/2026 : ~8 491 clients attendus, IN(actif/overdue/pending) = ~8 422 OK.
    ACTIVE_STATUSES = ['actif', 'overdue', 'pending']
    if client is not None:
        try:
            n_customers_actif = client.search_count('contacts', [
                {'filters': [
                    {'propertyName': 'lifecyclestage', 'operator': 'EQ', 'value': 'customer'},
                    {'propertyName': 'client_status', 'operator': 'IN', 'values': ACTIVE_STATUSES},
                ]}
            ])
            log.info(f"Liasses: n_customers_actif via search_count = {n_customers_actif} (statuts actif+overdue+pending)")
        except Exception as e:
            log.warning(f"search_count n_customers_actif failed ({e})")
            n_customers_actif = sum(1 for c in contacts
                                    if (c.get('properties', {}).get('client_status') or '').lower() in ACTIVE_STATUSES)
    else:
        n_customers_actif = sum(1 for c in contacts
                                if (c.get('properties', {}).get('client_status') or '').lower() in ACTIVE_STATUSES)

    # Helper: client became customer at this date
    def customer_since(c):
        ts = (c.get('properties', {}).get('hs_v2_date_entered_customer') or
              c.get('properties', {}).get('createdate'))
        return parse_ts(ts)

    # Liasses 2025 finalisations (signées en 2026)
    weekly_2025 = defaultdict(int)
    n_2025_done = 0
    for c in contacts:
        ts = c.get('properties', {}).get('timestamp__teledeclaration_liasse_2025_finalisee')
        if ts:
            n_2025_done += 1
            dt = parse_ts(ts)
            if dt and dt.year == 2026:
                w = dt.isocalendar()[1]
                weekly_2025[w] += 1

    # Liasses 2024 finalisations (signées en 2025)
    weekly_2024 = defaultdict(int)
    n_2024_done = 0
    for c in contacts:
        ts = c.get('properties', {}).get('timestamp___teledeclaration_liasse_2024_finalisee')
        if ts:
            n_2024_done += 1
            dt = parse_ts(ts)
            if dt and dt.year == 2025:
                w = dt.isocalendar()[1]
                weekly_2024[w] += 1

    # === Determine current ISO week and last COMPLETE week ===
    # A week is complete only if Sunday 23:59:59 has passed.
    # ref_now.weekday(): Mon=0...Sun=6
    current_iso_year, current_iso_week, current_iso_dow = ref_now.isocalendar()
    # last_complete_week = current_iso_week if dow == 7 (Sunday) AND time >= 23:59:59
    # else current_iso_week - 1
    is_sunday_end = (current_iso_dow == 7 and ref_now.hour == 23 and ref_now.minute >= 59)
    if is_sunday_end:
        last_complete_week_2025 = current_iso_week
    else:
        last_complete_week_2025 = current_iso_week - 1 if current_iso_week > 1 else 0

    # 2025 campaign weekly volume (W1 → last complete week)
    # We never extend beyond the data we have, and never beyond last_complete_week
    max_observed_w_2025 = max(weekly_2025.keys()) if weekly_2025 else 0
    # The "publishable" length = min(observed, last_complete_week)
    publishable_max_w_2025 = min(max_observed_w_2025, last_complete_week_2025) if last_complete_week_2025 > 0 else max_observed_w_2025
    # Mais on garde la structure jusqu'à last_complete_week pour les zéros
    final_max_w_2025 = max(publishable_max_w_2025, last_complete_week_2025)
    if final_max_w_2025 < 1:
        final_max_w_2025 = max_observed_w_2025 if max_observed_w_2025 > 0 else 17

    weekly_values_2025 = [weekly_2025.get(w, 0) for w in range(1, final_max_w_2025 + 1)]
    cumul_values_2025 = []
    cumul = 0
    for v in weekly_values_2025:
        cumul += v
        cumul_values_2025.append(cumul)

    # 2024 campaign weekly volume (W1 → W26 fin de campagne juin 2025)
    max_w_2024 = 26
    weekly_values_2024 = [weekly_2024.get(w, 0) for w in range(1, max_w_2024 + 1)]
    cumul_values_2024 = []
    cumul = 0
    for v in weekly_values_2024:
        cumul += v
        cumul_values_2024.append(cumul)

    # === Dynamic client base per week (key innovation) ===
    # v8.2 (13/05/2026) : FIX historique exited_customer.
    # v8.3 (14/05/2026) : FIX RE-ENTRIES — un contact compte si :
    #   (entered <= d) AND (
    #       exited > d                                  # a quitté APRÈS d
    #     OR exited absent                              # jamais quitté
    #     OR lifecyclestage == 'customer' aujourd'hui   # aller-retour
    #   )
    # Avant v8.3 on n'avait que les 2 premières branches, ce qui sous-comptait les
    # mois récents (~270 contacts qui ont fait exit/re-entry en mai 2026 manquaient).
    def _exited_naive(c):
        ts = c.get('properties', {}).get('hs_v2_date_exited_customer')
        if not ts:
            return None
        ex = parse_ts(ts)
        if not ex:
            return None
        return ex.replace(tzinfo=None) if ex.tzinfo else ex

    def _is_current_customer(c):
        return (c.get('properties', {}).get('lifecyclestage') or '').lower() == 'customer'

    def base_at(year, week):
        """Number of contacts who were customer at end of given ISO week (Sunday)."""
        try:
            d = datetime.datetime.fromisocalendar(year, week, 7)
        except ValueError:
            return 0
        cnt = 0
        for c in contacts:
            since = customer_since(c)
            if not since:
                continue
            since_naive = since.replace(tzinfo=None) if since.tzinfo else since
            if since_naive > d:
                continue  # pas encore customer à cette date
            exited = _exited_naive(c)
            if exited is None or exited > d:
                cnt += 1  # encore customer à d (jamais sorti, ou sorti après d)
            elif _is_current_customer(c):
                cnt += 1  # aller-retour : sorti avant d mais re-entré depuis → customer aujourd'hui
            # sinon : sorti avant d et toujours pas customer → exclus
        return cnt

    # v8 : variante "clients actifs" — même logique mais filtrée client_status=actif.
    # Note : client_status est un snapshot d'aujourd'hui (pas historisé) → la
    # base actif historique est sous-estimée. Tolérable car cette courbe a été
    # désactivée côté template en v8.1 (Option C validée 13/05/2026), on garde
    # le calcul uniquement pour la 3e courbe "% liasses 2025 vs clients actifs"
    # (qui s'aligne sur la situation actuelle, donc snapshot OK).
    def base_actif_at(year, week):
        try:
            d = datetime.datetime.fromisocalendar(year, week, 7)
        except ValueError:
            return 0
        cnt = 0
        for c in contacts:
            since = customer_since(c)
            if not since:
                continue
            since_naive = since.replace(tzinfo=None) if since.tzinfo else since
            if since_naive > d:
                continue
            exited = _exited_naive(c)
            is_current_customer_now = _is_current_customer(c)
            # Mêmes 3 branches qu'en base_at, mais filtre additionnel sur client_status=actif
            if not (exited is None or exited > d or is_current_customer_now):
                continue
            # v9.3 : client actif = client_status IN (actif, overdue, pending)
            if (c.get('properties', {}).get('client_status') or '').lower() in ('actif', 'overdue', 'pending'):
                cnt += 1
        return cnt

    # Base clients per week 2025 (campaign 2024)
    base_2025_year = [base_at(2025, w) for w in range(1, max_w_2024 + 1)]
    # Base clients per week 2026 (campaign 2025)
    base_2026_year = [base_at(2026, w) for w in range(1, final_max_w_2025 + 1)]
    # v8 : base ACTIFS per week 2026 — utilisée pour la 3e courbe du comparatif liasses
    base_2026_actif_year = [base_actif_at(2026, w) for w in range(1, final_max_w_2025 + 1)]

    # Pct dynamic
    pct_2024 = [
        round(cumul_values_2024[i] / base_2025_year[i] * 100, 1) if base_2025_year[i] > 0 else 0
        for i in range(len(cumul_values_2024))
    ]
    pct_2025 = [
        round(cumul_values_2025[i] / base_2026_year[i] * 100, 1) if base_2026_year[i] > 0 else 0
        for i in range(len(cumul_values_2025))
    ]
    # v8 : pct 2025 sur base actifs (3e courbe)
    pct_2025_actif = [
        round(cumul_values_2025[i] / base_2026_actif_year[i] * 100, 1) if base_2026_actif_year[i] > 0 else 0
        for i in range(len(cumul_values_2025))
    ]

    # Mask 2025 points beyond last_complete_week_2025 (template will skip them)
    # We set null for incomplete weeks so the curve stops cleanly.
    if last_complete_week_2025 > 0:
        for i in range(len(pct_2025)):
            week_num = i + 1
            if week_num > last_complete_week_2025:
                pct_2025[i] = None
                pct_2025_actif[i] = None  # v8 : mask aussi la courbe actifs
                cumul_values_2025[i] = None

    return {
        'n_customers': n_customers,
        'n_customers_actif': n_customers_actif,  # v8
        'n_2025_done': n_2025_done,
        'n_2024_done': n_2024_done,
        'pct_2025': round(n_2025_done / n_customers * 100, 1) if n_customers else 0,
        'pct_2025_actif_total': round(n_2025_done / n_customers_actif * 100, 1) if n_customers_actif else 0,  # v8
        'weekly_2025': weekly_values_2025,
        'cumul_2025': cumul_values_2025,
        'weekly_2024': weekly_values_2024,
        'cumul_2024': cumul_values_2024,
        'base_2025_year': base_2025_year,
        'base_2026_year': base_2026_year,
        'base_2026_actif_year': base_2026_actif_year,  # v8
        'pct_dynamic_2024': pct_2024,
        'pct_dynamic_2025': pct_2025,
        'pct_dynamic_2025_actif': pct_2025_actif,  # v8
        'last_complete_week_2025': last_complete_week_2025,
        'current_week_2025': current_iso_week,
    }

def compute_etp_monthly(tickets: list, ref_now: datetime.datetime, threshold_per_day: int = 6) -> dict:
    """
    Compute monthly ETP (full-time equivalent) of active ICs.

    Rules (per IC, per working day):
    - Count tickets closed (resolved + lost) on that day
    - IC is "active" on day D if closed >= THRESHOLD_PER_DAY (default: 6)
    - IC's ETP contribution for the month = active_days / total_working_days
    - Total ETP for the month = sum over all ICs (mapped or not)

    IMPORTANT: Uses owner_id (not ic_name) to include former ICs no longer in IC_MAP
    (e.g. Nathan, Théo, Léo, Nicolas in 2025). The threshold filters out managers
    and occasional contributors automatically.

    Also returns:
    - working_days per month
    - tickets per ETP per working day
    """
    import calendar
    from datetime import date as date_t, timedelta

    THRESHOLD_PER_DAY = threshold_per_day

    # French public holidays 2025-2026
    HOLIDAYS = {
        date_t(2025, 1, 1), date_t(2025, 4, 21), date_t(2025, 5, 1), date_t(2025, 5, 8),
        date_t(2025, 5, 29), date_t(2025, 6, 9), date_t(2025, 7, 14), date_t(2025, 8, 15),
        date_t(2025, 11, 1), date_t(2025, 11, 11), date_t(2025, 12, 25), date_t(2025, 12, 26),
        date_t(2026, 1, 1), date_t(2026, 4, 6), date_t(2026, 5, 1), date_t(2026, 5, 8),
        date_t(2026, 5, 14), date_t(2026, 5, 25), date_t(2026, 7, 14), date_t(2026, 8, 15),
        date_t(2026, 11, 1), date_t(2026, 11, 11), date_t(2026, 12, 25),
    }

    # Index: owner_id -> {date: count of tickets closed that day}
    daily_closed = defaultdict(lambda: defaultdict(int))
    for t in tickets:
        owner = t.get('owner_id')
        if not owner:
            continue
        if not (t['is_resolved'] or t['is_lost']):
            continue
        close_dt = t.get('closed')
        if not close_dt:
            continue
        d = close_dt.date() if hasattr(close_dt, 'date') else close_dt
        daily_closed[owner][d] += 1

    # Month list: Jan 2025 -> current month
    month_list = []
    for y in [2025, 2026]:
        for m in range(1, 13):
            if y > ref_now.year or (y == ref_now.year and m > ref_now.month):
                break
            month_list.append((y, m))

    etp_by_month = []
    closed_total_by_month = []
    closed_per_etp = []
    n_active_ics_by_month = []
    working_days_by_month = []
    tickets_per_etp_per_day = []
    ic_breakdown = defaultdict(list)

    all_owners = sorted(daily_closed.keys())

    for (y, m) in month_list:
        first = date_t(y, m, 1)
        last_day = calendar.monthrange(y, m)[1]
        last = date_t(y, m, last_day)
        if y == ref_now.year and m == ref_now.month:
            today = ref_now.date()
            if last > today:
                last = today
        working_days = []
        d = first
        while d <= last:
            if d.weekday() < 5 and d not in HOLIDAYS:
                working_days.append(d)
            d += timedelta(days=1)
        n_working = len(working_days)
        working_days_by_month.append(n_working)

        owner_contribs = {}
        for owner in all_owners:
            if n_working == 0:
                continue
            active_days = sum(1 for d in working_days if daily_closed[owner].get(d, 0) >= THRESHOLD_PER_DAY)
            if active_days > 0:
                owner_contribs[owner] = active_days / n_working

        for owner in IC_MAP.keys():
            ic_name = IC_MAP[owner][0]
            ic_breakdown[ic_name].append(round(owner_contribs.get(owner, 0), 2))

        first_full = date_t(y, m, 1)
        last_full = date_t(y, m, last_day)
        if y == ref_now.year and m == ref_now.month:
            last_full = ref_now.date()
        full_total = 0
        for owner, days in daily_closed.items():
            for d, count in days.items():
                if first_full <= d <= last_full:
                    full_total += count

        total_etp = sum(owner_contribs.values())
        n_active = sum(1 for v in owner_contribs.values() if v > 0.05)

        etp_by_month.append(round(total_etp, 2))
        closed_total_by_month.append(full_total)
        closed_per_etp.append(round(full_total / total_etp, 0) if total_etp > 0 else 0)
        n_active_ics_by_month.append(n_active)
        if total_etp > 0 and n_working > 0:
            tickets_per_etp_per_day.append(round(full_total / total_etp / n_working, 1))
        else:
            tickets_per_etp_per_day.append(0)

    mn = ['Jan', 'Fév', 'Mar', 'Avr', 'Mai', 'Jui', 'Jul', 'Aoû', 'Sep', 'Oct', 'Nov', 'Déc']
    labels = [f"{mn[m-1]}{str(y)[-2:]}" for (y, m) in month_list]

    return {
        'months': labels,
        'etp': etp_by_month,
        'closed_per_etp': closed_per_etp,
        'closed_total': closed_total_by_month,
        'n_active_ics': n_active_ics_by_month,
        'working_days': working_days_by_month,
        'tickets_per_etp_per_day': tickets_per_etp_per_day,
        'ic_breakdown_monthly': dict(ic_breakdown),
        'threshold_per_day': THRESHOLD_PER_DAY,
        # Index of current (partial) month, or None if today is the last day of the month.
        # Used to mark the corresponding bars/points as gray-dashed in the dashboard.
        'partial_month_index': (len(month_list) - 1) if (
            month_list and month_list[-1][0] == ref_now.year and month_list[-1][1] == ref_now.month
            and ref_now.date() < date_t(month_list[-1][0], month_list[-1][1],
                                        calendar.monthrange(month_list[-1][0], month_list[-1][1])[1])
        ) else None,
    }


def compute_etp_monthly_by_level(tickets: list, ref_now: datetime.datetime, level: str) -> dict:
    """
    ETP monthly filtered by ticket level (N1 or N2).

    Special rules:
    - N1: before December 2025, the niveau_de_ticket field didn't exist
      so we consider ALL tickets without explicit niveau as N1 (legacy fallback).
      Threshold: 6 tickets/day to count as 1 ETP-day.
    - N2: only counts from December 2025 onwards (tag didn't exist before).
      Threshold: 2 tickets/day (N2 deals with more complex tickets, lower volume).
      Months before December 2025 are forced to 0 ETP / 0 closed / 0 active.
    """
    from datetime import date as date_t

    level_lower = level.lower()
    N2_START_DATE = date_t(2025, 12, 1)  # tag created in December 2025

    if level_lower == 'n1':
        # Tickets considered N1: explicit n1 OR no level set (pre-Dec 2025 legacy)
        filtered = [t for t in tickets if (t.get('niveau') or '') in ('n1', '')]
        # Use default threshold 6
        result = compute_etp_monthly(filtered, ref_now)
        return result

    elif level_lower == 'n2':
        # Tickets explicitly tagged n2
        filtered = [t for t in tickets if (t.get('niveau') or '').lower() == 'n2']
        # Use threshold 2 for N2 (more complex tickets, lower volume)
        result = compute_etp_monthly(filtered, ref_now, threshold_per_day=2)
        # Force zero for months before Dec 2025 (tag didn't exist)
        for i, label in enumerate(result['months']):
            # Parse label like "Mar25" or "Déc25"
            mn_to_num = {'Jan':1,'Fév':2,'Mar':3,'Avr':4,'Mai':5,'Jui':6,
                         'Jul':7,'Aoû':8,'Sep':9,'Oct':10,'Nov':11,'Déc':12}
            yr = 2000 + int(label[-2:])
            mn = mn_to_num.get(label[:-2], 1)
            cutoff = (yr < N2_START_DATE.year) or (yr == N2_START_DATE.year and mn < N2_START_DATE.month)
            if cutoff:
                result['etp'][i] = 0
                result['closed_per_etp'][i] = 0
                result['closed_total'][i] = 0
                result['n_active_ics'][i] = 0
                result['tickets_per_etp_per_day'][i] = 0
        return result

    else:
        return compute_etp_monthly([t for t in tickets if (t.get('niveau') or '').lower() == level_lower], ref_now)




def compute_atr_monthly(tickets: list, contacts: list, client: 'HubSpotClient' = None) -> dict:
    """
    Compute monthly ATR + N2 % monthly.

    ATR (Anti-Ticket Ratio) = % Clients autonomes
    Formula: (clients - clients_with_at_least_1_ticket) / clients × 100
    The higher, the better — measures product self-sufficiency.

    CLIENTS BASE FIX (v7, mai 2026) : clients_by_month utilise désormais
    client.search_count() par mois au lieu de filtrer len(contacts). Évite
    le plafond 10k qui faisait stagner l'évolution base clients à 10 000
    depuis avril dans le dashboard.
    """
    # Tickets created per month
    created_by_month = defaultdict(int)
    for t in tickets:
        if t['created']:
            key = (t['created'].year, t['created'].month)
            created_by_month[key] += 1

    # Build month list
    now = datetime.datetime.now()
    month_list = []
    for y in [2025, 2026]:
        for m in range(1, 13):
            if y > now.year or (y == now.year and m > now.month):
                break
            month_list.append((y, m))

    # Clients per month — STRATÉGIE 1 (préférée) : search_count par mois.
    # Compte des contacts customer avec hs_v2_date_entered_customer < fin de mois.
    # Non plafonné à 10k contrairement à len(contacts).
    #
    # v8 (mai 2026) : on calcule EN PARALLÈLE :
    #   - clients_by_month        = base TOTALE (était customer à fin de mois)
    #   - clients_actif_by_month  = base ACTIFS (+ client_status=actif aujourd'hui)
    #
    # v8.2 (13/05/2026) : FIX historique exited_customer.
    # v8.3 (14/05/2026) : FIX RE-ENTRIES (aller-retours customer → autre stage → customer).
    # Cas réel détecté : ~270 contacts ont eu une "exit customer" puis "re-entry customer"
    # dans les jours suivants (workflow HubSpot, renouvellement, batch update).
    # `hs_v2_date_exited_customer` ne stocke que la DERNIÈRE sortie, donc un contact
    # qui a fait aller-retour avait une exit_date < eom alors qu'il est customer
    # aujourd'hui — la logique v8.2 l'excluait à tort.
    #
    # Logique correcte (3 filterGroups en OR) :
    #   Groupe A : entered < eom AND exited >= eom        (a churn APRÈS eom)
    #   Groupe B : entered < eom AND exited IS NULL       (jamais sorti)
    #   Groupe C : entered < eom AND lifecyclestage = customer aujourd'hui
    #     → capture les aller-retours (exit historique < eom mais re-entry depuis)
    clients_by_month = []
    clients_actif_by_month = []
    if client is not None:
        for (y, m) in month_list:
            if m == 12:
                eom = datetime.datetime(y + 1, 1, 1)
            else:
                eom = datetime.datetime(y, m + 1, 1)
            eom_ms = int(eom.timestamp() * 1000)
            # Base TOTALE — clients à cette date (incluant churners postérieurs + aller-retours)
            try:
                count = client.search_count('contacts', [
                    {  # Groupe A : a quitté APRÈS la date de référence
                        'filters': [
                            {'propertyName': 'hs_v2_date_entered_customer', 'operator': 'LT', 'value': str(eom_ms)},
                            {'propertyName': 'hs_v2_date_exited_customer', 'operator': 'GTE', 'value': str(eom_ms)},
                        ]
                    },
                    {  # Groupe B : n'a jamais quitté (exit_customer is null)
                        'filters': [
                            {'propertyName': 'hs_v2_date_entered_customer', 'operator': 'LT', 'value': str(eom_ms)},
                            {'propertyName': 'hs_v2_date_exited_customer', 'operator': 'NOT_HAS_PROPERTY'},
                        ]
                    },
                    {  # Groupe C (v8.3) : aller-retour — exit historique mais customer aujourd'hui
                        'filters': [
                            {'propertyName': 'hs_v2_date_entered_customer', 'operator': 'LT', 'value': str(eom_ms)},
                            {'propertyName': 'lifecyclestage', 'operator': 'EQ', 'value': 'customer'},
                        ]
                    },
                ])
            except Exception as e:
                log.warning(f"search_count clients {y}-{m:02d} échoué ({e}), fallback len(contacts)")
                count = sum(1 for c in contacts
                            if (ts := parse_ts(c.get('properties', {}).get('createdate'))) and ts.replace(tzinfo=None) < eom)
            clients_by_month.append(count)
            # Base ACTIFS (v9.3) — client_status IN (actif, overdue, pending)
            # snapshot d'aujourd'hui, pas historisé. Courbe historique désactivée
            # côté template (Option C validée 13/05).
            try:
                count_actif = client.search_count('contacts', [{
                    'filters': [
                        {'propertyName': 'lifecyclestage', 'operator': 'EQ', 'value': 'customer'},
                        {'propertyName': 'client_status', 'operator': 'IN', 'values': ['actif', 'overdue', 'pending']},
                        {'propertyName': 'hs_v2_date_entered_customer', 'operator': 'LT', 'value': str(eom_ms)},
                    ]
                }])
            except Exception as e:
                log.warning(f"search_count clients_actif {y}-{m:02d} échoué ({e}), fallback None")
                count_actif = None
            clients_actif_by_month.append(count_actif)
    else:
        # Fallback : compte basé sur les contacts paginés (plafonné 10k, legacy)
        # Garde la logique pré-v8.2 (createdate < eom) — moins précise mais limite les régressions.
        for (y, m) in month_list:
            if m == 12:
                eom = datetime.datetime(y + 1, 1, 1)
            else:
                eom = datetime.datetime(y, m + 1, 1)
            count = sum(1 for c in contacts
                        if (ts := parse_ts(c.get('properties', {}).get('createdate'))) and ts.replace(tzinfo=None) < eom)
            clients_by_month.append(count)
            # Fallback actifs : on filtre aussi sur client_status localement
            count_actif = sum(1 for c in contacts
                              if (ts := parse_ts(c.get('properties', {}).get('createdate'))) and ts.replace(tzinfo=None) < eom
                              and (c.get('properties', {}).get('client_status') or '').lower() in ('actif', 'overdue', 'pending'))
            clients_actif_by_month.append(count_actif)

    created_list = [created_by_month.get((y, m), 0) for (y, m) in month_list]

    # New ATR: % Clients autonomes = 100 - (tickets / clients × 100)
    autonomous_pct = [
        round(max(0, 100 - (created_list[i] / clients_by_month[i] * 100)), 1) if clients_by_month[i] > 0 else 0
        for i in range(len(month_list))
    ]
    # Keep old ATR (% solicitants) for backward compat if needed
    atr_list = [
        round(created_list[i] / clients_by_month[i] * 100, 1) if clients_by_month[i] > 0 else 0
        for i in range(len(month_list))
    ]

    labels = []
    mn = ['Jan', 'Fév', 'Mar', 'Avr', 'Mai', 'Jui', 'Jul', 'Aoû', 'Sep', 'Oct', 'Nov', 'Déc']
    for (y, m) in month_list:
        labels.append(f"{mn[m-1]}{str(y)[-2:]}")

    # N2 % monthly (n2 tag created Dec 2025, force null before)
    n2_by_month = defaultdict(int)
    for t in tickets:
        if t['created'] and t['niveau'] == 'n2':
            n2_by_month[(t['created'].year, t['created'].month)] += 1
    n2_pct_monthly = []
    for i, (y, m) in enumerate(month_list):
        # Avant Déc 2025 le tag n'existait pas
        if (y, m) < (2025, 12):
            n2_pct_monthly.append(None)
        elif created_list[i] > 0:
            n2_pct_monthly.append(round(n2_by_month.get((y, m), 0) / created_list[i] * 100, 1))
        else:
            n2_pct_monthly.append(0)

    return {
        'months': labels,
        'clients': clients_by_month,
        'clients_actif': clients_actif_by_month,  # v8
        'created': created_list,
        'atr': atr_list,                       # legacy (% solicitants)
        'autonomous_pct': autonomous_pct,      # new ATR ("% Clients autonomes")
        'n2_pct_monthly': n2_pct_monthly,
    }

def compute_new_vs_existing_monthly(tickets: list, contacts: list, ref_now: datetime.datetime, new_pivot_date: str = '2025-07-01') -> dict:
    """
    Monthly % of tickets opened by NEW vs EXISTING customers.

    LIASSES-COUNT MODEL (v7, mai 2026) — validé avec Pierre :
    Pour chaque ticket à la date T, on lookup le contact qui l'a ouvert et
    on compte combien de liasses il avait déjà finalisées AVANT T :

      - 0 liasse finalisée avant T → NOUVEAU
        (premier cycle Nopillo, soit en cours soit récemment terminé)

      - 1+ liasse finalisée avant T → RÉCURRENT
        (client revenu pour une nouvelle année après avoir déjà produit
        au moins une liasse, peu importe le type 2023/2024/2025/rattrapage)

      - Pas matchable (email vide ou contact pas en base) → INCONNU
        Exclu silencieusement du % final.

    Champs HubSpot pris en compte pour le compte de liasses finalisées :
      - timestamp__teledeclaration_liasse_2025_finalisee
      - timestamp___teledeclaration_liasse_2024_finalisee
      - timestamp___teledeclaration_finalisee (ancienne campagne 2023)

    `new_pivot_date` est conservé pour rétrocompat data.json mais n'a plus
    d'usage métier dans la définition.
    """
    # Index contacts par email avec leurs timestamps de liasses
    contacts_by_email = {}
    for c in contacts:
        p = c.get('properties', {}) or {}
        email = (p.get('email') or '').strip().lower()
        if not email:
            continue
        # Tous les timestamps de liasses finalisées (parsés en datetime)
        liasse_dates = []
        for prop in [
            'timestamp__teledeclaration_liasse_2025_finalisee',
            'timestamp___teledeclaration_liasse_2024_finalisee',
            'timestamp___teledeclaration_finalisee',
        ]:
            ts = p.get(prop)
            if ts:
                dt = parse_ts(ts)
                if dt:
                    liasse_dates.append(dt.replace(tzinfo=None) if dt.tzinfo else dt)
        contacts_by_email[email] = {
            'liasse_dates': liasse_dates,
            'props': p,
        }

    # Bucket tickets
    buckets = defaultdict(lambda: {'new': 0, 'existing': 0, 'unknown': 0})
    for t in tickets:
        created = t.get('created')
        if not created:
            continue
        key = (created.year, created.month)
        # v7 : on essaye TOUS les emails associés au ticket (pas juste le 1er)
        emails = t.get('contact_emails') or ([t['contact_email']] if t.get('contact_email') else [])
        if not emails:
            buckets[key]['unknown'] += 1
            continue
        # Essaie chaque email jusqu'à trouver un contact match
        c = None
        for email in emails:
            email_clean = (email or '').strip().lower()
            if not email_clean:
                continue
            c = contacts_by_email.get(email_clean)
            if c:
                break
        if not c:
            buckets[key]['unknown'] += 1
            continue
        # Compte des liasses finalisées AVANT la date du ticket
        ticket_dt = created.replace(tzinfo=None) if created.tzinfo else created
        n_liasses_before = sum(1 for ld in c['liasse_dates'] if ld < ticket_dt)
        if n_liasses_before == 0:
            buckets[key]['new'] += 1
        else:
            buckets[key]['existing'] += 1

    # Month list jan 2025 -> current
    month_list = []
    for y in [2025, 2026]:
        for m in range(1, 13):
            if y > ref_now.year or (y == ref_now.year and m > ref_now.month):
                break
            month_list.append((y, m))

    new_counts = []
    existing_counts = []
    unknown_counts = []
    new_pct = []
    for (y, m) in month_list:
        b = buckets.get((y, m), {'new': 0, 'existing': 0, 'unknown': 0})
        # % Nouveau = nouveau / (nouveau + récurrent), inconnu EXCLU du dénominateur
        total_known = b['new'] + b['existing']
        new_counts.append(b['new'])
        existing_counts.append(b['existing'])
        unknown_counts.append(b['unknown'])
        new_pct.append(round(b['new'] / total_known * 100, 1) if total_known > 0 else 0)

    mn = ['Jan', 'Fév', 'Mar', 'Avr', 'Mai', 'Jui', 'Jul', 'Aoû', 'Sep', 'Oct', 'Nov', 'Déc']
    labels = [f"{mn[m-1]}{str(y)[-2:]}" for (y, m) in month_list]

    # Snapshot global des contacts par catégorie (à la date d'aujourd'hui)
    today = ref_now.replace(tzinfo=None) if ref_now.tzinfo else ref_now
    n_new_now = sum(1 for c in contacts_by_email.values()
                    if not any(ld < today for ld in c['liasse_dates']))
    n_existing_now = sum(1 for c in contacts_by_email.values()
                         if any(ld < today for ld in c['liasse_dates']))

    new_per_cust = []
    existing_per_cust = []
    for i in range(len(month_list)):
        new_per_cust.append(round(new_counts[i] / n_new_now, 2) if n_new_now > 0 else 0)
        existing_per_cust.append(round(existing_counts[i] / n_existing_now, 2) if n_existing_now > 0 else 0)

    # Stats de couverture pour diagnostic
    total_tickets = sum(new_counts) + sum(existing_counts) + sum(unknown_counts)
    pct_unknown_global = round(sum(unknown_counts) / total_tickets * 100, 1) if total_tickets else 0
    log.info(f"Nouveau vs Récurrent : {sum(new_counts)} nouveaux, {sum(existing_counts)} récurrents, "
             f"{sum(unknown_counts)} inconnus ({pct_unknown_global}%)")

    return {
        'months': labels,
        'new_tickets': new_counts,
        'existing_tickets': existing_counts,
        'unknown_tickets': unknown_counts,
        'new_pct': new_pct,
        'n_new_customers': n_new_now,
        'n_existing_customers': n_existing_now,
        'new_tickets_per_customer': new_per_cust,
        'existing_tickets_per_customer': existing_per_cust,
        'new_pivot_date': new_pivot_date,  # legacy, plus utilisé en métier
        'pct_unknown_global': pct_unknown_global,
        'definition': 'liasses_count',  # marqueur de version pour template
    }





def compute_faq(client=None) -> dict:
    """
    Returns FAQ views data from CSV.

    Search order (PRIORITY = LOCAL FIRST to avoid macOS TCC permission issues
    on Google Drive Cloud Storage paths):
    1. ~/lighthouse_setup/faq-comparaison-performances.csv (manual or auto-copied)
    2. Google Drive sync (best-effort, gracefully handled if TCC denies access)
    3. None → empty (UI shows how-to)

    Note: client param kept for backward compat but unused (no API call).
    """
    import os, glob, shutil

    home = os.path.expanduser('~')
    csv_candidates = []

    # === PRIORITY 1: local file in script dir (NO TCC restrictions)
    local_path = SCRIPT_DIR / 'faq-comparaison-performances.csv'
    if local_path.exists():
        try:
            # Verify read access (catches edge-case permission errors early)
            with open(local_path, 'rb') as _f:
                _f.read(1)
            csv_candidates.append(('local', local_path))
            log.info(f"FAQ: local CSV found and readable ({local_path})")
        except (PermissionError, OSError) as e:
            log.warning(f"FAQ: local CSV exists but unreadable: {e}")

    # === PRIORITY 2: Google Drive sync (best-effort; can fail on TCC)
    drive_patterns = [
        f"{home}/Library/CloudStorage/GoogleDrive-*/Mon Drive/Make - Pierre Automations/faq-comparaison-performances.csv",
        f"{home}/Library/CloudStorage/GoogleDrive-*/My Drive/Make - Pierre Automations/faq-comparaison-performances.csv",
        f"{home}/Google Drive/Mon Drive/Make - Pierre Automations/faq-comparaison-performances.csv",
        f"{home}/Google Drive/My Drive/Make - Pierre Automations/faq-comparaison-performances.csv",
    ]
    drive_found = []
    for pattern in drive_patterns:
        try:
            for p in glob.glob(pattern):
                drive_found.append(p)
        except (PermissionError, OSError):
            pass

    # Try to read drive files (TCC may block this with [Errno 1] Operation not permitted)
    for p in drive_found:
        try:
            with open(p, 'rb') as _f:
                _f.read(1)
            csv_candidates.append(('drive_sync', pathlib.Path(p)))
            log.info(f"FAQ: Drive CSV readable ({p})")

            # Best-effort sync to local for next run, in case TCC reverts
            if not local_path.exists() or pathlib.Path(p).stat().st_mtime > local_path.stat().st_mtime:
                try:
                    shutil.copy2(p, local_path)
                    log.info(f"FAQ: synced Drive → local ({local_path})")
                except (PermissionError, OSError) as e:
                    log.debug(f"FAQ: sync Drive→local skipped: {e}")
        except (PermissionError, OSError) as e:
            log.info(f"FAQ: Drive CSV blocked by macOS TCC, skipping ({p}): {e}")

    if csv_candidates:
        # Pick the most recent
        csv_candidates.sort(key=lambda x: x[1].stat().st_mtime, reverse=True)
        source_label, csv_path = csv_candidates[0]
        log.info(f"FAQ: using CSV from {source_label} ({csv_path})")
        return _parse_faq_csv(csv_path, source_label)

    log.warning("FAQ: no readable CSV found — section will show 'data not available'")
    return {
        'available': False,
        'source': 'none',
        'weeks': [],
        'ancienne': [],
        'nouvelle': [],
        'last_update': None,
    }



def _parse_faq_csv(csv_path, source_label: str) -> dict:
    """Parse a FAQ CSV file and return aggregated weekly data."""
    import csv as csv_mod
    from datetime import datetime as dt_mod

    weekly_anc = defaultdict(int)
    weekly_nou = defaultdict(int)
    seen_events_anc = defaultdict(set)
    seen_events_nou = defaultdict(set)
    last_ts = None

    try:
        with open(csv_path, encoding='utf-8') as f:
            r = csv_mod.reader(f)
            headers = next(r, None)
            for row in r:
                if len(row) < 4:
                    continue
                ts_str = row[0].strip()
                event_id = row[1].strip()
                kind = (row[2] or '').upper()
                if not ts_str or not kind or not event_id:
                    continue
                try:
                    dt_obj = dt_mod.strptime(ts_str, '%Y-%m-%d %H:%M')
                except ValueError:
                    try:
                        dt_obj = dt_mod.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
                    except ValueError:
                        continue
                if last_ts is None or dt_obj > last_ts:
                    last_ts = dt_obj
                iso_year, iso_week, _ = dt_obj.isocalendar()
                key = (iso_year, iso_week)
                if 'ANCIENNE' in kind:
                    if event_id not in seen_events_anc[key]:
                        seen_events_anc[key].add(event_id)
                        weekly_anc[key] += 1
                elif 'NOUVELLE' in kind:
                    if event_id not in seen_events_nou[key]:
                        seen_events_nou[key].add(event_id)
                        weekly_nou[key] += 1
    except Exception as e:
        log.warning(f"FAQ CSV parse error: {e}")
        return {'available': False, 'source': 'none', 'weeks': [], 'ancienne': [], 'nouvelle': [], 'last_update': None}

    now = datetime.datetime.now()
    cur_y, cur_w, _ = now.isocalendar()
    weeks_list = []
    cy, cw = cur_y, cur_w
    for _ in range(30):
        weeks_list.insert(0, (cy, cw))
        cw -= 1
        if cw < 1:
            cw = 52
            cy -= 1

    keys_with_data = sorted(set(weekly_anc.keys()) | set(weekly_nou.keys()))
    if keys_with_data:
        first_data = keys_with_data[0]
        weeks_list = [k for k in weeks_list if k >= first_data]

    weeks_labels = [f"{str(y)[-2:]}-W{w:02d}" for (y, w) in weeks_list]
    anc_data = [weekly_anc.get(k, 0) for k in weeks_list]
    nou_data = [weekly_nou.get(k, 0) for k in weeks_list]

    return {
        'available': True,
        'source': 'csv_' + source_label,
        'weeks': weeks_labels,
        'ancienne': anc_data,
        'nouvelle': nou_data,
        'last_update': last_ts.strftime('%Y-%m-%d %H:%M') if last_ts else None,
    }


def _fetch_trustpilot_widget(buid: str, widget_id: str, locale: str = 'fr-FR') -> Optional[dict]:
    """
    Récupère note + nombre d'avis Trustpilot LIVE via l'endpoint trustbox-data
    (celui que le widget TrustBox appelle côté navigateur).

    Découverte 18/06/2026 : c'est le seul endpoint Trustpilot non bloqué depuis
    une IP serveur. Le déblocage tient à UN header : Referer pointant vers
    l'iframe du widget. Pas de cookie ni token nécessaire (endpoint public).
    Les pages HTML (fr.trustpilot.com/review/...) et les autres API renvoient 403.

    Args:
        buid: businessUnitId Trustpilot (config: trustpilot_buid)
        widget_id: identifiant du TrustBox posé sur le site (config: trustpilot_widget_id)
        locale: locale d'affichage

    Returns:
        {'trust_score': float, 'stars': float, 'review_count': int} ou None si échec.
    """
    if not buid or not widget_id:
        return None
    url = (f"https://widget.trustpilot.com/trustbox-data/{widget_id}"
           f"?businessUnitId={buid}&locale={locale}")
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36',
        'Accept': '*/*',
        'Accept-Language': 'fr-FR,fr;q=0.9',
        # Header décisif : sans lui, Trustpilot renvoie 403.
        'Referer': (f"https://widget.trustpilot.com/trustboxes/{widget_id}"
                    f"/index.html?templateId={widget_id}&businessunitId={buid}"),
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8', errors='replace'))
    except Exception as e:
        log.warning(f"Trustpilot widget fetch échoué : {e}")
        return None

    bu = data.get('businessUnit') or data.get('businessEntity') or {}
    nr = bu.get('numberOfReviews')
    count = nr.get('total') if isinstance(nr, dict) else nr
    score = bu.get('trustScore')
    if score is None or count is None:
        log.warning("Trustpilot widget : réponse OK mais score/count absents")
        return None
    log.info(f"Trustpilot widget OK : trustScore={score} · {count} avis")
    return {
        'trust_score': round(float(score), 2),
        'stars': round(float(bu.get('stars')), 2) if bu.get('stars') is not None else None,
        'review_count': int(count),
    }


def _scrape_nopillo_ratings() -> dict:
    """
    Scrape les notes Google + Trustpilot depuis nopillo.com/avis-client.

    C'est notre PROPRE site web qui affiche déjà les 2 notes (mises à jour
    manuellement par l'équipe Marketing). Source unique, fiable, jamais
    bloquée par Cloudflare contrairement à fr.trustpilot.com ou Google.

    Structure HTML ciblée (Webflow, classe stable depuis fin 2024) :

        <div class="grid_grade is-review">
          <a href="https://www.google.com/search?...">
            ...
            <div class="text_card-grade">4,6/5</div>
          </a>
          <a href="https://fr.trustpilot.com/review/nopillo.com">
            ...
            <div class="text_card-grade">4,7/5</div>
          </a>
        </div>

    Returns:
        {
            'google': float | None,
            'trustpilot': float | None,
            'fetched_at': str,
            'success': bool,
            'error': str | None,
        }
    """
    import re
    url = 'https://www.nopillo.com/avis-client'
    result = {
        'google': None,
        'trustpilot': None,
        'fetched_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
        'success': False,
        'error': None,
    }
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.8',
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        result['error'] = f"fetch error: {e}"
        log.warning(f"Scrape nopillo.com échoué : {e}")
        return result

    # v9.3 (26/05/2026) — Le bloc <div class="grid_grade is-review"> a disparu
    # du HTML de nopillo.com/avis-client début 2026. Les notes sont maintenant
    # directement dans des liens style <a href="google.com/search?...">Google 4,6/5</a>
    # et <a href="trustpilot.com/review/nopillo.com">Trustpilot 4,7/5</a>.
    #
    # Nouvelle approche : on cherche directement tous les <a> avec href vers
    # google.com/search ou trustpilot.com/review, puis on extrait "X,Y/5" du texte.
    # Plus robuste car ne dépend pas de la classe CSS parent (qui change).

    # Pattern : <a ... href="...google.com/search..." ...>...Google 4,6/5...</a>
    #           <a ... href="...trustpilot.com/review/nopillo..." ...>...Trustpilot 4,7/5...</a>
    anchor_pattern = re.compile(
        r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        re.DOTALL | re.IGNORECASE
    )
    rating_in_text = re.compile(r'([0-9]+[,\.][0-9])\s*/\s*5')

    for href, body in anchor_pattern.findall(html):
        href_low = href.lower()
        # Filtre : on veut uniquement les liens Google search OU Trustpilot review
        is_google = 'google.com/search' in href_low and 'nopillo' in href_low.lower().replace('%20', ' ').replace('+', ' ')
        is_trustpilot = 'trustpilot.com/review' in href_low and 'nopillo' in href_low
        if not (is_google or is_trustpilot):
            continue
        # Extraire "X,Y/5" du body de l'ancre (avec ou sans HTML inline)
        # Strip HTML tags pour récupérer juste le texte
        body_text = re.sub(r'<[^>]+>', ' ', body)
        rm = rating_in_text.search(body_text)
        if not rm:
            continue
        raw = rm.group(1).replace(',', '.')
        try:
            value = float(raw)
        except ValueError:
            continue
        if is_google and result['google'] is None:
            result['google'] = round(value, 2)
        elif is_trustpilot and result['trustpilot'] is None:
            result['trustpilot'] = round(value, 2)

    if result['google'] is not None or result['trustpilot'] is not None:
        result['success'] = True
        log.info(
            f"Scrape nopillo.com OK : "
            f"Google={result['google']} · Trustpilot={result['trustpilot']}"
        )
    else:
        result['error'] = "Aucune note Google/Trustpilot extraite du bloc"
        log.warning(result['error'])

    return result


def compute_satisfaction(cfg: dict = None) -> dict:
    """
    Récupère les données satisfaction Trustpilot + Google.

    Stratégies en cascade (v6 — mai 2026, priorité au scraping nopillo.com) :

    1. **scrape nopillo.com/avis-client** : notre propre site affiche les 2 notes
       (Google + Trustpilot). Source la plus fiable, jamais bloquée.
    2. **scrape fr.trustpilot.com/review/nopillo.com** : page publique
       (peut échouer en 403 selon l'IP serveur).
    3. **Widget Trustpilot officiel** : si `trustpilot_buid` configuré, le
       template charge le widget côté navigateur.
    4. **Fallback config.json** : valeurs manuelles (`trustpilot_fallback_*`,
       `google_fallback_*`) — visibles même si tout échoue.

    Le scraping nopillo.com récupère les notes mais PAS le nb d'avis.
    Le `review_count` Trustpilot reste piloté par config.json.

    Returns:
        {
            'trustpilot': {
                'available': bool, 'rating': float, 'review_count': int,
                'best_rating': 5.0, 'url': str, 'fetched_at': str,
                'source': 'scrape_nopillo' | 'scrape' | 'fallback' | 'widget_only',
                'buid': str | None, 'sitename': 'nopillo.com',
            },
            'google': { 'available': bool, 'rating': float | None, ... }
        }
    """
    import re
    cfg = cfg or {}

    # === Defaults / fallback known values (peuvent être réajustés via config) ===
    TP_FALLBACK_RATING = float(cfg.get('trustpilot_fallback_rating', 4.6))
    TP_FALLBACK_COUNT = int(cfg.get('trustpilot_fallback_review_count', 358))
    GOOGLE_FALLBACK_RATING = cfg.get('google_fallback_rating')  # optional
    GOOGLE_FALLBACK_COUNT = cfg.get('google_fallback_review_count')

    result = {
        'trustpilot': {
            'available': False,
            'source': 'fallback',
            'rating': TP_FALLBACK_RATING,
            'review_count': TP_FALLBACK_COUNT,
            'best_rating': 5.0,
            'url': 'https://fr.trustpilot.com/review/nopillo.com',
            'fetched_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
            'buid': cfg.get('trustpilot_buid'),
            'sitename': 'nopillo.com',
        },
        'google': {
            'available': bool(GOOGLE_FALLBACK_RATING),
            'rating': float(GOOGLE_FALLBACK_RATING) if GOOGLE_FALLBACK_RATING else None,
            'review_count': int(GOOGLE_FALLBACK_COUNT) if GOOGLE_FALLBACK_COUNT else None,
            'best_rating': 5.0,
            'url': cfg.get('google_review_url',
                'https://www.google.com/search?q=nopillo+avis'),
            'place_id': cfg.get('google_place_id'),
            'place_query': cfg.get('google_place_query',
                'Nopillo, 37 Bd Saint-Martin, Paris'),
        },
    }

    # === Stratégie #0 (18/06/2026) : Trustpilot LIVE via endpoint trustbox-data ===
    # Source PRIORITAIRE pour Trustpilot : note ET nombre d'avis en temps réel,
    # via l'endpoint que le widget appelle (cf. _fetch_trustpilot_widget).
    # Si elle marche, elle écrase le fallback config (plus de review_count figé).
    tp_widget = _fetch_trustpilot_widget(
        cfg.get('trustpilot_buid'),
        cfg.get('trustpilot_widget_id'),
        locale=cfg.get('trustpilot_locale', 'fr-FR'),
    )
    if tp_widget:
        result['trustpilot']['rating'] = tp_widget['trust_score']
        result['trustpilot']['review_count'] = tp_widget['review_count']
        result['trustpilot']['available'] = True
        result['trustpilot']['source'] = 'widget_live'
        result['trustpilot']['fetched_at'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')

    # === Stratégie #1 (mai 2026) : scraping nopillo.com/avis-client POUR GOOGLE ===
    # On utilise nopillo.com/avis-client UNIQUEMENT pour récupérer la note Google.
    # Pour Trustpilot, la Stratégie #0 (widget live) est prioritaire ; les stratégies
    # suivantes (scrape direct, fallback config) ne servent que si #0 échoue.
    nopillo = _scrape_nopillo_ratings()
    if nopillo['success'] and nopillo['google'] is not None:
        result['google']['rating'] = nopillo['google']
        result['google']['available'] = True
        result['google']['source'] = 'scrape_nopillo'
        result['google']['fetched_at'] = nopillo['fetched_at']

    # === Stratégie #2 : scraping serveur Trustpilot direct (peut échouer en 403) ===
    # NB : si la Stratégie #0 (widget live) a réussi, ce bloc ne réécrit PAS
    # le résultat (garde au moment de l'update, plus bas).
    try:
        url = 'https://fr.trustpilot.com/review/nopillo.com'
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.8',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Upgrade-Insecure-Requests': '1',
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode('utf-8', errors='replace')

        ratingValue = None
        reviewCount = None
        buid_found = None

        # ld+json aggregateRating
        for m in re.finditer(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                             html, re.DOTALL):
            try:
                data = json.loads(m.group(1))
            except Exception:
                continue
            stack = [data]
            while stack:
                cur = stack.pop()
                if isinstance(cur, dict):
                    if 'aggregateRating' in cur and isinstance(cur['aggregateRating'], dict):
                        ar = cur['aggregateRating']
                        ratingValue = ar.get('ratingValue') or ratingValue
                        reviewCount = (ar.get('reviewCount') or ar.get('ratingCount')
                                      or reviewCount)
                    stack.extend(cur.values())
                elif isinstance(cur, list):
                    stack.extend(cur)

        # __NEXT_DATA__ — Trustpilot uses Next.js, ID is buried in here
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                     html, re.DOTALL)
        if m:
            try:
                nd = json.loads(m.group(1))
                def walk_for_keys(obj, target):
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            if k in target and isinstance(v, (str, int, float)):
                                yield (k, v)
                            yield from walk_for_keys(v, target)
                    elif isinstance(obj, list):
                        for item in obj:
                            yield from walk_for_keys(item, target)
                for k, v in walk_for_keys(nd, {'businessUnitId', 'trustScore', 'numberOfReviews', 'numberOfTotalReviews'}):
                    if k == 'businessUnitId' and not buid_found:
                        buid_found = str(v)
                    elif k == 'trustScore' and not ratingValue:
                        ratingValue = v
                    elif k in ('numberOfReviews', 'numberOfTotalReviews') and not reviewCount:
                        reviewCount = v
            except Exception as e:
                log.debug(f"__NEXT_DATA__ parse error: {e}")

        if ratingValue is not None and result['trustpilot']['source'] != 'widget_live':
            rating = float(str(ratingValue).replace(',', '.'))
            rc = int(str(reviewCount)) if reviewCount else None
            result['trustpilot'].update({
                'available': True,
                'source': 'scrape',
                'rating': round(rating, 2),
                'review_count': rc,
                'fetched_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
                'buid': buid_found or result['trustpilot']['buid'],
            })
            log.info(f"  Trustpilot scrape OK: {rating}/5 ({rc} avis)" +
                    (f" BUID={buid_found}" if buid_found else ""))
        elif ratingValue is None:
            log.warning("Trustpilot scrape: aggregateRating not found in page")
    except Exception as e:
        log.warning(f"Trustpilot scrape failed ({e}) — using fallback values")

    # === Stratégie 2 : si BUID config → exposer pour widget côté navigateur ===
    # Si le scraping a échoué mais qu'on a un BUID en config, on indique au
    # template d'utiliser le widget officiel (qui marche depuis n'importe quel
    # navigateur car le chargement se fait côté client, pas serveur).
    if (result['trustpilot']['source'] != 'widget_live'
            and not result['trustpilot']['available']
            and result['trustpilot']['buid']):
        result['trustpilot']['source'] = 'widget_only'
        log.info(f"  Trustpilot: scrape KO mais BUID configuré → widget officiel côté navigateur")

    return result


def compute_ytd_kpis(tickets: list, contacts: list, ref_now: datetime.datetime,
                     liasses: dict, satisfaction: dict) -> dict:
    """
    Compute Year-To-Date (YtD) KPIs pour le hero de la Vue d'ensemble.

    Returns:
        {
            'period_label': 'YtD 2026 · janvier → 11 mai',
            'liasses_pct': 32.5,           # % d'atteinte campagne fiscale en cours
            'liasses_done': 3145,          # liasses 2025 finalisées
            'liasses_target_n': 9678,      # base clients
            'trustpilot_rating': 4.5,
            'trustpilot_count': 357,
            'google_rating': null or float,
            'google_count': null or int,
            'ces_score': 81.2,             # CES YtD (moy pondérée 2026)
            'ces_n': 1234,                 # nb de réponses CES YtD
            'tickets_closed_ytd': 12450,
            'tickets_created_ytd': 13100,
            'sla_ytd': 99.5,               # SLA YtD 1er touchpoint
            'autonomy_pct_now': 87.4,      # % clients autonomes ce mois
        }
    """
    year = ref_now.year
    period_start = datetime.datetime(year, 1, 1)
    days_elapsed = (ref_now - period_start).days + 1
    months_fr = ['janvier', 'février', 'mars', 'avril', 'mai', 'juin',
                 'juillet', 'août', 'septembre', 'octobre', 'novembre', 'décembre']
    period_label = f"YtD {year} · janvier → {ref_now.day} {months_fr[ref_now.month - 1]}"

    # Tickets YtD
    tickets_created_ytd = 0
    tickets_closed_ytd = 0
    ces_total_n = 0
    ces_promo_n = 0
    sla_total_n = 0
    sla_ok_n = 0
    for t in tickets:
        created = t.get('created')
        if created:
            created_naive = created.replace(tzinfo=None) if created.tzinfo else created
            if created_naive.year == year:
                tickets_created_ytd += 1
        closed = t.get('closed')
        if closed and (t.get('is_resolved') or t.get('is_lost')):
            closed_naive = closed.replace(tzinfo=None) if closed.tzinfo else closed
            if closed_naive.year == year:
                tickets_closed_ytd += 1
                # CES YtD — clé dans enrich_ticket = 'ces' (pas 'ces_score')
                # CES = note 1-7 du dernier sondage CES. Promoteur = note >= 6.
                # On ne compte que les tickets RESOLVED avec une réponse CES.
                ces = t.get('ces')
                if t.get('is_resolved') and ces is not None:
                    ces_total_n += 1
                    if ces >= 6:
                        ces_promo_n += 1
                # SLA YtD — uniquement les tickets ayant un statut SLA "terminé"
                # (= '3' SLA completed on time, ou '4' SLA completed late).
                # On ignore '0' (active), '1' (overdue), '2' (due soon) qui sont des SLA en cours.
                sla_status = t.get('sla_status')
                if sla_status == '3':
                    sla_total_n += 1
                    sla_ok_n += 1
                elif sla_status == '4':
                    sla_total_n += 1

    ces_score = round(ces_promo_n / ces_total_n * 100, 1) if ces_total_n > 0 else None
    sla_ytd = round(sla_ok_n / sla_total_n * 100, 1) if sla_total_n > 0 else None

    # Liasses
    liasses_done = liasses.get('n_2025_done', 0)
    liasses_base = liasses.get('n_customers', 0)
    liasses_pct = round(liasses_done / liasses_base * 100, 1) if liasses_base else 0

    return {
        'period_label': period_label,
        'days_elapsed': days_elapsed,
        'liasses_pct': liasses_pct,
        'liasses_done': liasses_done,
        'liasses_target_n': liasses_base,
        'trustpilot_rating': satisfaction.get('trustpilot', {}).get('rating'),
        'trustpilot_count': satisfaction.get('trustpilot', {}).get('review_count'),
        'google_rating': satisfaction.get('google', {}).get('rating'),
        'google_count': satisfaction.get('google', {}).get('review_count'),
        'ces_score': ces_score,
        'ces_n': ces_total_n,
        'tickets_closed_ytd': tickets_closed_ytd,
        'tickets_created_ytd': tickets_created_ytd,
        'sla_ytd': sla_ytd,
    }


def compute_monthly_quality(tickets: list, ref_now: datetime.datetime) -> dict:
    """
    Compute monthly quality metrics from CLOSED tickets:
    - CES % promoteurs (rating >= 6/7)
    - SLA % OK (sla_status == '3')
    - Response time median (hours)
    - Reopen % (tickets reopened after close)

    All metrics measured on tickets CLOSED in the month (CES sent at close).
    """
    # Build month list jan 2025 -> current month
    month_list = []
    for y in [2025, 2026]:
        for m in range(1, 13):
            if y > ref_now.year or (y == ref_now.year and m > ref_now.month):
                break
            month_list.append((y, m))

    by_month = defaultdict(lambda: {'ces_total':0, 'ces_prom':0, 'sla_has':0, 'sla_ok':0,
                                      'resp_times':[], 'closed':0, 'reopened':0})

    for t in tickets:
        if not t.get('closed'):
            continue
        key = (t['closed'].year, t['closed'].month)
        b = by_month[key]
        b['closed'] += 1
        if t.get('ces_has'):
            b['ces_total'] += 1
            if t.get('ces_promoter'):
                b['ces_prom'] += 1
        if t.get('sla_has'):
            b['sla_has'] += 1
            if t.get('sla_ok'):
                b['sla_ok'] += 1
        if t.get('resp_time_h') and t['resp_time_h'] > 0:
            b['resp_times'].append(t['resp_time_h'])
        if t.get('reopened_at') and t['closed'] and t['reopened_at'] > t['closed']:
            b['reopened'] += 1

    ces_month = []
    sla_month = []
    resp_month = []
    reopen_month = []
    for (y, m) in month_list:
        b = by_month.get((y, m), {'ces_total':0, 'ces_prom':0, 'sla_has':0, 'sla_ok':0, 'resp_times':[], 'closed':0, 'reopened':0})
        # CES hybride : valeur CSV historique pour les mois antérieurs à la mise en place
        # du nouveau survey HubSpot (20 mars 2026), live HubSpot ensuite (Mar26+).
        if is_month_hardcoded(y, m):
            hc = CES_MENSUEL_HARDCODED.get((y, m))
            ces_month.append(float(hc) if hc is not None else None)
        else:
            ces_month.append(round(b['ces_prom'] / b['ces_total'] * 100, 1) if b['ces_total'] > 0 else None)
        sla_month.append(round(b['sla_ok'] / b['sla_has'] * 100, 1) if b['sla_has'] > 0 else None)
        if b['resp_times']:
            srt = sorted(b['resp_times'])
            mid = len(srt) // 2
            median = srt[mid] if len(srt) % 2 else (srt[mid-1] + srt[mid]) / 2
            resp_month.append(round(median, 2))
        else:
            resp_month.append(None)
        reopen_month.append(round(b['reopened'] / b['closed'] * 100, 1) if b['closed'] > 0 else None)

    return {
        'ces_month': ces_month,
        'sla_month': sla_month,
        'resp_month': resp_month,
        'reopen_month': reopen_month,
    }


def compute_weekly_ces(tickets: list, ref_now: datetime.datetime, n_weeks: int = 68) -> dict:
    """
    Weekly CES % promoters for the last N ISO weeks.
    
    Hybrid logic:
    - Weeks <= cutoff (W13 2026): use CSV historical hardcoded values
    - Weeks > cutoff: compute dynamically from HubSpot tickets (>= 6/7 rating)
    """
    cur_y, cur_w, _ = ref_now.isocalendar()
    weeks_list = []
    cy, cw = cur_y, cur_w
    for _ in range(n_weeks):
        weeks_list.insert(0, (cy, cw))
        cw -= 1
        if cw < 1:
            cw = 52
            cy -= 1

    # Compute dynamic CES from HubSpot tickets (used for weeks after cutoff)
    by_week = defaultdict(lambda: {'total':0, 'prom':0})
    for t in tickets:
        if not t.get('closed') or not t.get('ces_has'):
            continue
        iso = t['closed'].isocalendar()
        key = (iso[0], iso[1])
        by_week[key]['total'] += 1
        if t.get('ces_promoter'):
            by_week[key]['prom'] += 1

    labels = [f"{str(y)[-2:]}-W{w:02d}" for (y, w) in weeks_list]
    values = []
    sources = []  # Track source per week for tooltip
    for (y, w) in weeks_list:
        if is_week_hardcoded(y, w):
            # Use historical hardcoded value from CSV
            val = CES_HEBDO_HARDCODED.get((y, w))
            values.append(float(val) if val is not None else None)
            sources.append('csv_historical')
        else:
            # Dynamic from HubSpot
            b = by_week.get((y, w), {'total':0, 'prom':0})
            values.append(round(b['prom'] / b['total'] * 100, 1) if b['total'] > 0 else None)
            sources.append('hubspot_live')
    
    return {'weeks': labels, 'ces_pct': values, 'sources': sources}


def compute_charge_indicator(tickets: list, ref_now: datetime.datetime, client=None) -> dict:
    """
    v9.1 (19/05/2026, demande Pierre) — Indicateur de charge team Care en temps réel.

    Calcule 3 métriques de charge "live" avec comparaison vs moyenne 90 derniers jours :

    1. TICKETS TRAITÉS HIER PAR IC N1 (moyenne)
       - "Hier" = dernier jour où ≥ 3 IC N1 ont fermé ≥ 6 tickets
         (filtre ≥ 3 IC : élimine WE/fériés/ponts/jours creux sans calendrier hardcodé)
       - "IC N1 actif" = celui qui a fermé ≥ 6 tickets ce jour-là (règle ETP du dash)
       - Métrique : moyenne (tickets fermés N1 ce jour-là / IC actif N1 ce jour-là)

    2. BACKLOG ACTUEL (v9.1 — filtre composé)
       - Pipeline = 253837526 (Customer Care)
       - Stage NOT IN (Résolution=422008004, LOST=5222199527)
       - ET : (stage != "Nouveau"=422008001  OU  créé dans les 90 derniers jours)
         → on exclut les 3 793 tickets "Nouveau" > 90j (fossiles historiques)
         → on garde la vraie charge active : tickets en cours + récents non traités
       - Comptage via 2 search_count séparés (additionnés)

    3. TICKETS CRÉÉS AUJOURD'HUI / IC N1 ACTIF
       - Numérateur : tickets créés aujourd'hui sur le pipeline Customer Care
       - Dénominateur : nombre d'IC N1 actifs HIER (= capacity attendue, option D2)
         → donne un chiffre stable dès 8h du matin

    Comparaison : moyenne, min, max sur les 90 derniers jours business
    (jours où ≥ 3 IC actifs = élimine les jours creux qui faussaient le max).

    Returns:
        dict avec pour chaque métrique : value, mean_90d, min_90d, max_90d, delta_pct, status
        status ∈ {green, yellow, orange, red}
        + overall_status = pire des 3 statuts (= statut global)
        + ref_date_yesterday : la vraie date "hier business" utilisée
        + n_active_ics_yesterday : nb IC actifs hier (pour transparence)
        + backlog_old_nouveau : nb tickets "Nouveau" > 90j (= dette historique séparée)
    """
    today_date = ref_now.date()

    # Identifier les tickets Customer Care N1 uniquement
    # (on exclut les N2 pour rester aligné avec la team Care "front-line")
    n1_tickets = [t for t in tickets if t.get('ic_level') == 'N1']

    # === STEP 1 : Trouver "hier business" (avec filtre ≥ 3 IC actifs en v9.1) ===
    # On scanne en arrière jusqu'à trouver un jour où ≥ 3 IC N1 ont fermé ≥ 6 tickets.
    # Limite : 14 jours en arrière (sécurité, ne devrait jamais aller au-delà).
    ETP_THRESHOLD = 6
    MIN_ACTIVE_ICS = 3  # v9.1 : élève le seuil pour éviter jours quasi-vides

    def closed_per_ic_for_date(d):
        """Returns dict {ic_name: nb_tickets_closed_that_day} for N1 tickets."""
        by_ic = defaultdict(int)
        for t in n1_tickets:
            if t.get('closed') and t['closed'].date() == d and (t.get('is_resolved') or t.get('is_lost')):
                ic = t.get('ic_name')
                if ic:
                    by_ic[ic] += 1
        return by_ic

    yesterday_business = None
    yesterday_active_ics = []
    yesterday_total_closed = 0
    for delta in range(1, 15):
        candidate = today_date - datetime.timedelta(days=delta)
        by_ic = closed_per_ic_for_date(candidate)
        active = [ic for ic, n in by_ic.items() if n >= ETP_THRESHOLD]
        if len(active) >= MIN_ACTIVE_ICS:
            yesterday_business = candidate
            yesterday_active_ics = active
            yesterday_total_closed = sum(by_ic[ic] for ic in active)
            break

    if yesterday_business is None:
        log.warning("compute_charge_indicator: pas de jour business trouvé sur 14j → fallback NULL")
        return {
            'available': False,
            'reason': 'Aucun jour business trouvé sur les 14 derniers jours',
        }

    # Métrique 1 : moyenne tickets traités par IC actif hier
    if yesterday_active_ics:
        tickets_per_ic_yesterday = round(yesterday_total_closed / len(yesterday_active_ics), 1)
    else:
        tickets_per_ic_yesterday = 0

    # === STEP 2 : Backlog actuel (v9.1 — Option 2 + filtre 90j) ===
    # On compte :
    #   - Tickets "Nouveau" (422008001) créés dans les 90 derniers jours
    #   - + Tous les autres tickets ouverts (hors Résolution, LOST, Nouveau historique)
    #
    # Et on isole "backlog_old_nouveau" = tickets "Nouveau" > 90j (dette historique).
    backlog = None
    backlog_old_nouveau = None  # = "Nouveau" > 90j à auditer (data quality)
    backlog_components = {}     # détail breakdown pour transparence
    if client is not None:
        try:
            today_minus_90d = (ref_now - datetime.timedelta(days=90)).strftime('%Y-%m-%d')

            # (a) Tickets "Nouveau" récents (≤ 90j) = charge entrante non encore traitée
            n_nouveau_recent = client.search_count('tickets', [{
                'filters': [
                    {'propertyName': 'hs_pipeline', 'operator': 'EQ', 'value': CC_PIPELINE_ID},
                    {'propertyName': 'hs_pipeline_stage', 'operator': 'EQ', 'value': '422008001'},
                    {'propertyName': 'createdate', 'operator': 'GTE', 'value': today_minus_90d},
                ]
            }])

            # (b) Tous les autres tickets ouverts (hors Résolution, LOST, et Nouveau)
            n_other_open = client.search_count('tickets', [{
                'filters': [
                    {'propertyName': 'hs_pipeline', 'operator': 'EQ', 'value': CC_PIPELINE_ID},
                    {'propertyName': 'hs_pipeline_stage', 'operator': 'NOT_IN',
                     'values': ['422008004', '5222199527', '422008001']},
                ]
            }])

            # (c) "Nouveau" > 90j : c'est la dette historique = qu'on isole hors backlog
            n_nouveau_old = client.search_count('tickets', [{
                'filters': [
                    {'propertyName': 'hs_pipeline', 'operator': 'EQ', 'value': CC_PIPELINE_ID},
                    {'propertyName': 'hs_pipeline_stage', 'operator': 'EQ', 'value': '422008001'},
                    {'propertyName': 'createdate', 'operator': 'LT', 'value': today_minus_90d},
                ]
            }])

            backlog = n_nouveau_recent + n_other_open
            backlog_old_nouveau = n_nouveau_old
            backlog_components = {
                'nouveau_recent_90d': n_nouveau_recent,
                'other_open_stages': n_other_open,
                'nouveau_old_90d_plus': n_nouveau_old,
            }
            log.info(f"Charge: backlog actif = {backlog} (Nouveau≤90j={n_nouveau_recent} + autres ouverts={n_other_open}) | Dette Nouveau>90j = {n_nouveau_old}")
        except Exception as e:
            log.warning(f"Charge: search_count backlog échoué ({e}), fallback comptage local")
            # Fallback : comptage sur les tickets en mémoire
            cutoff = ref_now - datetime.timedelta(days=90)
            backlog = 0
            backlog_old_nouveau = 0
            for t in tickets:
                if t.get('is_resolved') or t.get('is_lost'):
                    continue
                # Approche fallback : on n'a pas le stage exact mais on a la classification
                # On approxime : si pas closed → ticket ouvert
                created = t.get('created')
                if created and created < cutoff and t.get('subject', '').startswith('Nouveau ticket'):
                    backlog_old_nouveau += 1
                else:
                    backlog += 1
    else:
        backlog = sum(1 for t in tickets
                      if not (t.get('is_resolved') or t.get('is_lost')))

    # === STEP 3 : Tickets créés aujourd'hui / IC actif hier (option D2) ===
    tickets_created_today = sum(1 for t in n1_tickets
                                 if t.get('created') and t['created'].date() == today_date)
    n_active_ics_yesterday = len(yesterday_active_ics)
    if n_active_ics_yesterday > 0:
        tickets_per_ic_today = round(tickets_created_today / n_active_ics_yesterday, 1)
    else:
        tickets_per_ic_today = 0

    # === STEP 4 : Référence 90 derniers jours business (v9.1 : seuil ≥ 3 IC) ===
    # On collecte pour chaque jour business (≥ 3 IC actifs N1) les 3 métriques
    # afin de calculer moyenne / min / max sur 90 jours.
    # On itère sur 150 jours calendaires pour avoir ~90 jours business effectifs.
    series_closed_per_ic = []  # M1
    series_backlog_estimate = []  # M2 (approximation locale, mais m2_mean reste lisible)
    series_created_per_ic = []  # M3

    business_dates_collected = []
    for delta in range(1, 151):
        d = today_date - datetime.timedelta(days=delta)
        by_ic_closed = closed_per_ic_for_date(d)
        active = [ic for ic, n in by_ic_closed.items() if n >= ETP_THRESHOLD]
        if len(active) < MIN_ACTIVE_ICS:
            continue
        business_dates_collected.append(d)
        total_closed_d = sum(by_ic_closed[ic] for ic in active)
        series_closed_per_ic.append(total_closed_d / len(active))
        created_d = sum(1 for t in n1_tickets if t.get('created') and t['created'].date() == d)
        series_created_per_ic.append(created_d / len(active))
        # Backlog estimate ce jour-là : approximation locale
        # On exclut les "Nouveau" > 90j à la date d pour cohérence avec la définition live
        d_minus_90 = d - datetime.timedelta(days=90)
        backlog_d = 0
        for t in tickets:
            cr = t.get('created')
            if not cr or cr.date() > d:
                continue
            cl = t.get('closed')
            if cl is not None and cl.date() <= d:
                continue
            # Heuristique : si "Nouveau" et créé > 90j avant d, c'est de la dette
            if cr.date() < d_minus_90 and (t.get('subject') or '').startswith('Nouveau ticket'):
                continue
            backlog_d += 1
        series_backlog_estimate.append(backlog_d)
        if len(series_closed_per_ic) >= 90:
            break

    def stats(serie):
        if not serie:
            return {'mean': 0, 'min': 0, 'max': 0}
        return {
            'mean': round(sum(serie) / len(serie), 1),
            'min': round(min(serie), 1),
            'max': round(max(serie), 1),
        }

    s_closed = stats(series_closed_per_ic)
    s_backlog = stats(series_backlog_estimate)
    s_created = stats(series_created_per_ic)

    # === STEP 5 : Classification status par métrique ===
    def classify(value, mean):
        if mean <= 0:
            return 'gray'
        ratio = value / mean
        if ratio < 0.80:
            return 'green'
        elif ratio <= 1.20:
            return 'yellow'
        elif ratio <= 1.50:
            return 'orange'
        else:
            return 'red'

    m1_status = classify(tickets_per_ic_yesterday, s_closed['mean'])
    m2_status = classify(backlog, s_backlog['mean'])
    m3_status = classify(tickets_per_ic_today, s_created['mean'])

    status_rank = {'green': 0, 'yellow': 1, 'orange': 2, 'red': 3, 'gray': -1}
    # v9.2 : M2 n'a plus de comparaison ni de couleur (chiffre brut / IC), donc
    # on ne le compte pas dans le statut global. Le global reflète uniquement
    # M1 (productivité hier) et M3 (charge entrante aujourd'hui).
    statuses = [m1_status, m3_status]
    valid_statuses = [s for s in statuses if status_rank.get(s, -1) >= 0]
    if valid_statuses:
        overall_status = max(valid_statuses, key=lambda s: status_rank[s])
    else:
        overall_status = 'gray'

    def delta_pct(value, mean):
        if mean <= 0:
            return None
        return round((value - mean) / mean * 100, 0)

    return {
        'available': True,
        'ref_date_today': today_date.strftime('%Y-%m-%d'),
        'ref_date_yesterday': yesterday_business.strftime('%Y-%m-%d'),
        'ref_date_yesterday_label': yesterday_business.strftime('%a %d %b').replace('.', ''),
        'days_back_for_yesterday': (today_date - yesterday_business).days,
        # M1 : tickets traités hier par IC
        'm1_tickets_per_ic_yesterday': tickets_per_ic_yesterday,
        'm1_n_active_ics': n_active_ics_yesterday,
        'm1_total_closed': yesterday_total_closed,
        'm1_mean_90d': s_closed['mean'],
        'm1_min_90d': s_closed['min'],
        'm1_max_90d': s_closed['max'],
        'm1_delta_pct': delta_pct(tickets_per_ic_yesterday, s_closed['mean']),
        'm1_status': m1_status,
        # M2 : backlog actuel (Option 2 + 90j)
        'm2_backlog': backlog,
        'm2_backlog_old_nouveau': backlog_old_nouveau,  # dette historique isolée
        'm2_components': backlog_components,
        'm2_mean_90d': s_backlog['mean'],
        'm2_min_90d': s_backlog['min'],
        'm2_max_90d': s_backlog['max'],
        'm2_delta_pct': delta_pct(backlog, s_backlog['mean']),
        'm2_status': m2_status,
        # M3 : tickets créés aujourd'hui / IC actif hier
        'm3_tickets_created_today': tickets_created_today,
        'm3_tickets_per_ic_today': tickets_per_ic_today,
        'm3_mean_90d': s_created['mean'],
        'm3_min_90d': s_created['min'],
        'm3_max_90d': s_created['max'],
        'm3_delta_pct': delta_pct(tickets_per_ic_today, s_created['mean']),
        'm3_status': m3_status,
        # Synthèse
        'overall_status': overall_status,
        'business_days_sampled': len(series_closed_per_ic),
        'min_active_ics_threshold': MIN_ACTIVE_ICS,
    }


def compute_weekly_productivity(tickets: list, ref_now: datetime.datetime) -> dict:
    """
    v9.1 (19/05/2026, demande Pierre) — Tickets traités cette semaine, moy. par IC actif/jour.

    Calcule :
    - Cette semaine : sum(tickets fermés N1 lundi → jour ouvré écoulé d'aujourd'hui)
                      / (sum jour par jour des IC actifs ces jours)
      = moyenne pondérée des tickets traités par IC actif/jour cette semaine
    - Semaine dernière : même calcul sur lundi → vendredi de la semaine précédente
      (toujours 5 jours ouvrés complets)
    - Statut couleur : compare cette semaine vs semaine dernière (avec mêmes seuils que indicateur de charge)

    "Jour ouvré écoulé" = lundi à hier (inclus), sauf si on est lundi (alors = vide → on affiche
    la semaine dernière comme référence principale).
    Weekends (sat, sun) sont auto-exclus car on filtre sur "≥ 3 IC actifs avec ≥ 6 tickets fermés".

    Returns:
        dict {
            available, this_week_value, this_week_total_closed, this_week_active_ic_days,
            this_week_days_elapsed, this_week_label,
            last_week_value_per_day, last_week_total_closed, last_week_active_ic_days,
            last_week_label,
            delta_pct, status,
        }
    """
    today_date = ref_now.date()
    n1_tickets = [t for t in tickets if t.get('ic_level') == 'N1']
    ETP_THRESHOLD = 6
    MIN_ACTIVE_ICS = 3

    def closed_per_ic_for_date(d):
        by_ic = defaultdict(int)
        for t in n1_tickets:
            if t.get('closed') and t['closed'].date() == d and (t.get('is_resolved') or t.get('is_lost')):
                ic = t.get('ic_name')
                if ic:
                    by_ic[ic] += 1
        return by_ic

    def compute_week_avg(start_date, end_date):
        """
        Pour une fenêtre [start_date, end_date] (incluse) :
        - somme tickets fermés N1 sur tous les jours business (≥ MIN_ACTIVE_ICS IC actifs)
        - somme du nb d'IC actifs sur ces mêmes jours
        - retourne (avg = total / sum_active_ics, total_closed, sum_active_ic_days, n_business_days)
        """
        total_closed = 0
        sum_active = 0
        n_biz = 0
        d = start_date
        while d <= end_date:
            by_ic = closed_per_ic_for_date(d)
            active = [ic for ic, n in by_ic.items() if n >= ETP_THRESHOLD]
            if len(active) >= MIN_ACTIVE_ICS:
                day_closed = sum(by_ic[ic] for ic in active)
                total_closed += day_closed
                sum_active += len(active)
                n_biz += 1
            d += datetime.timedelta(days=1)
        if sum_active == 0:
            return None, 0, 0, 0
        return round(total_closed / sum_active, 1), total_closed, sum_active, n_biz

    # Cette semaine = lundi de la semaine courante → hier (= jours ouvrés écoulés)
    # weekday() : lundi=0, mardi=1, ..., dimanche=6
    weekday_today = today_date.weekday()
    monday_this_week = today_date - datetime.timedelta(days=weekday_today)
    # On exclut "aujourd'hui" (en cours) : on prend lundi → hier
    end_this_week = today_date - datetime.timedelta(days=1)

    # Cas particulier : si on est lundi, end_this_week = dimanche → fenêtre vide
    this_week_avg = None
    this_week_total = 0
    this_week_active = 0
    this_week_n_biz = 0
    if end_this_week >= monday_this_week:
        this_week_avg, this_week_total, this_week_active, this_week_n_biz = compute_week_avg(
            monday_this_week, end_this_week)

    # Semaine dernière = lundi (-7) → vendredi (-3)
    monday_last_week = monday_this_week - datetime.timedelta(days=7)
    friday_last_week = monday_last_week + datetime.timedelta(days=4)
    last_week_avg, last_week_total, last_week_active, last_week_n_biz = compute_week_avg(
        monday_last_week, friday_last_week)

    # Statut : on compare cette semaine vs semaine dernière (référence stable)
    if this_week_avg is not None and last_week_avg and last_week_avg > 0:
        ratio = this_week_avg / last_week_avg
        if ratio < 0.80:
            status = 'green'
        elif ratio <= 1.20:
            status = 'yellow'
        elif ratio <= 1.50:
            status = 'orange'
        else:
            status = 'red'
        delta_p = round((this_week_avg - last_week_avg) / last_week_avg * 100, 0)
    else:
        status = 'gray'
        delta_p = None

    # Labels FR pour affichage
    def fmt_d(d):
        return d.strftime('%d/%m')

    this_week_label = f"{fmt_d(monday_this_week)} → {fmt_d(end_this_week)}" if end_this_week >= monday_this_week else "(début lundi)"
    last_week_label = f"{fmt_d(monday_last_week)} → {fmt_d(friday_last_week)}"

    return {
        'available': True,
        # This week
        'this_week_value': this_week_avg,
        'this_week_total_closed': this_week_total,
        'this_week_active_ic_days': this_week_active,  # somme cumulée (IC × jours)
        'this_week_n_business_days': this_week_n_biz,
        'this_week_label': this_week_label,
        # Last week (reference)
        'last_week_value_per_day': last_week_avg,
        'last_week_total_closed': last_week_total,
        'last_week_active_ic_days': last_week_active,
        'last_week_n_business_days': last_week_n_biz,
        'last_week_label': last_week_label,
        # Comparaison
        'delta_pct': delta_p,
        'status': status,
    }

def compute_daily_30days(tickets: list, ref_now: datetime.datetime) -> dict:
    """
    Daily series for the last 30 days:
    - DAYS_30 labels (ex: '15/04')
    - CREATED_DAYS: tickets created per day
    - CLOSED_DAYS: tickets closed per day
    """
    end = ref_now.date()
    start = end - datetime.timedelta(days=29)
    days_list = [start + datetime.timedelta(days=i) for i in range(30)]

    by_day_created = defaultdict(int)
    by_day_closed = defaultdict(int)
    for t in tickets:
        if t.get('created'):
            d = t['created'].date()
            if start <= d <= end:
                by_day_created[d] += 1
        if t.get('closed'):
            d = t['closed'].date()
            if start <= d <= end:
                by_day_closed[d] += 1

    labels = [d.strftime('%d/%m') for d in days_list]
    created = [by_day_created.get(d, 0) for d in days_list]
    closed = [by_day_closed.get(d, 0) for d in days_list]
    return {'days': labels, 'created': created, 'closed': closed}


def compute_callback_split(tickets: list, ref_now: datetime.datetime, months_list: list) -> dict:
    """
    Partition tickets created into "Callback" vs "Email" based on subject prefix.

    A ticket is classified as "callback" if its subject starts with
    "Demande de callback" — this prefix is auto-set by the web form
    "Faire une demande de rappel". All other tickets are classified as
    "email" (whether they came in via email, in-app form, or other channel).

    Returns daily (last 30 days), weekly (current year ISO weeks),
    and monthly breakdowns.

    months_list: pre-computed list of (year, month) labels matching MONTHS
                 — must be passed so we share the same time window as the
                 rest of the dashboard (incl. partial current month).
    """
    # ---- Daily, last 30 days ----
    end = ref_now.date()
    start = end - datetime.timedelta(days=29)
    days_list = [start + datetime.timedelta(days=i) for i in range(30)]
    daily_callback = defaultdict(int)
    daily_email = defaultdict(int)
    for t in tickets:
        if not t.get('created'):
            continue
        d = t['created'].date()
        if not (start <= d <= end):
            continue
        if t.get('is_callback'):
            daily_callback[d] += 1
        else:
            daily_email[d] += 1
    callback_days = [daily_callback.get(d, 0) for d in days_list]
    email_days = [daily_email.get(d, 0) for d in days_list]

    # ---- Weekly, ISO weeks of the current year up to current week ----
    cur_y, cur_w, _ = ref_now.isocalendar()
    weekly_callback = defaultdict(int)
    weekly_email = defaultdict(int)
    for t in tickets:
        if not (t.get('year_created') and t.get('week_created')):
            continue
        if t['year_created'] != cur_y:
            continue
        wk = t['week_created']
        if t.get('is_callback'):
            weekly_callback[wk] += 1
        else:
            weekly_email[wk] += 1
    callback_2026 = [weekly_callback.get(w, 0) for w in range(1, cur_w + 1)]
    email_2026 = [weekly_email.get(w, 0) for w in range(1, cur_w + 1)]

    # ---- Monthly, aligned with months_list ----
    monthly_callback = defaultdict(int)
    monthly_email = defaultdict(int)
    for t in tickets:
        if not t.get('created'):
            continue
        key = (t['created'].year, t['created'].month)
        if t.get('is_callback'):
            monthly_callback[key] += 1
        else:
            monthly_email[key] += 1
    # Reverse-engineer (y, m) from labels like "Mai26" so we stay aligned
    mn = ['Jan','Fév','Mar','Avr','Mai','Jui','Jul','Aoû','Sep','Oct','Nov','Déc']
    months_keys = []
    for label in months_list:
        prefix = label[:3]
        yy = int('20' + label[3:])
        mm = mn.index(prefix) + 1
        months_keys.append((yy, mm))
    callback_month = [monthly_callback.get(k, 0) for k in months_keys]
    email_month = [monthly_email.get(k, 0) for k in months_keys]

    return {
        'CALLBACK_DAYS': callback_days,
        'EMAIL_DAYS': email_days,
        'CALLBACK_2026': callback_2026,
        'EMAIL_2026': email_2026,
        'CALLBACK_MONTH': callback_month,
        'EMAIL_MONTH': email_month,
    }


def _format_date_fr(dt) -> str:
    """Format date in French manually (no locale dependency)."""
    days_fr = ['lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche']
    months_fr = ['janv.', 'févr.', 'mars', 'avr.', 'mai', 'juin', 'juil.', 'août', 'sept.', 'oct.', 'nov.', 'déc.']
    return f"{days_fr[dt.weekday()]} {dt.day} {months_fr[dt.month - 1]} · {dt.hour:02d}h{dt.minute:02d}"


def build_dynamic_months(ref_now: datetime.datetime, only_complete: bool = True) -> list:
    """Build the list of month labels (Jan25, Fév25, ...) up to last full month.
    
    only_complete=True (default): exclude current month if partial (e.g. May 4 → exclude May)
    only_complete=False: include current month even if partial
    """
    month_list = []
    cutoff_y = ref_now.year
    cutoff_m = ref_now.month
    if only_complete:
        # Only include months that are fully completed
        # Last full month = previous month
        if cutoff_m == 1:
            cutoff_y, cutoff_m = cutoff_y - 1, 12
        else:
            cutoff_m = cutoff_m - 1
    for y in [2025, 2026]:
        for m in range(1, 13):
            if y > cutoff_y or (y == cutoff_y and m > cutoff_m):
                break
            month_list.append((y, m))
    mn = ['Jan','Fév','Mar','Avr','Mai','Jui','Jul','Aoû','Sep','Oct','Nov','Déc']
    return [f"{mn[m-1]}{str(y)[-2:]}" for (y, m) in month_list]


def render_template(template_html: str, data: dict, refresh_time: datetime.datetime) -> str:
    """Replace placeholders in the template with computed data."""
    # The template has 2 main injection points:
    # 1. <script id="lighthouseData"> block — we inject the full data dict as JSON
    # 2. <span id="snapshotDate"> — we inject the refresh timestamp
    data_json = json.dumps(data, default=str, ensure_ascii=False)
    html = template_html
    # Data injection
    import re
    html = re.sub(
        r'<script id="lighthouseData" type="application/json">.*?</script>',
        f'<script id="lighthouseData" type="application/json">{data_json}</script>',
        html, count=1, flags=re.DOTALL
    )
    # Refresh timestamp for the header (both span + js fallback)
    # The JS in template reads this data-refresh attribute from the data json
    return html

# ---------- MAIN ----------
def main():
    log.info("=" * 60)
    log.info("📡 Lighthouse refresh starting")
    start = time.time()
    cfg = load_config()
    log.info(f"IC_MAP source : {_load_ic_map._source}")
    client = HubSpotClient(cfg['hubspot_token'])

    # Fetch data
    try:
        pipeline_info = fetch_pipeline_info(client)
        log.info(f"Pipeline stages loaded: {len(pipeline_info)} stages")
        tickets_raw = fetch_cc_tickets(client)
        contacts = fetch_contacts_for_liasses(client)
    except Exception as e:
        log.exception(f"Fetch failed: {e}")
        sys.exit(2)

    # Enrich + detect lost
    log.info("Processing tickets...")
    tickets = [enrich_ticket(t) for t in tickets_raw]
    detect_lost_flags(tickets, pipeline_info)

    # Compute all stats
    now = datetime.datetime.now()
    # #3.1 — appels OUTBOUND par IC (peut ajouter ~10-30s : 1 count/IC/période)
    try:
        calls_by_ic = fetch_calls_by_ic(client, now)
    except Exception as e:
        log.warning(f"Calls fetch KO ({e}) — calls_out = 0 partout")
        calls_by_ic = {}
    ic_data = compute_ic_data(tickets, now, calls_by_ic=calls_by_ic)
    ic_history = compute_ic_history(tickets)
    global_weekly = compute_global_weekly(tickets)
    liasses = compute_liasses(contacts, ref_now=now, client=client)
    atr = compute_atr_monthly(tickets, contacts, client=client)

    # Build final data dict
    etp = compute_etp_monthly(tickets, now)
    etp_n1 = compute_etp_monthly_by_level(tickets, now, 'n1')
    etp_n2 = compute_etp_monthly_by_level(tickets, now, 'n2')
    new_vs_exist = compute_new_vs_existing_monthly(tickets, contacts, now)
    monthly_quality = compute_monthly_quality(tickets, now)
    weekly_ces = compute_weekly_ces(tickets, now)
    daily_30 = compute_daily_30days(tickets, now)
    # v9 (19/05/2026) — Indicateur de charge team Care (3 métriques + comparaison 90j)
    charge = compute_charge_indicator(tickets, now, client=client)
    # v9.1 (19/05/2026) — Tickets traités cette semaine, moy. par IC actif/jour
    weekly_prod = compute_weekly_productivity(tickets, now)
    # Include the current (partial) month so May 2026 etc. shows up the moment
    # the month starts, instead of waiting for the 1st of the next month.
    months_dynamic = build_dynamic_months(now, only_complete=False)
    # Number of months (incl. partial current month)
    n_months = len(months_dynamic)
    # Callback vs email split (uses subject prefix from "Demande de rappel" form)
    callback_split = compute_callback_split(tickets, now, months_dynamic)
    
    # Helper to truncate a list to n_months (avoid showing partial current month in charts)
    def trunc(lst):
        if not isinstance(lst, list):
            return lst
        return lst[:n_months]
    
    # === Partial-month metadata (used to gray-dash the last bar of ETP/autonomy charts) ===
    # If today is the last day of the month, no partial — partial_month_index is None.
    import calendar as _cal
    last_day_of_now_month = _cal.monthrange(now.year, now.month)[1]
    is_partial_now = now.date().day < last_day_of_now_month
    # PARTIAL_MONTH_INDEX = position of current partial month in MONTHS array (or None)
    if is_partial_now and months_dynamic:
        # Last entry of months_dynamic should match now.year/month since only_complete=False
        partial_idx = len(months_dynamic) - 1
    else:
        partial_idx = None
    # Working days elapsed in the current month (for tooltip display)
    from datetime import date as _date_t, timedelta as _td
    HOLIDAYS_FR = {
        _date_t(2026, 1, 1), _date_t(2026, 4, 6), _date_t(2026, 5, 1), _date_t(2026, 5, 8),
        _date_t(2026, 5, 14), _date_t(2026, 5, 25), _date_t(2026, 7, 14), _date_t(2026, 8, 15),
        _date_t(2026, 11, 1), _date_t(2026, 11, 11), _date_t(2026, 12, 25),
    }
    working_days_elapsed = 0
    d_iter = _date_t(now.year, now.month, 1)
    while d_iter <= now.date():
        if d_iter.weekday() < 5 and d_iter not in HOLIDAYS_FR:
            working_days_elapsed += 1
        d_iter += _td(days=1)
    # Total working days for the current month (full month)
    working_days_total_now = 0
    d_iter = _date_t(now.year, now.month, 1)
    last_d = _date_t(now.year, now.month, last_day_of_now_month)
    while d_iter <= last_d:
        if d_iter.weekday() < 5 and d_iter not in HOLIDAYS_FR:
            working_days_total_now += 1
        d_iter += _td(days=1)

    # === Customer Autonomy projection for the current month ===
    # Projection: tickets_projected = tickets_to_date * (working_days_total / working_days_elapsed)
    # Then autonomous_pct_projected = 100 - (tickets_projected / clients * 100)
    autonomous_arr = trunc(atr['autonomous_pct'])
    is_autonomous_projected = [False] * len(autonomous_arr)
    if partial_idx is not None and working_days_elapsed > 0 and working_days_total_now > 0:
        tickets_to_date = atr['created'][partial_idx] if partial_idx < len(atr['created']) else 0
        clients_now = atr['clients'][partial_idx] if partial_idx < len(atr['clients']) else 0
        if clients_now > 0:
            ratio = working_days_total_now / working_days_elapsed
            tickets_projected = tickets_to_date * ratio
            pct_projected = round(100 - (tickets_projected / clients_now * 100), 1)
            autonomous_arr[partial_idx] = pct_projected
            is_autonomous_projected[partial_idx] = True

    # === Liasses Countdown — dynamic calculation ===
    # v8.1 (13/05/2026) : pct_actuel est désormais aligné sur le hero YtD :
    # n_2025_done / n_customers (search_count direct, fiable).
    # Avant v8.1 on utilisait pct_dynamic_2025[-1] = % à la dernière semaine
    # complète, mais ça créait un écart de 10+ pts avec le YtD (la pagination
    # contacts ratait des liasses, et le ring affichait une valeur incohérente).
    # Replaces the hardcoded "40.7% / 10 sem / 4780 / 478/sem" in template.html
    target_pct = 90
    target_week = 26  # W26 fin juin
    n_customers_liasses = liasses.get('n_customers', 0)
    n_2025_done = liasses.get('n_2025_done', 0)
    # v8.1 : pct_actuel = n_done / n_customers (cohérence YtD hero)
    pct_actuel = (n_2025_done / n_customers_liasses * 100) if n_customers_liasses else 0.0
    target_liasses = math.ceil(n_customers_liasses * target_pct / 100) if n_customers_liasses else 0
    liasses_a_faire = max(0, target_liasses - n_2025_done)
    current_iso_week = now.isocalendar()[1]
    weeks_remaining = max(0, target_week - current_iso_week)
    rythme_requis = math.ceil(liasses_a_faire / weeks_remaining) if weeks_remaining > 0 else liasses_a_faire
    countdown = {
        'pct_actuel': round(pct_actuel, 1),
        'target_pct': target_pct,
        'target_liasses': target_liasses,
        'liasses_a_faire': liasses_a_faire,
        'liasses_done': n_2025_done,  # v8.1 : expose pour template
        'target_week': target_week,
        'current_week': current_iso_week,
        'weeks_remaining': weeks_remaining,
        'rythme_requis': rythme_requis,
    }

    faq = compute_faq(client=client)
    satisfaction = compute_satisfaction(cfg)
    ytd_kpis = compute_ytd_kpis(tickets, contacts, now, liasses, satisfaction)
    data = {
        'IC_DATA': ic_data,
        'IC_MAP_CONFIG': [
            {'owner_id': oid, 'name': nm, 'tl': tl, 'level': lvl}
            for oid, (nm, tl, lvl) in IC_MAP.items()
        ],
        'IC_MAP_SOURCE': _load_ic_map._source,
        'PARAM_PWD': cfg.get('param_pwd', 'nopillo-cs-2026'),
        'IC_HISTORY': ic_history,
        'GLOBAL_WEEKLY': global_weekly,
        'LIASSES': liasses,
        'LIASSES_COUNTDOWN': countdown,
        'PARTIAL_MONTH_INDEX': partial_idx,
        'WORKING_DAYS_ELAPSED_MONTH': working_days_elapsed,
        'WORKING_DAYS_TOTAL_NOW': working_days_total_now,
        'IS_AUTONOMOUS_PROJECTED': is_autonomous_projected,
        # Migration date for Lost stage refactor (22/04/2026 = doctrine Notion update)
        'LOST_MIGRATION_WEEK_LABEL': '26W17',
        'ATR_MONTH': trunc(atr['atr']),
        'CLIENTS_MONTH': trunc(atr['clients']),
        'CLIENTS_ACTIF_MONTH': trunc(atr['clients_actif']),  # v8 — base "clients actifs" (client_status=actif)
        'CREATED_MONTH': trunc(atr['created']),
        'N2_PCT_MONTHLY': trunc(atr['n2_pct_monthly']),
        
        'ETP_MONTH': trunc(etp['etp']),
        'CLOSED_PER_ETP_MONTH': trunc(etp['closed_per_etp']),
        'CLOSED_TOTAL_MONTH': trunc(etp['closed_total']),
        'ETP_N_ACTIVE_ICS': trunc(etp['n_active_ics']),
        'WORKING_DAYS_MONTH': trunc(etp['working_days']),
        'TICKETS_PER_ETP_PER_DAY_MONTH': trunc(etp['tickets_per_etp_per_day']),
        'ETP_BREAKDOWN_MONTHLY': etp['ic_breakdown_monthly'],
        'ETP_THRESHOLD': etp['threshold_per_day'],
        # N1 split
        'ETP_MONTH_N1': trunc(etp_n1['etp']),
        'CLOSED_PER_ETP_MONTH_N1': trunc(etp_n1['closed_per_etp']),
        'CLOSED_TOTAL_MONTH_N1': trunc(etp_n1['closed_total']),
        'ETP_N_ACTIVE_ICS_N1': trunc(etp_n1['n_active_ics']),
        'TICKETS_PER_ETP_PER_DAY_MONTH_N1': trunc(etp_n1['tickets_per_etp_per_day']),
        # N2 split
        'ETP_MONTH_N2': trunc(etp_n2['etp']),
        'CLOSED_PER_ETP_MONTH_N2': trunc(etp_n2['closed_per_etp']),
        'CLOSED_TOTAL_MONTH_N2': trunc(etp_n2['closed_total']),
        'ETP_N_ACTIVE_ICS_N2': trunc(etp_n2['n_active_ics']),
        'TICKETS_PER_ETP_PER_DAY_MONTH_N2': trunc(etp_n2['tickets_per_etp_per_day']),
        'ETP_THRESHOLD_N1': 6,
        'ETP_THRESHOLD_N2': 2,
        'N2_START_LABEL': 'Déc25',
        # ATR refondu — autonomous_pct du mois courant projeté linéairement
        # (tickets_à_date × jours_ouvrés_total / jours_ouvrés_écoulés). IS_AUTONOMOUS_PROJECTED
        # marque l'index pour appliquer le style "barre grise pointillée + estimatif" côté template.
        'AUTONOMOUS_PCT': autonomous_arr,
        # Nouveaux vs récurrents (modèle ancienneté = createdate du contact)
        'NEW_TICKETS_MONTH': trunc(new_vs_exist['new_tickets']),
        'EXISTING_TICKETS_MONTH': trunc(new_vs_exist['existing_tickets']),
        'UNKNOWN_TICKETS_MONTH': trunc(new_vs_exist['unknown_tickets']),
        'NEW_TICKETS_PCT_MONTH': trunc(new_vs_exist['new_pct']),
        'NEW_CUSTOMERS_COUNT': new_vs_exist['n_new_customers'],
        'EXISTING_CUSTOMERS_COUNT': new_vs_exist['n_existing_customers'],
        'NEW_TICKETS_PER_CUSTOMER_MONTH': trunc(new_vs_exist['new_tickets_per_customer']),
        'EXISTING_TICKETS_PER_CUSTOMER_MONTH': trunc(new_vs_exist['existing_tickets_per_customer']),
        'NEW_PIVOT_DATE': new_vs_exist['new_pivot_date'],
        # FAQ (CSV export)
        'FAQ': faq,
        'SATISFACTION': satisfaction,
        'YTD_KPIS': ytd_kpis,
        # Dynamic month labels
        'MONTHS': months_dynamic,
        # Monthly quality (replaces hardcoded CES_MONTH, SLA_MONTH, RESP_MONTH, REOPEN_MONTH)
        'CES_MONTH': trunc(monthly_quality['ces_month']),
        'SLA_MONTH': trunc(monthly_quality['sla_month']),
        'RESP_MONTH': trunc(monthly_quality['resp_month']),
        'REOPEN_MONTH': trunc(monthly_quality['reopen_month']),
        # Weekly CES (replaces hardcoded CES_LONG)
        'WEEKS_LONG': weekly_ces['weeks'],
        'CES_LONG': weekly_ces['ces_pct'],
        # v9 (19/05/2026) : indicateur de charge team Care (3 métriques + ref 90j)
        'CHARGE_INDICATOR': charge,
        # v9.1 (19/05/2026) : tickets traités cette semaine, moy. par IC actif/jour
        'WEEKLY_PRODUCTIVITY': weekly_prod,
        # Daily 30 days (replaces hardcoded CREATED_DAYS / CLOSED_DAYS)
        'DAYS_30': daily_30['days'],
        'CREATED_DAYS': daily_30['created'],
        'CLOSED_DAYS': daily_30['closed'],
        # Tickets · Email vs Callback split (replaces fake "Tickets + Calls" chart)
        'CALLBACK_DAYS': callback_split['CALLBACK_DAYS'],
        'EMAIL_DAYS': callback_split['EMAIL_DAYS'],
        'CALLBACK_2026': callback_split['CALLBACK_2026'],
        'EMAIL_2026': callback_split['EMAIL_2026'],
        'CALLBACK_MONTH': callback_split['CALLBACK_MONTH'],
        'EMAIL_MONTH': callback_split['EMAIL_MONTH'],
        'REFRESH_AT': now.strftime('%Y-%m-%d %H:%M'),
        'REFRESH_AT_FR': _format_date_fr(now),
    }

    # Save data for debug
    with open(DATA_PATH, 'w') as f:
        json.dump(data, f, indent=1, default=str, ensure_ascii=False)

    # Render template
    if not TEMPLATE_PATH.exists():
        log.error(f"Template introuvable: {TEMPLATE_PATH}")
        sys.exit(3)
    template_html = TEMPLATE_PATH.read_text()
    output_html = render_template(template_html, data, now)
    OUTPUT_PATH.write_text(output_html)

    elapsed = time.time() - start
    log.info(f"✓ Refresh completed in {elapsed:.1f}s")
    log.info(f"  → {OUTPUT_PATH}")
    log.info(f"  Tickets: {len(tickets)} | Customers: {liasses['n_customers']} | Liasses 2025: {liasses['n_2025_done']}")
    log.info(f"  ETP last month: {etp['etp'][-1]} | Closed/ETP: {etp['closed_per_etp'][-1]}")
    log.info(f"  Customers: {new_vs_exist['n_new_customers']} new (signed 2025+ no liasse 24) / {new_vs_exist['n_existing_customers']} existing")
    if faq['available']:
        latest_w = faq['weeks'][-1] if faq['weeks'] else '?'
        latest_total = (faq['ancienne'][-1] if faq['ancienne'] else 0) + (faq['nouvelle'][-1] if faq['nouvelle'] else 0)
        log.info(f"  FAQ: source={faq.get('source','?')} · {len(faq['weeks'])} weeks · last week {latest_w} = {latest_total} views (last update: {faq['last_update']})")
    else:
        log.info("  FAQ: API not accessible AND CSV not found — section will show how-to")
    if new_vs_exist['new_tickets'] and new_vs_exist['new_pct']:
        log.info(f"  Last month tickets: {new_vs_exist['new_tickets'][-1]} new / {new_vs_exist['existing_tickets'][-1]} existing → {new_vs_exist['new_pct'][-1]}% new")

if __name__ == '__main__':
    main()
