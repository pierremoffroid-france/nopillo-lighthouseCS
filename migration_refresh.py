#!/usr/bin/env python3
"""
🚚 Nopillo · Migration V2 → V3 : refresh script
================================================
Aspire HubSpot, calcule l'avancement de la migration vers V3 et la charge
tickets par version, puis régénère migration.html.

Conventions reprises de Lighthouse (refresh.py) :
  - zéro dépendance externe (stdlib uniquement)
  - lit le token dans config.json (clé "hubspot_token")
  - fuseau Europe/Paris forcé pour que l'horodatage soit juste en CI
  - template.html avec un placeholder remplacé par du JSON

Usage :
    python3 migration_refresh.py
"""

import datetime
import json
import logging
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

# ---------- FUSEAU HORAIRE ----------
os.environ['TZ'] = 'Europe/Paris'
try:
    time.tzset()
except AttributeError:
    pass

# ---------- PARAMÈTRES MÉTIER ----------
TARGET = 5000                                  # objectif clients migrés
DEADLINE = datetime.date(2026, 9, 30)          # échéance
KICKOFF = datetime.date(2026, 7, 22)           # première vague (mercredi 22/07)
SERIES_START = datetime.date(2026, 7, 13)      # W29 : début de tout affichage. Rien avant.
CC_PIPELINE_ID = '253837526'                   # pipeline Customer Care
HS_PORTAL_ID = '26173790'                      # pour construire les liens ticket
ACTIVE_STATUSES = ('actif', 'overdue', 'pending')

# ---------- PATHS ----------
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / 'config.json'
TEMPLATE_PATH = SCRIPT_DIR / 'migration_template.html'
OUTPUT_PATH = SCRIPT_DIR / 'migration.html'
DATA_PATH = SCRIPT_DIR / 'migration_data.json'
HISTORY_PATH = SCRIPT_DIR / 'migration_history.json'
LOG_PATH = SCRIPT_DIR / 'migration_refresh.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

HS_API_BASE = 'https://api.hubapi.com'


# ======================================================================
# CLIENT HUBSPOT
# ======================================================================
class HubSpot:
    def __init__(self, token):
        self.headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        }

    def _req(self, method, path, body=None, max_retries=4):
        url = HS_API_BASE + path
        data = json.dumps(body).encode('utf-8') if body is not None else None
        for attempt in range(max_retries):
            req = urllib.request.Request(url, data=data, method=method, headers=self.headers)
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    return json.loads(resp.read().decode('utf-8'))
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = int(e.headers.get('Retry-After', '2'))
                    log.warning(f"429 rate limit, retry dans {wait}s ({attempt + 1}/{max_retries})")
                    time.sleep(wait)
                    continue
                if 500 <= e.code < 600 and attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                detail = e.read().decode('utf-8', errors='replace')[:400]
                log.error(f"HubSpot {e.code} sur {method} {path} : {detail}")
                raise
            except urllib.error.URLError as e:
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                log.error(f"Réseau KO sur {method} {path} : {e}")
                raise
        raise RuntimeError(f"échec définitif sur {path}")

    def search(self, object_type, filter_groups, properties, limit=100):
        """Search paginée. Retourne tous les objets correspondants."""
        out, after = [], None
        while True:
            body = {'filterGroups': filter_groups, 'properties': properties, 'limit': limit}
            if after:
                body['after'] = after
            resp = self._req('POST', f'/crm/v3/objects/{object_type}/search', body)
            out.extend(resp.get('results', []))
            after = (resp.get('paging') or {}).get('next', {}).get('after')
            if not after:
                break
            if len(out) >= 9900:  # plafond dur HubSpot : 10k par requête
                log.warning(f"  plafond 10k approché sur {object_type}, chunk trop large")
                break
        return out

    def count(self, object_type, filter_groups):
        """Compte exact via le champ 'total' (non plafonné à 10k)."""
        body = {'filterGroups': filter_groups, 'properties': ['hs_object_id'], 'limit': 1}
        return self._req('POST', f'/crm/v3/objects/{object_type}/search', body).get('total', 0)

    def batch_history(self, ids, prop, chunk=50):
        """Batch read avec historique de propriété. 50 ids max par appel."""
        out = {}
        for i in range(0, len(ids), chunk):
            body = {
                'propertiesWithHistory': [prop],
                'properties': [prop, 'createdate'],
                'inputs': [{'id': str(x)} for x in ids[i:i + chunk]],
            }
            resp = self._req('POST', '/crm/v3/objects/contacts/batch/read', body)
            for r in resp.get('results', []):
                out[str(r.get('id'))] = r
        return out

    def batch_assoc(self, from_type, to_type, ids, chunk=1000):
        """Associations en batch (API v4). Retourne {from_id: [to_id, ...]}."""
        out = {}
        for i in range(0, len(ids), chunk):
            body = {'inputs': [{'id': str(x)} for x in ids[i:i + chunk]]}
            resp = self._req('POST', f'/crm/v4/associations/{from_type}/{to_type}/batch/read', body)
            for r in resp.get('results', []):
                src = str((r.get('from') or {}).get('id'))
                out[src] = [str(t.get('toObjectId')) for t in r.get('to', [])]
        return out


