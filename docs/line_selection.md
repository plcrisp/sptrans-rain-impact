# Seleção de Linhas e Estações

Este documento descreve a metodologia direta de seleção das 8 linhas de ônibus da SPTrans e o mapeamento com as estações pluviométricas ativas do CEMADEN, implementados no pipeline `src.pipelines.bronze.select_lines`.

---

## 1. Objetivo

Selecionar **8 linhas de ônibus operacionais representativas** com:
- **Alta cobertura pluviométrica** ao longo de todo o trajeto.
- **Diversidade geográfica** (sem sobreposição de estações de chuva).
- **Consistência operacional** (alta frequência, ambos os sentidos operantes e validação em tempo real na API Olho Vivo da SPTrans).

---

## 2. Etapas do Processo

```
[Estações CEMADEN] + [Traçados GTFS]
          │
          ▼
1. Pré-filtragem e Elegibilidade
          │
          ▼
2. Amostragem Espacial (100m) e Cobertura (raio 2km)
          │
          ▼
3. Scoring Multi-Critério e Ranking
          │
          ▼
4. Seleção Diversificada e Validação Olho Vivo (Tempo Real)
          │
          ▼
[selected_lines.json] + [line_station_mapping.parquet]
```

### 2.1 Pré-filtragem de Dados
- **Estações CEMADEN:** Apenas estações com `status == 'Active'` e `station_type == 'Pluviométrica'` no município de São Paulo (64 ativas).
- **Linhas GTFS:** Descarte de linhas com velocidade programada suspeita (`scheduled_speed_suspect`), viagens zeradas em dias úteis e geometrias inválidas. Seleciona-se um único traçado (`shape_id`) por `(route_id, direction_id)` baseado no maior número de viagens.

### 2.2 Amostragem Espacial e Cálculo de Cobertura
- Cada traçado é discretizado em pontos a cada **100 metros** (`EPSG:31983`).
- Para cada ponto, busca-se a estação pluviométrica ativa mais próxima (`sjoin_nearest`).
- Um ponto é considerado coberto se a distância à estação for $\le \mathbf{2.000\text{ m}}$.
- Identifica-se a **estação dominante** de cada rota (aquela que cobre o maior percentual de pontos nos dois sentidos) e a **cobertura mínima** (`coverage_pct_min`).

### 2.3 Ranking Multi-Critério
Cada linha candidata recebe uma pontuação normalizada de 0 a 1:

$$\text{Score} = 0.5 \times \text{coverage\_norm} + 0.3 \times \text{freq\_norm} + 0.2 \times \text{length\_norm}$$

- **`coverage_norm`:** Menor cobertura percentual entre os sentidos da linha.
- **`freq_norm`:** Percentil do volume de partidas em dias úteis (`weekday_trips`).
- **`length_norm`:** Pontuação do comprimento médio da rota (faixa ideal: 8 a 30 km $= 1.0$; decaimento linear para linhas muito curtas ou muito longas).

### 2.4 Seleção Diversificada e Validação em Tempo Real (API Olho Vivo)
Percorrendo o ranking do maior para o menor score, uma linha é selecionada se cumprir:
1. **Cobertura mínima:** $\ge 90\%$ em ambos os sentidos.
2. **Headway pico:** $\le 15\text{ minutos}$.
3. **Sentidos completos:** Ambos os sentidos presentes no GTFS (ida e volta).
4. **Diversidade espacial:**
   - A estação dominante da linha não pode ter sido escolhida por outra linha anterior.
   - A estação dominante deve estar a pelo menos **3.000 metros** de distância de todas as estações dominantes já selecionadas.
5. **Validação na API Olho Vivo:**
   - A linha deve existir e ter correspondência não ambígua de `codigoLinha` e sentido (`sl`) para ambos os itinerários via similaridade textual com o GTFS.
   - Deve possuir **veículos ativos transmitindo GPS no momento da consulta** (`n_vehicles_seen >= 1`).

---

## 3. Artefatos de Saída

| Arquivo | Localização | Descrição |
| :--- | :--- | :--- |
| `selected_lines.json` | `data/bronze/lines/` | Metadados completos das 8 linhas selecionadas, `codigoLinha` de ida/volta, estações necessárias e parâmetros da seleção. |
| `line_station_mapping.parquet` | `data/bronze/lines/` | Tabela de pontos amostrados a cada 100m de todas as linhas escolhidas, com coordenadas (lat/lon), distância e ID da estação pluviométrica mais próxima. |
| `line_ranking.csv` | `data/reports/` | Ranking multi-critério completo de todas as linhas avaliadas e motivos de exclusão. |
| `selected_lines_map.html` | `data/reports/` | Mapa interativo Folium com os trajetos das linhas, buffers de 2km das estações dominantes e estações vizinhas cobertas. |
