# Pipeline CEMADEN: Bronze & Prata

Este documento explica de forma direta como os dados pluviométricos do CEMADEN chegam na camada Bronze e como são transformados na camada Prata.

---

## 1. Camada Bronze (Como os dados chegam)

* **Fonte:** API PED (Plataforma de Coleta de Dados) do CEMADEN, via autenticação JWT e agendamento de jobs de exportação assíncronos para sensores pluviométricos (rede 11, sensor 10).
* **Estações Alvo:** Lista de estações pluviométricas ativas necessárias (`stations_needed`) definidas em `data/bronze/lines/selected_lines.json`.
* **Janela Temporal:** Período correspondente à coleta da SPTrans acrescido de margem de segurança (1 dia antes e 1 dia depois), fatiado em blocos temporais de até 14 dias (`chunk_date_range`).
* **Controle de Estado e Idempotência:** Arquivo de manifesto `data/bronze/cemaden/_manifest.jsonl` registra o ciclo de vida de cada job agendado, evitando requisições duplicadas para períodos já cobertos.
* **Gravação Atômica:** O coletor consulta periodicamente o status do job (`/status`); quando concluído, realiza o download do pacote ZIP, inspeciona o conteúdo e salva o CSV bruto extraído em:
  ```text
  data/bronze/cemaden/station={station_id}/req_{data_inicio}_{data_fim}.csv
  ```

---

## 2. Camada Prata (Como se tornam os dados da Prata)

A transformação da Prata consolida todos os arquivos brutos baixados em uma visão limpa, contínua e sem lacunas temporais:

1. **Leitura e Parsing:** Lê os arquivos CSV brutos de todas as pastas `station=*`. Converte a coluna `datahora` para o fuso horário local (`America/Sao_Paulo`).
2. **Filtragem e Limpeza:**
   - Descarte de valores sentinela da API (-999, -9999, -99).
   - Descarte de valores fisicamente implausíveis para a Grande São Paulo (> 150 mm/h).
   - Remoção de duplicatas por `(station_id, timestamp_sp)`.
3. **Preservação de Granularidade:** Registros reais com medições válidas mantêm seu timestamp exato (preservando minutos) e o valor medido de chuva (`rain_mm`), com a flag `is_missing = False`.
4. **Preenchimento de Horas sem Dados:** Para cada estação, o pipeline identifica quais horas inteiras da janela não receberam nenhuma leitura e gera uma linha artificial para aquela hora com `rain_mm = null` e `is_missing = True`.
5. **Exportação Consolidada:** Gravação enxuta e direta em:
   ```text
   data/silver/cemaden/rain_silver_export.csv
   ```
   *(Colunas: `station_id`, `timestamp_sp`, `rain_mm`, `is_missing`).*