# ======================================================================
# HELPERS
# ======================================================================
def iso_ms(dt):
    return str(int(dt.timestamp() * 1000))


def parse_ts(v):
    """Parse un timestamp HubSpot (ISO string ou epoch ms) → datetime naïf local."""
    if v in (None, ''):
        return None
    if isinstance(v, (int, float)):
        return datetime.datetime.fromtimestamp(v / 1000.0)
    s = str(v)
    if s.isdigit():
        return datetime.datetime.fromtimestamp(int(s) / 1000.0)
    try:
        d = datetime.datetime.fromisoformat(s.replace('Z', '+00:00'))
        return d.replace(tzinfo=None) + (d.utcoffset() or datetime.timedelta(0))
    except ValueError:
        return None


def week_key(d):
    """Clé de semaine ISO triable : (année, numéro)."""
    y, w, _ = d.isocalendar()
    return (y, w)


def week_monday(y, w):
    return datetime.date.fromisocalendar(y, w, 1)


def week_label(y, w):
    return f"W{w:02d}"


def week_range(start_date, end_date):
    """Liste ordonnée de (année, semaine) couvrant [start, end]."""
    out, cur = [], start_date - datetime.timedelta(days=start_date.weekday())
    stop = end_date
    while cur <= stop:
        out.append(week_key(cur))
        cur += datetime.timedelta(days=7)
    return out


def working_days(a, b):
    """Jours ouvrés entre deux dates incluses."""
    n, cur = 0, a
    while cur <= b:
        if cur.weekday() < 5:
            n += 1
        cur += datetime.timedelta(days=1)
    return n


# ======================================================================
# 1 · CONTACTS
# ======================================================================
def fetch_customers(hs):
    """
    Tous les contacts lifecyclestage=customer avec leur produit_version.
    Chunké par createdate (6 mois) pour ne jamais toucher le plafond 10k.
    """
    log.info("1 · Contacts clients + produit_version…")
    props = ['produit_version', 'client_status', 'lifecyclestage',
             'createdate', 'hs_v2_date_entered_customer']
    seen, out = set(), []
    end = datetime.datetime.now() + datetime.timedelta(days=1)
    floor = datetime.datetime(2019, 1, 1)
    while end > floor:
        start = max(end - datetime.timedelta(days=183), floor)
        groups = [{'filters': [
            {'propertyName': 'lifecyclestage', 'operator': 'EQ', 'value': 'customer'},
            {'propertyName': 'createdate', 'operator': 'GTE', 'value': iso_ms(start)},
            {'propertyName': 'createdate', 'operator': 'LT', 'value': iso_ms(end)},
        ]}]
        try:
            chunk = hs.search('contacts', groups, props)
        except Exception as e:
            log.warning(f"  chunk {start.date()} → {end.date()} KO : {e}")
            end = start
            continue
        added = 0
        for c in chunk:
            cid = str(c.get('id'))
            if cid not in seen:
                seen.add(cid)
                out.append(c)
                added += 1
        log.info(f"  {start.date()} → {end.date()} : {len(chunk)} ({added} nouveaux)")
        end = start
    log.info(f"  total clients : {len(out)}")
    return out


