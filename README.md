# 🚌 SPTrans Rain Impact

Pipeline de engenharia de dados ponta a ponta (**Bronze → Silver → Gold → Dashboard**) para responder:

> **"A precipitação reduz a velocidade dos ônibus em São Paulo?"**

### 📊 Principais resultados (`20/11/2026` a `25/11/2026`)
- **Todos os horários:** queda média de **-0,54 km/h (-3,8%)**, com **8 de 8 linhas (100%)** mais lentas na chuva.
- **Horário de pico (06h–09h, 17h–20h):** queda média de **-0,84 km/h (-6,4%)**, chegando a **-13,8%** na linha `4734-10` e **-10,6%** na `1705-10`.
- **Base validada:** `638.142` leituras na camada Gold e **31/31 testes dbt** passando.

---

## 🔌 Fontes de dados

| Fonte | Formato (Bronze) | Acesso | Chave natural | Link |
| :--- | :--- | :--- | :--- | :--- |
| **SPTrans Olho Vivo** | JSON (`date=YYYY-MM-DD/hour=HH`) | API REST (`.env`) | `(vehicle_id, codigoLinha, timestamp_sp)` | [SPTrans](https://www.sptrans.com.br/desenvolvedores/) |
| **SPTrans GTFS** | ZIP / CSVs estáticos | `data/reference/gtfs/` | `route_id` / `shape_id` | [SPTrans](https://www.sptrans.com.br/desenvolvedores/) |
| **CEMADEN PED** | CSV (`station=ID/req_*.csv`) | API REST pública | `(station_id, timestamp_sp)` | [CEMADEN](https://www.gov.br/cemaden/) |

Cada etapa grava proveniência incremental em `provenance.jsonl` (versionado no Git).

---

## 🚀 Como executar

```bash
# 1. Ambiente e dependências
git clone https://github.com/plcrisp/sptrans-rain-impact.git
cd sptrans-rain-impact
python -m venv venv
.\venv\Scripts\activate          # Linux/Mac: source venv/bin/activate
pip install -r requirements.txt
# Crie .env a partir de .env.example com seu token SPTrans

# 2. Coleta contínua (Bronze SPTrans) e pipeline em lote (Silver -> Gold -> dbt)
python src/pipelines/bronze/collect_sptrans.py
python src/pipeline.py           # ou retome de uma etapa: python src/pipeline.py sptrans_silver

# 3. Dashboard interativo (somente leitura na Gold)
streamlit run dashboard/app.py

# 4. Documentação e linhagem dbt
cd sptrans_rain_dbt && dbt docs generate --profiles-dir . && dbt docs serve --profiles-dir .
```

---

## 🔍 Defeitos conhecidos das fontes

- **SPTrans Olho Vivo:** leituras duplicadas em ciclos seguidos, timestamps fora da janela, veículos parados em terminal e saltos de GPS acima de 80 km/h.
- **CEMADEN PED:** cadência variável (10 min com chuva, 1h no seco), janelas sem transmissão (sem enviar `0.0 mm`) e timestamps em UTC.

---

## 🧹 Decisões de tratamento

Carga consolidada em `2026-11-26T03:37:16Z` (`106.433` JSONs SPTrans · `76` CSVs CEMADEN):

| Camada / Regra | Volume | Decisão |
| :--- | :--- | :--- |
| **Deduplicação SPTrans** | `645.052` → `638.142` | Removidas `5.945` duplicadas e `965` timestamps inválidos (`0` coordenadas inválidas). |
| **Sinalização (`speed_flag`)** | `452.180` válidas (`ok`) | Mantidas e marcadas: `164.666` `parked`, `16.680` `dt_out_of_range`, `4.037` `too_fast`, `579` `first_reading`. Só `ok` entra nas médias. |
| **Falhas CEMADEN** | `7.725` linhas (`19` estações) | `7.162` leituras reais + `563` marcações explícitas (`is_missing = True`) para separar falha de sensor de `0 mm`. |

---

## 🔗 Junção (Enriquecimento Silver)

- **Chave e tipo:** `(station_id, hour_sp)` via `how="left"` e `validate="many_to_one"` (CEMADEN agregado por hora).
- **Integridade:** `638.142` linhas antes → `638.142` linhas depois (`0` duplicações · `0` leituras sem chave).
- **Cobertura:** `543.494` leituras com chuva válida (`430.370` seco · `113.124` chuva) e `94.648` com falha de sensor (`unknown`).

---

## 🧮 Atributos derivados

| Atributo | Descrição | Quando fica nulo / ausente |
| :--- | :--- | :--- |
| `speed_kmh` | Velocidade (km/h) entre leituras sucessivas na rota GTFS. | Se `speed_flag != 'ok'`. |
| `is_raining` | Booleano (`rain_mm > 0`) para comparação Seco vs. Chuva. | Se `rain_mm` ausente. |
| `rain_intensity` | Faixa horária (`dry`, `light`, `moderate`, `heavy`). | `'unknown'` se `rain_mm` ausente. |
| `dry_baseline_speed_kmh` | Média no seco para a mesma linha, sentido, estação, tipo de dia e hora exata. | Se não houver leitura seca `ok` naquela hora/trecho. |
| `speed_diff_from_dry_avg` | `speed_kmh - dry_baseline_speed_kmh` (negativo = mais lento na chuva). | Se `speed_flag != 'ok'` ou sem baseline exato. |

---

## ⭐ Modelo dimensional (Camada Gold)

Esquema estrela em **DuckDB** (`data/gold/project.duckdb`) via **dbt Core**, validado por **31 testes automatizados** (`dbt test`):

| Modelo | Grão | Linhas |
| :--- | :--- | ---: |
| `gold.fact_bus_readings` | 1 leitura de GPS `(vehicle_id, route_code, observed_at)` | `638.142` |
| `gold.dim_lines` | 1 linha por rota/sentido (`route_code`) | `16` |
| `gold.dim_stations` | 1 estação pluviométrica CEMADEN (`station_id`) | `79` |
| `gold.dim_time` | 1 hora local SP (`hour_key`, pico, fim de semana) | `128` |
| `gold.vw_bus_rain_impact` | View analítica única consumida pelo dashboard | `638.142` |

---

## 📁 Estrutura do projeto

```text
├── data/               # reference/, bronze/, silver/, gold/project.duckdb e provenance.jsonl
├── src/                # pipeline.py (orquestrador), clients/, core/, pipelines/ e utils/
├── sptrans_rain_dbt/   # Modelos SQL Gold, schema.yml e testes dbt
├── dashboard/          # queries.py (SQL parametrizado read_only) e app.py (Streamlit)
└── requirements.txt
```

---

Projeto acadêmico desenvolvido na **Universidade Federal de Itajubá (UNIFEI)** — disciplina ECOX14.
