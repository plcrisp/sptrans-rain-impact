import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Credenciais
    SPTRANS_TOKEN = os.getenv("SPTRANS_TOKEN")
    CEMADEN_USER = os.getenv("CEMADEN_USER")
    CEMADEN_PASSWORD = os.getenv("CEMADEN_PASSWORD")

    # Diretórios base
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    DATA_DIR = os.path.join(BASE_DIR, "data")
    RAW_DATA_DIR = os.path.join(DATA_DIR, "bronze")
    GTFS_DATA_DIR = os.path.join(RAW_DATA_DIR, "gtfs")
    STATIONS_DATA_DIR = os.path.join(RAW_DATA_DIR, "stations")

    # Camadas medalhão e relatórios/logs
    BRONZE_DIR = RAW_DATA_DIR
    SILVER_DIR = os.path.join(DATA_DIR, "silver")
    REPORTS_DIR = os.path.join(DATA_DIR, "reports")
    LOGS_DIR = os.path.join(BASE_DIR, "logs")

    # Subpastas específicas por fonte
    SPTRANS_BRONZE_DIR = os.path.join(BRONZE_DIR, "sptrans")
    CEMADEN_BRONZE_DIR = os.path.join(BRONZE_DIR, "cemaden")
    SPTRANS_SILVER_DIR = os.path.join(SILVER_DIR, "sptrans")
    CEMADEN_SILVER_DIR = os.path.join(SILVER_DIR, "cemaden")

    # Arquivos de dados
    SELECTED_LINES_PATH = os.path.join(BRONZE_DIR, "selected_lines.json")
    LINE_STATION_MAPPING_PATH = os.path.join(BRONZE_DIR, "line_station_mapping.parquet")

    # Parâmetros de coleta SPTrans
    SPTRANS_BASE_URL: str = os.getenv("SPTRANS_BASE_URL", "http://api.olhovivo.sptrans.com.br/v2.1")
    COLLECTION_INTERVAL_SECONDS: int = int(os.getenv("COLLECTION_INTERVAL_SECONDS", "60"))
    HTTP_TIMEOUT_SECONDS: int = int(os.getenv("HTTP_TIMEOUT_SECONDS", "15"))
    HTTP_MAX_RETRIES: int = int(os.getenv("HTTP_MAX_RETRIES", "5"))
    HTTP_BACKOFF_BASE_SECONDS: int = int(os.getenv("HTTP_BACKOFF_BASE_SECONDS", "2"))

    # Regras geográficas e de velocidade
    SP_BBOX: dict = dict(lat_min=-24.10, lat_max=-23.30, lon_min=-47.00, lon_max=-46.20)
    MAX_SPEED_KMH: int = 80
    MIN_DELTA_T_SECONDS: int = 20
    MAX_DELTA_T_SECONDS: int = 300

    # Fusos
    TZ_UTC: str = "UTC"
    TZ_LOCAL: str = "America/Sao_Paulo"

    @classmethod
    def validate(cls, required: list[str]) -> None:
        """Valida se as variáveis de ambiente obrigatórias foram fornecidas."""
        missing = [attr for attr in required if not getattr(cls, attr, None)]
        if missing:
            missing_str = ", ".join(missing)
            raise ValueError(
                f"Variáveis obrigatórias ausentes ou vazias no arquivo .env: {missing_str}"
            )

    @classmethod
    def ensure_dirs(cls) -> None:
        """Cria todos os diretórios de dados e logs se não existirem."""
        dirs = [
            cls.DATA_DIR,
            cls.BRONZE_DIR,
            cls.GTFS_DATA_DIR,
            cls.STATIONS_DATA_DIR,
            cls.SPTRANS_BRONZE_DIR,
            cls.CEMADEN_BRONZE_DIR,
            cls.SILVER_DIR,
            cls.SPTRANS_SILVER_DIR,
            cls.CEMADEN_SILVER_DIR,
            cls.REPORTS_DIR,
            cls.LOGS_DIR,
        ]
        for directory in dirs:
            os.makedirs(directory, exist_ok=True)