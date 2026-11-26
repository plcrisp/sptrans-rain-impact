# SPTrans Rain Impact

Pipeline de engenharia de dados ponta a ponta (Bronze → Silver → Gold → Dashboard) para responder à pergunta:

> **"A precipitação reduz a velocidade dos ônibus em São Paulo, e essa perda aumenta conforme a intensidade da chuva?"**

---

## Fontes de dados

| Fonte                 | Formato (Bronze)                    | Acesso                               | Chave natural                             | Link                                                   |
| :-------------------- | :---------------------------------- | :----------------------------------- | :---------------------------------------- | :----------------------------------------------------- |
| **SPTrans Olho Vivo** | JSON (`date=YYYY-MM-DD/hour=HH`)    | API REST com token (`.env`)          | `(vehicle_id, codigoLinha, timestamp_sp)` | [SPTrans](https://www.sptrans.com.br/desenvolvedores/) |
| **SPTrans GTFS**      | ZIP / CSVs estáticos                | Versionado em `data/reference/gtfs/` | `route_id` / `shape_id`                   | [SPTrans](https://www.sptrans.com.br/desenvolvedores/) |
| **CEMADEN PED**       | CSV (`station=ID/req_*.csv`) + JSON | API REST pública                     | `(station_id, timestamp_sp)`              | [CEMADEN](https://www.gov.br/cemaden/)                 |

Cada etapa registra proveniência incremental em `provenance.jsonl`, guardando origem, saídas, contagens e timestamp UTC.

---

## Como executar

### 1. Instalação e configuração

```bash
git clone https://github.com/plcrisp/sptrans-rain-impact.git
cd sptrans-rain-impact

python -m venv venv
.\venv\Scripts\activate          # Windows (Linux/Mac: source venv/bin/activate)
pip install -r requirements.txt
```

Crie um arquivo `.env` na raiz a partir de `.env.example` com o token da API Olho Vivo.

### 2. Rodar o pipeline

```bash
# Coleta contínua de GPS da SPTrans (Camada Bronze)
python src/pipelines/bronze/collect_sptrans.py

# Pipeline completo em lote: CEMADEN Bronze -> Silver -> Enriquecimento -> DuckDB -> dbt
python src/pipeline.py

# Retomar a partir de uma etapa específica (sem chamar APIs novamente)
python src/pipeline.py silver_enrichment
```

### 3. Abrir o painel e a documentação dbt

```bash
# Dashboard interativo (somente leitura na camada Gold)
streamlit run dashboard/app.py

# Documentação e grafo de linhagem dos modelos Gold
cd sptrans_rain_dbt
dbt docs generate --profiles-dir .
dbt docs serve --profiles-dir .
```

---

## Defeitos conhecidos das fontes

- **SPTrans Olho Vivo:** leituras duplicadas em ciclos consecutivos; timestamps ocasionais fora da janela (`stale_or_future`); veículos parados em terminais (`parked`) e saltos de GPS acima de 80 km/h (`too_fast`).
- **CEMADEN PED:** cadência variável (10 min com chuva, 1h no seco); janelas sem transmissão de dados (sem enviar `0.0 mm`); timestamps em UTC exigindo conversão para `America/Sao_Paulo`.

---

## Decisões de tratamento

Números referentes à carga consolidada em `2026-11-22T15:46:49Z` (38.813 JSONs da SPTrans e 76 CSVs do CEMADEN):

- **Deduplicação e filtros na SPTrans:** de `186.137` leituras brutas, `1.069` duplicadas na chave natural e `3` timestamps inválidos foram removidos, resultando em **`185.065`** registros na Silver (`0` coordenadas inválidas).
- **Sinalização de velocidade (`speed_flag`):** em vez de excluir extremos silenciosamente, todas as leituras são mantidas e classificadas — `123.860` válidas (`ok`), `54.202` parados (`parked`), `5.475` intervalo inválido (`dt_out_of_range`), `1.076` acima de 80 km/h (`too_fast`) e `452` primeiras leituras (`first_reading`). Apenas `ok` entra nas médias.
- **Falhas de pluviômetro no CEMADEN:** dos `27.005` registros na Silver (`19` estações), `4.923` são leituras reais e `22.082` são marcações explícitas de ausência (`is_missing = True`) para não confundir falta de sinal com `0 mm`.

---

## Junção

- **Chave e tipo:** `merge(..., on=["station_id", "hour_sp"], how="left", validate="many_to_one")` entre SPTrans Silver (`185.065` linhas) e CEMADEN Silver agregado por hora (`24.871` chaves únicas).
- **Validação:** `185.065` linhas antes → `185.065` linhas depois (`0` duplicações).
- **Cobertura:** `927` chaves casadas e `0` leituras sem chave (`unmatched_key_rows = 0`). Das `185.065` leituras, `154.082` têm chuva válida (`dry`: `146.324`, `light`: `6.700`, `moderate`: `665`, `heavy`: `393`) e `30.983` coincidem com falha de sensor (`rain_is_missing = True` → `rain_intensity = 'unknown'`).

---

## Atributos derivados

| Atributo                  | O que é                                                                                            | Papel na análise                                              | Quando não calcula                            |
| :------------------------ | :------------------------------------------------------------------------------------------------- | :------------------------------------------------------------ | :-------------------------------------------- |
| `speed_kmh`               | Velocidade (km/h) entre leituras sucessivas projetadas na rota GTFS.                               | Variável principal de desempenho.                             | Nulo se `speed_flag != 'ok'`.                 |
| `rain_intensity`          | Faixa horária (`dry`, `light <= 2,5`, `moderate <= 10`, `heavy > 10` mm/h).                        | Testa perda de velocidade por intensidade.                    | `'unknown'` se `rain_mm` ausente.             |
| `is_raining`              | Booleano (`rain_mm > 0`).                                                                          | Comparação direta Seco vs. Chuva.                             | Nulo se `rain_mm` ausente.                    |
| `dry_baseline_speed_kmh`  | Média no seco para a mesma linha, sentido, estação, tipo de dia e hora (com fallback hierárquico). | Isola o efeito da chuva do trânsito típico do horário/trecho. | Nulo se não houver leitura seca `ok`.         |
| `speed_diff_from_dry_avg` | `speed_kmh - dry_baseline_speed_kmh` (km/h).                                                       | Mede o impacto direto da chuva (negativo = mais lento).       | Nulo se `speed_flag != 'ok'` ou sem baseline. |

---

## Modelo dimensional (Camada Gold)

Construído em **DuckDB** (`data/gold/project.duckdb`) via **dbt Core** em esquema estrela:

- **`gold.fact_bus_readings`** _(185.065 linhas — grão: `vehicle_id`, `route_code`, `observed_at`)_: velocidade, flag, distância até a estação, chuva e impacto vs. seco.
- **`gold.dim_lines`** _(16 linhas)_: linhas selecionadas e sentidos operacionais.
- **`gold.dim_stations`** _(79 linhas)_: catálogo de estações CEMADEN em São Paulo.
- **`gold.dim_time`** _(52 linhas)_: dimensão horária local (`date_sp`, `hour_of_day`, `day_of_week`, `is_weekend`, `is_peak`).
- **`gold.vw_bus_rain_impact`** _(VIEW — 185.065 linhas)_: visão analítica única consumida pelo painel.

Qualidade validada automaticamente por **31 testes no `dbt test`** (`not_null`, `unique`, `relationships`, `accepted_values`, unicidade do grão composto e faixa `[0, 80]` km/h).

---

## Estrutura do projeto

```text
├── data/               # reference/, bronze/, silver/, gold/project.duckdb e provenance.jsonl
├── src/                # pipeline.py (orquestrador), clients/, core/, pipelines/ e utils/
├── sptrans_rain_dbt/   # Modelos SQL da camada Gold, schema.yml e testes dbt
├── dashboard/          # queries.py (SQL parametrizado read_only) e app.py (Streamlit)
└── requirements.txt
```

---

Projeto acadêmico desenvolvido na **Universidade Federal de Itajubá (UNIFEI)** — disciplina ECOX14.
