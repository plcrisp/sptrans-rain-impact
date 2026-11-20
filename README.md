# 🚌 SPTrans Rain Impact Pipeline 🌧️

Pipeline de engenharia de dados ponta a ponta projetado para medir e analisar de forma precisa **como e em que intensidade a chuva reduz a velocidade operacional dos ônibus na cidade de São Paulo**.

---

## 🎯 Proposta do Projeto

> **"A precipitação reduz a velocidade dos ônibus urbanos, e essa perda de velocidade é diretamente proporcional à intensidade da chuva no local por onde o veículo trafega."**

Para responder a essa pergunta, o projeto combina:

1. **Telemetria de transporte público (SPTrans)**: Posição de GPS minuto a minuto de ônibus em circulação e malha viária oficial.
2. **Dados meteorológicos em tempo real (CEMADEN)**: Séries temporais de pluviômetros espalhados pela capital paulista.
3. **Engenharia de Dados**: Separação clara entre dados de **Referência**, dados brutos (**Bronze**), métricas limpas (**Silver**) e análise (**Gold**).

---

## 🔌 Fontes de Dados

- **[SPTrans Olho Vivo API](https://www.sptrans.com.br/desenvolvedores/)**: Coleta de telemetria GPS em tempo real dos veículos em operação e feeds estáticos em formato GTFS (linhas, itinerários, paradas e horários).
- **[CEMADEN PED](https://www.gov.br/cemaden/)**: Rede oficial de monitoramento de desastres com estações pluviométricas automáticas que medem o volume de chuva a cada 10 minutos ou 1 hora.

---

## 🏗️ Arquitetura dos Dados

A pasta `data/` é organizada da seguinte maneira:

```text
data/
├── provenance.jsonl          # 🧾 Histórico consolidado de proveniência das execuções (em inglês, ignorado no Git)
│
├── reference/                # 🗺️ Dados estáticos de apoio (gerados uma única vez)
│   ├── gtfs/                 # Feeds GTFS e geometria das rotas (routes_geometry.geojson)
│   ├── stations/             # Catálogo de estações de SP (cemaden_sp_stations.json)
│   └── lines/                # Linhas selecionadas e malha espacial (line_station_mapping.parquet)
│
├── bronze/                   # 📥 Dados brutos ingeridos diretamente das APIs
│   ├── sptrans/              # Posições de GPS brutas particionadas por date=YYYY-MM-DD/hour=HH/*.json
│   └── cemaden/              # Leituras de chuva brutas por estação (station=ID/req_inicio_fim.csv)
│
├── silver/                   # 🧹 Dados limpos, padronizados e prontos para análise
│   ├── sptrans/              # sptrans_silver_export.{parquet,csv} (velocidade calculada, sentido e estação)
│   ├── cemaden/              # rain_silver_export.{parquet,csv} (chuva em mm com marcação de falhas)
│   └── sptrans_rain_enriched.parquet  # Cruzamento Silver (ônibus + chuva horária + métricas derivadas)
│
└── gold/                     # 📊 Camada Analítica final (DuckDB)
    └── project.duckdb        # Banco embutido com schemas silver e gold (Esquema Estrela)
```

> Cada subpasta de dados (`reference/gtfs`, `reference/stations`, `reference/lines`, `bronze/sptrans`, `bronze/cemaden`, `silver/sptrans`, `silver/cemaden`) também mantém seu próprio arquivo incremental `provenance.jsonl` (em inglês, ignorado no Git) registrando origem, saídas, contagem de registros e timestamp UTC de cada execução.

---

## 🔄 Fluxo do Pipeline (Passo a Passo)

O pipeline divide-se em 4 etapas lógicas e intuitivas:

### 1. Etapa de Referência (`data/reference/`) — _Setup_

- **GTFS**: Processa itinerários (`shapes`), viagens e paradas, calculando o comprimento e a velocidade programada de cada traçado.
- **Catálogo Cemaden**: Mapeia todas as estações pluviométricas ativas na capital paulista com suas coordenadas geográficas.
- **Seleção das Linhas**: Ranqueia as melhores rotas combinando:
  - **Cobertura Pluviométrica (50%)**: Proximidade contínua a pluviômetros ao longo do percurso.
  - **Frequência de Viagens (30%)**: Alto volume de ônibus em circulação.
  - **Extensão Ideal (20%)**: Faixa de comprimento ótimo.
  - **Filtro de Headway no Pico**: Descarta rotas com intervalos muito longos entre ônibus.

### 2. Ingestão Bronze (`data/bronze/`) — _Dados Brutos_

- **SPTrans**: Um coletor contínuo bate na API a cada 60 segundos consultando as linhas selecionadas. Grava apenas quando há atualização de posição dos veículos, organizando em partições temporais.
- **CEMADEN**: Para a mesma janela de datas da coleta dos ônibus, faz o download do histórico bruto de precipitação das estações mapeadas.

### 3. Tratamento Silver (`data/silver/`) — _Métricas Reais_

- **SPTrans Silver (`sptrans_silver_export.csv`)**:
  - Filtra ruídos de GPS, coordenadas fora do município e leituras duplicadas.
  - Projeta os ônibus na geometria da rota e calcula a velocidade instantânea observada (`speed_kmh`) entre leituras sucessivas do mesmo veículo.
  - Vincula o sentido da viagem (`direction_id`) e a distância até a estação pluviométrica correspondente (`dist_to_station_m`).
- **CEMADEN Silver (`rain_silver_export.parquet` / `.csv`)**:
  - Padroniza os fusos horários para o horário local de São Paulo (`America/Sao_Paulo`).
  - Preserva os minutos reais das leituras (10 min ou 1h) e arredonda os valores de milímetros.
  - Detecta janelas sem dados da estação e insere uma linha sintética indicando falha de comunicação (`is_missing = True`).
- **Enriquecimento Silver (`sptrans_rain_enriched.parquet`)**:
  - Agrega o CEMADEN para o grão único `(station_id, hour_sp)` (acumulado horário em mm/h) para evitar multiplicação de linhas no `merge`.
  - Faz a junção com `how="left"` e `validate="many_to_one"` partindo da SPTrans Silver (mantendo todas as leituras de ônibus).
  - Gera atributos derivados pós-junção: `rain_intensity` (`dry`, `light`, `moderate`, `heavy`, `unknown`), `is_raining`, `dry_baseline_speed_kmh` e `speed_diff_from_dry_avg`.
  - **Motivos de leituras sem chuva pareada (`rain_intensity = "unknown"`):** ocorrem quando a janela temporal da SPTrans ainda não foi ingerida no CEMADEN ou quando a estação apresentou falha de comunicação naquela hora (`is_missing = True`).

### 4. Camada Ouro / Gold (`data/gold/project.duckdb`) — _Esquema Estrela_

Modelagem dimensional carregada em **DuckDB** (`silver.*` → `gold.*`):
- **`gold.fact_bus_readings`** *(grão: 1 leitura válida de veículo em um instante — `vehicle_id`, `route_code`, `observed_at`)*: métricas de velocidade (`speed_kmh`, `speed_diff_from_dry_avg`), distância até a estação (`distance_to_station_m`) e chuva (`precipitation_mm`, `rain_intensity`, `is_raining`).
- **`gold.dim_lines`** *(1 linha por `route_code` / sentido)*: código, nome da linha (`route_name`), `direction_id` e sentido operacional.
- **`gold.dim_stations`** *(1 linha por `station_id`)*: nome da estação CEMADEN, cidade, UF, coordenadas e status.
- **`gold.dim_time`** *(1 linha por `hour_key`)*: data, hora, dia da semana, `is_weekend` e `is_peak`.
- **`gold.vw_bus_rain_impact`**: view analítica juntando fato e dimensões como fonte única de verdade para consumo.

---

## 🚀 Como Executar

### Execução pelo Orquestrador Único (`src/pipeline.py`)

Depois de coletar dados na Bronze da SPTrans (`src/pipelines/bronze/collect_sptrans.py`), todo o fluxo em lote (CEMADEN Bronze → Silver SPTrans → Silver CEMADEN → Enriquecimento Silver → DuckDB → dbt run & test) roda por um único comando e grava logs no console e em `pipeline.log`:

```bash
# Rodar todas as etapas prontas do início ao fim
python src/pipeline.py

# Retomar a partir de uma etapa específica (ex.: sem bater nas APIs)
python src/pipeline.py silver_enrichment

# Listar etapas declaradas
python src/pipeline.py --list
```

### Dashboard Interativo (Streamlit) e Documentação dbt

```bash
# Abrir o painel consumidor (leitura somente leitura em data/gold/project.duckdb)
streamlit run dashboard/app.py

# Gerar e visualizar a documentação e grafo de linhagem do dbt
cd sptrans_rain_dbt
dbt docs generate --profiles-dir .
dbt docs serve --profiles-dir .
```

### 1. Pré-requisitos e Instalação

Clone o repositório e configure o ambiente Python (recomendado Python 3.11+):

```bash
git clone https://github.com/plcrisp/sptrans-rain-impact.git
cd sptrans-rain-impact

python -m venv venv
# No Windows:
.\venv\Scripts\activate
# No Linux/Mac:
source venv/bin/activate

pip install -r requirements.txt
```

### 2. Configuração de Variáveis de Ambiente

Crie um arquivo `.env` na raiz do projeto baseado no `.env.example`.

---

## 📓 Execução pelos Notebooks (Recomendado)

O projeto conta com notebooks interativos numerados na pasta `notebooks/`, cobrindo o fluxo do início ao fim:

| Notebook                          | Objetivo                                                               | O que gera                                             |
| :-------------------------------- | :--------------------------------------------------------------------- | :----------------------------------------------------- |
| **`00_lines_and_stations.ipynb`** | Extrai a geometria GTFS e cataloga os pluviômetros de SP.              | `data/reference/gtfs/` e `cemaden_sp_stations.json`    |
| **`01_line_selection.ipynb`**     | Ranqueia as linhas candidatas e gera o mapeamento espacial.            | `selected_lines.json` e `line_station_mapping.parquet` |
| **`02_sptrans_pipeline.ipynb`**   | Dispara a coleta em tempo real e processa o dataset Silver da SPTrans. | `data/bronze/sptrans/` e `sptrans_silver_export.csv`   |
| **`03_cemaden_pipeline.ipynb`**   | Coleta os dados históricos de chuva e gera a Silver tratada.           | `data/bronze/cemaden/` e `rain_silver_export.csv`      |

---

## 📁 Estrutura do Código (`sptrans_rain_dbt/          # Modelos SQL declarativos (dbt-duckdb) da camada Gold
├── models/
│   ├── sources.yml        # Declaração das tabelas Silver no DuckDB
│   ├── schema.yml         # Documentação e testes declarativos (not_null, unique, relationships)
│   ├── dim_lines.sql      # Dimensão de linhas
│   ├── dim_stations.sql   # Dimensão de estações pluviométricas
│   ├── dim_time.sql       # Dimensão temporal (hora/pico/fim de semana)
│   ├── fact_bus_readings.sql
│   └── vw_bus_rain_impact.sql
├── tests/                 # Testes SQL customizados de grão composto e invariantes
└── profiles.yml           # Perfil local apontando para data/gold/project.duckdb

dashboard/                 # Painel consumidor em Streamlit (somente leitura na Gold)
├── queries.py             # Consultas DuckDB parametrizadas (read_only=True)
└── app.py                 # Interface visual sem SQL

src/`)

```text
sptrans_rain_dbt/          # Modelos SQL declarativos (dbt-duckdb) da camada Gold
├── models/
│   ├── sources.yml        # Declaração das tabelas Silver no DuckDB
│   ├── schema.yml         # Documentação e testes declarativos (not_null, unique, relationships)
│   ├── dim_lines.sql      # Dimensão de linhas
│   ├── dim_stations.sql   # Dimensão de estações pluviométricas
│   ├── dim_time.sql       # Dimensão temporal (hora/pico/fim de semana)
│   ├── fact_bus_readings.sql
│   └── vw_bus_rain_impact.sql
├── tests/                 # Testes SQL customizados de grão composto e invariantes
└── profiles.yml           # Perfil local apontando para data/gold/project.duckdb

dashboard/                 # Painel consumidor em Streamlit (somente leitura na Gold)
├── queries.py             # Consultas DuckDB parametrizadas (read_only=True)
└── app.py                 # Interface visual sem SQL

src/
├── pipeline.py           # Orquestrador único do pipeline em lote (com retomada por etapa)
├── clients/              # Clientes HTTP com retry e autenticação (SPTrans e CEMADEN)
├── core/                 # Configurações centralizadas e leitura do .env (config.py)
├── pipelines/            # Scripts de execução das camadas
│   ├── bronze/           # Ingestão de dados brutos (select_lines, collect_sptrans, ingest_cemaden)
│   ├── silver/           # Transformação, limpeza e enriquecimento (build_sptrans_silver, build_cemaden_silver, enrich_sptrans_rain)
│   └── gold/             # Carga no DuckDB (load_duckdb) e execução declarativa dbt (run_dbt)
└── utils/                # Módulos especializados de apoio
    ├── cemaden_bronze.py # Manipulação de arquivos brutos e requisições da chuva
    ├── cemaden_silver.py # Limpeza, arredondamento e detecção de falhas da chuva
    ├── gtfs_parser.py    # Processamento de arquivos estáticos de transporte
    ├── line_selection.py # Amostragem espacial, métricas e ranking de linhas
    ├── logger.py         # Configuração de logging estruturado
    ├── provenance.py     # Registro incremental de proveniência (provenance.jsonl)
    ├── silver_enrichment.py # Agregação horária de chuva e junção SPTrans x CEMADEN
    ├── sptrans_silver.py # Projeção geográfica e cálculo de velocidade de ônibus
    └── station_parser.py # Normalização das estações
```

---

## 📄 Licença

Projeto desenvolvido para fins acadêmicos e de pesquisa na **Universidade Federal de Itajubá (UNIFEI)**.
