#!/usr/bin/env python3
"""
care_activity.py ; nouvelles données Customer Care pour Lighthouse
==================================================================
À poser dans ~/lighthouse_setup/ à côté de refresh.py.

Deux usages :

  1. Autonome, pour tester sans toucher au refresh :
         python3 care_activity.py
     → écrit care_activity.json et imprime un résumé lisible.

  2. Importé par refresh.py (branchement à l'étape suivante) :
         from care_activity import compute_care_activity
         DATA.update(compute_care_activity(client_token, ic_map, ic_active, ref_now))

Ce que le module calcule, à partir d'UN SEUL fetch de 35 jours :

  CARE_TOUCH          touchpoints (emails sortants + entrants) par IC et par période
  CARE_TOUCH_DAYS     série 30 jours, département
  CARE_TOUCH_WEEKS    série hebdo, département
  CARE_CALLS          appels sortants par IC et par période, durée, dispositions
  CARE_HOURLY         répartition horaire mails/appels, département et par IC
  CARE_DELAYS         délai de réponse ET délai de relance, séparés, par IC
  CARE_CES            détail des réponses CES : note, groupe, verbatim, lien ticket

Zéro dépendance externe (stdlib only), comme refresh.py.
"""

import json
import os
import sys
import time
import datetime
import urllib.request
import urllib.error
from pathlib import Path
from collections import defaultdict

# ============================================================
# RÉGLAGES
# ============================================================

# Publication des verbatims clients dans data.json (repo public aujourd'hui).
# True  = verbatims publiés tels quels (consigne Pierre du 28/08).
# False = verbatims calculés en local mais vidés à la publication ;
#         à repasser à True le jour de la migration Cloudflare Access.
PUBLISH_VERBATIMS = True

WINDOW_DAYS = 35          # fenêtre glissante emails + appels (30j + bords de semaine)

# Seuil d'aboutissement d'un appel (validé Pierre 28/08). L'intégration
# téléphonie crée un enregistrement par NUMÉROTATION et les marque toutes
# "Connecté" : sous ce seuil c'est une tentative, pas une conversation.
CALL_OK_MIN_S = 30

# Historique incrémental. Le premier run remonte à HISTORY_FROM, les suivants
# n'ajoutent que les jours manquants. Sans ça, impossible d'afficher les
# touchpoints "mois depuis janvier 2025" dans un refresh qui tourne 5x/jour.
HISTORY_FILE = 'care_history.json'
HISTORY_FROM = '2025-01-01'
HISTORY_BACKFILL_MAX_DAYS = 45   # plafond par run hors --backfill
PAGE_LIMIT = 200          # pagination search (100 -> 200 : moitié moins d'appels)

# Plancher de relance. Deux mails sortants séparés de moins de ce délai
# relèvent du même geste (mail + pièce jointe oubliée), pas d'une relance.
# Sans ce filtre, Manon ressortait à 0,1h de "relance" médiane.
RELANCE_FLOOR_H = 5 / 60
CES_DAYS = 90             # fenêtre CES (peu coûteuse, 1 fetch)
HOUR_MIN, HOUR_MAX = 7, 21
BIZ_START, BIZ_END = 9, 18     # heures ouvrées pour les délais nets

PORTAL_ID = '26173790'
TICKET_URL = 'https://app.hubspot.com/contacts/' + PORTAL_ID + '/record/0-5/{}'

CARE_LEVELS = ('N1', 'N2', 'Immat', 'N1+Liasses')
PERIODS = ('today', 'yesterday', 'thisweek', 'lastweek', '30d')

# Fallback des libellés de disposition si /calling/v1/dispositions échoue.
# Les GUID non résolus sont affichés tronqués plutôt que devinés.
DISPO_FALLBACK = {}

HS = 'https://api.hubapi.com'

os.environ['TZ'] = 'Europe/Paris'
try:
    time.tzset()
except AttributeError:
    pass

SCRIPT_DIR = Path(__file__).resolve().parent


# ============================================================
# CLIENT HTTP
# ============================================================
class Api:
    def __init__(self, token):
        self.h = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        self.calls = 0

    def raw(self, method, path, body=None, tolerate=False):
        self.calls += 1
        data = json.dumps(body).encode('utf-8') if body else None
        req = urllib.request.Request(HS + path, data=data, method=method, headers=self.h)
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return r.status, json.loads(r.read().decode('utf-8'))
            except urllib.error.HTTPError as e:
                txt = e.read().decode('utf-8', errors='replace')
                if e.code == 429:
                    time.sleep(int(e.headers.get('Retry-After', '2')))
                    continue
                if 500 <= e.code < 600 and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                if tolerate:
                    return e.code, {'error': txt[:300]}
                raise RuntimeError(f"HTTP {e.code} {method} {path} · {txt[:300]}")
            except urllib.error.URLError as e:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"Réseau KO : {e}")
        raise RuntimeError("Échec après 4 tentatives")

    def search(self, obj, filters, properties, cap=10000):
        out, after = [], None
        while True:
            body = {'filterGroups': [{'filters': filters}],
                    'properties': properties, 'limit': PAGE_LIMIT}
            if after:
                body['after'] = after
            _, resp = self.raw('POST', f'/crm/v3/objects/{obj}/search', body)
            out.extend(resp.get('results', []))
            after = resp.get('paging', {}).get('next', {}).get('after')
            if not after or len(out) >= cap:
                break
        return out

    def assoc(self, src, dst, ids, chunk=500):
        """Retourne (mapping, rapport). Le rapport remonte les lots en echec :
        les avaler en silence faussait la couverture (bug v1 du 28/08)."""
        out = {}
        report = {'n_in': len(ids), 'chunks': 0, 'chunks_failed': 0, 'errors': []}
        for i in range(0, len(ids), chunk):
            batch = ids[i:i + chunk]
            report['chunks'] += 1
            st, resp = self.raw('POST', f'/crm/v4/associations/{src}/{dst}/batch/read',
                                {'inputs': [{'id': x} for x in batch]},
                                tolerate=True)
            if st not in (200, 207):
                report['chunks_failed'] += 1
                report['errors'].append(f"lot {i}-{i + len(batch)} : HTTP {st} "
                                        f"{str(resp.get('error'))[:120]}")
                continue
            for r in resp.get('results', []):
                tos = [str(t.get('toObjectId')) for t in r.get('to', []) if t.get('toObjectId')]
                if tos:
                    out[str(r['from']['id'])] = tos[0]
        report['n_out'] = len(out)
        return out, report