def index_customers(contacts):
    idx = {}
    for c in contacts:
        p = c.get('properties') or {}
        version = (p.get('produit_version') or '').strip().upper() or None
        status = (p.get('client_status') or '').strip().lower()
        idx[str(c.get('id'))] = {
            'v': version,
            'active': status in ACTIVE_STATUSES,
            'created': parse_ts(p.get('createdate')),
        }
    return idx


# ======================================================================
# 2 · DATE DE MIGRATION (historique de propriété)
# ======================================================================
def fetch_migration_dates(hs, v3_ids):
    """
    Pour chaque contact actuellement en V3, retrouve la date à laquelle
    produit_version est passé à V3, via l'historique de propriété HubSpot.

    Retourne (dates, born_v3, ok)
      dates   : {contact_id: date}  → passage V2 (ou autre) → V3
      born_v3 : set(contact_id)     → jamais été V2, créé directement en V3
      ok      : False si l'historique n'est pas exploitable → mode dégradé
    """
    log.info(f"2 · Dates de migration via historique produit_version ({len(v3_ids)} contacts V3)…")
    if not v3_ids:
        return {}, set(), True
    try:
        raw = hs.batch_history(list(v3_ids), 'produit_version')
    except Exception as e:
        log.warning(f"  historique indisponible ({e}) → mode dégradé (snapshots)")
        return {}, set(), False

    dates, born, no_hist = {}, set(), 0
    for cid, rec in raw.items():
        hist = ((rec.get('propertiesWithHistory') or {}).get('produit_version') or [])
        if not hist:
            no_hist += 1
            continue
        # HubSpot renvoie l'historique du plus récent au plus ancien
        entries = []
        for h in hist:
            ts = parse_ts(h.get('timestamp'))
            val = (h.get('value') or '').strip().upper()
            if ts:
                entries.append((ts, val))
        entries.sort()
        if not entries:
            no_hist += 1
            continue
        first_v3 = next((ts for ts, val in entries if val == 'V3'), None)
        had_other = any(val and val != 'V3' for _, val in entries)
        if first_v3 is None:
            continue
        if had_other:
            dates[cid] = first_v3.date()
        else:
            # produit_version a toujours valu V3 → client né V3, pas une migration
            born.add(cid)
            dates[cid] = first_v3.date()

    ok = len(dates) + len(born) > 0
    log.info(f"  {len(dates)} dates trouvées · {len(born)} nés V3 · {no_hist} sans historique · ok={ok}")
    return dates, born, ok


# ======================================================================
# 3 · JOURNAL DE SNAPSHOTS (filet de sécurité)
# ======================================================================
def update_history(counts):
    """Un enregistrement par jour. Sert de série de secours et d'audit."""
    hist = []
    if HISTORY_PATH.exists():
        try:
            hist = json.loads(HISTORY_PATH.read_text(encoding='utf-8'))
        except Exception:
            hist = []
    today = datetime.date.today().isoformat()
    hist = [h for h in hist if h.get('date') != today]
    hist.append({
        'date': today,
        'active_total': counts['active_total'],
        'active_v2': counts['active_v2'],
        'active_v3': counts['active_v3'],
        'active_unknown': counts['active_unknown'],
        'all_v3': counts['all_v3'],
    })
    hist.sort(key=lambda h: h['date'])
    HISTORY_PATH.write_text(json.dumps(hist, indent=1, ensure_ascii=False), encoding='utf-8')
    return hist


