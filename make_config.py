#!/usr/bin/env python3
"""
make_config.py — génère config.json à partir des variables d'environnement
(secrets GitHub Actions). Permet à refresh.py de tourner SANS modification :
il lit config.json comme d'habitude, qu'on soit sur le Mac ou dans le cloud.

Variables d'environnement attendues (définies en secrets GitHub) :
  HUBSPOT_TOKEN              (obligatoire)
  PARAM_PWD                  (optionnel — mot de passe onglet Paramètres)

Les autres clés (trustpilot_buid, widget_id, etc.) sont des valeurs publiques
non secrètes : on les écrit en dur ici (ou tu peux les passer en env aussi).

Usage (dans le workflow) :
  HUBSPOT_TOKEN=*** python3 make_config.py
"""
import json
import os
import sys

token = os.environ.get('HUBSPOT_TOKEN', '').strip()
if not token:
    print("ERREUR: variable d'environnement HUBSPOT_TOKEN absente ou vide.", file=sys.stderr)
    sys.exit(1)

config = {
    "hubspot_token": token,
    "trustpilot_buid": "63c82089f7ad8e71f3e6d1b6",
    "trustpilot_widget_id": "5419b732fbfb950b10de65e5",
    "trustpilot_locale": "fr-FR",
    "trustpilot_fallback_rating": 4.6,
    "trustpilot_fallback_review_count": 376,
    "google_fallback_rating": None,
    "google_fallback_review_count": None,
    "google_review_url": "https://www.google.com/search?q=nopillo+avis",
    "google_place_query": "Nopillo, 37 Bd Saint-Martin, Paris",
    "param_pwd": os.environ.get('PARAM_PWD', 'nopillo-cs-2026'),
}

with open('config.json', 'w', encoding='utf-8') as f:
    json.dump(config, f, indent=2, ensure_ascii=False)

print(f"config.json généré (token: {token[:12]}…, {len(config)} clés)")
