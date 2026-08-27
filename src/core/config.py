import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    SPTRANS_TOKEN = os.getenv("SPTRANS_TOKEN")
    
    CEMADEN_USER = os.getenv("CEMADEN_USER")
    CEMADEN_PASSWORD = os.getenv("CEMADEN_PASSWORD")
    
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    DATA_DIR = os.path.join(BASE_DIR, "data")
    RAW_DATA_DIR = os.path.join(DATA_DIR, "bronze")
    GTFS_DATA_DIR = os.path.join(RAW_DATA_DIR, "gtfs")
    STATIONS_DATA_DIR = os.path.join(RAW_DATA_DIR, "stations")

if not Config.SPTRANS_TOKEN:
    raise ValueError("Variável de ambiente SPTRANS_TOKEN não encontrada no arquivo .env")

if not Config.CEMADEN_USER or not Config.CEMADEN_PASSWORD:
    raise ValueError("Credenciais do CEMADEN não encontradas no arquivo .env")