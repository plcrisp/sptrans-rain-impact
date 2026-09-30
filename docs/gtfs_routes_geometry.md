# Dicionário de Dados: GeoJSON de Traçados e Métricas SPTrans

Este documento descreve as propriedades e regras de enriquecimento do arquivo `routes_geometry.geojson`, gerado pelo módulo `src.utils.gtfs_parser.build_gtfs_dataset` a partir dos dados estáticos do GTFS da SPTrans (`data/bronze/gtfs/`).

---

## 1. Visão Geral

- **Formato:** GeoJSON (FeatureCollection).
- **Projeção espacial:** WGS 84 (`EPSG:4326` / CRS84).
- **Granularidade:** Uma feature para cada `shape_id` ativo do modal ônibus (`route_type == '3'`), totalizando exatamente 2.239 features.
- **Campos:** 26 propriedades escalares + geometria `LineString`.

---

## 2. Dicionário de Propriedades

| Propriedade | Tipo | Exemplo | Descrição |
| :--- | :--- | :--- | :--- |
| `shape_id` | `string` | `"44331"` | Identificador único do traçado geográfico no GTFS. |
| `route_id` | `string` | `"4113-10"` | Identificador interno da linha da SPTrans. |
| `route_short_name` | `string` | `"4113-10"` | Código da linha exibido no letreiro do veículo. |
| `route_long_name` | `string` | `"Gentil De Moura - Pça. Da República"` | Denominação completa do itinerário (origem - destino). |
| `direction_id` | `string` | `"0"` | Sentido da linha (`0` = Ida / Principal, `1` = Volta / Secundário). |
| `trip_headsign` | `string` | `"Pça. Da República"` | Letreiro de destino visível para os passageiros. |
| `length_km` | `float` | `20.87` | Extensão física do traçado em quilômetros, calculada via projeção métrica `EPSG:31983` (UTM 23S) e arredondada a 2 casas decimais. |
| `weekday_trips` | `integer` | `166` | Total estimado de partidas programadas ao longo de um dia útil típico (segunda a sexta-feira). |
| `saturday_trips` | `integer` | `166` | Total estimado de partidas programadas aos sábados. |
| `sunday_trips` | `integer` | `166` | Total estimado de partidas programadas aos domingos. |
| `peak_headway_min` | `float` | `6.0` | Mediana do intervalo entre partidas (headway em minutos) nos períodos de pico em dias úteis, arredondada a 1 casa decimal. |
| `offpeak_headway_min` | `float` | `6.0` | Mediana do intervalo entre partidas (headway em minutos) nos períodos de entrepico em dias úteis, arredondada a 1 casa decimal. |
| `first_departure_weekday` | `string` | `"00:00:00"` | Horário de início da primeira janela de operação programada em dias úteis (`HH:MM:SS`). |
| `last_departure_weekday` | `string` | `"23:59:00"` | Horário de término da última janela de operação programada em dias úteis (`HH:MM:SS`). |
| `origin_stop_id` | `string` | `"3305204"` | Identificador do ponto inicial (menor `stop_sequence`) da viagem representativa. |
| `origin_stop_name` | `string` | `"R. Visc. De Pirajá, 462"` | Nome ou logradouro do ponto inicial. |
| `origin_lat` | `float` | `-23.602308` | Latitude da parada de origem em WGS84 (6 casas decimais). |
| `origin_lon` | `float` | `-46.612888` | Longitude da parada de origem em WGS84 (6 casas decimais). |
| `dest_stop_id` | `string` | `"3305204"` | Identificador do ponto final (maior `stop_sequence`) da viagem representativa. |
| `dest_stop_name` | `string` | `"R. Visc. De Pirajá, 462"` | Nome ou logradouro do ponto final. |
| `dest_lat` | `float` | `-23.602308` | Latitude da parada de destino em WGS84 (6 casas decimais). |
| `dest_lon` | `float` | `-46.612888` | Longitude da parada de destino em WGS84 (6 casas decimais). |
| `n_stops` | `integer` | `72` | Número de paradas registradas ao longo do trajeto da viagem representativa. |
| `scheduled_duration_min` | `float` | `146.00` | Duração programada total da viagem em minutos (horário final de chegada menos horário inicial de partida), arredondada a 2 casas decimais. |
| `scheduled_speed_kmh` | `float` | `8.58` | Velocidade média programada em km/h (`length_km / (scheduled_duration_min / 60)`). É nula (`null`) quando classificada como anômala. |
| `scheduled_speed_suspect` | `boolean` | `false` | Indicador booleano de velocidade anômala (`true` se < 3 km/h, > 80 km/h ou se a duração programada for inválida). |
| `geometry` | `LineString` | - | Geometria vetorial do itinerário construída a partir dos pontos de `shapes.csv`. |