# ============================================================
# HELPERS
# ============================================================
def ms(dt):
    return str(int(dt.timestamp() * 1000))


def parse_ts(s):
    if not s:
        return None
    s = str(s)
    if s.isdigit():
        try:
            return datetime.datetime.fromtimestamp(int(s) / 1000)
        except (ValueError, OSError):
            return None
    try:
        return datetime.datetime.fromisoformat(s.replace('Z', '+00:00')).astimezone().replace(tzinfo=None)
    except ValueError:
        return None


def median(vals):
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def pct_at(vals, p):
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    return v[min(len(v) - 1, int(len(v) * p))]


def biz_hours_between(a, b):
    """Heures ouvrées (lun-ven, BIZ_START-BIZ_END) entre deux datetime.

    Sans ça, un mail envoyé vendredi 17h et la réponse lundi 9h comptent
    68 heures brutes alors que l'IC a mis 1 heure ouvrée. C'est la métrique
    honnête pour piloter une équipe.
    """
    if b <= a:
        return 0.0
    total = 0.0
    day = a.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= b:
        if day.weekday() < 5:
            s = day.replace(hour=BIZ_START)
            e = day.replace(hour=BIZ_END)
            lo = max(s, a)
            hi = min(e, b)
            if hi > lo:
                total += (hi - lo).total_seconds() / 3600
        day += datetime.timedelta(days=1)
    return round(total, 2)


def period_bounds(period, ref):
    d0 = ref.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'today':
        return d0, None
    if period == 'yesterday':
        return d0 - datetime.timedelta(days=1), d0
    if period == 'thisweek':
        return d0 - datetime.timedelta(days=d0.weekday()), None
    if period == 'lastweek':
        mon = d0 - datetime.timedelta(days=d0.weekday())
        return mon - datetime.timedelta(days=7), mon
    if period == '30d':
        return ref - datetime.timedelta(days=30), None
    return None, None


def in_period(ts, period, ref):
    if ts is None:
        return False
    s, e = period_bounds(period, ref)
    if s and ts < s:
        return False
    if e and ts >= e:
        return False
    return True


def business_days(start, end):
    n, d = 0, start.date()
    while d <= end.date():
        if d.weekday() < 5:
            n += 1
        d += datetime.timedelta(days=1)
    return max(1, n)


def iso_week_label(dt):
    y, w, _ = dt.isocalendar()
    return f"{str(y)[2:]}W{w}"


# ============================================================
# FETCH
# ============================================================
def fetch_emails(api, care_ids, start, days):
    """Sortants ET entrants. Les entrants sont indispensables : sans eux, le
    'temps entre touchpoints' mesure le délai de réponse du CLIENT, pas celui
    de l'IC (constat de l'audit du 28/08 : Aaron 0,8h vs Athalia 26,8h)."""
    out = []
    for d in range(days):
        d0 = start + datetime.timedelta(days=d)
        d1 = d0 + datetime.timedelta(days=1)
        out += api.search('emails', [
            {'propertyName': 'hs_email_direction', 'operator': 'IN',
             'values': ['EMAIL', 'INCOMING_EMAIL']},
            {'propertyName': 'hubspot_owner_id', 'operator': 'IN', 'values': care_ids},
            {'propertyName': 'hs_timestamp', 'operator': 'GTE', 'value': ms(d0)},
            {'propertyName': 'hs_timestamp', 'operator': 'LT', 'value': ms(d1)},
        ], ['hs_timestamp', 'hubspot_owner_id', 'hs_email_direction'])
    return out


def fetch_calls(api, care_ids, start, days):
    out = []
    for d in range(days):
        d0 = start + datetime.timedelta(days=d)
        d1 = d0 + datetime.timedelta(days=1)
        out += api.search('calls', [
            {'propertyName': 'hubspot_owner_id', 'operator': 'IN', 'values': care_ids},
            {'propertyName': 'hs_timestamp', 'operator': 'GTE', 'value': ms(d0)},
            {'propertyName': 'hs_timestamp', 'operator': 'LT', 'value': ms(d1)},
        ], ['hs_timestamp', 'hubspot_owner_id', 'hs_call_direction',
            'hs_call_duration', 'hs_call_disposition'])
    return out


def fetch_dispositions(api):
    """Libellés réels du portail. L'endpoint properties renvoie 0 option chez
    Nopillo ; celui du module Calling est le bon."""
    st, resp = api.raw('GET', '/calling/v1/dispositions', tolerate=True)
    if st != 200:
        return dict(DISPO_FALLBACK), f'HTTP {st}'
    out = {}
    items = resp if isinstance(resp, list) else resp.get('results', [])
    for o in items:
        if isinstance(o, dict) and o.get('id'):
            out[str(o['id'])] = o.get('label') or str(o['id'])[:8]
    return (out or dict(DISPO_FALLBACK)), None


