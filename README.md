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
│   ├── sptrans/              # sptrans_silver_export.csv (velocidade calculada, sentido e estação)
│   └── cemaden/              # rain_silver_export.csv (chuva em mm com marcação de falhas)
│
└── gold/                     # 📊 Camada Analítica final
    └── ...                   # Cruzamento espaço-temporal (Velocidade vs. Precipitação)
```

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
- **CEMADEN Silver (`rain_silver_export.csv`)**:
  - Padroniza os fusos horários para o horário local de São Paulo (`America/Sao_Paulo`).
  - Preserva os minutos reais das leituras (10 min ou 1h) e arredonda os valores de milímetros.
  - Detecta janelas sem dados da estação e insere uma linha sintética indicando falha de comunicação (`is_missing = True`).

---

## 🚀 Como Executar

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

## 📁 Estrutura do Código (`src/`)

```text
src/
├── clients/              # Clientes HTTP com retry e autenticação (SPTrans e CEMADEN)
├── core/                 # Configurações centralizadas e leitura do .env (config.py)
├── pipelines/            # Scripts de execução das camadas
│   ├── bronze/           # Ingestão de dados brutos (select_lines, collect_sptrans, ingest_cemaden)
│   └── silver/           # Transformação e limpeza (build_sptrans_silver, build_cemaden_silver)
└── utils/                # Módulos especializados de apoio
    ├── cemaden_bronze.py # Manipulação de arquivos brutos e requisições da chuva
    ├── cemaden_silver.py # Limpeza, arredondamento e detecção de falhas da chuva
    ├── gtfs_parser.py    # Processamento de arquivos estáticos de transporte
    ├── line_selection.py # Amostragem espacial, métricas e ranking de linhas
    ├── logger.py         # Configuração de logging estruturado
    ├── sptrans_silver.py # Projeção geográfica e cálculo de velocidade de ônibus
    └── station_parser.py # Normalização das estações
```

---

## 📄 Licença

Projeto desenvolvido para fins acadêmicos e de pesquisa na **Universidade Federal de Itajubá (UNIFEI)**.