# ======================================================================
# 4 · TICKETS
# ======================================================================
def fetch_tickets(hs, since):
    """Tickets Customer Care créés depuis `since`. Chunké par mois."""
    log.info(f"4 · Tickets Customer Care depuis {since.date()}…")
    props = ['createdate', 'hs_pipeline']
    out, cur, now = [], since, datetime.datetime.now() + datetime.timedelta(days=1)
    while cur < now:
        nxt = min(cur + datetime.timedelta(days=30), now)
        groups = [{'filters': [
            {'propertyName': 'hs_pipeline', 'operator': 'EQ', 'value': CC_PIPELINE_ID},
            {'propertyName': 'createdate', 'operator': 'GTE', 'value': iso_ms(cur)},
            {'propertyName': 'createdate', 'operator': 'LT', 'value': iso_ms(nxt)},
        ]}]
        chunk = hs.search('tickets', groups, props)
        log.info(f"  {cur.date()} → {nxt.date()} : {len(chunk)} tickets")
        out.extend(chunk)
        cur = nxt
    log.info(f"  total : {len(out)} tickets")
    return out


def classify_tickets(hs, tickets, cust_idx, mig_dates, hist_ok):
    """
    Ventile chaque ticket par semaine et par version du client AU MOMENT
    de la création du ticket (et non par sa version d'aujourd'hui).
    """
    log.info("5 · Association tickets → contacts…")
    tids = [str(t.get('id')) for t in tickets]
    assoc = hs.batch_assoc('tickets', 'contacts', tids)
    log.info(f"  {len(assoc)} tickets associés à au moins un contact")

    per_week = defaultdict(lambda: {'v2': 0, 'v3': 0, 'unknown': 0, 'noclient': 0})
    v3_rows = []   # tickets V3 : id + date uniquement, aucune donnée personnelle
    for t in tickets:
        tid = str(t.get('id'))
        created = parse_ts((t.get('properties') or {}).get('createdate'))
        if not created:
            continue
        wk = week_key(created.date())
        contact = None
        for cid in assoc.get(tid, []):
            if cid in cust_idx:
                contact = cid
                break
        if contact is None:
            per_week[wk]['noclient'] += 1
            continue
        info = cust_idx[contact]
        if info['v'] == 'V3':
            mdate = mig_dates.get(contact)
            if hist_ok and mdate:
                # avant sa bascule, ce client vivait l'expérience V2
                is_v3 = created.date() >= mdate
                per_week[wk]['v3' if is_v3 else 'v2'] += 1
            else:
                is_v3 = True
                per_week[wk]['v3'] += 1
            if is_v3:
                v3_rows.append({
                    'id': tid,
                    'date': created.date().isoformat(),
                    'created': created.strftime('%d/%m/%Y à %Hh%M'),
                    'week': week_label(*wk),
                })
        elif info['v'] == 'V2':
            per_week[wk]['v2'] += 1
        else:
            per_week[wk]['unknown'] += 1
    v3_rows.sort(key=lambda r: (r['date'], r['id']), reverse=True)
    log.info(f"  {len(v3_rows)} ticket(s) V3 post-bascule listés pour contrôle")
    return per_week, v3_rows