def fetch_ces(api, start):
    """Le détail CES ne nécessite AUCUNE association : hs_ticket_id et
    hs_ticket_owner_id sont portés par la soumission elle-même."""
    return api.search('feedback_submissions', [
        {'propertyName': 'hs_submission_timestamp', 'operator': 'GTE', 'value': ms(start)},
        {'propertyName': 'hs_survey_type', 'operator': 'EQ', 'value': 'CES'},
    ], ['hs_value', 'hs_content', 'hs_submission_timestamp', 'hs_response_group',
        'hs_sentiment', 'hs_ticket_id', 'hs_ticket_owner_id', 'hs_ticket_subject',
        'hs_agent_name', 'hs_survey_name'])



# ============================================================
# HISTORIQUE INCRÉMENTAL
# ============================================================
def load_history(path):
    """{'YYYY-MM-DD': {'ic': {nom: {sent,recv,call_ok,call_try}}, 'hours': {...}}}"""
    f = Path(path)
    if not f.exists():
        return {'days': {}, 'meta': {}}
    try:
        d = json.loads(f.read_text(encoding='utf-8'))
        d.setdefault('days', {})
        d.setdefault('meta', {})
        return d
    except (ValueError, OSError):
        return {'days': {}, 'meta': {}}


def save_history(path, hist):
    Path(path).write_text(json.dumps(hist, ensure_ascii=False, separators=(',', ':')),
                          encoding='utf-8')


def missing_days(hist, start, end):
    """Jours ouvrés absents de l'historique entre start et end (exclus du jour même,
    qui est toujours recalculé depuis la fenêtre glissante)."""
    out, d = [], start.date()
    last = end.date()
    while d < last:
        if d.weekday() < 5 and d.isoformat() not in hist['days']:
            out.append(d)
        d += datetime.timedelta(days=1)
    return out


def fetch_day_counts(api, care_ids, day):
    """Compteurs d'un seul jour : emails sortants/entrants et appels par IC,
    plus la ventilation horaire. Un seul appel de recherche par objet."""
    d0 = datetime.datetime.combine(day, datetime.time.min)
    d1 = d0 + datetime.timedelta(days=1)
    ic = defaultdict(lambda: {'sent': 0, 'recv': 0, 'call_ok': 0, 'call_try': 0})
    hours = {'mails': defaultdict(int), 'calls': defaultdict(int)}

    for e in api.search('emails', [
        {'propertyName': 'hs_email_direction', 'operator': 'IN',
         'values': ['EMAIL', 'INCOMING_EMAIL']},
        {'propertyName': 'hubspot_owner_id', 'operator': 'IN', 'values': care_ids},
        {'propertyName': 'hs_timestamp', 'operator': 'GTE', 'value': ms(d0)},
        {'propertyName': 'hs_timestamp', 'operator': 'LT', 'value': ms(d1)},
    ], ['hs_timestamp', 'hubspot_owner_id', 'hs_email_direction']):
        p = e['properties']
        oid = p.get('hubspot_owner_id')
        out = p.get('hs_email_direction') == 'EMAIL'
        ic[oid]['sent' if out else 'recv'] += 1
        ts = parse_ts(p.get('hs_timestamp'))
        if out and ts:
            hours['mails'][ts.hour] += 1

    for c in api.search('calls', [
        {'propertyName': 'hubspot_owner_id', 'operator': 'IN', 'values': care_ids},
        {'propertyName': 'hs_timestamp', 'operator': 'GTE', 'value': ms(d0)},
        {'propertyName': 'hs_timestamp', 'operator': 'LT', 'value': ms(d1)},
    ], ['hs_timestamp', 'hubspot_owner_id', 'hs_call_direction', 'hs_call_duration']):
        p = c['properties']
        if p.get('hs_call_direction') != 'OUTBOUND':
            continue
        try:
            dur = int(p.get('hs_call_duration') or 0) / 1000
        except (TypeError, ValueError):
            dur = 0
        oid = p.get('hubspot_owner_id')
        ic[oid]['call_ok' if dur >= CALL_OK_MIN_S else 'call_try'] += 1
        ts = parse_ts(p.get('hs_timestamp'))
        if ts:
            hours['calls'][ts.hour] += 1

    return ({k: v for k, v in ic.items()},
            {'mails': dict(hours['mails']), 'calls': dict(hours['calls'])})


