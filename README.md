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

| Fonte                 | Formato (Bronze)                                          | Acesso                                                 | Chave Natural                             | Link                                                                   |
| :-------------------- | :-------------------------------------------------------- | :----------------------------------------------------- | :---------------------------------------- | :--------------------------------------------------------------------- |
| **SPTrans Olho Vivo** | JSON (particionado por `date=YYYY-MM-DD/hour=HH`)         | API REST autenticada por token (`.env`)                | `(vehicle_id, codigoLinha, timestamp_sp)` | [SPTrans Desenvolvedores](https://www.sptrans.com.br/desenvolvedores/) |
| **SPTrans GTFS**      | ZIP / CSVs estáticos                                      | Download estático versionado em `data/reference/gtfs/` | `route_id` / `shape_id`                   | [SPTrans GTFS](https://www.sptrans.com.br/desenvolvedores/)            |
| **CEMADEN PED**       | CSV (por estação `station=ID/req_*.csv`) + JSON cadastral | API REST pública (agendamento histórico + cadastro)    | `(station_id, timestamp_sp)`              | [CEMADEN PED](https://www.gov.br/cemaden/)                             |

---

## 🏗️ Arquitetura dos Dados

A pasta `data/` é organizada da seguinte maneira:

```text
data/
├── provenance.jsonl          # 🧾 Histórico consolidado de proveniência das execuções (versionado no Git)
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

> Cada subpasta de dados (`reference/stations`, `bronze/sptrans`, `bronze/cemaden`, `silver/sptrans`, `silver/cemaden`, `gold`) também mantém seu próprio arquivo incremental `provenance.jsonl` (versionado no Git via exceção no `.gitignore`) registrando origem, saídas, contagem de registros e timestamp UTC de cada execução.

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

Modelagem dimensional declarativa em **dbt Core + DuckDB** (`silver.*` → `gold.*`):

- **`gold.fact_bus_readings`** _(grão: 1 leitura de GPS de veículo em um instante — `vehicle_id`, `route_code`, `observed_at` — 185.065 linhas)_: métricas de velocidade (`speed_kmh`, `speed_diff_from_dry_avg`), flag de qualidade (`speed_flag`), distância até a estação (`distance_to_station_m`) e chuva (`precipitation_mm`, `rain_intensity`, `is_raining`).
- **`gold.dim_lines`** _(1 linha por `route_code` / sentido — 16 linhas)_: código interno da API, `route_id` GTFS, nome da linha (`route_name`), `direction_id` e sentido operacional.
- **`gold.dim_stations`** _(1 linha por `station_id` — 79 estações em SP, 19 mapeadas às rotas)_: nome da estação CEMADEN, cidade, UF, coordenadas e status operacional.
- **`gold.dim_time`** _(1 linha por `hour_key` — 52 horas na última carga)_: data local (`date_sp`), hora (`hour_of_day`), dia da semana ISO (`day_of_week`), `is_weekend` e `is_peak`.
- **`gold.vw_bus_rain_impact`** _(VIEW — 185.065 linhas)_: visão analítica achatada juntando fato e dimensões como fonte única de verdade para o painel consumidor.
- **Integridade validada automaticamente (`dbt test`)**: `0` duplicatas de grão composto, `0` fatos órfãos em `dim_lines`, `dim_stations` e `dim_time`, e `0` velocidades `ok` fora do domínio `[0, 80]` km/h.

---

## 🔍 Defeitos Conhecidos das Fontes

### SPTrans Olho Vivo (`data/bronze/sptrans/`)

- **Leituras duplicadas de GPS**: veículos parados ou com cache na API retornam o mesmo par `(vehicle_id, codigoLinha, timestamp_sp)` em ciclos consecutivos.
- **Timestamps defasados ou futuros**: registros ocasionais com horário muito antigo ou no futuro em relação à janela de coleta.
- **Saltos de GPS e veículos estacionados**: pontos com deslocamento incompatível com velocidade urbana (`> 80 km/h`), intervalos fora da janela válida entre leituras consecutivas (`dt_out_of_range`) ou veículos parados em terminal/ponto (`parked`).

### CEMADEN PED (`data/bronze/cemaden/`)

- **Cadência irregular entre estações**: pluviômetros transmitem a cada 10 minutos quando há chuva e a cada 1 hora quando está seco.
- **Janelas sem transmissão (falha de sensor/comunicação)**: buracos temporais em que a estação não envia registro algum (nem `0.0 mm`).
- **Fuso horário em UTC**: timestamps originais vêm em UTC e precisam ser convertidos para `America/Sao_Paulo` para cruzar com a SPTrans.

---

## 🧹 Decisões de Tratamento (Camada Silver)

Números referentes à última carga consolidada na Gold (`2026-11-22T15:46:49Z` — 38.813 arquivos JSON da SPTrans e 76 CSVs do CEMADEN):

- **Remoção de duplicadas exatas na SPTrans**: de `186.137` leituras brutas, `1.069` duplicadas na chave `(vehicle_id, codigoLinha, timestamp_sp)` e `3` timestamps inválidos (`stale_or_future`) foram removidos, restando **`185.065`** registros na Silver (`0` nulos/inválidos e `0` fora da bounding box de SP).
- **Sinalização em vez de exclusão silenciosa (`speed_flag`)**: extremos e estados não comparáveis são mantidos e classificados explicitamente na Silver/Gold — `123.860` válidos (`ok`), `54.202` parados (`parked`), `5.475` intervalo inválido (`dt_out_of_range`), `1.076` acima de 80 km/h (`too_fast`) e `452` primeiras leituras (`first_reading`). Apenas `speed_flag = 'ok'` entra nos cálculos de velocidade média.
- **Sinalização explícita de ausência de chuva no CEMADEN**: dos `27.005` registros na Silver do CEMADEN (`19` estações), `4.923` são leituras reais e `22.082` são linhas sintéticas com `is_missing = True` para distinguir explicitamente _"não choveu (`0 mm`)"_ de _"sensor sem transmissão"_.

---

## 🧮 Atributos Derivados

| Atributo                  | O que é                                                                                                                                                                                                             | Por que responde à pergunta norteadora                                                   | Quando não é possível calcular                                                                       |
| :------------------------ | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | :--------------------------------------------------------------------------------------- | :--------------------------------------------------------------------------------------------------- |
| `speed_kmh`               | Velocidade instantânea (km/h) calculada a partir da projeção na rota GTFS entre leituras sucessivas do mesmo veículo.                                                                                               | É a variável resposta principal para medir lentidão operacional.                         | Fica nulo quando `speed_flag != 'ok'` (primeira leitura, parado, intervalo inválido ou `> 80 km/h`). |
| `rain_intensity`          | Faixa de intensidade horária (`dry`: `0 mm/h`, `light`: `<= 2,5`, `moderate`: `<= 10`, `heavy`: `> 10`).                                                                                                            | Permite testar se a perda de velocidade cresce proporcionalmente à intensidade da chuva. | Recebe `'unknown'` quando `rain_mm` é nulo (`rain_is_missing = True`).                               |
| `is_raining`              | Indicador booleano (`rain_mm > 0`).                                                                                                                                                                                 | Facilita a comparação direta agregado Seco vs. Chuva.                                    | Fica nulo (`pd.NA`) quando `rain_mm` é ausente.                                                      |
| `dry_baseline_speed_kmh`  | Média hierárquica de velocidade no tempo seco, priorizando mesma linha, sentido, estação, tipo de dia (útil/fim de semana) e hora do dia (com fallback progressivo para grupos mais amplos da mesma linha/sentido). | Isola o efeito da chuva do efeito natural do horário, do sentido e da via.               | Fica nulo se não existir nenhuma leitura seca válida (`speed_flag = 'ok'`) para a linha/sentido.     |
| `speed_diff_from_dry_avg` | Diferença `speed_kmh - dry_baseline_speed_kmh` (km/h).                                                                                                                                                              | Mede diretamente o impacto da chuva em km/h (valores negativos = perda de velocidade).   | Fica nulo quando `speed_flag != 'ok'` ou sem baseline seco disponível.                               |

---

## 🔗 Junção (Enriquecimento Silver)

- **Tabelas e grãos**: SPTrans Silver (`185.065` leituras no grão veículo × linha × instante) à esquerda + CEMADEN Silver agregado para o grão único `(station_id, hour_sp)` (`24.871` chaves horárias) à direita.
- **Chave e tipo de join**: `merge(..., on=["station_id", "hour_sp"], how="left", validate="many_to_one")`.
- **Validação de contagem**: `185.065` linhas antes → `185.065` linhas depois (`0` multiplicações de linha).
- **Análise de casamento**: `927` chaves `(station_id, hour_sp)` casadas, `0` chaves exclusivas da SPTrans (`unmatched_key_rows = 0`) e `23.944` chaves exclusivas do CEMADEN (horas fora da janela ativa dos ônibus). Das `185.065` leituras, `154.082` possuem medição válida de chuva (`dry`: `146.324`, `light`: `6.700`, `moderate`: `665`, `heavy`: `393`) e `30.983` possuem chave casada com falha de transmissão do pluviômetro (`rain_is_missing = True` → `rain_intensity = 'unknown'`).

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

## 📁 Estrutura do Código

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