# ======================================================================
# 5 · CALCUL DU MODÈLE
# ======================================================================
def build_model(cust_idx, mig_dates, born_v3, hist_ok, tickets_week, hist_snapshots,
                v3_rows=None):
    today = datetime.date.today()

    active_v2 = sum(1 for i in cust_idx.values() if i['active'] and i['v'] == 'V2')
    active_v3 = sum(1 for i in cust_idx.values() if i['active'] and i['v'] == 'V3')
    active_unknown = sum(1 for i in cust_idx.values() if i['active'] and not i['v'])
    active_total = active_v2 + active_v3 + active_unknown
    all_v3 = sum(1 for i in cust_idx.values() if i['v'] == 'V3')

    counts = {
        'active_total': active_total, 'active_v2': active_v2, 'active_v3': active_v3,
        'active_unknown': active_unknown, 'all_v3': all_v3,
    }

    # --- série hebdo de migration ---
    # La série démarre à SERIES_START (W29) : aucune semaine antérieure n'est
    # affichée, ni dans les graphes, ni dans le tableau.
    weeks = week_range(SERIES_START, max(today, DEADLINE))
    kept = set(weeks)

    migrated_per_week = defaultdict(int)
    born_per_week = defaultdict(int)
    first_wk = weeks[0]
    for cid, d in mig_dates.items():
        wk = week_key(d)
        if wk not in kept:          # bascule antérieure à W29 : repliée sur W29
            wk = first_wk
        (born_per_week if cid in born_v3 else migrated_per_week)[wk] += 1

    # base de secours : deltas de snapshots quotidiens
    snap_by_week = {}
    for s in hist_snapshots:
        try:
            d = datetime.date.fromisoformat(s['date'])
        except Exception:
            continue
        snap_by_week[week_key(d)] = s   # dernier snapshot de la semaine

    rows, cum = [], 0
    ticket_rows = []
    for (y, w) in weeks:
        mon = week_monday(y, w)
        sun = mon + datetime.timedelta(days=6)
        is_future = mon > today
        n_mig = migrated_per_week.get((y, w), 0)
        n_born = born_per_week.get((y, w), 0)
        cum += n_mig + n_born

        # clients V3 en fin de semaine (pour les ratios)
        v3_at = cum if hist_ok else (snap_by_week.get((y, w), {}).get('active_v3') or active_v3)
        base_at = (snap_by_week.get((y, w), {}).get('active_total') or active_total)
        v2_at = max(base_at - v3_at - active_unknown, 0)

        tk = tickets_week.get((y, w))
        row = {
            'y': y, 'w': w, 'label': week_label(y, w),
            'start': mon.isoformat(), 'end': sun.isoformat(),
            'future': is_future,
            'partial': (not is_future) and (sun >= today),
            'migrated': n_mig, 'born': n_born,
            'cum': cum if not is_future else None,
            'pct_target': round(100 * cum / TARGET, 1) if not is_future else None,
            'pct_base': round(100 * cum / base_at, 1) if not is_future and base_at else None,
            'clients_v2': v2_at if not is_future else None,
            'clients_v3': v3_at if not is_future else None,
        }
        if tk:
            tv2, tv3 = tk['v2'], tk['v3']
            r2 = round(tv2 / v2_at, 4) if v2_at else None
            r3 = round(tv3 / v3_at, 4) if v3_at else None
            row.update({'tickets_v2': tv2, 'tickets_v3': tv3,
                        'ratio_v2': r2, 'ratio_v3': r3,
                        'delta': (round(r3 - r2, 4) if (r2 is not None and r3 is not None) else None)})
            ticket_rows.append(row['label'])
        rows.append(row)

    # --- trajectoire cible ---
    kickoff_wk = week_key(KICKOFF)
    deadline_wk = week_key(DEADLINE)
    idx_k = weeks.index(kickoff_wk) if kickoff_wk in weeks else 0
    idx_d = weeks.index(deadline_wk) if deadline_wk in weeks else len(weeks) - 1
    span = max(idx_d - idx_k, 1)
    for i, r in enumerate(rows):
        if i < idx_k:
            r['target_cum'] = 0
        else:
            r['target_cum'] = round(TARGET * min((i - idx_k) / span, 1.0))

    days_left = (DEADLINE - today).days
    weeks_left = max(days_left / 7, 0.1)
    remaining = max(TARGET - all_v3, 0)

    return {
        'target': TARGET,
        'deadline': DEADLINE.isoformat(),
        'kickoff': KICKOFF.isoformat(),
        'series_start': SERIES_START.isoformat(),
        'today': today.isoformat(),
        'days_left': days_left,
        'weeks_left': round(weeks_left, 1),
        'working_days_left': working_days(today, DEADLINE),
        'remaining': remaining,
        'rate_needed': round(remaining / weeks_left),
        'counts': counts,
        'pct_target': round(100 * all_v3 / TARGET, 1),
        'pct_base': round(100 * active_v3 / active_total, 1) if active_total else 0,
        'history_ok': hist_ok,
        'n_born_v3': len(born_v3),
        'portal_id': HS_PORTAL_ID,
        'v3_tickets': v3_rows or [],
        'weeks': rows,
        'ticket_weeks': ticket_rows,
        'refreshed_at': datetime.datetime.now().strftime('%d/%m/%Y à %Hh%M'),
        'refreshed_iso': datetime.datetime.now().isoformat(timespec='seconds'),
    }