def build_history_series(hist, name_of, ref):
    """Séries jour / semaine / mois pour les touchpoints et les appels,
    plus la ventilation horaire par profondeur."""
    days = sorted(hist['days'].keys())

    def agg(keys):
        s = {'sent': 0, 'recv': 0, 'call_ok': 0, 'call_try': 0}
        for k in keys:
            for v in hist['days'][k].get('ic', {}).values():
                for f in s:
                    s[f] += v.get(f, 0)
        return s

    # --- 30 derniers jours calendaires
    d30 = [(ref - datetime.timedelta(days=k)).date().isoformat() for k in range(29, -1, -1)]
    touch_days = {'days': [], 'sent': [], 'recv': []}
    call_days = {'days': [], 'ok': [], 'tries': []}
    for k in d30:
        lbl = datetime.date.fromisoformat(k).strftime('%d/%m')
        a = agg([k]) if k in hist['days'] else {'sent': 0, 'recv': 0, 'call_ok': 0, 'call_try': 0}
        touch_days['days'].append(lbl); touch_days['sent'].append(a['sent']); touch_days['recv'].append(a['recv'])
        call_days['days'].append(lbl); call_days['ok'].append(a['call_ok']); call_days['tries'].append(a['call_try'])

    # --- semaines ISO
    byw = defaultdict(list)
    for k in days:
        byw[iso_week_label(datetime.date.fromisoformat(k))].append(k)
    weeks = sorted(byw, key=lambda w: (int(w.split('W')[0]), int(w.split('W')[1])))
    tw = {'weeks': weeks, 'sent': [], 'recv': []}
    cw = {'weeks': weeks, 'ok': [], 'tries': []}
    for w in weeks:
        a = agg(byw[w])
        tw['sent'].append(a['sent']); tw['recv'].append(a['recv'])
        cw['ok'].append(a['call_ok']); cw['tries'].append(a['call_try'])

    # --- mois
    MOIS = ['Jan', 'Fév', 'Mar', 'Avr', 'Mai', 'Jui', 'Jul', 'Aoû', 'Sep', 'Oct', 'Nov', 'Déc']
    bym = defaultdict(list)
    for k in days:
        d = datetime.date.fromisoformat(k)
        bym[f"{MOIS[d.month - 1]}{str(d.year)[2:]}"].append(k)
    months = sorted(bym, key=lambda m: datetime.date.fromisoformat(bym[m][0]))
    tm = {'months': months, 'sent': [], 'recv': []}
    cm = {'months': months, 'ok': [], 'tries': []}
    for m in months:
        a = agg(bym[m])
        tm['sent'].append(a['sent']); tm['recv'].append(a['recv'])
        cm['ok'].append(a['call_ok']); cm['tries'].append(a['call_try'])

    # --- historique hebdo par IC (onglet Zoom IC)
    per_ic = {}
    for w in weeks:
        for k in byw[w]:
            for oid, v in hist['days'][k].get('ic', {}).items():
                nm = name_of.get(str(oid))
                if not nm:
                    continue
                h = per_ic.setdefault(nm, {'weeks': [], 'sent': [], 'recv': [],
                                           'calls_ok': [], 'calls_try': []})
                if not h['weeks'] or h['weeks'][-1] != w:
                    h['weeks'].append(w)
                    for f in ('sent', 'recv', 'calls_ok', 'calls_try'):
                        h[f].append(0)
                h['sent'][-1] += v.get('sent', 0)
                h['recv'][-1] += v.get('recv', 0)
                h['calls_ok'][-1] += v.get('call_ok', 0)
                h['calls_try'][-1] += v.get('call_try', 0)

    # --- ventilation horaire par profondeur
    def hours_for(keys):
        m, c = defaultdict(int), defaultdict(int)
        for k in keys:
            hh = hist['days'][k].get('hours', {})
            for h_, n in (hh.get('mails') or {}).items():
                m[int(h_)] += n
            for h_, n in (hh.get('calls') or {}).items():
                c[int(h_)] += n
        rng = list(range(HOUR_MIN, HOUR_MAX + 1))
        tot = sum(m.values()) or 1
        return {'hours': [f'{h}h' for h in rng],
                'mails': [m[h] for h in rng], 'calls': [c[h] for h in rng],
                'out_of_range_pct': round(sum(n for h, n in m.items()
                                              if h < 8 or h >= 19) / tot * 100, 1)}

    y = ref.year
    hourly_by_period = {
        'weeks': hours_for([k for k in days if datetime.date.fromisoformat(k).year == y]),
        'months': hours_for(days),
    }
    return {
        'CARE_TOUCH_DAYS': touch_days, 'CARE_TOUCH_WEEKS': tw, 'CARE_TOUCH_MONTHS': tm,
        'CARE_CALL_DAYS': call_days, 'CARE_CALL_WEEKS': cw, 'CARE_CALL_MONTHS': cm,
        'CARE_IC_HISTORY': per_ic,
        'CARE_HOURLY_BY_PERIOD': hourly_by_period,
    }

# ============================================================
# CALCULS
# ============================================================
def compute_touch(emails, ic, care_ids, ref):
    """Touchpoints par IC et par période + séries jour / semaine."""
    ev = []
    for e in emails:
        p = e['properties']
        ts = parse_ts(p.get('hs_timestamp'))
        if ts:
            ev.append((ts, p.get('hubspot_owner_id'),
                       p.get('hs_email_direction') == 'EMAIL'))

    by_period = {}
    for period in PERIODS:
        s, e_ = period_bounds(period, ref)
        nd = business_days(s, e_ or ref)
        rows = []
        for oid in care_ids:
            sent = sum(1 for ts, o, out in ev if o == oid and out and in_period(ts, period, ref))
            recv = sum(1 for ts, o, out in ev if o == oid and not out and in_period(ts, period, ref))
            rows.append({
                'name': ic[oid]['name'],
                'level': ic[oid]['level'],
                'tl': ic[oid].get('tl', ''),
                'sent': sent,
                'recv': recv,
                'per_day': round(sent / nd, 1),
            })
        rows.sort(key=lambda r: -r['sent'])
        by_period[period] = rows

    # série 30 jours, département
    days, d_sent, d_recv = [], [], []
    for k in range(29, -1, -1):
        d = (ref - datetime.timedelta(days=k)).date()
        days.append(d.strftime('%d/%m'))
        d_sent.append(sum(1 for ts, o, out in ev if ts.date() == d and out))
        d_recv.append(sum(1 for ts, o, out in ev if ts.date() == d and not out))

    # série hebdo
    wk_s, wk_r = defaultdict(int), defaultdict(int)
    for ts, o, out in ev:
        (wk_s if out else wk_r)[iso_week_label(ts)] += 1
    weeks = sorted(set(list(wk_s) + list(wk_r)),
                   key=lambda w: (int(w.split('W')[0]), int(w.split('W')[1])))

    return (by_period,
            {'days': days, 'sent': d_sent, 'recv': d_recv},
            {'weeks': weeks, 'sent': [wk_s[w] for w in weeks], 'recv': [wk_r[w] for w in weeks]})


