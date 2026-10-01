import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Credenciais
    SPTRANS_TOKEN = os.getenv("SPTRANS_TOKEN")
    CEMADEN_EMAIL = os.getenv("CEMADEN_EMAIL")
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
    LINES_BRONZE_DIR = os.path.join(BRONZE_DIR, "lines")
    SPTRANS_SILVER_DIR = os.path.join(SILVER_DIR, "sptrans")
    CEMADEN_SILVER_DIR = os.path.join(SILVER_DIR, "cemaden")

    # Arquivos de dados
    SELECTED_LINES_PATH = os.path.join(LINES_BRONZE_DIR, "selected_lines.json")
    LINE_STATION_MAPPING_PATH = os.path.join(LINES_BRONZE_DIR, "line_station_mapping.parquet")

    # Parâmetros de coleta SPTrans
    SPTRANS_BASE_URL: str = os.getenv("SPTRANS_BASE_URL", "http://api.olhovivo.sptrans.com.br/v2.1")
    COLLECTION_INTERVAL_SECONDS: int = int(os.getenv("COLLECTION_INTERVAL_SECONDS", "60"))
    COLLECT_INTERVAL_SECONDS: int = int(os.getenv("COLLECT_INTERVAL_SECONDS", "60"))
    COLLECT_INTER_CALL_DELAY_SECONDS: float = float(os.getenv("COLLECT_INTER_CALL_DELAY_SECONDS", "0.2"))
    COLLECT_MAX_CONSECUTIVE_FAILED_CYCLES: int = int(os.getenv("COLLECT_MAX_CONSECUTIVE_FAILED_CYCLES", "5"))
    CYCLES_LOG_PATH: str = os.path.join(LOGS_DIR, "collect_sptrans_cycles.jsonl")
    HTTP_TIMEOUT_SECONDS: int = int(os.getenv("HTTP_TIMEOUT_SECONDS", "15"))
    HTTP_MAX_RETRIES: int = int(os.getenv("HTTP_MAX_RETRIES", "5"))
    HTTP_BACKOFF_BASE_SECONDS: int = int(os.getenv("HTTP_BACKOFF_BASE_SECONDS", "2"))

    CEMADEN_USER = os.getenv("CEMADEN_USER") or os.getenv("CEMADEN_EMAIL")

    # Parâmetros da API do CEMADEN
    CEMADEN_AUTH_URL: str = os.getenv("CEMADEN_AUTH_URL")
    CEMADEN_SCHEDULE_URL: str = os.getenv("CEMADEN_SCHEDULE_URL")
    CEMADEN_STATION_URL: str = os.getenv("CEMADEN_STATION_URL")
    CEMADEN_STATUS_URL: str = os.getenv("CEMADEN_STATUS_URL")
    CEMADEN_NETWORK_ID: str = os.getenv("CEMADEN_NETWORK_ID", "11")
    CEMADEN_SENSOR_ID: str = os.getenv("CEMADEN_SENSOR_ID", "10")
    CEMADEN_POLL_INTERVAL_SECONDS: int = int(os.getenv("CEMADEN_POLL_INTERVAL_SECONDS", "20"))
    CEMADEN_JOB_TIMEOUT_SECONDS: int = int(os.getenv("CEMADEN_JOB_TIMEOUT_SECONDS", "1800"))
    CEMADEN_MAX_PENDING_JOBS: int = int(os.getenv("CEMADEN_MAX_PENDING_JOBS", "5"))
    CEMADEN_SOURCE_TZ: str = "America/Sao_Paulo"
    CEMADEN_EXPECTED_CADENCE_MIN: int = 60
    # Limite físico de plausibilidade para São Paulo: precipitação > 150mm em um intervalo é anomalamente extrema/ruído
    RAIN_MAX_MM_PER_INTERVAL: float = float(os.getenv("RAIN_MAX_MM_PER_INTERVAL", "150.0"))
    RAIN_SENTINEL_VALUES: tuple[float, ...] = (-999.0, -9999.0, -99.0)
    GAP_FACTOR: float = 1.5

    # Arquivos e diretórios de metadados CEMADEN
    CEMADEN_STATION_STATUS_DIR: str = os.path.join(CEMADEN_BRONZE_DIR, "_station_status")
    CEMADEN_MANIFEST_PATH: str = os.path.join(CEMADEN_BRONZE_DIR, "_manifest.jsonl")
    CEMADEN_GAPS_PATH: str = os.path.join(CEMADEN_SILVER_DIR, "gaps.parquet")
    CEMADEN_QUALITY_REPORT_PATH: str = os.path.join(CEMADEN_SILVER_DIR, "quality_report.json")
    CEMADEN_INGEST_SUMMARY_PATH: str = os.path.join(REPORTS_DIR, "cemaden_ingest_summary.json")

    # Regras geográficas e de velocidade
    SP_BBOX: dict = dict(lat_min=-24.10, lat_max=-23.30, lon_min=-47.00, lon_max=-46.20)
    MAX_SPEED_KMH: int = 80
    MIN_DELTA_T_SECONDS: int = 20
    MAX_DELTA_T_SECONDS: int = 300

    # Fusos
    TZ_UTC: str = "UTC"
    TZ_LOCAL: str = "America/Sao_Paulo"

    # Regras e limites para a Silver da SPTrans (Task 5)
    SPEED_MAX_KMH: float = 80.0
    DT_MIN_S: float = 20.0
    DT_MAX_S: float = 300.0
    STOP_DIST_M: float = 20.0
    PARKED_MIN_MINUTES: float = 10.0
    STALE_MAX_MINUTES: float = 10.0
    OFF_ROUTE_M: float = 300.0
    PEAK_WINDOWS_SP: list = [("06:00", "09:00"), ("17:00", "20:00")]

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
            cls.LINES_BRONZE_DIR,
            cls.SILVER_DIR,
            cls.SPTRANS_SILVER_DIR,
            cls.CEMADEN_SILVER_DIR,
            cls.CEMADEN_STATION_STATUS_DIR,
            cls.REPORTS_DIR,
            cls.LOGS_DIR,
        ]
        for directory in dirs:
            os.makedirs(directory, exist_ok=True)