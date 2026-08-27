import json
import os
import time
import sys
import requests
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from typing import List, Dict, Optional

load_dotenv()

# Config
CEMADEN_USER = os.getenv("CEMADEN_USER")
CEMADEN_PASSWORD = os.getenv("CEMADEN_PASSWORD")

AUTH_URL = "https://sgaa.cemaden.gov.br/SGAA/rest/controle-token/tokens"
CITIES_URL = "https://sws.cemaden.gov.br/PED/rest/pcds-cadastro/cidades"
STATIONS_URL = "https://sws.cemaden.gov.br/PED/rest/pcds-cadastro/dados-cadastrais"

MAX_RETRIES = 3
BACKOFF_FACTOR = 2.0
DAYS_WITHOUT_DATA_OUTAGE = 30
MAX_GLOBAL_RETRIES = 12

STATE = "SP"
CITY_NAME = "SÃO PAULO"

# Auth
_token_cache: Optional[str] = None

def get_token() -> str:
    global _token_cache
    if _token_cache:
        return _token_cache
        
    r = requests.post(AUTH_URL, json={"email": CEMADEN_USER, "password": CEMADEN_PASSWORD}, timeout=15)
    r.raise_for_status()
    
    data = r.json()
    _token_cache = data.get("access_token") or data.get("token")
    
    if not _token_cache:
        raise RuntimeError("Token not found in auth response.")
        
    print("Cemaden Token acquired.")
    return _token_cache

def reset_token():
    global _token_cache
    _token_cache = None

# HTTP & Retries
_consecutive_global_retries = 0

def get_with_retry(url: str, params: Dict, timeout: int = 15) -> List[Dict]:
    global _consecutive_global_retries
    wait = 0.5
    
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            token = get_token()
            r = requests.get(url, headers={"token": token}, params=params, timeout=timeout)

            if r.status_code in (401, 400):
                print("    Token expired/invalid, reauthenticating...")
                reset_token()
                _consecutive_global_retries += 1
                check_global_limit()
                continue

            if r.status_code == 404:
                _consecutive_global_retries = 0 
                return []

            r.raise_for_status()
            _consecutive_global_retries = 0
            return r.json()

        except requests.exceptions.HTTPError as e:
            print(f"    HTTP {e.response.status_code} (attempt {attempt}/{MAX_RETRIES})")
            _consecutive_global_retries += 1
            
        except requests.exceptions.RequestException as e:
            print(f"    Connection error: {e} (attempt {attempt}/{MAX_RETRIES})")
            _consecutive_global_retries += 1

        check_global_limit()

        if attempt < MAX_RETRIES:
            print(f"    ⏳ Waiting {wait}s before next attempt...")
            time.sleep(wait)
            wait *= BACKOFF_FACTOR

    print(f"    Failed after {MAX_RETRIES} attempts: {url}")
    return []

def check_global_limit():
    global _consecutive_global_retries
    if _consecutive_global_retries >= MAX_GLOBAL_RETRIES:
        print("\n" + "!" * 60)
        print("ABORTING EXECUTION!")
        print(f"Reached {MAX_GLOBAL_RETRIES} consecutive failures.")
        print("!" * 60 + "\n")
        sys.exit(1)

# Normalization
def infer_status(last_update_time: Optional[str]) -> str:
    if not last_update_time:
        return "Offline"
    try:
        last_update = datetime.strptime(last_update_time[:19], "%Y-%m-%d %H:%M:%S")
        is_offline = (datetime.now() - last_update).days > DAYS_WITHOUT_DATA_OUTAGE
        return "Offline" if is_offline else "Active"
    except ValueError:
        return "Offline"

def normalize(e: Dict, city_name: str) -> Dict:
    return {
        "station_name": e.get("nome", ""),
        "city": city_name,
        "state": e.get("uf", ""),
        "latitude": e.get("latitude"),
        "longitude": e.get("longitude"),
        "status": infer_status(e.get("dh_ultima_remessa")),
        "installation_date": e.get("data_instalacao", "")[:10] if e.get("data_instalacao") else "",
        "last_update": e.get("dh_ultima_remessa", "")[:10] if e.get("dh_ultima_remessa") else "",
        "station_id": e.get("codestacao", ""),
        "station_type": e.get("tipoestacao_descricao", ""),
        "source": "CEMADEN",
    }

# Main
def get_sp_stations(output_dir=None):
    if not CEMADEN_USER or not CEMADEN_PASSWORD:
        raise RuntimeError("Set CEMADEN_USER and CEMADEN_PASSWORD in .env file")

    cities = get_with_retry(CITIES_URL, params={"uf": STATE, "formato": "JSON"})

    target_city = next((c for c in cities if isinstance(c, dict) and c.get("cidade", "").upper() == CITY_NAME), None)
    
    if not target_city:
        print(f"City '{CITY_NAME}' not found in {STATE}.")
        return

    ibge_code = target_city["codibge"]

    raw_stations = get_with_retry(STATIONS_URL, params={"codibge": ibge_code, "formato": "JSON"})
    
    normalized_stations = [
        normalize(e, target_city['cidade']) for e in raw_stations if isinstance(e, dict)
    ]

    if not normalized_stations:
        print("No stations found for this city.")
        return
    
    print(f"Total active/mapped stations: {len(normalized_stations)}")


    if output_dir:
        output_file = Path(output_dir) / "cemaden_sp_stations.json"
        
        output_file.parent.mkdir(parents=True, exist_ok=True)
        
        output_file.write_text(
            json.dumps(normalized_stations, ensure_ascii=False, indent=2), 
            encoding="utf-8"
        )
        print(f"Saved to: {output_file.resolve()}")

    return normalized_stations