def compute_calls(calls, dispo, ic, care_ids, ref):
    """Appels SORTANTS uniquement (confirmé : le Care ne prend aucun entrant)."""
    ev = []
    for c in calls:
        p = c['properties']
        ts = parse_ts(p.get('hs_timestamp'))
        if not ts or p.get('hs_call_direction') != 'OUTBOUND':
            continue
        try:
            dur = int(p.get('hs_call_duration') or 0) / 1000
        except (TypeError, ValueError):
            dur = 0
        g = p.get('hs_call_disposition') or ''
        ev.append((ts, p.get('hubspot_owner_id'), dur,
                   dispo.get(g, (g[:8] + '…') if g else 'sans résultat'),
                   dur >= CALL_OK_MIN_S))

    by_period = {}
    for period in PERIODS:
        s, e_ = period_bounds(period, ref)
        nd = business_days(s, e_ or ref)
        rows = []
        dept_dispo = defaultdict(int)
        for oid in care_ids:
            mine = [(d, lb, ok) for ts, o, d, lb, ok in ev
                    if o == oid and in_period(ts, period, ref)]
            for _, lb, _ok in mine:
                dept_dispo[lb] += 1
            aboutis = [d for d, _, ok in mine if ok]
            tries = [d for d, _, ok in mine if not ok]
            md = median(aboutis)   # médiane sur les APPELS RÉELS uniquement
            rows.append({
                'name': ic[oid]['name'],
                'level': ic[oid]['level'],
                'tl': ic[oid].get('tl', ''),
                'n': len(aboutis),
                'tries': len(tries),
                'ok_rate': round(len(aboutis) / len(mine) * 100, 1) if mine else None,
                'per_day': round(len(aboutis) / nd, 1),
                'median_s': round(md) if md else None,
                'total_min': round(sum(aboutis) / 60),
            })
        rows.sort(key=lambda r: -r['n'])
        tot_ok = sum(r['n'] for r in rows)
        tot_try = sum(r['tries'] for r in rows)
        by_period[period] = {
            'ics': rows,
            'total': tot_ok,
            'tries': tot_try,
            'ok_rate': round(tot_ok / (tot_ok + tot_try) * 100, 1) if (tot_ok + tot_try) else None,
            'per_day': round(tot_ok / nd, 1),
            'dispositions': dict(sorted(dept_dispo.items(), key=lambda x: -x[1])),
            'threshold_s': CALL_OK_MIN_S,
        }
    return by_period


def compute_hourly(emails, calls, ic, care_ids):
    """Répartition horaire, jours ouvrés uniquement, heure de Paris."""
    hours = list(range(HOUR_MIN, HOUR_MAX + 1))
    dm = defaultdict(int)
    dc = defaultdict(int)
    per_ic = {oid: {'mails': defaultdict(int), 'calls': defaultdict(int)} for oid in care_ids}

    for e in emails:
        p = e['properties']
        if p.get('hs_email_direction') != 'EMAIL':
            continue
        ts = parse_ts(p.get('hs_timestamp'))
        if not ts or ts.weekday() >= 5:
            continue
        dm[ts.hour] += 1
        oid = p.get('hubspot_owner_id')
        if oid in per_ic:
            per_ic[oid]['mails'][ts.hour] += 1

    for c in calls:
        p = c['properties']
        if p.get('hs_call_direction') != 'OUTBOUND':
            continue
        try:
            if int(p.get('hs_call_duration') or 0) / 1000 < CALL_OK_MIN_S:
                continue      # tentative de numérotation, pas une activité
        except (TypeError, ValueError):
            continue
        ts = parse_ts(p.get('hs_timestamp'))
        if not ts or ts.weekday() >= 5:
            continue
        dc[ts.hour] += 1
        oid = p.get('hubspot_owner_id')
        if oid in per_ic:
            per_ic[oid]['calls'][ts.hour] += 1

    tot_m = sum(dm.values()) or 1
    return {
        'hours': [f'{h}h' for h in hours],
        'mails': [dm[h] for h in hours],
        'calls': [dc[h] for h in hours],
        'out_of_range_pct': round(sum(n for h, n in dm.items()
                                      if h < 8 or h >= 19) / tot_m * 100, 1),
        'per_ic': {
            ic[oid]['name']: {
                'mails': [per_ic[oid]['mails'][h] for h in hours],
                'calls': [per_ic[oid]['calls'][h] for h in hours],
            } for oid in care_ids
        },
    }