---

## 3. Regras de Negócio e Cálculo

### 3.1 Derivação de Tipos de Dia a partir do `calendar.csv`
Não são utilizados identificadores codificados de forma rígida (*hardcoded*). A classificação dos `service_id` é derivada diretamente das colunas booleanas (`monday` a `sunday`):
- **Dia Útil (`weekday`):** Qualquer `service_id` onde todas as colunas de segunda a sexta-feira sejam iguais a 1 (`monday == 1 & tuesday == 1 & wednesday == 1 & thursday == 1 & friday == 1`).
- **Sábado (`saturday`):** Qualquer `service_id` com `saturday == 1`.
- **Domingo (`sunday`):** Qualquer `service_id` com `sunday == 1`.

*Observação:* Um mesmo `service_id` (como `USD`) atua em todas as categorias simultaneamente.

### 3.2 Contagem de Partidas (`trips_in_window`)
A SPTrans utiliza o arquivo `frequencies.csv` para declarar as viagens baseadas em janelas temporais (`start_time`, `end_time`) e intervalos de headway (`headway_secs`).
O número de partidas de cada janela segue a semântica padrão GTFS:
$$\text{partidas} = \left\lceil \frac{\text{end\_sec} - \text{start\_sec}}{\text{headway\_secs}} \right\rceil$$
- Janelas com `headway_secs <= 0` ou `end_sec <= start_sec` são desconsideradas (contabilizam 0 partidas).

### 3.3 Janelas Horárias de Pico e Entrepico
Para a avaliação dos intervalos de serviço em dias úteis:
- **Pico (`peak_headway_min`):** Janelas com `start_time` dentro dos intervalos `[06:00, 09:00)` ou `[17:00, 20:00)`.
- **Entrepico (`offpeak_headway_min`):** Janelas com `start_time` dentro do intervalo `[10:00, 16:00)`.
A agregação por `shape_id` calcula a **mediana** dos valores de headway encontrados.

### 3.4 Seleção da Viagem Representativa para Terminais e Duração
Cada `shape_id` pode ter viagens modelo associadas em `trips.csv`. Para extrair terminais, paradas e duração programada de forma determinística:
1. Ranqueiam-se os `trip_id` do shape pelo total de viagens operadas em dia útil (maior volume primeiro).
2. O desempate é realizado lexicograficamente por `trip_id` (ordem crescente).
3. A parada de origem corresponde ao menor `stop_sequence` e a de destino ao maior `stop_sequence`.
4. A duração programada é calculada pela diferença entre a chegada na última parada e a saída da primeira parada.

### 3.5 Tratamento de Horários e Horas $\ge 24$
O GTFS permite que viagens que ultrapassam a meia-noite declarem horários como `24:15:00` ou `25:30:00`.
A conversão para segundos é realizada de forma puramente aritmética por decomposição de strings (`HH * 3600 + MM * 60 + SS`), sem uso de bibliotecas de datetime tradicionais que falhariam com horas $\ge 24$.

### 3.6 Velocidade Programada e Filtro de Suspeitas
A velocidade programada representa a velocidade comercial esperada na tabela horária:
$$\text{scheduled\_speed\_kmh} = \frac{\text{length\_km}}{\text{scheduled\_duration\_min} / 60}$$
- Valores fora da faixa operacional plausível $[3, 80]\text{ km/h}$, bem como trajetos com duração $\le 0\text{ min}$, são sinalizados com `scheduled_speed_suspect = true` e têm seu valor numérico convertido para `null` no GeoJSON.

---

## 4. Limitações e Casos Especiais Observados

1. **Linha 626A-10 (`shape_id 82713`):** Cadastrada no GTFS com apenas 1 parada registrada em `stop_times.csv`. Como origem e destino coincidem no mesmo instante (`duration = 0 min`), sua duração programada e velocidade são definidas como `null`, com a flag `scheduled_speed_suspect = true`.
2. **Velocidade Programada Mediana:** A velocidade comercial programada mediana da rede é de aproximadamente $10.68\text{ km/h}$. Esse valor reflete as durações de tabela cheia (mediana de 78 minutos para rotas de 14.4 km), características da operação com tráfego intenso e margens de pontualidade no município de São Paulo.
