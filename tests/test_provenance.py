from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import zipfile

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString

from src.clients.sptrans_client import PositionResponse
from src.core.config import Config
from src.pipelines.bronze import collect_sptrans, ingest_cemaden, select_lines
from src.pipelines.silver import build_cemaden_silver, build_sptrans_silver
from src.utils.gtfs_parser import build_gtfs_dataset
from src.utils.provenance import record_provenance, sanitize_url, to_relative_path
from src.utils.sptrans_silver import load_bronze
from src.utils.station_parser import get_sp_stations


def _read_jsonl(path: Path) -> list[dict]:
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def _isolate_config(monkeypatch, tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    ref_dir = data_dir / "reference"
    bronze_dir = data_dir / "bronze"
    silver_dir = data_dir / "silver"

    monkeypatch.setattr(Config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(Config, "REFERENCE_DIR", str(ref_dir))
    monkeypatch.setattr(Config, "GTFS_DATA_DIR", str(ref_dir / "gtfs"))
    monkeypatch.setattr(Config, "STATIONS_DATA_DIR", str(ref_dir / "stations"))
    monkeypatch.setattr(Config, "LINES_REFERENCE_DIR", str(ref_dir / "lines"))
    monkeypatch.setattr(Config, "LINES_BRONZE_DIR", str(ref_dir / "lines"))
    monkeypatch.setattr(Config, "RAW_DATA_DIR", str(bronze_dir))
    monkeypatch.setattr(Config, "BRONZE_DIR", str(bronze_dir))
    monkeypatch.setattr(Config, "SILVER_DIR", str(silver_dir))
    monkeypatch.setattr(Config, "SPTRANS_BRONZE_DIR", str(bronze_dir / "sptrans"))
    monkeypatch.setattr(Config, "CEMADEN_BRONZE_DIR", str(bronze_dir / "cemaden"))
    monkeypatch.setattr(Config, "SPTRANS_SILVER_DIR", str(silver_dir / "sptrans"))
    monkeypatch.setattr(Config, "CEMADEN_SILVER_DIR", str(silver_dir / "cemaden"))
    monkeypatch.setattr(Config, "SELECTED_LINES_PATH", str(ref_dir / "lines" / "selected_lines.json"))
    monkeypatch.setattr(
        Config, "LINE_STATION_MAPPING_PATH", str(ref_dir / "lines" / "line_station_mapping.parquet")
    )
    monkeypatch.setattr(Config, "LINE_RANKING_PATH", str(ref_dir / "lines" / "line_ranking.csv"))
    monkeypatch.setattr(Config, "CEMADEN_MANIFEST_PATH", str(bronze_dir / "cemaden" / "_manifest.jsonl"))
    monkeypatch.setattr(
        Config, "CEMADEN_STATION_STATUS_DIR", str(bronze_dir / "cemaden" / "_station_status")
    )
    return data_dir


def test_sanitize_url_strips_secrets_and_queries():
    assert (
        sanitize_url("https://user:secret_pass@api.example.com:8443/v1/data?token=ABC123#frag")
        == "https://api.example.com:8443/v1/data"
    )
    assert sanitize_url("http://olhovivo.sptrans.com.br/v2.1/?t=xyz") == "http://olhovivo.sptrans.com.br/v2.1/"
    assert sanitize_url("/relative/path?token=123") == "/relative/path"
    assert sanitize_url(None) == ""
    assert sanitize_url("") == ""


def test_to_relative_path_normalizes_repo_and_external_paths(tmp_path):
    repo_file = Path(Config.BASE_DIR) / "src" / "utils" / "provenance.py"
    assert to_relative_path(repo_file) == "src/utils/provenance.py"

    ext_file = tmp_path / "sub" / "file.csv"
    assert "\\" not in to_relative_path(ext_file)


def test_record_provenance_writes_local_and_global_incrementally(monkeypatch, tmp_path):
    data_dir = _isolate_config(monkeypatch, tmp_path)
    stage_dir = data_dir / "bronze" / "sptrans"

    local_path = record_provenance(
        target_dir=stage_dir,
        stage="collect_sptrans_bronze",
        source="http://api.example.com/Posicao/Linha",
        output=["date=2026-11-20/hour=14"],
        records=10,
        extra={"cycles": 1},
    )
    record_provenance(
        target_dir=stage_dir,
        stage="collect_sptrans_bronze",
        source="http://api.example.com/Posicao/Linha",
        output=["date=2026-11-20/hour=15"],
        records=5,
        extra={"cycles": 2},
    )

    assert local_path == stage_dir / "provenance.jsonl"
    local_entries = _read_jsonl(local_path)
    global_entries = _read_jsonl(data_dir / "provenance.jsonl")

    assert len(local_entries) == 2
    assert len(global_entries) == 2
    for idx, entry in enumerate(local_entries):
        assert set(["stage", "source", "output", "records", "executed_at"]).issubset(entry.keys())
        assert entry["executed_at"].endswith("Z")
        assert "directory" not in entry
        assert "directory" in global_entries[idx]

    # When target_dir equals global_dir, do not write duplicate lines
    record_provenance(
        target_dir=data_dir,
        stage="custom_root_stage",
        source="test_source",
        output="out.csv",
        records=1,
    )
    assert len(_read_jsonl(data_dir / "provenance.jsonl")) == 3


def test_load_bronze_ignores_provenance_jsonl(tmp_path):
    bronze_dir = tmp_path / "bronze" / "sptrans"
    part_dir = bronze_dir / "date=2026-11-20" / "hour=14"
    part_dir.mkdir(parents=True)

    (bronze_dir / "provenance.jsonl").write_text('{"stage": "collect_sptrans_bronze"}\n', encoding="utf-8")
    sample_json = {
        "envelope": {
            "collected_at_utc": "2026-11-20T14:00:00Z",
            "codigoLinha": 910,
            "route_id": "2202-10",
            "direction_id": 1,
        },
        "payload": {"vs": [{"p": "1001", "py": -23.55, "px": -46.63, "ta": "2026-11-20T13:59:30Z"}]},
    }
    (part_dir / "posicao_910_20261120T140000000000Z.json").write_text(
        json.dumps(sample_json), encoding="utf-8"
    )

    df, stats = load_bronze(bronze_dir)
    assert len(df) == 1
    assert stats == {"files_read": 1, "files_corrupted": 0, "files_ignored": 0}


def test_build_gtfs_dataset_records_provenance(monkeypatch, tmp_path):
    data_dir = _isolate_config(monkeypatch, tmp_path)
    gtfs_dir = Path(Config.GTFS_DATA_DIR)
    gtfs_dir.mkdir(parents=True)

    (gtfs_dir / "shapes.txt").write_text(
        "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
        "S1,-23.5500,-46.6300,1\n"
        "S1,-23.5600,-46.6400,2\n",
        encoding="utf-8",
    )
    (gtfs_dir / "trips.txt").write_text(
        "route_id,service_id,trip_id,shape_id,direction_id,trip_headsign\n"
        "2202-10,WD,T1,S1,0,Terminal\n",
        encoding="utf-8",
    )
    (gtfs_dir / "routes.txt").write_text(
        "route_id,route_short_name,route_long_name,route_type\n"
        "2202-10,2202-10,Linha Teste,3\n",
        encoding="utf-8",
    )

    gdf = build_gtfs_dataset(str(gtfs_dir), str(gtfs_dir))
    assert len(gdf) == 1

    local_entries = _read_jsonl(gtfs_dir / "provenance.jsonl")
    global_entries = _read_jsonl(data_dir / "provenance.jsonl")
    assert len(local_entries) == 1
    assert len(global_entries) == 1
    assert local_entries[0]["stage"] == "build_gtfs_dataset"
    assert local_entries[0]["output"] == "routes_geometry.geojson"
    assert local_entries[0]["records"] == 1
    assert local_entries[0]["aux_files_loaded"] is False


def test_get_sp_stations_records_provenance_without_leaking_secrets(monkeypatch, tmp_path):
    data_dir = _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setattr(Config, "CEMADEN_EMAIL", "secret_user@example.com")
    monkeypatch.setattr(Config, "CEMADEN_PASSWORD", "SUPER_SECRET_CEMADEN_PW")

    class FakeCemadenClient:
        station_url = "https://user:pw@cemaden.example.com/estacoes?token=SECRET_URL_TOKEN"

        def get_cities(self, uf="SP"):
            return [{"cidade": "SÃO PAULO", "codibge": "3550308"}], 0.1, 1

        def get_stations_by_city(self, ibge_code):
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return [
                {
                    "nome": "Estação Ativa",
                    "uf": "SP",
                    "latitude": -23.55,
                    "longitude": -46.63,
                    "dh_ultima_remessa": now_str,
                    "data_instalacao": "2020-01-01",
                    "codestacao": "355030801A",
                    "tipoestacao_descricao": "Pluviométrica",
                },
                {
                    "nome": "Estação Offline",
                    "uf": "SP",
                    "latitude": -23.56,
                    "longitude": -46.64,
                    "dh_ultima_remessa": "2020-01-01 00:00:00",
                    "data_instalacao": "2018-01-01",
                    "codestacao": "355030802A",
                    "tipoestacao_descricao": "Pluviométrica",
                },
            ], 0.1, 1

    stations_dir = Path(Config.STATIONS_DATA_DIR)
    stations = get_sp_stations(output_dir=str(stations_dir), client=FakeCemadenClient())
    assert len(stations) == 2

    prov_text = (stations_dir / "provenance.jsonl").read_text(encoding="utf-8")
    assert "SUPER_SECRET_CEMADEN_PW" not in prov_text
    assert "secret_user@example.com" not in prov_text
    assert "SECRET_URL_TOKEN" not in prov_text

    entries = _read_jsonl(stations_dir / "provenance.jsonl")
    assert len(entries) == 1
    assert entries[0]["stage"] == "fetch_cemaden_stations"
    assert entries[0]["source"] == "https://cemaden.example.com/estacoes"
    assert entries[0]["output"] == "cemaden_sp_stations.json"
    assert entries[0]["records"] == 2
    assert entries[0]["active_stations"] == 1
    assert entries[0]["offline_stations"] == 1
    assert len(_read_jsonl(data_dir / "provenance.jsonl")) == 1


def test_select_lines_skip_api_records_provenance(monkeypatch, tmp_path):
    data_dir = _isolate_config(monkeypatch, tmp_path)
    stations_dir = Path(Config.STATIONS_DATA_DIR)
    gtfs_dir = Path(Config.GTFS_DATA_DIR)
    lines_dir = Path(Config.LINES_REFERENCE_DIR)
    stations_dir.mkdir(parents=True)
    gtfs_dir.mkdir(parents=True)
    lines_dir.mkdir(parents=True)

    (stations_dir / "cemaden_sp_stations.json").write_text(
        json.dumps(
            [
                {
                    "station_id": "355030801A",
                    "station_name": "Centro",
                    "status": "Active",
                    "station_type": "Pluviométrica",
                    "latitude": -23.5505,
                    "longitude": -46.6333,
                }
            ]
        ),
        encoding="utf-8",
    )

    routes_gdf = gpd.GeoDataFrame(
        [
            {
                "shape_id": "S0",
                "route_id": "2202-10",
                "route_short_name": "2202-10",
                "route_long_name": "Linha A",
                "direction_id": 0,
                "length_km": 10.0,
                "weekday_trips": 200,
                "peak_headway_min": 5.0,
                "offpeak_headway_min": 8.0,
                "scheduled_speed_kmh": 15.0,
                "scheduled_speed_suspect": False,
                "origin_stop_id": "1",
                "dest_stop_id": "2",
                "geometry": LineString([(-46.635, -23.550), (-46.630, -23.551)]),
            },
            {
                "shape_id": "S1",
                "route_id": "2202-10",
                "route_short_name": "2202-10",
                "route_long_name": "Linha A",
                "direction_id": 1,
                "length_km": 10.0,
                "weekday_trips": 200,
                "peak_headway_min": 5.0,
                "offpeak_headway_min": 8.0,
                "scheduled_speed_kmh": 15.0,
                "scheduled_speed_suspect": False,
                "origin_stop_id": "2",
                "dest_stop_id": "1",
                "geometry": LineString([(-46.630, -23.551), (-46.635, -23.550)]),
            },
        ],
        crs="EPSG:4326",
    )
    routes_gdf.to_file(gtfs_dir / "routes_geometry.geojson", driver="GeoJSON")

    monkeypatch.setattr(sys, "argv", ["select_lines.py", "--skip-api"])
    select_lines.main()

    entries = _read_jsonl(lines_dir / "provenance.jsonl")
    assert len(entries) == 1
    assert entries[0]["stage"] == "select_lines"
    assert entries[0]["output"] == "line_ranking.csv"
    assert entries[0]["records"] == 1
    assert entries[0]["skip_api"] is True
    assert len(_read_jsonl(data_dir / "provenance.jsonl")) == 1


def test_collect_sptrans_records_provenance_without_leaking_token(monkeypatch, tmp_path):
    data_dir = _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setattr(Config, "SPTRANS_TOKEN", "SECRET_SPTRANS_TOKEN_999")
    monkeypatch.setattr(
        Config,
        "SPTRANS_BASE_URL",
        "http://user:pw@api.olhovivo.sptrans.com.br/v2.1?token=SECRET_SPTRANS_TOKEN_999",
    )
    monkeypatch.setattr(Config, "COLLECT_INTER_CALL_DELAY_SECONDS", 0.0)

    lines_path = Path(Config.SELECTED_LINES_PATH)
    lines_path.parent.mkdir(parents=True, exist_ok=True)
    lines_path.write_text(
        json.dumps(
            {
                "stations_needed": ["355030801A"],
                "lines": [
                    {
                        "route_id": "2202-10",
                        "route_long_name": "Linha A",
                        "directions": [{"direction_id": 1, "codigoLinha": 910, "sl": 1}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    class FakeSPTransClient:
        def get_positions(self, codigo_linha: int) -> PositionResponse:
            return PositionResponse(
                payload={
                    "hr": "12:00",
                    "vs": [{"p": "15163", "py": -23.55, "px": -46.63, "ta": "2026-11-20T14:00:00Z"}],
                },
                http_status=200,
                latency_ms=12.5,
                attempts=1,
                relogin=False,
            )

        def close(self) -> None:
            pass

    bronze_dir = Path(Config.SPTRANS_BRONZE_DIR)
    exit_code = collect_sptrans.run_collection(
        interval_s=1,
        lines_file=str(lines_path),
        once=True,
        keep_awake=False,
        client=FakeSPTransClient(),
        base_dir=str(bronze_dir),
    )
    assert exit_code == 0

    prov_text = (bronze_dir / "provenance.jsonl").read_text(encoding="utf-8")
    assert "SECRET_SPTRANS_TOKEN_999" not in prov_text

    entries = _read_jsonl(bronze_dir / "provenance.jsonl")
    assert len(entries) == 1
    assert entries[0]["stage"] == "collect_sptrans_bronze"
    assert entries[0]["source"][0] == "http://api.olhovivo.sptrans.com.br/v2.1/Posicao/Linha"
    assert entries[0]["records"] == 1
    assert entries[0]["cycles"] == 1
    assert entries[0]["stop_reason"] == "once"
    assert len(entries[0]["output"]) == 1
    assert len(_read_jsonl(data_dir / "provenance.jsonl")) == 1


def test_ingest_cemaden_dry_run_and_up_to_date_provenance(monkeypatch, tmp_path):
    data_dir = _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setattr(Config, "CEMADEN_EMAIL", "cemaden_secret@example.com")
    monkeypatch.setattr(Config, "CEMADEN_PASSWORD", "SECRET_PW_XYZ")
    monkeypatch.setattr(
        Config,
        "CEMADEN_SCHEDULE_URL",
        "https://sws.cemaden.gov.br/PED/rest/schedule?token=SECRET_TOKEN",
    )

    lines_path = Path(Config.SELECTED_LINES_PATH)
    lines_path.parent.mkdir(parents=True, exist_ok=True)
    lines_path.write_text(json.dumps({"stations_needed": ["355030802A"], "lines": []}), encoding="utf-8")

    stations_path = Path(Config.STATIONS_DATA_DIR) / "cemaden_sp_stations.json"
    stations_path.parent.mkdir(parents=True, exist_ok=True)
    stations_path.write_text(json.dumps([{"station_id": "355030802A", "uf": "SP"}]), encoding="utf-8")

    cemaden_bronze = Path(Config.CEMADEN_BRONZE_DIR)

    # Dry-run must NOT emit provenance
    assert ingest_cemaden.main(["--start", "2026-01-01", "--end", "2026-01-01", "--dry-run"]) == 0
    assert not (cemaden_bronze / "provenance.jsonl").exists()
    assert not (data_dir / "provenance.jsonl").exists()

    # Pre-seed manifest so chunk is already covered
    manifest_path = Path(Config.CEMADEN_MANIFEST_PATH)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "ts": "2026-01-02T00:00:00+00:00",
                "station_id": "355030802A",
                "job_id": 1,
                "status": "ok",
                "start_str": "202601010000",
                "end_str": "202601012359",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert ingest_cemaden.main(["--start", "2026-01-01", "--end", "2026-01-01"]) == 0
    prov_text = (cemaden_bronze / "provenance.jsonl").read_text(encoding="utf-8")
    assert "SECRET_TOKEN" not in prov_text
    assert "SECRET_PW_XYZ" not in prov_text

    entries = _read_jsonl(cemaden_bronze / "provenance.jsonl")
    assert len(entries) == 1
    assert entries[0]["stage"] == "ingest_cemaden_bronze"
    assert entries[0]["status"] == "up_to_date"
    assert entries[0]["output"] == "none"
    assert entries[0]["records"] == 0
    assert entries[0]["date_range"] == ["2026-01-01", "2026-01-01"]
    assert len(_read_jsonl(data_dir / "provenance.jsonl")) == 1


def test_ingest_cemaden_download_records_provenance(monkeypatch, tmp_path):
    data_dir = _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setattr(Config, "CEMADEN_POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(Config, "CEMADEN_SCHEDULE_URL", "https://sws.cemaden.gov.br/PED/rest/schedule")

    lines_path = Path(Config.SELECTED_LINES_PATH)
    lines_path.parent.mkdir(parents=True, exist_ok=True)
    lines_path.write_text(json.dumps({"stations_needed": ["355030802A"], "lines": []}), encoding="utf-8")

    stations_path = Path(Config.STATIONS_DATA_DIR) / "cemaden_sp_stations.json"
    stations_path.parent.mkdir(parents=True, exist_ok=True)
    stations_path.write_text(json.dumps([{"station_id": "355030802A", "uf": "SP"}]), encoding="utf-8")

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        zf.writestr("dados.csv", "datahora,valor\n2026-01-01 00:10:00,2.5\n2026-01-01 01:10:00,0.0\n")
    zip_bytes = zip_buf.getvalue()

    class FakeCemadenClient:
        def schedule(self, station_id, uf, start_dt, end_dt):
            return 42, 0.1, 1

        def list_jobs(self):
            return [{"id": 42, "status": {"description": "CONCLUIDA"}, "link": "http://dl/42.zip"}], 0.1, 1

        def download(self, link):
            return zip_bytes, 0.1, 1

    monkeypatch.setattr(ingest_cemaden, "CemadenClient", FakeCemadenClient)
    monkeypatch.setattr(ingest_cemaden.time, "sleep", lambda _: None)

    assert ingest_cemaden.main(["--start", "2026-01-01", "--end", "2026-01-01"]) == 0

    cemaden_bronze = Path(Config.CEMADEN_BRONZE_DIR)
    entries = _read_jsonl(cemaden_bronze / "provenance.jsonl")
    assert len(entries) == 1
    assert entries[0]["stage"] == "ingest_cemaden_bronze"
    assert entries[0]["status"] == "completed"
    assert entries[0]["records"] == 2
    assert entries[0]["files_downloaded"] == 1
    assert entries[0]["output"] == ["station=355030802A/req_01012026_01012026.csv"]
    assert len(_read_jsonl(data_dir / "provenance.jsonl")) == 1


def test_build_sptrans_silver_records_provenance(monkeypatch, tmp_path):
    data_dir = _isolate_config(monkeypatch, tmp_path)

    bronze_dir = Path(Config.SPTRANS_BRONZE_DIR)
    part_dir = bronze_dir / "date=2026-11-20" / "hour=14"
    part_dir.mkdir(parents=True)
    (bronze_dir / "provenance.jsonl").write_text('{"stage": "collect_sptrans_bronze"}\n', encoding="utf-8")

    rec1 = {
        "envelope": {
            "collected_at_utc": "2026-11-20T14:00:10Z",
            "codigoLinha": 910,
            "route_id": "2202-10",
            "direction_id": 1,
        },
        "payload": {"vs": [{"p": "15163", "py": -23.5500, "px": -46.6300, "ta": "2026-11-20T14:00:00Z"}]},
    }
    rec2 = {
        "envelope": {
            "collected_at_utc": "2026-11-20T14:01:10Z",
            "codigoLinha": 910,
            "route_id": "2202-10",
            "direction_id": 1,
        },
        "payload": {"vs": [{"p": "15163", "py": -23.5520, "px": -46.6300, "ta": "2026-11-20T14:01:00Z"}]},
    }
    (part_dir / "posicao_910_20261120T140010000000Z.json").write_text(json.dumps(rec1), encoding="utf-8")
    (part_dir / "posicao_910_20261120T140110000000Z.json").write_text(json.dumps(rec2), encoding="utf-8")

    mapping_path = Path(Config.LINE_STATION_MAPPING_PATH)
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {
                "route_id": "2202-10",
                "direction_id": 1,
                "codigoLinha": 910,
                "lat": -23.551,
                "lon": -46.630,
                "station_id": "355030801A",
                "dist_to_station_m": 100.0,
            }
        ]
    ).to_parquet(mapping_path, index=False)

    assert build_sptrans_silver.run_pipeline() == 0

    silver_dir = Path(Config.SPTRANS_SILVER_DIR)
    assert (silver_dir / "sptrans_silver_export.csv").exists()
    entries = _read_jsonl(silver_dir / "provenance.jsonl")
    assert len(entries) == 1
    assert entries[0]["stage"] == "build_sptrans_silver"
    assert entries[0]["output"] == ["sptrans_silver_export.csv", "sptrans_silver_export.parquet"]
    assert entries[0]["records"] == 2
    assert entries[0]["bronze_files_read"] == 2
    assert entries[0]["raw_readings"] == 2
    assert entries[0]["speed_flags"]["first_reading"] == 1
    assert entries[0]["speed_flags"]["ok"] == 1
    assert len(_read_jsonl(data_dir / "provenance.jsonl")) == 1


def test_build_cemaden_silver_records_provenance(monkeypatch, tmp_path):
    data_dir = _isolate_config(monkeypatch, tmp_path)

    lines_path = Path(Config.SELECTED_LINES_PATH)
    lines_path.parent.mkdir(parents=True, exist_ok=True)
    lines_path.write_text(json.dumps({"stations_needed": ["355030802A"], "lines": []}), encoding="utf-8")

    st_dir = Path(Config.CEMADEN_BRONZE_DIR) / "station=355030802A"
    st_dir.mkdir(parents=True)
    (st_dir / "req_01012026_01012026.csv").write_text(
        "datahora,valor\n"
        "2026-01-01 00:10:00,1.2\n"
        "2026-01-01 02:10:00,-999\n",
        encoding="utf-8",
    )

    assert build_cemaden_silver.main([]) == 0

    silver_dir = Path(Config.CEMADEN_SILVER_DIR)
    assert (silver_dir / "rain_silver_export.csv").exists()
    entries = _read_jsonl(silver_dir / "provenance.jsonl")
    assert len(entries) == 1
    assert entries[0]["stage"] == "build_cemaden_silver"
    assert entries[0]["output"] == ["rain_silver_export.csv", "rain_silver_export.parquet"]
    assert entries[0]["records"] == 3
    assert entries[0]["bronze_files_read"] == 1
    assert entries[0]["stations_count"] == 1
    assert entries[0]["real_rows"] == 2
    assert entries[0]["missing_rows"] == 1
    assert len(_read_jsonl(data_dir / "provenance.jsonl")) == 1
