# Pipeline SPTrans: Bronze & Prata

Este documento explica de forma direta como os dados de telemetria de ônibus da SPTrans chegam na camada Bronze e como são transformados na camada Prata.

---

## 1. Camada Bronze (Como os dados chegam)

* **Fonte:** API Olho Vivo da SPTrans via endpoint `/Posicao/Linha?codigoLinha={id}`.
* **Alvos:** 16 sentidos (8 linhas selecionadas × 2 sentidos de trajeto) definidos em `data/reference/lines/selected_lines.json`.
* **Cadência e Agendamento:** Coleta periódica a cada 60 segundos com controle de taxa fixa via `time.monotonic()` para evitar atraso cumulativo (*drift*).
* **Deduplicação na Origem:** Cada resposta da API tem seu payload JSON avaliado via hash SHA-256. Se o conteúdo for idêntico ao ciclo anterior daquela linha (ônibus não atualizaram GPS), a gravação é ignorada.
* **Persistência Atômica:** Cada resposta nova é salva em arquivo temporário `.tmp` e renomeada via `os.replace` para sua partição UTC final:
  ```text
  data/bronze/sptrans/date=YYYY-MM-DD/hour=HH/posicao_{codigoLinha}_{timestamp}.json
  ```
* **Envelope de Auditoria:** O arquivo JSON armazena um envelope com metadados (timestamp UTC de coleta, latência da API, quantidade de veículos, linha e sentido) juntamente com o payload bruto retornado (`hr` local e lista `vs` de veículos com GPS e timestamp `ta`).

---

## 2. Camada Prata (Como se tornam os dados da Prata)

A transformação da Prata lê todo o Bronze acumulado e gera uma tabela limpa e tipada com granularidade de **uma linha por leitura de veículo**:

1. **Achatamento (Flattening):** Extrai cada leitura de veículo da lista `vs` mantendo `vehicle_id`, coordenadas (`lat`, `lon`), timestamp GPS (`ta`), identificadores de rota (`codigoLinha`, `route_id`, `direction_id`) e proveniência (`source_file`).
2. **Limpeza Estrutural:** Aplica filtros na ordem estrita de negócio com fechamento contábil:
   - Descarte de nulos ou registros com datas inválidas.
   - Descarte de coordenadas fora dos limites geográficos da cidade de São Paulo (`Config.SP_BBOX`).
   - Descarte de leituras com defasagem excessiva ($collected\_at\_utc - ta > 10\text{ min}$) ou timestamps no futuro.
   - Deduplicação por `(vehicle_id, ta)`, preservando a primeira leitura coletada.
3. **Cálculo de Velocidade:** Ordena por veículo, linha e tempo. Calcula $\Delta t$ e distância em linha reta via fórmula de Haversine em relação à leitura imediatamente anterior do mesmo veículo:
   - $v = \frac{\text{dist\_m}}{\text{dt\_s}} \times 3.6$ (em km/h).
   - Classificação via `speed_flag`: `first_reading` (primeira leitura), `dt_out_of_range` ($\Delta t < 20\text{s}$ ou $> 300\text{s}$), `parked` (veículo parado $\ge 10\text{ min}$ com deslocamento $< 20\text{m}$), `too_fast` ($> 80\text{ km/h}$) ou `ok`.
   - Leituras com problemas de velocidade **não são descartadas**: mantêm `speed_kmh = null` e a flag explicativa para diagnóstico.
4. **Enriquecimento Temporal:** Conversão de `ta` para o fuso local (`America/Sao_Paulo`), extração de `hour`, `weekday`, `is_weekend` (sábado/domingo) e `is_peak` (dias úteis nos horários de pico 06:00–09:00 e 17:00–20:00).
5. **Enriquecimento Espacial:** Via `geopandas.sjoin_nearest` (projeção métrica SIRGAS 2000 / UTM 23S, EPSG:31983), cada ponto é associado ao ponto mais próximo da mesma linha em `line_station_mapping.parquet`, herdando:
   - `station_id`: pluviômetro CEMADEN de referência.
   - `dist_to_station_m`: distância em metros até a estação (para filtros de corte na camada Ouro).
6. **Entrega Final:** Exportação tabular consolidada e arredondada (2 casas decimais) em:
   ```text
   data/silver/sptrans/sptrans_silver_export.csv
   ```