def compute_delays(emails, e2t, ic, care_ids, ref):
    """DEUX métriques distinctes, c'est le correctif clé de l'audit.

      reply   : mail client entrant → premier mail sortant de l'IC.
                C'est la réactivité réelle de l'IC.
      relance : deux mails sortants consécutifs sans entrant entre les deux.
                C'est la discipline de suivi.

    Chacune en heures brutes ET en heures ouvrées.
    """
    by_ticket = defaultdict(list)
    for e in emails:
        tid = e2t.get(e['id'])
        p = e['properties']
        ts = parse_ts(p.get('hs_timestamp'))
        if tid and ts:
            by_ticket[tid].append((ts, p.get('hubspot_owner_id'),
                                   p.get('hs_email_direction') == 'EMAIL'))

    reply = defaultdict(list)      # oid → [(ts, raw_h, biz_h)]
    relance = defaultdict(list)
    skipped = {'n': 0}             # doubles envois écartés par le plancher
    for evts in by_ticket.values():
        evts.sort(key=lambda x: x[0])
        pending_in = None
        prev_out = None
        for ts, oid, is_out in evts:
            if not is_out:
                pending_in = ts
                prev_out = None
                continue
            if pending_in is not None:
                reply[oid].append((ts, (ts - pending_in).total_seconds() / 3600,
                                   biz_hours_between(pending_in, ts)))
                pending_in = None
            elif prev_out is not None:
                raw_h = (ts - prev_out).total_seconds() / 3600
                if raw_h >= RELANCE_FLOOR_H:
                    relance[oid].append((ts, raw_h, biz_hours_between(prev_out, ts)))
                else:
                    skipped['n'] += 1
            prev_out = ts

    by_period = {}
    for period in PERIODS:
        rows = []
        for oid in care_ids:
            rp = [(r, b) for ts, r, b in reply[oid] if in_period(ts, period, ref)]
            rl = [(r, b) for ts, r, b in relance[oid] if in_period(ts, period, ref)]
            rows.append({
                'name': ic[oid]['name'],
                'level': ic[oid]['level'],
                'reply_n': len(rp),
                'reply_raw_h': round(median([r for r, _ in rp]), 1) if len(rp) >= 5 else None,
                'reply_biz_h': round(median([b for _, b in rp]), 1) if len(rp) >= 5 else None,
                'reply_p75_biz': round(pct_at([b for _, b in rp], 0.75), 1) if len(rp) >= 5 else None,
                'relance_n': len(rl),
                'relance_raw_h': round(median([r for r, _ in rl]), 1) if len(rl) >= 5 else None,
                'relance_biz_h': round(median([b for _, b in rl]), 1) if len(rl) >= 5 else None,
            })
        rows.sort(key=lambda r: (r['reply_biz_h'] is None, r['reply_biz_h'] or 0))
        by_period[period] = rows

    all_reply = [b for oid in care_ids for _, _, b in reply[oid]]
    all_relance = [b for oid in care_ids for _, _, b in relance[oid]]
    return {
        'by_period': by_period,
        'dept_reply_biz_h': round(median(all_reply), 1) if all_reply else None,
        'dept_relance_biz_h': round(median(all_relance), 1) if all_relance else None,
        'n_tickets': len(by_ticket),
        'min_sample': 5,
        'biz_window': f'{BIZ_START}h-{BIZ_END}h, lun-ven',
        'relance_floor_min': round(RELANCE_FLOOR_H * 60),
        'double_sends_excluded': skipped['n'],
    }


def compute_ces(subs, ic, ref, ic_all=None):
    """Détail nominatif des réponses CES + distribution par IC et par semaine."""
    owner_name = {oid: e['name'] for oid, e in ic.items()}
    departed = {oid for oid, e in (ic_all or {}).items() if oid not in owner_name}
    rows = []
    for s in subs:
        p = s['properties']
        ts = parse_ts(p.get('hs_submission_timestamp'))
        if not ts:
            continue
        try:
            note = int(float(p.get('hs_value')))
        except (TypeError, ValueError):
            continue
        oid = str(p.get('hs_ticket_owner_id') or '')
        tid = p.get('hs_ticket_id')
        # Les IC qui ont quitté l'équipe sont regroupés sur une ligne unique :
        # leur travail reste dans le total département, sans polluer le comparatif.
        label = owner_name.get(oid) or ('IC sortis' if oid in departed else 'hors périmètre')
        verb = ' '.join((p.get('hs_content') or '').split())
        rows.append({
            'date': ts.strftime('%d/%m'),
            'ts': ts.isoformat(timespec='minutes'),
            'week': iso_week_label(ts),
            'note': note,
            'group': p.get('hs_response_group') or '',
            'ic': label,
            'ic_id': oid,
            'subject': (p.get('hs_ticket_subject') or '')[:90],
            'ticket_id': tid,
            'ticket_url': TICKET_URL.format(tid) if tid else None,
            'verbatim': verb if PUBLISH_VERBATIMS else '',
            'has_verbatim': bool(verb),
        })
    rows.sort(key=lambda r: r['ts'], reverse=True)

    by_ic = {}
    for r in rows:
        d = by_ic.setdefault(r['ic'], {'n': 0, 'high': 0, 'low': 0, 'dist': {},
                                       'verbatims': 0})
        d['n'] += 1
        d['dist'][r['note']] = d['dist'].get(r['note'], 0) + 1
        if r['note'] >= 6:
            d['high'] += 1
        if r['note'] <= 3:
            d['low'] += 1
        if r['has_verbatim']:
            d['verbatims'] += 1
    for d in by_ic.values():
        d['pct_high'] = round(d['high'] / d['n'] * 100, 1) if d['n'] else None

    wk = defaultdict(lambda: {'n': 0, 'high': 0})
    for r in rows:
        wk[r['week']]['n'] += 1
        if r['note'] >= 6:
            wk[r['week']]['high'] += 1
    weeks = sorted(wk, key=lambda w: (int(w.split('W')[0]), int(w.split('W')[1])))

    dist = defaultdict(int)
    for r in rows:
        dist[r['note']] += 1

    unmapped = defaultdict(lambda: {'n': 0, 'high': 0})
    for r in rows:
        if r['ic'] == 'hors périmètre':
            unmapped[r['ic_id'] or 'sans owner']['n'] += 1
            if r['note'] >= 6:
                unmapped[r['ic_id'] or 'sans owner']['high'] += 1

    return {
        'available': bool(rows),
        'n_total': len(rows),
        'unmapped_owners': dict(sorted(unmapped.items(), key=lambda x: -x[1]['n'])),
        'window_days': CES_DAYS,
        'verbatims_published': PUBLISH_VERBATIMS,
        'distribution': {str(k): dist[k] for k in sorted(dist)},
        'pct_high': round(sum(n for k, n in dist.items() if k >= 6) / len(rows) * 100, 1) if rows else None,
        'by_ic': by_ic,
        'weeks': weeks,
        'weekly_pct_high': [round(wk[w]['high'] / wk[w]['n'] * 100, 1) if wk[w]['n'] else None
                            for w in weeks],
        'weekly_n': [wk[w]['n'] for w in weeks],
        'responses': rows,
    }