# ======================================================================
# MAIN
# ======================================================================
def main():
    if not CONFIG_PATH.exists():
        log.error(f"config.json introuvable à {CONFIG_PATH}")
        sys.exit(1)
    cfg = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    token = cfg.get('hubspot_token')
    if not token:
        log.error("config.json sans 'hubspot_token'")
        sys.exit(1)

    hs = HubSpot(token)

    contacts = fetch_customers(hs)
    cust_idx = index_customers(contacts)

    # garde-fou : on recoupe avec le compte exact non plafonné
    try:
        exact = hs.count('contacts', [{'filters': [
            {'propertyName': 'lifecyclestage', 'operator': 'EQ', 'value': 'customer'}]}])
        log.info(f"  contrôle : {len(cust_idx)} récupérés vs {exact} annoncés par HubSpot")
        if exact and abs(exact - len(cust_idx)) / exact > 0.02:
            log.warning("  ⚠ écart > 2% entre le fetch et le compte HubSpot")
    except Exception as e:
        log.warning(f"  contrôle de volumétrie impossible : {e}")

    v3_ids = [cid for cid, i in cust_idx.items() if i['v'] == 'V3']
    mig_dates, born_v3, hist_ok = fetch_migration_dates(hs, v3_ids)

    counts_preview = {
        'active_total': sum(1 for i in cust_idx.values() if i['active']),
        'active_v2': sum(1 for i in cust_idx.values() if i['active'] and i['v'] == 'V2'),
        'active_v3': sum(1 for i in cust_idx.values() if i['active'] and i['v'] == 'V3'),
        'active_unknown': sum(1 for i in cust_idx.values() if i['active'] and not i['v']),
        'all_v3': len(v3_ids),
    }
    snapshots = update_history(counts_preview)

    since = datetime.datetime.combine(
        SERIES_START - datetime.timedelta(days=SERIES_START.weekday()), datetime.time.min)
    tickets = fetch_tickets(hs, since)
    tickets_week, v3_rows = classify_tickets(hs, tickets, cust_idx, mig_dates, hist_ok)

    model = build_model(cust_idx, mig_dates, born_v3, hist_ok, tickets_week, snapshots,
                        v3_rows=v3_rows)

    DATA_PATH.write_text(json.dumps(model, indent=1, ensure_ascii=False), encoding='utf-8')

    if not TEMPLATE_PATH.exists():
        log.error(f"template introuvable : {TEMPLATE_PATH}")
        sys.exit(1)
    html = TEMPLATE_PATH.read_text(encoding='utf-8')
    if '__MIGRATION_DATA__' not in html:
        log.error("le template ne contient pas le placeholder __MIGRATION_DATA__")
        sys.exit(1)
    html = html.replace('__MIGRATION_DATA__', json.dumps(model, ensure_ascii=False))
    OUTPUT_PATH.write_text(html, encoding='utf-8')

    c = model['counts']
    log.info("=" * 66)
    log.info(f"✓ {OUTPUT_PATH.name} régénéré")
    log.info(f"  V3 : {c['all_v3']} ({model['pct_target']}% de l'objectif {TARGET})")
    log.info(f"  Base active : {c['active_total']} · V2 {c['active_v2']} · V3 {c['active_v3']}")
    log.info(f"  Reste {model['remaining']} en {model['weeks_left']} sem → {model['rate_needed']}/sem")
    log.info(f"  Historique produit_version exploitable : {model['history_ok']}")
    log.info(f"  Tickets V3 listés pour contrôle : {len(model['v3_tickets'])}")
    log.info("=" * 66)


if __name__ == '__main__':
    main()
