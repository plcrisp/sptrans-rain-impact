from datetime import datetime
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.clients.cemaden_client import CemadenClient
from src.core.config import Config
from src.utils.logger import get_logger

logger = get_logger("station_parser")

DAYS_WITHOUT_DATA_OUTAGE = 30
STATE = "SP"
CITY_NAME = "SÃO PAULO"


def infer_status(last_update_time: Optional[str]) -> str:
    """Infere o status operacional da estação com base na data da última remessa."""
    if not last_update_time:
        return "Offline"
    try:
        last_update = datetime.strptime(last_update_time[:19], "%Y-%m-%d %H:%M:%S")
        is_offline = (datetime.now() - last_update).days > DAYS_WITHOUT_DATA_OUTAGE
        return "Offline" if is_offline else "Active"
    except ValueError:
        return "Offline"


def normalize(e: Dict[str, Any], city_name: str) -> Dict[str, Any]:
    """Padroniza os metadados cadastrais de uma estação da API do CEMADEN."""
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


def get_sp_stations(
    output_dir: Optional[str] = None,
    client: Optional[CemadenClient] = None,
) -> List[Dict[str, Any]]:
    """Consulta cidades e estações de São Paulo via CemadenClient e salva metadados cadastrais."""
    Config.validate(["CEMADEN_EMAIL", "CEMADEN_PASSWORD"])

    cemaden = client or CemadenClient()

    cities, _, _ = cemaden.get_cities(uf=STATE)
    target_city = next(
        (c for c in cities if isinstance(c, dict) and c.get("cidade", "").upper() == CITY_NAME),
        None,
    )

    if not target_city:
        logger.error(f"Cidade '{CITY_NAME}' não encontrada no estado {STATE}.")
        return []

    ibge_code = target_city["codibge"]
    raw_stations, _, _ = cemaden.get_stations_by_city(ibge_code)

    normalized_stations = [
        normalize(e, target_city["cidade"]) for e in raw_stations if isinstance(e, dict)
    ]

    if not normalized_stations:
        logger.warning(f"Nenhuma estação encontrada para {CITY_NAME} (IBGE: {ibge_code}).")
        return []

    logger.info(f"Total de estações mapeadas para {CITY_NAME}: {len(normalized_stations)}.")

    if output_dir:
        output_file = Path(output_dir) / "cemaden_sp_stations.json"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(
            json.dumps(normalized_stations, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(f"Estações salvas com sucesso em: {output_file.resolve()}")

    return normalized_stations