# ============================================================
# ORCHESTRATION
# ============================================================
def compute_care_activity(token, ic_map_raw, ref_now=None, verbose=False,
                          history_dir=None, backfill=False):
    """ic_map_raw : liste de dicts {owner_id, name, tl, level, active}
    (le contenu de ic_config.json, ou la table reconstruite par refresh.py)."""
    ref = ref_now or datetime.datetime.now()
    api = Api(token)
    t0 = time.time()

    ic = {str(e['owner_id']): e for e in ic_map_raw if e.get('active', True)}
    ic_all = {str(e['owner_id']): e for e in ic_map_raw}
    care_ids = [oid for oid, e in ic.items() if e['level'] in CARE_LEVELS]
    name_of = {oid: e['name'] for oid, e in ic.items()}
    start = (ref - datetime.timedelta(days=WINDOW_DAYS)).replace(
        hour=0, minute=0, second=0, microsecond=0)

    def log(m):
        if verbose:
            print(m, flush=True)

    log(f"care_activity · {len(care_ids)} IC Care · fenêtre {WINDOW_DAYS}j")

    emails = fetch_emails(api, care_ids, start, WINDOW_DAYS)
    log(f"  emails            {len(emails):6d}")

    calls = fetch_calls(api, care_ids, start, WINDOW_DAYS)
    log(f"  appels            {len(calls):6d}")

    dispo, dispo_err = fetch_dispositions(api)
    log(f"  dispositions      {len(dispo):6d}" + (f"  ({dispo_err})" if dispo_err else ""))

    subs = fetch_ces(api, ref - datetime.timedelta(days=CES_DAYS))
    log(f"  réponses CES      {len(subs):6d}")

    e2t, assoc_report = api.assoc('emails', 'tickets', [e['id'] for e in emails])
    cov = round(len(e2t) / len(emails) * 100, 1) if emails else 0

    # Couverture VENTILEE PAR DIRECTION : c'est le seul chiffre qui compte.
    # Un entrant non rattaché est normal ; un sortant non rattaché est un trou.
    cov_dir = {}
    for d, lab in (('EMAIL', 'sortants'), ('INCOMING_EMAIL', 'entrants')):
        sub = [e for e in emails if e['properties'].get('hs_email_direction') == d]
        lk = sum(1 for e in sub if e['id'] in e2t)
        cov_dir[lab] = {'n': len(sub), 'linked': lk,
                        'pct': round(lk / len(sub) * 100, 1) if sub else None}
    log(f"  rattachés ticket  {len(e2t):6d}  ({cov}% global)")
    log(f"    sortants        {cov_dir['sortants']['linked']:6d}/"
        f"{cov_dir['sortants']['n']}  ({cov_dir['sortants']['pct']}%)")
    log(f"    entrants        {cov_dir['entrants']['linked']:6d}/"
        f"{cov_dir['entrants']['n']}  ({cov_dir['entrants']['pct']}%)")
    if assoc_report['chunks_failed']:
        log(f"  ⚠️ {assoc_report['chunks_failed']}/{assoc_report['chunks']} lots "
            f"d'associations en échec :")
        for e_ in assoc_report['errors'][:5]:
            log(f"      {e_}")
    if (cov_dir['sortants']['pct'] or 100) < 95:
        log(f"  ⚠️ couverture des SORTANTS sous 95% : les délais par ticket "
            f"portent sur une base incomplète.")

    touch, touch_days, touch_weeks = compute_touch(emails, ic, care_ids, ref)

    # ---------- historique incrémental ----------
    hdir = Path(history_dir or SCRIPT_DIR)
    hpath = hdir / HISTORY_FILE
    hist = load_history(hpath)
    h_start = max(datetime.datetime.fromisoformat(HISTORY_FROM),
                  ref - datetime.timedelta(days=365 * 3))
    todo = missing_days(hist, h_start, ref)
    cap = len(todo) if backfill else min(len(todo), HISTORY_BACKFILL_MAX_DAYS)
    if cap < len(todo):
        todo = todo[-cap:] if not backfill else todo
    log(f"  historique        {len(hist['days'])} jours en base · {len(todo)} à compléter"
        + ("  (backfill)" if backfill else ""))
    for n, day in enumerate(todo, 1):
        ic_counts, hours = fetch_day_counts(api, care_ids, day)
        hist['days'][day.isoformat()] = {'ic': ic_counts, 'hours': hours}
        if verbose and (n % 20 == 0 or n == len(todo)):
            log(f"    {n}/{len(todo)} jours ({day})")
    hist['meta'] = {'updated_at': ref.strftime('%Y-%m-%d %H:%M'),
                    'from': HISTORY_FROM, 'n_days': len(hist['days']),
                    'call_threshold_s': CALL_OK_MIN_S}
    if todo:
        save_history(hpath, hist)
    series = build_history_series(hist, name_of, ref) if hist['days'] else {}

    out = {
        'CARE_TOUCH': touch,
        'CARE_TOUCH_DAYS': touch_days,
        'CARE_TOUCH_WEEKS': touch_weeks,
        'CARE_CALLS': compute_calls(calls, dispo, ic, care_ids, ref),
        'CARE_HOURLY': compute_hourly(emails, calls, ic, care_ids),
        'CARE_DELAYS': compute_delays(emails, e2t, ic, care_ids, ref),
        'CARE_CES': compute_ces(subs, ic, ref, ic_all),
        'CARE_META': {
            'window_days': WINDOW_DAYS,
            'n_emails': len(emails),
            'n_calls': len(calls),
            'ticket_coverage_pct': cov,
            'ticket_coverage_by_direction': cov_dir,
            'assoc_report': assoc_report,
            'page_limit': PAGE_LIMIT,
            'dispositions': dispo,
            'dispositions_error': dispo_err,
            'api_calls': api.calls,
            'elapsed_s': round(time.time() - t0, 1),
            'computed_at': ref.strftime('%Y-%m-%d %H:%M'),
            'call_threshold_s': CALL_OK_MIN_S,
            'history_days': len(hist['days']),
            'history_from': (min(hist['days']) if hist['days'] else None),
            'history_filled_this_run': len(todo),
        },
    }
    # Les séries historiques écrasent les séries de la fenêtre glissante :
    # elles sont plus profondes et couvrent les mêmes 30 jours.
    out.update(series)
    log(f"  {api.calls} appels API · {out['CARE_META']['elapsed_s']}s")
    return out


# ============================================================
# MODE AUTONOME
# ============================================================
def _standalone():
    cfg_path = SCRIPT_DIR / 'config.json'
    ic_path = SCRIPT_DIR / 'ic_config.json'
    if not cfg_path.exists():
        sys.exit(f"config.json introuvable dans {SCRIPT_DIR}")
    token = (json.loads(cfg_path.read_text(encoding='utf-8')).get('hubspot_token') or '').strip()
    if not token:
        sys.exit("hubspot_token absent de config.json")
    ic_raw = json.loads(ic_path.read_text(encoding='utf-8'))['ic_map']

    backfill = '--backfill' in sys.argv
    data = compute_care_activity(token, ic_raw, verbose=True, backfill=backfill)

    outp = SCRIPT_DIR / 'care_activity.json'
    outp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')

    m = data['CARE_META']
    print(f"\n{'=' * 70}\nRESUME · fenetre {m['window_days']}j · "
          f"{m['api_calls']} appels API · {m['elapsed_s']}s\n{'=' * 70}")

    cd = m['ticket_coverage_by_direction']
    print(f"\nCOUVERTURE TICKET")
    for lab in ('sortants', 'entrants'):
        v = cd[lab]
        print(f"  {lab:10s} {v['linked']:6d}/{v['n']:<6d} {v['pct']}%")
    ar = m['assoc_report']
    print(f"  lots associations : {ar['chunks']} dont {ar['chunks_failed']} en echec")
    for e_ in ar['errors'][:5]:
        print(f"    {e_}")

    print(f"\nTOUCHPOINTS · 30 derniers jours")
    print(f"{'IC':12s} {'niveau':11s} {'sortants':>9s} {'/j ouvre':>9s} {'entrants':>9s}")
    for r in data['CARE_TOUCH']['30d']:
        print(f"{r['name']:12s} {r['level']:11s} {r['sent']:9d} "
              f"{r['per_day']:9.1f} {r['recv']:9d}")

    c = data['CARE_CALLS']['30d']
    print(f"\nAPPELS SORTANTS · 30j · total {c['total']} ({c['per_day']}/j ouvre)")
    print(f"{'IC':12s} {'n':>6s} {'/j':>6s} {'med.':>7s} {'min cumul':>10s}")
    for r in c['ics']:
        print(f"{r['name']:12s} {r['n']:6d} {r['per_day']:6.1f} "
              f"{(str(r['median_s']) + 's' if r['median_s'] else '-'):>7s} {r['total_min']:10d}")
    print(f"\nDispositions : {c['dispositions']}")

    d = data['CARE_DELAYS']
    print(f"\nDELAIS · 30j · heures ouvrees ({d['biz_window']})")
    print(f"Departement : reponse {d['dept_reply_biz_h']}h · relance {d['dept_relance_biz_h']}h")
    print(f"Doubles envois ecartes (< {d['relance_floor_min']} min) : "
          f"{d['double_sends_excluded']}")
    print(f"{'IC':12s} {'n rep':>6s} {'reponse':>9s} {'p75':>7s} {'n rel':>6s} {'relance':>9s}")
    for r in d['by_period']['30d']:
        f = lambda v: f"{v}h" if v is not None else '-'
        print(f"{r['name']:12s} {r['reply_n']:6d} {f(r['reply_biz_h']):>9s} "
              f"{f(r['reply_p75_biz']):>7s} {r['relance_n']:6d} {f(r['relance_biz_h']):>9s}")

    h = data['CARE_HOURLY']
    mx = max(h['mails'] + [1])
    print(f"\nHORAIRE · hors 8h-19h : {h['out_of_range_pct']}%")
    for i, lab in enumerate(h['hours']):
        b = '#' * max(0, round(h['mails'][i] / mx * 28))
        print(f"{lab:5s} {h['mails'][i]:5d} mails {h['calls'][i]:4d} calls  {b}")

    ces = data['CARE_CES']
    print(f"\nCES · {ces['n_total']} reponses sur {ces['window_days']}j · "
          f"{ces['pct_high']}% notes 6-7 · verbatims publies : {ces['verbatims_published']}")
    print(f"Distribution : {ces['distribution']}")
    print(f"{'IC':16s} {'n':>5s} {'6-7':>5s} {'1-3':>5s} {'% haut':>8s}")
    for name, v in sorted(ces['by_ic'].items(), key=lambda x: -x[1]['n']):
        print(f"{name:16s} {v['n']:5d} {v['high']:5d} {v['low']:5d} {v['pct_high']:7.1f}%")
    if ces['unmapped_owners']:
        print(f"\nOwners CES hors ic_config ({len(ces['unmapped_owners'])}) :")
        for oid, v in ces['unmapped_owners'].items():
            print(f"  owner {oid:>12s} : {v['n']:3d} reponses, {v['high']:3d} en 6-7")

    print("\n3 dernieres reponses :")
    for r in ces['responses'][:3]:
        print(f"  [{r['note']}] {r['date']} · {r['ic']} · {r['verbatim'][:70] or '(sans verbatim)'}")
        print(f"       {r['ticket_url']}")

    print(f"\n→ ecrit dans {outp}")


if __name__ == '__main__':
    _standalone()
